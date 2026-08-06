from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

from google import genai
from google.genai import types as genai_types


OPTIONAL_QUALIFICATION_FIELDS = (
    "business_name",
    "business_type",
    "current_shipping_arrangement",
    "current_provider_name",
    "current_shipping_rate",
    "current_problem",
)
COMPANY_DETAIL_FIELDS = ("business_name", "business_type")
PINCODE_FIELDS = ("pickup_pincode", "delivery_pincode")
LOCATION_FIELDS = ("pickup_location", "delivery_location")
ROUTE_FIELDS = (*PINCODE_FIELDS, *LOCATION_FIELDS)
RATE_IMPACTING_FIELDS = (*ROUTE_FIELDS, "dead_weight", "payment_type", "order_value")
STATE_MANAGED_RATE_FIELDS = frozenset(
    {
        *OPTIONAL_QUALIFICATION_FIELDS,
        *ROUTE_FIELDS,
        "current_rate_status",
        "current_rate_basis",
        "dead_weight",
        "weight_unit",
        "payment_type",
        "order_value",
        "order_value_status",
        "qualification_refused_field",
        "pickup_pincode_status",
        "delivery_pincode_status",
        "zone",
    }
)
SUPPORTED_FIELDS = frozenset(
    {
        "conversation_consent",
        "assistance_intent",
        *OPTIONAL_QUALIFICATION_FIELDS,
        *ROUTE_FIELDS,
        "dead_weight",
        "payment_type",
        "order_value",
        "current_rate_basis",
        "monthly_shipments",
        "service",
        "zone",
    }
)
SEMANTIC_FIELDS = frozenset({*OPTIONAL_QUALIFICATION_FIELDS, *LOCATION_FIELDS, "service"})
PROVIDER_ARRANGEMENTS = {"direct courier", "shipping aggregator"}
UNKNOWN_PHRASES = {
    "unknown",
    "not applicable",
    "pata nahi",
    "pata nhi",
    "malum nahi",
    "maalum nahi",
    "nahi pata",
    "nahin pata",
    "i do not know",
    "i don't know",
    "dont know",
    "not sure",
    "पता नहीं",
    "पता नही",
    "मालूम नहीं",
    "मालूम नही",
    "नहीं पता",
    "نہیں پتا",
    "معلوم نہیں",
}
REFUSAL_PHRASES = {
    "not shared",
    "refused",
    "prefer not to share",
    "do not want to share",
    "don't want to share",
    "nahi batana",
    "nahin batana",
    "share nahi karna",
    "share nhi karna",
    "batana nahi chahta",
    "batana nahi chahti",
    "nahi bata sakta",
    "nahi bata sakti",
    "nahin bata sakta",
    "nahin bata sakti",
    "main nahi bata sakta",
    "main nahi bata sakti",
    "ab ye main nahi bata sakta",
    "skip",
    "skip karo",
    "skip kar do",
    "chhod do",
    "chod do",
    "rehne do",
    "leave it",
    "cannot share",
    "can't share",
    "\u091b\u094b\u0921\u093c \u0926\u094b",
    "\u0930\u0939\u0928\u0947 \u0926\u094b",
    "नहीं बता सकता",
    "नहीं बता सकती",
    "नही बता सकता",
    "نہیں بتا سکتا",
    "نہیں بتا سکتی",
}
INVALID_FREE_TEXT_VALUES = UNKNOWN_PHRASES | REFUSAL_PHRASES | {
    "n/a",
    "na",
    "none",
}
ARRANGEMENT_ALIASES = {
    "direct courier": "Direct Courier",
    "courier": "Direct Courier",
    "shipping aggregator": "Shipping Aggregator",
    "aggregator": "Shipping Aggregator",
    "own arrangement": "Own Arrangement",
    "self": "Own Arrangement",
    "other": "Other",
}
PAYMENT_ALIASES = {
    "prepaid": "Prepaid",
    "pre-paid": "Prepaid",
    "paid": "Prepaid",
    "\u092a\u094d\u0930\u0940\u092a\u0947\u0921": "Prepaid",
    "cod": "COD",
    "cash on delivery": "COD",
    "both": "Both",
    "dono": "Both",
    "donon": "Both",
    "dona": "Both",
    "\u0926\u094b\u0928\u093e": "Both",
    "\u0926\u094b\u0928\u094b": "Both",
    "दोनों": "Both",
    "دونوں": "Both",
    "prepaid and cod": "Both",
    "prepaid aur cod": "Both",
    "cod and prepaid": "Both",
    "cod aur prepaid": "Both",
}
APPROVED_ZONES = frozenset({"A", "B", "C", "D", "E", "F"})
_BUSINESS_TYPE_LETTERS = {
    "b": "B",
    "bee": "B",
    "d": "D",
    "day": "D",
    "dee": "D",
    "g": "G",
    "gee": "G",
}
_PAN_INDIA_PATTERN = re.compile(
    r"(?:\bp(?:an|en|ar)[\s-]*india\b|\ball\s+(?:over\s+)?india\b|"
    r"(?:\u092a\u093e\u0928|\u092a\u0948\u0928|\u092a\u0947\u0928|\u092a\u0930)[\s-]*(?:india|\u0907\u0902\u0921\u093f\u092f\u093e)|"
    r"\u0911\u0932\s+(?:\u0913\u0935\u0930\s+)?\u0907\u0902\u0921\u093f\u092f\u093e)",
    re.IGNORECASE,
)
_LOCATION_ALIASES = {
    "delhi": "Delhi",
    "new delhi": "New Delhi",
    "noida": "Noida",
    "greater noida": "Greater Noida",
    "ghaziabad": "Ghaziabad",
    "gurgaon": "Gurugram",
    "gurugram": "Gurugram",
    "faridabad": "Faridabad",
    "mumbai": "Mumbai",
    "bombay": "Mumbai",
    "pune": "Pune",
    "bengaluru": "Bengaluru",
    "bangalore": "Bengaluru",
    "chennai": "Chennai",
    "hyderabad": "Hyderabad",
    "kolkata": "Kolkata",
    "calcutta": "Kolkata",
    "ahmedabad": "Ahmedabad",
    "jaipur": "Jaipur",
    "lucknow": "Lucknow",
    "chandigarh": "Chandigarh",
    "indore": "Indore",
    "bhopal": "Bhopal",
    "patna": "Patna",
    "ranchi": "Ranchi",
    "bhubaneswar": "Bhubaneswar",
    "kochi": "Kochi",
    "guwahati": "Guwahati",
    "srinagar": "Srinagar",
    "jammu": "Jammu",
    "leh": "Leh",
    "port blair": "Port Blair",
    "kerala": "Kerala",
    "दिल्ली": "Delhi",
    "नई दिल्ली": "New Delhi",
    "नोएडा": "Noida",
    "ग्रेटर नोएडा": "Greater Noida",
    "गाजियाबाद": "Ghaziabad",
    "गाज़ियाबाद": "Ghaziabad",
    "गुरुग्राम": "Gurugram",
    "गुड़गांव": "Gurugram",
    "मुंबई": "Mumbai",
    "बॉम्बे": "Mumbai",
    "पुणे": "Pune",
    "बेंगलुरु": "Bengaluru",
    "बैंगलोर": "Bengaluru",
    "बंगलौर": "Bengaluru",
    "चेन्नई": "Chennai",
    "हैदराबाद": "Hyderabad",
    "कोलकाता": "Kolkata",
    "अहमदाबाद": "Ahmedabad",
    "जयपुर": "Jaipur",
    "लखनऊ": "Lucknow",
    "चंडीगढ़": "Chandigarh",
    "इंदौर": "Indore",
    "भोपाल": "Bhopal",
    "पटना": "Patna",
    "रांची": "Ranchi",
    "भुवनेश्वर": "Bhubaneswar",
    "कोच्चि": "Kochi",
    "केरल": "Kerala",
}
_LOCATION_ALTERNATION = "|".join(
    re.escape(alias) for alias in sorted(_LOCATION_ALIASES, key=len, reverse=True)
)
_RATE_REQUEST_PATTERN = re.compile(
    r"(?:\b(?:rate|rates|price|pricing|charge|charges|shipping|delivery)\b|रेट(?:्स)?|कीमत)",
    re.IGNORECASE,
)
_EXPLICIT_RATE_INTENT_PATTERN = re.compile(
    r"(?:\b(?:rate|rates|price|pricing|charge|charges)\b|"
    r"\b(?:shipping|delivery)\s+(?:rate|rates|price|pricing|charge|charges)\b|"
    r"\u0930\u0947\u091f(?:\u094d\u0938)?|\u0915\u0940\u092e\u0924)",
    re.IGNORECASE,
)
_USP_QUERY_PATTERN = re.compile(
    r"(?:\b(?:feature|features|benefit|benefits|advantage|advantages|usp|usps|"
    r"facility|facilities|fayda|fayde|faayda|faayde)\b|\b(?:what|tell me|batao|bata do|bataye)\b.{0,35}"
    r"\bship\s*kia\b|\u092b\u0940\u091a\u0930|\u092b\u093e\u092f\u0926|\u092b\u093e\u092f\u0926\u0947|\u0938\u0941\u0935\u093f\u0927\u093e)",
    re.IGNORECASE,
)
_BROAD_USP_QUERY_PATTERN = re.compile(
    r"(?:\b(?:procedure|process|working|how\s+(?:does|do)\s+(?:ship\s*kia|you)\s+work|"
    r"kaise\s+(?:kaam|work)|kya\s+(?:benefit|fayda|faayda))\b|"
    r"(?:\u092c\u0947\u0928\u093f\u092b\u093f\u091f|\u092a\u094d\u0930\u094b\u0938\u0940\u091c\u0930|\u092a\u094d\u0930\u094b\u0938\u0947\u0938|"
    r"\u0915\u0948\u0938\u0947).{0,35}(?:\u0915\u093e\u092e|\u092b\u093e\u092f\u0926\u093e|\u092c\u0947\u0928\u093f\u092b\u093f\u091f))",
    re.IGNORECASE,
)
_FLAT_ZONAL_RATE_REQUEST_PATTERN = re.compile(
    r"(?:\bflat[\s-]*zonal\s*(?:rate|rates|pricing|charge|charges)?\b|"
    r"\b(?:flat|flatt|flood)[\s,.-]*(?:zone|zonal)\s*(?:rate|rates|pricing|available)?\b|"
    r"\bflat[\s,.-]*(?:2|two|to)?\s*(?:night|nite|nait|tonight)\s*"
    r"(?:rate|rates|pricing|available)\b|"
    r"\bflat\s+(?:channel|chainal|journal)\b|"
    r"\b(?:zonal|donal|jonal)[\s-]*(?:flat|plate|flait)\s*"
    r"(?:rate|rates|pricing|charge|charges)?\b|"
    r"\bzone[\s-]*wise\s+flat\s*(?:rate|rates|pricing|charge|charges)?\b|"
    r"(?:फ्लैट\s*(?:ज़ोनल|जोनल|डोनल)|"
    r"(?:ज़ोनल|जोनल|डोनल|नल)\s*(?:फ्लैट|प्लेट))\s*रेट(?:्स)?)",
    re.IGNORECASE,
)
_ZONAL_RATE_REQUEST_PATTERN = re.compile(
    r"(?:\bzonal\s*(?:rate|rates|pricing|charge|charges)\b|"
    r"\bzone[\s-]*wise\s*(?:rate|rates|pricing|charge|charges)\b)",
    re.IGNORECASE,
)
_FLAT_RATE_REQUEST_PATTERN = re.compile(
    r"(?:\b(?:flat|flatt|flait|flight|fly|plate|letter|slide|blood|blud)\s*(?:rate|rates|to|two|pricing|charge|charges)\b|"
    r"\b(?:platelet|ratrate)\b|"
    r"(?:फ्लैट|फ्लेट|फ्लाइट|प्लेट)\s*(?:रेट|रेट्स)|प्लेटलेट)",
    re.IGNORECASE,
)
_BOTH_CATALOGS_REQUEST_PATTERN = re.compile(
    r"(?:\b(?:both|dono|donon|all|sabhi)\b|\bjo\s+jo\b|दोनों|दोनो|सभी)",
    re.IGNORECASE,
)
_CATALOG_CHOICE_CONTEXT_PATTERN = re.compile(
    r"(?=.*(?:e[\s-]*kart|ekart|ई[\s-]*कार्ट))(?=.*(?:surface|सरफेस))"
    r"(?=.*(?:express|एक्सप्रेस))",
    re.IGNORECASE,
)
_NORMAL_RATE_REQUEST_PATTERN = re.compile(
    r"(?:\bnormal\s*(?:rate|rates|pricing|charge|charges)\b|"
    r"नॉर्मल\s*(?:रेट|रेट्स))",
    re.IGNORECASE,
)
_ONBOARDING_REQUEST_PATTERN = re.compile(
    r"(?:\b(?:on\s*board|onboarding|sign\s*up|signup|register|registration|"
    r"account\s*(?:open|create))\b|ऑनबोर्डिंग)",
    re.IGNORECASE,
)
_SATISFIED_OR_FINISHED_PATTERN = re.compile(
    r"(?:\b(?:satisfied|i am satisfied|rate is fine|rate theek hai|rate thik hai|"
    r"nothing else|no more questions|aur kuch nahi|bas itna hi|that's all|that is all)\b|"
    r"(?:संतुष्ट|सैटिस्फाइड)\s*(?:हूँ|हूं|है)|बस\s+इतना\s+ही|"
    r"और\s+कुछ\s+नहीं)",
    re.IGNORECASE,
)
_EKART_RATE_REQUEST_PATTERN = re.compile(
    r"(?:(?:\be[\s-]*(?:kart|card)\b|\bekart\b|यह\s+कार्ट|ई[\s-]*कार्ट).{0,28}"
    r"(?:\brate(?:s)?\b|रेट(?:्स)?)|(?:\brate(?:s)?\b|रेट(?:्स)?).{0,28}"
    r"(?:\be[\s-]*(?:kart|card)\b|\bekart\b|यह\s+कार्ट|ई[\s-]*कार्ट))",
    re.IGNORECASE,
)
_EKART_SURFACE_PATTERN = re.compile(r"(?:\bsurface\b|सरफेस)", re.IGNORECASE)
_EKART_EXPRESS_PATTERN = re.compile(r"(?:\bexpress\b|एक्सप्रेस)", re.IGNORECASE)
_SHADOWFAX_SURFACE_RATE_PATTERN = re.compile(
    r"(?=.*\b(?:shadow\s*fax|shado\s*fax|shadowfax|shadofax)\b)"
    r"(?=.*(?:\bsurface\b|\brate(?:s)?\b|सरफेस|रेट(?:्स)?))",
    re.IGNORECASE,
)
_ZONE_APPLICABILITY_QUERY_PATTERN = re.compile(
    r"(?:\b(?:which|what|kaun|konsa|kaunsa)\b.{0,35}\bzone\b|"
    r"\bzone\b.{0,35}\b(?:aata|apply|applicable|lagega|hai)\b|"
    r"(?:कौन|कौनसा|कौन\s+से).{0,35}(?:ज़ोन|जोन))",
    re.IGNORECASE,
)
_PROVIDER_OPTIONS_QUERY_PATTERN = re.compile(
    r"(?:\b(?:which|what|other|available|kaun|kon|aur\s+kya|kya\s+kya)\b.{0,55}"
    r"\b(?:courier|provider|service|option)s?\b|"
    r"\b(?:courier|provider|service|option)s?\b.{0,55}"
    r"\b(?:available|option|rate|rates|kaun|kon|bata)\b|"
    r"\b(?:sabke|sabhi|saare|sare|all)\b.{0,25}\b(?:rates?|prices?)\b|"
    r"\b(?:char|chaar|four|panch|paanch|five)\b.{0,25}\brates?\b|"
    r"(?:कौन[\s-]*कौन|और\s+क्या|क्या\s+क्या).{0,55}"
    r"(?:कूरियर|प्रोवाइडर|सर्विस|ऑप्शन)|"
    r"(?:चार|पांच).{0,25}(?:रेट|रेट्स))",
    re.IGNORECASE,
)
_DETAILED_USP_QUERY_PATTERN = re.compile(
    r"\b(?:detail|detailed|poori|puri|pura|complete|all|sabhi|saare|sare|kya\s+kya)\b|"
    r"(?:डिटेल|पूरी|पूरा|सभी|क्या\s+क्या)",
    re.IGNORECASE,
)
_DISSATISFIED_PATTERN = re.compile(
    r"(?:\b(?:not satisfied|satisfied (?:nahi|nahin|nhi)|khush (?:nahi|nahin|nhi)|"
    r"not good|does not work|doesn't work|too high|mehenga|"
    r"mahanga|jyada|zyada|rate achha nahi|rate theek nahi|rate thik nahi|"
    r"rate pasand (?:nahi|nahin|nhi)|"
    r"rates? (?:ka|ki) (?:issue|problem))\b|"
    r"\u0938\u0902\u0924\u0941\u0937\u094d\u091f \u0928\u0939\u0940\u0902|\u092e\u0939\u0902\u0917\u093e|"
    r"सेटिस्फाइड\s+नहीं|रेट.{0,15}(?:ज्यादा|अधिक))",
    re.IGNORECASE,
)
_UNEXPLAINED_PRICING_PATTERN = re.compile(
    r"(?:\b(?:you|aap(?:ne)?)\b.{0,30}\b(?:did(?:n't| not)|nahi|nahin|nhi)\b.{0,20}"
    r"\b(?:explain|bataya|clarify)\b|"
    r"\b(?:comparison|compare|exact\s+prices?|prices?|rates?)\b.{0,35}"
    r"\b(?:explain|clarify|nahi|nahin|nhi)\b|"
    r"\b(?:explain|bataye|bataya)\b.{0,25}\b(?:nahi|nahin|nhi)\b|"
    r"(?:\u0906\u092a\u0928\u0947).{0,35}(?:\u092c\u0924\u093e\u092f\u093e|\u090f\u0915\u094d\u0938\u092a\u094d\u0932\u0947\u0928).{0,20}"
    r"(?:\u0928\u0939\u0940\u0902)|(?:\u0915\u0902\u092a\u0948\u0930\u093f\u091c\u0928|\u092a\u094d\u0930\u093e\u0907\u0938|\u0930\u0947\u091f).{0,30}"
    r"(?:\u092c\u0924\u093e|\u0926\u094b|\u0938\u092e\u091d\u093e))",
    re.IGNORECASE,
)
_ANYTHING_ELSE_QUESTION_PATTERN = re.compile(
    r"(?:aap\s+)?(?:kuch\s+aur|aur\s+kuch)\s+(?:jaan-?na|jaanna|jana|janna|puchna|poochna)\s+"
    r"(?:chahenge|chahte)|anything\s+else\s+(?:you(?:'d|\s+would)?\s+like\s+to\s+know|"
    r"you\s+want\s+to\s+know)|\u0906\u092a.{0,12}\u0915\u0941\u091b\s+\u0914\u0930.{0,12}"
    r"\u091c\u093e\u0928\u0928\u093e",
    re.IGNORECASE,
)
_ANYTHING_ELSE_NO_PATTERN = re.compile(
    r"^(?:no|no\s+thanks?|nahi(?:\s+thank\s*you)?|nahin(?:\s+thank\s*you)?|"
    r"nhi(?:\s+thank\s*you)?|bas|bas\s+itna|aur\s+kuch\s+nahi|"
    r"kuch\s+aur\s+nahi|nothing\s+else|no\s+more|\u0928\u0939\u0940\u0902|"
    r"\u092c\u0938|\u0914\u0930\s+\u0915\u0941\u091b\s+\u0928\u0939\u0940\u0902)[.!?\u0964]*$",
    re.IGNORECASE,
)
_ANYTHING_ELSE_YES_PATTERN = re.compile(
    r"^(?:yes|haan|han|haan\s+ji|ji|bilkul|sure|\u0939\u093e\u0901|\u0939\u093e\u0902|"
    r"\u091c\u0940\s+\u0939\u093e\u0901|\u091c\u0940\s+\u0939\u093e\u0902)[.!?\u0964]*$",
    re.IGNORECASE,
)
_EXPLICIT_WEIGHT_PATTERN = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:kg|kgs|kilogram|kilograms|kilo|kilos|g|gram|grams)\b|"
    r"\d+(?:\.\d+)?\s*(?:\u0915\u093f\u0932\u094b|\u0915\u093f\u0932\u094b\u0917\u094d\u0930\u093e\u092e|\u0917\u094d\u0930\u093e\u092e)",
    re.IGNORECASE,
)
_MOVE_FORWARD_QUESTION_PATTERN = re.compile(
    r"(?:ship\s*kia.{0,35}(?:aage\s+badh|aage\s+bad|move\s+forward|proceed|continue)|"
    r"(?:aage\s+badh|aage\s+bad|move\s+forward|proceed|continue).{0,35}ship\s*kia|"
    r"\u0936\u093f\u092a\s*\u0915\u093f\u092f\u093e.{0,35}\u0906\u0917\u0947\s+\u092c\u0922)",
    re.IGNORECASE,
)
_MOVE_FORWARD_YES_PATTERN = re.compile(
    r"^(?:yes|yes\s+please|haan|han|haan\s+ji|ji\s+haan|bilkul|sure|"
    r"(?:ok|okay)(?:\s+(?:ok|okay))*(?:\s+(?:theek|thik)\s+hai|\s+\u0920\u0940\u0915\s+\u0939\u0948)?|"
    r"(?:theek|thik)\s+hai|\u0920\u0940\u0915\s+\u0939\u0948|"
    r"aage\s+(?:badhna|badna|badho)|karna\s+chahta\s+hoo?n|karna\s+chahti\s+hoo?n|"
    r"\u0939\u093e\u0901|\u0939\u093e\u0902|\u091c\u0940\s+\u0939\u093e\u0901|\u091c\u0940\s+\u0939\u093e\u0902|\u092c\u093f\u0932\u094d\u0915\u0941\u0932)[.!?\u0964]*$",
    re.IGNORECASE,
)
_MOVE_FORWARD_NO_PATTERN = re.compile(
    r"^(?:no|no\s+thanks?|nahi|nahin|nhi|abhi\s+nahi|not\s+now|not\s+interested|"
    r"nahi\s+thank\s*you|nahin\s+thank\s*you|"
    r"\u0928\u0939\u0940\u0902|\u0905\u092d\u0940\s+\u0928\u0939\u0940\u0902|\u0928\u0939\u0940\u0902\s+\u0925\u0948\u0902\u0915\s*\u092f\u0942)[.!?\u0964]*$",
    re.IGNORECASE,
)

_NON_ANSWER_CHATTER_PATTERN = re.compile(
    r"^(?:hello|hallo|helo|hi|hey|haan|han|yes|okay|ok|theek\s+hai|thik\s+hai|"
    r"sun\s+rahe\s+ho|sunai\s+de\s+raha\s+hai|can\s+you\s+hear\s+me)$",
    re.IGNORECASE,
)
_RATE_REPEAT_REQUEST_PATTERN = re.compile(
    r"^(?:how\s+much|kitna|kitne|rate\s+(?:batao|bataye|kya\s+hai)|"
    r"pehle\s+(?:rate\s+)?batao|\u0915\u093f\u0924\u0928\u093e|\u0924\u094b\s+\u092c\u0924\u093e\u0913\s+\u092a\u0939\u0932\u0947)[.!?\u0964]*$",
    re.IGNORECASE,
)
_EXACT_RATE_PATTERN = re.compile(
    r"\b(?:exact|specific|calculate|calculated)\s+(?:rate|price|charge)\b",
    re.IGNORECASE,
)
_BARE_NEGATIONS = {
    "no",
    "nope",
    "nahi",
    "nahin",
    "नहीं",
    "नही",
}
_CURRENT_ARRANGEMENT_NONE_PATTERNS = (
    re.compile(r"\b(?:abhi|currently|right now)?\s*(?:main|mein|hum)?\s*kuchh?(?:\s+bhi|\s+hi)?\s+use\s+n(?:a|ah)i(?:n)?\s+kar"),
    re.compile(r"\b(?:abhi|currently|right now)?\s*(?:main|mein|hum)?\s*koi(?:\s+\w+){0,4}\s+use\s+n(?:a|ah)i(?:n)?\s+kar"),
    re.compile(r"\babhi\s+tak\s+koi(?:\s+\w+){0,4}\s+select\s+n(?:a|ah)i(?:n)?\s+ki"),
    re.compile(r"\b(?:i(?:'m| am)?\s+)?not\s+using\s+(?:anything|any(?:\s+\w+){0,3}|a\s+\w+)"),
    re.compile(
        r"\b(?:abhi|filhaal|abhi\s+tak|main|mein|hum|khud)\b.{0,100}"
        r"\b(?:istemal|istamal|use|upyog)\b.{0,20}\b(?:nahi|nahin|nhi)\b"
    ),
    re.compile(r"\b(?:nothing|none)\b"),
    re.compile(r"\bno\s+current\s+(?:courier|aggregator|provider|shipping\s+(?:solution|arrangement))\b"),
    re.compile(r"\b(?:i\s+am\s+)?not\s+shipping\b.{0,55}\b(?:start|starting|begin|beginning)\b"),
    re.compile(r"\bshipping\b.{0,30}\b(?:nahi|nahin|nhi)\b.{0,55}\b(?:shuru|start)\b"),
    re.compile(r"\u0936\u093f\u092a\u093f\u0902\u0917.{0,30}\u0928\u0939\u0940\u0902.{0,60}(?:\u0936\u0941\u0930\u0942|\u0938\u094d\u091f\u093e\u0930\u094d\u091f)"),
    re.compile(r"^(?:kuch\s+n(?:a|ah)i(?:n)?)(?:\s+kuch\s+n(?:a|ah)i(?:n)?)?[.!?।]*$"),
    re.compile(r"^(?:कुछ\s+नहीं|कुछ\s+नही)(?:\s+(?:कुछ\s+नहीं|कुछ\s+नही))?[.!?।]*$"),
    re.compile(r"(?:अभी|फिलहाल).{0,35}(?:कुछ|कोई).{0,25}(?:यूज|उपयोग).{0,12}(?:नहीं|नही)"),
    re.compile(
        r"(?:\u0905\u092d\u0940|\u092b\u093f\u0932\u0939\u093e\u0932|\u0905\u092d\u0940\s+\u0924\u0915|\u0939\u092e|\u092e\u0948\u0902|\u0916\u0941\u0926)"
        r".{0,100}(?:\u0907\u0938\u094d\u0924\u0947\u092e\u093e\u0932|\u092f\u0942\u091c|\u092f\u0942\u091c\u093c|\u0909\u092a\u092f\u094b\u0917)"
        r".{0,20}\u0928\u0939\u0940\u0902"
    ),
)
_ARRANGEMENT_QUESTION_MARKERS = (
    "shipping arrangement",
    "shipping solution",
    "current shipping",
    "courier",
    "aggregator",
    "provider",
    "shipping ke liye",
    "shipping mein",
    "शिपिंग",
    "कूरियर",
    "एग्रीगेटर",
)


def _arrangement_question_context(previous_agent_text: object) -> bool:
    """Require an actual provider question, not a greeting that mentions couriers."""
    previous = normalize_text(previous_agent_text)
    if not previous or not any(marker in previous for marker in _ARRANGEMENT_QUESTION_MARKERS):
        return False
    return bool(
        re.search(
            r"\b(?:which|what|kaun|kaunsa|konsa|kya)\b.{0,75}"
            r"\b(?:courier|aggregator|provider|shipping\s+(?:arrangement|solution))\b"
            r"|\b(?:courier|aggregator|provider)\b.{0,55}"
            r"\b(?:use|using|used|karte|karti|chala|selected|naam|name)\b"
            r"|(?:\u0915\u094c\u0928|\u0915\u094d\u092f\u093e).{0,75}"
            r"(?:\u0915\u0942\u0930\u093f\u092f\u0930|\u0936\u093f\u092a\u093f\u0902\u0917|\u092a\u094d\u0930\u094b\u0935\u093e\u0907\u0921\u0930)",
            previous,
            re.IGNORECASE,
        )
    )
_NEGATIVE_CONFIRMATION_MARKERS = (
    "select nahi",
    "select nahin",
    "selected nahi",
    "selected nahin",
    "not selected",
    "haven't selected",
    "have not selected",
    "use nahi",
    "use nahin",
    "not using",
    "नहीं चुना",
    "नही चुना",
    "यूज नहीं",
    "यूज नही",
)


def normalize_text(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _evidence_present(evidence: object, customer_text: object) -> bool:
    clean_evidence = normalize_text(evidence)
    clean_customer = normalize_text(customer_text)
    return bool(clean_evidence and clean_evidence in clean_customer)


def _contains_phrase(text: object, phrases: set[str]) -> bool:
    clean = normalize_text(text)
    return any(
        clean == phrase
        or re.search(
            rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])",
            clean,
            re.IGNORECASE,
        )
        for phrase in phrases
    )


def _spoken_business_type(text: object) -> tuple[str, str] | None:
    """Recognize short business-type acronyms despite common realtime ASR spacing."""
    clean = normalize_text(text).rstrip(".!?")
    match = re.fullmatch(
        r"(?:my|mera|mere)?\s*(?:business(?:\s+type)?\s*(?:is|hai)?\s*)?"
        r"(b|bee|d|day|dee|g|gee)\s*(?:2|to)\s*([bc])",
        clean,
    )
    if not match:
        return None
    return f"{_BUSINESS_TYPE_LETTERS[match.group(1)]}2{match.group(2).upper()}", clean


def _conversation_consent(text: object) -> str:
    clean = re.sub(r"[.,!?।]+", " ", normalize_text(text))
    clean = " ".join(clean.split())
    declined = {
        "no",
        "no thanks",
        "nahi",
        "nahin",
        "abhi nahi",
        "not now",
        "busy",
        "call later",
        "अभी नहीं",
        "नहीं",
    }
    accepted = {
        "yes",
        "yes sure",
        "sure",
        "haan",
        "han",
        "haan ji",
        "haan ji bataiye",
        "achchi bataiye",
        "acchi bataiye",
        "achhi bataiye",
        "achha bataiye",
        "acha bataiye",
        "haan boliye",
        "ji haan",
        "ji",
        "ji bataiye",
        "boliye",
        "baat kar sakte hain",
        "we can talk",
        "okay",
        "ok",
        "जी बताइए",
        "बताइए",
        "\u0939\u093e\u0901 \u091c\u0940 \u092c\u0924\u093e\u0907\u090f",
        "\u0939\u093e\u0902 \u091c\u0940 \u092c\u0924\u093e\u0907\u090f",
        "\u0939\u093e\u0901 \u092c\u094b\u0932\u093f\u090f",
        "\u0939\u093e\u0902 \u092c\u094b\u0932\u093f\u090f",
        "जी",
        "हाँ",
        "हां",
        "जी हाँ",
        "जी हां",
    }
    if clean in declined:
        return "Declined"
    if clean in accepted:
        return "Accepted"
    if len(clean.split()) <= 3 and re.search(r"(?:जी|की|कि)?\s*बताइए$", clean):
        return "Accepted"
    return ""


def _current_provider_evidence(text: object, previous_agent_text: object) -> str:
    """Preserve an unfamiliar provider answer without guessing its brand or category."""
    if not _arrangement_question_context(previous_agent_text):
        return ""
    clean = re.sub(r"[.,!?।:;]+", " ", normalize_text(text))
    clean = " ".join(clean.split())
    if (
        not clean
        or clean in _BARE_NEGATIONS
        or _current_arrangement_none_evidence(text, previous_agent_text)
    ):
        return ""
    candidate = re.sub(
        r"^(?:(?:haan|han|ha|yes|ji|haan\s+ji|ha\s+ji)\s+)?"
        r"(?:(?:abhi|currently|right\s+now)\s+)?"
        r"(?:(?:main|mein|hum|i)\s+)?"
        r"(?:(?:use|using|used)\s*(?:kar\s+raha\s+h(?:u|oo)n|kar\s+rahi\s+h(?:u|oo)n|karta\s+h(?:u|oo)n|karte\s+hain|hai)?\s*)?",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip()
    if not candidate or len(candidate) > 80:
        return ""
    generic = {
        "courier",
        "provider",
        "shipping provider",
        "shipping aggregator",
        "aggregator",
        "direct courier",
        "own arrangement",
    }
    return "" if candidate in generic else candidate


def _assistance_intent(text: object, previous_agent_text: object = "") -> str:
    clean = normalize_text(text)
    if _ONBOARDING_REQUEST_PATTERN.search(clean):
        return "Onboarding"
    # A benefits/features side-question is not consent to enter the pricing
    # qualification flow. Answer it and keep the rates/onboarding choice open.
    if (
        _USP_QUERY_PATTERN.search(clean) or _BROAD_USP_QUERY_PATTERN.search(clean)
    ) and not _EXPLICIT_RATE_INTENT_PATTERN.search(clean):
        return ""
    if _EXPLICIT_RATE_INTENT_PATTERN.search(clean):
        return "Rates"
    if re.search(r"\b(?:rash|rush)\s+(?:check|dekh|bata)\b", clean):
        return "Rates"
    previous = normalize_text(previous_agent_text)
    route_reply = bool(
        re.search(
            rf"(?<!\w)({_LOCATION_ALTERNATION})(?!\w)\s+(?:to|se|à¤Ÿà¥‚|à¤¸à¥‡)\s+"
            rf"(?<!\w)({_LOCATION_ALTERNATION})(?!\w)",
            clean,
            re.IGNORECASE,
        )
        or len(re.findall(r"\b\d{6}\b", clean)) >= 2
    )
    if route_reply and any(marker in previous for marker in ("rate", "rates", "onboarding")):
        # Realtime ASR can turn "rate check" into "research" while preserving
        # a clear Delhi-to-Bangalore style route. In direct response to the
        # rates/onboarding choice, an explicit route is unambiguously Rates.
        return "Rates"
    if (
        any(marker in previous for marker in ("rate", "rates", "onboarding"))
        and re.fullmatch(r"(?:read|reads|red|rate|rates)(?:\s+\d+)?[.!?]?", clean)
    ):
        return "Rates"
    if (
        (
            re.search(
                r"\b(?:rate|rates)\b.{0,18}\b(?:check|dekh|bata)(?:\s+karna)?\b",
                clean,
            )
            or re.search(r"\b(?:check|dekh)(?:\s+karna)?\b", clean)
        )
        and any(marker in previous for marker in ("rate", "rates", "onboarding"))
    ):
        return "Rates"
    return ""


def _requested_rate_type(text: object) -> str:
    clean = normalize_text(text)
    if _FLAT_ZONAL_RATE_REQUEST_PATTERN.search(clean):
        return "Flat Zonal"
    if _FLAT_RATE_REQUEST_PATTERN.search(clean):
        return "Flat"
    if _ZONAL_RATE_REQUEST_PATTERN.search(clean):
        return "Zonal"
    if _NORMAL_RATE_REQUEST_PATTERN.search(clean):
        return "Normal"
    return ""


def _pincode_question_target(previous_agent_text: object) -> str:
    """Return the last pincode endpoint mentioned in the agent's actual question."""
    previous = normalize_text(previous_agent_text)
    if "pincode" not in previous and "pin code" not in previous:
        return ""
    matches: list[tuple[int, str]] = []
    for field, pattern in (
        ("pickup_pincode", r"\b(?:pickup|pick[\s-]?up|origin)\b"),
        ("delivery_pincode", r"\b(?:delivery|drop|destination)\b"),
    ):
        matches.extend((match.start(), field) for match in re.finditer(pattern, previous))
    return max(matches)[1] if matches else ""


def _current_arrangement_none_evidence(
    customer_text: object,
    previous_agent_text: object = "",
) -> str:
    clean = normalize_text(customer_text)
    if not clean:
        return ""
    if any(pattern.search(clean) for pattern in _CURRENT_ARRANGEMENT_NONE_PATTERNS):
        return str(customer_text or "").strip()
    if clean.rstrip(".!?।") not in _BARE_NEGATIONS:
        return ""

    previous = normalize_text(previous_agent_text)
    if not _arrangement_question_context(previous_agent_text):
        return ""
    if any(marker in previous for marker in _NEGATIVE_CONFIRMATION_MARKERS):
        return ""
    return str(customer_text or "").strip()


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _weight_kg(value: object, evidence: object) -> float | None:
    if isinstance(value, (int, float)):
        number = float(value)
        clean_evidence = normalize_text(evidence)
        if re.search(r"\b(?:g|gram|grams)\b|\u0917\u094d\u0930\u093e\u092e", clean_evidence):
            number /= 1000
        return number if number > 0 else None

    clean = normalize_text(value)
    match = re.search(
        r"(\d+(?:\.\d+)?)\s*(kg|kgs|kilogram|kilograms|kilo|kilos|g|gram|grams|"
        r"\u0915\u093f\u0932\u094b|\u0915\u093f\u0932\u094b\u0917\u094d\u0930\u093e\u092e|\u0917\u094d\u0930\u093e\u092e)?",
        clean,
    )
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2) or ""
    if not unit:
        evidence_match = re.search(
            r"(\d+(?:\.\d+)?)\s*(kg|kgs|kilogram|kilograms|kilo|kilos|g|gram|grams|"
            r"\u0915\u093f\u0932\u094b|\u0915\u093f\u0932\u094b\u0917\u094d\u0930\u093e\u092e|\u0917\u094d\u0930\u093e\u092e)",
            normalize_text(evidence),
        )
        unit = evidence_match.group(2) if evidence_match else "kg"
    if unit in {"g", "gram", "grams", "\u0917\u094d\u0930\u093e\u092e"}:
        number /= 1000
    return number if number > 0 else None


@dataclass
class FieldState:
    field: str
    status: str
    value: object
    evidence: str
    turn_id: str
    confidence: float
    updated_at: float


class GatedConversationState:
    """Authoritative, evidence-backed state for one LiveKit room."""

    def __init__(
        self,
        *,
        minimum_confidence: float = 0.78,
        v4_strict_flow: bool = False,
        v5_company_pair_flow: bool = False,
    ) -> None:
        self.minimum_confidence = minimum_confidence
        self.v4_strict_flow = v4_strict_flow
        self.v5_company_pair_flow = v5_company_pair_flow
        self.fields: dict[str, FieldState] = {}
        self.optional_ended_by = ""
        self.company_details_ended_by = ""
        self.transitions: list[dict[str, Any]] = []
        self.last_turn_disposition = ""
        self.last_guard_error = ""
        self.general_rate_requested = False
        self.general_rate_evidence = ""
        self.general_rate_turn_id = ""
        self.pan_india_requested = False
        self.route_zone_lookup_status = ""
        self.requested_routes: list[dict[str, str]] = []
        self._resolved_route_keys: set[tuple[tuple[str, str], ...]] = set()
        self._presented_starting_rates: set[str] = set()
        self.authorized_rate_amounts: set[float] = set()
        self.primary_rate_amount: float | None = None
        self.verified_starting_options: list[dict[str, Any]] = []
        self.available_courier_partners: list[str] = []
        self.requested_rate_type = ""
        self.pending_catalogs: set[str] = set()
        self.catalog_choice_turn_ids: set[str] = set()
        self.flat_catalog_presented = False
        self.flat_zonal_catalog_presented = False
        self.flat_zonal_group_totals: dict[str, float] = {}
        self.last_flat_zonal_route_query = False
        self.verified_pricing_path = ""
        self.verified_pricing_tool = ""
        self.verified_payment_basis = ""
        self.customer_satisfied = False
        self.anything_else_question_due = False
        self.anything_else_detail_due = False
        self.anything_else_decision = ""
        self.move_forward_question_due = False
        self.move_forward_decision = ""
        self.onboarding_link_due = False
        self.better_plan_close_due = False
        self.better_plan_close_presented = False
        self.callback_close_concern = ""
        self.onboarding_link_presented = False
        self.ekart_rate_choice_due = False
        self.last_monthly_quantity_captured = False
        self.monthly_quantity_due = False
        self.last_rate_repeat_requested = False
        self.provider_clarification_due = False
        self.last_customer_dissatisfied = False
        self.unsatisfied_problem_due = False
        self.unsatisfied_resolution_due = False
        self.unsatisfied_resolution_presented = False
        self.unsatisfied_concern = ""
        self.last_usp_query = False
        self.last_detailed_usp_query = False
        self.last_provider_options_query = False
        self.last_problem_captured = False
        self.shadowfax_surface_rate_due = False
        self.shadowfax_surface_rate_presented = False
        self.revision = 0

    def seed_context(self, context: dict[str, Any]) -> None:
        mappings = {
            "business_name": ("business_name", "organization", "company_name"),
            "business_type": ("business_type", "shipkia_business_type"),
            "current_shipping_arrangement": (
                "current_shipping_arrangement",
                "shipkia_current_provider_type",
            ),
            "current_provider_name": (
                "current_provider_name",
                "shipkia_current_courier_partner",
            ),
            "current_shipping_rate": (
                "current_shipping_rate",
                "shipkia_current_shipping_rate",
            ),
            "current_rate_basis": ("current_rate_basis", "shipkia_current_rate_basis"),
            "current_problem": ("current_problem", "shipkia_main_pain_point"),
            "pickup_pincode": ("pickup_pincode", "shipkia_pickup_pincode"),
            "delivery_pincode": ("delivery_pincode",),
            "pickup_location": ("pickup_location", "shipkia_pickup_location"),
            "delivery_location": ("delivery_location", "shipkia_delivery_location"),
            "dead_weight": ("dead_weight", "dead_weight_kg"),
            "payment_type": ("payment_type",),
            "order_value": ("order_value",),
            "monthly_shipments": (
                "monthly_shipments",
                "shipkia_monthly_shipments",
            ),
            "service": ("service",),
            "zone": ("zone", "shipping_zone", "approved_zone"),
        }
        for field, keys in mappings.items():
            value = next((context.get(key) for key in keys if context.get(key) not in (None, "")), None)
            if value is None:
                continue
            normalized = self._normalize_answer(field, value, str(value))
            if normalized is None:
                continue
            self._set_field(
                field,
                status="confirmed",
                value=normalized,
                evidence="[CRM context]",
                turn_id="context",
                confidence=1.0,
                source="context",
            )

    def optional_sequence(self) -> tuple[str, ...]:
        sequence = ["business_name", "business_type", "current_shipping_arrangement"]
        arrangement = self.value("current_shipping_arrangement")
        if normalize_text(arrangement) in PROVIDER_ARRANGEMENTS:
            sequence.append("current_provider_name")
        sequence.extend(["current_shipping_rate", "current_problem"])
        return tuple(sequence)

    def pending_field(self) -> str:
        if self.v4_strict_flow:
            if not self.is_handled("conversation_consent"):
                return "conversation_consent"
            if self.value("conversation_consent") == "Declined":
                return ""
            if not self.is_handled("assistance_intent"):
                return "assistance_intent"

        if self.v5_company_pair_flow and (
            self.explicit_zone_requested()
            or self.flat_catalog_due()
            or self.flat_zonal_catalog_due()
            or (
                self.requested_rate_type == "Flat"
                and self.flat_catalog_presented
            )
            or (
                self.requested_rate_type == "Flat Zonal"
                and self.flat_zonal_catalog_presented
            )
            or self.pan_india_requested
        ):
            return ""

        optional_sequence = self.optional_sequence()
        if self.company_details_ended_by:
            optional_sequence = tuple(
                field for field in optional_sequence if field not in COMPANY_DETAIL_FIELDS
            )
        optional_limit = len(optional_sequence)
        if self.optional_ended_by in optional_sequence:
            optional_limit = optional_sequence.index(self.optional_ended_by)
        for field in optional_sequence[:optional_limit]:
            if not self.is_handled(field):
                return field

        needs_route = not (
            self.v4_strict_flow
            and self.requested_rate_type in {"Flat", "Flat Zonal"}
        )
        if needs_route and not self.pan_india_requested:
            for endpoint in ("pickup", "delivery"):
                if not self.route_endpoint_handled(endpoint):
                    return f"{endpoint}_pincode"
        if self.v5_company_pair_flow and needs_route:
            if self.route_zone_lookup_status in {"verified_starting", "unavailable"}:
                return ""
            if self.route_ready_for_lookup():
                return ""
            if self.route_input_unavailable():
                return ""
        if not self.is_handled("dead_weight"):
            return "dead_weight"
        if not self.is_handled("payment_type"):
            return "payment_type"
        if normalize_text(self.value("payment_type")) == "cod" and not self.is_handled("order_value"):
            return "order_value"
        return ""

    def is_handled(self, field: str) -> bool:
        state = self.fields.get(field)
        return bool(
            state
            and state.status in {"confirmed", "refused", "unavailable", "not_applicable"}
        )

    def route_endpoint_handled(self, endpoint: str) -> bool:
        return self.is_handled(f"{endpoint}_pincode") or self.is_confirmed(
            f"{endpoint}_location"
        )

    def route_ready_for_lookup(self) -> bool:
        return self.pan_india_requested or bool(self.next_route_for_lookup()) or all(
            self.is_confirmed(f"{endpoint}_pincode")
            or self.is_confirmed(f"{endpoint}_location")
            for endpoint in ("pickup", "delivery")
        )

    @staticmethod
    def _route_request_key(route: dict[str, object]) -> tuple[tuple[str, str], ...]:
        return tuple(
            (field, normalize_text(route.get(field)))
            for field in ROUTE_FIELDS
            if str(route.get(field) or "").strip()
        )

    def register_requested_route(self, route: dict[str, object]) -> bool:
        normalized = {
            field: str(route.get(field) or "").strip()
            for field in ROUTE_FIELDS
            if str(route.get(field) or "").strip()
        }
        has_pickup = bool(normalized.get("pickup_pincode") or normalized.get("pickup_location"))
        has_delivery = bool(
            normalized.get("delivery_pincode") or normalized.get("delivery_location")
        )
        if not has_pickup or not has_delivery:
            return False
        key = self._route_request_key(normalized)
        if not key or any(self._route_request_key(existing) == key for existing in self.requested_routes):
            return False
        self.requested_routes.append(normalized)
        self.authorized_rate_amounts.clear()
        self.verified_starting_options = []
        self.available_courier_partners = []
        self.pan_india_requested = False
        self.route_zone_lookup_status = ""
        self._append_transition(
            {
                "event": "route_request_registered",
                "route": normalized,
                "route_count": len(self.requested_routes),
                "created_at": time.time(),
            }
        )
        return True

    def next_route_for_lookup(self) -> dict[str, object] | None:
        if self.pan_india_requested:
            return {"pan_india": True}
        for route in self.requested_routes:
            if self._route_request_key(route) not in self._resolved_route_keys:
                return dict(route)
        current: dict[str, object] = {}
        for endpoint in ("pickup", "delivery"):
            pincode_field = f"{endpoint}_pincode"
            location_field = f"{endpoint}_location"
            if self.is_confirmed(pincode_field):
                current[pincode_field] = self.value(pincode_field)
            elif self.is_confirmed(location_field):
                current[location_field] = self.value(location_field)
        has_pickup = bool(current.get("pickup_pincode") or current.get("pickup_location"))
        has_delivery = bool(
            current.get("delivery_pincode") or current.get("delivery_location")
        )
        if not has_pickup or not has_delivery:
            # A partial route is not resolvable. Returning it used to let an
            # eager model call the backend with only one endpoint, which then
            # surfaced as a misleading zone/rate configuration failure.
            return None
        return current if self._route_request_key(current) not in self._resolved_route_keys else None

    def unresolved_route_count(self) -> int:
        return sum(
            self._route_request_key(route) not in self._resolved_route_keys
            for route in self.requested_routes
        )

    def route_input_unavailable(self) -> bool:
        return any(
            self.fields.get(f"{endpoint}_pincode")
            and self.fields[f"{endpoint}_pincode"].status in {"refused", "unavailable"}
            and not self.is_confirmed(f"{endpoint}_location")
            for endpoint in ("pickup", "delivery")
        )

    def is_confirmed(self, field: str) -> bool:
        state = self.fields.get(field)
        return bool(state and state.status == "confirmed")

    def value(self, field: str, default: object = "") -> object:
        state = self.fields.get(field)
        return state.value if state else default

    def apply_classifier_result(
        self,
        result: dict[str, Any],
        *,
        customer_text: str,
        turn_id: str,
        pending_field_at_turn_start: str | None = None,
    ) -> list[dict[str, Any]]:
        decisions = result.get("decisions")
        if not isinstance(decisions, list):
            self.record_guard_error("classifier_decisions_missing", turn_id=turn_id)
            return []

        if pending_field_at_turn_start is None:
            pending_field_at_turn_start = self.pending_field()
            for transition in self.transitions:
                if (
                    transition.get("turn_id") == turn_id
                    and transition.get("event") == "field_updated"
                ):
                    pending_field_at_turn_start = str(
                        transition.get("pending_before") or pending_field_at_turn_start
                    )
                    break

        applied: list[dict[str, Any]] = []
        self.last_turn_disposition = str(result.get("turn_disposition") or "").strip().lower()
        for raw in decisions:
            if not isinstance(raw, dict):
                continue
            field = str(raw.get("field") or "").strip()
            disposition = str(raw.get("disposition") or "").strip().lower()
            confidence = _number(raw.get("confidence"))
            allow_semantic_negative = bool(
                disposition in {"unknown", "refused", "not_applicable"}
                # A negative decision can apply only to the question that was
                # pending when the customer started speaking. Deterministic
                # parsing may already have advanced the state; the same words
                # must never refuse/close that newly opened next field.
                and field == pending_field_at_turn_start
                and confidence is not None
                and confidence >= 0.90
                and self.last_turn_disposition in {"answered", "mixed"}
            )
            transition = self.apply_decision(
                field=field,
                disposition=disposition,
                value=raw.get("value"),
                evidence=str(raw.get("evidence") or ""),
                confidence=confidence,
                customer_text=customer_text,
                turn_id=turn_id,
                allow_semantic_negative=allow_semantic_negative,
            )
            if transition:
                applied.append(transition)
        if applied and self.last_turn_disposition == "unrelated":
            self.last_turn_disposition = "answered"
        if not applied and self.last_turn_disposition not in {"unrelated", "mixed"}:
            self.last_turn_disposition = "unrelated"
        return applied

    def apply_deterministic_answers(
        self,
        customer_text: str,
        *,
        turn_id: str,
        previous_agent_text: str = "",
    ) -> list[dict[str, Any]]:
        """Validate structured answers without relying on the semantic classifier."""
        clean = normalize_text(customer_text)
        if not clean:
            return []

        applied: list[dict[str, Any]] = []
        self.last_monthly_quantity_captured = False
        self.last_flat_zonal_route_query = bool(
            self.v5_company_pair_flow
            and self.flat_zonal_catalog_presented
            and _ZONE_APPLICABILITY_QUERY_PATTERN.search(clean)
        )
        self.last_usp_query = bool(
            self.v5_company_pair_flow
            and (
                _USP_QUERY_PATTERN.search(clean)
                or _BROAD_USP_QUERY_PATTERN.search(clean)
            )
        )
        self.last_detailed_usp_query = bool(
            self.last_usp_query and _DETAILED_USP_QUERY_PATTERN.search(clean)
        )
        self.last_provider_options_query = bool(
            self.v5_company_pair_flow and _PROVIDER_OPTIONS_QUERY_PATTERN.search(clean)
        )
        informational_followup = bool(
            self.last_provider_options_query
            or self.last_usp_query
            or re.search(
                r"\b(?:sabke|saare|sare|all|which|kaun\s+kaun|kitne|total)\b.{0,30}"
                r"\b(?:rates?|prices?|couriers?|providers?|services?|options?)\b",
                clean,
                re.IGNORECASE,
            )
        )
        if self.anything_else_detail_due and (
            informational_followup or len(clean.split()) > 2
        ):
            # A substantive follow-up has arrived. Answer it, then return to
            # the anything-else checkpoint.
            self.anything_else_detail_due = False
            self.anything_else_question_due = True
        self.last_problem_captured = False
        awaiting_unsatisfied_problem = self.unsatisfied_problem_due
        self.last_customer_dissatisfied = bool(
            self.v5_company_pair_flow
            and self.verified_rate_presented()
            and (
                _DISSATISFIED_PATTERN.search(clean)
                or _UNEXPLAINED_PRICING_PATTERN.search(clean)
            )
        )
        captured_unsatisfied_problem = False
        if awaiting_unsatisfied_problem and not self.last_customer_dissatisfied:
            concern = str(customer_text or "").strip(" \t\r\n.,!?;")[:240]
            if concern:
                self.unsatisfied_concern = concern
                self.callback_close_concern = concern
                self.unsatisfied_problem_due = False
                self.unsatisfied_resolution_due = True
                captured_unsatisfied_problem = True
                transition = {
                    "event": "unsatisfied_problem_captured",
                    "evidence": concern,
                    "turn_id": turn_id,
                    "source": "deterministic",
                    "created_at": time.time(),
                }
                self._append_transition(transition)
                applied.append(transition)
        if self.last_customer_dissatisfied and not captured_unsatisfied_problem:
            self.customer_satisfied = False
            self.onboarding_link_due = False
            explicit_move_forward_decline = bool(
                self.move_forward_question_due
                and _MOVE_FORWARD_QUESTION_PATTERN.search(
                    normalize_text(previous_agent_text)
                )
                and re.match(
                    r"^(?:no|nahi|nahin|nhi|\u0928\u0939\u0940\u0902)\b",
                    clean,
                    re.IGNORECASE,
                )
            )
            if not explicit_move_forward_decline:
                self.move_forward_question_due = False
                self.move_forward_decision = ""
            self.better_plan_close_due = False
            explicit_concern = bool(
                _UNEXPLAINED_PRICING_PATTERN.search(clean)
                or re.search(
                    r"\b(?:rate|rates|price|pricing|mehenga|mahanga|jyada|zyada|"
                    r"support|ticket|ndr|rto|delay|delayed|pickup|delivery|claim|"
                    r"problem|issue|dikkat|pareshani)\b",
                    clean,
                )
            )
            known_problem = str(self.value("current_problem") or "").strip()
            if explicit_concern:
                concern = str(customer_text or "").strip(" \t\r\n.,!?;")[:240]
                self.unsatisfied_concern = concern or known_problem or "current concern"
                self.callback_close_concern = self.unsatisfied_concern
                self.unsatisfied_problem_due = False
                self.unsatisfied_resolution_due = True
            elif known_problem:
                self.unsatisfied_concern = known_problem[:240]
                self.callback_close_concern = self.unsatisfied_concern
                self.unsatisfied_problem_due = False
                self.unsatisfied_resolution_due = True
            else:
                self.unsatisfied_concern = ""
                self.callback_close_concern = ""
                self.unsatisfied_problem_due = True
                self.unsatisfied_resolution_due = False
        pending = self.pending_field()
        previous_clean = normalize_text(previous_agent_text)
        self.last_rate_repeat_requested = bool(
            self.v5_company_pair_flow
            and self.verified_rate_presented()
            and self.primary_rate_amount is not None
            and _RATE_REPEAT_REQUEST_PATTERN.fullmatch(clean.rstrip(".!?\u0964"))
        )
        contextual_call_1627_flat_asr = bool(
            self.v5_company_pair_flow
            and re.fullmatch(r"bhojpuri\s+(?:17|seventeen)[.!?]*", clean)
            and (
                self.verified_rate_presented()
                or re.search(r"\b(?:aur\s+kuch|anything\s+else|kuch\s+aur)\b", previous_clean)
            )
        )
        current_rate_question_context = bool(
            pending == "current_shipping_rate"
            or (
                re.search(r"\b(?:rate|rates|price|pricing|charge|charges)\b", previous_clean)
                and re.search(
                    r"\b(?:current|abhi|mil|chal|provider|courier|ship\s*rocket|shipping\s*rocket)\b",
                    previous_clean,
                )
            )
        )

        catalog_choice_context = bool(
            self.v5_company_pair_flow
            and _BOTH_CATALOGS_REQUEST_PATTERN.search(clean)
            and (
                self.ekart_rate_choice_due
                or _CATALOG_CHOICE_CONTEXT_PATTERN.search(normalize_text(previous_agent_text))
            )
        )
        if catalog_choice_context:
            self.pending_catalogs.update({"Flat", "Flat Zonal"})
            self.catalog_choice_turn_ids.add(turn_id)
            self.requested_rate_type = "Flat"
            self.flat_catalog_presented = False
            self.flat_zonal_catalog_presented = False
            self.ekart_rate_choice_due = False
            self._append_transition(
                {
                    "event": "both_rate_catalogs_requested",
                    "evidence": str(customer_text or "").strip(),
                    "turn_id": turn_id,
                    "created_at": time.time(),
                }
            )

        ekart_express_all_groups_context = bool(
            self.v5_company_pair_flow
            and _BOTH_CATALOGS_REQUEST_PATTERN.search(clean)
            and _EKART_RATE_REQUEST_PATTERN.search(clean)
            and _EKART_EXPRESS_PATTERN.search(clean)
        )
        if ekart_express_all_groups_context:
            # "E-Card Express ke dono rates" means both Flat-Zonal zone
            # groups, not Both payment mode and not both pricing catalogs.
            self.catalog_choice_turn_ids.add(turn_id)

        if self.v4_strict_flow and pending == "conversation_consent":
            consent = _conversation_consent(clean)
            if self.v5_company_pair_flow and not consent and (
                _EXPLICIT_RATE_INTENT_PATTERN.search(clean)
                or _ONBOARDING_REQUEST_PATTERN.search(clean)
                or _requested_rate_type(clean)
            ):
                consent = "Accepted"
            if consent:
                transition = self.apply_decision(
                    field=pending,
                    disposition="answered",
                    value=consent,
                    evidence=clean,
                    confidence=1.0,
                    customer_text=customer_text,
                    turn_id=turn_id,
                )
                if transition:
                    transition["source"] = "deterministic"
                    applied.append(transition)
                pending = self.pending_field()

        if self.v4_strict_flow and pending == "assistance_intent":
            intent = _assistance_intent(clean, previous_agent_text)
            if intent:
                transition = self.apply_decision(
                    field=pending,
                    disposition="answered",
                    value=intent,
                    evidence=clean,
                    confidence=1.0,
                    customer_text=customer_text,
                    turn_id=turn_id,
                )
                if transition:
                    transition["source"] = "deterministic"
                    applied.append(transition)
                    if intent == "Rates":
                        requested_rate_type = _requested_rate_type(clean) or "Normal"
                        if requested_rate_type == "Flat" and self.requested_rate_type != "Flat":
                            self.flat_catalog_presented = False
                        if (
                            requested_rate_type == "Flat Zonal"
                            and self.requested_rate_type != "Flat Zonal"
                        ):
                            self.flat_zonal_catalog_presented = False
                        self.requested_rate_type = requested_rate_type
                        if requested_rate_type in {"Flat", "Flat Zonal"}:
                            self.pending_catalogs = {requested_rate_type}
                        else:
                            self.pending_catalogs.clear()

        if (
            self.v4_strict_flow
            and self.value("assistance_intent") == "Onboarding"
            and _assistance_intent(clean, previous_agent_text) == "Rates"
        ):
            transition = self.apply_decision(
                field="assistance_intent",
                disposition="answered",
                value="Rates",
                evidence=str(customer_text or "").strip(),
                confidence=1.0,
                customer_text=customer_text,
                turn_id=turn_id,
            )
            if transition:
                transition["source"] = "deterministic"
                applied.append(transition)
                self.requested_rate_type = _requested_rate_type(clean) or "Normal"
                self.pending_catalogs = (
                    {self.requested_rate_type}
                    if self.requested_rate_type in {"Flat", "Flat Zonal"}
                    else set()
                )

        if self.v4_strict_flow and self.value("conversation_consent") == "Accepted":
            explicit_rate_type = (
                _requested_rate_type(clean)
                or ("Flat" if contextual_call_1627_flat_asr else "")
            )
            same_presented_catalog = bool(
                explicit_rate_type == self.requested_rate_type
                and (
                    explicit_rate_type == "Flat" and self.flat_catalog_presented
                    or explicit_rate_type == "Flat Zonal" and self.flat_zonal_catalog_presented
                )
            )
            if explicit_rate_type and not same_presented_catalog:
                # A new explicit pricing request supersedes the post-answer
                # checkpoint. Otherwise guidance can keep asking "anything
                # else" while the newly requested catalog is still pending.
                self.anything_else_question_due = False
                self.anything_else_detail_due = False
                self.anything_else_decision = ""
                self.move_forward_question_due = False
            if explicit_rate_type == "Flat Zonal":
                if self.requested_rate_type != "Flat Zonal":
                    self.flat_zonal_catalog_presented = False
                self.requested_rate_type = "Flat Zonal"
                self.pending_catalogs = {"Flat Zonal"}
            elif explicit_rate_type == "Flat":
                if self.requested_rate_type != "Flat":
                    self.flat_catalog_presented = False
                self.requested_rate_type = "Flat"
                self.pending_catalogs = {"Flat"}
            elif explicit_rate_type in {"Normal", "Zonal"}:
                self.requested_rate_type = explicit_rate_type
                self.pending_catalogs.clear()

            if self.v5_company_pair_flow:
                ekart_rate_requested = bool(_EKART_RATE_REQUEST_PATTERN.search(clean))
                if (
                    (self.ekart_rate_choice_due or ekart_rate_requested)
                    and _EKART_SURFACE_PATTERN.search(clean)
                ):
                    self.requested_rate_type = "Flat"
                    self.pending_catalogs = {"Flat"}
                    self.flat_catalog_presented = False
                    self.ekart_rate_choice_due = False
                elif (
                    (self.ekart_rate_choice_due or ekart_rate_requested)
                    and _EKART_EXPRESS_PATTERN.search(clean)
                ):
                    self.requested_rate_type = "Flat Zonal"
                    self.pending_catalogs = {"Flat Zonal"}
                    self.flat_zonal_catalog_presented = False
                    self.ekart_rate_choice_due = False
                elif (
                    ekart_rate_requested
                    and not explicit_rate_type
                    and not _EKART_SURFACE_PATTERN.search(clean)
                    and not _EKART_EXPRESS_PATTERN.search(clean)
                ):
                    self.ekart_rate_choice_due = True

                if (
                    explicit_rate_type
                    or _EKART_SURFACE_PATTERN.search(clean)
                    or _EKART_EXPRESS_PATTERN.search(clean)
                ):
                    self.ekart_rate_choice_due = False

                if _SHADOWFAX_SURFACE_RATE_PATTERN.search(clean):
                    self.shadowfax_surface_rate_due = True
                    self.shadowfax_surface_rate_presented = False
                    self.requested_rate_type = "Zonal"
                    self.pending_catalogs.clear()
                    self.ekart_rate_choice_due = False
                    self._append_transition(
                        {
                            "event": "shadowfax_surface_rate_requested",
                            "evidence": str(customer_text or "").strip(),
                            "turn_id": turn_id,
                            "created_at": time.time(),
                        }
                    )

            # Explicit onboarding is a valid mid-call path switch. It must not
            # erase any already-confirmed discovery or route facts.
            if _ONBOARDING_REQUEST_PATTERN.search(clean) and self.is_handled(
                "assistance_intent"
            ):
                transition = self.apply_decision(
                    field="assistance_intent",
                    disposition="answered",
                    value="Onboarding",
                    evidence=str(customer_text or "").strip(),
                    confidence=1.0,
                    customer_text=customer_text,
                    turn_id=turn_id,
                )
                if transition:
                    transition["source"] = "deterministic"
                    applied.append(transition)

        closing_no = bool(
            re.fullmatch(
                r"(?:no|nahi|nahin|nei|नहीं|नही)(?:\s+(?:thank\s*you|thanks|थैंक\s*यू|धन्यवाद))?",
                clean.rstrip(".!?।"),
                re.IGNORECASE,
            )
            and re.search(
                r"(?:aur\s+kuch|anything\s+else|kuch\s+aur|और\s+कुछ)",
                normalize_text(previous_agent_text),
                re.IGNORECASE,
            )
        )
        contextual_satisfaction = bool(
            re.search(
                r"\b(?:samajh gaya|idea mil gaya|got it|all clear)\b|"
                r"समझ\s+गया|आइडिया\s+मिल\s+गया",
                clean,
                re.IGNORECASE,
            )
            and re.search(r"\b(?:thank|thanks)\b|धन्यवाद|थैंक\s*यू", clean, re.IGNORECASE)
        )
        if (
            self.v5_company_pair_flow
            and False  # V5 closes only after the explicit move-forward question.
            and self.verified_rate_presented()
            and (
                _SATISFIED_OR_FINISHED_PATTERN.search(clean)
                or closing_no
                or contextual_satisfaction
            )
            and not _DISSATISFIED_PATTERN.search(clean)
            and not self.onboarding_link_presented
        ):
            self.customer_satisfied = True
            self.onboarding_link_due = True
            self.callback_close_concern = ""
            transition = {
                "event": "customer_satisfied",
                "evidence": str(customer_text or "").strip(),
                "turn_id": turn_id,
                "source": "deterministic",
                "created_at": time.time(),
            }
            self._append_transition(transition)
            applied.append(transition)

        move_forward_answer = " ".join(clean.replace(",", " ").split())
        anything_else_context = bool(
            self.v5_company_pair_flow
            and self.anything_else_question_due
            and _ANYTHING_ELSE_QUESTION_PATTERN.search(previous_clean)
        )
        if anything_else_context and _ANYTHING_ELSE_NO_PATTERN.fullmatch(move_forward_answer):
            self.anything_else_question_due = False
            self.anything_else_detail_due = False
            self.anything_else_decision = "No"
            self.move_forward_question_due = True
            transition = {
                "event": "anything_else_decided",
                "decision": "No",
                "evidence": str(customer_text or "").strip(),
                "turn_id": turn_id,
                "source": "deterministic",
                "created_at": time.time(),
            }
            self._append_transition(transition)
            applied.append(transition)
        elif anything_else_context and _ANYTHING_ELSE_YES_PATTERN.fullmatch(move_forward_answer):
            self.anything_else_question_due = False
            self.anything_else_detail_due = True
            self.anything_else_decision = "Yes"
            self.move_forward_question_due = False
            transition = {
                "event": "anything_else_decided",
                "decision": "Yes",
                "evidence": str(customer_text or "").strip(),
                "turn_id": turn_id,
                "source": "deterministic",
                "created_at": time.time(),
            }
            self._append_transition(transition)
            applied.append(transition)
        move_forward_context = bool(
            self.v5_company_pair_flow
            and self.move_forward_question_due
            and (
                _MOVE_FORWARD_QUESTION_PATTERN.search(previous_clean)
                or (
                    not previous_clean
                    and (
                        _MOVE_FORWARD_YES_PATTERN.fullmatch(move_forward_answer)
                        or _MOVE_FORWARD_NO_PATTERN.fullmatch(move_forward_answer)
                    )
                )
            )
        )
        move_forward_no = bool(
            _MOVE_FORWARD_NO_PATTERN.fullmatch(move_forward_answer)
            or (
                re.match(
                    r"^(?:no|nahi|nahin|nhi|\u0928\u0939\u0940\u0902)\b",
                    move_forward_answer,
                    re.IGNORECASE,
                )
                and (
                    _DISSATISFIED_PATTERN.search(clean)
                    or _UNEXPLAINED_PRICING_PATTERN.search(clean)
                )
            )
        )
        if move_forward_context and _MOVE_FORWARD_YES_PATTERN.fullmatch(move_forward_answer):
            self.customer_satisfied = True
            self.anything_else_question_due = False
            self.anything_else_detail_due = False
            self.move_forward_question_due = False
            self.move_forward_decision = "Yes"
            self.onboarding_link_due = True
            self.better_plan_close_due = False
            self.unsatisfied_problem_due = False
            self.unsatisfied_resolution_due = False
            self.callback_close_concern = ""
            transition = {
                "event": "move_forward_decided",
                "decision": "Yes",
                "evidence": str(customer_text or "").strip(),
                "turn_id": turn_id,
                "source": "deterministic",
                "created_at": time.time(),
            }
            self._append_transition(transition)
            applied.append(transition)
        elif move_forward_context and move_forward_no:
            self.customer_satisfied = False
            self.anything_else_question_due = False
            self.anything_else_detail_due = False
            self.move_forward_question_due = False
            self.move_forward_decision = "No"
            self.onboarding_link_due = False
            self.better_plan_close_due = True
            self.unsatisfied_problem_due = False
            self.unsatisfied_resolution_due = False
            transition = {
                "event": "move_forward_decided",
                "decision": "No",
                "evidence": str(customer_text or "").strip(),
                "turn_id": turn_id,
                "source": "deterministic",
                "created_at": time.time(),
            }
            self._append_transition(transition)
            applied.append(transition)

        quantity_context = bool(
            re.search(
                r"\b(?:monthly|month|per month|shipment quantity|shipment volume|"
                r"shipments|orders)\b|मंथली|महीने|शिपमेंट",
                normalize_text(previous_agent_text),
                re.IGNORECASE,
            )
        )
        quantity_match = re.fullmatch(
            r"(?:around|approximately|approx|lagbhag|करीब|लगभग)?\s*"
            r"(\d[\d,]*)\s*(?:shipments?|orders?|शिपमेंट्स?|ऑर्डर्स?)?\s*[.!?।]*",
            clean,
            re.IGNORECASE,
        )
        volunteered_quantity = re.search(
            r"(?:monthly|per\s+month|har\s+mahine|मंथली|हर\s+महीने)"
            r"[^\d]{0,18}(\d[\d,]*)|"
            r"(\d[\d,]*)\s*(?:shipments?|orders?|शिपमेंट्स?|ऑर्डर्स?)\s*"
            r"(?:monthly|per\s+month|मंथली|हर\s+महीने)",
            clean,
            re.IGNORECASE,
        )
        quantity_text = ""
        if (quantity_context or self.monthly_quantity_due) and quantity_match:
            quantity_text = quantity_match.group(1)
        elif quantity_context:
            # Realtime ASR often renders an approximate range as a full
            # sentence (for example, "200 se 250 shipments month ki"). The
            # upper/last spoken bound is the safest single CRM quantity.
            quantity_numbers = re.findall(r"\d[\d,]*", clean)
            if quantity_numbers:
                quantity_text = quantity_numbers[-1]
        elif volunteered_quantity:
            quantity_text = volunteered_quantity.group(1) or volunteered_quantity.group(2)
        elif self.monthly_quantity_due and re.search(
            r"\b(?:monthly|month|shipments?|orders?)\b|मंथली|महीने|शिपमेंट|ऑर्डर",
            clean,
            re.IGNORECASE,
        ):
            quantity_numbers = re.findall(r"\d[\d,]*", clean)
            if quantity_numbers:
                quantity_text = quantity_numbers[-1]
        if quantity_text:
            quantity_value = int(quantity_text.replace(",", ""))
            if quantity_value > 0:
                transition = self.apply_decision(
                    field="monthly_shipments",
                    disposition="answered",
                    value=quantity_value,
                    evidence=quantity_text,
                    confidence=1.0,
                    customer_text=customer_text,
                    turn_id=turn_id,
                )
                if transition:
                    transition["source"] = "deterministic"
                    self.last_monthly_quantity_captured = True
                    self.monthly_quantity_due = False
                    self.anything_else_question_due = True
                    self.anything_else_detail_due = False
                    self.anything_else_decision = ""
                    self.move_forward_question_due = False
                    applied.append(transition)

        business_name_question_context = bool(
            re.search(
                r"\b(?:business|brand|company)\s+(?:ka\s+|ki\s+|kya\s+)?name\b"
                r"|\bname\s+of\s+(?:your\s+)?(?:business|brand|company)\b",
                previous_clean,
            )
        )
        if business_name_question_context and not self.is_handled("business_name"):
            candidate = str(customer_text or "").strip(" \t\r\n.,!?;")
            candidate_clean = normalize_text(candidate)
            if (
                candidate
                and len(candidate) <= 80
                and not _contains_phrase(candidate_clean, UNKNOWN_PHRASES | REFUSAL_PHRASES)
                and not re.search(r"\b(?:rate|rates|onboarding|pincode|zone)\b", candidate_clean)
                and not re.search(r"\b\d{6}\b", candidate_clean)
            ):
                transition = self.apply_decision(
                    field="business_name",
                    disposition="answered",
                    value=candidate,
                    evidence=candidate,
                    confidence=1.0,
                    customer_text=customer_text,
                    turn_id=turn_id,
                )
                if transition:
                    transition["source"] = "deterministic"
                    applied.append(transition)

        business_type = _spoken_business_type(customer_text)
        if not business_type and re.search(
            r"\b(?:business|b2c|d2c|b2b|type|sector)\b",
            previous_clean,
        ):
            if re.fullmatch(r"(?:a|ay)\s*(?:2|to)\s*b\s*(?:sorry)?[.!?]*", clean):
                business_type = ("B2B", clean)
            elif re.fullmatch(r"(?:j|ji|jee|yes|haan|han)[.!?]*", clean):
                mentioned_types = re.findall(r"\b[bdg]\s*(?:2|to)\s*[bc]\b", previous_clean)
                if len(mentioned_types) == 1:
                    confirmed = _spoken_business_type(mentioned_types[0])
                    if confirmed:
                        business_type = (confirmed[0], clean)
        if business_type:
            value, evidence = business_type
            transition = self.apply_decision(
                field="business_type",
                disposition="answered",
                value=value,
                evidence=evidence,
                confidence=1.0,
                customer_text=customer_text,
                turn_id=turn_id,
            )
            if transition:
                transition["source"] = "deterministic"
                applied.append(transition)

        provider_match = re.search(
            r"\b(?:ship\s*rocket|shipping\s*rocket|shirocket|shiv\s*rakesh|ship\s*rakesh|shiv\s*rocket)\b",
            clean,
        )
        provider_none_evidence = _current_arrangement_none_evidence(
            customer_text,
            previous_agent_text,
        )
        contradictory_provider_answer = bool(provider_match and provider_none_evidence)
        if contradictory_provider_answer:
            self.provider_clarification_due = True
            transition = {
                "event": "provider_clarification_required",
                "evidence": str(customer_text or "").strip(),
                "turn_id": turn_id,
                "created_at": time.time(),
            }
            self._append_transition(transition)
            applied.append(transition)
        if provider_match and not contradictory_provider_answer and (
            pending == "current_shipping_arrangement"
            or re.search(r"\b(?:use|using|used)\b|\u092f\u0942\u091c", clean)
            or _arrangement_question_context(previous_agent_text)
        ):
            self.provider_clarification_due = False
            for field, value in (
                ("current_shipping_arrangement", "Shipping Aggregator"),
                ("current_provider_name", "Shiprocket"),
            ):
                transition = self.apply_decision(
                    field=field,
                    disposition="answered",
                    value=value,
                    evidence=provider_match.group(0),
                    confidence=1.0,
                    customer_text=customer_text,
                    turn_id=turn_id,
                )
                if transition:
                    transition["source"] = "deterministic"
                    applied.append(transition)

        if current_rate_question_context and not self.is_handled("current_shipping_rate"):
            rate_match = re.search(
                r"(?:\b(?:rs\.?|inr|rate(?:\s+is|\s+mil\s+raha\s+hai)?)\s*)?"
                r"(\d+(?:\.\d+)?)\b(?:\s*(?:rupees?|ka))?",
                clean,
                re.IGNORECASE,
            )
            if rate_match:
                rate_value = _number(rate_match.group(1))
                if rate_value is not None and rate_value > 0:
                    transition = self.apply_decision(
                        field="current_shipping_rate",
                        disposition="answered",
                        value=rate_value,
                        evidence=rate_match.group(0).strip() or rate_match.group(1),
                        confidence=1.0,
                        customer_text=customer_text,
                        turn_id=turn_id,
                    )
                    if transition:
                        transition["source"] = "deterministic"
                        applied.append(transition)

        problem_question_context = bool(
            re.search(r"\b(?:problem|issue|challenge|difficulty|dikkat|pareshani)\b", previous_clean)
            and re.search(r"\b(?:provider|courier|shipping|ship\s*rocket)\b", previous_clean)
        )
        if problem_question_context and not self.is_handled("current_problem"):
            problem_evidence = str(customer_text or "").strip(" \t\r\n.,!?;")
            problem_clean = normalize_text(problem_evidence)
            if (
                problem_evidence
                and len(problem_evidence) <= 160
                and not _contains_phrase(problem_clean, UNKNOWN_PHRASES | REFUSAL_PHRASES)
                and not _NON_ANSWER_CHATTER_PATTERN.fullmatch(problem_clean)
            ):
                transition = self.apply_decision(
                    field="current_problem",
                    disposition="answered",
                    value=problem_evidence,
                    evidence=problem_evidence,
                    confidence=1.0,
                    customer_text=customer_text,
                    turn_id=turn_id,
                )
                if transition:
                    transition["source"] = "deterministic"
                    self.last_problem_captured = True
                    applied.append(transition)

        if (
            not contradictory_provider_answer
            and not self.is_handled("current_shipping_arrangement")
        ):
            provider_evidence = _current_provider_evidence(
                customer_text,
                previous_agent_text,
            )
            if provider_evidence:
                for field, value in (
                    ("current_shipping_arrangement", "Other"),
                    ("current_provider_name", provider_evidence),
                ):
                    transition = self.apply_decision(
                        field=field,
                        disposition="answered",
                        value=value,
                        evidence=provider_evidence,
                        confidence=1.0,
                        customer_text=customer_text,
                        turn_id=turn_id,
                    )
                    if transition:
                        transition["source"] = "deterministic"
                        applied.append(transition)

        if pending == "current_shipping_arrangement" and not contradictory_provider_answer:
            none_evidence = _current_arrangement_none_evidence(
                customer_text,
                previous_agent_text,
            )
            if none_evidence:
                self.provider_clarification_due = False
                transition = self.apply_decision(
                    field=pending,
                    disposition="not_applicable",
                    value=None,
                    evidence=none_evidence,
                    confidence=1.0,
                    customer_text=customer_text,
                    turn_id=turn_id,
                    allow_semantic_negative=True,
                )
                if transition:
                    transition["source"] = "deterministic"
                    self.provider_clarification_due = False
                    applied.append(transition)

        # Explicit unknown/refusal maps only to the question currently pending.
        if (
            not applied
            and pending
            in {
                *OPTIONAL_QUALIFICATION_FIELDS,
                *PINCODE_FIELDS,
                "dead_weight",
                "payment_type",
                "order_value",
            }
        ):
            for disposition, phrases in (("refused", REFUSAL_PHRASES), ("unknown", UNKNOWN_PHRASES)):
                evidence = next(
                    (
                        phrase
                        for phrase in sorted(phrases, key=len, reverse=True)
                        if _contains_phrase(clean, {phrase})
                    ),
                    "",
                )
                if not evidence:
                    continue
                transition = self.apply_decision(
                    field=pending,
                    disposition=disposition,
                    value=None,
                    evidence=evidence,
                    confidence=1.0,
                    customer_text=customer_text,
                    turn_id=turn_id,
                )
                if transition:
                    transition["source"] = "deterministic"
                    applied.append(transition)
                break

        zone_match = re.search(
            r"\b(?:zone[\s:-]*([a-f])|([a-f])[\s:-]*zone)\b",
            clean,
            re.IGNORECASE,
        )
        hindi_zone_match = re.search(
            r"(?:\u091c\u093c\u094b\u0928|\u091c\u094b\u0928)[\s:-]*"
            r"(\u090f\u092b|\u090f|\u092c\u0940|\u0938\u0940|\u0921\u0940|\u0908)",
            clean,
        )
        if zone_match or hindi_zone_match:
            evidence = (zone_match or hindi_zone_match).group(0)
            if zone_match:
                zone_value = zone_match.group(1) or zone_match.group(2)
            else:
                zone_value = {
                    "\u090f": "A",
                    "\u092c\u0940": "B",
                    "\u0938\u0940": "C",
                    "\u0921\u0940": "D",
                    "\u0908": "E",
                    "\u090f\u092b": "F",
                }[hindi_zone_match.group(1)]
            transition = self.apply_decision(
                field="zone",
                disposition="answered",
                value=zone_value,
                evidence=evidence,
                confidence=1.0,
                customer_text=customer_text,
                turn_id=turn_id,
            )
            if transition:
                transition["source"] = "deterministic"
                applied.append(transition)

        if (not self.v4_strict_flow or self.v5_company_pair_flow) and _PAN_INDIA_PATTERN.search(clean) and (
            _RATE_REQUEST_PATTERN.search(clean)
            or self.requested_rate_type in {"Normal", "Zonal"}
            or self.value("assistance_intent") == "Rates"
        ):
            self.pan_india_requested = self.v5_company_pair_flow
            self.general_rate_requested = not self.v5_company_pair_flow
            self.general_rate_evidence = str(customer_text or "").strip()
            self.general_rate_turn_id = turn_id
            self._presented_starting_rates.discard("zone:A")
            transition = {
                "event": "pricing_mode_updated",
                "pricing_mode": (
                    "pan_india_zone_a_starting" if self.v5_company_pair_flow else "general_starting"
                ),
                "trigger_field": "pan_india",
                "evidence": self.general_rate_evidence,
                "turn_id": turn_id,
                "source": "deterministic",
                "created_at": time.time(),
            }
            self._append_transition(transition)
            applied.append(transition)
        elif self.general_rate_requested and _EXACT_RATE_PATTERN.search(clean):
            self.general_rate_requested = False
            self.pan_india_requested = False
            self.general_rate_evidence = ""
            self.general_rate_turn_id = ""
            transition = {
                "event": "pricing_mode_updated",
                "pricing_mode": "pending",
                "trigger_field": "exact_rate_requested",
                "evidence": str(customer_text or "").strip(),
                "turn_id": turn_id,
                "source": "deterministic",
                "created_at": time.time(),
            }
            self._append_transition(transition)
            applied.append(transition)

        labelled_pins: dict[str, str] = {}
        for field, label in (
            ("pickup_pincode", r"(?:pickup|pick[\s-]?up|origin)"),
            ("delivery_pincode", r"(?:delivery|drop|destination)"),
        ):
            match = re.search(rf"\b{label}\b[^\d]{{0,25}}(\d{{6}})\b", clean)
            if match:
                labelled_pins[field] = match.group(1)

        pin_values = list(dict.fromkeys(re.findall(r"\b\d{6}\b", clean)))
        assignments = dict(labelled_pins)
        unused = [pin for pin in pin_values if pin not in assignments.values()]
        previous = normalize_text(previous_agent_text)
        if len(pin_values) == 1 and not assignments:
            question_target = _pincode_question_target(previous_agent_text)
            if question_target and not self.is_confirmed(question_target):
                assignments[question_target] = pin_values[0]
            elif pending in PINCODE_FIELDS and not self.is_confirmed(pending):
                assignments[pending] = pin_values[0]
            unused = [pin for pin in unused if pin not in assignments.values()]
        if pending in PINCODE_FIELDS or len(pin_values) >= 2:
            for field in PINCODE_FIELDS:
                if field in assignments or not unused:
                    continue
                if not self.is_confirmed(field):
                    assignments[field] = unused.pop(0)
        for field, value in assignments.items():
            transition = self.apply_decision(
                field=field,
                disposition="answered",
                value=value,
                evidence=value,
                confidence=1.0,
                customer_text=customer_text,
                turn_id=turn_id,
            )
            if transition:
                transition["source"] = "deterministic"
                applied.append(transition)

        location_routes = list(re.finditer(
            rf"(?<!\w)({_LOCATION_ALTERNATION})(?!\w)\s+(?:to|se|टू|से)\s+"
            rf"(?<!\w)({_LOCATION_ALTERNATION})(?!\w)",
            clean,
            re.IGNORECASE,
        ))
        location_assignments: dict[str, str] = {}
        if location_routes:
            if current_rate_question_context:
                rate_basis = "; ".join(match.group(0) for match in location_routes)
                transition = self.apply_decision(
                    field="current_rate_basis",
                    disposition="answered",
                    value=rate_basis,
                    evidence=rate_basis,
                    confidence=1.0,
                    customer_text=customer_text,
                    turn_id=turn_id,
                )
                if transition:
                    transition["source"] = "deterministic"
                    applied.append(transition)
                location_route = None
            else:
                for route_match in location_routes:
                    self.register_requested_route(
                        {
                            "pickup_location": _LOCATION_ALIASES[
                                route_match.group(1).casefold()
                            ],
                            "delivery_location": _LOCATION_ALIASES[
                                route_match.group(2).casefold()
                            ],
                        }
                    )
                location_route = location_routes[0]
                location_assignments = {
                    "pickup_location": _LOCATION_ALIASES[location_route.group(1).casefold()],
                    "delivery_location": _LOCATION_ALIASES[location_route.group(2).casefold()],
                }
        elif pending in PINCODE_FIELDS:
            location_route = None
            whole_location = _LOCATION_ALIASES.get(clean)
            if whole_location:
                location_assignments[
                    "pickup_location" if pending == "pickup_pincode" else "delivery_location"
                ] = whole_location
        for field, value in location_assignments.items():
            transition = self.apply_decision(
                field=field,
                disposition="answered",
                value=value,
                evidence=location_route.group(1 if field == "pickup_location" else 2)
                if location_route
                else customer_text,
                confidence=1.0,
                customer_text=customer_text,
                turn_id=turn_id,
            )
            if transition:
                transition["source"] = "deterministic"
                applied.append(transition)

        if len(pin_values) >= 2:
            self.register_requested_route(
                {
                    "pickup_pincode": assignments.get("pickup_pincode", ""),
                    "delivery_pincode": assignments.get("delivery_pincode", ""),
                }
            )

        weight_match = _EXPLICIT_WEIGHT_PATTERN.search(clean)
        if weight_match:
            evidence = weight_match.group(0)
            transition = self.apply_decision(
                field="dead_weight",
                disposition="answered",
                value=evidence,
                evidence=evidence,
                confidence=1.0,
                customer_text=customer_text,
                turn_id=turn_id,
            )
            if transition:
                transition["source"] = "deterministic"
                applied.append(transition)

        payment_value = ""
        payment_evidence = ""
        for phrase in sorted(PAYMENT_ALIASES, key=len, reverse=True):
            if _contains_phrase(clean, {phrase}):
                payment_value = PAYMENT_ALIASES[phrase]
                payment_evidence = phrase
                break
        if payment_value and turn_id not in self.catalog_choice_turn_ids:
            transition = self.apply_decision(
                field="payment_type",
                disposition="answered",
                value=payment_value,
                evidence=payment_evidence,
                confidence=1.0,
                customer_text=customer_text,
                turn_id=turn_id,
            )
            if transition:
                transition["source"] = "deterministic"
                applied.append(transition)

        value_match = re.search(
            r"\b(?:order\s*(?:value|amount)|cod\s*value|value)\b[^\d]{0,15}"
            r"(\d+(?:\.\d+)?)\b",
            clean,
        )
        if not value_match and self.pending_field() == "order_value":
            value_match = re.fullmatch(r"(?:rs\.?|inr|₹)?\s*(\d+(?:\.\d+)?)", clean)
        if value_match:
            numeric_value = _number(value_match.group(1))
            if numeric_value and numeric_value > 0:
                evidence = value_match.group(0)
                transition = self.apply_decision(
                    field="order_value",
                    disposition="answered",
                    value=numeric_value,
                    evidence=evidence,
                    confidence=1.0,
                    customer_text=customer_text,
                    turn_id=turn_id,
                )
                if transition:
                    transition["source"] = "deterministic"
                    applied.append(transition)

        return applied

    def apply_decision(
        self,
        *,
        field: str,
        disposition: str,
        value: object,
        evidence: str,
        confidence: object,
        customer_text: str,
        turn_id: str,
        allow_semantic_negative: bool = False,
    ) -> dict[str, Any] | None:
        field = field.strip()
        disposition = disposition.strip().lower()
        numeric_confidence = _number(confidence)
        if (
            field not in SUPPORTED_FIELDS
            or disposition not in {"answered", "unknown", "refused", "not_applicable"}
            or numeric_confidence is None
            or numeric_confidence < self.minimum_confidence
            or not _evidence_present(evidence, customer_text)
        ):
            return None

        if (
            field == "current_shipping_rate"
            and disposition == "answered"
            and _EXPLICIT_WEIGHT_PATTERN.search(normalize_text(evidence))
        ):
            # A spoken shipment weight such as "3.5 kilo" is not the
            # customer's current courier price, even if the semantic model
            # proposes the same numeric value for current_shipping_rate.
            return None

        pending_before = self.pending_field()
        if self.provider_clarification_due and field in {
            "current_shipping_arrangement",
            "current_provider_name",
        }:
            # A single contradictory turn ("nothing currently, but Shiprocket")
            # cannot choose either branch. Only the customer's clarification on
            # a later turn may advance provider discovery.
            return None
        if field == "current_problem" and disposition == "answered":
            if any(
                transition.get("turn_id") == turn_id
                and transition.get("field") == "monthly_shipments"
                for transition in self.transitions
            ):
                return None
        if field in {"current_provider_name", "service"} and _PROVIDER_OPTIONS_QUERY_PATTERN.search(
            normalize_text(customer_text)
        ):
            # "Which providers/options are available?" is a product question,
            # never the customer's current provider name or selected service.
            return None
            existing_problem = self.fields.get("current_problem")
            explicit_problem_language = bool(
                re.search(
                    r"\b(?:problem|issue|challenge|difficulty|dikkat|pareshani|rto|ndr|"
                    r"return|returns|delay|support|lost|damage|order.{0,20}dikkat)\b|"
                    r"\u092a\u094d\u0930\u0949\u092c\u094d\u0932\u092e|\u0926\u093f\u0915\u094d\u0915\u0924|\u092a\u0930\u0947\u0936\u093e\u0928\u0940",
                    normalize_text(customer_text),
                    re.IGNORECASE,
                )
            )
            if (
                existing_problem
                and existing_problem.turn_id != turn_id
                and pending_before != "current_problem"
                and not explicit_problem_language
            ):
                return None
        if turn_id in self.catalog_choice_turn_ids and field in {"payment_type", "service"}:
            # In an E-Kart Surface-vs-Express choice, "dono/both" means both
            # pricing catalogs. It must never overwrite payment mode or service.
            return None
        if (
            self.v4_strict_flow
            and pending_before in {"conversation_consent", "assistance_intent"}
            and field != pending_before
        ):
            # Preserve explicit positive facts even if the realtime model asked
            # a later question before the authoritative early gate advanced.
            # The early pending field still controls the next question, but a
            # clear answer must not be discarded and asked again later.
            # Negative spillover remains fail-closed.
            if disposition != "answered" or field not in OPTIONAL_QUALIFICATION_FIELDS:
                return None
        if disposition == "answered":
            normalized = self._normalize_answer(field, value, evidence)
            if normalized is None:
                return None
            status = "confirmed"
        else:
            if disposition == "not_applicable":
                company_detail_not_applicable = (
                    self.v5_company_pair_flow and field in COMPANY_DETAIL_FIELDS
                )
                if field != "current_shipping_arrangement" and not company_detail_not_applicable:
                    return None
                phrases_match = (
                    bool(
                        re.search(
                            r"\b(?:not[\s-]*applicable|n/?a|apply nahi|applicable nahi)\b",
                            normalize_text(evidence),
                        )
                    )
                    if company_detail_not_applicable
                    else bool(_current_arrangement_none_evidence(evidence))
                )
            else:
                phrases = UNKNOWN_PHRASES if disposition == "unknown" else REFUSAL_PHRASES
                phrases_match = _contains_phrase(evidence, phrases)
            if not phrases_match and not allow_semantic_negative:
                return None
            if disposition == "not_applicable":
                status = "not_applicable"
                normalized = (
                    "Not Applicable"
                    if field in COMPANY_DETAIL_FIELDS
                    else "No Current Arrangement"
                )
            elif field in {*PINCODE_FIELDS, "dead_weight"}:
                status = "unavailable"
                normalized = "Unavailable"
            else:
                status = "refused"
                normalized = "Unknown" if disposition == "unknown" else "Not Shared"

        previous = self.fields.get(field)
        if previous and previous.turn_id == turn_id and previous.value != normalized:
            if (
                field == "current_problem"
                and disposition == "answered"
                and previous.status == "confirmed"
            ):
                previous_problem = str(previous.value or "").strip()
                new_problem = str(normalized or "").strip()
                if normalize_text(new_problem) not in normalize_text(previous_problem):
                    normalized = f"{previous_problem}; {new_problem}"
                    evidence = f"{previous.evidence}; {evidence}"
                else:
                    return None
            else:
                # Deterministic parsing owns every same-turn fact it has
                # already confirmed. A later semantic decision must not replace
                # it or turn it into Unknown/Not Shared. Multiple explicit
                # problem statements are merged above instead of overwritten.
                return None
        if (
            field in PINCODE_FIELDS
            and previous
            and previous.status == "confirmed"
            and previous.value != normalized
        ):
            clean_customer = normalize_text(customer_text)
            label = (
                r"\b(?:pickup|pick[\s-]?up|origin)\b"
                if field == "pickup_pincode"
                else r"\b(?:delivery|drop|destination)\b"
            )
            correction_marked = bool(
                re.search(label, clean_customer)
                or re.search(r"\b(?:change|changed|correct|correction|new|instead|actually)\b", clean_customer)
            )
            if not correction_marked:
                return None
        if field in LOCATION_FIELDS:
            endpoint = "pickup" if field == "pickup_location" else "delivery"
            confirmed_pin = self.fields.get(f"{endpoint}_pincode")
            if confirmed_pin and confirmed_pin.status == "confirmed":
                location_correction_marked = bool(
                    re.search(
                        r"\b(?:change|changed|correct|correction|new|instead|actually)\b",
                        normalize_text(customer_text),
                    )
                )
                explicit_complete_city_route = bool(
                    re.search(
                        rf"(?<!\w)({_LOCATION_ALTERNATION})(?!\w)\s+(?:to|se)\s+"
                        rf"(?<!\w)({_LOCATION_ALTERNATION})(?!\w)",
                        normalize_text(customer_text),
                        re.IGNORECASE,
                    )
                )
                if (
                    not explicit_complete_city_route
                    and (confirmed_pin.turn_id == turn_id or not location_correction_marked)
                ):
                    # A pincode is the stronger endpoint. Do not let the
                    # semantic location decision from the same utterance erase it.
                    return None
        if (
            previous
            and previous.turn_id == turn_id
            and previous.status == status
            and previous.value == normalized
        ):
            return None

        transition = self._set_field(
            field,
            status=status,
            value=normalized,
            evidence=evidence,
            turn_id=turn_id,
            confidence=float(numeric_confidence),
            source="classifier",
            pending_before=pending_before,
        )
        if disposition == "answered" and field == "current_problem":
            self.last_problem_captured = True
        if (
            self.v5_company_pair_flow
            and field in COMPANY_DETAIL_FIELDS
            and disposition in {"unknown", "refused", "not_applicable"}
        ):
            self.company_details_ended_by = field
            transition["company_detail_pair_ended"] = True
        elif field in OPTIONAL_QUALIFICATION_FIELDS and disposition in {
            "unknown",
            "refused",
            "not_applicable",
        }:
            self.optional_ended_by = field
            transition["optional_sequence_ended"] = True
        elif disposition == "answered" and field == self.optional_ended_by:
            self.optional_ended_by = ""
            transition["optional_sequence_reopened"] = True
        if (
            disposition == "answered"
            and field in RATE_IMPACTING_FIELDS
            and self.general_rate_requested
            and turn_id != self.general_rate_turn_id
        ):
            self.general_rate_requested = False
            self.general_rate_evidence = ""
            self.general_rate_turn_id = ""
            transition["general_starting_cleared"] = True
        return transition

    def record_guard_error(self, error: object, *, turn_id: str) -> None:
        self.last_guard_error = str(error or "semantic_guard_failed")[:240]
        self.last_turn_disposition = "guard_failed"
        self._append_transition(
            {
                "event": "guard_failed",
                "turn_id": turn_id,
                "pending_field": self.pending_field(),
                "error": self.last_guard_error,
                "created_at": time.time(),
            }
        )

    def _normalize_answer(self, field: str, value: object, evidence: str) -> object | None:
        clean = normalize_text(value)
        if field == "conversation_consent":
            return str(value) if value in {"Accepted", "Declined"} else None
        if field == "assistance_intent":
            return str(value) if value in {"Rates", "Onboarding"} else None
        if field in PINCODE_FIELDS:
            return str(value).strip() if re.fullmatch(r"\d{6}", str(value).strip()) else None
        if field == "dead_weight":
            return _weight_kg(value, evidence)
        if field in {"current_shipping_rate", "order_value"}:
            return _number(value)
        if field == "monthly_shipments":
            number = _number(value)
            return int(number) if number is not None and number > 0 else None
        if field == "payment_type":
            return PAYMENT_ALIASES.get(clean)
        if field == "zone":
            zone = clean.removeprefix("zone").strip().upper()
            return zone if zone in APPROVED_ZONES else None
        if field == "current_shipping_arrangement":
            return ARRANGEMENT_ALIASES.get(clean)
        if field == "business_type":
            canonical = re.sub(r"\s+", "", str(value or "")).upper()
            return canonical if re.fullmatch(r"[BDG]2[BC]", canonical) else None
        if field in {
            "business_name",
            "current_provider_name",
            "current_problem",
            "current_rate_basis",
            *LOCATION_FIELDS,
            "service",
        }:
            rendered = str(value or "").strip()
            if not rendered or clean in INVALID_FREE_TEXT_VALUES:
                return None
            if field in LOCATION_FIELDS and clean in {
                "pin",
                "pina",
                "pin code",
                "pincode",
            }:
                return None
            if field in LOCATION_FIELDS and re.fullmatch(r"\d+", clean):
                # Numeric route answers belong to the deterministic pincode
                # fields and must never overwrite them as a free-text city.
                return None
            if field == "current_problem" and not any(char.isalpha() for char in rendered):
                return None
            if field == "service" and (
                not any(char.isalpha() for char in rendered)
                or clean in {"rash", "rush", "rate", "rates", "pricing"}
            ):
                return None
            if field == "service" and clean in {
                "prepaid",
                "cod",
                "cash on delivery",
                "both",
                "dono",
                "donon",
                "dona",
                "दोनों",
                "दोनो",
            }:
                return None
            return rendered
        return None

    def _set_field(
        self,
        field: str,
        *,
        status: str,
        value: object,
        evidence: str,
        turn_id: str,
        confidence: float,
        source: str,
        pending_before: str = "",
    ) -> dict[str, Any]:
        previous = self.fields.get(field)
        endpoint = "pickup" if field.startswith("pickup_") else "delivery"
        alternative_field = ""
        if field in PINCODE_FIELDS:
            alternative_field = f"{endpoint}_location"
        elif field in LOCATION_FIELDS:
            alternative_field = f"{endpoint}_pincode"
        alternative = self.fields.get(alternative_field) if alternative_field else None
        route_changed = field in ROUTE_FIELDS and (
            (previous and (previous.status != status or previous.value != value))
            or (status == "confirmed" and alternative is not None)
        )
        if route_changed:
            self.route_zone_lookup_status = ""
            self.authorized_rate_amounts.clear()
            self.primary_rate_amount = None
            self.verified_starting_options = []
            self.available_courier_partners = []
            self._presented_starting_rates.discard("general")
            for approved_zone in APPROVED_ZONES:
                self._presented_starting_rates.discard(f"zone:{approved_zone}")
            resolved_zone = self.fields.get("zone")
            if resolved_zone and resolved_zone.evidence == "[verified route resolver]":
                self.fields.pop("zone", None)
        if field in ROUTE_FIELDS and status == "confirmed" and alternative_field:
            self.fields.pop(alternative_field, None)
        state = FieldState(
            field=field,
            status=status,
            value=value,
            evidence=evidence,
            turn_id=turn_id,
            confidence=confidence,
            updated_at=time.time(),
        )
        self.fields[field] = state
        transition = {
            "event": "field_updated",
            "field": field,
            "status": status,
            "value": value,
            "evidence": evidence,
            "turn_id": turn_id,
            "confidence": confidence,
            "source": source,
            "pending_before": pending_before,
            "previous_status": previous.status if previous else "",
            "previous_value": previous.value if previous else None,
            "created_at": state.updated_at,
        }
        self._append_transition(transition)
        return transition

    def _append_transition(self, transition: dict[str, Any]) -> None:
        self.revision += 1
        transition.setdefault("state_revision", self.revision)
        self.transitions.append(transition)
        if len(self.transitions) > 120:
            self.transitions = self.transitions[-120:]

    def rate_arguments(self) -> dict[str, object]:
        arguments: dict[str, object] = {}
        for field in (
            "business_name",
            "business_type",
            "current_shipping_arrangement",
            "current_provider_name",
            "current_problem",
            "current_rate_basis",
            *PINCODE_FIELDS,
            *LOCATION_FIELDS,
            "dead_weight",
            "payment_type",
            "order_value",
            "monthly_shipments",
            "zone",
        ):
            if self.is_confirmed(field):
                arguments[field] = self.value(field)

        if self.is_confirmed("current_shipping_rate"):
            arguments["current_rate_status"] = "Shared"
            arguments["current_shipping_rate"] = self.value("current_shipping_rate")
        if self.optional_ended_by:
            arguments["qualification_refused_field"] = self.optional_ended_by
        for field in PINCODE_FIELDS:
            if self.fields.get(field) and self.fields[field].status == "unavailable":
                arguments[f"{field}_status"] = "Unavailable"
        if self.fields.get("payment_type") and self.fields["payment_type"].status == "refused":
            arguments["payment_type"] = "Not Shared"
        if self.fields.get("order_value") and self.fields["order_value"].status == "refused":
            arguments["order_value_status"] = "Not Shared"
        return arguments

    def pricing_ready(self) -> bool:
        return self.pricing_mode() == "exact"

    def explicit_zone_requested(self) -> bool:
        zone_state = self.fields.get("zone")
        return bool(
            zone_state
            and zone_state.status == "confirmed"
            and zone_state.evidence != "[verified route resolver]"
        )

    def flat_catalog_due(self) -> bool:
        return bool(
            self.v5_company_pair_flow
            and (
                "Flat" in self.pending_catalogs
                or self.requested_rate_type == "Flat" and not self.pending_catalogs
            )
            and not self.flat_catalog_presented
        )

    def flat_zonal_catalog_due(self) -> bool:
        return bool(
            self.v5_company_pair_flow
            and (
                "Flat Zonal" in self.pending_catalogs
                or self.requested_rate_type == "Flat Zonal" and not self.pending_catalogs
            )
            and not self.flat_zonal_catalog_presented
        )

    def mark_flat_catalog_requested(self) -> None:
        """Authorize the direct V5 catalog selected from realtime audio."""
        if not self.v5_company_pair_flow:
            return
        self.requested_rate_type = "Flat"
        self.pending_catalogs.add("Flat")

    def mark_flat_catalog_presented(self) -> None:
        self.flat_catalog_presented = True
        self.pending_catalogs.discard("Flat")
        if "Flat Zonal" in self.pending_catalogs:
            self.requested_rate_type = "Flat Zonal"
        self._append_transition(
            {
                "event": "flat_catalog_presented",
                "created_at": time.time(),
            }
        )

    def mark_flat_zonal_catalog_presented(
        self,
        zone_groups: list[dict[str, Any]] | None = None,
    ) -> None:
        self.flat_zonal_catalog_presented = True
        self.pending_catalogs.discard("Flat Zonal")
        if "Flat" in self.pending_catalogs:
            self.requested_rate_type = "Flat"
        if zone_groups:
            self.flat_zonal_group_totals = {
                str(item.get("zone_group") or "").strip(): float(item["total"])
                for item in zone_groups
                if isinstance(item, dict)
                and str(item.get("zone_group") or "").strip() in {"A-B", "C-F"}
                and isinstance(item.get("total"), (int, float))
            }
        self._append_transition(
            {
                "event": "flat_zonal_catalog_presented",
                "created_at": time.time(),
            }
        )

    def mark_shadowfax_surface_rate_presented(self) -> None:
        if self.shadowfax_surface_rate_presented:
            return
        self.shadowfax_surface_rate_due = False
        self.shadowfax_surface_rate_presented = True
        self._append_transition(
            {
                "event": "shadowfax_surface_rate_presented",
                "created_at": time.time(),
            }
        )

    def verified_rate_presented(self) -> bool:
        return bool(
            self.verified_pricing_tool
            or self._presented_starting_rates
            or self.flat_catalog_presented
            or self.flat_zonal_catalog_presented
        )

    def authorize_rate_result(self, payload: object) -> None:
        """Remember only monetary amounts returned by a successful pricing result."""
        # Keep authorization scoped to the latest successful pricing response.
        # Otherwise an old Flat amount remains speakable after a newer
        # Flat-Zonal result.
        self.authorized_rate_amounts.clear()
        self.primary_rate_amount = None
        self.verified_starting_options = []
        self.available_courier_partners = []
        if isinstance(payload, dict) and str(payload.get("response_type") or "") in {
            "zone_starting",
            "general_starting",
        }:
            amount = _number(payload.get("amount"))
            if amount is not None and amount > 0:
                normalized_amount = round(float(amount), 2)
                self.authorized_rate_amounts.add(normalized_amount)
                self.primary_rate_amount = normalized_amount
            raw_options = payload.get("starting_rate_options")
            if isinstance(raw_options, list):
                for raw_option in raw_options[:5]:
                    if not isinstance(raw_option, dict):
                        continue
                    option_amount = _number(raw_option.get("amount"))
                    courier = str(raw_option.get("courier") or "").strip()
                    service = str(raw_option.get("service") or "").strip()
                    if option_amount is None or option_amount <= 0 or not courier or not service:
                        continue
                    option = {
                        "courier": courier,
                        "service": service,
                        "amount": round(float(option_amount), 2),
                        "weight_slab_g": raw_option.get("weight_slab_g"),
                        "movement_type": raw_option.get("movement_type"),
                        "gst_inclusive": bool(raw_option.get("gst_inclusive")),
                    }
                    self.verified_starting_options.append(option)
                    self.authorized_rate_amounts.add(option["amount"])
            raw_partners = payload.get("available_courier_partners")
            if isinstance(raw_partners, list):
                self.available_courier_partners = [
                    str(partner).strip() for partner in raw_partners if str(partner).strip()
                ]
            return
        monetary_keys = {
            "amount",
            "total",
            "shipping_charge",
            "cod_charge",
            "gst",
            "additional_rate",
        }

        def collect(value: object, key: str = "") -> None:
            if isinstance(value, dict):
                for child_key, child_value in value.items():
                    collect(child_value, str(child_key))
                return
            if isinstance(value, list):
                for child in value:
                    collect(child, key)
                return
            if key not in monetary_keys or isinstance(value, bool):
                return
            number = _number(value)
            if number is not None and number > 0:
                self.authorized_rate_amounts.add(round(float(number), 2))

        collect(payload)
        if isinstance(payload, dict):
            primary_candidates = [
                payload.get("amount"),
                payload.get("total"),
                payload.get("shipping_charge"),
            ]
            starting = payload.get("starting_rate") or payload.get("starting_flat_rate")
            if isinstance(starting, dict):
                primary_candidates.extend(
                    [starting.get("amount"), starting.get("total")]
                )
            for candidate in primary_candidates:
                number = _number(candidate)
                if number is not None and number > 0:
                    self.primary_rate_amount = round(float(number), 2)
                    break

    def rate_claim_amounts_authorized(self, amounts: list[float]) -> bool:
        if not amounts:
            return True
        return bool(self.authorized_rate_amounts) and all(
            any(abs(float(amount) - allowed) < 0.011 for allowed in self.authorized_rate_amounts)
            for amount in amounts
        )

    def mark_onboarding_link_presented(self) -> None:
        if self.onboarding_link_presented:
            return
        self.onboarding_link_presented = True
        self.onboarding_link_due = False
        self._append_transition(
            {
                "event": "onboarding_link_presented",
                "created_at": time.time(),
            }
        )

    def mark_better_plan_close_presented(self) -> None:
        if self.better_plan_close_presented:
            return
        self.better_plan_close_presented = True
        self.better_plan_close_due = False
        self._append_transition(
            {
                "event": "better_plan_close_presented",
                "created_at": time.time(),
            }
        )

    def mark_unsatisfied_resolution_presented(self) -> None:
        if self.unsatisfied_resolution_presented:
            return
        self.unsatisfied_resolution_presented = True
        self.unsatisfied_problem_due = False
        self.unsatisfied_resolution_due = False
        self._append_transition(
            {
                "event": "unsatisfied_resolution_presented",
                "concern": self.unsatisfied_concern,
                "created_at": time.time(),
            }
        )

    def pricing_mode(self) -> str:
        if self.v4_strict_flow:
            if self.value("conversation_consent") == "Declined":
                return "conversation_declined"
            if self.value("assistance_intent") == "Onboarding":
                return "onboarding"
            if self.v5_company_pair_flow and self.explicit_zone_requested():
                return "zone_starting"
            if self.flat_catalog_due():
                return "flat_catalog"
            if self.flat_zonal_catalog_due():
                return "flat_zonal_catalog"
            if (
                self.v5_company_pair_flow
                and self.requested_rate_type == "Flat"
                and self.flat_catalog_presented
            ):
                return "flat_catalog_presented"
            if (
                self.v5_company_pair_flow
                and self.requested_rate_type == "Flat Zonal"
                and self.flat_zonal_catalog_presented
            ):
                return "flat_zonal_catalog_presented"
            if self.v5_company_pair_flow and self.route_zone_lookup_status == "unavailable":
                return "general_starting"
            if self.v5_company_pair_flow and self.route_zone_lookup_status == "verified_starting":
                return "zone_starting"
            if (
                self.v5_company_pair_flow
                and self.route_ready_for_lookup()
                and not self.pending_field()
            ):
                return "route_starting_pending"
            if self.v5_company_pair_flow and self.route_input_unavailable():
                return "general_starting"
            required_fields = (
                ("dead_weight", "payment_type")
                if self.requested_rate_type in {"Flat", "Flat Zonal"}
                else (*PINCODE_FIELDS, "dead_weight", "payment_type")
            )
            if normalize_text(self.value("payment_type")) == "cod":
                required_fields = (*required_fields, "order_value")
            if (
                self.requested_rate_type == "Normal"
                and normalize_text(self.value("payment_type")) == "both"
                and self.is_confirmed("dead_weight")
                and self.pending_field() not in self.optional_sequence()
                and not all(self.is_confirmed(field) for field in PINCODE_FIELDS)
            ):
                return "general_starting"
            if self.pending_field() or any(
                not self.is_confirmed(field) for field in required_fields
            ):
                return "pending"
            return "exact"
        if self.is_confirmed("zone"):
            return "zone_starting"
        if self.general_rate_requested:
            return "general_starting"
        for field in RATE_IMPACTING_FIELDS:
            state = self.fields.get(field)
            if not state or state.status not in {"refused", "unavailable"}:
                continue
            if field == "order_value" and normalize_text(self.value("payment_type")) != "cod":
                continue
            return "general_starting"
        return "exact" if not self.pending_field() else "pending"

    def pricing_trigger_field(self) -> str:
        mode = self.pricing_mode()
        if mode == "zone_starting":
            return "zone"
        if self.general_rate_requested:
            return "pan_india"
        if mode == "general_starting":
            if (
                self.v4_strict_flow
                and normalize_text(self.value("payment_type")) == "both"
            ):
                return "payment_type_both"
            for field in RATE_IMPACTING_FIELDS:
                state = self.fields.get(field)
                if state and state.status in {"refused", "unavailable"}:
                    return field
        return "complete" if mode == "exact" else self.pending_field()

    def starting_rate_key(self) -> str:
        mode = self.pricing_mode()
        if mode == "zone_starting":
            return f"zone:{self.value('zone')}"
        if mode == "general_starting":
            return "general"
        return ""

    def starting_rate_due(self) -> bool:
        key = self.starting_rate_key()
        return bool(key and key not in self._presented_starting_rates)

    def mark_starting_rate_presented(self) -> None:
        key = self.starting_rate_key()
        if not key:
            return
        self._presented_starting_rates.add(key)

    def mark_pricing_verified(self, tool_name: str, *, payment_basis: str = "") -> None:
        self.verified_pricing_path = self.requested_rate_type or self.pricing_mode()
        self.verified_pricing_tool = str(tool_name or "")
        self.verified_payment_basis = str(payment_basis or self.value("payment_type") or "")
        if self.v5_company_pair_flow:
            self.customer_satisfied = False
            self.anything_else_question_due = False
            self.anything_else_detail_due = False
            self.anything_else_decision = ""
            self.move_forward_decision = ""
            self.onboarding_link_due = False
            self.better_plan_close_due = False
            self.unsatisfied_problem_due = False
            self.unsatisfied_resolution_due = False
            self.unsatisfied_resolution_presented = False
            self.unsatisfied_concern = ""
            if not self.is_handled("monthly_shipments"):
                self.monthly_quantity_due = True
                self.move_forward_question_due = False
            else:
                self.monthly_quantity_due = False
                self.anything_else_question_due = True
                self.move_forward_question_due = False

    def mark_route_zone_lookup_unavailable(self, *, fallback_presented: bool = True) -> None:
        """End exact-route pricing when the trusted resolver cannot verify a zone."""
        if self.route_zone_lookup_status == "unavailable":
            if fallback_presented:
                self._presented_starting_rates.add("general")
            return
        self.route_zone_lookup_status = "unavailable"
        if fallback_presented:
            self._presented_starting_rates.add("general")
        self._append_transition(
            {
                "event": "route_zone_lookup_updated",
                "status": "unavailable",
                "fallback_presented": fallback_presented,
                "created_at": time.time(),
            }
        )

    def mark_route_zone_verified(
        self,
        zone: str,
        *,
        starting_presented: bool = False,
        route_arguments: dict[str, object] | None = None,
    ) -> bool:
        """Store only an approved zone returned by the trusted route resolver."""
        normalized = normalize_text(zone).removeprefix("zone").strip().upper()
        if normalized not in APPROVED_ZONES:
            return False
        effective_route = route_arguments or self.next_route_for_lookup() or {}
        route_key = self._route_request_key(effective_route)
        if starting_presented and route_key:
            self._resolved_route_keys.add(route_key)
        remaining_routes = self.unresolved_route_count()
        if starting_presented and remaining_routes:
            self.route_zone_lookup_status = ""
            self.fields.pop("zone", None)
            self._append_transition(
                {
                    "event": "route_zone_lookup_updated",
                    "status": "route_starting_presented",
                    "zone": normalized,
                    "route": effective_route,
                    "remaining_route_count": remaining_routes,
                    "created_at": time.time(),
                }
            )
            return True
        self.route_zone_lookup_status = (
            "verified_starting" if starting_presented else "verified"
        )
        self._set_field(
            "zone",
            status="confirmed",
            value=normalized,
            evidence="[verified route resolver]",
            turn_id="route-resolver",
            confidence=1.0,
            source="resolver",
        )
        if starting_presented:
            self._presented_starting_rates.add(f"zone:{normalized}")
        return True

    def guidance(self) -> str:
        pending = self.pending_field()
        pricing_mode = self.pricing_mode()
        if self.v4_strict_flow:
            if self.onboarding_link_due:
                return (
                    "The customer explicitly said yes to moving forward with ShipKia. Say exactly "
                    "once: 'Theek hai, main aapko WhatsApp par onboarding ka link bhej raha "
                    "hoon. Aap us link se apni onboarding complete kar lijiye.' Then close warmly. "
                    "Do not restart qualification, repeat a rate, speak the URL aloud, or ask "
                    "another question."
                )
            if self.better_plan_close_due:
                return (
                    "The customer explicitly declined moving forward or rejected the current "
                    "offer. Say exactly once: 'Theek hai, main aapke liye ek better plan team ke "
                    "saath discuss karke aapko batata hoon. Thank you for calling ShipKia.' Then "
                    "end politely. Do not ask another question, send an onboarding link, quote a "
                    "new rate, or promise a specific discount."
                )
            if self.onboarding_link_presented or self.better_plan_close_presented:
                return (
                    "The approved closing has already been presented. Do not restart discovery, "
                    "pricing, or onboarding. If the customer speaks again, give only one brief "
                    "polite farewell and end."
                )
            if self.unsatisfied_resolution_presented:
                return (
                    "The customer's unsatisfied concern has already been acknowledged and the team "
                    "solution/better-plan follow-up has already been promised. Do not restart "
                    "qualification, repeat any question, quote another rate, or send onboarding. "
                    "Give only one brief polite farewell if the customer speaks again."
                )
            if self.unsatisfied_problem_due:
                return (
                    "The customer said they are not satisfied, but has not shared what exact problem "
                    "is unresolved. Ask only: 'Aapko exactly kya problem aa rahi hai?' Wait for the "
                    "answer. Do not ask business type, provider, route, monthly quantity, or the "
                    "move-forward question, and do not assume their problem."
                )
            if self.unsatisfied_resolution_due:
                concern = self.unsatisfied_concern or self.callback_close_concern or "current concern"
                return (
                    f"The customer's exact unresolved concern is: {concern}. Briefly acknowledge "
                    "that same concern, then say in natural Hinglish: 'Main aapki is problem ko "
                    "apni team ke saath discuss karke aapko solution ya better plan deta hoon. "
                    "Thank you for calling ShipKia.' Do not ask another question, restart discovery, "
                    "send onboarding, invent a rate, or promise a specific discount."
                )
            if self.provider_clarification_due:
                return (
                    "The customer's provider answer was contradictory. Ask only: 'Aap abhi "
                    "Shiprocket use kar rahe hain, ya filhaal koi shipping provider use nahi kar "
                    "rahe?' Do not assume either answer and do not continue to current rate yet."
                )
            if self.last_provider_options_query:
                if self.verified_starting_options:
                    options = json.dumps(
                        self.verified_starting_options,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    partners = ", ".join(self.available_courier_partners)
                    return (
                        "The customer explicitly asked which courier/service options and rates are "
                        "available. Answer that question before any move-forward close. List every "
                        f"worker-verified starting option in this exact data: {options}. Explain that "
                        "each amount is a GST-inclusive 500 g Forward starting option for the already "
                        "verified zone, not an exact shipment quote or delivery/serviceability "
                        f"guarantee. Configured courier partners in this result are: {partners}. "
                        "Do not invent another courier, service, rate, SLA, saving, or discount. "
                        "Finish by asking exactly: 'Kya aap kuch aur jaanna chahenge?' Do not ask "
                        "the move-forward question in this response."
                    )
                return (
                    "The customer asked for courier/provider options, but no detailed option list is "
                    "authorized in current worker state. Explain that the exact options and rates "
                    "must be checked against the active rate card; do not invent names or amounts, "
                    "then ask exactly: 'Kya aap kuch aur jaanna chahenge?' Do not jump to the "
                    "move-forward close."
                )
            if self.last_usp_query:
                resume = {
                    "assistance_intent": "Then ask only whether they want rates or onboarding help.",
                    "business_name": "Then resume by asking only for their business or brand name.",
                    "business_type": "Then resume by asking only whether their business is B2C or D2C.",
                    "current_shipping_arrangement": "Then resume by asking only which courier or shipping provider they use.",
                    "current_provider_name": "Then resume by asking only which courier or aggregator they use.",
                    "current_shipping_rate": "Then resume by asking only their current comparable shipping rate.",
                    "current_problem": "Then resume by asking only their main problem with that provider.",
                }.get(
                    pending,
                    "Then ask: 'Kya aap kuch aur jaanna chahenge?'",
                )
                detail_scope = (
                    "The customer explicitly requested detail, so explain all four verified facts "
                    "with a short practical description for each. "
                    if self.last_detailed_usp_query
                    else "Choose two or three facts that are most relevant to what the customer asked. "
                )
                return (
                    "Answer the ShipKia information or benefits question naturally and directly. "
                    + detail_scope
                    + "The verified facts are: "
                    "ShipKia helps manage shipments across multiple courier partners; it provides a "
                    "dedicated account manager for ticketing and support; it supports WhatsApp order "
                    "confirmation followed by call confirmation when WhatsApp gets no response; and "
                    "it supports WhatsApp plus IVR-call follow-up for same-delivery NDR. You may vary "
                    "the wording and give a brief practical explanation, but never invent a feature, "
                    "guarantee, saving, discount, or delivery promise. "
                    f"{resume} Do not change or clear any already captured detail, and do not infer "
                    "satisfaction from this side question."
                )
            if self.ekart_rate_choice_due:
                return (
                    "The customer asked generally for E-Kart rates without selecting a pricing "
                    "structure. Ask only: 'E-Kart Surface ke Flat rates chahiye ya E-Kart Express "
                    "ke Flat-Zonal rates?' Do not ask for a zone or route, do not quote a price, "
                    "and do not choose a structure for them."
                )
            if self.shadowfax_surface_rate_due and self.is_confirmed("zone"):
                return (
                    "The customer explicitly requested Shadowfax Surface pricing and the route zone "
                    "is already verified. Call get_shipkia_starting_rate exactly once now. The worker "
                    "will apply the verified zone and Shadowfax Surface filter from state. Speak the "
                    "returned exact service label and GST-inclusive starting amount directly. Do not "
                    "ask the customer to identify a zone, choose a zone group, confirm again, or give "
                    "permission before the rate."
                )
            if self.last_flat_zonal_route_query and self.is_confirmed("zone"):
                zone = str(self.value("zone") or "").upper()
                group = "A-B" if zone in {"A", "B"} else "C-F"
                amount = self.flat_zonal_group_totals.get(group)
                pickup = str(self.value("pickup_location") or "pickup")
                delivery = str(self.value("delivery_location") or "delivery")
                amount_instruction = (
                    f" The verified 500-gram Flat-Zonal total for Zones {group} is Rs {amount:.2f}, "
                    "GST included."
                    if amount is not None
                    else ""
                )
                return (
                    f"Answer directly that {pickup} to {delivery} is verified as Zone {zone}, which "
                    f"falls in the E-Kart Express Zones {group} Flat-Zonal group."
                    f"{amount_instruction} Do not call a pricing tool again, do not ask which group "
                    "they want, and do not ask whether they want the rate."
                )
            if self.last_rate_repeat_requested and self.primary_rate_amount is not None:
                continuation = (
                    " Then ask only for their approximate monthly shipment quantity."
                    if self.monthly_quantity_due
                    else " Then ask: 'Kya aap kuch aur jaanna chahenge?'"
                )
                return (
                    f"The customer asked how much. Lead with the verified starting rate: Rs "
                    f"{self.primary_rate_amount:.2f}, GST included.{continuation} Do not delay the "
                    "amount, ask permission, or ask another question first."
                )
            if self.last_monthly_quantity_captured:
                return (
                    "Briefly acknowledge the captured monthly shipment quantity, then ask exactly: "
                    "'Kya aap kuch aur jaanna chahenge?' Do not ask for the "
                    "quantity, business details, route, weight, or payment mode again."
                )
            if self.anything_else_detail_due:
                return (
                    "The customer said they want more information. Ask exactly: 'Ji, aap kya "
                    "jaanna chahenge?' Do not ask the ShipKia move-forward question yet."
                )
            if self.anything_else_question_due:
                return (
                    "Ask exactly once: 'Kya aap kuch aur jaanna chahenge?' If the customer wants "
                    "more information, answer that request fully. Only a clear no/nothing-else "
                    "answer may advance to the ShipKia move-forward question."
                )
            if self.move_forward_question_due:
                return (
                    "Ask exactly once: 'Kya aap ShipKia ke saath aage badhna chahte hain?' Wait "
                    "for a clear yes or no. Do not infer the decision from thanks, satisfaction, "
                    "silence, or an unrelated reply."
                )
            if (
                self.monthly_quantity_due
                and not self.flat_catalog_due()
                and not self.flat_zonal_catalog_due()
                and pricing_mode != "route_starting_pending"
            ):
                return (
                    "The verified requested rate has been presented and monthly shipment quantity "
                    "is still missing. Ask only for the customer's approximate monthly shipment "
                    "quantity. Do not ask business details, route, weight, or payment mode in the "
                    "same turn."
                )
            if pricing_mode == "conversation_declined":
                return (
                    "Say a brief polite thank-you and end the conversation. Ask nothing else. "
                    "Never describe this private direction."
                )
            pending_directions = {
                "conversation_consent": "Ask only whether this is a convenient time to talk.",
                "assistance_intent": (
                    "Ask only whether they want to check shipping rates or need onboarding help."
                ),
                "business_name": "Ask only for their business or brand name.",
                "business_type": "Ask only whether their business is B2C or D2C.",
                "current_shipping_arrangement": (
                    "Ask only what courier or shipping arrangement they currently use."
                ),
                "current_provider_name": "Ask only which courier or aggregator they currently use.",
                "current_shipping_rate": (
                    "Ask only their current rate for a comparable shipment."
                ),
                "current_problem": "Ask only their main current shipping challenge.",
                "pickup_pincode": "Ask only for the six-digit pickup pincode or pickup city/location.",
                "delivery_pincode": "Ask only for the six-digit delivery pincode or drop city/location.",
                "dead_weight": "Ask only for the shipment weight.",
                "payment_type": "Ask only whether the shipment is Prepaid, COD, or Both.",
                "order_value": "Ask only for the COD order value.",
            }
            if (
                self.v5_company_pair_flow
                and pricing_mode == "route_starting_pending"
                and self.route_ready_for_lookup()
            ):
                problem_instruction = ""
                if self.last_problem_captured:
                    problem = normalize_text(self.value("current_problem"))
                    if re.search(r"\b(?:return|returns|rto|ndr)\b|रिटर्न|आरटीओ", problem):
                        problem_instruction = (
                            "First acknowledge their return/RTO problem and briefly explain that ShipKia "
                            "supports WhatsApp/IVR NDR follow-up plus order confirmation to help reduce "
                            "avoidable returns. Then "
                        )
                    elif re.search(
                        r"\b(?:order|orders)\b.{0,35}\b(?:problem|issue|dikkat|confirmation|"
                        r"wrong|fake|cancel)\b|\b(?:problem|issue|dikkat)\b.{0,35}\borders?\b|"
                        r"\u0911\u0930\u094d\u0921\u0930.{0,35}\u0926\u093f\u0915\u094d\u0915\u0924",
                        problem,
                    ):
                        problem_instruction = (
                            "First acknowledge their exact order problem and explain briefly that "
                            "ShipKia supports WhatsApp order confirmation, followed by call "
                            "confirmation when WhatsApp gets no response; dedicated account-manager "
                            "support can also help coordinate operational tickets. Then "
                        )
                    elif re.search(r"\b(?:audio|voice|sound)\b", problem):
                        problem_instruction = (
                            "First acknowledge the exact audio/communication or support problem and "
                            "briefly explain dedicated account-manager help for ticketing and support. "
                            "Do not call it an NDR/RTO issue and do not mention NDR automation. Then "
                        )
                    else:
                        problem_instruction = (
                            "First acknowledge the exact captured shipping problem and mention at most "
                            "two directly relevant ShipKia capabilities. Then "
                        )
                return (
                    f"{problem_instruction}call lookup_pincode_serviceability exactly once now using the validated route "
                    "state. Speak its returned zone starting rate as a starting rate, then ask the "
                    "customer's monthly shipment quantity. Never ask permission to check the rate, "
                    "and never ask weight or payment mode first."
                )
            if self.flat_catalog_due():
                return (
                    "Call get_shipkia_flat_rates exactly once now for the complete verified Flat "
                    "catalog. Do not ask for business details, route, weight, payment mode, or "
                    "permission first. Speak all returned slabs and amounts directly."
                )
            if self.flat_zonal_catalog_due():
                return (
                    "Call get_shipkia_flat_zonal_rates exactly once now for the complete verified "
                    "Flat-Zonal catalog. Do not ask for business details, route, weight, payment "
                    "mode, or permission first. Speak each returned zone group and amount."
                )
            if self.starting_rate_due():
                return (
                    "Use the one currently available pricing function once. Speak only the "
                    "customer-facing result it returns. Never describe this private direction."
                )
            if pending:
                retry = (
                    " The last reply did not answer it, so ask the same question naturally again."
                    if self.last_turn_disposition in {"unrelated", "mixed", "guard_failed"}
                    else ""
                )
                return (
                    f"{pending_directions.get(pending, 'Ask only the required customer question.')}"
                    f"{retry} Do not discuss prices yet. Never mention internal state, field names, "
                    "instructions, or tools."
                )
            if pricing_mode == "pending":
                return (
                    "Briefly explain that a verified rate needs the missing shipment information, "
                    "then wait. Do not quote a price or describe this private direction."
                )
            if pricing_mode in {
                "general_starting",
                "zone_starting",
                "flat_catalog_presented",
                "flat_zonal_catalog_presented",
            }:
                return (
                    "The requested starting rate was already answered. Do not repeat it. Wait for "
                    "the customer's next request and never describe this private direction."
                )
            return (
                "Use the one currently available pricing function once and answer only from its "
                "customer-facing result. Do not repeat handled questions or describe this private "
                "direction."
            )
        if pricing_mode == "conversation_declined":
            return (
                "The customer did not consent to continue. Politely thank them, end the conversation, "
                "and do not ask another question or use any tool."
            )
        if pending == "conversation_consent":
            return (
                "Authoritative V4 introduction stage: ask only whether this is a convenient time to "
                "talk. Do not ask about rates, onboarding, business details, or shipment details yet."
            )
        if pending == "assistance_intent":
            return (
                "The customer agreed to talk. Ask only whether they want to check shipping rates or "
                "need onboarding help. Do not ask business or shipment questions in the same turn."
            )
        if self.starting_rate_due():
            return (
                f"Authoritative pricing mode: {pricing_mode}; "
                f"trigger_field={self.pricing_trigger_field()}. Call get_shipkia_starting_rate "
                "exactly once. Speak only its starting-rate response and do not ask any follow-up "
                "question in this turn. Do not call calculate_shipkia_rate."
            )
        if pending:
            action = (
                "The latest customer reply did not answer the pending question. Briefly handle "
                "their side query, then naturally ask the same pending question again."
                if self.last_turn_disposition in {"unrelated", "mixed"}
                else "Ask only this pending question next."
            )
            if self.last_turn_disposition == "guard_failed":
                action = (
                    "The answer guard could not verify the latest reply. Do not advance or guess; "
                    "briefly clarify the same pending question."
                )
            return (
                f"Authoritative gated state: pending_field={pending}. {action} "
                "Do not call calculate_shipkia_rate yet. Do not fill Unknown or Not Applicable."
            )
        if self.v4_strict_flow and pricing_mode == "pending":
            return (
                "Authoritative V4 pricing state: a required shipment input was refused or is "
                "unavailable. Do not quote any Normal, Flat, general-starting, or zone-starting "
                "rate and do not substitute a default. Briefly explain that a verified rate needs "
                "the missing shipment basis, then wait without repeating a refused question."
            )
        if pricing_mode in {"general_starting", "zone_starting"}:
            return (
                f"Authoritative pricing mode: {pricing_mode}. The starting-rate response was "
                "already presented. Do not repeat it and do not call calculate_shipkia_rate. "
                "Wait for the customer's next request."
            )
        pricing_tool = (
            "get_shipkia_flat_rates"
            if self.v4_strict_flow and self.requested_rate_type == "Flat"
            else "calculate_shipkia_rate"
        )
        return (
            "Authoritative gated state: pricing_ready=true. All applicable gated inputs are "
            f"handled. Call {pricing_tool} once using the validated state; do not invent or "
            "re-ask any handled field."
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "pending_field": self.pending_field(),
            "pricing_ready": self.pricing_ready(),
            "pricing_mode": self.pricing_mode(),
            "pricing_trigger_field": self.pricing_trigger_field(),
            "starting_rate_due": self.starting_rate_due(),
            "requested_rate_type": self.requested_rate_type,
            "pending_catalogs": sorted(self.pending_catalogs),
            "flat_catalog_due": self.flat_catalog_due(),
            "flat_catalog_presented": self.flat_catalog_presented,
            "flat_zonal_catalog_due": self.flat_zonal_catalog_due(),
            "flat_zonal_catalog_presented": self.flat_zonal_catalog_presented,
            "flat_zonal_group_totals": dict(self.flat_zonal_group_totals),
            "shadowfax_surface_rate_due": self.shadowfax_surface_rate_due,
            "shadowfax_surface_rate_presented": self.shadowfax_surface_rate_presented,
            "customer_satisfied": self.customer_satisfied,
            "move_forward_question_due": self.move_forward_question_due,
            "anything_else_question_due": self.anything_else_question_due,
            "anything_else_detail_due": self.anything_else_detail_due,
            "anything_else_decision": self.anything_else_decision,
            "move_forward_decision": self.move_forward_decision,
            "onboarding_link_due": self.onboarding_link_due,
            "better_plan_close_due": self.better_plan_close_due,
            "better_plan_close_presented": self.better_plan_close_presented,
            "unsatisfied_problem_due": self.unsatisfied_problem_due,
            "unsatisfied_resolution_due": self.unsatisfied_resolution_due,
            "unsatisfied_resolution_presented": self.unsatisfied_resolution_presented,
            "unsatisfied_concern": self.unsatisfied_concern,
            "callback_close_concern": self.callback_close_concern,
            "onboarding_link_presented": self.onboarding_link_presented,
            "ekart_rate_choice_due": self.ekart_rate_choice_due,
            "monthly_shipments_handled": self.is_handled("monthly_shipments"),
            "monthly_quantity_due": self.monthly_quantity_due,
            "last_rate_repeat_requested": self.last_rate_repeat_requested,
            "primary_rate_amount": self.primary_rate_amount,
            "provider_clarification_due": self.provider_clarification_due,
            "last_customer_dissatisfied": self.last_customer_dissatisfied,
            "verified_pricing_path": self.verified_pricing_path,
            "verified_pricing_tool": self.verified_pricing_tool,
            "verified_payment_basis": self.verified_payment_basis,
            "authorized_rate_amounts": sorted(self.authorized_rate_amounts),
            "verified_starting_options": list(self.verified_starting_options),
            "available_courier_partners": list(self.available_courier_partners),
            "last_provider_options_query": self.last_provider_options_query,
            "last_detailed_usp_query": self.last_detailed_usp_query,
            "approved_zone": self.value("zone") if self.is_confirmed("zone") else None,
            "state_revision": self.revision,
            "optional_ended_by": self.optional_ended_by,
            "company_details_ended_by": self.company_details_ended_by,
            "route_zone_lookup_status": self.route_zone_lookup_status,
            "pan_india_requested": self.pan_india_requested,
            "requested_routes": list(self.requested_routes),
            "unresolved_route_count": self.unresolved_route_count(),
            "last_turn_disposition": self.last_turn_disposition,
            "last_guard_error": self.last_guard_error,
            "fields": {
                name: {
                    key: value
                    for key, value in asdict(state).items()
                    if key != "updated_at"
                }
                for name, state in self.fields.items()
            },
        }


class SemanticAnswerGuard:
    """Classify the latest customer turn without allowing the model to mutate state directly."""

    RESPONSE_SCHEMA = {
        "type": "object",
        "properties": {
            "turn_disposition": {
                "type": "string",
                "enum": ["answered", "unrelated", "mixed"],
            },
            "decisions": {
                "type": "array",
                "maxItems": len(SEMANTIC_FIELDS),
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "enum": sorted(SEMANTIC_FIELDS)},
                        "disposition": {
                            "type": "string",
                            "enum": [
                                "answered",
                                "unknown",
                                "refused",
                                "not_applicable",
                            ],
                        },
                        "value": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "number"},
                                {"type": "null"},
                            ]
                        },
                        "evidence": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": [
                        "field",
                        "disposition",
                        "value",
                        "evidence",
                        "confidence",
                    ],
                },
            },
        },
        "required": ["turn_disposition", "decisions"],
    }

    def __init__(
        self,
        *,
        model: str | None = None,
        timeout_seconds: float | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model or os.getenv(
            "SHIPKIA_ANSWER_GUARD_MODEL",
            "gemini-2.5-flash-lite",
        )
        self.timeout_seconds = timeout_seconds or float(
            os.getenv("SHIPKIA_ANSWER_GUARD_TIMEOUT_SECONDS", "5")
        )
        self._client = client

    def _client_or_create(self):
        if self._client is None:
            self._client = genai.Client(
                vertexai=True,
                project=os.getenv("GOOGLE_CLOUD_PROJECT"),
                location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
            )
        return self._client

    async def classify(
        self,
        *,
        customer_text: str,
        pending_field: str,
        state_snapshot: dict[str, Any],
        previous_agent_text: str = "",
    ) -> dict[str, Any]:
        prompt = (
            "Classify only explicit free-text qualification or service facts in the latest customer "
            "utterance for a shipping sales conversation. Also capture explicit pickup_location and "
            "delivery_location city/place names. For a route such as 'Delhi to Noida' or 'Delhi se "
            "Noida', Delhi is pickup_location and Noida is delivery_location. When the pending field "
            "is pickup_pincode or delivery_pincode and the customer answers with a city/location, "
            "store it in the matching *_location field. Structured pincodes, weight, payment and "
            "order value are handled elsewhere and must not be returned. Never infer a business "
            "name/type, problem, provider, arrangement, current rate, or service. Business type "
            "is valid only as a clear standard acronym such as B2C, D2C, or B2B; never return an "
            "unclear phrase such as A-to-Z as business_type. An unrelated request does not answer "
            "the pending field. Quote exact evidence from the customer text "
            "for every decision. Map an unknown/refusal only to its explicitly named field, or to "
            "the pending field when that pending field is one of the allowed free-text fields. "
            "For current_shipping_arrangement, use disposition=not_applicable when the customer "
            "clearly says they currently use nothing, selected no courier/aggregator, or are a new "
            "business with no shipping solution. Future intent to use ShipKia is not a current "
            "arrangement. A service is an exact courier product or explicitly selected shipping "
            "service, such as E-Kart SURFACE; Prepaid, COD, Both/dono, 'COD rate', and an order "
            "value are payment/rate facts and must never be classified as service. Interpret a "
            "bare no/nahi only against the previous agent question; if "
            "that question is a negative confirmation, leave it unrelated rather than reversing "
            "its meaning. A shipment quantity or a purely numeric reply is never a current_problem. "
            "When current_problem is already confirmed, update it only when the latest customer "
            "text explicitly states a new problem, issue, challenge, difficulty, RTO/NDR, return, "
            "delay, or support concern. "
            "Extract multiple fields only when each is explicit.\n"
            "The pending field and state snapshot are frozen from the start of this customer turn. "
            "Never treat an answer to that field as unknown/refusal/not-applicable for a later field "
            "that would become pending after this answer. For example, B2C/D2C cannot refuse the "
            "courier-arrangement question.\n"
            f"Pending field: {pending_field or '[none]'}\n"
            f"Previous agent question: {previous_agent_text or '[none]'}\n"
            f"Existing state: {json.dumps(state_snapshot, ensure_ascii=False, default=str)}\n"
            f"Latest customer text: {customer_text}"
        )
        config = genai_types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=1200,
            response_mime_type="application/json",
            response_json_schema=self.RESPONSE_SCHEMA,
        )
        response = await asyncio.wait_for(
            self._client_or_create().aio.models.generate_content(
                model=self.model,
                contents=prompt,
                config=config,
            ),
            timeout=self.timeout_seconds,
        )
        payload = json.loads(response.text or "{}")
        if not isinstance(payload, dict):
            raise ValueError("Semantic guard returned a non-object response.")
        return payload
