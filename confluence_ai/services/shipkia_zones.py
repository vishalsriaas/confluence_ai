from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


ZONES = frozenset({"A", "B", "C", "D", "E", "F"})


@dataclass(frozen=True)
class LocationProfile:
    city: str
    state: str
    cluster: str = ""
    metro: bool = False
    special: bool = False
    remote: bool = False


def _profile(
    city: str,
    state: str,
    *,
    cluster: str = "",
    metro: bool = False,
    special: bool = False,
    remote: bool = False,
) -> LocationProfile:
    return LocationProfile(
        city=city,
        state=state,
        cluster=cluster,
        metro=metro,
        special=special,
        remote=remote,
    )


_CITY_PROFILES = {
    "delhi": _profile("Delhi", "Delhi", cluster="NCR", metro=True),
    "new delhi": _profile("Delhi", "Delhi", cluster="NCR", metro=True),
    "noida": _profile("Noida", "Uttar Pradesh", cluster="NCR"),
    "greater noida": _profile("Greater Noida", "Uttar Pradesh", cluster="NCR"),
    "ghaziabad": _profile("Ghaziabad", "Uttar Pradesh", cluster="NCR"),
    "gurgaon": _profile("Gurugram", "Haryana", cluster="NCR"),
    "gurugram": _profile("Gurugram", "Haryana", cluster="NCR"),
    "faridabad": _profile("Faridabad", "Haryana", cluster="NCR"),
    "mumbai": _profile("Mumbai", "Maharashtra", metro=True),
    "bombay": _profile("Mumbai", "Maharashtra", metro=True),
    "pune": _profile("Pune", "Maharashtra"),
    "bengaluru": _profile("Bengaluru", "Karnataka", metro=True),
    "bangalore": _profile("Bengaluru", "Karnataka", metro=True),
    "chennai": _profile("Chennai", "Tamil Nadu", metro=True),
    "hyderabad": _profile("Hyderabad", "Telangana", metro=True),
    "kolkata": _profile("Kolkata", "West Bengal", metro=True),
    "calcutta": _profile("Kolkata", "West Bengal", metro=True),
    "ahmedabad": _profile("Ahmedabad", "Gujarat"),
    "surat": _profile("Surat", "Gujarat"),
    "jaipur": _profile("Jaipur", "Rajasthan"),
    "lucknow": _profile("Lucknow", "Uttar Pradesh"),
    "kanpur": _profile("Kanpur", "Uttar Pradesh"),
    "varanasi": _profile("Varanasi", "Uttar Pradesh"),
    "chandigarh": _profile("Chandigarh", "Chandigarh"),
    "ludhiana": _profile("Ludhiana", "Punjab"),
    "amritsar": _profile("Amritsar", "Punjab"),
    "indore": _profile("Indore", "Madhya Pradesh"),
    "bhopal": _profile("Bhopal", "Madhya Pradesh"),
    "nagpur": _profile("Nagpur", "Maharashtra"),
    "patna": _profile("Patna", "Bihar"),
    "ranchi": _profile("Ranchi", "Jharkhand"),
    "bhubaneswar": _profile("Bhubaneswar", "Odisha"),
    "kochi": _profile("Kochi", "Kerala"),
    "cochin": _profile("Kochi", "Kerala"),
    "thiruvananthapuram": _profile("Thiruvananthapuram", "Kerala"),
    "trivandrum": _profile("Thiruvananthapuram", "Kerala"),
    "coimbatore": _profile("Coimbatore", "Tamil Nadu"),
    "guwahati": _profile("Guwahati", "Assam", special=True),
    "srinagar": _profile("Srinagar", "Jammu and Kashmir", special=True),
    "jammu": _profile("Jammu", "Jammu and Kashmir", special=True),
    "leh": _profile("Leh", "Ladakh", remote=True),
    "port blair": _profile("Port Blair", "Andaman and Nicobar Islands", remote=True),
}


_PIN_PREFIX_PROFILES = {
    "110": _CITY_PROFILES["delhi"],
    "121": _CITY_PROFILES["faridabad"],
    "122": _CITY_PROFILES["gurugram"],
    "160": _CITY_PROFILES["chandigarh"],
    "180": _CITY_PROFILES["jammu"],
    "190": _CITY_PROFILES["srinagar"],
    "194": _CITY_PROFILES["leh"],
    "201": _CITY_PROFILES["noida"],
    "208": _CITY_PROFILES["kanpur"],
    "221": _CITY_PROFILES["varanasi"],
    "226": _CITY_PROFILES["lucknow"],
    "302": _CITY_PROFILES["jaipur"],
    "380": _CITY_PROFILES["ahmedabad"],
    "395": _CITY_PROFILES["surat"],
    "400": _CITY_PROFILES["mumbai"],
    "411": _CITY_PROFILES["pune"],
    "440": _CITY_PROFILES["nagpur"],
    "452": _CITY_PROFILES["indore"],
    "462": _CITY_PROFILES["bhopal"],
    "500": _CITY_PROFILES["hyderabad"],
    "560": _CITY_PROFILES["bengaluru"],
    "600": _CITY_PROFILES["chennai"],
    "641": _CITY_PROFILES["coimbatore"],
    "682": _CITY_PROFILES["kochi"],
    "695": _CITY_PROFILES["thiruvananthapuram"],
    "700": _CITY_PROFILES["kolkata"],
    "744": _CITY_PROFILES["port blair"],
    "751": _CITY_PROFILES["bhubaneswar"],
    "781": _CITY_PROFILES["guwahati"],
    "800": _CITY_PROFILES["patna"],
    "834": _CITY_PROFILES["ranchi"],
}


def _clean_location(value: object) -> str:
    clean = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    return re.sub(r"\s+", " ", clean)


def _valid_pincode(value: object) -> str:
    rendered = str(value or "").strip()
    return rendered if re.fullmatch(r"\d{6}", rendered) else ""


def _location_profile(*, pincode: object = "", location: object = "") -> LocationProfile | None:
    pin = _valid_pincode(pincode)
    if pin:
        return _PIN_PREFIX_PROFILES.get(pin[:3])
    clean = _clean_location(location)
    if not clean:
        return None
    if clean in _CITY_PROFILES:
        return _CITY_PROFILES[clean]
    for alias in sorted(_CITY_PROFILES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", clean):
            return _CITY_PROFILES[alias]
    return None


def _zone_for_profiles(pickup: LocationProfile, delivery: LocationProfile) -> tuple[str, str]:
    if pickup.city == delivery.city:
        return "A", "same_city"
    if pickup.cluster and pickup.cluster == delivery.cluster:
        return "A", "same_shipping_cluster"
    if pickup.remote or delivery.remote:
        return "F", "remote_location"
    if pickup.special or delivery.special:
        return "E", "special_region"
    if pickup.state == delivery.state:
        return "B", "same_state"
    if pickup.metro and delivery.metro:
        return "C", "metro_to_metro"
    return "D", "domestic_interstate"


def resolve_shipkia_zone(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the V5 route zone using ShipKia's deterministic voice-routing policy."""
    arguments = arguments or {}
    pan_india = bool(arguments.get("pan_india"))
    pickup_pin = _valid_pincode(arguments.get("pickup_pincode"))
    delivery_pin = _valid_pincode(arguments.get("delivery_pincode"))
    pickup_location = str(arguments.get("pickup_location") or "").strip()
    delivery_location = str(arguments.get("delivery_location") or "").strip()

    if pan_india:
        return {
            "status": "success",
            "serviceable": True,
            "zone": "A",
            "zone_verified": True,
            "rate_scope": "starting_only",
            "resolution_basis": "pan_india_zone_a_starting_policy",
            "message": "Pan-India enquiries use the Zone A floor only as a starting-rate headline.",
        }

    if not (pickup_pin or pickup_location) or not (delivery_pin or delivery_location):
        return {
            "status": "route_details_required",
            "serviceable": None,
            "zone": None,
            "zone_verified": False,
            "message": "Provide a pickup and delivery pincode or city/location.",
        }

    pickup_profile = _location_profile(pincode=pickup_pin, location=pickup_location)
    delivery_profile = _location_profile(pincode=delivery_pin, location=delivery_location)

    if pickup_profile and delivery_profile:
        zone, basis = _zone_for_profiles(pickup_profile, delivery_profile)
    elif pickup_pin and delivery_pin:
        if pickup_pin[:3] == delivery_pin[:3]:
            zone, basis = "A", "same_pincode_district_prefix"
        elif pickup_pin[0] == delivery_pin[0]:
            zone, basis = "B", "same_postal_region"
        else:
            zone, basis = "D", "cross_postal_region"
    else:
        pickup_clean = _clean_location(pickup_location)
        delivery_clean = _clean_location(delivery_location)
        if pickup_clean and pickup_clean == delivery_clean:
            zone, basis = "A", "same_named_location"
        else:
            zone, basis = "D", "domestic_intercity_default"

    if zone not in ZONES:
        raise ValueError(f"Unsupported ShipKia route zone: {zone}")

    result: dict[str, Any] = {
        "status": "success",
        "serviceable": True,
        "zone": zone,
        "zone_verified": True,
        "rate_scope": "starting_only",
        "resolution_basis": basis,
        "pickup_pincode": pickup_pin or None,
        "delivery_pincode": delivery_pin or None,
        "pickup_location": pickup_location or None,
        "delivery_location": delivery_location or None,
    }
    if pickup_profile:
        result["resolved_pickup"] = asdict(pickup_profile)
    if delivery_profile:
        result["resolved_delivery"] = asdict(delivery_profile)
    result["message"] = f"Route resolved to Zone {zone} for a starting-rate lookup."
    return result
