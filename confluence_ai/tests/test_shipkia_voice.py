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
        self.assertNotIn("guaranteed RTO reduction", prompt)

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

    def test_rate_sales_prompt_asks_current_rate_and_speaks_verified_starting_price(self):
        prompt = _with_managed_shipkia_prompt("ShipKia voice prompt.")

        self.assertIn("current-rate-first approach", prompt)
        self.assertIn("Aap abhi similar shipment ke liye approximately kitna rate", prompt)
        self.assertIn('ShipKia rates ₹{amount} se start hote hain', prompt)
        self.assertIn("does not share or does not have a current rate", prompt)
        self.assertIn("shipment volume is known", prompt)
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
                "shipkia_business_type": "D2C",
                "shipkia_monthly_shipments": 125,
                "shipkia_current_provider_type": "Shipping Aggregator",
                "shipkia_current_courier_partner": "Example Aggregator",
                "shipkia_current_shipping_rate": 42.5,
                "shipkia_current_rate_basis": "500 g prepaid, GST included, Delhi to Mumbai",
            },
            agent="agent-445",
        )
        second = create_or_update_shipkia_lead(
            {
                "phone": phone.replace("+91", ""),
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
        self.assertEqual(rate["eligible_rates"][0]["zone_breakdowns"]["A"]["shipping_charge"], 27.5)
        self.assertEqual(rate["eligible_rates"][0]["zone_breakdowns"]["F"]["shipping_charge"], 51.7)

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
