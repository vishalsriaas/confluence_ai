from __future__ import annotations

import asyncio
import unittest

from livekit_agent.conversation_state import (
    GatedConversationState,
    SemanticAnswerGuard,
    V6ConversationState,
)


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
    def test_latest_call_rate_choice_route_is_retained_as_active_route(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("Ji bataiye", turn_id="consent")

        state.apply_deterministic_answers(
            "Theek hai, mujhe rates bata dijiye Delhi to Kerala ke.",
            turn_id="intent-route",
            previous_agent_text="Kya aap rates check karna chahenge?",
        )

        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(state.value("pickup_location"), "Delhi")
        self.assertEqual(state.value("delivery_location"), "Kerala")
        self.assertEqual(
            state.next_route_for_lookup(),
            {"pickup_location": "Delhi", "delivery_location": "Kerala"},
        )

    def test_latest_call_irrelevant_or_garbled_provider_is_not_saved(self):
        for answer in (
            "Currently toh main stock market ka use kar raha hun.",
            "Karthik x ^ 6Y",
        ):
            with self.subTest(answer=answer):
                state = V6ConversationState()
                state.seed_context(
                    {
                        "conversation_consent": "Accepted",
                        "assistance_intent": "Rates",
                        "business_name": "Evergreen",
                        "business_type": "D2C",
                    }
                )
                transitions = state.apply_deterministic_answers(
                    answer,
                    turn_id="provider",
                    previous_agent_text=(
                        "Abhi shipments ke liye kaunsa courier ya aggregator use karte hain?"
                    ),
                )

                self.assertEqual(transitions, [])
                self.assertFalse(state.is_handled("current_shipping_arrangement"))
                self.assertFalse(state.is_handled("current_provider_name"))

    def test_unfamiliar_but_plausible_provider_name_is_preserved(self):
        state = V6ConversationState()
        state.seed_context(
            {
                "conversation_consent": "Accepted",
                "assistance_intent": "Rates",
                "business_name": "Evergreen",
                "business_type": "D2C",
            }
        )

        state.apply_deterministic_answers(
            "Main NimbusPost use kar raha hun.",
            turn_id="provider",
            previous_agent_text="Abhi aap kaunsa shipping provider use karte hain?",
        )

        self.assertEqual(state.value("current_provider_name"), "nimbuspost")
        self.assertEqual(state.value("current_shipping_arrangement"), "Other")

    def test_v7_provider_is_followed_by_problem_and_matching_usp_transition(self):
        state = V6ConversationState()
        state.apply_deterministic_answers(
            "Haan ji, main handle karta hoon aur abhi baat kar sakte hain.",
            turn_id="consent",
            previous_agent_text=(
                "Kya main business ki shipping ya operations handle karne wale person se baat "
                "kar raha hoon, aur kya abhi around do minute baat karna convenient hai?"
            ),
        )
        state.apply_deterministic_answers(
            "Rates check karne hain",
            turn_id="intent",
            previous_agent_text="Aapko shipping mein abhi kis cheez ki help chahiye?",
        )
        state.seed_context({"business_name": "Evergreen", "business_type": "D2C"})
        state.apply_deterministic_answers(
            "Main Shiprocket use kar raha hun.",
            turn_id="provider",
            previous_agent_text="Abhi aap kaunsa shipping provider use karte hain?",
        )

        self.assertEqual(state.pending_field(), "current_problem")
        self.assertIn("main current shipping challenge", state.guidance())

        apply(
            state,
            "RTO follow-up mein dikkat hai.",
            decision("current_problem", "RTO follow-up mein dikkat hai", "RTO follow-up mein dikkat hai"),
            turn_id="problem",
        )

        self.assertEqual(state.pending_field(), "pickup_location")
        self.assertIn("central problem-to-USP response rule", state.guidance())

    def test_unrecognized_p2c_audio_cannot_be_rewritten_as_d2c(self):
        state = V6ConversationState()
        state.seed_context(
            {
                "conversation_consent": "Accepted",
                "assistance_intent": "Rates",
                "business_name": "Evergreen",
            }
        )

        applied = apply(
            state,
            "Mera business P2C model hai.",
            decision("business_type", "D2C", "Mera business P2C model hai."),
            turn_id="business-type",
        )

        self.assertEqual(applied, [])
        self.assertFalse(state.is_handled("business_type"))

    def test_latest_call_site_rate_asr_requests_flat_catalog(self):
        state = V6ConversationState()
        state.seed_context(
            {
                "conversation_consent": "Accepted",
                "assistance_intent": "Rates",
                "business_name": "Evergreen",
                "business_type": "D2C",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
            }
        )

        state.apply_deterministic_answers(
            "Mujhe site rate bata sakte hain?", turn_id="flat-asr"
        )

        self.assertEqual(state.requested_rate_type, "Flat")
        self.assertTrue(state.flat_catalog_due())

    def test_business_name_answer_saves_only_the_name(self):
        state = V6ConversationState()
        state.seed_context(
            {"conversation_consent": "Accepted", "assistance_intent": "Rates"}
        )

        state.apply_deterministic_answers(
            "Haan. Mere business ka naam hai Evergreen.",
            turn_id="business-name",
            previous_agent_text="Aapke business ka naam kya hai?",
        )

        self.assertEqual(state.value("business_name"), "Evergreen")

    def test_v7_affirmative_to_rate_only_followup_confirms_rates_intent(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("Ji bataiye", turn_id="consent")

        state.apply_deterministic_answers(
            "Haan ji, karvao.",
            turn_id="rates-yes",
            previous_agent_text="Kya aap rates check karna chahenge?",
        )

        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(state.pending_field(), "business_name")

    def test_v7_rate_discovery_question_settles_intent_and_keeps_business_answer(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("Ji bataiye", turn_id="consent")

        state.apply_deterministic_answers(
            "Mere business ka naam E-Light Fashion hai.",
            turn_id="business",
            previous_agent_text=(
                "Rates check karne ke liye aapke business ya brand ka naam kya hai?"
            ),
        )

        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(
            state.value("business_name"),
            "Mere business ka naam E-Light Fashion hai",
        )
        self.assertEqual(state.pending_field(), "business_type")

    def test_v7_irrelevant_answer_retries_once_then_skips_noncritical_topic(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("Ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Rates check karne hain", turn_id="intent")
        state.seed_context({"business_name": "E-Light Fashion"})

        state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    decision("business_platform", "Shopify", "Shopify")
                ],
            },
            customer_text="Main Shopify use karta hoon.",
            turn_id="irrelevant-one",
        )

        self.assertEqual(state.pending_field(), "business_type")
        self.assertEqual(state.pending_retry_counts["business_type"], 1)
        self.assertEqual(state.last_turn_disposition, "mixed")
        self.assertIn("still-needed question once more", state.guidance())

        transitions = state.apply_classifier_result(
            {"turn_disposition": "unrelated", "decisions": []},
            customer_text="SHOP5",
            turn_id="irrelevant-two",
        )

        self.assertIn("business_type", state.skipped_after_retry_fields)
        self.assertEqual(state.pending_field(), "current_shipping_arrangement")
        self.assertTrue(
            any(item["event"] == "noncritical_field_skipped_after_retry" for item in transitions)
        )

    def test_v7_sentence_fragment_is_not_saved_as_business_name(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("Ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Rates check karne hain", turn_id="intent")

        transitions = state.apply_deterministic_answers(
            "B.Tech ka naam hai I am a queen.",
            turn_id="bad-name",
            previous_agent_text="Aapke business ka naam kya hai?",
        )

        self.assertFalse(any(item.get("field") == "business_name" for item in transitions))
        self.assertFalse(state.is_handled("business_name"))
        self.assertEqual(state.pending_field(), "business_name")

    def test_v7_new_generic_route_clears_old_provider_scope_and_pricing(self):
        state = V6ConversationState()
        state.seed_context(
            {
                "conversation_consent": "Accepted",
                "assistance_intent": "Rates",
                "business_name": "Evergreen",
                "business_type": "D2C",
                "current_shipping_arrangement": "Own Arrangement",
                "pickup_location": "Delhi",
                "delivery_location": "Kerala",
            }
        )
        state.mark_pricing_verified("get_shipkia_starting_rate")
        state.begin_provider_rate_request("Blue Dart")

        state.apply_deterministic_answers(
            "Delhi to Noida ka rate batao",
            turn_id="changed-route",
        )

        self.assertEqual(state.active_route(), {
            "pickup_location": "Delhi",
            "delivery_location": "Noida",
        })
        self.assertEqual(state.provider_rate_scope, "")
        self.assertEqual(state.requested_provider_rate_name, "")
        self.assertFalse(state.provider_rates_answer_due)
        self.assertFalse(state.verified_rate_presented())
        self.assertEqual(state.pricing_mode(), "route_starting_pending")

    def test_v7_semantic_model_cannot_rewrite_a_customer_business_name(self):
        state = V6ConversationState()
        state.seed_context({"business_name": "Elite Fashion"})

        transitions = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    decision(
                        "business_name",
                        "Allied Fashion",
                        "Sorry but main light fresh bola tha",
                        confidence=0.9,
                    )
                ],
            },
            customer_text="Sorry but main light fresh bola tha.",
            turn_id="name-correction",
        )

        self.assertEqual(state.value("business_name"), "Elite Fashion")
        self.assertEqual(transitions, [])

    def test_v7_opening_uses_one_combined_consent(self):
        state = V6ConversationState()

        first = state.apply_deterministic_answers(
            "Haan ji, main handle karta hoon aur abhi baat kar sakte hain.",
            turn_id="combined-consent",
            previous_agent_text=(
                "Kya main business ki shipping ya operations handle karne wale person se baat "
                "kar raha hoon, aur kya abhi around do minute baat karna convenient hai?"
            ),
        )

        self.assertTrue(state.correct_person_confirmed)
        self.assertEqual(state.value("conversation_consent"), "Accepted")
        self.assertEqual(state.pending_field(), "assistance_intent")
        self.assertTrue(any(item["event"] == "correct_person_confirmed" for item in first))
        self.assertTrue(any(item.get("field") == "conversation_consent" for item in first))

    def test_production_v6_state_has_no_legacy_mode_selection(self):
        state = V6ConversationState()

        self.assertTrue(state.v4_strict_flow)
        self.assertTrue(state.v5_company_pair_flow)
        self.assertTrue(state.direct_onboarding_flow)
        self.assertTrue(state.model_led_flow)
        with self.assertRaisesRegex(ValueError, "fixed for the V6-only production flow"):
            state.model_led_flow = False

    def test_call_1708_followup_rate_does_not_rearm_anything_else_checkpoint(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 1000})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates batao", turn_id="intent")
        state.mark_pricing_verified("lookup_shipkia_route_rate")
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
                self.assertIn("handles the business's shipping or operations", guidance)
                self.assertNotIn("shipping rates check karne ya onboarding", guidance)

    def test_call_1707_unclear_first_audio_cannot_advance_consent(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)

        transitions = state.apply_deterministic_answers("Adios.", turn_id="call-1707-noise")
        guidance = state.guidance()

        self.assertEqual(transitions, [])
        self.assertEqual(state.pending_field(), "conversation_consent")
        self.assertIn("handles the business's shipping or operations", guidance)
        self.assertNotIn("shipping rates check", guidance)

    def test_natural_hindi_availability_advances_consent_without_exact_phrase(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)

        transitions = state.apply_deterministic_answers(
            "हां, फ्री हूं मैं।",
            turn_id="natural-consent",
        )

        self.assertEqual(state.value("conversation_consent"), "Accepted")
        self.assertEqual(state.pending_field(), "assistance_intent")
        self.assertEqual(transitions[0]["field"], "conversation_consent")

    def test_semantic_consent_can_advance_novel_natural_wording(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        customer_text = "जी हां, अभी आराम से बात हो सकती है"

        transitions = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    {
                        "field": "conversation_consent",
                        "disposition": "answered",
                        "value": "Accepted",
                        "evidence": customer_text,
                        "confidence": 0.99,
                    }
                ],
            },
            customer_text=customer_text,
            turn_id="semantic-consent",
            pending_field_at_turn_start="conversation_consent",
        )

        self.assertEqual(state.value("conversation_consent"), "Accepted")
        self.assertEqual(state.pending_field(), "assistance_intent")
        self.assertEqual(transitions[0]["source"], "classifier")

    def test_semantic_consent_cannot_spill_into_later_turns(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("haan", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")

        transitions = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    {
                        "field": "conversation_consent",
                        "disposition": "answered",
                        "value": "Accepted",
                        "evidence": "rates chahiye",
                        "confidence": 0.99,
                    }
                ],
            },
            customer_text="rates chahiye",
            turn_id="later-turn",
            pending_field_at_turn_start="assistance_intent",
        )

        self.assertEqual(transitions, [])
        self.assertEqual(state.fields["conversation_consent"].turn_id, "consent")

    def test_semantic_business_type_requires_matching_spoken_acronym(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("haan", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.seed_context({"business_name": "Harsh Enterprises"})

        transitions = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    {
                        "field": "business_type",
                        "disposition": "answered",
                        "value": "B2C",
                        "evidence": "वी मस्ट ऑफ यूज़ करता हूं",
                        "confidence": 0.99,
                    }
                ],
            },
            customer_text="वी मस्ट ऑफ यूज़ करता हूं",
            turn_id="noisy-business-type",
            pending_field_at_turn_start="business_type",
        )

        self.assertEqual(transitions, [])
        self.assertFalse(state.is_handled("business_type"))

    def test_time_like_asr_fragment_is_not_saved_as_current_rate(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("haan", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.seed_context(
            {
                "business_name": "Harsh Enterprises",
                "business_type": "B2C",
                "current_shipping_arrangement": "Other",
            }
        )

        state.apply_deterministic_answers(
            "ए, 2:30.",
            turn_id="broken-rate",
            previous_agent_text="Aapka current shipping rate kya hai?",
        )

        self.assertFalse(state.is_handled("current_shipping_rate"))

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
        self.assertIn("handles the business's shipping or operations", guidance)
        self.assertNotIn("shipping rates check karne ya onboarding", guidance)

    def test_early_side_queries_use_natural_help_continuation(self):
        expected = (
            "Aap kuch aur jaanna chahenge, ya main aapko rates check karne ya "
            "onboarding mein help karun?"
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
                self.assertEqual(state.guidance().count(expected), 1)

    def test_call_1725_hindi_date_check_asr_selects_rates_without_problem_spillover(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("जी बताइए", turn_id="consent")
        customer_text = "ठीक है, मुझे डेट चेक करने हैं।"
        previous_agent_text = (
            "Aap kuch aur jaanna chahenge, ya main aapko rates check karne ya "
            "onboarding mein help karun?"
        )

        deterministic = state.apply_deterministic_answers(
            customer_text,
            turn_id="rate-intent",
            previous_agent_text=previous_agent_text,
        )
        semantic = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    decision("current_problem", "date check", "डेट चेक")
                ],
            },
            customer_text=customer_text,
            turn_id="rate-intent",
            pending_field_at_turn_start="assistance_intent",
        )

        self.assertTrue(deterministic)
        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertFalse(state.is_handled("current_problem"))
        self.assertEqual(semantic, [])
        self.assertEqual(state.pending_field(), "business_name")

    def test_call_1726_asr_what_shipkia_does_requests_all_services_once(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")

        state.apply_deterministic_answers(
            "Mai pehle janana chahta hu, Ship kya ki-kya-kya kar dethe hain?",
            turn_id="services-asr",
            previous_agent_text="Aap shipping rates check karna chahenge ya onboarding mein help chahiye?",
        )
        guidance = state.guidance()

        self.assertTrue(state.last_usp_query)
        self.assertTrue(state.last_detailed_usp_query)
        self.assertIn("explain all four verified facts", guidance)
        self.assertIn("WhatsApp order confirmation", guidance)
        self.assertIn("IVR-call", guidance)
        self.assertEqual(guidance.count("Aap kuch aur jaanna chahenge"), 1)

    def test_call_1726_contextual_aur_kya_kya_keeps_detailed_services_context(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")

        state.apply_deterministic_answers(
            "यह भी चीज और और क्या-क्या?",
            turn_id="more-services",
            previous_agent_text=(
                "ShipKia par multiple courier partners ke saath shipments manage hote hain. "
                "Dedicated account manager support bhi milta hai."
            ),
        )

        self.assertTrue(state.last_usp_query)
        self.assertTrue(state.last_detailed_usp_query)
        self.assertIn("explain all four verified facts", state.guidance())

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
            "Aap kuch aur jaanna chahenge, ya main aapko rates check karne ya "
            "onboarding mein help karun?",
            state.guidance(),
        )
        self.assertNotIn("ask the same question naturally again", state.guidance())

    def test_consumed_checkpoint_is_not_reasked_after_late_quantity_answer(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 1000})
        state.last_monthly_quantity_captured = True
        state.anything_else_checkpoint_consumed = True

        guidance = state.guidance()

        self.assertIn("acknowledge", guidance)
        self.assertIn("then stop", guidance)
        self.assertNotIn("Kya aap kuch aur jaanna chahenge", guidance)

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
        self.assertIn("kuch aur jaanna chahenge", guidance)
        self.assertIn("rates check karne ya onboarding", guidance)
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
        self.assertTrue(state.all_courier_rates_due())
        state.authorize_rate_result(
            {
                "status": "success",
                "response_type": "zone_starting",
                "amount": 36.34,
                "available_courier_partners": ["Amazon"],
                "starting_rate_options": [
                    {"courier": "Amazon", "service": "Amazon Standard", "amount": 36.34}
                ],
            }
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
        self.assertIn("kuch aur jaanna chahenge", guidance)
        self.assertIn("rates check karne ya onboarding", guidance)

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
        state.mark_pricing_verified("lookup_shipkia_route_rate")

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
        self.assertIn("kuch aur jaanna chahenge", guidance)
        self.assertIn("rates check karne ya onboarding", guidance)

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

    def test_call_2011_english_asr_show_a_rocket_normalizes_to_shiprocket(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context(
            {
                "business_name": "Harsh Enterprises",
                "business_type": "D2C",
                "business_platform": "Website",
            }
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="intent")

        state.apply_deterministic_answers(
            "Show a rocket.",
            turn_id="provider",
            previous_agent_text="Aap abhi kaun si shipping company use kar rahe hain?",
        )

        self.assertEqual(state.value("current_shipping_arrangement"), "Shipping Aggregator")
        self.assertEqual(state.value("current_provider_name"), "Shiprocket")

    def test_call_1681_generic_services_query_requests_all_verified_usps(self):
        for customer_text in (
            "Aur kaun-kaun si services provide karte hain aap?",
            "Chupkiyan ki services ke bare mein bataye.",
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

        detail_state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        detail_state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        detail_state.apply_deterministic_answers(
            "\u0939\u093e\u0902, \u092e\u0941\u091d\u0947 \u0921\u093f\u091f\u0947\u0932 \u092e\u0947\u0902 \u092c\u0924\u093e\u0907\u090f\u0964 \u091c\u093f\u0924\u0928\u093e \u0921\u093f\u091f\u0947\u0932 \u0939\u094b \u0938\u0915\u0947 \u0909\u0938\u092e\u0947\u0902 \u092c\u0924\u093e\u0907\u090f\u0964",
            turn_id="details",
            previous_agent_text=(
                "ShipKia mein dedicated account manager aur WhatsApp order confirmation support "
                "milta hai."
            ),
        )
        self.assertTrue(detail_state.last_usp_query)
        self.assertTrue(detail_state.last_detailed_usp_query)

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
        self.assertTrue(state.all_courier_rates_due())
        state.authorize_rate_result(
            {
                "status": "success",
                "response_type": "zone_starting",
                "zone": "C",
                "amount": 31.15,
                "available_courier_partners": ["Shree Maruti", "Amazon"],
                "starting_rate_options": [
                    {"courier": "Shree Maruti", "service": "Shree Maruti Surface", "amount": 31.15},
                    {"courier": "Amazon", "service": "Amazon Standard", "amount": 36.34},
                ],
            }
        )
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
        state.mark_pricing_verified("lookup_shipkia_route_rate")

        for index, customer_text in enumerate(
            ("इन सबके रेट बता दीजिए।", "मैंने पूछा है सबके रेट बता दीजिए।")
        ):
            state.apply_deterministic_answers(
                customer_text,
                turn_id=f"all-rates-{index}",
                previous_agent_text="Kya aap kuch aur jaanna chahenge?",
            )
            self.assertTrue(state.all_courier_rates_due())
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

    def test_observed_shipkia_asr_variants_remain_usp_questions(self):
        for transcript in (
            "मुझे पहले शिप क्या के बारे में बताएंगे, शिप क्या क्या है?",
            "मुझे शिव प्रिया के बारे में बताओ।",
            "Tell me about Shipyard",
        ):
            with self.subTest(transcript=transcript):
                state = GatedConversationState(
                    v4_strict_flow=True,
                    v5_company_pair_flow=True,
                )
                state.apply_deterministic_answers("ji bataiye", turn_id="consent")
                state.apply_deterministic_answers(transcript, turn_id="usp")

                self.assertTrue(state.last_usp_query)
                self.assertEqual(state.pending_field(), "assistance_intent")
                self.assertIn("ShipKia", state.guidance())

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
        self.assertEqual(state.pending_field(), "pickup_location")

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
        self.assertIn("sell directly to customers", state.guidance())
        self.assertIn("supply other businesses", state.guidance())

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
        state.mark_pricing_verified("lookup_shipkia_route_rate")

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

    def test_latest_call_bare_number_cannot_fill_volume_and_rate_while_name_is_pending(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates check karte hain", turn_id="intent")
        self.assertEqual(state.pending_field(), "business_name")

        customer_text = "10-10"
        deterministic = state.apply_deterministic_answers(
            customer_text,
            turn_id="latest-number",
            previous_agent_text=(
                "ShipKia multiple courier partners ke saath shipments manage karta hai. "
                "Aap rates check karna chahenge?"
            ),
        )
        semantic = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    decision("monthly_shipments", 10, "10"),
                    decision("current_shipping_rate", 10, "10"),
                ],
            },
            customer_text=customer_text,
            turn_id="latest-number",
            pending_field_at_turn_start="business_name",
        )

        self.assertEqual(deterministic, [])
        self.assertEqual(semantic, [])
        self.assertFalse(state.is_handled("monthly_shipments"))
        self.assertFalse(state.is_handled("current_shipping_rate"))
        self.assertEqual(state.pending_field(), "business_name")

        state.apply_deterministic_answers("D2C", turn_id="premature-type")
        self.assertFalse(state.is_handled("business_type"))
        self.assertEqual(state.pending_field(), "business_name")

    def test_v5_asks_and_captures_business_operating_platform_after_type(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
        )
        state.seed_context({"business_name": "Acme", "business_type": "D2C"})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="intent")

        self.assertEqual(state.pending_field(), "business_platform")
        self.assertIn("Shopify", state.guidance())
        state.apply_deterministic_answers(
            "WooCommerce",
            turn_id="platform",
            previous_agent_text=(
                "Aap apna business Shopify, WooCommerce, marketplace, apni website, "
                "ya kisi aur platform se operate karte hain?"
            ),
        )

        self.assertEqual(state.value("business_platform"), "WooCommerce")
        self.assertEqual(state.pending_field(), "current_shipping_arrangement")

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
            "Delhi",
            turn_id="pickup",
            previous_agent_text="Aap shipments kahan se kahan bhejte hain?",
        )

        self.assertIsNone(state.next_route_for_lookup())
        self.assertFalse(state.route_ready_for_lookup())
        self.assertEqual(state.pending_field(), "delivery_location")
        self.assertIn("delivery city or locality", state.guidance())
        self.assertIn("delivery city or locality", state.guidance().casefold())

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
            "Rflat Zonal",
            # call-1835: Gemini ASR rendered "Flat-Zonal rate" as
            # "flat zone ... let" and then "flat donal ... date".
            "\u0905\u091a\u094d\u091b\u093e, \u0906\u092a\u0915\u0947 \u092a\u093e\u0938 \u0915\u0941\u091b \u092b\u094d\u0932\u0948\u091f \u091c\u094b\u0928 \u0939\u0948, \u0932\u0947\u091f \u0926\u0947 \u0915\u094d\u092f\u093e?",
            "\u092e\u0948\u0902 \u092a\u0942\u091b \u0930\u0939\u093e \u0939\u0942\u0902, \u0906\u092a\u0915\u0947 \u092a\u093e\u0938 \u0915\u0941\u091b \u092b\u094d\u0932\u0948\u091f, \u0921\u094b\u0928\u0932, \u0921\u0947\u091f \u092d\u0940 \u0939\u0948 \u0915\u094d\u092f\u093e?",
            "flat donal date bhi hai kya?",
            "orthonal flat rate",
            "à flat Donner trade",
            "\u092b\u094d\u0932\u0948\u091f \u091c\u0930\u094d\u0928\u0932 \u0930\u0947\u091f",
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

    def test_call_1835_new_flat_zonal_request_overrides_stale_move_forward_state(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 1000})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("flat rate batao", turn_id="flat")
        state.mark_flat_catalog_presented()
        state.mark_pricing_verified("get_shipkia_flat_rates", payment_basis="Prepaid")
        state.anything_else_checkpoint_consumed = True
        state.move_forward_question_due = True

        state.apply_deterministic_answers(
            "\u0905\u091a\u094d\u091b\u093e, \u0906\u092a\u0915\u0947 \u092a\u093e\u0938 \u0915\u0941\u091b \u092b\u094d\u0932\u0948\u091f \u091c\u094b\u0928 \u0939\u0948, \u0932\u0947\u091f \u0926\u0947 \u0915\u094d\u092f\u093e?",
            turn_id="call-1835-flat-zonal",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
        )

        self.assertEqual(state.requested_rate_type, "Flat Zonal")
        self.assertTrue(state.flat_zonal_catalog_due())
        self.assertFalse(state.move_forward_question_due)
        self.assertFalse(state.anything_else_question_due)
        self.assertIn("get_shipkia_flat_zonal_rates exactly once", state.guidance())
        self.assertNotIn("aage badhna", state.guidance())

    def test_pending_catalog_guidance_defensively_precedes_stale_move_forward_flag(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.requested_rate_type = "Flat Zonal"
        state.pending_catalogs = {"Flat Zonal"}
        state.move_forward_question_due = True

        guidance = state.guidance()

        self.assertIn("get_shipkia_flat_zonal_rates exactly once", guidance)
        self.assertNotIn("aage badhna", guidance)

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
        self.assertIn("kuch aur jaanna chahenge", guidance)
        self.assertIn("rates check karne ya onboarding", guidance)
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
        self.assertIn("lookup_shipkia_route_rate", guidance)
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
        self.assertIn("lookup_shipkia_route_rate", guidance)

    def test_v5_devanagari_city_routes_are_queued(self):
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
        self.assertIn("dedicated account manager", state.guidance())
        self.assertIn("support and ticketing", state.guidance())

    def test_live_v5_high_volume_moves_directly_to_onboarding_decision(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
        )
        state.seed_context({"monthly_shipments": 2000})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("flat rate batao", turn_id="rate")
        state.mark_flat_catalog_presented()
        state.mark_pricing_verified("get_shipkia_flat_rates")

        self.assertFalse(state.anything_else_question_due)
        self.assertTrue(state.move_forward_question_due)
        guidance = state.guidance()
        self.assertIn("ShipKia ke saath aage badhna", guidance)
        self.assertNotIn("Kya aap kuch aur jaanna chahenge", guidance)

    def test_v5_manager_message_applies_only_above_five_hundred_shipments(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C rate batao", turn_id="rate")
        state.mark_pricing_verified("get_shipkia_starting_rate")

        state.apply_deterministic_answers(
            "500",
            turn_id="quantity",
            previous_agent_text="Aapki monthly shipment quantity kitni hoti hai?",
        )

        self.assertNotIn("dedicated account manager", state.guidance())
        self.assertIn("Kya aap kuch aur jaanna chahenge", state.guidance())

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
        state.mark_pricing_verified("lookup_shipkia_route_rate")

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

    def test_v4_extended_hindi_hinglish_consent_variants(self):
        for spoken in (
            "हां, जी, बताइए।",
            "हाँ बोलिए",
            "जी बताइए।",
            "Achchi bataiye.",
            "haan ji bataiye",
            "yeah",
            "yep",
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

    def test_v4_flat_rate_skips_route_but_requires_weight_and_payment(self):
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
        for spoken, expected in (
            ("G2C", "G2C"),
            ("day 2c", "D2C"),
            ("dee to c", "D2C"),
            ("due to x", "D2C"),
        ):
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

    def test_plain_language_operating_models_are_understood_and_saved(self):
        for spoken, expected in (
            ("We sell directly to customers", "D2C"),
            ("business to business", "B2B"),
            ("customer to customer", "C2C"),
            ("We sell mainly through marketplaces", "Marketplace-led"),
        ):
            with self.subTest(spoken=spoken):
                state = GatedConversationState()
                state.seed_context({"business_name": "Evergreen"})

                transitions = state.apply_deterministic_answers(
                    spoken,
                    turn_id=f"operating-model-{expected}",
                )

                self.assertTrue(
                    any(item.get("field") == "business_type" for item in transitions)
                )
                self.assertEqual(state.value("business_type"), expected)

    def test_semantic_guard_can_save_explicit_plain_language_operating_model(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("rates check karna hai", turn_id="intent")
        state.seed_context({"business_name": "Evergreen"})

        transitions = apply(
            state,
            "Hum directly end customers ko sell karte hain",
            decision(
                "business_type",
                "Direct to Customer",
                "directly end customers",
            ),
        )

        self.assertTrue(any(item.get("field") == "business_type" for item in transitions))
        self.assertEqual(state.value("business_type"), "D2C")

    def test_v6_hindi_selling_model_unlocks_route_knowledge_lookup(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("rates check karna hai", turn_id="intent")
        state.seed_context({"business_name": "Evergreen"})

        transitions = apply(
            state,
            "हां, मैं सीधे ग्राहकों को सामान बेचता हूं।",
            decision(
                "business_type",
                "D2C",
                "सीधे ग्राहकों को सामान बेचता हूं",
            ),
        )

        self.assertTrue(any(item.get("field") == "business_type" for item in transitions))
        self.assertEqual(state.pending_field(), "current_shipping_arrangement")
        state.seed_context(
            {
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_problem": "High rates",
            }
        )
        self.assertEqual(state.pending_field(), "pickup_location")
        state.apply_deterministic_answers("Delhi se Bangalore", turn_id="route")
        self.assertEqual(state.pricing_mode(), "route_starting_pending")
        self.assertIn("lookup_shipkia_route_rate", state.guidance())

    def test_operating_model_guidance_uses_plain_language_not_acronym_menu(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("rates check karna hai", turn_id="intent")
        state.seed_context({"business_name": "Evergreen"})

        guidance = state.guidance()

        self.assertIn("sell directly to customers", guidance)
        self.assertIn("supply other businesses", guidance)
        self.assertIn("Do not use B2C, B2B, or D2C acronyms", guidance)

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
        self.assertEqual(state.pending_field(), "pickup_location")
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
        self.assertIn("lookup_shipkia_route_rate", state.guidance())

        self.assertTrue(state.mark_route_zone_verified("A", starting_presented=True))
        self.assertEqual(state.pricing_mode(), "zone_starting")
        self.assertFalse(state.starting_rate_due())

    def test_v5_pan_india_uses_route_lookup(self):
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
        self.assertIn("lookup_shipkia_route_rate", state.guidance())

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

    def test_v6_active_route_keeps_other_endpoint_until_customer_changes_it(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.apply_deterministic_answers(
            "Delhi se Bengaluru",
            turn_id="initial-route",
        )
        initial = state.next_route_for_lookup()
        self.assertEqual(
            initial,
            {"pickup_location": "Delhi", "delivery_location": "Bengaluru"},
        )
        state.mark_route_zone_verified("C", starting_presented=True, route_arguments=initial)
        self.assertIsNone(state.next_route_for_lookup())
        self.assertEqual(state.active_route(), initial)

        state.apply_deterministic_answers(
            "ab Mumbai ke liye rate batao",
            turn_id="new-destination",
        )
        self.assertEqual(
            state.active_route(),
            {"pickup_location": "Delhi", "delivery_location": "Mumbai"},
        )
        self.assertEqual(state.next_route_for_lookup(), state.active_route())

        state.apply_deterministic_answers(
            "ab Noida se same rate batao",
            turn_id="new-pickup",
        )
        self.assertEqual(
            state.active_route(),
            {"pickup_location": "Noida", "delivery_location": "Mumbai"},
        )

    def test_v6_hindi_followup_route_replaces_zone_authorization_before_volume(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("हां बताइए", turn_id="consent")
        state.apply_deterministic_answers(
            "दिल्ली टू बेंगलुरु के रेट्स बता दीजिए",
            turn_id="first-route",
        )
        first_route = state.next_route_for_lookup()
        self.assertEqual(
            first_route,
            {"pickup_location": "Delhi", "delivery_location": "Bengaluru"},
        )
        state.mark_route_zone_verified(
            "C",
            starting_presented=True,
            route_arguments=first_route,
        )
        state.mark_pricing_verified("lookup_shipkia_route_rate")

        state.apply_deterministic_answers(
            "और दिल्ली टू मुंबई के भी रेट्स बता दो, फिर डिटेल्स बताता हूं",
            turn_id="second-route",
            previous_agent_text="क्या मैं आपके बिजनेस का नाम जान सकता हूं?",
        )

        expected_route = {"pickup_location": "Delhi", "delivery_location": "Mumbai"}
        self.assertEqual(state.active_route(), expected_route)
        self.assertEqual(state.next_route_for_lookup(), expected_route)
        self.assertEqual(state.pending_field(), "business_name")
        self.assertEqual(state.pricing_mode(), "pending")
        self.assertFalse(state.is_handled("business_name"))

    def test_v7_latest_call_two_thousand_asr_selects_flat_zonal_catalog(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("Ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Flat rates batao", turn_id="flat")
        state.mark_flat_catalog_presented()

        state.apply_deterministic_answers(
            "aur 2,000 rate mein",
            turn_id="flat-zonal-asr",
            previous_agent_text="Flat rates mein aur koi option chahiye?",
        )

        self.assertEqual(state.requested_rate_type, "Flat Zonal")
        self.assertTrue(state.flat_zonal_catalog_due())
        self.assertFalse(state.pricing_close_locked())

    def test_v7_named_courier_request_reopens_rate_lookup_with_trusted_zone(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("Ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Rates batao", turn_id="intent")
        state.seed_context({"zone": "D"})
        state.authorized_rate_amounts = {35.05}
        state.primary_rate_amount = 35.05
        state.verified_starting_options = [
            {"courier": "Shree Maruti", "service": "Surface", "amount": 35.05}
        ]
        state.onboarding_link_presented = True

        state.apply_deterministic_answers(
            "Blue Dart ka rate bata do",
            turn_id="bluedart-rate",
        )

        self.assertEqual(state.requested_provider_rate_name, "Blue Dart")
        self.assertTrue(state.named_courier_rate_due())
        self.assertEqual(state.authorized_rate_amounts, set())
        self.assertEqual(state.verified_starting_options, [])
        self.assertFalse(state.onboarding_link_presented)
        self.assertFalse(state.pricing_close_locked())
        self.assertIn("get_shipkia_starting_rate", state.guidance())

    def test_v6_new_explicit_zone_owns_turn_after_prior_zone_rate(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("हां बताइए", turn_id="consent")
        state.apply_deterministic_answers("Zone C के रेट्स", turn_id="zone-c")
        state.mark_pricing_verified("get_shipkia_starting_rate")
        state.mark_starting_rate_presented()

        state.apply_deterministic_answers("Zone D के रेट्स क्या हैं?", turn_id="zone-d")

        self.assertEqual(state.value("zone"), "D")
        self.assertEqual(state.pricing_mode(), "zone_starting")
        self.assertTrue(state.starting_rate_due())

    def test_v6_route_rate_resumes_complete_business_discovery_before_volume(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("हां जी", turn_id="consent")
        state.apply_deterministic_answers(
            "Delhi to Kerala ke rates check karne hain",
            turn_id="route",
        )
        route = state.next_route_for_lookup()
        state.mark_route_zone_verified(
            "D",
            starting_presented=True,
            route_arguments=route,
        )
        state.mark_pricing_verified("lookup_shipkia_route_rate")

        self.assertEqual(state.pending_field(), "business_name")
        state.apply_deterministic_answers(
            "हाँ, मेरे बिज़नेस का नाम है Harish Enterprises.",
            turn_id="business-name",
            previous_agent_text="Aapke business ka naam kya hai?",
        )
        self.assertEqual(state.value("business_name"), "Harish Enterprises")
        self.assertEqual(state.pending_field(), "business_type")

        state.apply_deterministic_answers(
            "main directly customers ko sell karta hun",
            turn_id="business-type",
        )
        self.assertEqual(state.value("business_type"), "D2C")
        self.assertEqual(state.pending_field(), "current_shipping_arrangement")

        state.apply_deterministic_answers(
            "Shiprocket use करता हूं",
            turn_id="provider",
            previous_agent_text="अभी कौन सा courier या shipping provider use करते हैं?",
        )
        self.assertEqual(state.value("current_shipping_arrangement"), "Shipping Aggregator")
        self.assertEqual(state.value("current_provider_name"), "Shiprocket")
        self.assertEqual(state.pending_field(), "current_problem")
        apply(
            state,
            "RTO follow-up mein dikkat hai",
            decision("current_problem", "RTO follow-up mein dikkat hai", "RTO follow-up mein dikkat hai"),
            turn_id="problem",
        )
        self.assertEqual(state.pending_field(), "monthly_shipments")

    def test_v6_observed_flame_zona_red_asr_requests_flat_zonal_catalog(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("हां जी", turn_id="consent")
        state.apply_deterministic_answers("rates check karne hain", turn_id="intent")
        state.mark_pricing_verified("get_shipkia_flat_rates")
        state.mark_flat_catalog_presented()
        state.anything_else_checkpoint_consumed = True

        state.apply_deterministic_answers("flame zona red", turn_id="flat-zonal-asr")

        self.assertEqual(state.requested_rate_type, "Flat Zonal")
        self.assertTrue(state.flat_zonal_catalog_due())
        self.assertEqual(state.pricing_mode(), "flat_zonal_catalog")

    def test_v6_background_city_does_not_replace_active_route(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.seed_context(
            {"pickup_location": "Delhi", "delivery_location": "Bengaluru"}
        )
        transitions = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    {
                        "field": "delivery_location",
                        "disposition": "answered",
                        "value": "Mumbai",
                        "evidence": "Mumbai",
                        "confidence": 1.0,
                    }
                ],
            },
            customer_text="Mera office Mumbai mein hai.",
            turn_id="background-city",
        )
        self.assertFalse(transitions)
        self.assertEqual(
            state.active_route(),
            {"pickup_location": "Delhi", "delivery_location": "Bengaluru"},
        )

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

    def test_payment_and_cod_refusals_do_not_enable_generic_rate(self):
        payment_state = GatedConversationState()
        apply(
            payment_state,
            "business pata nahi",
            decision("business_name", None, "pata nahi", disposition="unknown"),
        )
        apply(
            payment_state,
            "Delhi se Mumbai, 2 kg",
            decision("pickup_location", "Delhi", "Delhi"),
            decision("delivery_location", "Mumbai", "Mumbai"),
            decision("dead_weight", 2, "2 kg"),
        )
        apply(
            payment_state,
            "payment nahi batana",
            decision("payment_type", None, "nahi batana", disposition="refused"),
        )
        self.assertFalse(payment_state.pricing_ready())
        self.assertEqual(payment_state.pricing_mode(), "pending")
        self.assertEqual(payment_state.rate_arguments()["payment_type"], "Not Shared")

        cod_state = GatedConversationState()
        apply(
            cod_state,
            "business pata nahi",
            decision("business_name", None, "pata nahi", disposition="unknown"),
        )
        apply(
            cod_state,
            "Delhi se Mumbai, 2 kg COD",
            decision("pickup_location", "Delhi", "Delhi"),
            decision("delivery_location", "Mumbai", "Mumbai"),
            decision("dead_weight", 2, "2 kg"),
            decision("payment_type", "COD", "COD"),
        )
        apply(
            cod_state,
            "order value nahi batana",
            decision("order_value", None, "nahi batana", disposition="refused"),
        )
        self.assertFalse(cod_state.pricing_ready())
        self.assertEqual(cod_state.pricing_mode(), "pending")
        self.assertEqual(cod_state.rate_arguments()["order_value_status"], "Not Shared")

    def test_legacy_pan_india_request_does_not_enable_generic_rate(self):
        state = GatedConversationState()

        transitions = state.apply_deterministic_answers(
            "Pan India shipping rate batao",
            turn_id="pan-india",
        )

        self.assertTrue(
            any(item.get("event") == "pricing_mode_updated" for item in transitions)
        )
        self.assertEqual(state.pricing_mode(), "pending")
        self.assertEqual(state.pricing_trigger_field(), "pan_india")
        self.assertFalse(state.starting_rate_due())
        state.mark_starting_rate_presented()
        self.assertFalse(state.starting_rate_due())
        self.assertEqual(state.pricing_mode(), "pending")

        state.apply_deterministic_answers(
            "pickup Delhi",
            turn_id="specific-route",
        )
        self.assertEqual(state.pricing_mode(), "pending")

    def test_explicit_zone_is_not_inferred_from_route(self):
        state = GatedConversationState()
        state.apply_deterministic_answers(
            "Delhi se Bengaluru",
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

    def test_unlabelled_number_does_not_bypass_optional_question(self):
        state = GatedConversationState()

        state.apply_deterministic_answers("100000", turn_id="ambiguous")

        self.assertFalse(state.is_handled("pickup_location"))
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
        self.assertEqual(state.pending_field(), "pickup_location")

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
                self.assertEqual(state.pending_field(), "pickup_location")

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

    def test_call_1627_contextual_bhojpuri_17_asr_authorizes_flat_catalog(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="rates")
        state.mark_pricing_verified("lookup_shipkia_route_rate")

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

    def test_v6_latest_call_noise_numbers_do_not_pollute_sales_state(self):
        cases = (
            ("US 2", "current_shipping_rate", 2, "2"),
            ("rate bata 10", "monthly_shipments", 10, "10"),
            ("500 g", "service", "Surface", "500 g"),
        )
        for customer_text, field, value, evidence in cases:
            with self.subTest(customer_text=customer_text, field=field):
                state = GatedConversationState(
                    v4_strict_flow=True,
                    v5_company_pair_flow=True,
                    direct_onboarding_flow=True,
                    model_led_flow=True,
                )
                transitions = state.apply_classifier_result(
                    {
                        "turn_disposition": "answered",
                        "decisions": [
                            {
                                "field": field,
                                "disposition": "answered",
                                "value": value,
                                "evidence": evidence,
                                "confidence": 1.0,
                            }
                        ],
                    },
                    customer_text=customer_text,
                    turn_id="latest-call-noise",
                )
                self.assertEqual(transitions, [])
                self.assertFalse(state.is_handled(field))

    def test_v6_rate_omission_complaint_is_not_saved_as_customer_problem(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.seed_context(
            {"pickup_location": "Noida", "delivery_location": "Delhi"}
        )
        state.apply_deterministic_answers(
            "Aapne rate nahi bataye",
            turn_id="rate-complaint",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
        )
        semantic = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    {
                        "field": "current_problem",
                        "disposition": "answered",
                        "value": "Rate not shared",
                        "evidence": "rate nahi bataye",
                        "confidence": 1.0,
                    }
                ],
            },
            customer_text="Aapne rate nahi bataye",
            turn_id="rate-complaint-semantic",
        )

        self.assertTrue(state.rate_answer_owed)
        self.assertFalse(state.is_handled("current_problem"))
        self.assertEqual(semantic, [])
        self.assertFalse(state.move_forward_question_due)
        self.assertFalse(state.unsatisfied_resolution_due)
        self.assertIn("lookup_shipkia_route_rate", state.guidance())

        state.primary_rate_amount = 31.15
        self.assertIn("Rs 31.15", state.guidance())
        state.mark_owed_rate_presented()
        self.assertFalse(state.rate_answer_owed)

    def test_v6_latest_call_accepts_natural_consent_on_first_answer(self):
        for index, answer in enumerate(
            ("haan ji, kar sakte hain", "हां जी, कर सकते हैं।", "ji bilkul"),
            start=1,
        ):
            with self.subTest(answer=answer):
                state = GatedConversationState(
                    v4_strict_flow=True,
                    v5_company_pair_flow=True,
                    direct_onboarding_flow=True,
                    model_led_flow=True,
                )
                state.apply_deterministic_answers(answer, turn_id=f"consent-{index}")
                self.assertEqual(state.value("conversation_consent"), "Accepted")
                self.assertEqual(state.pending_field(), "assistance_intent")

    def test_v6_information_intent_exits_choice_gate_and_remembers_business_name(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers(
            "haan ji, kar sakte hain",
            turn_id="consent",
        )
        state.apply_deterministic_answers(
            "A message kiya, Kya chal raha hai se janna chahta hun.",
            turn_id="information",
            previous_agent_text=(
                "Aap kis mein help chahenge: shipping rates ya onboarding?"
            ),
        )

        self.assertTrue(state.last_usp_query)
        self.assertEqual(state.value("assistance_intent"), "Information")
        self.assertEqual(state.pricing_mode(), "information")
        self.assertFalse(state.pricing_ready())
        self.assertEqual(state.pending_field(), "business_name")

        state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    {
                        "field": "business_name",
                        "disposition": "answered",
                        "value": "Harsh Enterprises",
                        "evidence": "Harsh Enterprises",
                        "confidence": 1.0,
                    }
                ],
            },
            customer_text="Harsh Enterprises",
            turn_id="business-name",
            pending_field_at_turn_start="business_name",
        )

        self.assertEqual(state.value("business_name"), "Harsh Enterprises")
        self.assertEqual(state.pending_field(), "business_type")

    def test_v6_rate_flow_completes_discovery_route_rate_then_volume(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.seed_context(
            {
                "business_name": "Mars Enterprises",
                "business_type": "B2C",
                "business_platform": "Website",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_problem": "Support delays",
            }
        )
        self.assertEqual(state.pending_field(), "pickup_location")
        self.assertEqual(state.pricing_mode(), "pending")
        self.assertFalse(state.move_forward_question_due)

        state.apply_deterministic_answers(
            "Noida se Bangalore",
            turn_id="route",
            previous_agent_text="Aap shipments usually kahan se kahan bhejte hain?",
        )
        self.assertEqual(state.pending_field(), "")
        self.assertEqual(state.pricing_mode(), "route_starting_pending")

    def test_v6_route_answer_uses_hindi_city_not_provider_or_arrangement(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.seed_context({"business_name": "Evergreen", "business_type": "D2C"})
        self.assertEqual(state.pending_field(), "current_shipping_arrangement")

        state.apply_deterministic_answers(
            "मैं दिल्ली से",
            turn_id="pickup-city",
            previous_agent_text="Aap shipments kahan se bhejte hain?",
        )

        self.assertEqual(state.value("pickup_location"), "Delhi")
        self.assertFalse(state.is_handled("current_shipping_arrangement"))
        self.assertFalse(state.is_handled("current_provider_name"))
        self.assertEqual(state.pending_field(), "delivery_location")

        state.apply_deterministic_answers(
            "Bangalore ke liye",
            turn_id="delivery-city",
            previous_agent_text="Shipments kahan ke liye jaate hain?",
        )

        self.assertEqual(state.value("delivery_location"), "Bengaluru")
        self.assertEqual(state.pending_field(), "current_shipping_arrangement")
        self.assertEqual(state.pricing_mode(), "pending")

    def test_v6_complete_hindi_city_route_goes_directly_to_rate_lookup(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")

        state.apply_deterministic_answers(
            "दिल्ली से बैंगलोर",
            turn_id="complete-route",
        )

        self.assertEqual(state.value("pickup_location"), "Delhi")
        self.assertEqual(state.value("delivery_location"), "Bengaluru")
        self.assertEqual(state.pending_field(), "business_name")
        self.assertEqual(state.pricing_mode(), "pending")

    def test_v6_retains_d2c_volunteered_before_business_name(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")

        state.apply_deterministic_answers(
            "Mera business D2C hai aur main Delhi se ship karta hun",
            turn_id="multi-fact",
            previous_agent_text="Aapke business ka naam kya hai?",
        )

        self.assertEqual(state.value("business_type"), "D2C")
        self.assertEqual(state.value("pickup_location"), "Delhi")
        self.assertEqual(state.pending_field(), "business_name")

    def test_weight_answer_never_becomes_current_shipping_rate(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.seed_context(
            {
                "business_name": "Raj Enterprises",
                "business_type": "D2C",
                "business_platform": "Website",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
            }
        )

        state.apply_deterministic_answers(
            "500 g",
            turn_id="rate-basis-weight",
            previous_agent_text="Aapka current rate kis weight ke liye hai?",
        )

        self.assertFalse(state.is_handled("current_shipping_rate"))
        self.assertEqual(state.value("dead_weight"), 0.5)

    def test_explicit_flat_asr_request_supersedes_close_lock(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.seed_context(
            {
                "business_name": "North Star",
                "business_type": "D2C",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
            }
        )
        state.move_forward_decision = "Yes"
        state.onboarding_link_presented = True
        self.assertTrue(state.pricing_close_locked())

        state.apply_deterministic_answers("Play it rate toh batao", turn_id="flat-after-close")

        self.assertEqual(state.requested_rate_type, "Flat")
        self.assertTrue(state.flat_catalog_due())
        self.assertFalse(state.pricing_close_locked())

        state.apply_deterministic_answers(
            "Flat zonal rates bhi batao",
            turn_id="flat-zonal-after-close",
        )
        self.assertEqual(state.requested_rate_type, "Flat Zonal")
        self.assertTrue(state.flat_zonal_catalog_due())
        self.assertFalse(state.pricing_close_locked())

    def test_v7_latest_call_catalog_asr_variants_use_matching_kb_paths(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates check karne hain", turn_id="intent")
        state.seed_context(
            {
                "business_name": "North Star",
                "business_type": "D2C",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "monthly_shipments": 2000,
            }
        )
        state.polite_close_due = True
        state.polite_close_presented = True

        state.apply_deterministic_answers(
            "Plater available hai?",
            turn_id="flat-asr",
            previous_agent_text="Theek hai, thank you for your time.",
        )

        self.assertEqual(state.requested_rate_type, "Flat")
        self.assertTrue(state.flat_catalog_due())
        self.assertFalse(state.polite_close_due)
        self.assertFalse(state.polite_close_presented)
        self.assertIn("get_shipkia_flat_rates", state.guidance())

        state.mark_flat_catalog_presented()
        state.apply_deterministic_answers(
            "of flight floral rates",
            turn_id="flat-zonal-asr",
            previous_agent_text="E-Kart Surface Flat catalog share kiya hai.",
        )

        self.assertEqual(state.requested_rate_type, "Flat Zonal")
        self.assertTrue(state.flat_zonal_catalog_due())
        self.assertIn("get_shipkia_flat_zonal_rates", state.guidance())

    def test_v7_early_route_waits_for_short_sales_discovery(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers(
            "Delhi to Kerala ke rates check karne hain",
            turn_id="intent-route",
        )

        self.assertEqual(state.pending_field(), "business_name")
        self.assertEqual(state.pricing_mode(), "pending")
        self.assertEqual(
            state.next_route_for_lookup(),
            {"pickup_location": "Delhi", "delivery_location": "Kerala"},
        )

        state.seed_context(
            {
                "business_name": "North Star",
                "business_type": "D2C",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_problem": "RTO follow-up",
            }
        )

        self.assertEqual(state.pending_field(), "")
        self.assertEqual(state.pricing_mode(), "route_starting_pending")

    def test_v7_unasked_pending_question_does_not_consume_retry(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates check karne hain", turn_id="intent")

        for index in range(2):
            state.apply_classifier_result(
                {"turn_disposition": "unrelated", "decisions": []},
                customer_text="haan theek hai",
                turn_id=f"wrong-question-{index}",
                pending_field_at_turn_start="business_name",
                pending_question_was_asked=False,
            )

        self.assertEqual(state.pending_field(), "business_name")
        self.assertNotIn("business_name", state.pending_retry_counts)
        self.assertNotIn("business_name", state.skipped_after_retry_fields)

    def test_v7_chip_rocket_asr_is_saved_as_shiprocket_aggregator(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates check karne hain", turn_id="intent")
        state.seed_context({"business_name": "North Star", "business_type": "D2C"})

        state.apply_deterministic_answers(
            "Abhi mein chip Rocket use kar raha hun.",
            turn_id="provider",
            previous_agent_text="Aap abhi kaunsa courier ya shipping provider use karte hain?",
        )

        self.assertEqual(state.value("current_shipping_arrangement"), "Shipping Aggregator")
        self.assertEqual(state.value("current_provider_name"), "Shiprocket")

    def test_v6_information_call_pivots_to_rates_from_natural_acceptance(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("haan bataiye", turn_id="consent")
        state.apply_deterministic_answers(
            "ShipKia ki kya kya services hain?",
            turn_id="services",
            previous_agent_text="Aapki shipping priority kya hai?",
        )
        self.assertEqual(state.value("assistance_intent"), "Information")

        state.apply_deterministic_answers(
            "haan ji bataiye",
            turn_id="accept-rates",
            previous_agent_text="Kya aap abhi rates ke baare mein jaanna chahte hain?",
        )

        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(state.requested_rate_type, "Normal")
        self.assertEqual(state.pending_field(), "business_name")

    def test_v6_zone_request_pivots_information_call_to_pricing(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.seed_context(
            {
                "conversation_consent": "Accepted",
                "assistance_intent": "Information",
                "business_name": "Evergreen",
                "business_type": "D2C",
                "business_platform": "Website",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_shipping_rate": "Not Shared",
                "current_problem": "RTO/NDR",
            }
        )
        state.optional_ended_by = "business_name"

        state.apply_deterministic_answers("Pahle zone D ke rates batao", turn_id="zone-d-rate")

        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(state.value("zone"), "D")
        self.assertEqual(state.pricing_mode(), "zone_starting")

    def test_v6_plane_rate_asr_selects_flat_catalog_without_shipment_inputs(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.seed_context(
            {
                "conversation_consent": "Accepted",
                "assistance_intent": "Information",
            }
        )
        state.optional_ended_by = "business_name"

        state.apply_deterministic_answers("plane ka rate bata do", turn_id="flat-asr")

        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(state.requested_rate_type, "Flat")
        self.assertTrue(state.flat_catalog_due())
        self.assertEqual(state.pending_field(), "")

    def test_v6_latest_call_whatsapp_status_asr_selects_flat_catalog(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.seed_context(
            {
                "conversation_consent": "Accepted",
                "assistance_intent": "Rates",
                "business_name": "Acme",
                "business_type": "D2C",
                "business_platform": "Own website",
                "current_shipping_arrangement": "Direct Courier",
                "current_provider_name": "Delhivery",
                "current_shipping_rate": 35,
                "current_problem": "Support",
                "pickup_location": "Delhi",
                "delivery_location": "Bangalore",
                "monthly_shipments": 2000,
            }
        )
        state.apply_deterministic_answers(
            "Zone D ke rates bata dijiye", turn_id="zone-d"
        )
        state.mark_starting_rate_presented()

        state.apply_deterministic_answers(
            "WhatsApp status available hain aap ke pass?", turn_id="flat-asr"
        )

        self.assertEqual(state.requested_rate_type, "Flat")
        self.assertTrue(state.flat_catalog_due())
        self.assertEqual(state.pricing_mode(), "flat_catalog")
        self.assertIn("get_shipkia_flat_rates", state.guidance())
        self.assertNotIn("shipment weight", state.guidance())

        state.apply_deterministic_answers("pkg, 1500 g", turn_id="weight")

        self.assertEqual(state.value("dead_weight"), 1.5)
        self.assertEqual(state.requested_rate_type, "Flat")
        self.assertTrue(state.flat_catalog_due())
        self.assertEqual(state.pricing_mode(), "flat_catalog")
        self.assertIn("get_shipkia_flat_rates", state.guidance())

    def test_v6_latest_call_flat_jo_after_flat_catalog_selects_flat_zonal(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.seed_context(
            {
                "conversation_consent": "Accepted",
                "assistance_intent": "Rates",
                "monthly_shipments": 2000,
            }
        )
        state.optional_ended_by = "business_name"
        state.apply_deterministic_answers("flat rates batao", turn_id="flat")
        state.mark_flat_catalog_presented()
        state.mark_pricing_verified("get_shipkia_flat_rates")

        state.apply_deterministic_answers(
            "Flat jo rate kya hai?", turn_id="flat-zonal-asr"
        )

        self.assertEqual(state.requested_rate_type, "Flat Zonal")
        self.assertTrue(state.flat_zonal_catalog_due())
        self.assertEqual(state.pricing_mode(), "flat_zonal_catalog")
        self.assertIn("get_shipkia_flat_zonal_rates", state.guidance())
        self.assertFalse(state.move_forward_question_due)

    def test_v6_latest_call_explicit_route_question_beats_stale_rate_pending(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("rates bata dijiye", turn_id="intent")
        state.seed_context(
            {
                "business_name": "Harsh Enterprises",
                "business_type": "D2C",
                "business_platform": "Custom website",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_problem": "Support issue",
            }
        )
        self.assertEqual(state.pending_field(), "pickup_location")

        state.apply_deterministic_answers(
            "Meri shipment hoti hai Delhi to Bangalore.",
            turn_id="route",
            previous_agent_text="Kahan se kahan tak aapki shipments hoti hain?",
        )

        self.assertEqual(state.value("pickup_location"), "Delhi")
        self.assertEqual(state.value("delivery_location"), "Bengaluru")
        self.assertFalse(state.is_handled("current_rate_basis"))
        self.assertEqual(state.pending_field(), "")
        self.assertEqual(state.pricing_mode(), "route_starting_pending")

    def test_prepaid_and_cod_in_same_answer_is_both(self):
        state = GatedConversationState()
        state.apply_deterministic_answers(
            "Prepaid bata do, COD bhi bata do, dono bata do",
            turn_id="both-payment-modes",
        )

        self.assertEqual(state.value("payment_type"), "Both")

    def test_v6_pan_india_quotes_zone_a_then_resumes_business_discovery(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates bataiye", turn_id="intent")
        state.seed_context({"business_name": "Acme"})
        self.assertEqual(state.pending_field(), "business_type")

        state.apply_deterministic_answers("delivery Pan India hoti hai", turn_id="pan-india")

        self.assertTrue(state.pan_india_requested)
        self.assertEqual(state.pending_field(), "")
        self.assertEqual(state.pricing_mode(), "route_starting_pending")
        self.assertEqual(state.next_route_for_lookup(), {"pan_india": True})
        self.assertIn("lookup_shipkia_route_rate", state.guidance())
        self.assertFalse(state.verified_rate_presented())
        self.assertFalse(state.move_forward_question_due)

        state.mark_pricing_verified("lookup_shipkia_route_rate")
        self.assertEqual(state.pending_field(), "business_type")
        state.seed_context(
            {
                "business_type": "D2C",
                "current_shipping_arrangement": "Own Arrangement",
            }
        )
        self.assertEqual(state.pending_field(), "monthly_shipments")

        state.apply_deterministic_answers(
            "1000",
            turn_id="volume",
            previous_agent_text="Aapka monthly shipment volume kitna rehta hai?",
        )
        self.assertEqual(state.pending_field(), "")

    def test_v6_latest_call_dates_asr_selects_rates_and_cleans_discovery_answers(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("Okay", turn_id="consent")
        state.apply_deterministic_answers(
            "I want to know about the dates.",
            turn_id="dates-asr",
            previous_agent_text="What's your main priority with shipping right now?",
        )

        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(state.pending_field(), "business_name")

        state.apply_deterministic_answers(
            "My business name is Harsh Enterprises.",
            turn_id="business-name",
            previous_agent_text="What's your business or brand name?",
        )
        self.assertEqual(state.value("business_name"), "Harsh Enterprises")
        self.assertEqual(state.pending_field(), "business_type")

        state.seed_context({"business_type": "D2C"})
        state.apply_deterministic_answers(
            "Hello.",
            turn_id="platform-noise",
            previous_agent_text="Which platform do you receive orders from?",
        )
        self.assertFalse(state.is_handled("business_platform"))

        state.apply_deterministic_answers(
            "basically it is D to C",
            turn_id="type-not-platform",
            previous_agent_text="Do you use Shopify or another platform?",
        )
        self.assertFalse(state.is_handled("business_platform"))

    def test_v6_latest_call_usb_then_rest_asr_pivots_information_to_rates(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers(
            "\u0938\u093f\u0902\u092a\u0932\u0940 \u0915\u0947 \u092f\u0942\u090f\u0938\u092c\u0940 \u0915\u093e \u0915\u094d\u092f\u093e \u0939\u0948?",
            turn_id="usb-usp-asr",
            previous_agent_text=(
                "Aap rates check karna chahenge, onboarding mein help chahiye, "
                "ya ShipKia ke baare mein kuch aur jaanna hai?"
            ),
        )

        self.assertTrue(state.last_usp_query)
        self.assertEqual(state.value("assistance_intent"), "Information")

        state.apply_deterministic_answers(
            "\u0920\u0940\u0915 \u0939\u0948, \u0930\u0947\u0938\u094d\u091f \u0915\u0947 \u092c\u093e\u0930\u0947 \u092e\u0947\u0902 \u092c\u0924\u093e\u0907\u090f\u0964",
            turn_id="rest-rate-asr",
            previous_agent_text="Kya aap rates ke baare mein jaanna chahenge?",
        )

        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(state.requested_rate_type, "Normal")
        self.assertEqual(state.pending_field(), "business_name")

    def test_v6_latest_call_complaint_is_not_saved_as_business_platform(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates bataiye", turn_id="intent")
        state.seed_context(
            {
                "business_name": "Acme",
                "business_type": "D2C",
            }
        )
        self.assertEqual(state.pending_field(), "current_shipping_arrangement")

        state.apply_deterministic_answers(
            "\u090a\u092a\u0930 \u0915\u0947 question \u0915\u093e \u0924\u0941\u092e\u0928\u0947 \u0917\u0932\u0924 answer \u0926\u093f\u092f\u093e",
            turn_id="complaint",
            previous_agent_text="Aapke orders kahan se aate hain?",
        )

        self.assertFalse(state.is_handled("business_platform"))
        self.assertEqual(state.pending_field(), "current_shipping_arrangement")

        state.apply_deterministic_answers(
            "Meri khud ki website hai",
            turn_id="real-platform",
            previous_agent_text="Aap Shopify, marketplace ya website use karte hain?",
        )
        self.assertEqual(state.value("business_platform"), "Meri khud ki website hai")

    def test_v6_latest_call_slad_zonal_asr_never_opens_cod_questions(self):
        state = self._v6_ready_for_ending()
        state.flat_catalog_presented = True
        state.flat_catalog_delivery_due = True
        state.apply_deterministic_answers(
            "\u0938\u094d\u0932\u093e\u0921 \u091c\u094b\u0928\u0932 \u0930\u0947\u091f \u092c\u0924\u093e\u0913\u0917\u0947 \u090f\u0915 \u092c\u093e\u0930?",
            turn_id="slad-zonal-asr",
        )

        self.assertEqual(state.requested_rate_type, "Flat Zonal")
        self.assertTrue(state.flat_zonal_catalog_due())
        self.assertFalse(state.flat_catalog_delivery_due)
        self.assertFalse(state.is_handled("payment_type"))
        self.assertFalse(state.is_handled("order_value"))
        self.assertNotEqual(state.pending_field(), "order_value")
        self.assertIn("get_shipkia_flat_zonal_rates", state.guidance())

    def test_v6_cod_flat_zonal_request_asks_only_amount_then_unlocks_catalog(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers(
            "COD Flat-Zonal rates batao",
            turn_id="cod-flat-zonal",
        )

        self.assertTrue(state.cod_rate_requested)
        self.assertEqual(state.value("payment_type"), "COD")
        self.assertEqual(state.pending_field(), "order_value")
        self.assertIn("COD order value", state.guidance())
        self.assertNotIn("business or brand name", state.guidance())

        state.apply_deterministic_answers(
            "2000",
            turn_id="cod-amount",
            previous_agent_text="Aapka COD order value kitna hai?",
        )

        self.assertEqual(state.value("order_value"), 2000)
        self.assertEqual(state.pending_field(), "")
        self.assertTrue(state.flat_zonal_catalog_due())
        self.assertIn("get_shipkia_flat_zonal_rates", state.guidance())

    def test_v5_information_question_keeps_legacy_choice_gate_open(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers(
            "What is ShipKia?",
            turn_id="information",
        )

        self.assertTrue(state.last_usp_query)
        self.assertFalse(state.is_handled("assistance_intent"))
        self.assertEqual(state.pending_field(), "assistance_intent")

    def test_v6_volume_answer_never_becomes_business_name(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.mark_route_zone_verified("C", starting_presented=True)
        state.mark_pricing_verified("lookup_shipkia_route_rate")
        self.assertEqual(state.pending_field(), "business_name")

        state.apply_deterministic_answers(
            "around 2,000",
            turn_id="latest-volume",
            previous_agent_text="Aapki monthly shipments kitni hoti hain?",
        )
        semantic = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    {
                        "field": "business_name",
                        "disposition": "answered",
                        "value": "2,000",
                        "evidence": "around 2,000",
                        "confidence": 1.0,
                    }
                ],
            },
            customer_text="around 2,000",
            turn_id="latest-volume",
            pending_field_at_turn_start="business_name",
        )

        self.assertEqual(state.value("monthly_shipments"), 2000)
        self.assertFalse(state.is_handled("business_name"))
        self.assertEqual(semantic, [])
        self.assertFalse(state.move_forward_question_due)

    def test_v6_business_type_acronym_never_becomes_business_name(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")

        applied = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    decision("business_name", "G2C", "Mera business G2C hai"),
                    decision("business_type", "G2C", "Mera business G2C hai"),
                ],
            },
            customer_text="Mera business G2C hai",
            turn_id="business-type-before-name",
            pending_field_at_turn_start="business_name",
        )

        self.assertEqual(applied, [])
        self.assertFalse(state.is_handled("business_name"))
        self.assertEqual(state.pending_field(), "business_name")

    def test_v6_last_call_t2c_classifier_value_never_becomes_business_name(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("rates bata dijiye", turn_id="intent")

        applied = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [decision("business_name", "t2c", "uska t2c")],
            },
            customer_text="Mera business hai, uska t2c.",
            turn_id="asr-business-code",
            pending_field_at_turn_start="business_name",
        )

        self.assertEqual(applied, [])
        self.assertFalse(state.is_handled("business_name"))
        self.assertEqual(state.pending_field(), "business_name")

    def test_v6_latest_call_payment_and_weight_cannot_restamp_service(self):
        state = GatedConversationState()
        state.seed_context({"service": "E-Kart SURFACE"})

        for index, customer_text in enumerate(("aap mujhe prepaid bata dijiye", "500 gram")):
            applied = state.apply_classifier_result(
                {
                    "turn_disposition": "answered",
                    "decisions": [
                        decision("service", "E-Kart SURFACE", customer_text)
                    ],
                },
                customer_text=customer_text,
                turn_id=f"unrelated-service-{index}",
            )
            self.assertEqual(applied, [])

        service_updates = [
            item
            for item in state.transitions
            if item.get("field") == "service" and item.get("source") == "classifier"
        ]
        self.assertEqual(service_updates, [])
        self.assertEqual(state.value("service"), "E-Kart SURFACE")

    def test_v6_explicit_zone_rate_request_reopens_pricing_after_close(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.optional_ended_by = "business_name"
        state.move_forward_decision = "Yes"
        state.onboarding_link_presented = True
        self.assertTrue(state.pricing_close_locked())

        state.apply_deterministic_answers(
            "Zone D ke rates batao",
            turn_id="zone-after-close",
        )

        self.assertEqual(state.value("zone"), "D")
        self.assertTrue(state.starting_rate_due())
        self.assertFalse(state.pricing_close_locked())

    def _v6_ready_for_ending(self) -> GatedConversationState:
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.optional_ended_by = "business_name"
        state.authorize_rate_result(
            {"status": "success", "response_type": "zone_starting", "amount": 31.15}
        )
        state.mark_route_zone_verified("C", starting_presented=True)
        state.mark_pricing_verified("get_shipkia_starting_rate")
        state.apply_deterministic_answers(
            "2000",
            turn_id="volume",
            previous_agent_text="Aapki monthly shipments kitni hain?",
        )
        state.mark_anything_else_question_presented()
        state.apply_deterministic_answers(
            "nahi",
            turn_id="nothing-else",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
        )
        self.assertTrue(state.move_forward_question_due)
        return state

    def test_v6_rate_rejection_overrides_move_forward_close(self):
        state = self._v6_ready_for_ending()

        state.apply_deterministic_answers(
            "Nahi, mujhe rates pasand nahi aaye.",
            turn_id="rates-rejected",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
        )

        self.assertEqual(state.rate_sentiment, "Unsuitable")
        self.assertTrue(state.unsatisfied_problem_due)
        self.assertFalse(state.move_forward_question_due)
        self.assertFalse(state.polite_close_due)
        self.assertFalse(state.onboarding_link_due)
        self.assertIn("specific zone", state.guidance())

    def test_v6_possible_dropped_rate_negation_requires_clarification(self):
        state = self._v6_ready_for_ending()

        state.apply_deterministic_answers(
            "Nahi, abhi nahi. Mujhe rates pasand hain.",
            turn_id="ambiguous-negation",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
        )

        self.assertEqual(state.rate_sentiment, "Unclear")
        self.assertEqual(state.move_forward_decision, "Later")
        self.assertTrue(state.rate_sentiment_clarification_due)
        self.assertFalse(state.polite_close_due)
        self.assertIn("suitable lage, ya suitable nahi lage", state.guidance())

        state.apply_deterministic_answers(
            "Rates pasand nahi aaye.",
            turn_id="sentiment-clarified",
            previous_agent_text=(
                "Jo rates share hue woh aapko suitable lage, ya suitable nahi lage?"
            ),
        )
        self.assertEqual(state.rate_sentiment, "Unsuitable")
        self.assertFalse(state.rate_sentiment_clarification_due)
        self.assertTrue(state.unsatisfied_problem_due)

    def test_v6_positive_sentiment_after_dropped_negation_gets_deferred_close(self):
        state = self._v6_ready_for_ending()
        state.apply_deterministic_answers(
            "Nahi, abhi nahi. Mujhe rates pasand hain.",
            turn_id="ambiguous-negation",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
        )

        state.apply_deterministic_answers(
            "Haan, rates suitable hain.",
            turn_id="sentiment-positive",
            previous_agent_text=(
                "Jo rates share hue woh aapko suitable lage, ya suitable nahi lage?"
            ),
        )

        self.assertEqual(state.rate_sentiment, "Suitable")
        self.assertTrue(state.deferred_close_due)
        self.assertIn("pressure-free close", state.guidance())

    def test_v6_specific_price_objection_offers_review_before_close(self):
        state = self._v6_ready_for_ending()

        state.apply_deterministic_answers(
            "Zone C ka rate zyada hai.",
            turn_id="specific-objection",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
        )

        self.assertTrue(state.unsatisfied_resolution_due)
        self.assertFalse(state.unsatisfied_problem_due)
        self.assertFalse(state.polite_close_due)
        self.assertIn("pricing team", state.guidance())

    def test_v6_latest_call_clear_proceed_reverses_prior_rate_rejection(self):
        state = self._v6_ready_for_ending()
        state.apply_deterministic_answers(
            "Nahi, main nahi judna chahta. Rate zyada lag raha hai.",
            turn_id="rejected",
            previous_agent_text="Kya aap ShipKia ke saath judna chahenge?",
        )
        state.polite_close_due = True
        state.polite_close_presented = True

        state.apply_deterministic_answers(
            "Main aapke saath aage badhna chahta hoon.",
            turn_id="reversed",
            previous_agent_text="Call karne ke liye dhanyavaad.",
        )

        self.assertEqual(state.move_forward_decision, "Yes")
        self.assertTrue(state.onboarding_link_due)
        self.assertFalse(state.polite_close_due)
        self.assertFalse(state.polite_close_presented)
        self.assertFalse(state.unsatisfied_problem_due)
        self.assertFalse(state.unsatisfied_resolution_due)
        self.assertIn("WhatsApp", state.guidance())

    def test_v6_latest_call_named_provider_rate_reopens_objection_close(self):
        state = self._v6_ready_for_ending()
        state.verified_starting_options = [
            {"courier": "Bluedart", "service": "Surface", "amount": 42.5},
            {"courier": "Delhivery", "service": "Surface", "amount": 39.0},
        ]
        state.available_courier_partners = ["Bluedart", "Delhivery"]
        state.apply_deterministic_answers(
            "Rate zyada lag raha hai.", turn_id="objection"
        )
        self.assertTrue(state.unsatisfied_resolution_due)

        state.apply_deterministic_answers(
            "Aap mujhe Blue Dart ke rate bata dijiye.",
            turn_id="bluedart-rate",
        )

        self.assertEqual(state.requested_provider_rate_name, "Blue Dart")
        self.assertTrue(state.provider_rates_answer_due)
        self.assertFalse(state.unsatisfied_resolution_due)
        self.assertFalse(state.pricing_close_locked())
        guidance = state.guidance()
        self.assertIn("get_shipkia_starting_rate", guidance)
        self.assertNotIn("42.5", guidance)
        self.assertNotIn('"amount":39.0', guidance)

    def test_v7_contextual_x_15_20_followup_requests_xpressbees_from_kb(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates batao", turn_id="intent")
        state.seed_context({"zone": "C"})

        state.apply_deterministic_answers(
            "aur X 15 20 ke",
            turn_id="xpressbees-followup",
            previous_agent_text=(
                "Zone C ke liye Bluedart Surface Express ka starting rate bataya hai."
            ),
        )

        self.assertEqual(state.provider_rate_scope, "Named")
        self.assertEqual(state.requested_provider_rate_name, "Xpressbees")
        self.assertTrue(state.named_courier_rate_due())
        self.assertIn("get_shipkia_starting_rate", state.guidance())

    def test_v7_all_available_rates_reopens_unfiltered_kb_lookup(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates batao", turn_id="intent")
        state.seed_context({"zone": "C"})
        state.verified_starting_options = [
            {"courier": "Bluedart", "service": "Surface", "amount": 90.68}
        ]
        state.available_courier_partners = ["Bluedart"]

        state.apply_deterministic_answers(
            "Jo jo available hain, sab bata do.",
            turn_id="all-rates",
            previous_agent_text="Aap kisi specific courier ka rate jaanna chahte hain?",
        )

        self.assertEqual(state.provider_rate_scope, "All")
        self.assertEqual(state.requested_provider_rate_name, "")
        self.assertEqual(state.verified_starting_options, [])
        self.assertTrue(state.all_courier_rates_due())
        self.assertIn("no courier filter", state.guidance())

    def test_v7_percentage_asr_noise_is_not_saved_as_business_name(self):
        state = V6ConversationState()
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates batao", turn_id="intent")

        state.apply_deterministic_answers(
            "And it is a 90, 100%.",
            turn_id="business-noise",
            previous_agent_text="Aapke business ya brand ka naam kya hai?",
        )
        semantic = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    decision(
                        "business_name",
                        "And it is a 90, 100%",
                        "And it is a 90, 100%",
                    )
                ],
            },
            customer_text="And it is a 90, 100%.",
            turn_id="business-noise",
            pending_field_at_turn_start="business_name",
        )

        self.assertFalse(state.is_handled("business_name"))
        self.assertEqual(semantic, [])

    def test_v7_newly_launched_business_has_no_current_provider(self):
        state = V6ConversationState()
        state.seed_context(
            {
                "business_name": "North Star",
                "business_type": "D2C",
                "business_platform": "Website",
            }
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates batao", turn_id="intent")

        state.apply_deterministic_answers(
            "Abhi main is ka launch kiya.",
            turn_id="new-launch",
            previous_agent_text="Aap abhi kaunsa courier ya shipping provider use karte hain?",
        )

        self.assertEqual(
            state.value("current_shipping_arrangement"),
            "No Current Arrangement",
        )
        self.assertFalse(state.is_handled("current_provider_name"))

    def test_v6_pricing_review_yes_gets_one_consented_review_close(self):
        state = self._v6_ready_for_ending()
        state.apply_deterministic_answers(
            "Zone C ka rate zyada hai.",
            turn_id="specific-objection",
        )

        state.apply_deterministic_answers(
            "haan",
            turn_id="review-yes",
            previous_agent_text=(
                "Kya aap chahenge ki pricing team aapke shipment volume ke basis par isko review kare?"
            ),
        )

        self.assertEqual(state.pricing_review_decision, "Yes")
        self.assertTrue(state.better_plan_close_due)
        self.assertFalse(state.onboarding_link_due)
        self.assertIn("accepted a pricing-team review", state.guidance())

    def test_v6_pricing_review_no_gets_polite_close_without_promise(self):
        state = self._v6_ready_for_ending()
        state.apply_deterministic_answers(
            "Flat rate mehenga hai.",
            turn_id="specific-objection",
        )

        state.apply_deterministic_answers(
            "nahi",
            turn_id="review-no",
            previous_agent_text=(
                "Kya aap chahenge ki pricing team aapke shipment volume ke basis par isko review kare?"
            ),
        )

        self.assertEqual(state.pricing_review_decision, "No")
        self.assertTrue(state.polite_close_due)
        self.assertFalse(state.better_plan_close_due)

    def test_v6_not_now_is_distinct_from_rejection(self):
        state = self._v6_ready_for_ending()

        state.apply_deterministic_answers(
            "Abhi nahi.",
            turn_id="later",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
        )

        self.assertEqual(state.move_forward_decision, "Later")
        self.assertTrue(state.deferred_close_due)
        self.assertTrue(state.polite_close_due)
        self.assertIn("promise no callback", state.guidance())

    def test_v6_clear_yes_and_no_keep_distinct_terminal_actions(self):
        yes_state = self._v6_ready_for_ending()
        yes_state.apply_deterministic_answers(
            "haan",
            turn_id="yes",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
        )
        self.assertTrue(yes_state.onboarding_link_due)
        self.assertFalse(yes_state.polite_close_due)

        no_state = self._v6_ready_for_ending()
        no_state.apply_deterministic_answers(
            "nahi",
            turn_id="no",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
        )
        self.assertEqual(no_state.move_forward_decision, "No")
        self.assertTrue(no_state.onboarding_decline_reason_due)
        self.assertFalse(no_state.polite_close_due)
        self.assertFalse(no_state.onboarding_link_due)

        no_state.apply_deterministic_answers(
            "Abhi rate budget se zyada hai.",
            turn_id="no-reason",
            previous_agent_text="Abhi onboarding na karne ka main reason kya hai?",
        )
        self.assertFalse(no_state.onboarding_decline_reason_due)
        self.assertTrue(no_state.better_plan_close_due)
        self.assertIn("rate budget se zyada", no_state.guidance())

    def test_v7_no_thank_you_after_rate_followup_advances_to_onboarding_question(self):
        state = self._v6_ready_for_ending()
        state.move_forward_question_due = False
        state.post_rate_followup_active = True

        transitions = state.apply_deterministic_answers(
            "Nahin. Thank you.",
            turn_id="followup-done",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
        )

        self.assertFalse(state.polite_close_due)
        self.assertTrue(state.move_forward_question_due)
        self.assertTrue(any(item["event"] == "post_information_completed" for item in transitions))
        self.assertIn("aage badhna chahte", state.guidance())

    def test_v7_bare_thank_you_after_rate_followup_does_not_speak_early_farewell(self):
        state = self._v6_ready_for_ending()
        state.move_forward_question_due = False
        state.post_rate_followup_active = True

        transitions = state.apply_deterministic_answers(
            "thank you",
            turn_id="followup-done",
            previous_agent_text="Kya aap kisi provider ka rate jaanna chahte hain?",
        )

        self.assertFalse(state.polite_close_due)
        self.assertTrue(state.move_forward_question_due)
        self.assertTrue(any(item["event"] == "post_information_completed" for item in transitions))

    def test_v6_positive_rate_comment_never_implies_onboarding(self):
        state = self._v6_ready_for_ending()

        state.apply_deterministic_answers(
            "Rates mujhe pasand hain.",
            turn_id="positive-only",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
        )

        self.assertEqual(state.rate_sentiment, "Suitable")
        self.assertTrue(state.move_forward_question_due)
        self.assertFalse(state.onboarding_link_due)

    def test_v6_conditional_lower_rate_request_enters_pricing_review(self):
        state = self._v6_ready_for_ending()

        state.apply_deterministic_answers(
            "Rate kam ho toh main proceed karunga.",
            turn_id="conditional",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
        )

        self.assertEqual(state.rate_sentiment, "Unsuitable")
        self.assertTrue(state.unsatisfied_resolution_due)
        self.assertFalse(state.onboarding_link_due)

    def test_v6_followup_rates_do_not_repeat_close_until_customer_is_done(self):
        state = self._v6_ready_for_ending()
        state.move_forward_question_due = False
        state.anything_else_question_due = True
        state.anything_else_question_presented = True
        state.anything_else_checkpoint_consumed = False

        state.apply_deterministic_answers(
            "Zone D ke rates batao",
            turn_id="zone-followup",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
        )
        self.assertTrue(state.post_rate_followup_active)
        state.mark_pricing_verified("get_shipkia_starting_rate")
        self.assertFalse(state.anything_else_question_due)
        self.assertFalse(state.move_forward_question_due)

        state.apply_deterministic_answers(
            "Flat zonal rates bhi batao",
            turn_id="flat-zonal-followup",
        )
        state.mark_flat_zonal_catalog_presented()
        state.mark_pricing_verified("get_shipkia_flat_zonal_rates")
        self.assertTrue(state.post_rate_followup_active)
        self.assertFalse(state.move_forward_question_due)

        state.apply_deterministic_answers("bas itna hi", turn_id="done")
        self.assertFalse(state.post_rate_followup_active)
        self.assertTrue(state.move_forward_question_due)

    def test_v6_no_thank_you_gets_terminal_polite_close(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")

        transitions = state.apply_deterministic_answers(
            "No, thank you.",
            turn_id="customer-close",
            previous_agent_text="Aap abhi kaunsa courier use karte hain?",
        )

        self.assertEqual(transitions[0]["event"], "polite_close_requested")
        self.assertTrue(state.polite_close_due)
        self.assertEqual(state.pending_field(), "")
        self.assertIn("Have a good day", state.guidance())
        self.assertFalse(state.is_handled("current_shipping_arrangement"))

    def test_v6_last_call_replay_requires_verified_delhi_bengaluru_rate_before_volume(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers(
            "Delhi se Bangalore ke rates bataiye",
            turn_id="intent-route",
        )
        state.apply_deterministic_answers(
            "Harsh Enterprises",
            turn_id="business-name",
            previous_agent_text="Aapke business ya brand ka naam kya hai?",
        )
        state.apply_deterministic_answers(
            "Mera hai direct to customer",
            turn_id="business-type",
            previous_agent_text="Aapka business B2B, B2C ya D2C hai?",
        )
        state.apply_deterministic_answers(
            "Meri website hai",
            turn_id="platform",
            previous_agent_text="Aap orders kis platform se lete hain?",
        )
        state.apply_deterministic_answers(
            "Main Shriprakat use kar raha hun",
            turn_id="provider",
            previous_agent_text="Abhi kaunsa courier ya aggregator use karte hain?",
        )

        self.assertEqual(state.value("business_type"), "D2C")
        self.assertEqual(state.value("current_shipping_arrangement"), "Shipping Aggregator")
        self.assertEqual(state.value("current_provider_name"), "Shiprocket")
        state.apply_deterministic_answers(
            "Meri main problem high rates hai",
            turn_id="problem",
            previous_agent_text="Shipping mein abhi sabse badi problem kya aa rahi hai?",
        )
        self.assertEqual(state.value("pickup_location"), "Delhi")
        self.assertEqual(state.value("delivery_location"), "Bengaluru")
        self.assertEqual(state.pending_field(), "")
        self.assertEqual(state.pricing_mode(), "route_starting_pending")

        # Volume cannot advance before the KB-backed route rate is presented.
        state.apply_deterministic_answers(
            "1000",
            turn_id="premature-volume",
            previous_agent_text="Aapki monthly shipments kitni hain?",
        )
        self.assertFalse(state.is_handled("monthly_shipments"))
        self.assertEqual(state.pending_field(), "")
        self.assertEqual(state.pricing_mode(), "route_starting_pending")
        self.assertIn("lookup_shipkia_route_rate", state.guidance())
        self.assertFalse(state.verified_rate_presented())

        state.mark_route_zone_verified("C", starting_presented=True)
        state.mark_pricing_verified("lookup_shipkia_route_rate")
        self.assertTrue(state.verified_rate_presented())
        self.assertEqual(state.pending_field(), "monthly_shipments")

        state.apply_deterministic_answers(
            "1000",
            turn_id="valid-volume",
            previous_agent_text="Aapki monthly shipments kitni hain?",
        )
        self.assertEqual(state.value("monthly_shipments"), 1000)

    def test_v6_weight_answer_never_overwrites_pending_monthly_shipments(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.seed_context(
            {
                "business_name": "Evergreen",
                "business_type": "D2C",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_problem": "High rates",
                "pickup_location": "Delhi",
                "delivery_location": "Bengaluru",
            }
        )
        state.mark_route_zone_verified("C", starting_presented=True)
        state.mark_pricing_verified("lookup_shipkia_route_rate")
        self.assertEqual(state.pending_field(), "monthly_shipments")

        state.apply_deterministic_answers(
            "around 500 g",
            turn_id="weight-not-volume",
            previous_agent_text="Aapki approximate monthly shipments kitni hain?",
        )

        self.assertFalse(state.is_handled("monthly_shipments"))
        self.assertEqual(state.value("dead_weight"), 0.5)

    def test_v6_greeting_is_never_saved_as_business_name(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")

        state.apply_deterministic_answers(
            "Hello.",
            turn_id="greeting",
            previous_agent_text="Aapke business ya brand ka naam kya hai?",
        )

        self.assertFalse(state.is_handled("business_name"))
        self.assertEqual(state.pending_field(), "business_name")

    def test_v6_latest_call_platform_cannot_also_become_business_name(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        self.assertEqual(state.pending_field(), "business_name")

        state.apply_deterministic_answers(
            "Haan, mere orders WooCommerce se aate hain",
            turn_id="platform-not-name",
            previous_agent_text="Aapke orders Shopify, marketplace ya website se aate hain?",
        )
        applied = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    decision(
                        "business_name",
                        "WooCommerce",
                        "WooCommerce",
                    )
                ],
            },
            customer_text="Haan, mere orders WooCommerce se aate hain",
            turn_id="platform-not-name",
            pending_field_at_turn_start="business_name",
        )

        self.assertTrue(state.is_handled("business_platform"))
        self.assertFalse(state.is_handled("business_name"))
        self.assertEqual(state.pending_field(), "business_name")
        self.assertEqual(applied, [])

    def test_v6_latest_call_remembers_explicit_route_while_current_rate_pending(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.seed_context(
            {
                "business_name": "Harsh Enterprises",
                "business_type": "D2C",
                "business_platform": "WooCommerce",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_problem": "Support issue",
            }
        )
        self.assertEqual(state.pending_field(), "pickup_location")

        state.apply_deterministic_answers(
            "Delhi se Bangalore",
            turn_id="route-while-rate-pending",
            previous_agent_text="Aapke shipments kahan se pick up hote hain?",
        )

        self.assertEqual(state.value("pickup_location"), "Delhi")
        self.assertEqual(state.value("delivery_location"), "Bengaluru")
        self.assertEqual(state.pending_field(), "")
        self.assertEqual(
            state.active_route(),
            {"pickup_location": "Delhi", "delivery_location": "Bengaluru"},
        )


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
        self.assertIn("conversation_consent", field_enum)
        self.assertIn("service", field_enum)
        self.assertNotIn("zone", field_enum)
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
