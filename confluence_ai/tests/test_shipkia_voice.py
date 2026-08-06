from __future__ import annotations

import secrets
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from confluence_ai.prompts.shipkia_voice import (
    SHIPKIA_VOICE_PROMPT_VERSION,
    SHIPKIA_VOICE_V3_PROMPT,
    SHIPKIA_VOICE_V4_PROMPT,
    SHIPKIA_VOICE_V5_PROMPT,
    get_shipkia_voice_prompt,
    list_shipkia_voice_prompt_versions,
)
from confluence_ai.shipkia_setup import (
    LANGUAGE_PROMPT_MARKER,
    RATE_PROMPT_MARKER,
    RATE_SALES_PROMPT_MARKER,
    USP_PROMPT_MARKER,
    _with_managed_shipkia_prompt,
)
from confluence_ai.services.livekit import (
    _removable_automatic_dispatch_ids,
    _is_participant_disconnect,
    _shipkia_dispatch_conflict,
    _rate_flow_completed,
    _rate_flow_started,
    _successful_pricing_outcome,
    build_voice_metadata,
)
from confluence_ai.services.shipkia_voice import (
    calculate_shipkia_rate,
    create_or_update_shipkia_lead,
    get_shipkia_flat_rates,
    get_shipkia_flat_zonal_rates,
    get_shipkia_starting_rate,
    lookup_pincode_serviceability,
    lookup_shipkia_crm_lead,
    normalize_phone,
)


class TestShipKiaVoice(FrappeTestCase):
    def test_voice_v3_prompt_has_gated_context_and_safe_benefit_flow(self):
        prompt = get_shipkia_voice_prompt("shipkia-voice-v3")

        self.assertEqual(prompt, SHIPKIA_VOICE_V3_PROMPT)
        self.assertEqual(SHIPKIA_VOICE_PROMPT_VERSION, "shipkia-voice-v5")
        self.assertEqual(
            list_shipkia_voice_prompt_versions(),
            ["shipkia-voice-v3", "shipkia-voice-v4", "shipkia-voice-v5"],
        )
        self.assertIn("Direct Courier", prompt)
        self.assertIn("Shipping Aggregator", prompt)
        self.assertIn("current rate for a comparable shipment", prompt)
        self.assertIn("kuch nahi", prompt)
        self.assertIn(
            "does not reopen current arrangement",
            prompt.lower(),
        )
        self.assertIn("whether GST and COD charges are included", prompt)
        self.assertIn("If it is equal or higher, say so honestly", prompt)
        self.assertIn("Give no more than two benefits", prompt)
        self.assertIn("eligible accounts", prompt)
        self.assertIn("support and ticketing channels", prompt)
        self.assertIn("can help reduce avoidable RTO", prompt)
        self.assertIn("without ending or disappearing", prompt)
        self.assertIn("ShipCart remains ShipCart", prompt)
        self.assertIn("explicitly confirm both weight and", prompt)
        self.assertIn("A request for information is not consent to a callback", prompt)
        self.assertEqual(prompt.count("Namaste! Main ShipKia ka assistant hoon"), 1)
        self.assertIn("worker-controlled gated state", prompt)
        self.assertIn("multi-detail reply", prompt)
        self.assertIn("brand or business name", prompt)
        self.assertIn("handled question again", prompt)
        self.assertIn("shipping challenge the next qualification priority", prompt)
        self.assertIn("Aapko shipping operations mein abhi sabse badi challenge", prompt)
        self.assertIn("relevant solution using only approved ShipKia capabilities", prompt)
        self.assertIn("6-digit pickup pincode", prompt)
        self.assertIn("Ask each pincode once", prompt)
        self.assertIn("general Rs 22 starting response", prompt)
        self.assertIn("Weight remains mandatory for exact calculation only", prompt)
        self.assertIn("get_shipkia_starting_rate", prompt)
        self.assertIn("exact pending question", prompt)
        self.assertIn("https://auth.shipkia.com/signup", prompt)
        self.assertIn("Do not push, repeatedly offer, or assume a scheduled sales call", prompt)
        self.assertIn("hard price floor", prompt)
        self.assertIn("switch from a quoted GST-inclusive total", prompt)
        self.assertIn("use calculate_shipkia_rate.flat_rate_options", prompt)
        self.assertIn("flat additional-weight component", prompt)
        self.assertIn("speak only the single returned", prompt)
        self.assertIn("Never call a lowest, average, starting", prompt)
        self.assertIn("production task provides a customer phone", prompt)
        self.assertNotIn("guaranteed RTO reduction", prompt)

    def test_voice_v4_is_standalone_compact_and_has_verified_pricing_flow(self):
        prompt = get_shipkia_voice_prompt("shipkia-voice-v4")

        self.assertEqual(prompt, SHIPKIA_VOICE_V4_PROMPT)
        self.assertFalse(prompt.startswith(SHIPKIA_VOICE_V3_PROMPT))
        self.assertLess(len(prompt), 10000)
        self.assertIn("get_shipkia_flat_rates", prompt)
        self.assertNotIn("Rs 22", prompt)
        self.assertNotIn("₹22", prompt)
        self.assertIn("Namaste, main ShipKia ki taraf se baat kar", prompt)
        self.assertIn(
            "Kya abhi hum do minute baat kar sakte hain?",
            " ".join(prompt.split()),
        )
        self.assertIn("Stop and wait for consent", prompt)
        self.assertIn("Normal requires, in order", prompt)
        self.assertIn("response_scope=Matching", prompt)
        self.assertIn("response_scope=All", prompt)
        self.assertIn(
            "Never ask for pickup or delivery pincode",
            " ".join(prompt.split()),
        )
        self.assertIn("Both/dono is a complete payment answer", prompt)
        self.assertIn("Never ask for permission", prompt)
        self.assertIn(
            "Reuse every unchanged confirmed detail",
            " ".join(prompt.split()),
        )
        self.assertIn(
            'Only an explicit "flat rate" selects Flat',
            " ".join(prompt.split()),
        )
        self.assertIn("Kya aap aur kuch jaanna chahenge?", prompt)

    def test_voice_v5_has_harsh_discovery_solution_and_zone_flow(self):
        prompt = get_shipkia_voice_prompt("shipkia-voice-v5")

        self.assertEqual(prompt, SHIPKIA_VOICE_V5_PROMPT)
        self.assertIn("You are Harsh", prompt)
        self.assertIn("Kya abhi hum do minute baat kar sakte hain?", " ".join(prompt.split()))
        self.assertIn("shipping rates check karna chahenge ya onboarding", prompt)
        self.assertIn("Company name and company type form one optional pair", prompt)
        self.assertIn("current comparable shipping rate", prompt)
        self.assertIn("main problem with that provider", prompt)
        self.assertIn("PROBLEM-TO-SOLUTION RESPONSE", prompt)
        self.assertIn("SHIPKIA USP RESPONSE", prompt)
        self.assertIn("Dedicated account manager", prompt)
        self.assertIn("WhatsApp confirmation", prompt)
        self.assertIn("call confirmation", prompt)
        self.assertIn("Delivery NDR assistance", prompt)
        self.assertIn("WhatsApp and IVR calling", prompt)
        self.assertIn("benefits/about-ShipKia question", prompt)
        self.assertIn("six-digit pickup pincode or pickup city/location", prompt)
        self.assertIn("delivery pincode or drop city/location", prompt)
        self.assertIn("Pan-India starting rate", prompt)
        self.assertIn(
            "Never ask the customer to identify ShipKia's internal zone",
            " ".join(prompt.split()),
        )
        self.assertIn("monthly shipment quantity/volume", prompt)
        self.assertIn("Rate Card 10 June CSV", prompt)
        self.assertIn("lookup_pincode_serviceability exactly once", prompt)
        self.assertIn("prompt itself contains no fallback number", prompt)
        self.assertIn("The customer's original rate enquiry remains active", prompt)
        self.assertIn('Never ask "Kya aap aur', prompt)
        self.assertIn('close ASR variants such as "Par India"', prompt)
        self.assertIn("Before the successful requested rate", prompt)
        self.assertIn("mandatory normal-discovery boundary", prompt)
        self.assertIn("A shipment quantity is never an answer", prompt)
        self.assertIn("provider-problem question is pending", prompt)
        self.assertIn("exactly three customer-facing pricing structures", prompt)
        self.assertIn("get_shipkia_flat_zonal_rates", prompt)
        self.assertIn("Kya aap ShipKia ke saath aage badhna chahte hain?", prompt)
        self.assertIn("clear yes to that move-forward question", prompt)
        self.assertIn("onboarding link is being sent", prompt)
        self.assertIn("better plan will be discussed with the team", prompt)
        self.assertIn("not as a reset", prompt)
        self.assertIn("E-Kart Surface ke Flat rates chahiye", prompt)
        self.assertIn('or "thank you" by\n  itself is not', prompt)

    def test_voice_v5_prompt_matches_authoritative_current_flow_contract(self):
        prompt = " ".join(get_shipkia_voice_prompt("shipkia-voice-v5").split())

        self.assertIn("worker-updated private current action is the sole authority", prompt)
        self.assertIn(
            "Iske alawa aap kuch aur jaanna chahenge, ya main aapko shipping rates "
            "check karne ya onboarding mein help kar doon?",
            prompt,
        )
        self.assertNotIn("ask the rates/onboarding choice again", prompt)
        self.assertIn("choose two or three relevant USPs", prompt)
        self.assertIn("all four verified USPs", prompt)
        self.assertIn(
            "Theek hai, main aapko WhatsApp par onboarding ka link bhej raha hoon",
            prompt,
        )
        self.assertNotIn("auth dot shipkia dot com slash signup", prompt)
        self.assertIn(
            'remember it, then ask exactly: "Kya aap kuch aur jaanna chahenge?"',
            prompt,
        )

    def test_shipkia_production_metadata_defaults_to_current_v5_prompt(self):
        class FakeTask:
            assigned_agent = "agent-445"
            target_agent = ""
            name = "task-test-v5"
            context_json = "{}"

        class FakeAgent:
            personality = ""

            @staticmethod
            def get_system_prompt(include_tool_catalog=False):
                return "legacy prompt"

            @staticmethod
            def get(field):
                values = {
                    "audio_name": "Puck",
                    "agent_type": "Single Agent",
                }
                return values.get(field)

        with patch(
            "confluence_ai.services.livekit.frappe.get_doc",
            side_effect=[FakeTask(), FakeAgent()],
        ):
            metadata = build_voice_metadata("task-test-v5", {})

        self.assertEqual(metadata["prompt_version"], "shipkia-voice-v5")
        self.assertEqual(metadata["system_prompt"], SHIPKIA_VOICE_V5_PROMPT)

    def test_console_rate_completion_requires_successful_pricing_tool(self):
        audit = {
            "state_snapshot": {
                "requested_rate_type": "Normal",
                "fields": {"assistance_intent": {"value": "Rates"}},
            },
            "tool_outcomes": [
                {"tool_name": "calculate_shipkia_rate", "status": "blocked"}
            ],
        }
        self.assertTrue(_rate_flow_started(audit))
        self.assertFalse(_successful_pricing_outcome(audit))
        audit["tool_outcomes"].append(
            {"tool_name": "calculate_shipkia_rate", "status": "success"}
        )
        self.assertTrue(_successful_pricing_outcome(audit))
        route_audit = {
            "tool_outcomes": [
                {"tool_name": "lookup_pincode_serviceability", "status": "success"}
            ]
        }
        self.assertTrue(_successful_pricing_outcome(route_audit))
        self.assertTrue(
            _is_participant_disconnect(
                {
                    "failure_code": "participant_disconnect",
                    "reason": "participant_disconnect_timeout",
                }
            )
        )

    def test_v5_rate_flow_is_complete_only_after_an_approved_close(self):
        unfinished = {
            "prompt_version": "shipkia-voice-v5",
            "state_snapshot": {
                "move_forward_question_due": True,
                "onboarding_link_presented": False,
                "better_plan_close_presented": False,
            },
        }
        onboarding_complete = {
            **unfinished,
            "state_snapshot": {
                **unfinished["state_snapshot"],
                "move_forward_question_due": False,
                "onboarding_link_presented": True,
            },
        }
        better_plan_complete = {
            **unfinished,
            "state_snapshot": {
                **unfinished["state_snapshot"],
                "move_forward_question_due": False,
                "better_plan_close_presented": True,
            },
        }

        self.assertFalse(_rate_flow_completed(unfinished))
        self.assertTrue(_rate_flow_completed(onboarding_complete))
        self.assertTrue(_rate_flow_completed(better_plan_complete))
        self.assertTrue(_rate_flow_completed({"prompt_version": "shipkia-voice-v4"}))

    def test_voice_v3_prompt_distinguishes_complete_and_additional_flat_rates(self):
        prompt = get_shipkia_voice_prompt("shipkia-voice-v3")

        self.assertIn("use calculate_shipkia_rate.flat_rate_options", prompt)
        self.assertIn("flat additional-weight component", prompt)
        self.assertIn("state only that service's returned current-shipment", prompt)
        self.assertIn("stop without asking a follow-up question", prompt)
        self.assertIn("Shadowfax Surface 5 KG", prompt)
        self.assertIn("E-Kart SURFACE", prompt)

    def test_starting_rate_lookup_returns_general_and_zone_floors(self):
        general = get_shipkia_starting_rate({})
        invalid = get_shipkia_starting_rate({"zone": "invented"})

        self.assertEqual(general["status"], "success")
        self.assertEqual(general["response_type"], "general_starting")
        self.assertEqual(general["amount"], 22.0)
        self.assertFalse(general["gst_inclusive"])

        expected_zone_amounts = {
            "A": 22.07,
            "B": 25.96,
            "C": 31.15,
            "D": 35.05,
            "E": 53.22,
            "F": 54.52,
        }
        for zone, expected_amount in expected_zone_amounts.items():
            with self.subTest(zone=zone):
                result = get_shipkia_starting_rate({"zone": zone})
                self.assertEqual(result["status"], "success")
                self.assertEqual(result["response_type"], "zone_starting")
                self.assertEqual(result["zone"], zone)
                self.assertEqual(result["amount"], expected_amount)
                self.assertTrue(result["gst_inclusive"])

        zone_d = get_shipkia_starting_rate({"zone": "D"})
        self.assertEqual(
            zone_d["available_courier_partners"],
            ["Amazon", "Bluedart", "Delhivery", "E-Kart", "Shadowfax", "Shree Maruti", "Xpressbees"],
        )
        self.assertEqual(len(zone_d["starting_rate_options"]), 5)
        self.assertEqual(
            [option["courier"] for option in zone_d["starting_rate_options"]],
            ["Shree Maruti", "Amazon", "Delhivery", "Xpressbees", "E-Kart"],
        )
        self.assertEqual(
            [option["amount"] for option in zone_d["starting_rate_options"]],
            [35.05, 38.94, 45.43, 50.62, 76.58],
        )

        self.assertEqual(invalid["response_type"], "general_starting")
        self.assertEqual(invalid["amount"], 22.0)

    def test_shadowfax_surface_starting_rate_uses_requested_verified_zone(self):
        result = get_shipkia_starting_rate(
            {
                "zone": "C",
                "courier_partner": "Shadowfax",
                "transport_mode": "Surface",
            }
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["zone"], "C")
        self.assertEqual(result["amount"], 76.58)
        self.assertEqual(result["basis"]["courier"], "Shadowfax")
        self.assertEqual(result["basis"]["service"], "Shadowfax Surface 500 G")

    def test_flat_rate_catalog_returns_only_three_verified_ekart_slabs(self):
        result = get_shipkia_flat_rates({"response_scope": "All"})

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["response_type"], "flat_all")
        self.assertEqual(result["verified_flat_rate_count"], 3)
        self.assertTrue(result["excluded_additional_weight_components"])
        self.assertEqual(
            [
                (
                    option["service"],
                    option["min_weight_g"],
                    option["max_weight_g"],
                    option["shipping_charge"],
                    option["gst"],
                    option["total"],
                )
                for option in result["flat_rate_options"]
            ],
            [
                ("E-Kart SURFACE", 0, 500, 64.9, 11.68, 76.58),
                ("E-Kart SURFACE", 501, 1000, 74.8, 13.46, 88.26),
                ("E-Kart SURFACE", 1001, 2000, 88.0, 15.84, 103.84),
            ],
        )
        self.assertNotIn("E-Kart EXPRESS", str(result["flat_rate_options"]))
        self.assertNotIn("Shadowfax", str(result["flat_rate_options"]))

    def test_flat_zonal_catalog_returns_verified_ekart_express_zone_groups(self):
        result = get_shipkia_flat_zonal_rates({})

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["response_type"], "flat_zonal_all")
        self.assertEqual(result["service"], "E-Kart EXPRESS")
        self.assertEqual(
            [
                (group["zone_group"], group["zones"], group["max_weight_g"], group["total"])
                for group in result["zone_groups"]
            ],
            [
                ("A-B", ["A", "B"], 500, 84.37),
                ("C-F", ["C", "D", "E", "F"], 500, 109.03),
            ],
        )
        self.assertEqual(result["additional_weight"]["additional_weight_unit_g"], 500)
        self.assertEqual(result["additional_weight"]["total"], 38.94)

    def test_flat_rate_catalog_matches_boundaries_and_volumetric_weight(self):
        expected = {
            0.5: 76.58,
            0.501: 88.26,
            1.0: 88.26,
            1.001: 103.84,
            2.0: 103.84,
        }
        for weight, total in expected.items():
            with self.subTest(weight=weight):
                result = get_shipkia_flat_rates(
                    {
                        "response_scope": "Matching",
                        "dead_weight": weight,
                    }
                )
                self.assertEqual(result["status"], "success")
                self.assertEqual(result["response_type"], "flat_matching")
                self.assertTrue(result["exact_match_available"])
                self.assertEqual(result["flat_rate_options"][0]["total"], total)

        volumetric = get_shipkia_flat_rates(
            {
                "response_scope": "Matching",
                "dead_weight": 0.5,
                "length": 50,
                "width": 40,
                "height": 3,
            }
        )
        self.assertEqual(volumetric["chargeable_weight_g"], 1200)
        self.assertEqual(volumetric["flat_rate_options"][0]["total"], 103.84)

    def test_flat_rate_catalog_above_two_kg_returns_starting_fallback_only(self):
        result = get_shipkia_flat_rates(
            {
                "response_scope": "Matching",
                "dead_weight": 2.001,
            }
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["response_type"], "flat_starting_fallback")
        self.assertFalse(result["exact_match_available"])
        self.assertEqual(result["chargeable_weight_g"], 2001)
        self.assertEqual(len(result["flat_rate_options"]), 1)
        self.assertEqual(result["flat_rate_options"][0]["total"], 76.58)
        self.assertIn("do not imply", result["message"].lower())

    def test_flat_rate_catalog_requires_cod_value_then_returns_zero_cod_charge(self):
        missing = get_shipkia_flat_rates(
            {
                "response_scope": "Starting",
                "payment_type": "COD",
            }
        )
        self.assertEqual(missing["status"], "order_value_required")
        self.assertTrue(missing["cod_order_value_required"])
        self.assertEqual(missing["flat_rate_options"], [])

        result = get_shipkia_flat_rates(
            {
                "response_scope": "Matching",
                "dead_weight": 0.5,
                "payment_type": "COD",
                "order_value": 5000,
            }
        )
        option = result["flat_rate_options"][0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["payment_type"], "COD")
        self.assertEqual(option["cod_charge"], 0.0)
        self.assertEqual(option["total"], 76.58)

    def test_managed_prompt_sections_are_idempotent_and_capability_gated(self):
        original = "ShipKia voice prompt."
        once = _with_managed_shipkia_prompt(original)
        twice = _with_managed_shipkia_prompt(once)

        self.assertEqual(once, twice)
        self.assertEqual(once.count(RATE_PROMPT_MARKER), 1)
        self.assertEqual(once.count(USP_PROMPT_MARKER), 1)
        self.assertEqual(once.count(RATE_SALES_PROMPT_MARKER), 1)
        self.assertEqual(once.count(LANGUAGE_PROMPT_MARKER), 1)
        self.assertIn("WhatsApp and an automated voice call", once)
        self.assertIn("WhatsApp and IVR", once)
        self.assertIn("show the NDR status on the dashboard", once)
        self.assertIn("unless an approved tool returns a verified success result", once)
        self.assertIn("do not trigger order-confirmation or NDR workflows", once)
        self.assertIn("reply naturally in English", once)
        self.assertIn("reply in natural conversational Hinglish", once)
        self.assertIn("Do not translate, repeat, or paraphrase", once)
        self.assertEqual(once.count("Namaste! Main ShipKia ka assistant hoon"), 1)
        self.assertIn("worker-controlled gated state", once)
        self.assertIn("flat_rate_options", once)
        self.assertIn("optional qualification in order", once)
        self.assertIn("and main problem", once)
        self.assertIn("same pending question", once)
        self.assertIn("6-digit pickup pincode", once)
        self.assertIn("Weight is mandatory and must never be assumed", once)
        self.assertIn("exact rate depends on", once)
        self.assertIn("https://auth.shipkia.com/signup", once)
        self.assertIn("Do not push, repeatedly offer, or assume a scheduled sales call", once)
        self.assertIn("hard price", once)
        self.assertIn("quoted GST-inclusive total", once)
        self.assertIn("Direct Console remains read-only", once)

    def test_shipkia_dispatch_guard_rejects_automatic_or_duplicate_dispatches(self):
        self.assertIsNone(_shipkia_dispatch_conflict([]))
        self.assertEqual(
            _removable_automatic_dispatch_ids([{"id": "AD_auto", "agent_name": ""}]),
            ["AD_auto"],
        )
        self.assertEqual(
            _removable_automatic_dispatch_ids(
                [
                    {
                        "id": "AD_active_auto",
                        "agent_name": "",
                        "state": {"job": {"id": "AJ_english"}},
                    }
                ]
            ),
            [],
        )
        self.assertIn(
            "existing dispatches detected",
            _shipkia_dispatch_conflict([{"id": "AD_auto", "agent_name": ""}]),
        )
        self.assertIn(
            "expected one dispatch",
            _shipkia_dispatch_conflict(
                [
                    {"id": "AD_auto", "agent_name": ""},
                    {"id": "AD_shipkia", "agent_name": "shipkia-voice-sales"},
                ],
                expected_dispatch_id="AD_shipkia",
            ),
        )

    def test_shipkia_dispatch_guard_accepts_only_the_expected_named_dispatch(self):
        self.assertIsNone(
            _shipkia_dispatch_conflict(
                [{"id": "AD_shipkia", "agent_name": "shipkia-voice-sales"}],
                expected_dispatch_id="AD_shipkia",
            )
        )
        self.assertIn(
            "unexpected dispatch agent",
            _shipkia_dispatch_conflict(
                [{"id": "AD_shipkia", "agent_name": ""}],
                expected_dispatch_id="AD_shipkia",
            ),
        )

    def test_rate_sales_prompt_prioritizes_request_and_speaks_verified_price(self):
        prompt = _with_managed_shipkia_prompt("ShipKia voice prompt.")

        self.assertIn("customer-request-first approach", prompt)
        self.assertIn("worker-gated optional", prompt)
        self.assertIn("explicit unknown/refusal", prompt)
        self.assertIn('ShipKia rates ₹{amount} se start hote hain', prompt)
        self.assertIn("does not share or does not have a current rate", prompt)
        self.assertIn("End the remaining optional qualification", prompt)
        self.assertIn("avoidable RTO reduce karne mein", prompt)
        self.assertIn("Never add an arbitrary margin", prompt)
        self.assertIn("Never quote a remembered or universal starting rate", prompt)
        self.assertIn('"Standard"', prompt)
        self.assertIn('is not "Express," and "Surface" is not "Air."', prompt)
        self.assertIn("requested_service_unavailable", prompt)
        self.assertIn("verified transit time is not available", prompt)
        self.assertIn('mode="Express"', prompt)
        self.assertIn('mode="Fast"', prompt)
        self.assertIn('Never create a name such as "Durata Express"', prompt)

    def test_normalize_phone(self):
        self.assertEqual(normalize_phone("98765 43210"), "+919876543210")
        self.assertEqual(normalize_phone("09876543210"), "+919876543210")
        self.assertEqual(normalize_phone("+91-98765-43210"), "+919876543210")

    def test_crm_lead_upsert_is_phone_deduplicated_and_non_destructive(self):
        phone = f"+9198{secrets.randbelow(10**8):08d}"
        first = create_or_update_shipkia_lead(
            {
                "phone": phone,
                "customer_name": "Local Voice Test",
                "organization": "Example Brand",
                "shipkia_business_type": "D2C",
                "shipkia_monthly_shipments": 125,
                "shipkia_current_provider_type": "Shipping Aggregator",
                "shipkia_current_courier_partner": "Example Aggregator",
                "shipkia_current_shipping_rate": 42.5,
                "shipkia_current_rate_basis": "500 g prepaid, GST included, Delhi to Mumbai",
                "shipkia_interested_services": "Flat rates, Onboarding",
                "shipkia_chat_summary": "Customer wants a flat-rate comparison.",
            },
            agent="agent-445",
        )
        second = create_or_update_shipkia_lead(
            {
                "phone": phone.replace("+91", ""),
                "organization": "",
                "shipkia_business_type": "",
                "shipkia_main_pain_point": "High Rates",
            },
            agent="agent-445",
        )

        self.assertEqual(first["status"], "success")
        self.assertEqual(first["action"], "created")
        self.assertEqual(second["status"], "success")
        self.assertEqual(second["action"], "updated")
        self.assertEqual(first["lead"], second["lead"])

        lead = frappe.get_doc("CRM Lead", first["lead"])
        self.assertEqual(lead.organization, "Example Brand")
        self.assertEqual(lead.shipkia_business_type, "D2C")
        self.assertEqual(lead.shipkia_monthly_shipments, 125)
        self.assertEqual(lead.shipkia_current_provider_type, "Shipping Aggregator")
        self.assertEqual(lead.shipkia_current_courier_partner, "Example Aggregator")
        self.assertEqual(float(lead.shipkia_current_shipping_rate), 42.5)
        self.assertEqual(
            lead.shipkia_current_rate_basis,
            "500 g prepaid, GST included, Delhi to Mumbai",
        )
        self.assertEqual(lead.shipkia_main_pain_point, "High Rates")
        self.assertIn("Flat rates", lead.shipkia_interested_services)
        self.assertEqual(lead.shipkia_chat_summary, "Customer wants a flat-rate comparison.")

        lookup = lookup_shipkia_crm_lead({"phone": phone}, agent="agent-445")
        self.assertTrue(lookup["found"])
        self.assertEqual(lookup["lead"], first["lead"])


    def test_rate_card_calculates_prepaid_zone_rate(self):
        serviceability = lookup_pincode_serviceability(
            {"pickup_pincode": "110001", "delivery_pincode": "400001"},
            agent="agent-445",
        )
        rate = calculate_shipkia_rate(
            {
                "pickup_pincode": "110001",
                "delivery_pincode": "400001",
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
                "zone": "A",
            },
            agent="agent-445",
        )
        self.assertEqual(serviceability["status"], "success")
        self.assertTrue(serviceability["serviceable"])
        self.assertEqual(serviceability["zone"], "C")
        self.assertTrue(serviceability["zone_verified"])
        self.assertEqual(serviceability["starting_rate"]["amount"], 31.15)
        self.assertEqual(rate["status"], "success")
        self.assertEqual(rate["rate_card"]["version"], "Rate Card 10 - June")
        self.assertEqual(rate["eligible_rates"][0]["service"], "Shree Maruti Surface")
        self.assertEqual(rate["eligible_rates"][0]["shipping_charge"], 18.7)
        self.assertEqual(rate["eligible_rates"][0]["gst"], 3.37)
        self.assertEqual(rate["eligible_rates"][0]["total"], 22.07)

    def test_v5_route_resolver_supports_ncr_locations_and_pan_india(self):
        ncr = lookup_pincode_serviceability(
            {"pickup_location": "Delhi", "delivery_location": "Noida"},
            agent="agent-445",
        )
        pan_india = lookup_pincode_serviceability(
            {"pan_india": True},
            agent="agent-445",
        )

        self.assertEqual(ncr["zone"], "A")
        self.assertEqual(ncr["starting_rate"]["amount"], 22.07)
        self.assertEqual(pan_india["zone"], "A")
        self.assertEqual(
            pan_india["resolution_basis"],
            "pan_india_zone_a_starting_policy",
        )
        self.assertEqual(pan_india["starting_rate"]["amount"], 22.07)

    def test_rate_card_returns_zone_matrix_when_zone_is_unknown(self):
        rate = calculate_shipkia_rate(
            {
                "pickup_pincode": "110001",
                "delivery_pincode": "400001",
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
                "courier": "Delhivery Surface",
            },
            agent="agent-445",
        )
        self.assertEqual(rate["status"], "success")
        self.assertTrue(rate["zone_required"])
        self.assertFalse(rate["eligible_rates"][0]["is_flat_rate"])
        self.assertIsNone(rate["eligible_rates"][0]["flat_rate_breakdown"])
        self.assertFalse(rate["eligible_rates"][0]["additional_rate_is_flat"])
        self.assertIsNone(rate["eligible_rates"][0]["flat_additional_rate_breakdown"])
        self.assertFalse(rate["flat_rate_available"])
        self.assertEqual(rate["flat_rate_options"], [])
        self.assertFalse(rate["flat_additional_rate_available"])
        self.assertEqual(rate["flat_additional_rate_options"], [])
        self.assertEqual(rate["eligible_rates"][0]["zone_breakdowns"]["A"]["shipping_charge"], 27.5)
        self.assertEqual(rate["eligible_rates"][0]["zone_breakdowns"]["F"]["shipping_charge"], 51.7)

    def test_rate_card_detects_ekart_surface_500g_all_zone_flat_rate(self):
        rate = calculate_shipkia_rate(
            {
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
                "service": "E-Kart SURFACE",
            },
            agent="agent-445",
        )

        self.assertEqual(rate["status"], "success")
        self.assertFalse(rate["zone_required"])
        self.assertTrue(rate["flat_rate_available"])
        self.assertEqual(len(rate["eligible_rates"]), 1)
        option = rate["eligible_rates"][0]
        self.assertEqual(option["service"], "E-Kart SURFACE")
        self.assertTrue(option["is_flat_rate"])
        self.assertEqual(option["flat_rate_breakdown"]["shipping_charge"], 64.9)
        self.assertEqual(option["flat_rate_breakdown"]["gst"], 11.68)
        self.assertEqual(option["flat_rate_breakdown"]["total"], 76.58)
        self.assertEqual(set(option["zone_breakdowns"]), set("ABCDEF"))
        self.assertTrue(
            all(
                breakdown == option["flat_rate_breakdown"]
                for breakdown in option["zone_breakdowns"].values()
            )
        )
        self.assertEqual(
            [flat_option["service"] for flat_option in rate["flat_rate_options"]],
            ["E-Kart SURFACE"],
        )

    def test_generic_500g_result_exposes_flat_option_without_replacing_cheapest_rates(self):
        rate = calculate_shipkia_rate(
            {
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
            },
            agent="agent-445",
        )

        self.assertTrue(rate["zone_required"])
        self.assertEqual(
            [option["service"] for option in rate["eligible_rates"]],
            ["Shree Maruti Surface", "Amazon Shipping Standard", "Delhivery Surface"],
        )
        self.assertEqual(
            [option["service"] for option in rate["flat_rate_options"]],
            ["E-Kart SURFACE"],
        )
        self.assertEqual(
            rate["flat_rate_options"][0]["flat_rate_breakdown"]["total"],
            76.58,
        )
        self.assertNotIn("zone_breakdowns", rate["flat_rate_options"][0])
        self.assertTrue(rate["flat_additional_rate_available"])
        self.assertEqual(
            [option["service"] for option in rate["flat_additional_rate_options"]],
            ["Shadowfax Surface 5 KG", "E-Kart EXPRESS"],
        )
        shadowfax = rate["flat_additional_rate_options"][0]
        self.assertEqual(shadowfax["courier_partner"], "Shadowfax")
        self.assertEqual(shadowfax["applies_after_weight_g"], 10000)
        self.assertEqual(shadowfax["additional_weight_unit_g"], 1000)
        self.assertEqual(
            shadowfax["flat_additional_rate_breakdown"]["shipping_charge"],
            9.9,
        )
        self.assertEqual(shadowfax["flat_additional_rate_breakdown"]["gst"], 1.78)
        self.assertEqual(shadowfax["flat_additional_rate_breakdown"]["total"], 11.68)
        self.assertEqual(
            rate["flat_additional_rate_options"][1]["additional_weight_unit_g"],
            500,
        )

    def test_call_1411_returns_distinct_cod_rate_and_verified_flat_option(self):
        prepaid = calculate_shipkia_rate(
            {
                "pickup_pincode": "201305",
                "delivery_pincode": "110001",
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
                "courier": "Delhivery Surface",
            },
            agent="agent-445",
        )
        cod = calculate_shipkia_rate(
            {
                "pickup_pincode": "201305",
                "delivery_pincode": "110001",
                "dead_weight": 0.5,
                "payment_type": "COD",
                "order_value": 5000,
                "courier": "Delhivery Surface",
            },
            agent="agent-445",
        )
        flat = calculate_shipkia_rate(
            {
                "pickup_pincode": "201305",
                "delivery_pincode": "110001",
                "dead_weight": 0.5,
                "payment_type": "COD",
                "order_value": 5000,
            },
            agent="agent-445",
        )

        prepaid_a = prepaid["eligible_rates"][0]["zone_breakdowns"]["A"]
        cod_a = cod["eligible_rates"][0]["zone_breakdowns"]["A"]
        self.assertEqual(prepaid_a["total"], 32.45)
        self.assertEqual(cod_a["cod_charge"], 90.0)
        self.assertEqual(cod_a["total"], 138.65)
        self.assertNotEqual(prepaid_a["total"], cod_a["total"])
        self.assertTrue(flat["flat_rate_available"])
        self.assertEqual(flat["flat_rate_options"][0]["service"], "E-Kart SURFACE")
        self.assertEqual(
            flat["flat_rate_options"][0]["flat_rate_breakdown"]["total"],
            76.58,
        )

    def test_shadowfax_flat_additional_rate_does_not_mark_base_as_flat(self):
        rate = calculate_shipkia_rate(
            {
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
                "service": "Shadowfax Surface 5 KG",
            },
            agent="agent-445",
        )

        self.assertEqual(rate["status"], "success")
        self.assertTrue(rate["zone_required"])
        self.assertFalse(rate["flat_rate_available"])
        self.assertEqual(rate["flat_rate_options"], [])
        self.assertTrue(rate["flat_additional_rate_available"])
        self.assertEqual(len(rate["eligible_rates"]), 1)
        option = rate["eligible_rates"][0]
        self.assertFalse(option["is_flat_rate"])
        self.assertIsNone(option["flat_rate_breakdown"])
        self.assertTrue(option["additional_rate_is_flat"])
        self.assertEqual(
            option["zone_breakdowns"]["A"]["shipping_charge"],
            86.9,
        )
        self.assertEqual(
            option["zone_breakdowns"]["F"]["shipping_charge"],
            207.9,
        )
        additional = rate["flat_additional_rate_options"][0]
        self.assertEqual(additional["service"], "Shadowfax Surface 5 KG")
        self.assertEqual(additional["applies_after_weight_g"], 10000)
        self.assertEqual(additional["additional_weight_unit_g"], 1000)
        self.assertEqual(
            additional["flat_additional_rate_breakdown"],
            {
                "shipping_charge": 9.9,
                "cod_charge": 0.0,
                "gst": 1.78,
                "total": 11.68,
            },
        )

    def test_rate_card_uses_volumetric_weight_and_additional_slabs(self):
        rate = calculate_shipkia_rate(
            {
                "pickup_pincode": "110001",
                "delivery_pincode": "400001",
                "dead_weight": 0.5,
                "length": 50,
                "width": 40,
                "height": 30,
                "payment_type": "Prepaid",
                "zone": "A",
                "courier": "Delhivery Surface",
            },
            agent="agent-445",
        )
        option = rate["eligible_rates"][0]
        self.assertEqual(rate["chargeable_weight_kg"], 12.0)
        self.assertEqual(option["additional_units"], 23)
        self.assertEqual(option["shipping_charge"], 584.1)

    def test_rate_card_calculates_cod_minimum_percentage_and_gst(self):
        rate = calculate_shipkia_rate(
            {
                "pickup_pincode": "110001",
                "delivery_pincode": "400001",
                "dead_weight": 0.5,
                "payment_type": "COD",
                "order_value": 2000,
                "zone": "A",
                "courier": "Delhivery Surface",
            },
            agent="agent-445",
        )
        option = rate["eligible_rates"][0]
        self.assertEqual(option["shipping_charge"], 27.5)
        self.assertEqual(option["cod_charge"], 36.0)
        self.assertEqual(option["gst"], 11.43)
        self.assertEqual(option["total"], 74.93)

    def test_zero_rate_is_not_returned_as_free(self):
        rate = calculate_shipkia_rate(
            {
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
                "movement_type": "RTO",
                "zone": "A",
                "courier": "E-Kart",
            },
            agent="agent-445",
        )
        self.assertEqual(rate["status"], "requested_service_unavailable")
        self.assertTrue(rate["preferred_courier_unavailable"])
        self.assertEqual(rate["eligible_rates"], [])

    def test_amazon_air_does_not_fall_back_to_surface_or_other_couriers(self):
        rate = calculate_shipkia_rate(
            {
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
                "courier": "Amazon",
                "mode": "Air",
            },
            agent="agent-445",
        )
        self.assertEqual(rate["status"], "requested_service_unavailable")
        self.assertFalse(rate["zone_required"])
        self.assertEqual(rate["eligible_rates"], [])
        self.assertEqual(
            rate["available_services_for_requested_courier"],
            ["Amazon Shipping Standard"],
        )
        self.assertNotIn("Amazon Shipping Standard", rate["available_services_for_requested_mode"])
        self.assertIn("Do not rename a Standard service as Express", rate["message"])

    def test_amazon_standard_returns_only_the_excel_service(self):
        rate = calculate_shipkia_rate(
            {
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
                "courier": "Amazon Shipping Standard",
                "zone": "A",
            },
            agent="agent-445",
        )
        self.assertEqual(rate["status"], "success")
        self.assertEqual(
            [option["service"] for option in rate["eligible_rates"]],
            ["Amazon Shipping Standard"],
        )

    def test_unknown_courier_is_reported_unavailable_without_alternatives(self):
        rate = calculate_shipkia_rate(
            {
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
                "courier": "Imaginary Express",
                "zone": "A",
            },
            agent="agent-445",
        )
        self.assertEqual(rate["status"], "requested_service_unavailable")
        self.assertEqual(rate["eligible_rates"], [])
        self.assertEqual(rate["available_services_for_requested_courier"], [])

    def test_express_filter_returns_only_excel_services_named_express(self):
        rate = calculate_shipkia_rate(
            {
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
                "mode": "Express",
                "zone": "A",
            },
            agent="agent-445",
        )
        self.assertEqual(rate["status"], "success")
        services = [option["service"] for option in rate["eligible_rates"]]
        self.assertTrue(services)
        self.assertTrue(all("express" in service.lower() for service in services))
        self.assertNotIn("Amazon Shipping Standard", services)
        self.assertFalse(rate["transit_time_available"])

    def test_invented_exact_service_is_rejected_instead_of_ignored(self):
        rate = calculate_shipkia_rate(
            {
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
                "service": "Durata Express",
                "zone": "A",
            },
            agent="agent-445",
        )
        self.assertEqual(rate["status"], "requested_service_unavailable")
        self.assertEqual(rate["eligible_rates"], [])
        self.assertTrue(rate["exact_service_unavailable"])
        self.assertEqual(rate["available_services_for_requested_service"], [])

    def test_exact_service_filter_returns_only_that_excel_service(self):
        rate = calculate_shipkia_rate(
            {
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
                "service": "Amazon Shipping Standard",
                "zone": "A",
            },
            agent="agent-445",
        )
        self.assertEqual(rate["status"], "success")
        self.assertEqual(
            [option["service"] for option in rate["eligible_rates"]],
            ["Amazon Shipping Standard"],
        )

    def test_fast_delivery_returns_only_air_or_express_named_excel_services(self):
        rate = calculate_shipkia_rate(
            {
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
                "mode": "Fast",
                "zone": "A",
            },
            agent="agent-445",
        )
        self.assertEqual(rate["status"], "success")
        services = [option["service"].lower() for option in rate["eligible_rates"]]
        self.assertTrue(services)
        self.assertTrue(all("air" in service or "express" in service for service in services))
        self.assertNotIn("amazon shipping standard", services)
        self.assertFalse(rate["transit_time_available"])
        self.assertIn("not verified as fastest", rate["speed_selection_note"])

    def test_unknown_zone_response_does_not_ask_customer_for_internal_zone(self):
        rate = calculate_shipkia_rate(
            {
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
                "courier": "Amazon",
            },
            agent="agent-445",
        )
        self.assertEqual(rate["status"], "success")
        self.assertTrue(rate["zone_required"])
        self.assertIn("Do not ask the customer to identify an internal zone", rate["message"])

    def test_cod_without_order_value_still_returns_shipping_and_formula(self):
        rate = calculate_shipkia_rate(
            {
                "dead_weight": 0.5,
                "payment_type": "COD",
                "zone": "A",
                "courier": "Delhivery Surface",
            },
            agent="agent-445",
        )
        option = rate["eligible_rates"][0]
        self.assertTrue(rate["cod_order_value_required"])
        self.assertEqual(option["shipping_charge"], 27.5)
        self.assertIsNone(option["total"])
        self.assertIn("order value", option["cod_formula"])
