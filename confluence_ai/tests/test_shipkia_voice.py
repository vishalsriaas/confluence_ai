from __future__ import annotations

import secrets
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from confluence_ai.prompts.shipkia_voice import (
    SHIPKIA_VOICE_PROMPT_VERSION,
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
    _shipkia_dispatch_conflict,
)
from confluence_ai.services.shipkia_voice import (
    calculate_shipkia_rate,
    create_or_update_shipkia_lead,
    lookup_pincode_serviceability,
    lookup_shipkia_crm_lead,
    normalize_phone,
)


class TestShipKiaVoice(FrappeTestCase):
    def test_voice_v3_prompt_has_provider_rate_and_safe_benefit_flow(self):
        prompt = get_shipkia_voice_prompt(SHIPKIA_VOICE_PROMPT_VERSION)

        self.assertEqual(SHIPKIA_VOICE_PROMPT_VERSION, "shipkia-voice-v3")
        self.assertEqual(
            list_shipkia_voice_prompt_versions(),
            ["shipkia-voice-v2", "shipkia-voice-v3"],
        )
        self.assertIn("Direct Courier", prompt)
        self.assertIn("Shipping Aggregator", prompt)
        self.assertIn("current rate for a comparable shipment", prompt)
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
        self.assertIn("answered-fields checklist", prompt)
        self.assertIn("detail supplied inside a", prompt)
        self.assertIn("multi-detail answer", prompt)
        self.assertIn("brand or business name", prompt)
        self.assertIn("handled question again", prompt)
        self.assertIn("shipping challenge the next qualification priority", prompt)
        self.assertIn("Aapko shipping operations mein abhi sabse badi challenge", prompt)
        self.assertIn("relevant solution using only approved ShipKia capabilities", prompt)
        self.assertIn("6-digit pickup pincode", prompt)
        self.assertIn("Both pickup_pincode and delivery_pincode are mandatory", prompt)
        self.assertIn("even when the requested service appears", prompt)
        self.assertIn("https://auth.shipkia.com/signup", prompt)
        self.assertIn("Do not push, repeatedly offer, or assume a scheduled sales call", prompt)
        self.assertIn("hard price floor", prompt)
        self.assertIn("switch from a quoted GST-inclusive total", prompt)
        self.assertIn("use calculate_shipkia_rate.flat_rate_options", prompt)
        self.assertIn("calculate_shipkia_rate.flat_additional_rate_options", prompt)
        self.assertIn("shipment charge remains zone-dependent", prompt)
        self.assertIn("Never call a lowest, average, starting", prompt)
        self.assertIn("production task provides a customer phone", prompt)
        self.assertNotIn("guaranteed RTO reduction", prompt)

    def test_voice_v3_prompt_distinguishes_complete_and_additional_flat_rates(self):
        prompt = get_shipkia_voice_prompt("shipkia-voice-v3")

        self.assertIn("use calculate_shipkia_rate.flat_rate_options", prompt)
        self.assertIn("calculate_shipkia_rate.flat_additional_rate_options", prompt)
        self.assertIn("shipment charge remains zone-dependent", prompt)
        self.assertIn("never describe a flat additional-weight component", prompt)
        self.assertIn("If Shadowfax Surface 5 KG is returned", prompt)
        self.assertIn("do not rename SURFACE as", prompt)

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
        self.assertIn("answered-fields checklist", once)
        self.assertIn("flat_rate_options", once)
        self.assertIn("brand/business name", once)
        self.assertIn("main shipping challenge the next qualification priority", once)
        self.assertIn("Aapko shipping operations mein abhi sabse", once)
        self.assertIn("6-digit pickup pincode", once)
        self.assertIn("Never call the rate tool or quote a rate before both pincodes", once)
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
        self.assertIn("Do not require a", prompt)
        self.assertIn("current rate before giving the requested ShipKia rate", prompt)
        self.assertIn('ShipKia rates ₹{amount} se start hote hain', prompt)
        self.assertIn("does not share or does not have a current rate", prompt)
        self.assertIn("monthly shipment volume", prompt)
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
        self.assertEqual(serviceability["status"], "configuration_required")
        self.assertIsNone(serviceability["serviceable"])
        self.assertEqual(rate["status"], "success")
        self.assertEqual(rate["rate_card"]["version"], "Rate Card 10 - June")
        self.assertEqual(rate["eligible_rates"][0]["service"], "Shree Maruti Surface")
        self.assertEqual(rate["eligible_rates"][0]["shipping_charge"], 18.7)
        self.assertEqual(rate["eligible_rates"][0]["gst"], 3.37)
        self.assertEqual(rate["eligible_rates"][0]["total"], 22.07)

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
