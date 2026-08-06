from __future__ import annotations

import asyncio
import unittest

from livekit_agent.conversation_state import GatedConversationState, SemanticAnswerGuard


def decision(field, value, evidence, *, disposition="answered", confidence=0.99):
    return {
        "field": field,
        "disposition": disposition,
        "value": value,
        "evidence": evidence,
        "confidence": confidence,
    }


def apply(state, text, *decisions, turn_id="turn", turn_disposition=None):
    return state.apply_classifier_result(
        {
            "turn_disposition": turn_disposition
            or ("answered" if decisions else "unrelated"),
            "decisions": list(decisions),
        },
        customer_text=text,
        turn_id=turn_id,
    )


def arrangement_pending_state():
    state = GatedConversationState()
    apply(
        state,
        "Business Book Shop hai aur type D2C hai",
        decision("business_name", "Book Shop", "Book Shop"),
        decision("business_type", "D2C", "D2C"),
        turn_id="qualification",
    )
    return state


class TestGatedConversationState(unittest.TestCase):
    def test_call_1708_followup_rate_does_not_rearm_anything_else_checkpoint(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 1000})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates batao", turn_id="intent")
        state.mark_pricing_verified("lookup_pincode_serviceability")
        self.assertTrue(state.anything_else_question_due)
        state.mark_anything_else_question_presented()

        state.apply_deterministic_answers(
            "Flat rate available hai?",
            turn_id="flat-followup",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
        )

        self.assertTrue(state.anything_else_checkpoint_consumed)
        self.assertFalse(state.anything_else_question_due)
        self.assertTrue(state.flat_catalog_due())
        state.mark_flat_catalog_presented()
        state.mark_pricing_verified("get_shipkia_flat_rates")
        self.assertFalse(state.anything_else_question_due)
        self.assertNotIn("Kya aap kuch aur jaanna chahenge", state.guidance())

    def test_call_1708_flat_structure_and_asr_followups_do_not_loop(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 1000})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("flat rate batao", turn_id="flat")
        state.mark_flat_catalog_presented()
        state.mark_pricing_verified("get_shipkia_flat_rates")
        state.mark_anything_else_question_presented()

        state.apply_deterministic_answers(
            "so zone of flat rate",
            turn_id="structure",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
        )
        guidance = state.guidance()
        self.assertTrue(state.last_flat_structure_query)
        self.assertTrue(state.anything_else_checkpoint_consumed)
        self.assertIn("Flat is the route-independent all-zone", guidance)
        self.assertNotIn("Kya aap kuch aur jaanna chahenge", guidance)

        state.apply_deterministic_answers(
            "\u0938\u094d\u0915\u094d\u0935\u093e\u092f\u0930 \u091a\u0948\u0928\u0932 \u0930\u0947\u091f \u0939\u0948 \u0906\u092a\u0915\u0947 \u092a\u093e\u0938?",
            turn_id="flat-zonal-asr",
        )
        self.assertEqual(state.requested_rate_type, "Flat Zonal")
        self.assertTrue(state.flat_zonal_catalog_due())
        self.assertFalse(state.anything_else_question_due)
        self.assertIn("get_shipkia_flat_zonal_rates", state.guidance())

    def test_call_1708_forgotten_question_or_acknowledgement_moves_forward_once(self):
        for customer_text in (
            "Mujhe aur kuch jaanna tha, but main bhool gaya kya jaanna tha?",
            "\u092e\u0941\u091d\u0947 \u0914\u0930 \u0924\u094b \u0915\u0941\u091b \u091c\u093e\u0928\u0928\u093e \u0925\u093e, \u092c\u091f \u092e\u0948\u0902 \u092d\u0942\u0932 \u0917\u092f\u093e \u0915\u094d\u092f\u093e \u091c\u093e\u0928\u0928\u093e \u0925\u093e?",
            "\u0920\u0940\u0915 \u0939\u0948\u0964",
        ):
            with self.subTest(customer_text=customer_text):
                state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
                state.seed_context({"monthly_shipments": 1000})
                state.verified_pricing_tool = "get_shipkia_flat_rates"
                state.anything_else_checkpoint_consumed = True

                transitions = state.apply_deterministic_answers(
                    customer_text,
                    turn_id="post-info-done",
                    previous_agent_text="Flat aur Flat-Zonal alag pricing structures hain.",
                )

                self.assertTrue(state.move_forward_question_due)
                self.assertFalse(state.anything_else_question_due)
                self.assertEqual(
                    len([item for item in transitions if item["event"] == "post_information_completed"]),
                    1,
                )
                self.assertIn("ShipKia ke saath aage badhna", state.guidance())

    def test_call_1708_unclear_checkpoint_reply_is_not_forcibly_repeated(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.anything_else_question_due = True
        state.mark_anything_else_question_presented()
        state.last_turn_disposition = "unrelated"

        guidance = state.guidance()

        self.assertIn("Ji, main sun raha hoon", guidance)
        self.assertIn("Do not repeat or rephrase", guidance)
        self.assertNotIn("Ask exactly once: 'Kya aap kuch aur", guidance)

    def test_call_1707_about_shipkia_before_consent_resumes_only_consent(self):
        for customer_text in (
            "Aap mujhe pehle bataiye, ShipKia kya hai?",
            "\u0906\u092a \u092e\u0941\u091d\u0947 \u092a\u0939\u0932\u0947 \u092c\u0924\u093e\u0907\u090f, \u0936\u093f\u092a \u0915\u093f\u092f\u093e-\u0915\u094d\u092f\u093e \u0939\u0948?",
        ):
            with self.subTest(customer_text=customer_text):
                state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)

                transitions = state.apply_deterministic_answers(
                    customer_text,
                    turn_id="call-1707-about",
                )
                guidance = state.guidance()

                self.assertEqual(transitions, [])
                self.assertTrue(state.last_usp_query)
                self.assertEqual(state.pending_field(), "conversation_consent")
                self.assertIn("Answer the ShipKia information", guidance)
                self.assertIn("Kya abhi hum do minute baat kar sakte hain?", guidance)
                self.assertNotIn("shipping rates check karne ya onboarding", guidance)

    def test_call_1707_unclear_first_audio_cannot_advance_consent(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)

        transitions = state.apply_deterministic_answers("Adios.", turn_id="call-1707-noise")
        guidance = state.guidance()

        self.assertEqual(transitions, [])
        self.assertEqual(state.pending_field(), "conversation_consent")
        self.assertIn("convenient time to talk", guidance)
        self.assertNotIn("shipping rates check", guidance)

    def test_provider_question_before_consent_answers_then_resumes_consent(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)

        state.apply_deterministic_answers(
            "ShipKia mein kaun kaun se courier partners hain?",
            turn_id="pre-consent-providers",
        )
        guidance = state.guidance()

        self.assertTrue(state.last_provider_options_query)
        self.assertEqual(state.pending_field(), "conversation_consent")
        self.assertIn("Give these known partner names directly", guidance)
        self.assertIn("Kya abhi hum do minute baat kar sakte hain?", guidance)
        self.assertNotIn("shipping rates check karne ya onboarding", guidance)

    def test_early_side_queries_use_natural_help_continuation(self):
        expected = (
            "Aap shipping rates check karna chahenge ya onboarding mein help chahiye?"
        )
        for customer_text in (
            "ShipKia ki services kya kya hain?",
            "Aur kaun kaun se courier options available hain?",
        ):
            with self.subTest(customer_text=customer_text):
                state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
                state.apply_deterministic_answers("haan", turn_id="consent")
                state.apply_deterministic_answers(customer_text, turn_id="side-query")

                self.assertEqual(state.pending_field(), "assistance_intent")
                self.assertIn(expected, state.guidance())
                self.assertNotIn("kuch aur jaanna chahenge", state.guidance())

    def test_initial_assistance_choice_remains_short_before_any_side_query(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("haan", turn_id="consent")

        guidance = state.guidance()

        self.assertIn("check shipping rates or need onboarding help", guidance)
        self.assertNotIn("Iske alawa aap kuch aur jaanna chahenge", guidance)

    def test_generic_early_side_question_uses_natural_help_continuation(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("haan", turn_id="consent")
        state.apply_classifier_result(
            {"turn_disposition": "unrelated", "decisions": []},
            customer_text="Aapka office kahan hai?",
            turn_id="side-query",
            pending_field_at_turn_start="assistance_intent",
        )

        self.assertIn(
            "Aap shipping rates check karna chahenge ya onboarding mein help chahiye?",
            state.guidance(),
        )
        self.assertNotIn("kuch aur jaanna chahenge", state.guidance())

    def test_consumed_checkpoint_is_not_reasked_after_late_quantity_answer(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 1000})
        state.last_monthly_quantity_captured = True
        state.anything_else_checkpoint_consumed = True

        guidance = state.guidance()

        self.assertIn("acknowledge", guidance)
        self.assertIn("then stop", guidance)
        self.assertNotIn("Kya aap kuch aur jaanna chahenge", guidance)

    def test_call_1698_delivery_pincode_never_becomes_monthly_shipments(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("haan", turn_id="consent")
        state.apply_deterministic_answers("rates janna chahta hoon", turn_id="intent")
        state.monthly_quantity_due = True

        state.apply_deterministic_answers(
            "166403",
            turn_id="delivery-pincode",
            previous_agent_text=(
                "Pan India shipments ke liye mujhe aapka delivery pincode chahiye. "
                "Kya aap bata sakte hain?"
            ),
        )

        self.assertEqual(state.value("delivery_pincode"), "166403")
        self.assertFalse(state.is_handled("monthly_shipments"))
        self.assertTrue(state.monthly_quantity_due)

    def test_call_1693_filler_repeated_no_advances_once_to_move_forward(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.anything_else_question_due = True

        transitions = state.apply_deterministic_answers(
            "a nahin nahin.",
            turn_id="anything-else-no",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
        )

        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["event"], "anything_else_decided")
        self.assertEqual(state.anything_else_decision, "No")
        self.assertFalse(state.anything_else_question_due)
        self.assertTrue(state.move_forward_question_due)
        self.assertIn("ShipKia ke saath aage", state.guidance())

    def test_filler_repeated_no_is_not_a_close_without_checkpoint_context(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.anything_else_question_due = True

        transitions = state.apply_deterministic_answers(
            "a nahin nahin.",
            turn_id="unrelated-no",
            previous_agent_text="Aapka current shipping rate kya hai?",
        )

        self.assertEqual(transitions, [])
        self.assertTrue(state.anything_else_question_due)
        self.assertFalse(state.move_forward_question_due)

    def test_call_1688_monthly_volume_cannot_overwrite_current_rate(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("haan", turn_id="consent")
        state.apply_deterministic_answers("rates janna chahta hoon", turn_id="intent")
        state.apply_decision(
            field="current_shipping_rate",
            disposition="refused",
            value=None,
            evidence="nahin bata sakta",
            confidence=1.0,
            customer_text="Wo main nahin bata sakta",
            turn_id="rate-refusal",
        )
        state.monthly_quantity_due = True
        turn_id = "monthly-volume"
        state.apply_deterministic_answers(
            "around 5,000",
            turn_id=turn_id,
            previous_agent_text="Aapki monthly shipment quantity kitni hai?",
        )

        state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    decision("current_shipping_rate", 5000, "around 5,000")
                ],
            },
            customer_text="around 5,000",
            turn_id=turn_id,
            pending_field_at_turn_start="",
        )

        self.assertEqual(state.value("monthly_shipments"), 5000)
        self.assertEqual(state.value("current_shipping_rate"), "Not Shared")
        self.assertEqual(state.optional_ended_by, "current_shipping_rate")

    def test_call_1688_bluedart_availability_does_not_replace_current_provider(self):
        for customer_text, evidence in (
            ("Bluedart hain aapke paas?", "Bluedart"),
            ("\u092c\u094d\u0932\u0948\u0921\u0947\u091f \u0939\u0948\u0902 \u0906\u092a\u0915\u0947 \u092a\u093e\u0938?", "\u092c\u094d\u0932\u0948\u0921\u0947\u091f"),
        ):
            with self.subTest(customer_text=customer_text):
                state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
                state.seed_context(
                    {
                        "current_shipping_arrangement": "Shipping Aggregator",
                        "current_provider_name": "Shiprocket",
                    }
                )
                state.apply_deterministic_answers(customer_text, turn_id="provider-query")
                state.apply_classifier_result(
                    {
                        "turn_disposition": "answered",
                        "decisions": [
                            decision("current_provider_name", "Bluedart", evidence)
                        ],
                    },
                    customer_text=customer_text,
                    turn_id="provider-query",
                    pending_field_at_turn_start="",
                )

                self.assertTrue(state.last_provider_options_query)
                self.assertEqual(state.value("current_provider_name"), "Shiprocket")
                self.assertIn("Bluedart", state.guidance())

    def test_call_1684_pytant_captures_five_thousand_once(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("haan", turn_id="consent")
        state.apply_deterministic_answers("rates janna chahta hoon", turn_id="intent")
        state.monthly_quantity_due = True

        state.apply_deterministic_answers(
            "Pytant",
            turn_id="monthly-volume",
            previous_agent_text="Aapki monthly shipments kitni hoti hain?",
        )

        self.assertEqual(state.value("monthly_shipments"), 5000)
        self.assertTrue(state.is_handled("monthly_shipments"))
        self.assertFalse(state.monthly_quantity_due)
        self.assertTrue(state.anything_else_question_due)
        state.mark_pricing_verified("get_shipkia_flat_rates")
        state.mark_pricing_verified("get_shipkia_flat_zonal_rates")
        self.assertFalse(state.monthly_quantity_due)
        self.assertTrue(state.anything_else_question_due)

    def test_call_1684_or_services_query_requests_all_verified_usps(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)

        state.apply_deterministic_answers("Or services kya kya hai?", turn_id="services")

        self.assertTrue(state.last_usp_query)
        self.assertTrue(state.last_detailed_usp_query)
        self.assertFalse(state.last_provider_options_query)
        guidance = state.guidance().lower()
        for expected in ("multiple courier", "account manager", "whatsapp", "ivr"):
            self.assertIn(expected, guidance)

    def test_rates_intent_adds_one_time_qualification_bridge(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("haan", turn_id="consent")
        state.apply_deterministic_answers("rates janna chahta hoon", turn_id="intent")

        self.assertEqual(state.pending_field(), "business_name")
        self.assertTrue(state.qualification_bridge_due())
        self.assertIn("Rates batane se pehle", state.guidance())
        state.mark_qualification_bridge_presented()
        self.assertFalse(state.qualification_bridge_due())
        self.assertNotIn("Rates batane se pehle", state.guidance())

    def test_call_1660_options_query_lists_verified_rates_before_close(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"business_name": "Harsh Enterprises", "business_type": "D2C"})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="intent")
        state.apply_deterministic_answers(
            "nahi",
            turn_id="no-provider",
            previous_agent_text="Kya aap abhi koi courier partner ya shipping aggregator use karte hain?",
        )
        state.authorize_rate_result(
            {
                "status": "success",
                "response_type": "zone_starting",
                "zone": "D",
                "amount": 35.05,
                "available_courier_partners": [
                    "Amazon",
                    "Bluedart",
                    "Delhivery",
                    "E-Kart",
                    "Shadowfax",
                    "Shree Maruti",
                    "Xpressbees",
                ],
                "starting_rate_options": [
                    {"courier": "Shree Maruti", "service": "Shree Maruti Surface", "amount": 35.05, "weight_slab_g": 500, "movement_type": "Forward", "gst_inclusive": True},
                    {"courier": "Amazon", "service": "Amazon Shipping Standard", "amount": 38.94, "weight_slab_g": 500, "movement_type": "Forward", "gst_inclusive": True},
                    {"courier": "Delhivery", "service": "Delhivery Surface", "amount": 45.43, "weight_slab_g": 500, "movement_type": "Forward", "gst_inclusive": True},
                    {"courier": "Xpressbees", "service": "Surface Xpressbees 0.5 K.G", "amount": 50.62, "weight_slab_g": 500, "movement_type": "Forward", "gst_inclusive": True},
                    {"courier": "E-Kart", "service": "E-Kart SURFACE", "amount": 76.58, "weight_slab_g": 500, "movement_type": "Forward", "gst_inclusive": True},
                ],
            }
        )
        state.monthly_quantity_due = True
        customer_text = (
            "Meri monthly shipment 5000 hoti hai, but aur kya kya options available hain, "
            "kaun kaun se providers hain?"
        )

        state.apply_deterministic_answers(customer_text, turn_id="options")
        apply(
            state,
            customer_text,
            decision("service", "35 se starting", "35 se starting"),
            decision(
                "current_provider_name",
                "kaun kaun se providers hain",
                "kaun kaun se providers hain",
            ),
            turn_id="options",
        )

        self.assertEqual(state.value("monthly_shipments"), 5000)
        self.assertFalse(state.is_handled("current_provider_name"))
        self.assertFalse(state.is_handled("service"))
        self.assertTrue(state.last_provider_options_query)
        self.assertFalse(state.last_provider_rates_query)
        self.assertEqual(
            state.authorized_rate_amounts,
            {35.05, 38.94, 45.43, 50.62, 76.58},
        )
        guidance = state.guidance()
        self.assertIn("Shree Maruti", guidance)
        self.assertIn("Amazon", guidance)
        self.assertIn("names directly", guidance.casefold())
        self.assertIn("do not quote any rate", guidance.casefold())
        self.assertNotIn("Rs ", guidance)
        self.assertNotIn("kuch aur jaanna chahenge", guidance)
        self.assertIn("shipping rates check karna chahenge ya onboarding", guidance)
        self.assertIn("do not jump to the move-forward", guidance.casefold())

    def test_call_1662_rate_list_is_information_not_dissatisfaction(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 3000})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C rate batao", turn_id="rate")
        state.authorize_rate_result(
            {
                "status": "success",
                "response_type": "zone_starting",
                "amount": 31.15,
                "starting_rate_options": [
                    {"courier": "Amazon", "service": "Amazon Standard", "amount": 36.34}
                ],
            }
        )
        state.mark_pricing_verified("get_shipkia_starting_rate")

        state.apply_deterministic_answers(
            "sabke rates bata sakte ho tum mujhe",
            turn_id="all-rates",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
        )

        self.assertTrue(state.last_provider_options_query)
        self.assertTrue(state.last_provider_rates_query)
        self.assertFalse(state.last_customer_dissatisfied)
        self.assertFalse(state.unsatisfied_resolution_due)
        self.assertFalse(state.better_plan_close_due)
        self.assertTrue(state.anything_else_checkpoint_consumed)
        self.assertFalse(state.anything_else_question_due)
        guidance = state.guidance()
        self.assertIn("list every", guidance.casefold())
        self.assertIn("Amazon Standard", guidance)
        self.assertNotIn("Kya aap kuch aur jaanna chahenge", guidance)
        self.assertIn("End after answering", guidance)

    def test_anything_else_yes_asks_for_detail_before_move_forward(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 1000})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("flat rates batao", turn_id="rate")
        state.mark_pricing_verified("get_shipkia_flat_rates")

        state.apply_deterministic_answers(
            "haan ji",
            turn_id="anything-else-yes",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
        )

        self.assertTrue(state.anything_else_detail_due)
        self.assertFalse(state.move_forward_question_due)
        self.assertIn("aap kya jaanna chahenge", state.guidance())

    def test_call_1668_provider_names_are_available_before_rate_details(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")

        state.apply_deterministic_answers(
            "Kaun-kaun se shipping providers available hain aapke paas?",
            turn_id="providers",
            previous_agent_text="Rates check karna chahenge ya onboarding help chahiye?",
        )

        self.assertTrue(state.last_provider_options_query)
        guidance = state.guidance()
        for partner in (
            "Amazon",
            "Bluedart",
            "Delhivery",
            "E-Kart",
            "Shadowfax",
            "Shree Maruti",
            "Xpressbees",
        ):
            self.assertIn(partner, guidance)
        self.assertIn("names-only", guidance)
        self.assertIn("do not quote any rate", guidance)
        self.assertNotIn("Rs ", guidance)
        self.assertNotIn("kuch aur jaanna chahenge", guidance)
        self.assertIn("shipping rates check karna chahenge ya onboarding", guidance)

    def test_call_1668_hindi_no_thanks_advances_once_to_move_forward(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 5000})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C rate batao", turn_id="rate")
        state.mark_pricing_verified("get_shipkia_starting_rate")

        transitions = state.apply_deterministic_answers(
            "नहीं, थैंक यू।",
            turn_id="anything-else-no-thanks",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
        )

        decisions = [item for item in transitions if item.get("event") == "anything_else_decided"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(state.anything_else_decision, "No")
        self.assertFalse(state.anything_else_question_due)
        self.assertTrue(state.move_forward_question_due)
        self.assertIn("ShipKia ke saath aage badhna", state.guidance())

    def test_call_1675_danda_no_thanks_then_no_closes_without_reopening(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 5000})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone D rate batao", turn_id="rate")
        state.mark_pricing_verified("lookup_pincode_serviceability")

        transitions = state.apply_deterministic_answers(
            "\u0928\u0939\u0940\u0902\u0964 \u0925\u0948\u0902\u0915 \u092f\u0942\u0964",
            turn_id="anything-else-no-thanks",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
        )

        decisions = [item for item in transitions if item.get("event") == "anything_else_decided"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(state.anything_else_decision, "No")
        self.assertTrue(state.move_forward_question_due)

        state.apply_deterministic_answers(
            "\u0928\u0939\u0940\u0902\u0964",
            turn_id="move-forward-no",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahenge?",
        )
        self.assertEqual(state.move_forward_decision, "No")
        self.assertTrue(state.better_plan_close_due)
        self.assertTrue(state.pricing_close_locked())

    def test_negated_rate_intent_provider_question_stays_names_only(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")

        state.apply_deterministic_answers(
            "Haan mein shipping rate check karna nahi chahta. Mujhe yeh janna hai ki "
            "aapke pass kon kon se providers available hai?",
            turn_id="provider-side-question",
            previous_agent_text="Aap shipping rates check karna chahenge ya onboarding mein help chahiye?",
        )

        self.assertFalse(state.is_handled("assistance_intent"))
        self.assertTrue(state.last_provider_options_query)
        guidance = state.guidance()
        self.assertIn("Amazon", guidance)
        self.assertIn("Xpressbees", guidance)
        self.assertIn("do not quote any rate", guidance.casefold())
        self.assertNotIn("kuch aur jaanna chahenge", guidance)
        self.assertIn("shipping rates check karna chahenge ya onboarding", guidance)

    def test_call_1677_sparse_followup_preserves_verified_provider_rates(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.mark_route_zone_verified("C", starting_presented=True)
        state.authorize_rate_result(
            {
                "status": "success",
                "response_type": "zone_starting",
                "zone": "C",
                "amount": 31.15,
                "available_courier_partners": ["Shree Maruti", "Amazon"],
                "starting_rate_options": [
                    {"courier": "Shree Maruti", "service": "Shree Maruti Surface", "amount": 31.15},
                    {"courier": "Amazon", "service": "Amazon Shipping Standard", "amount": 36.34},
                ],
            }
        )

        state.authorize_rate_result(
            {
                "status": "success",
                "response_type": "zone_starting",
                "zone": "C",
                "amount": 31.15,
                "available_courier_partners": [],
                "starting_rate_options": [],
            }
        )

        self.assertEqual(
            [option["amount"] for option in state.verified_starting_options],
            [31.15, 36.34],
        )
        self.assertEqual(state.available_courier_partners, ["Shree Maruti", "Amazon"])
        self.assertEqual(state.authorized_rate_amounts, {31.15, 36.34})

    def test_call_1677_stray_business_type_fragment_cannot_overwrite_confirmation(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("D2C", turn_id="business-type")

        transitions = state.apply_deterministic_answers("g to c", turn_id="stray-fragment")

        self.assertEqual(transitions, [])
        self.assertEqual(state.value("business_type"), "D2C")

        corrected = state.apply_deterministic_answers(
            "sorry G2C",
            turn_id="explicit-correction",
        )
        self.assertTrue(any(item.get("field") == "business_type" for item in corrected))
        self.assertEqual(state.value("business_type"), "G2C")

    def test_call_1677_gujarati_asr_shiprocket_is_recognized_without_garbage(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"business_name": "Har Shankar", "business_type": "D2C"})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="intent")

        state.apply_deterministic_answers(
            "\u0a9c\u0ac0 \u0ab8\u0ac0\u0aaa\u0acd\u0ab0\u0acb\u0a95\u0ac7\u0a9f \u0aaf\u0ac1\u0a9d \u0a95\u0ab0 \u0ab0\u0ab9\u0abe \u0ab9\u0ac1\u0a82.",
            turn_id="provider",
            previous_agent_text="Kya aap abhi koi courier ya shipping aggregator use karte hain?",
        )

        self.assertEqual(state.value("current_shipping_arrangement"), "Shipping Aggregator")
        self.assertEqual(state.value("current_provider_name"), "Shiprocket")

    def test_call_1681_generic_services_query_requests_all_verified_usps(self):
        for customer_text in (
            "Aur kaun-kaun si services provide karte hain aap?",
            "\u0906\u092a \u0915\u094c\u0928-\u0915\u094c\u0928 \u0938\u0940 \u0938\u0930\u094d\u0935\u093f\u0938\u0947\u091c \u092a\u094d\u0930\u094b\u0935\u093e\u0907\u0921 \u0915\u0930\u0924\u0947 \u0939\u0948\u0902?",
        ):
            with self.subTest(customer_text=customer_text):
                state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
                state.apply_deterministic_answers("ji bataiye", turn_id="consent")
                state.apply_deterministic_answers(
                    customer_text,
                    turn_id="services",
                    previous_agent_text="Rates check karna chahenge ya onboarding help chahiye?",
                )

                self.assertTrue(state.last_usp_query)
                self.assertTrue(state.last_detailed_usp_query)
                self.assertFalse(state.last_provider_options_query)
                guidance = state.guidance()
                self.assertIn("all four verified facts", guidance)
                self.assertIn("dedicated account manager", guidance)
                self.assertIn("WhatsApp order confirmation", guidance)
                self.assertIn("call confirmation", guidance)
                self.assertIn("IVR-call follow-up", guidance)

        courier_state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        courier_state.apply_deterministic_answers(
            "E-Kart ki kaun si service available hai?",
            turn_id="courier-service",
        )
        self.assertFalse(courier_state.last_usp_query)
        self.assertTrue(courier_state.last_provider_options_query)

    def test_call_1681_provider_rate_asr_intent_persists_until_answered(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.mark_route_zone_verified("C", starting_presented=True)
        state.authorize_rate_result(
            {
                "status": "success",
                "response_type": "zone_starting",
                "zone": "C",
                "amount": 31.15,
                "starting_rate_options": [
                    {"courier": "Shree Maruti", "service": "Shree Maruti Surface", "amount": 31.15},
                    {"courier": "Amazon", "service": "Amazon Standard", "amount": 36.34},
                ],
            }
        )

        state.apply_deterministic_answers("dates to batao sabke", turn_id="asr-rates")
        self.assertTrue(state.provider_rates_answer_due)
        self.assertTrue(state.last_provider_rates_query)
        self.assertIn("List every", state.guidance())

        state.apply_deterministic_answers("dried dates 500 g", turn_id="asr-followup")
        self.assertTrue(state.provider_rates_answer_due)
        self.assertTrue(state.last_provider_rates_query)
        self.assertIn("List every", state.guidance())

        state.mark_provider_rates_presented()
        self.assertFalse(state.provider_rates_answer_due)

    def test_call_1681_semantic_comparison_route_and_quantity_cannot_corrupt_state(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"current_shipping_rate": 35})

        route_transitions = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    decision("pickup_location", "Delhi", "Delhi"),
                    decision("delivery_location", "Bangalore", "Bangalore"),
                ],
            },
            customer_text="Rs 35 from Delhi to Bangalore",
            turn_id="current-rate",
            pending_field_at_turn_start="current_shipping_rate",
        )
        quantity_transitions = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [decision("current_shipping_rate", 5000, "5,000")],
            },
            customer_text="5,000",
            turn_id="quantity",
            pending_field_at_turn_start="monthly_shipments",
        )

        self.assertEqual(route_transitions, [])
        self.assertEqual(quantity_transitions, [])
        self.assertFalse(state.is_handled("pickup_location"))
        self.assertFalse(state.is_handled("delivery_location"))
        self.assertEqual(state.value("current_shipping_rate"), 35)

    def test_call_1670_hindi_all_rates_is_information_not_dissatisfaction(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 5000})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C rate batao", turn_id="rate")
        state.authorize_rate_result(
            {
                "status": "success",
                "response_type": "zone_starting",
                "amount": 31.15,
                "starting_rate_options": [
                    {
                        "courier": "Shree Maruti",
                        "service": "Shree Maruti Surface",
                        "amount": 31.15,
                        "weight_slab_g": 500,
                        "movement_type": "Forward",
                        "gst_inclusive": True,
                    },
                    {
                        "courier": "Amazon",
                        "service": "Amazon Shipping Standard",
                        "amount": 36.34,
                        "weight_slab_g": 500,
                        "movement_type": "Forward",
                        "gst_inclusive": True,
                    },
                ],
            }
        )
        state.mark_pricing_verified("lookup_pincode_serviceability")

        for index, customer_text in enumerate(
            ("इन सबके रेट बता दीजिए।", "मैंने पूछा है सबके रेट बता दीजिए।")
        ):
            state.apply_deterministic_answers(
                customer_text,
                turn_id=f"all-rates-{index}",
                previous_agent_text="Kya aap kuch aur jaanna chahenge?",
            )
            self.assertTrue(state.last_provider_options_query)
            self.assertTrue(state.last_provider_rates_query)
            self.assertFalse(state.last_customer_dissatisfied)
            self.assertFalse(state.unsatisfied_resolution_due)
            self.assertFalse(state.better_plan_close_due)
            guidance = state.guidance()
            self.assertIn("list every", guidance.casefold())
            self.assertIn("Shree Maruti Surface", guidance)

    def test_explicit_detailed_shipkia_query_requests_all_verified_facts(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)

        state.apply_deterministic_answers(
            "ShipKia ki poori detail batao, kya kya facilities available hain?",
            turn_id="details",
        )

        self.assertTrue(state.last_usp_query)
        self.assertTrue(state.last_detailed_usp_query)
        guidance = state.guidance()
        self.assertIn("all four verified facts", guidance)
        self.assertIn("dedicated account manager", guidance)
        self.assertIn("WhatsApp plus IVR", guidance)

    def test_greeting_does_not_fill_current_problem(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context(
            {
                "business_name": "Harsh Enterprises",
                "business_type": "D2C",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_shipping_rate": 35,
            }
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates check karni hain", turn_id="intent")

        transitions = state.apply_deterministic_answers(
            "Hello.",
            turn_id="greeting",
            previous_agent_text="Shiprocket ke saath aapko kya problem aa rahi hai?",
        )

        self.assertFalse(state.is_handled("current_problem"))
        self.assertFalse(any(item.get("field") == "current_problem" for item in transitions))

    def test_real_problem_still_fills_current_problem(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context(
            {
                "business_name": "Harsh Enterprises",
                "business_type": "D2C",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_shipping_rate": 35,
            }
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates check karni hain", turn_id="intent")

        state.apply_deterministic_answers(
            "RTO follow-up mein dikkat hai.",
            turn_id="problem",
            previous_agent_text="Shiprocket ke saath aapko kya problem aa rahi hai?",
        )

        self.assertEqual(state.value("current_problem"), "RTO follow-up mein dikkat hai")

    def test_v5_unsatisfied_without_known_problem_asks_problem_then_team_solution(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 500})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C rate batao", turn_id="rate")
        state.authorize_rate_result(
            {"status": "success", "response_type": "zone_starting", "amount": 31.15}
        )
        state.mark_pricing_verified("get_shipkia_starting_rate")

        state.apply_deterministic_answers(
            "Main satisfied nahi hoon.",
            turn_id="unsatisfied",
        )

        self.assertTrue(state.unsatisfied_problem_due)
        self.assertFalse(state.move_forward_question_due)
        self.assertIn("what exact problem", state.guidance())

        state.apply_deterministic_answers(
            "Mujhe pricing aur support mein problem hai.",
            turn_id="unsatisfied-problem",
            previous_agent_text="Aapko exactly kya problem aa rahi hai?",
        )

        self.assertFalse(state.unsatisfied_problem_due)
        self.assertTrue(state.unsatisfied_resolution_due)
        guidance = state.guidance()
        self.assertIn("pricing aur support", guidance)
        self.assertIn("team", guidance)
        self.assertIn("solution ya better plan", guidance)

    def test_v5_unsatisfied_reuses_known_problem_without_asking_it_again(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"current_problem": "pricing problem", "monthly_shipments": 500})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C rate batao", turn_id="rate")
        state.authorize_rate_result(
            {"status": "success", "response_type": "zone_starting", "amount": 31.15}
        )
        state.mark_pricing_verified("get_shipkia_starting_rate")

        state.apply_deterministic_answers("Main satisfied nahi hoon.", turn_id="unsatisfied")

        self.assertFalse(state.unsatisfied_problem_due)
        self.assertTrue(state.unsatisfied_resolution_due)
        self.assertEqual(state.unsatisfied_concern, "pricing problem")
        self.assertNotIn("what exact problem", state.guidance())
        self.assertIn("solution ya better plan", state.guidance())

    def test_v5_call_1637_combined_rate_and_benefits_query_answers_usps_first(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")

        state.apply_deterministic_answers(
            "Main shipping rate check karna chahta hoon, aapka procedure kya hai aur kya benefit milega?",
            turn_id="intent-and-benefits",
        )

        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertTrue(state.last_usp_query)
        guidance = state.guidance()
        self.assertIn("dedicated account manager", guidance)
        self.assertIn("WhatsApp order confirmation", guidance)
        self.assertIn("IVR-call follow-up", guidance)
        self.assertIn("business or brand name", guidance)

    def test_v5_call_1637_verbose_no_provider_answer_is_handled_once(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"business_name": "Randhawa Transport", "business_type": "B2C"})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="intent")

        state.apply_deterministic_answers(
            "\u0939\u092e \u0905\u092d\u0940 \u0924\u0915 \u0915\u093f\u0938\u0940 provider \u0915\u093e \u0907\u0938\u094d\u0924\u0947\u092e\u093e\u0932 \u0928\u0939\u0940\u0902 \u0915\u0930\u0924\u0947, bas products launch karne ki planning hai.",
            turn_id="no-provider",
            previous_agent_text="Kya aap koi courier partner use karte hain ya apna arrangement hai?",
        )

        self.assertTrue(state.is_handled("current_shipping_arrangement"))
        self.assertEqual(state.value("current_shipping_arrangement"), "No Current Arrangement")
        self.assertEqual(state.optional_ended_by, "current_shipping_arrangement")
        self.assertEqual(state.pending_field(), "pickup_pincode")

    def test_v5_call_1637_hindi_kilo_is_weight_and_never_current_rate(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="intent")

        deterministic = state.apply_deterministic_answers(
            "Package ka wazan approximately 3.5 \u0915\u093f\u0932\u094b hai.",
            turn_id="weight",
        )
        semantic = apply(
            state,
            "Package ka wazan approximately 3.5 \u0915\u093f\u0932\u094b hai.",
            decision("current_shipping_rate", 3.5, "3.5 \u0915\u093f\u0932\u094b"),
            turn_id="weight",
        )

        self.assertTrue(any(item.get("field") == "dead_weight" for item in deterministic))
        self.assertEqual(state.value("dead_weight"), 3.5)
        self.assertFalse(state.is_handled("current_shipping_rate"))
        self.assertEqual(semantic, [])

    def test_v5_call_1637_quantity_sentence_captures_last_numeric_bound(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone D rate batao", turn_id="rate")
        state.mark_pricing_verified("get_shipkia_starting_rate")

        state.apply_deterministic_answers(
            "Yahi kuch 200 se 250 shipment hoti hain month ki.",
            turn_id="quantity",
            previous_agent_text="Aapki monthly shipments kitni rehti hain?",
        )

        self.assertEqual(state.value("monthly_shipments"), 250)
        self.assertFalse(state.monthly_quantity_due)
        self.assertTrue(state.anything_else_question_due)
        self.assertFalse(state.move_forward_question_due)

    def test_v5_call_1637_no_with_explanation_uses_better_plan_close(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 250})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone D rate batao", turn_id="rate")
        state.authorize_rate_result(
            {"status": "success", "response_type": "zone_starting", "amount": 35.05}
        )
        state.mark_pricing_verified("get_shipkia_starting_rate")
        state.apply_deterministic_answers(
            "nahi",
            turn_id="anything-else-no",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
        )

        state.apply_deterministic_answers(
            "Nahi, aapne mujhe explain nahi kara.",
            turn_id="no-with-reason",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
        )

        self.assertTrue(state.last_customer_dissatisfied)
        self.assertEqual(state.move_forward_decision, "No")
        self.assertTrue(state.better_plan_close_due)
        self.assertFalse(state.move_forward_question_due)
        self.assertIn("better plan team ke saath discuss", state.guidance())

    def test_v5_zone_starting_authorizes_only_spoken_top_level_amount(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)

        state.authorize_rate_result(
            {
                "status": "success",
                "response_type": "zone_starting",
                "amount": 35.05,
                "basis": {"additional_rate": 5.35},
            }
        )

        self.assertEqual(state.authorized_rate_amounts, {35.05})
        self.assertEqual(state.primary_rate_amount, 35.05)

    def test_v5_call_1631_invalid_business_type_remains_pending(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Delhi to Bangalore rate", turn_id="intent")
        state.seed_context({"business_name": "Harsh Enterprises"})

        apply(
            state,
            "ato-z",
            decision("business_type", "ato-z", "ato-z"),
            turn_id="invalid-type",
        )

        self.assertFalse(state.is_handled("business_type"))
        self.assertEqual(state.pending_field(), "business_type")
        self.assertIn("B2C or D2C", state.guidance())

    def test_v5_call_1631_contradictory_provider_requires_clarification(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"business_name": "Harsh Enterprises", "business_type": "B2C"})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="intent")

        state.apply_deterministic_answers(
            "Main abhi toh kuch use nahin kar raha hoon, but Shiprocket use karta hoon.",
            turn_id="contradiction",
            previous_agent_text="Abhi aap kaun sa courier ya shipping aggregator use karte hain?",
        )

        self.assertTrue(state.provider_clarification_due)
        self.assertFalse(state.is_handled("current_shipping_arrangement"))
        self.assertFalse(state.is_handled("current_provider_name"))
        self.assertIn("Shiprocket use kar rahe hain", state.guidance())

        state.apply_deterministic_answers(
            "Haan, Shiprocket use karta hoon",
            turn_id="provider-confirmed",
            previous_agent_text=(
                "Aap abhi Shiprocket use kar rahe hain, ya filhaal koi shipping provider use nahi kar rahe?"
            ),
        )
        self.assertFalse(state.provider_clarification_due)
        self.assertEqual(state.value("current_provider_name"), "Shiprocket")

    def test_v5_call_1631_monthly_quantity_never_overwrites_problem(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"current_problem": "order mein thodi dikkat"})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C rate batao", turn_id="rate")
        state.mark_pricing_verified("get_shipkia_starting_rate")
        state.apply_deterministic_answers(
            "1,000 shipments",
            turn_id="quantity",
            previous_agent_text="Aapki monthly shipments kitni hoti hain?",
        )
        transitions = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    decision("current_problem", "1000 shipments", "1,000 shipments")
                ],
            },
            customer_text="1,000 shipments",
            turn_id="quantity",
            pending_field_at_turn_start="",
        )

        self.assertEqual(transitions, [])
        self.assertEqual(state.value("monthly_shipments"), 1000)
        self.assertEqual(state.value("current_problem"), "order mein thodi dikkat")

    def test_v5_call_1631_how_much_repeats_rate_before_quantity(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C rate batao", turn_id="rate")
        state.authorize_rate_result({"status": "success", "amount": 31.15})
        state.mark_pricing_verified("lookup_pincode_serviceability")

        state.apply_deterministic_answers("How much?", turn_id="repeat-rate")

        self.assertTrue(state.last_rate_repeat_requested)
        self.assertIn("Rs 31.15", state.guidance())
        self.assertIn("monthly shipment quantity", state.guidance())

    def test_v5_move_forward_no_gets_better_plan_close(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 1000})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C rate batao", turn_id="rate")
        state.mark_pricing_verified("get_shipkia_starting_rate")
        state.apply_deterministic_answers(
            "nahi",
            turn_id="anything-else-no",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
        )

        state.apply_deterministic_answers(
            "नहीं, थैंक यू।",
            turn_id="move-forward-no",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
        )

        self.assertEqual(state.move_forward_decision, "No")
        self.assertTrue(state.better_plan_close_due)
        self.assertFalse(state.onboarding_link_due)
        self.assertIn("better plan team ke saath discuss", state.guidance())
        self.assertIn("Thank you for calling ShipKia", state.guidance())

    def test_v5_b2c_turn_cannot_refuse_newly_opened_arrangement_field(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="intent")
        state.apply_deterministic_answers(
            "Harsh Enterprises",
            turn_id="business",
            previous_agent_text="Aapke business ya brand ka name kya hai?",
        )
        self.assertEqual(state.pending_field(), "business_type")

        state.apply_deterministic_answers("B2C", turn_id="business-type")
        transitions = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    decision(
                        "current_shipping_arrangement",
                        None,
                        "B2C",
                        disposition="unknown",
                        confidence=0.90,
                    )
                ],
            },
            customer_text="B2C",
            turn_id="business-type",
            pending_field_at_turn_start="business_type",
        )

        self.assertEqual(transitions, [])
        self.assertEqual(state.value("business_type"), "B2C")
        self.assertFalse(state.is_handled("current_shipping_arrangement"))
        self.assertEqual(state.optional_ended_by, "")
        self.assertEqual(state.pending_field(), "current_shipping_arrangement")

    def test_v5_shipping_rocket_and_bare_current_rate_are_captured(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"business_name": "Harsh Enterprises", "business_type": "B2C"})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="intent")

        state.apply_deterministic_answers(
            "mein shipping ko use kara shipping rocket",
            turn_id="provider",
            previous_agent_text="Aap kaun sa courier ya shipping provider use kar rahe hain?",
        )
        self.assertEqual(state.value("current_shipping_arrangement"), "Shipping Aggregator")
        self.assertEqual(state.value("current_provider_name"), "Shiprocket")
        self.assertEqual(state.pending_field(), "current_shipping_rate")

        state.apply_deterministic_answers(
            "Rs35",
            turn_id="current-rate",
            previous_agent_text="Shiprocket mein aapko abhi kya rate mil raha hai?",
        )
        self.assertEqual(state.value("current_shipping_rate"), 35.0)
        self.assertEqual(state.pending_field(), "current_problem")

    def test_v5_stray_decimal_does_not_become_rate_before_rate_question(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")

        transitions = state.apply_deterministic_answers(
            "1.7",
            turn_id="asr-noise",
            previous_agent_text=(
                "ShipKia multiple courier partners ke saath shipments manage karta hai. "
                "Kya aap rates ke baare mein jaanna chahenge?"
            ),
        )
        semantic = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [decision("current_shipping_rate", 1.7, "1.7")],
            },
            customer_text="1.7",
            turn_id="asr-noise",
            pending_field_at_turn_start="assistance_intent",
        )

        self.assertEqual(transitions, [])
        self.assertEqual(semantic, [])
        self.assertFalse(state.is_handled("current_shipping_rate"))
        self.assertEqual(state.pending_field(), "assistance_intent")

    def test_same_turn_classifier_does_not_rewrite_deterministic_problem(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context(
            {
                "business_name": "Harsh Enterprises",
                "business_type": "D2C",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_shipping_rate": 35,
            }
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates batao", turn_id="intent")
        customer_text = "Koi problem nahi, bas services mein dikkat hai."

        deterministic = state.apply_deterministic_answers(
            customer_text,
            turn_id="problem-turn",
            previous_agent_text="Shiprocket ke saath kya problem ya dikkat aa rahi hai?",
        )
        semantic = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    decision("current_problem", "Services mein dikkat", "services mein dikkat")
                ],
            },
            customer_text=customer_text,
            turn_id="problem-turn",
            pending_field_at_turn_start="current_problem",
        )

        self.assertEqual(len(deterministic), 1)
        self.assertEqual(semantic, [])
        self.assertEqual(state.value("current_problem"), customer_text.rstrip("."))
        self.assertEqual(
            len(
                [
                    item
                    for item in state.transitions
                    if item.get("turn_id") == "problem-turn"
                    and item.get("field") == "current_problem"
                ]
            ),
            1,
        )

    def test_multiple_explicit_same_turn_problems_are_preserved(self):
        state = GatedConversationState()
        apply(
            state,
            "Harsh Enterprises B2C Shiprocket Rs 35",
            decision("business_name", "Harsh Enterprises", "Harsh Enterprises"),
            decision("business_type", "B2C", "B2C"),
            decision("current_shipping_arrangement", "Shipping Aggregator", "Shiprocket"),
            decision("current_provider_name", "Shiprocket", "Shiprocket"),
            decision("current_shipping_rate", 35, "35"),
            turn_id="qualification",
        )

        apply(
            state,
            "mere workflow mein problem hai aur RTO bahut ho raha hai",
            decision("current_problem", "RTO bahut ho raha hai", "RTO bahut ho raha hai"),
            decision("current_problem", "workflow mein problem hai", "workflow mein problem hai"),
            turn_id="problem",
        )

        problem = str(state.value("current_problem"))
        self.assertIn("RTO", problem)
        self.assertIn("workflow", problem)
        self.assertTrue(state.is_handled("current_problem"))

    def test_partial_route_is_not_ready_for_lookup(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context(
            {
                "business_name": "Harsh Enterprises",
                "business_type": "B2C",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_shipping_rate": 35,
                "current_problem": "RTO issue",
            }
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="intent")
        state.apply_deterministic_answers(
            "110001",
            turn_id="pickup",
            previous_agent_text="Pickup pincode bataiye",
        )

        self.assertIsNone(state.next_route_for_lookup())
        self.assertFalse(state.route_ready_for_lookup())
        self.assertEqual(state.pending_field(), "delivery_pincode")
        self.assertIn("drop city/location", state.guidance())

    def test_v5_features_side_question_does_not_lock_rates_intent(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("haan ji bataiye", turn_id="consent")
        transitions = state.apply_deterministic_answers(
            "Ek minute pehle ShipKia ke features bata do",
            turn_id="features",
            previous_agent_text="Aap shipping rates check karna chahenge ya onboarding mein help chahiye?",
        )

        self.assertEqual(transitions, [])
        self.assertFalse(state.is_handled("assistance_intent"))
        self.assertEqual(state.pending_field(), "assistance_intent")

    def test_v5_supports_three_explicit_customer_rate_types(self):
        cases = {
            "zonal rates batao": ("Zonal", "business_name"),
            "flat zonal rates batao": ("Flat Zonal", ""),
            "flat rates batao": ("Flat", ""),
        }
        for text, (expected_type, expected_pending) in cases.items():
            with self.subTest(text=text):
                state = GatedConversationState(
                    v4_strict_flow=True,
                    v5_company_pair_flow=True,
                )
                state.apply_deterministic_answers("ji bataiye", turn_id="consent")
                state.apply_deterministic_answers(text, turn_id="rate-type")
                self.assertEqual(state.requested_rate_type, expected_type)
                self.assertEqual(state.pending_field(), expected_pending)

    def test_v5_switching_rate_types_preserves_memory_without_sticking_to_old_catalog(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("flat rates batao", turn_id="flat")
        state.mark_flat_catalog_presented()
        self.assertEqual(state.pricing_mode(), "flat_catalog_presented")

        state.apply_deterministic_answers("ab zonal rates check karo", turn_id="zonal")

        self.assertEqual(state.requested_rate_type, "Zonal")
        self.assertEqual(state.pending_field(), "business_name")
        self.assertEqual(state.pricing_mode(), "pending")

    def test_v5_devanagari_pan_india_is_retained(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates chahiye", turn_id="rates")
        state.apply_deterministic_answers("मुझे पान इंडिया के लिए चाहिए", turn_id="pan-india")

        self.assertTrue(state.pan_india_requested)
        self.assertEqual(state.next_route_for_lookup(), {"pan_india": True})

    def test_v5_move_forward_yes_gets_one_onboarding_link_close(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 1000})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("flat rates batao", turn_id="flat")
        state.mark_flat_catalog_presented()
        state.mark_pricing_verified("get_shipkia_flat_rates", payment_basis="Prepaid")

        self.assertIn("kuch aur jaanna", state.guidance())
        state.apply_deterministic_answers(
            "nahi",
            turn_id="anything-else-no",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
        )
        self.assertIn("ShipKia ke saath aage badhna", state.guidance())
        state.apply_deterministic_answers(
            "haan ji",
            turn_id="move-forward-yes",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
        )

        self.assertTrue(state.customer_satisfied)
        self.assertEqual(state.move_forward_decision, "Yes")
        self.assertTrue(state.onboarding_link_due)
        self.assertIn("onboarding ka link bhej raha", state.guidance())
        self.assertIn("onboarding complete kar lijiye", state.guidance())
        self.assertIn("Do not", state.guidance())
        self.assertIn("speak the URL aloud", state.guidance())
        state.mark_onboarding_link_presented()
        self.assertFalse(state.onboarding_link_due)
        self.assertTrue(state.onboarding_link_presented)

    def test_v5_contextual_okay_theek_hai_accepts_move_forward(self):
        for answer in ("okay", "okay okay", "theek hai", "ठीक है", "Okay Okay, ठीक है"):
            with self.subTest(answer=answer):
                state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
                state.seed_context({"monthly_shipments": 1000})
                state.apply_deterministic_answers("ji bataiye", turn_id="consent")
                state.apply_deterministic_answers("flat rates batao", turn_id="flat")
                state.mark_flat_catalog_presented()
                state.mark_pricing_verified("get_shipkia_flat_rates", payment_basis="Prepaid")
                state.apply_deterministic_answers(
                    "nahi",
                    turn_id="anything-else-no",
                    previous_agent_text="Kya aap kuch aur jaanna chahenge?",
                )

                state.apply_deterministic_answers(
                    answer,
                    turn_id=f"move-forward-{answer}",
                    previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
                )

                self.assertEqual(state.move_forward_decision, "Yes")
                self.assertTrue(state.onboarding_link_due)
                self.assertIn("WhatsApp par onboarding ka link", state.guidance())

    def test_v5_theek_hai_outside_move_forward_context_is_not_consent(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="intent")

        state.apply_deterministic_answers(
            "ठीक है",
            turn_id="ordinary-acknowledgement",
            previous_agent_text="Aapke business ka naam kya hai?",
        )

        self.assertEqual(state.move_forward_decision, "")
        self.assertFalse(state.onboarding_link_due)

    def test_v5_unclear_audio_never_authorizes_onboarding_close(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("flat rates batao", turn_id="flat")
        state.mark_flat_catalog_presented()
        state.mark_pricing_verified("get_shipkia_flat_rates", payment_basis="Prepaid")

        state.apply_deterministic_answers("tene", turn_id="unclear")

        self.assertFalse(state.customer_satisfied)
        self.assertFalse(state.onboarding_link_due)

        state.apply_deterministic_answers("Sì", turn_id="generic-yes")
        self.assertFalse(state.customer_satisfied)
        self.assertFalse(state.onboarding_link_due)

    def test_v5_replays_call_1602_and_keeps_discovery_before_route_rate(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("की बताइए।", turn_id="consent")
        state.apply_deterministic_answers(
            "Read 6.",
            turn_id="intent",
            previous_agent_text="Aap shipping rates check karna chahenge ya onboarding?",
        )
        state.apply_deterministic_answers("Bangalore to Delhi", turn_id="route")

        self.assertEqual(state.value("conversation_consent"), "Accepted")
        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(state.requested_rate_type, "Normal")
        self.assertEqual(state.pending_field(), "business_name")
        self.assertEqual(state.pricing_mode(), "pending")
        self.assertIn("business ya brand ka naam", state.guidance())
        self.assertEqual(
            state.next_route_for_lookup(),
            {"pickup_location": "Bengaluru", "delivery_location": "Delhi"},
        )

    def test_v5_call_1602_flat_asr_variants_select_correct_catalog(self):
        flat = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        flat.apply_deterministic_answers("ji bataiye", turn_id="consent")
        flat.apply_deterministic_answers("a fly rate", turn_id="flat")
        self.assertEqual(flat.value("assistance_intent"), "Rates")
        self.assertEqual(flat.requested_rate_type, "Flat")
        self.assertTrue(flat.flat_catalog_due())

        flat_zonal = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
        )
        flat_zonal.apply_deterministic_answers("ji bataiye", turn_id="consent")
        flat_zonal.apply_deterministic_answers(
            "नल फ्लैट रेट बता दो",
            turn_id="flat-zonal",
        )
        self.assertEqual(flat_zonal.value("assistance_intent"), "Rates")
        self.assertEqual(flat_zonal.requested_rate_type, "Flat Zonal")
        self.assertTrue(flat_zonal.flat_zonal_catalog_due())

        call_1605 = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
        )
        call_1605.apply_deterministic_answers("ji bataiye", turn_id="consent")
        call_1605.apply_deterministic_answers(
            "aur donal plate rate",
            turn_id="flat-zonal-asr",
        )
        self.assertEqual(call_1605.requested_rate_type, "Flat Zonal")
        self.assertTrue(call_1605.flat_zonal_catalog_due())

    def test_call_1665_flat_zonal_asr_switches_catalog_and_breaks_checkpoint_loop(self):
        for customer_text in (
            "Par flood zone rate available?",
            "Mai puch raha hoon, flat, 2 night rate available hai.",
        ):
            with self.subTest(customer_text=customer_text):
                state = GatedConversationState(
                    v4_strict_flow=True,
                    v5_company_pair_flow=True,
                )
                state.seed_context(
                    {
                        "pickup_location": "Delhi",
                        "delivery_location": "Bengaluru",
                        "monthly_shipments": 5000,
                    }
                )
                state.apply_deterministic_answers("ji bataiye", turn_id="consent")
                state.apply_deterministic_answers("flat rates batao", turn_id="flat")
                state.mark_flat_catalog_presented()
                state.mark_pricing_verified("get_shipkia_flat_rates", payment_basis="Prepaid")
                self.assertTrue(state.anything_else_question_due)

                state.apply_deterministic_answers(customer_text, turn_id="flat-zonal-asr")

                self.assertEqual(state.requested_rate_type, "Flat Zonal")
                self.assertTrue(state.flat_zonal_catalog_due())
                self.assertFalse(state.anything_else_question_due)
                self.assertFalse(state.move_forward_question_due)
                self.assertEqual(state.value("pickup_location"), "Delhi")
                self.assertEqual(state.value("delivery_location"), "Bengaluru")
                guidance = state.guidance()
                self.assertIn("Call get_shipkia_flat_zonal_rates exactly once", guidance)
                self.assertNotIn("location", guidance.casefold())

    def test_v5_call_1609_letter_to_asr_selects_flat_catalog(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates chahiye", turn_id="rates")

        state.apply_deterministic_answers("letter to", turn_id="flat-asr")

        self.assertEqual(state.requested_rate_type, "Flat")
        self.assertTrue(state.flat_catalog_due())
        self.assertEqual(state.pending_field(), "")

    def test_v5_call_1609_both_catalogs_context_does_not_overwrite_payment_or_service(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("E-Kart ke rates bata do", turn_id="ekart")
        self.assertTrue(state.ekart_rate_choice_due)

        state.apply_deterministic_answers(
            "dono ke bata do",
            turn_id="both-catalogs",
            previous_agent_text=(
                "E-Kart Surface ke Flat rates chahiye ya E-Kart Express ke Flat-Zonal rates?"
            ),
        )
        apply(
            state,
            "dono ke bata do",
            decision("payment_type", "Both", "dono"),
            decision("service", "dono", "dono"),
            turn_id="both-catalogs",
        )

        self.assertEqual(state.pending_catalogs, {"Flat", "Flat Zonal"})
        self.assertFalse(state.is_handled("payment_type"))
        self.assertFalse(state.is_handled("service"))
        self.assertTrue(state.flat_catalog_due())
        self.assertTrue(state.flat_zonal_catalog_due())

        state.mark_flat_catalog_presented()
        self.assertFalse(state.flat_catalog_due())
        self.assertTrue(state.flat_zonal_catalog_due())
        self.assertEqual(state.requested_rate_type, "Flat Zonal")
        state.mark_flat_zonal_catalog_presented()
        self.assertFalse(state.flat_zonal_catalog_due())

    def test_v5_call_1617_e_card_express_dono_means_both_zone_groups(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("flat rates batao", turn_id="flat")
        state.mark_flat_catalog_presented()

        state.apply_deterministic_answers(
            "aur Express ke, E-Card Express ke rate dono rate",
            turn_id="express-groups",
            previous_agent_text="E-Kart Surface ke flat rates ye hain.",
        )

        self.assertEqual(state.requested_rate_type, "Flat Zonal")
        self.assertTrue(state.flat_zonal_catalog_due())
        self.assertFalse(state.is_handled("payment_type"))
        self.assertIn("get_shipkia_flat_zonal_rates", state.guidance())

    def test_v5_call_1617_route_zone_question_uses_verified_flat_zonal_group_directly(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context(
            {
                "pickup_location": "Delhi",
                "delivery_location": "Mumbai",
                "zone": "C",
            }
        )
        state.requested_rate_type = "Flat Zonal"
        state.mark_flat_zonal_catalog_presented(
            [
                {"zone_group": "A-B", "total": 84.37},
                {"zone_group": "C-F", "total": 109.03},
            ]
        )

        state.apply_deterministic_answers(
            "Delhi to Mumbai kaun se zone mein aata hai?",
            turn_id="route-group",
        )

        guidance = state.guidance()
        self.assertIn("Zone C", guidance)
        self.assertIn("Zones C-F", guidance)
        self.assertIn("Rs 109.03", guidance)
        self.assertIn("do not ask whether they want the rate", guidance)

    def test_v5_shadowfax_surface_uses_already_verified_route_zone(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"zone": "C"})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")

        state.apply_deterministic_answers(
            "Shadowfax Surface ka rate batao",
            turn_id="shadowfax",
        )

        self.assertTrue(state.shadowfax_surface_rate_due)
        self.assertEqual(state.value("zone"), "C")
        self.assertIn("get_shipkia_starting_rate", state.guidance())
        self.assertIn("Do not ask the customer to identify a zone", state.guidance())

    def test_v5_no_thank_you_after_old_anything_else_does_not_choose_for_customer(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 1000})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("flat rates batao", turn_id="flat")
        state.mark_flat_catalog_presented()
        state.mark_pricing_verified("get_shipkia_flat_rates")

        state.apply_deterministic_answers(
            "nahi thank you",
            turn_id="close",
            previous_agent_text="Kya aap aur kuch jaan-na chahenge?",
        )

        self.assertFalse(state.customer_satisfied)
        self.assertFalse(state.onboarding_link_due)
        self.assertTrue(state.move_forward_question_due)
        self.assertIn("ShipKia ke saath aage badhna", state.guidance())

    def test_v5_usp_answer_is_complete_and_resumes_unchanged_pending_flow(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")

        state.apply_deterministic_answers(
            "ShipKia ke fayde batao",
            turn_id="usp",
            previous_agent_text="Rates check karenge ya onboarding help chahiye?",
        )

        guidance = state.guidance()
        self.assertIn("dedicated account manager", guidance)
        self.assertIn("WhatsApp order confirmation", guidance)
        self.assertIn("IVR-call", guidance)
        self.assertNotIn("kuch aur jaanna chahenge", guidance)
        self.assertIn("shipping rates check karna chahenge ya onboarding", guidance)
        self.assertEqual(state.pending_field(), "assistance_intent")

    def test_v5_problem_solution_continues_directly_to_retained_route_rate(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context(
            {
                "business_name": "Harsh Enterprises",
                "business_type": "B2C",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_shipping_rate": 32,
                "pickup_location": "Delhi",
                "delivery_location": "Mumbai",
            }
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates chahiye", turn_id="rates")
        apply(
            state,
            "returns meri main problem hai",
            decision("current_problem", "returns", "returns"),
            turn_id="problem",
        )

        guidance = state.guidance()
        self.assertIn("return/RTO problem", guidance)
        self.assertIn("WhatsApp/IVR NDR", guidance)
        self.assertIn("lookup_pincode_serviceability", guidance)
        self.assertIn("Never ask permission", guidance)
        self.assertEqual(
            state.next_route_for_lookup(),
            {"pickup_location": "Delhi", "delivery_location": "Mumbai"},
        )

    def test_v5_call_1631_order_problem_gets_order_confirmation_solution(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context(
            {
                "business_name": "Harsh Enterprises",
                "business_type": "B2C",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_shipping_rate": 37,
                "pickup_location": "Delhi",
                "delivery_location": "Bengaluru",
            }
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates chahiye", turn_id="rates")
        apply(
            state,
            "order mein thodi dikkat hai",
            decision("current_problem", "order mein thodi dikkat", "order mein thodi dikkat"),
            turn_id="problem",
        )

        guidance = state.guidance()
        self.assertIn("WhatsApp order confirmation", guidance)
        self.assertIn("call confirmation", guidance)
        self.assertIn("dedicated account-manager", guidance)
        self.assertIn("lookup_pincode_serviceability", guidance)

    def test_v5_devanagari_city_routes_are_queued_without_pincodes(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers(
            "दिल्ली टू बेंगलुरु या बेंगलुरु टू दिल्ली का rate batao",
            turn_id="routes",
        )

        self.assertEqual(
            state.requested_routes,
            [
                {"pickup_location": "Delhi", "delivery_location": "Bengaluru"},
                {"pickup_location": "Bengaluru", "delivery_location": "Delhi"},
            ],
        )
        self.assertTrue(state.route_ready_for_lookup())

    def test_v5_monthly_quantity_is_captured_and_not_asked_again(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C rate batao", turn_id="rate")
        state.mark_pricing_verified("get_shipkia_starting_rate")

        state.apply_deterministic_answers(
            "1000",
            turn_id="quantity",
            previous_agent_text="Aapki monthly shipment quantity kitni hoti hai?",
        )

        self.assertEqual(state.value("monthly_shipments"), 1000)
        self.assertTrue(state.is_handled("monthly_shipments"))
        self.assertTrue(state.last_monthly_quantity_captured)
        self.assertIn("acknowledge", state.guidance())

    def test_v5_monthly_quantity_due_survives_missing_agent_transcript_context(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C rate batao", turn_id="rate")
        state.mark_pricing_verified("get_shipkia_starting_rate")

        self.assertTrue(state.monthly_quantity_due)
        state.apply_deterministic_answers("1,000", turn_id="quantity")

        self.assertEqual(state.value("monthly_shipments"), 1000)
        self.assertFalse(state.monthly_quantity_due)

    def test_v5_call_1605_quantity_then_requires_move_forward_decision(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C rate batao", turn_id="rate")
        state.mark_pricing_verified("get_shipkia_starting_rate")
        state.apply_deterministic_answers(
            "main matlab 6 month 1,000",
            turn_id="quantity",
        )
        self.assertEqual(state.value("monthly_shipments"), 1000)

        state.apply_deterministic_answers(
            "theek hai samajh gaya, mujhe idea mil gaya, thank you",
            turn_id="satisfied",
        )
        self.assertFalse(state.onboarding_link_due)
        self.assertTrue(state.anything_else_question_due)
        self.assertFalse(state.move_forward_question_due)
        state.apply_deterministic_answers(
            "nahi",
            turn_id="anything-else-no",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
        )
        self.assertTrue(state.move_forward_question_due)
        state.apply_deterministic_answers(
            "yes",
            turn_id="yes",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
        )
        self.assertTrue(state.onboarding_link_due)
        self.assertIn("onboarding complete kar lijiye", state.guidance())

    def test_v5_ambiguous_ekart_request_requires_structure_choice(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("E-Kart ke rates bata do", turn_id="ekart")

        self.assertTrue(state.ekart_rate_choice_due)
        self.assertIn("E-Kart Surface", state.guidance())

        state.apply_deterministic_answers("Express wala", turn_id="express")
        self.assertFalse(state.ekart_rate_choice_due)
        self.assertEqual(state.requested_rate_type, "Flat Zonal")
        self.assertTrue(state.flat_zonal_catalog_due())

    def test_v5_dissatisfied_customer_reuses_route_and_never_closes(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Delhi to Mumbai rate batao", turn_id="route")
        route = state.next_route_for_lookup()
        state.mark_route_zone_verified("C", starting_presented=True, route_arguments=route)
        state.mark_pricing_verified("lookup_pincode_serviceability")

        state.apply_deterministic_answers(
            "बट मैं सेटिस्फाइड नहीं हूं, रेट ज्यादा है",
            turn_id="complaint",
        )

        self.assertTrue(state.last_customer_dissatisfied)
        self.assertFalse(state.onboarding_link_due)
        self.assertFalse(state.better_plan_close_due)
        self.assertTrue(state.unsatisfied_problem_due)
        self.assertIn("what exact problem", state.guidance())
        self.assertNotIn("onboarding link bhej", state.guidance())

    def test_v5_unsatisfied_close_references_the_customers_actual_problem(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"current_problem": "RTO problem"})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C rate batao", turn_id="rate")
        state.mark_pricing_verified("get_shipkia_starting_rate")

        state.apply_deterministic_answers(
            "main satisfied nahi hoon",
            turn_id="not-satisfied",
        )

        self.assertTrue(state.last_customer_dissatisfied)
        self.assertFalse(state.onboarding_link_due)
        self.assertFalse(state.better_plan_close_due)
        self.assertFalse(state.unsatisfied_problem_due)
        self.assertTrue(state.unsatisfied_resolution_due)
        self.assertEqual(state.unsatisfied_concern, "RTO problem")
        self.assertIn("solution ya better plan", state.guidance())

    def test_v4_replays_call_1443_without_skips_or_pincode_overwrite(self):
        state = GatedConversationState(v4_strict_flow=True)

        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers(
            "main check karna chaunga",
            turn_id="intent",
            previous_agent_text="Aap rates check karna chahenge ya onboarding help?",
        )
        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(state.pending_field(), "business_name")

        apply(
            state,
            "Mera business Alpha Store hai",
            decision("business_name", "Alpha Store", "Alpha Store"),
            turn_id="business-name",
        )
        apply(
            state,
            "D2C",
            decision("business_type", "D2C", "D2C"),
            turn_id="business-type",
        )
        state.apply_deterministic_answers(
            "main koi courier use nahi karta",
            turn_id="arrangement",
            previous_agent_text="Abhi aap kaunsa courier ya aggregator use karte hain?",
        )
        self.assertEqual(state.pending_field(), "pickup_pincode")
        self.assertFalse(state.is_handled("current_provider_name"))
        self.assertFalse(state.is_handled("current_problem"))

        state.apply_deterministic_answers(
            "110001",
            turn_id="delivery-pin",
            previous_agent_text="Aapka delivery pincode kya hai?",
        )
        self.assertEqual(state.value("delivery_pincode"), "110001")
        state.apply_deterministic_answers(
            "201305",
            turn_id="pickup-pin",
            previous_agent_text=(
                "Delivery pincode mil gaya. Ab pickup pincode bata sakte hain?"
            ),
        )
        self.assertEqual(state.value("pickup_pincode"), "201305")
        self.assertEqual(state.value("delivery_pincode"), "110001")
        self.assertEqual(state.pending_field(), "dead_weight")

        state.apply_deterministic_answers("0.5 kg", turn_id="weight")
        self.assertEqual(state.pending_field(), "payment_type")
        state.apply_deterministic_answers("dono", turn_id="payment")
        self.assertEqual(state.value("payment_type"), "Both")
        self.assertEqual(state.pricing_mode(), "exact")
        self.assertEqual(state.pending_field(), "")

    def test_v4_customer_question_guidance_never_exposes_internal_context(self):
        state = GatedConversationState(v4_strict_flow=True)
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("rate check karna hai", turn_id="intent")

        guidance = state.guidance()

        self.assertIn("business or brand name", guidance)
        for internal_phrase in (
            "worker-controlled",
            "pending_field",
            "pricing_ready",
            "authoritative gated state",
            "set to Normal",
        ):
            self.assertNotIn(internal_phrase.casefold(), guidance.casefold())

    def test_v4_unlabelled_pincode_does_not_overwrite_confirmed_endpoint(self):
        state = GatedConversationState(v4_strict_flow=True)
        for field, value in (
            ("conversation_consent", "Accepted"),
            ("assistance_intent", "Rates"),
            ("business_name", "Alpha"),
            ("business_type", "D2C"),
        ):
            apply(state, str(value), decision(field, value, str(value)), turn_id=field)
        apply(
            state,
            "No courier",
            decision(
                "current_shipping_arrangement",
                None,
                "No courier",
                disposition="not_applicable",
            ),
            turn_id="arrangement",
        )
        apply(state, "201305", decision("pickup_pincode", "201305", "201305"), turn_id="pickup")
        apply(state, "110001", decision("delivery_pincode", "110001", "110001"), turn_id="delivery")

        state.apply_deterministic_answers(
            "400001",
            turn_id="noise-pin",
            previous_agent_text="Shipment ka weight kitna hai?",
        )
        self.assertEqual(state.value("pickup_pincode"), "201305")
        self.assertEqual(state.value("delivery_pincode"), "110001")
        self.assertEqual(state.pending_field(), "dead_weight")

    def test_v4_extended_hindi_hinglish_consent_variants(self):
        for spoken in (
            "हां, जी, बताइए।",
            "हाँ बोलिए",
            "जी बताइए।",
            "Achchi bataiye.",
            "haan ji bataiye",
            "yes sure",
        ):
            with self.subTest(spoken=spoken):
                state = GatedConversationState(v4_strict_flow=True)
                transitions = state.apply_deterministic_answers(
                    spoken,
                    turn_id=f"consent-{spoken}",
                )

                self.assertEqual(state.value("conversation_consent"), "Accepted")
                self.assertEqual(state.pending_field(), "assistance_intent")
                self.assertEqual(len(transitions), 1)

    def test_v4_consent_then_intent_precede_every_requirement(self):
        state = GatedConversationState(v4_strict_flow=True)

        self.assertEqual(state.pending_field(), "conversation_consent")
        state.apply_deterministic_answers("rates check karne hain", turn_id="too-early")
        self.assertEqual(state.pending_field(), "conversation_consent")
        self.assertEqual(state.requested_rate_type, "")

        state.apply_deterministic_answers("haan ji", turn_id="consent")
        self.assertEqual(state.value("conversation_consent"), "Accepted")
        self.assertEqual(state.pending_field(), "assistance_intent")

        state.apply_deterministic_answers("normal rates check karne hain", turn_id="intent")
        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(state.requested_rate_type, "Normal")
        self.assertEqual(state.pending_field(), "business_name")

    def test_v4_hindi_transcript_consent_and_flat_intent_are_recognized(self):
        state = GatedConversationState(v4_strict_flow=True)
        state.apply_deterministic_answers("जी", turn_id="consent")
        state.apply_deterministic_answers(
            "जी, मैं फ्लाइट रेट चेक करना चाहूंगा।",
            turn_id="intent",
        )

        self.assertEqual(state.value("conversation_consent"), "Accepted")
        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(state.requested_rate_type, "Flat")
        self.assertEqual(state.pending_field(), "business_name")

    def test_v4_explicit_flat_can_switch_back_to_normal(self):
        state = GatedConversationState(v4_strict_flow=True)
        state.apply_deterministic_answers("yes", turn_id="consent")
        state.apply_deterministic_answers("flat rate check karna hai", turn_id="flat")

        self.assertEqual(state.requested_rate_type, "Flat")

        state.apply_deterministic_answers("ab normal rates bataiye", turn_id="normal")

        self.assertEqual(state.requested_rate_type, "Normal")

    def test_explicit_answers_are_remembered_even_when_state_is_one_question_behind(self):
        state = GatedConversationState(v4_strict_flow=True)
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers(
            "rates check karne hain", turn_id="intent"
        )

        state.apply_deterministic_answers("D2C", turn_id="business-type")
        state.apply_deterministic_answers(
            "main Shirocket use kar raha hoon",
            turn_id="provider",
        )
        state.apply_deterministic_answers(
            "pickup 201305",
            turn_id="pickup",
            previous_agent_text="Noida ka pincode kya hai?",
        )
        state.apply_deterministic_answers(
            "\u092a\u094d\u0930\u0940\u092a\u0947\u0921 \u0915\u093e \u092c\u0924\u093e \u0926\u0940\u091c\u093f\u090f",
            turn_id="payment",
        )

        self.assertEqual(state.value("business_type"), "D2C")
        self.assertEqual(state.value("current_shipping_arrangement"), "Shipping Aggregator")
        self.assertEqual(state.value("current_provider_name"), "Shiprocket")
        self.assertEqual(state.value("pickup_pincode"), "201305")
        self.assertEqual(state.value("payment_type"), "Prepaid")

    def test_v4_pan_india_normal_rate_still_requires_route_weight_and_payment(self):
        for spoken in ("Pan India na?", "Noida se all over India"):
            with self.subTest(spoken=spoken):
                state = GatedConversationState(v4_strict_flow=True)
                state.seed_context(
                    {
                        "business_name": "Work Shop",
                        "business_type": "D2C",
                        "current_shipping_arrangement": "Own Arrangement",
                        "current_shipping_rate": 40,
                        "current_problem": "High Rates",
                    }
                )
                state.apply_deterministic_answers("haan ji", turn_id="consent")
                state.apply_deterministic_answers(
                    "normal rates check karne hain",
                    turn_id="intent",
                )

                state.apply_deterministic_answers(spoken, turn_id="pan-india")

                self.assertEqual(state.pricing_mode(), "pending")
                self.assertFalse(state.starting_rate_due())
                self.assertEqual(state.pending_field(), "pickup_pincode")

    def test_v4_flat_rate_skips_route_pincodes_but_requires_weight_and_payment(self):
        state = GatedConversationState(v4_strict_flow=True)
        state.seed_context(
            {
                "business_name": "Work Shop",
                "business_type": "D2C",
                "current_shipping_arrangement": "Own Arrangement",
                "current_shipping_rate": 40,
                "current_problem": "High Rates",
            }
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("flat rate check karna hai", turn_id="intent")

        self.assertEqual(state.pending_field(), "dead_weight")
        self.assertFalse(state.is_handled("pickup_pincode"))
        self.assertFalse(state.is_handled("delivery_pincode"))

        state.apply_deterministic_answers("500 gram", turn_id="weight")
        self.assertEqual(state.pending_field(), "payment_type")

    def test_v4_declined_conversation_ends_without_qualification(self):
        state = GatedConversationState(v4_strict_flow=True)
        state.apply_deterministic_answers("abhi nahi", turn_id="declined")

        self.assertEqual(state.value("conversation_consent"), "Declined")
        self.assertEqual(state.pending_field(), "")
        self.assertEqual(state.pricing_mode(), "conversation_declined")
        self.assertIn("end the conversation", state.guidance())

    def test_realtime_asr_business_type_variants_do_not_leave_a_pending_gap(self):
        for spoken, expected in (("G2C", "G2C"), ("day 2c", "D2C"), ("dee to c", "D2C")):
            with self.subTest(spoken=spoken):
                state = GatedConversationState()
                apply(
                    state,
                    "mere brand name Harsh Enterprises",
                    decision(
                        "business_name",
                        "Harsh Enterprises",
                        "Harsh Enterprises",
                    ),
                )

                transitions = state.apply_deterministic_answers(
                    spoken,
                    turn_id=f"business-type-{spoken}",
                )

                self.assertTrue(
                    any(item.get("field") == "business_type" for item in transitions)
                )
                self.assertEqual(state.value("business_type"), expected)
                self.assertEqual(
                    state.pending_field(),
                    "current_shipping_arrangement",
                )

    def test_unrelated_rate_request_does_not_skip_business_name(self):
        state = GatedConversationState()

        transitions = apply(state, "pehle rate batao")

        self.assertEqual(transitions, [])
        self.assertEqual(state.pending_field(), "business_name")
        self.assertFalse(state.pricing_ready())
        self.assertIn("same pending question", state.guidance())

    def test_pata_nahi_ends_optional_qualification(self):
        state = GatedConversationState()

        apply(
            state,
            "business name pata nahi",
            decision("business_name", None, "pata nahi", disposition="unknown"),
        )

        self.assertEqual(state.optional_ended_by, "business_name")
        self.assertEqual(state.pending_field(), "pickup_pincode")
        self.assertEqual(state.rate_arguments()["qualification_refused_field"], "business_name")

    def test_v5_company_refusal_skips_pair_but_continues_provider_discovery(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers(
            "rates check karna hai",
            turn_id="intent",
            previous_agent_text="Aap rates check karna chahenge ya onboarding help?",
        )

        apply(
            state,
            "company name share nahi karna",
            decision(
                "business_name",
                None,
                "share nahi karna",
                disposition="refused",
            ),
            turn_id="company-refusal",
        )

        self.assertEqual(state.company_details_ended_by, "business_name")
        self.assertEqual(state.optional_ended_by, "")
        self.assertEqual(state.pending_field(), "current_shipping_arrangement")
        self.assertFalse(state.is_handled("business_type"))

    def test_v5_company_not_applicable_skips_pair_without_inventing_arrangement(self):
        state = GatedConversationState(v5_company_pair_flow=True)

        apply(
            state,
            "company details not applicable",
            decision(
                "business_name",
                None,
                "not applicable",
                disposition="not_applicable",
            ),
        )

        self.assertEqual(state.value("business_name"), "Not Applicable")
        self.assertEqual(state.company_details_ended_by, "business_name")
        self.assertEqual(state.pending_field(), "current_shipping_arrangement")
        self.assertEqual(state.value("current_shipping_arrangement"), "")

    def test_v5_unresolved_route_ends_exact_flow_after_fallback(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
        )
        state.seed_context(
            {
                "business_name": "North Star",
                "business_type": "D2C",
                "current_shipping_arrangement": "Own Arrangement",
                "current_shipping_rate": 40,
                "current_problem": "High Rates",
                "pickup_pincode": "201305",
                "delivery_pincode": "110001",
            }
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates check karna hai", turn_id="intent")

        self.assertEqual(state.pending_field(), "")
        self.assertEqual(state.pricing_mode(), "route_starting_pending")
        state.mark_route_zone_lookup_unavailable(fallback_presented=True)

        self.assertEqual(state.route_zone_lookup_status, "unavailable")
        self.assertEqual(state.pending_field(), "")
        self.assertEqual(state.pricing_mode(), "general_starting")
        self.assertFalse(state.starting_rate_due())

        state.apply_deterministic_answers(
            "delivery pincode change 400001",
            turn_id="route-correction",
        )
        self.assertEqual(state.route_zone_lookup_status, "")
        self.assertEqual(state.value("delivery_pincode"), "400001")
        self.assertEqual(state.pending_field(), "")
        self.assertEqual(state.pricing_mode(), "route_starting_pending")

    def test_v5_city_route_is_complete_and_waits_for_zone_starting_lookup(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context(
            {
                "business_name": "North Star",
                "business_type": "D2C",
                "current_shipping_arrangement": "Own Arrangement",
                "current_shipping_rate": 40,
                "current_problem": "High Rates",
            }
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates check karna hai", turn_id="intent")
        state.apply_deterministic_answers("Delhi se Noida", turn_id="route")

        self.assertEqual(state.value("pickup_location"), "Delhi")
        self.assertEqual(state.value("delivery_location"), "Noida")
        self.assertTrue(state.route_ready_for_lookup())
        self.assertEqual(state.pricing_mode(), "route_starting_pending")
        self.assertIn("lookup_pincode_serviceability", state.guidance())

        self.assertTrue(state.mark_route_zone_verified("A", starting_presented=True))
        self.assertEqual(state.pricing_mode(), "zone_starting")
        self.assertFalse(state.starting_rate_due())

    def test_v5_pan_india_uses_route_lookup_without_pincodes(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context(
            {
                "business_name": "North Star",
                "business_type": "D2C",
                "current_shipping_arrangement": "Own Arrangement",
                "current_shipping_rate": 40,
                "current_problem": "High Rates",
            }
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Pan India rates chahiye", turn_id="intent")

        self.assertTrue(state.pan_india_requested)
        self.assertTrue(state.route_ready_for_lookup())
        self.assertEqual(state.pricing_mode(), "route_starting_pending")

    def test_v5_replays_call_1581_and_prioritizes_garbled_pan_india_rate(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("Achchi bataiye.", turn_id="consent")
        state.apply_deterministic_answers(
            "shipping rate",
            turn_id="intent",
            previous_agent_text="Aap shipping rates check karna chahenge ya onboarding?",
        )
        apply(
            state,
            "Ha Ji, Harsh Enterprises.",
            decision("business_name", "Harsh Enterprises", "Harsh Enterprises"),
            turn_id="business",
        )
        state.apply_deterministic_answers("B2C", turn_id="business-type")
        state.apply_deterministic_answers(
            "Abhi mein use kar raha hun. shift kiya",
            turn_id="provider",
            previous_agent_text="Abhi shipments ke liye kaun sa courier use karte hain aap?",
        )
        apply(
            state,
            "mujhe rate mil raha hai 25 ka",
            decision("current_shipping_rate", 25, "25"),
            turn_id="current-rate",
        )
        apply(
            state,
            "meri problem support ki hai",
            decision("current_problem", "support", "support"),
            turn_id="problem",
        )
        state.apply_deterministic_answers("Par India ki.", turn_id="pan-india")

        self.assertEqual(state.value("conversation_consent"), "Accepted")
        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(state.value("current_shipping_arrangement"), "Other")
        self.assertEqual(state.value("current_provider_name"), "shift kiya")
        self.assertTrue(state.pan_india_requested)
        self.assertEqual(state.pending_field(), "")
        self.assertEqual(state.pricing_mode(), "route_starting_pending")
        self.assertEqual(state.next_route_for_lookup(), {"pan_india": True})
        self.assertIn("lookup_pincode_serviceability", state.guidance())

    def test_v5_call_1582_retains_route_but_requires_shiprocket_problem_first(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"business_name": "H Enterprises", "business_type": "B2C"})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rate", turn_id="intent")
        state.apply_deterministic_answers(
            "Shiprocket",
            turn_id="provider",
            previous_agent_text="Aap abhi kaun sa courier use karte hain?",
        )
        apply(
            state,
            "Shiprocket",
            decision("current_shipping_arrangement", "Other", "Shiprocket"),
            decision("current_provider_name", "Shiprocket", "Shiprocket"),
            turn_id="provider",
        )
        apply(
            state,
            "Shiprocket payment Rs 25",
            decision("current_shipping_rate", 25, "Rs 25"),
            turn_id="current-rate",
        )
        state.apply_deterministic_answers("Delhi to Noida", turn_id="route")

        self.assertEqual(
            state.value("current_shipping_arrangement"),
            "Shipping Aggregator",
        )
        self.assertEqual(state.value("current_provider_name"), "Shiprocket")
        self.assertTrue(state.route_ready_for_lookup())
        self.assertEqual(state.pending_field(), "current_problem")
        self.assertEqual(state.pricing_mode(), "pending")
        self.assertIn("main current shipping challenge", state.guidance())

        numeric_problem = apply(
            state,
            "1000",
            decision("current_problem", "1000", "1000"),
            turn_id="quantity",
        )
        self.assertEqual(numeric_problem, [])
        self.assertEqual(state.pending_field(), "current_problem")

        apply(
            state,
            "support ki problem hai",
            decision("current_problem", "support", "support"),
            turn_id="problem",
        )
        self.assertEqual(state.pending_field(), "")
        self.assertEqual(state.pricing_mode(), "route_starting_pending")
        self.assertEqual(
            state.next_route_for_lookup(),
            {"pickup_location": "Delhi", "delivery_location": "Noida"},
        )

    def test_v5_explicit_zone_rate_bypasses_qualification(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C ka rate batao", turn_id="zone-request")

        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(state.value("zone"), "C")
        self.assertTrue(state.explicit_zone_requested())
        self.assertEqual(state.pending_field(), "")
        self.assertEqual(state.pricing_mode(), "zone_starting")
        self.assertTrue(state.starting_rate_due())

    def test_v5_explicit_flat_request_bypasses_all_inputs(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Flat rates batao", turn_id="flat-request")

        self.assertEqual(state.requested_rate_type, "Flat")
        self.assertTrue(state.flat_catalog_due())
        self.assertEqual(state.pending_field(), "")
        self.assertEqual(state.pricing_mode(), "flat_catalog")
        self.assertIn("get_shipkia_flat_rates", state.guidance())

        state.mark_flat_catalog_presented()
        self.assertFalse(state.flat_catalog_due())

    def test_v5_flat_request_recognizes_latest_call_asr_variants(self):
        for customer_text in (
            "मुझे प्लेटलेट बता सकते हैं आप?",
            "ratrate bata sakte hain Noida to Delhi ke liye?",
        ):
            with self.subTest(customer_text=customer_text):
                state = GatedConversationState(
                    v4_strict_flow=True,
                    v5_company_pair_flow=True,
                )
                state.apply_deterministic_answers("ji bataiye", turn_id="consent")
                state.apply_deterministic_answers(
                    "shipping rates check karne hain",
                    turn_id="rates",
                )
                state.apply_deterministic_answers(customer_text, turn_id="flat-asr")

                self.assertEqual(state.requested_rate_type, "Flat")
                self.assertTrue(state.flat_catalog_due())

    def test_v5_multiple_city_routes_are_queued_and_resolved_in_order(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context(
            {
                "business_name": "North Star",
                "business_type": "D2C",
                "current_shipping_arrangement": "Own Arrangement",
                "current_shipping_rate": 40,
                "current_problem": "High Rates",
            }
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        text = "Mujhe do rate janna hai, ek Delhi to Bengaluru aur ek Noida to Delhi."
        state.apply_deterministic_answers(text, turn_id="multi-route")

        self.assertEqual(
            state.requested_routes,
            [
                {"pickup_location": "Delhi", "delivery_location": "Bengaluru"},
                {"pickup_location": "Noida", "delivery_location": "Delhi"},
            ],
        )
        self.assertEqual(state.value("pickup_location"), "Delhi")
        self.assertEqual(state.value("delivery_location"), "Bengaluru")

        state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    {
                        "field": "pickup_location",
                        "disposition": "answered",
                        "value": "Noida",
                        "evidence": "Noida",
                        "confidence": 1.0,
                    },
                    {
                        "field": "delivery_location",
                        "disposition": "answered",
                        "value": "Delhi",
                        "evidence": "Delhi",
                        "confidence": 1.0,
                    },
                ],
            },
            customer_text=text,
            turn_id="multi-route",
        )
        self.assertEqual(state.value("pickup_location"), "Delhi")
        self.assertEqual(state.value("delivery_location"), "Bengaluru")

        first = state.next_route_for_lookup()
        self.assertEqual(first["delivery_location"], "Bengaluru")
        state.mark_route_zone_verified("C", starting_presented=True, route_arguments=first)
        self.assertEqual(state.unresolved_route_count(), 1)
        self.assertEqual(state.next_route_for_lookup()["pickup_location"], "Noida")

        second = state.next_route_for_lookup()
        state.mark_route_zone_verified("A", starting_presented=True, route_arguments=second)
        self.assertEqual(state.unresolved_route_count(), 0)
        self.assertEqual(state.value("zone"), "A")

    def test_v5_city_route_replaces_stale_pincode_route(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context(
            {
                "business_name": "North Star",
                "business_type": "D2C",
                "current_shipping_arrangement": "Own Arrangement",
                "current_shipping_rate": 40,
                "current_problem": "High Rates",
                "pickup_pincode": "201305",
                "delivery_pincode": "110001",
            }
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates check karna hai", turn_id="intent")
        state.apply_deterministic_answers(
            "Delhi to Bengaluru",
            turn_id="new-route",
        )

        self.assertFalse(state.is_confirmed("pickup_pincode"))
        self.assertFalse(state.is_confirmed("delivery_pincode"))
        self.assertEqual(
            state.next_route_for_lookup(),
            {"pickup_location": "Delhi", "delivery_location": "Bengaluru"},
        )

    def test_multi_detail_reply_saves_explicit_fields_but_keeps_earlier_gap_pending(self):
        state = GatedConversationState()
        text = "Business North Star hai, D2C hai, pickup 110001 aur delivery 400001."

        apply(
            state,
            text,
            decision("business_name", "North Star", "North Star"),
            decision("business_type", "D2C", "D2C"),
            decision("pickup_pincode", "110001", "110001"),
            decision("delivery_pincode", "400001", "400001"),
        )

        self.assertEqual(state.value("business_name"), "North Star")
        self.assertEqual(state.value("delivery_pincode"), "400001")
        self.assertEqual(state.pending_field(), "current_shipping_arrangement")

    def test_low_confidence_or_missing_evidence_fails_closed(self):
        state = GatedConversationState()
        apply(
            state,
            "pehle rate batao",
            decision("business_name", "Invented Shop", "Invented Shop", confidence=0.5),
            turn_id="low",
        )
        apply(
            state,
            "pehle rate batao",
            decision("business_name", "Invented Shop", "not in the utterance"),
            turn_id="missing",
        )

        self.assertFalse(state.is_handled("business_name"))
        self.assertEqual(state.pending_field(), "business_name")

    def test_confirmed_correction_replaces_old_value(self):
        state = GatedConversationState()
        apply(state, "Name ShipCart hai", decision("business_name", "ShipCart", "ShipCart"), turn_id="one")
        apply(
            state,
            "Correction, name ShipKraft hai",
            decision("business_name", "ShipKraft", "ShipKraft"),
            turn_id="two",
        )

        self.assertEqual(state.value("business_name"), "ShipKraft")
        self.assertEqual(state.transitions[-1]["previous_value"], "ShipCart")

    def test_explicitly_unavailable_pincode_selects_general_starting_mode(self):
        state = GatedConversationState()
        apply(
            state,
            "business ka pata nahi",
            decision("business_name", None, "pata nahi", disposition="unknown"),
        )
        apply(
            state,
            "pickup pincode pata nahi",
            decision("pickup_pincode", None, "pata nahi", disposition="unknown"),
            turn_id="pin-one",
        )
        apply(
            state,
            "delivery 400001",
            decision("delivery_pincode", "400001", "400001"),
            turn_id="pin-two",
        )
        apply(
            state,
            "weight 1 kg",
            decision("dead_weight", 1, "1 kg"),
            turn_id="weight",
        )
        apply(
            state,
            "prepaid",
            decision("payment_type", "Prepaid", "prepaid"),
            turn_id="payment",
        )

        self.assertFalse(state.pricing_ready())
        self.assertEqual(state.pricing_mode(), "general_starting")
        self.assertEqual(state.pricing_trigger_field(), "pickup_pincode")
        self.assertTrue(state.starting_rate_due())
        arguments = state.rate_arguments()
        self.assertNotIn("pickup_pincode", arguments)
        self.assertEqual(arguments["pickup_pincode_status"], "Unavailable")
        self.assertEqual(arguments["dead_weight"], 1.0)

    def test_missing_weight_never_becomes_pricing_ready(self):
        state = GatedConversationState()
        apply(
            state,
            "business pata nahi",
            decision("business_name", None, "pata nahi", disposition="unknown"),
        )
        apply(
            state,
            "pickup 110001 delivery 400001 prepaid",
            decision("pickup_pincode", "110001", "110001"),
            decision("delivery_pincode", "400001", "400001"),
            decision("payment_type", "Prepaid", "prepaid"),
        )

        self.assertEqual(state.pending_field(), "dead_weight")
        self.assertFalse(state.pricing_ready())
        self.assertNotIn("dead_weight", state.rate_arguments())

    def test_payment_and_cod_refusals_produce_only_labelled_fallback_markers(self):
        payment_state = GatedConversationState()
        apply(
            payment_state,
            "business pata nahi",
            decision("business_name", None, "pata nahi", disposition="unknown"),
        )
        apply(
            payment_state,
            "110001 se 400001, 2 kg",
            decision("pickup_pincode", "110001", "110001"),
            decision("delivery_pincode", "400001", "400001"),
            decision("dead_weight", 2, "2 kg"),
        )
        apply(
            payment_state,
            "payment nahi batana",
            decision("payment_type", None, "nahi batana", disposition="refused"),
        )
        self.assertFalse(payment_state.pricing_ready())
        self.assertEqual(payment_state.pricing_mode(), "general_starting")
        self.assertEqual(payment_state.rate_arguments()["payment_type"], "Not Shared")

        cod_state = GatedConversationState()
        apply(
            cod_state,
            "business pata nahi",
            decision("business_name", None, "pata nahi", disposition="unknown"),
        )
        apply(
            cod_state,
            "110001 se 400001, 2 kg COD",
            decision("pickup_pincode", "110001", "110001"),
            decision("delivery_pincode", "400001", "400001"),
            decision("dead_weight", 2, "2 kg"),
            decision("payment_type", "COD", "COD"),
        )
        apply(
            cod_state,
            "order value nahi batana",
            decision("order_value", None, "nahi batana", disposition="refused"),
        )
        self.assertFalse(cod_state.pricing_ready())
        self.assertEqual(cod_state.pricing_mode(), "general_starting")
        self.assertEqual(cod_state.rate_arguments()["order_value_status"], "Not Shared")

    def test_pan_india_rate_request_selects_general_starting_once(self):
        state = GatedConversationState()

        transitions = state.apply_deterministic_answers(
            "Pan India shipping rate batao",
            turn_id="pan-india",
        )

        self.assertTrue(
            any(item.get("event") == "pricing_mode_updated" for item in transitions)
        )
        self.assertEqual(state.pricing_mode(), "general_starting")
        self.assertEqual(state.pricing_trigger_field(), "pan_india")
        self.assertTrue(state.starting_rate_due())
        state.mark_starting_rate_presented()
        self.assertFalse(state.starting_rate_due())
        self.assertEqual(state.pricing_mode(), "general_starting")

        state.apply_deterministic_answers(
            "pickup 110001",
            turn_id="specific-route",
        )
        self.assertEqual(state.pricing_mode(), "pending")

    def test_explicit_zone_has_precedence_and_is_never_inferred_from_pincode(self):
        state = GatedConversationState()
        state.apply_deterministic_answers(
            "pickup 110001 delivery 560001",
            turn_id="route",
        )
        self.assertFalse(state.is_confirmed("zone"))

        state.apply_deterministic_answers(
            "Mera approved zone B hai",
            turn_id="zone",
        )

        self.assertEqual(state.value("zone"), "B")
        self.assertEqual(state.pricing_mode(), "zone_starting")
        self.assertEqual(state.starting_rate_key(), "zone:B")

    def test_v4_hindi_spoken_zone_cannot_bypass_consent(self):
        state = GatedConversationState(v4_strict_flow=True)

        transitions = state.apply_deterministic_answers(
            "\u0905\u091a\u094d\u091b\u093e, \u0914\u0930 \u091c\u093c\u094b\u0928 \u0921\u0940 \u0915\u093e \u0915\u094d\u092f\u093e \u0930\u0947\u091f \u0939\u0948?",
            turn_id="hindi-zone-d",
        )

        self.assertFalse(any(item.get("field") == "zone" for item in transitions))
        self.assertEqual(state.value("zone"), "")
        self.assertEqual(state.pricing_mode(), "pending")
        self.assertFalse(state.starting_rate_due())

    def test_weight_refusal_is_handled_and_uses_general_starting_mode(self):
        state = GatedConversationState()
        state.apply_deterministic_answers("business pata nahi", turn_id="optional")
        state.apply_deterministic_answers(
            "pickup 110001 delivery 560001",
            turn_id="route",
        )

        transitions = state.apply_deterministic_answers(
            "weight nahi pata",
            turn_id="weight",
        )

        self.assertTrue(any(item.get("field") == "dead_weight" for item in transitions))
        self.assertTrue(state.is_handled("dead_weight"))
        self.assertEqual(state.pending_field(), "payment_type")
        self.assertEqual(state.pricing_mode(), "general_starting")

    def test_explicit_skip_phrase_triggers_general_starting_for_required_field(self):
        state = GatedConversationState()
        state.apply_deterministic_answers("business pata nahi", turn_id="optional")

        state.apply_deterministic_answers(
            "pickup pincode skip karo",
            turn_id="pickup",
        )

        self.assertEqual(state.fields["pickup_pincode"].status, "unavailable")
        self.assertEqual(state.pricing_mode(), "general_starting")

    def test_later_optional_refusal_preserves_earlier_business_type_gap(self):
        state = GatedConversationState()
        apply(
            state,
            "Business Book Show hai",
            decision("business_name", "Book Show", "Book Show"),
            turn_id="name",
        )
        apply(
            state,
            "abhi kuch use nahi kar raha",
            decision(
                "current_shipping_arrangement",
                None,
                "abhi kuch use nahi kar raha",
                disposition="not_applicable",
            ),
            turn_id="arrangement",
        )

        self.assertEqual(
            state.value("current_shipping_arrangement"),
            "No Current Arrangement",
        )
        self.assertEqual(
            state.optional_ended_by,
            "current_shipping_arrangement",
        )
        self.assertEqual(state.pending_field(), "business_type")

    def test_guard_error_keeps_pending_field(self):
        state = GatedConversationState()
        state.record_guard_error("timeout", turn_id="timeout-turn")

        self.assertEqual(state.pending_field(), "business_name")
        self.assertFalse(state.pricing_ready())
        self.assertEqual(state.transitions[-1]["event"], "guard_failed")

    def test_structured_shipment_answers_are_validated_without_classifier(self):
        state = GatedConversationState()
        state.apply_deterministic_answers("business pata nahi", turn_id="optional")

        transitions = state.apply_deterministic_answers(
            "pickup 110001 delivery 400001, weight 750 g, prepaid",
            turn_id="shipment",
        )

        self.assertEqual(state.value("pickup_pincode"), "110001")
        self.assertEqual(state.value("delivery_pincode"), "400001")
        self.assertEqual(state.value("dead_weight"), 0.75)
        self.assertEqual(state.value("payment_type"), "Prepaid")
        self.assertTrue(state.pricing_ready())
        self.assertEqual(len([item for item in transitions if item["field"] == "dead_weight"]), 1)

    def test_unlabelled_single_six_digit_number_does_not_bypass_optional_question(self):
        state = GatedConversationState()

        state.apply_deterministic_answers("100000", turn_id="ambiguous")

        self.assertFalse(state.is_handled("pickup_pincode"))
        self.assertEqual(state.pending_field(), "business_name")

    def test_deterministic_unknown_and_classifier_result_do_not_duplicate_transition(self):
        state = GatedConversationState()
        deterministic = state.apply_deterministic_answers("pata nahi", turn_id="same-turn")
        semantic = apply(
            state,
            "pata nahi",
            decision("business_name", None, "pata nahi", disposition="unknown"),
            turn_id="same-turn",
        )

        self.assertEqual(len(deterministic), 1)
        self.assertEqual(semantic, [])
        self.assertEqual(len([item for item in state.transitions if item.get("field") == "business_name"]), 1)

    def test_native_hindi_refusal_ends_optional_sequence(self):
        state = GatedConversationState()

        transitions = state.apply_deterministic_answers(
            "ab ye main nahi bata sakta",
            turn_id="refusal",
        )

        self.assertEqual(len(transitions), 1)
        self.assertEqual(state.optional_ended_by, "business_name")
        self.assertEqual(state.pending_field(), "pickup_pincode")

    def test_native_script_unknown_and_both_payment_are_deterministic(self):
        state = GatedConversationState()
        state.apply_deterministic_answers("पता नहीं", turn_id="unknown")
        state.apply_deterministic_answers(
            "pickup 201305 delivery 110001 weight 500 g دونوں",
            turn_id="shipment",
        )

        self.assertEqual(state.value("payment_type"), "Both")
        self.assertEqual(state.value("dead_weight"), 0.5)
        self.assertTrue(state.pricing_ready())

    def test_common_hindi_asr_both_variants_are_deterministic(self):
        for index, utterance in enumerate(("\u0926\u094b\u0928\u093e", "\u0926\u094b\u0928\u094b", "donon")):
            with self.subTest(utterance=utterance):
                state = GatedConversationState()
                state.apply_deterministic_answers(
                    "business pata nahi",
                    turn_id=f"optional-{index}",
                )
                state.apply_deterministic_answers(
                    f"pickup 201305 delivery 110001 weight 500 g {utterance}",
                    turn_id=f"shipment-{index}",
                )
                self.assertEqual(state.value("payment_type"), "Both")

    def test_v4_both_with_incomplete_route_selects_prepaid_starting_response(self):
        state = GatedConversationState(v4_strict_flow=True)
        state.seed_context(
            {
                "business_name": "Work Shop",
                "business_type": "D2C",
                "current_shipping_arrangement": "Own Arrangement",
                "current_shipping_rate": 40,
                "current_problem": "High Rates",
            }
        )
        state.apply_deterministic_answers("जी बताइए।", turn_id="consent")
        state.apply_deterministic_answers("normal rate check karna hai", turn_id="intent")
        state.apply_deterministic_answers(
            "pickup 201305 weight 500 g dono",
            turn_id="shipment",
        )

        self.assertEqual(state.value("conversation_consent"), "Accepted")
        self.assertEqual(state.value("payment_type"), "Both")
        self.assertEqual(state.pending_field(), "delivery_pincode")
        self.assertEqual(state.pricing_mode(), "general_starting")
        self.assertEqual(state.pricing_trigger_field(), "payment_type_both")
        self.assertTrue(state.starting_rate_due())

    def test_v4_both_with_complete_route_is_exact_prepaid_basis(self):
        state = GatedConversationState(v4_strict_flow=True)
        state.seed_context(
            {
                "business_name": "Work Shop",
                "business_type": "D2C",
                "current_shipping_arrangement": "Own Arrangement",
                "current_shipping_rate": 40,
                "current_problem": "High Rates",
                "pickup_pincode": "201305",
                "delivery_pincode": "110001",
                "dead_weight": 0.5,
                "payment_type": "Both",
            }
        )
        state.apply_deterministic_answers("yes", turn_id="consent")
        state.apply_deterministic_answers("normal rate check karna hai", turn_id="intent")

        self.assertEqual(state.pricing_mode(), "exact")
        self.assertTrue(state.pricing_ready())
        self.assertNotEqual(state.pending_field(), "order_value")

    def test_call_1392_no_current_arrangement_ends_optional_sequence(self):
        state = arrangement_pending_state()
        state.apply_deterministic_answers(
            "pickup 110001 delivery 201305 weight 500 g",
            turn_id="shipment",
        )

        transitions = state.apply_deterministic_answers(
            "Abhi mein kuchh use nahi kar raha hun naya home business mein.",
            turn_id="arrangement-none",
            previous_agent_text=(
                "Abhi aap shipping ke liye kaun sa solution use kar rahe hain? "
                "Koi courier company ya koi aggregator?"
            ),
        )

        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["field"], "current_shipping_arrangement")
        self.assertEqual(transitions[0]["status"], "not_applicable")
        self.assertTrue(transitions[0]["optional_sequence_ended"])
        self.assertEqual(state.optional_ended_by, "current_shipping_arrangement")
        self.assertEqual(state.pending_field(), "payment_type")
        self.assertEqual(state.value("pickup_pincode"), "110001")
        self.assertEqual(state.value("delivery_pincode"), "201305")
        self.assertEqual(state.value("dead_weight"), 0.5)
        self.assertNotIn("current_shipping_arrangement", state.rate_arguments())
        self.assertEqual(
            state.rate_arguments()["qualification_refused_field"],
            "current_shipping_arrangement",
        )

    def test_no_current_arrangement_hinglish_hindi_and_english_variants(self):
        variants = (
            "Abhi tak koi courier select nahi kiya",
            "I am not using any courier right now",
            "no current shipping solution",
            "kuch nahi kuch nahi",
            "कुछ नहीं कुछ नहीं।",
        )
        for index, text in enumerate(variants):
            with self.subTest(text=text):
                state = arrangement_pending_state()
                transitions = state.apply_deterministic_answers(
                    text,
                    turn_id=f"none-{index}",
                )
                self.assertEqual(len(transitions), 1)
                self.assertEqual(
                    state.fields["current_shipping_arrangement"].status,
                    "not_applicable",
                )
                self.assertEqual(state.pending_field(), "pickup_pincode")

    def test_contextual_bare_no_handles_direct_but_not_negative_confirmation(self):
        direct_state = arrangement_pending_state()
        direct = direct_state.apply_deterministic_answers(
            "नहीं।",
            turn_id="direct-no",
            previous_agent_text=(
                "Abhi aap kaun sa shipping solution use kar rahe hain, "
                "koi courier ya aggregator?"
            ),
        )
        self.assertEqual(len(direct), 1)
        self.assertEqual(
            direct_state.fields["current_shipping_arrangement"].status,
            "not_applicable",
        )

        negative_state = arrangement_pending_state()
        ambiguous = negative_state.apply_deterministic_answers(
            "नहीं।",
            turn_id="negative-no",
            previous_agent_text="Bas confirm kijiye ki aapne koi shipping solution select nahi kiya?",
        )
        self.assertEqual(ambiguous, [])
        self.assertEqual(negative_state.pending_field(), "current_shipping_arrangement")

    def test_high_confidence_semantic_not_applicable_is_narrow_and_fail_closed(self):
        accepted = arrangement_pending_state()
        transitions = apply(
            accepted,
            "Filhaal mera koi logistics setup active nahi hai",
            decision(
                "current_shipping_arrangement",
                None,
                "koi logistics setup active nahi hai",
                disposition="not_applicable",
                confidence=0.95,
            ),
        )
        self.assertEqual(len(transitions), 1)
        self.assertEqual(
            accepted.fields["current_shipping_arrangement"].status,
            "not_applicable",
        )

        low_confidence = arrangement_pending_state()
        self.assertEqual(
            apply(
                low_confidence,
                "Filhaal mera koi logistics setup active nahi hai",
                decision(
                    "current_shipping_arrangement",
                    None,
                    "koi logistics setup active nahi hai",
                    disposition="not_applicable",
                    confidence=0.89,
                ),
            ),
            [],
        )

        unrelated = arrangement_pending_state()
        self.assertEqual(
            apply(
                unrelated,
                "Filhaal mera koi logistics setup active nahi hai",
                decision(
                    "current_shipping_arrangement",
                    None,
                    "koi logistics setup active nahi hai",
                    disposition="not_applicable",
                    confidence=0.99,
                ),
                turn_disposition="unrelated",
            ),
            [],
        )

    def test_future_shipkia_intent_and_noise_do_not_reopen_arrangement(self):
        state = arrangement_pending_state()
        state.apply_deterministic_answers(
            "kuch nahi",
            turn_id="none",
        )
        state.apply_deterministic_answers(
            "pickup 110001 delivery 201305 weight 500 g",
            turn_id="shipment",
        )

        self.assertEqual(
            state.apply_deterministic_answers("select 190", turn_id="noise-one"),
            [],
        )
        self.assertEqual(
            state.apply_deterministic_answers("boot", turn_id="noise-two"),
            [],
        )
        apply(
            state,
            "Ab ShipKia hi use karunga",
            decision("service", "ShipKia", "ShipKia"),
            turn_id="future-service",
        )

        self.assertEqual(state.optional_ended_by, "current_shipping_arrangement")
        self.assertEqual(state.pending_field(), "payment_type")
        self.assertEqual(
            len(
                [
                    item
                    for item in state.transitions
                    if item.get("field") == "current_shipping_arrangement"
                ]
            ),
            1,
        )

    def test_payment_words_never_become_service(self):
        state = GatedConversationState()

        for index, value in enumerate(("Both", "COD", "दोनों"), start=1):
            transitions = state.apply_classifier_result(
                {
                    "turn_disposition": "answered",
                    "decisions": [
                        {
                            "field": "service",
                            "disposition": "answered",
                            "value": value,
                            "evidence": value,
                            "confidence": 1.0,
                        }
                    ],
                },
                customer_text=value,
                turn_id=f"payment-{index}",
            )
            self.assertFalse(transitions)

        self.assertFalse(state.is_handled("service"))

    def test_call_1622_keeps_discovery_context_and_separates_comparison_route(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers(
            "ji bataiye",
            turn_id="consent",
            previous_agent_text="Kya abhi baat karna convenient hai?",
        )
        state.apply_deterministic_answers(
            "mein research karna chaunga Delhi to Bangalore.",
            turn_id="intent-route",
            previous_agent_text="Aap rates check karna chahenge ya onboarding?",
        )
        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(state.requested_rate_type, "Normal")
        self.assertEqual(
            state.requested_routes,
            [{"pickup_location": "Delhi", "delivery_location": "Bengaluru"}],
        )
        self.assertEqual(state.pending_field(), "business_name")

        apply(
            state,
            "Harsh Enterprises",
            decision("business_name", "Harsh Enterprises", "Harsh Enterprises"),
            turn_id="business",
        )
        state.apply_deterministic_answers(
            "A to B sorry.",
            turn_id="business-type",
            previous_agent_text="Aapka business B2C hai ya D2C?",
        )
        self.assertEqual(state.value("business_type"), "B2B")
        state.apply_deterministic_answers(
            "Shiv Rakesh",
            turn_id="provider",
            previous_agent_text="Abhi aap kaun sa courier ya shipping provider use kar rahe hain?",
        )
        self.assertEqual(state.value("current_shipping_arrangement"), "Shipping Aggregator")
        self.assertEqual(state.value("current_provider_name"), "Shiprocket")

        state.apply_deterministic_answers(
            "32 for Delhi to Mumbai",
            turn_id="comparison-rate",
            previous_agent_text="Shiprocket par abhi current rate kya mil raha hai?",
        )
        self.assertEqual(state.value("current_shipping_rate"), 32.0)
        self.assertEqual(state.value("current_rate_basis"), "delhi to mumbai")
        self.assertEqual(
            state.requested_routes,
            [{"pickup_location": "Delhi", "delivery_location": "Bengaluru"}],
        )
        self.assertEqual(state.value("pickup_location"), "Delhi")
        self.assertEqual(state.value("delivery_location"), "Bengaluru")
        self.assertEqual(state.pending_field(), "current_problem")

        state.apply_deterministic_answers(
            "basically audio problem",
            turn_id="problem",
            previous_agent_text="Shiprocket ke saath main problem ya issue kya hai?",
        )
        self.assertEqual(state.pricing_mode(), "route_starting_pending")
        self.assertEqual(
            state.next_route_for_lookup(),
            {"pickup_location": "Delhi", "delivery_location": "Bengaluru"},
        )
        guidance = state.guidance().casefold()
        self.assertIn("audio/communication", guidance)
        self.assertIn("account-manager", guidance)
        self.assertIn("do not call it an ndr/rto issue", guidance)

    def test_clear_out_of_order_positive_fact_is_backfilled_without_advancing_early_gate(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        self.assertEqual(state.pending_field(), "assistance_intent")

        apply(
            state,
            "Harsh Enterprises",
            decision("business_name", "Harsh Enterprises", "Harsh Enterprises"),
            turn_id="early-business",
        )
        self.assertEqual(state.value("business_name"), "Harsh Enterprises")
        self.assertEqual(state.pending_field(), "assistance_intent")

        state.apply_deterministic_answers("shipping rates", turn_id="intent")
        self.assertEqual(state.pending_field(), "business_type")

    def test_only_successful_pricing_payload_amounts_are_authorized(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        self.assertFalse(state.rate_claim_amounts_authorized([45.0]))
        state.authorize_rate_result(
            {
                "status": "success",
                "amount": 31.15,
                "breakdown": {"shipping_charge": 26.40, "gst": 4.75, "total": 31.15},
            }
        )
        self.assertTrue(state.rate_claim_amounts_authorized([31.15]))
        self.assertTrue(state.rate_claim_amounts_authorized([26.40, 4.75]))
        self.assertFalse(state.rate_claim_amounts_authorized([45.0]))

    def test_call_1623_asr_flow_keeps_state_clean_and_switches_catalogs(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        greeting = (
            "ShipKia ek shipping platform hai jo multiple courier partners ke saath shipments "
            "manage karta hai. Kya abhi baat kar sakte hain?"
        )
        state.apply_deterministic_answers(
            "ji bataiye",
            turn_id="consent",
            previous_agent_text=greeting,
        )
        self.assertFalse(state.is_handled("current_shipping_arrangement"))
        self.assertFalse(state.is_handled("current_provider_name"))

        state.apply_deterministic_answers(
            "on board hai kya?",
            turn_id="onboarding",
            previous_agent_text="Rates check karna chahenge ya onboarding help chahiye?",
        )
        self.assertEqual(state.value("assistance_intent"), "Onboarding")
        state.apply_deterministic_answers(
            "Mein rash check karna chahunga.",
            turn_id="rate-switch",
            previous_agent_text="Main onboarding mein help kar sakta hoon.",
        )
        self.assertEqual(state.value("assistance_intent"), "Rates")
        apply(
            state,
            "Mein rash check karna chahunga.",
            decision("service", "rash", "rash"),
            turn_id="rate-switch-semantic",
        )
        self.assertFalse(state.is_handled("service"))

        apply(
            state,
            "Mera business Harsh Enterprises hai",
            decision("business_name", "Harsh Enterprises", "Harsh Enterprises"),
            turn_id="business",
        )
        state.apply_deterministic_answers("D2C", turn_id="business-type")
        self.assertEqual(state.pending_field(), "current_shipping_arrangement")
        state.apply_deterministic_answers(
            "Abhi main shipping nahi kar raha, shuru karne ki soch raha hoon.",
            turn_id="new-shipper",
            previous_agent_text="Abhi aap kaun sa courier ya provider use kar rahe hain?",
        )
        self.assertEqual(state.value("current_shipping_arrangement"), "No Current Arrangement")
        self.assertEqual(state.pending_field(), "pickup_pincode")

        state.apply_deterministic_answers(
            "Noida 201305",
            turn_id="pickup",
            previous_agent_text="Apna pickup pincode bataiye.",
        )
        apply(
            state,
            "Noida 201305",
            decision("pickup_location", "Noida", "Noida"),
            turn_id="pickup",
        )
        self.assertEqual(state.value("pickup_pincode"), "201305")
        self.assertFalse(state.is_handled("pickup_location"))

        state.apply_deterministic_answers(
            "Difficulty hai Pen India.",
            turn_id="pan-india",
            previous_agent_text="Delivery pincode bataiye.",
        )
        self.assertTrue(state.pan_india_requested)
        self.assertEqual(state.pending_field(), "")
        self.assertEqual(state.next_route_for_lookup(), {"pan_india": True})

        apply(
            state,
            "1000",
            decision("service", "1000", "1000"),
            turn_id="quantity-service-noise",
        )
        self.assertFalse(state.is_handled("service"))

        state.apply_deterministic_answers("slide rate bata do", turn_id="flat-asr")
        self.assertEqual(state.requested_rate_type, "Flat")
        self.assertTrue(state.flat_catalog_due())
        state.mark_flat_catalog_presented()
        state.apply_deterministic_answers("blood rate bata do", turn_id="flat-repeat")
        self.assertFalse(state.flat_catalog_due())
        state.apply_deterministic_answers(
            "flat rate ya flat channel ke option hain?",
            turn_id="flat-zonal-asr",
        )
        self.assertEqual(state.requested_rate_type, "Flat Zonal")
        self.assertTrue(state.flat_zonal_catalog_due())

    def test_call_1627_contextual_bhojpuri_17_asr_authorizes_flat_catalog(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="rates")
        state.mark_pricing_verified("lookup_pincode_serviceability")

        state.apply_deterministic_answers(
            "Bhojpuri 17",
            turn_id="flat-asr",
            previous_agent_text="Kya aap aur kuch jaanna chahenge?",
        )

        self.assertEqual(state.requested_rate_type, "Flat")
        self.assertTrue(state.flat_catalog_due())
        self.assertEqual(state.pricing_mode(), "flat_catalog")

    def test_latest_successful_pricing_result_replaces_old_amount_authorization(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.authorize_rate_result({"status": "success", "total": 76.58})
        self.assertTrue(state.rate_claim_amounts_authorized([76.58]))
        state.authorize_rate_result(
            {"status": "success", "zone_groups": [{"total": 84.37}, {"total": 92.04}]}
        )
        self.assertFalse(state.rate_claim_amounts_authorized([76.58]))
        self.assertTrue(state.rate_claim_amounts_authorized([84.37, 92.04]))


class _FakeModels:
    def __init__(self, response=None, *, delay=0):
        self.response = response
        self.delay = delay

    async def generate_content(self, **_kwargs):
        await asyncio.sleep(self.delay)
        return self.response


class _FakeClient:
    def __init__(self, response=None, *, delay=0):
        self.aio = type("Aio", (), {"models": _FakeModels(response, delay=delay)})()


class _Response:
    def __init__(self, text):
        self.text = text


class TestSemanticAnswerGuard(unittest.IsolatedAsyncioTestCase):
    def test_schema_restricts_model_to_free_text_fields(self):
        field_enum = (
            SemanticAnswerGuard.RESPONSE_SCHEMA["properties"]["decisions"]["items"]["properties"]
            ["field"]["enum"]
        )
        self.assertIn("business_name", field_enum)
        self.assertIn("service", field_enum)
        self.assertNotIn("pickup_pincode", field_enum)
        self.assertNotIn("dead_weight", field_enum)
        disposition_enum = (
            SemanticAnswerGuard.RESPONSE_SCHEMA["properties"]["decisions"]["items"]["properties"]
            ["disposition"]["enum"]
        )
        self.assertIn("not_applicable", disposition_enum)

    async def test_malformed_json_raises_for_fail_closed_caller(self):
        guard = SemanticAnswerGuard(client=_FakeClient(_Response("not-json")))
        with self.assertRaises(ValueError):
            await guard.classify(customer_text="North Star", pending_field="business_name", state_snapshot={})

    async def test_timeout_raises_for_fail_closed_caller(self):
        guard = SemanticAnswerGuard(
            client=_FakeClient(_Response('{"turn_disposition":"unrelated","decisions":[]}'), delay=0.05),
            timeout_seconds=0.01,
        )
        with self.assertRaises(asyncio.TimeoutError):
            await guard.classify(customer_text="pehle rate batao", pending_field="business_name", state_snapshot={})
