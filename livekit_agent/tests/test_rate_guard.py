from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from livekit_agent.agent import (
    GuardedTurnProcessor,
    ShipKiaAssistant,
    _authoritative_call_state_instruction,
    _authorized_controlled_reply_tools,
    _authoritative_rate_request_arguments,
    _detailed_services_reply_instruction,
    _gemini_end_sensitivity,
    _gemini_start_sensitivity,
    _high_volume_manager_reply_instruction,
    _high_volume_manager_delivery_complete,
    _asr_noise_reason,
    _is_opening_noise_turn,
    _normalize_rate_request_arguments,
    _prepare_rate_arguments,
    _provider_options_reply_instruction,
    _rate_gate_response,
    _response_language_for_turn,
    _assistant_pincode_claims,
    _assistant_single_zone_claims,
    _shipkia_rate_claim_amounts,
    _shipkia_flow_response_violation,
    _suppress_unsolicited_realtime_speech,
    _voice_flat_catalog_result,
    _voice_flat_zonal_catalog_result,
    _flat_catalog_response_complete,
    _flat_zonal_catalog_response_complete,
    _flow_violation_requires_correction,
    _voice_selected_flat_service_result,
    _voice_safe_pincode_serviceability_result,
    _voice_safe_unknown_zone_result,
    make_mcp_forwarder,
)
from livekit_agent.conversation_state import GatedConversationState, STATE_MANAGED_RATE_FIELDS


class _Runtime:
    def __init__(self):
        self.events = []
        self.turns = []
        self.tool_outcomes = []

    async def emit(self, event, **payload):
        self.events.append((event, payload))

    def transcript(self):
        return ""

    def metrics(self):
        return {}

    def record_tool_outcome(self, tool_name, *, status, summary=""):
        self.tool_outcomes.append((tool_name, status, summary))


class TestRateGateResponse(unittest.TestCase):
    def test_v5_controlled_turn_exposes_no_pricing_tool_before_rate_path(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")

        self.assertEqual(_authorized_controlled_reply_tools(state), [])

        state.apply_deterministic_answers("flat rates", turn_id="flat")
        self.assertEqual(
            _authorized_controlled_reply_tools(state),
            ["get_shipkia_flat_rates"],
        )

    def test_v6_last_call_flite_rate_authorizes_only_flat_catalog_tool(self):
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
                "zone": "D",
            }
        )
        state.optional_ended_by = "business_name"

        state.apply_deterministic_answers(
            "Flite rate available hai aapke pass?", turn_id="flat-asr"
        )

        self.assertEqual(state.pricing_mode(), "flat_catalog")
        self.assertEqual(
            _authorized_controlled_reply_tools(state),
            ["get_shipkia_flat_rates"],
        )

    def test_interrupted_high_volume_sentence_is_not_marked_delivered(self):
        self.assertFalse(
            _high_volume_manager_delivery_complete(
                "Theek hai, aapki monthly shipments 2,000 hain. Is"
            )
        )
        self.assertTrue(
            _high_volume_manager_delivery_complete(
                "Aapko dedicated account manager milega jo support aur ticketing mein help karega."
            )
        )

    def test_v5_suppresses_only_unowned_realtime_generation(self):
        self.assertTrue(
            _suppress_unsolicited_realtime_speech(
                controlled_flow=True,
                user_initiated=False,
                expected_tool_reply=False,
            )
        )
        for controlled_flow, user_initiated, expected_tool_reply in (
            (False, False, False),
            (True, True, False),
            (True, False, True),
        ):
            with self.subTest(
                controlled_flow=controlled_flow,
                user_initiated=user_initiated,
                expected_tool_reply=expected_tool_reply,
            ):
                self.assertFalse(
                    _suppress_unsolicited_realtime_speech(
                        controlled_flow=controlled_flow,
                        user_initiated=user_initiated,
                        expected_tool_reply=expected_tool_reply,
                    )
                )

    def test_call_1838_rejects_multilingual_noise_hallucinations(self):
        noisy_transcripts = (
            ("nai. Ladki, kaun? Nai nai.", None, "repeated_negative_fragment"),
            ("Mmm, no.", None, "filler_negative_fragment"),
            ("\u0646\u06c1\u06cc\u06ba.", None, "unsupported_script"),
            ("\ub0b4\uc774 \ub0b4\uc774", None, "unsupported_script"),
            ("\u306d \u3044\u3044 \u306d \u3044\u3044 \u3002", None, "unsupported_script"),
            ("estas? \u00bfY si quieres, vamos?", None, "unexpected_language_punctuation"),
            ("No", "es-ES", "unsupported_language:es-es"),
        )
        for transcript, language, expected in noisy_transcripts:
            with self.subTest(transcript=transcript):
                self.assertEqual(
                    _asr_noise_reason(transcript, language=language),
                    expected,
                )

    def test_noise_filter_keeps_supported_real_customer_answers(self):
        for transcript in (
            "no",
            "nahi",
            "\u0928\u0939\u0940\u0902",
            "haan ji",
            "shipping rates",
            "Gurgaon se pan India",
            "8000",
            "mujhe onboarding nahi chahiye",
        ):
            with self.subTest(transcript=transcript):
                self.assertEqual(_asr_noise_reason(transcript), "")

        self.assertEqual(
            _asr_noise_reason("shipping rates", confidence=0.2),
            "low_transcript_confidence",
        )

    def test_controlled_detailed_services_reply_is_complete_and_single(self):
        instruction = _detailed_services_reply_instruction("Hinglish")

        for expected in (
            "multiple courier partners",
            "dedicated account manager",
            "WhatsApp",
            "call fallback",
            "NDR",
            "IVR calls",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, instruction)
        self.assertEqual(instruction.count("Aap kuch aur jaanna chahenge"), 1)
        self.assertIn("Say exactly once", instruction)
        self.assertIn("repeat the closing question", instruction)

    def test_v6_detailed_services_does_not_reoffer_rates_or_onboarding(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        instruction = _detailed_services_reply_instruction("Hinglish", state)

        self.assertNotIn("rates check karne", instruction)
        self.assertNotIn("onboarding mein help", instruction)
        self.assertNotIn("Aap kuch aur jaanna chahenge", instruction)
        self.assertIn("central V6 sales flow", instruction)

    def test_controlled_provider_reply_lists_names_once_then_only_pending_question(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates", turn_id="intent")

        instruction = _provider_options_reply_instruction("Hinglish", state)

        for provider in (
            "Amazon",
            "Bluedart",
            "Delhivery",
            "E-Kart",
            "Shadowfax",
            "Shree Maruti",
            "Xpressbees",
        ):
            with self.subTest(provider=provider):
                self.assertEqual(instruction.count(provider), 1)
        self.assertEqual(instruction.count("business ya brand ka naam kya hai"), 1)
        self.assertNotIn("Aap kuch aur jaanna chahenge", instruction)
        self.assertIn("repeat any information", instruction)

    def test_high_volume_reply_adds_manager_support_before_information_checkpoint(self):
        instruction = _high_volume_manager_reply_instruction("Hinglish", 1000)

        self.assertIn("monthly shipments 1,000", instruction)
        self.assertIn("dedicated account manager", instruction)
        self.assertIn("support aur ticketing", instruction)
        self.assertNotIn("Kya aap ShipKia ke saath aage badhna chahte hain", instruction)
        self.assertEqual(instruction.count("Kya aap kuch aur jaanna chahenge"), 1)
        self.assertIn("Never ask for monthly shipments again", instruction)

    def test_opening_ignores_short_non_actionable_asr_noise(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)

        self.assertTrue(_is_opening_noise_turn("Coke", state))
        self.assertTrue(_is_opening_noise_turn("random sound", state))
        for meaningful in (
            "haan",
            "yeah",
            "hello",
            "ji bataiye",
            "okay",
            "nahi",
            "rates",
            "services",
            "one minute",
            "not interested",
            "wrong number",
        ):
            with self.subTest(meaningful=meaningful):
                self.assertFalse(_is_opening_noise_turn(meaningful, state))

        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        self.assertFalse(_is_opening_noise_turn("Coke", state))

    def test_opening_cannot_restart_after_consent_is_accepted(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")

        violation = _shipkia_flow_response_violation(
            agent_text=(
                "Namaste, main ShipKia ki taraf se baat kar raha hoon. Humein aapki shipping "
                "query mili thi. Kya abhi hum do minute baat kar sakte hain?"
            ),
            customer_text="ShipKia ki services kya kya hain?",
            previous_agent_text="Aap rates check karna chahenge ya onboarding mein help chahiye?",
            conversation_state=state,
        )

        self.assertEqual(violation, "restarted_opening")

    def test_call_1708_blocks_flat_zonal_claim_without_matching_catalog(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.flat_catalog_presented = True
        state.authorized_rate_amounts.update({76.58, 88.26})

        violation = _shipkia_flow_response_violation(
            agent_text=(
                "E-Kart Express ke Flat-Zonal rates available hain. Zones A aur B ke liye "
                "Rs 76.58 aur Zones C se F ke liye Rs 88.26 hain."
            ),
            customer_text="square channel rate hai?",
            previous_agent_text="",
            conversation_state=state,
        )

        self.assertEqual(violation, "unverified_flat_zonal_claim")

        clarification = _shipkia_flow_response_violation(
            agent_text=(
                "E-Kart Surface ke Flat rates chahiye ya E-Kart Express ke Flat-Zonal rates?"
            ),
            customer_text="E-Kart ke rates batao",
            previous_agent_text="",
            conversation_state=state,
        )
        self.assertEqual(clarification, "")

    def test_call_1708_followup_catalog_stops_without_rearming_checkpoint(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 1000})
        state.anything_else_checkpoint_consumed = True

        result = _voice_flat_catalog_result(
            {
                "status": "success",
                "response_type": "flat_all",
                "payment_type": "Prepaid",
                "flat_rate_options": [
                    {"min_weight_g": 0, "max_weight_g": 500, "total": 76.58}
                ],
            },
            conversation_state=state,
        )

        instruction = result["spoken_response_instruction"]
        self.assertIn("stop without asking another question", instruction)
        self.assertNotIn("Kya aap kuch aur jaanna chahenge", instruction)

        repeated = _shipkia_flow_response_violation(
            agent_text=(
                "Flat slabs bata diye hain. Kya aap kuch aur jaanna chahenge?"
            ),
            customer_text="Flat rates batao",
            previous_agent_text="",
            conversation_state=state,
        )
        self.assertEqual(repeated, "repeated_anything_else_checkpoint")

    def test_information_checkpoint_is_blocked_during_early_service_answer(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("haan", turn_id="consent")
        customer_text = "ShipKia ki services kya kya hain?"
        state.apply_deterministic_answers(customer_text, turn_id="services")

        violation = _shipkia_flow_response_violation(
            agent_text=(
                "ShipKia multiple courier partners ke shipments manage karta hai aur dedicated "
                "account manager support deta hai. Kya aap kuch aur jaanna chahenge?"
            ),
            customer_text=customer_text,
            previous_agent_text="Aap rates check karna chahenge ya onboarding help chahiye?",
            conversation_state=state,
        )

        self.assertEqual(violation, "usp_ignored")

    def test_call_1725_complete_service_answer_allows_one_combined_continuation(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        customer_text = "ShipKia ki services kya kya hain?"
        state.apply_deterministic_answers(customer_text, turn_id="services")

        violation = _shipkia_flow_response_violation(
            agent_text=(
                "ShipKia multiple courier partners ke shipments manage karta hai, dedicated "
                "account manager support deta hai, WhatsApp order confirmation ke baad call "
                "fallback deta hai, aur NDR ke liye WhatsApp aur IVR follow-up karta hai. "
                "Aap kuch aur jaanna chahenge, ya main aapko rates check karne ya onboarding "
                "mein help karun?"
            ),
            customer_text=customer_text,
            previous_agent_text="Aap shipping rates check karna chahenge ya onboarding mein help chahiye?",
            conversation_state=state,
        )

        self.assertEqual(violation, "")

    def test_call_1726_asr_service_answer_requires_all_workflow_details(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        customer_text = "Ship kya ki-kya-kya kar dethe hain?"
        state.apply_deterministic_answers(customer_text, turn_id="services-asr")

        generic_labels = _shipkia_flow_response_violation(
            agent_text=(
                "ShipKia multiple courier shipments manage karta hai. Order confirmation, NDR "
                "assistance aur dedicated account manager support milta hai. Kya aap kuch aur "
                "jaanna chahenge?"
            ),
            customer_text=customer_text,
            previous_agent_text="Aap rates check karna chahenge ya onboarding mein help chahiye?",
            conversation_state=state,
        )
        complete = _shipkia_flow_response_violation(
            agent_text=(
                "ShipKia multiple courier partners ke shipments manage karta hai, dedicated "
                "account manager ticketing support deta hai, WhatsApp order confirmation ke "
                "baad call fallback deta hai, aur NDR ke liye WhatsApp aur IVR follow-up karta "
                "hai. Aap kuch aur jaanna chahenge, ya main aapko rates check karne ya "
                "onboarding mein help karun?"
            ),
            customer_text=customer_text,
            previous_agent_text="Aap rates check karna chahenge ya onboarding mein help chahiye?",
            conversation_state=state,
        )

        self.assertEqual(generic_labels, "usp_ignored")
        self.assertEqual(complete, "")

    def test_information_checkpoint_remains_allowed_at_authorized_post_rate_stage(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.anything_else_question_due = True

        violation = _shipkia_flow_response_violation(
            agent_text="Kya aap kuch aur jaanna chahenge?",
            customer_text="around 1000",
            previous_agent_text="Aapki monthly shipments kitni hoti hain?",
            conversation_state=state,
        )

        self.assertEqual(violation, "")

    def test_initial_authoritative_instruction_seeds_consent_before_realtime_draft(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)

        instruction = _authoritative_call_state_instruction(state, "Hinglish")

        self.assertIn("Pending field: conversation_consent", instruction)
        self.assertIn("Current action: Ask only whether this is a convenient time to talk", instruction)
        self.assertIn("No ShipKia numeric amount is authorized", instruction)
        self.assertIn("Natural Hinglish in Latin script only", instruction)

    def test_assistant_constructor_sends_initial_consent_state_to_realtime_model(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)

        with patch("livekit_agent.agent.Agent.__init__", return_value=None) as agent_init:
            ShipKiaAssistant(
                system_prompt="ShipKia prompt",
                personality="",
                context={},
                tools=[],
                available_tool_names=(),
                runtime=_Runtime(),
                conversation_state=state,
                turn_processor=object(),
            )

        instructions = agent_init.call_args.kwargs["instructions"]
        self.assertIn("Pending field: conversation_consent", instructions)
        self.assertIn("Ask only whether this is a convenient time to talk", instructions)
        self.assertIn("## V5 quantity and close flow", instructions)
        self.assertNotIn("## V4 post-rate close flow", instructions)
        self.assertNotIn(
            "Never ask the customer for monthly shipment volume at any point",
            instructions,
        )
        self.assertIn("explain all four verified capabilities", instructions)

    def test_v6_assistant_uses_compact_model_led_runtime_without_state_injection(self):
        state = GatedConversationState(model_led_flow=True)
        pricing_tool = object()

        with patch("livekit_agent.agent.Agent.__init__", return_value=None) as agent_init:
            assistant = ShipKiaAssistant(
                system_prompt=(
                    "V6 sales prompt. Save using create_or_update_shipkia_lead, then call "
                    "get_shipkia_starting_rate for a verified starting rate."
                ),
                personality="",
                context={},
                tools=[pricing_tool],
                available_tool_names=("get_shipkia_starting_rate",),
                runtime=_Runtime(),
                conversation_state=state,
                turn_processor=object(),
            )

        instructions = agent_init.call_args.kwargs["instructions"]
        self.assertNotIn("## V6 runtime boundary", instructions)
        self.assertNotIn("create_or_update_shipkia_lead", instructions)
        self.assertIn("get_shipkia_starting_rate", instructions)
        self.assertNotIn("Current authoritative call state", instructions)
        self.assertNotIn("## Voice runtime rules", instructions)
        self.assertEqual(agent_init.call_args.kwargs["tools"], [pricing_tool])
        self.assertEqual(assistant._active_tools(), [pricing_tool])

    def test_empty_pending_field_returns_safe_gate_instead_of_crashing(self):
        result = _rate_gate_response(
            "starting_rate_required",
            "",
            "Pricing is not ready yet.",
        )

        self.assertEqual(result["next_missing_field"], "")
        self.assertEqual(result["next_question"], "")
        self.assertEqual(result["status"], "starting_rate_required")


class _AnswerGuard:
    def __init__(self, delay=0):
        self.calls = 0
        self.delay = delay
        self.last_kwargs = {}

    async def classify(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        await asyncio.sleep(self.delay)
        text = kwargs["customer_text"]
        return {
            "turn_disposition": "answered",
            "decisions": [
                {
                    "field": "business_name",
                    "disposition": "answered",
                    "value": text,
                    "evidence": text,
                    "confidence": 0.99,
                }
            ],
        }


class _FakeResponse:
    status = 200

    async def text(self):
        return json.dumps(
            {
                "message": {
                    "result": {
                        "status": "success",
                        "zone_required": False,
                        "eligible_rates": [],
                    }
                }
            }
        )


class _FakeResponseContext:
    async def __aenter__(self):
        return _FakeResponse()

    async def __aexit__(self, *_args):
        return False


class _FakeClientSession:
    captured_payload = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def post(self, _url, *, json, **_kwargs):
        type(self).captured_payload = json
        return _FakeResponseContext()


class _FlatFakeResponse:
    status = 200

    async def text(self):
        return json.dumps(
            {
                "message": {
                    "result": {
                        "status": "success",
                        "response_type": "flat_all",
                        "response_scope": "All",
                        "currency": "INR",
                        "payment_type": "Prepaid",
                        "courier_partner": "E-Kart",
                        "service": "E-Kart SURFACE",
                        "starting_flat_rate": {
                            "min_weight_g": 0,
                            "max_weight_g": 500,
                            "shipping_charge": 64.9,
                            "cod_charge": 0.0,
                            "gst": 11.68,
                            "total": 76.58,
                        },
                        "flat_rate_options": [
                            {"min_weight_g": 0, "max_weight_g": 500, "total": 76.58},
                            {"min_weight_g": 501, "max_weight_g": 1000, "total": 88.26},
                            {"min_weight_g": 1001, "max_weight_g": 2000, "total": 103.84},
                        ],
                        "flat_additional_rate_options": [
                            {
                                "courier_partner": "Shadowfax",
                                "service": "Shadowfax Surface 5 KG",
                                "applies_after_weight_g": 10000,
                                "additional_weight_unit_g": 1000,
                                "flat_additional_rate_breakdown": {"total": 11.68},
                            }
                        ],
                        "verified_flat_rate_count": 3,
                    }
                }
            }
        )


class _FlatFakeResponseContext:
    async def __aenter__(self):
        return _FlatFakeResponse()

    async def __aexit__(self, *_args):
        return False


class _FlatFakeClientSession(_FakeClientSession):
    def post(self, _url, *, json, **_kwargs):
        type(self).captured_payload = json
        return _FlatFakeResponseContext()


class _FlatZonalFakeResponse:
    status = 200

    async def text(self):
        return json.dumps(
            {
                "message": {
                    "result": {
                        "status": "success",
                        "response_type": "flat_zonal_all",
                        "currency": "INR",
                        "payment_type": "Prepaid",
                        "courier_partner": "E-Kart",
                        "service": "E-Kart EXPRESS",
                        "zone_groups": [
                            {"zone_group": "A-B", "zones": ["A", "B"], "max_weight_g": 500, "total": 84.37},
                            {"zone_group": "C-F", "zones": ["C", "D", "E", "F"], "max_weight_g": 500, "total": 109.03},
                        ],
                        "additional_weight": {"additional_weight_unit_g": 500, "total": 38.94},
                    }
                }
            }
        )


class _FlatZonalFakeResponseContext:
    async def __aenter__(self):
        return _FlatZonalFakeResponse()

    async def __aexit__(self, *_args):
        return False


class _FlatZonalFakeClientSession(_FakeClientSession):
    def post(self, _url, *, json, **_kwargs):
        type(self).captured_payload = json
        return _FlatZonalFakeResponseContext()


class _StartingFakeResponse:
    status = 200

    async def text(self):
        return json.dumps(
            {
                "message": {
                    "result": {
                        "status": "success",
                        "response_type": "general_starting",
                        "amount": 22.0,
                        "currency": "INR",
                        "gst_inclusive": False,
                    }
                }
            }
        )


class _StartingFakeResponseContext:
    async def __aenter__(self):
        return _StartingFakeResponse()

    async def __aexit__(self, *_args):
        return False


class _StartingFakeClientSession(_FakeClientSession):
    def post(self, _url, *, json, **_kwargs):
        type(self).captured_payload = json
        return _StartingFakeResponseContext()


class _ShadowfaxStartingFakeResponse:
    status = 200

    async def text(self):
        return json.dumps(
            {
                "message": {
                    "result": {
                        "status": "success",
                        "response_type": "zone_starting",
                        "zone": "C",
                        "amount": 76.58,
                        "currency": "INR",
                        "gst_inclusive": True,
                        "basis": {
                            "movement_type": "Forward",
                            "weight_slab_g": 500,
                            "courier": "Shadowfax",
                            "service": "Shadowfax Surface 500 G",
                        },
                    }
                }
            }
        )


class _ShadowfaxStartingFakeResponseContext:
    async def __aenter__(self):
        return _ShadowfaxStartingFakeResponse()

    async def __aexit__(self, *_args):
        return False


class _ShadowfaxStartingFakeClientSession(_FakeClientSession):
    def post(self, _url, *, json, **_kwargs):
        type(self).captured_payload = json
        return _ShadowfaxStartingFakeResponseContext()


class _RouteFakeResponse:
    status = 200

    def __init__(self, result):
        self.result = result

    async def text(self):
        return json.dumps({"message": {"result": self.result}})


class _RouteFakeResponseContext:
    def __init__(self, result):
        self.result = result

    async def __aenter__(self):
        return _RouteFakeResponse(self.result)

    async def __aexit__(self, *_args):
        return False


class _RouteFakeClientSession(_FakeClientSession):
    captured_payloads = []

    def post(self, _url, *, json, **_kwargs):
        arguments = json["params"]["arguments"]
        type(self).captured_payloads.append(json)
        bengaluru = arguments.get("delivery_location") == "Bengaluru"
        zone = "C" if bengaluru else "A"
        amount = 31.15 if bengaluru else 22.07
        return _RouteFakeResponseContext(
            {
                "status": "success",
                "serviceable": True,
                "zone": zone,
                "zone_verified": True,
                "pickup_location": arguments.get("pickup_location"),
                "delivery_location": arguments.get("delivery_location"),
                "resolution_basis": (
                    "metro_to_metro" if bengaluru else "same_shipping_cluster"
                ),
                "starting_rate": {
                    "status": "success",
                    "response_type": "zone_starting",
                    "zone": zone,
                    "amount": amount,
                    "currency": "INR",
                    "gst_inclusive": True,
                },
            }
        )


class TestRateGuard(unittest.IsolatedAsyncioTestCase):
    def test_call_1698_noise_greeting_is_detected_as_repeated_pending_question(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("haan", turn_id="consent")
        state.apply_deterministic_answers("rates janna chahta hoon", turn_id="intent")
        state.mark_qualification_bridge_presented()
        question = "Aapke business ya brand ka naam kya hai?"

        violation = _shipkia_flow_response_violation(
            agent_text=question,
            customer_text="Hallo.",
            previous_agent_text=question,
            conversation_state=state,
        )

        self.assertEqual(violation, "repeated_pending:business_name")

    def test_call_1693_stale_state_does_not_reject_contextual_move_forward(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.anything_else_question_due = True

        allowed = _shipkia_flow_response_violation(
            agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
            customer_text="a nahin nahin.",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
            conversation_state=state,
        )
        unrelated = _shipkia_flow_response_violation(
            agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
            customer_text="a nahin nahin.",
            previous_agent_text="Aapka current shipping rate kya hai?",
            conversation_state=state,
        )

        self.assertEqual(allowed, "")
        self.assertEqual(unrelated, "premature_move_forward")

    def test_noise_resistant_gemini_vad_profile_is_configurable(self):
        with patch.dict(
            os.environ,
            {
                "GEMINI_VAD_START_SENSITIVITY": "LOW",
                "GEMINI_VAD_END_SENSITIVITY": "HIGH",
            },
        ):
            self.assertEqual(_gemini_start_sensitivity().value, "START_SENSITIVITY_LOW")
            self.assertEqual(_gemini_end_sensitivity().value, "END_SENSITIVITY_HIGH")

        with patch.dict(
            os.environ,
            {
                "GEMINI_VAD_START_SENSITIVITY": "HIGH",
                "GEMINI_VAD_END_SENSITIVITY": "LOW",
            },
        ):
            self.assertEqual(_gemini_start_sensitivity().value, "START_SENSITIVITY_HIGH")
            self.assertEqual(_gemini_end_sensitivity().value, "END_SENSITIVITY_LOW")

    def test_invalid_gemini_vad_profile_falls_back_safely(self):
        with patch.dict(
            os.environ,
            {
                "GEMINI_VAD_START_SENSITIVITY": "invalid",
                "GEMINI_VAD_END_SENSITIVITY": "invalid",
            },
        ):
            self.assertEqual(_gemini_start_sensitivity().value, "START_SENSITIVITY_LOW")
            self.assertEqual(_gemini_end_sensitivity().value, "END_SENSITIVITY_HIGH")

    def test_v5_blocks_plan_offer_immediately_after_monthly_quantity(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)

        violation = _shipkia_flow_response_violation(
            agent_text=(
                "Theek hai, 5000 shipments ke liye hum aapke liye ek alag plan discuss "
                "kar sakte hain. Kya aap kuch aur jaanna chahenge?"
            ),
            customer_text="around 5,000",
            previous_agent_text="Aapki monthly shipment quantity kitni hai?",
            conversation_state=state,
        )

        self.assertEqual(violation, "unauthorized_better_plan")

    def test_v5_blocks_advancing_optional_discovery_after_rate_refusal(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)

        violation = _shipkia_flow_response_violation(
            agent_text="Koi baat nahi. Aapko shipping mein kya problem aa rahi hai?",
            customer_text="Wo main nahin bata sakta.",
            previous_agent_text="Shiprocket ke saath aapka kya rate chal raha hai?",
            conversation_state=state,
        )

        self.assertEqual(violation, "advanced_after_optional_refusal")

    def test_v5_rate_discovery_business_name_requires_natural_bridge_once(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("haan", turn_id="consent")
        state.apply_deterministic_answers("rates janna chahta hoon", turn_id="intent")

        omitted = _shipkia_flow_response_violation(
            agent_text="Aapke business ya brand ka naam kya hai?",
            customer_text="rates janna chahta hoon",
            previous_agent_text="Rates chahiye ya onboarding help?",
            conversation_state=state,
        )
        allowed = _shipkia_flow_response_violation(
            agent_text=(
                "Rates batane se pehle main aapse kuch zaroori details jaan lena chahunga. "
                "Aapke business ya brand ka naam kya hai?"
            ),
            customer_text="rates janna chahta hoon",
            previous_agent_text="Rates chahiye ya onboarding help?",
            conversation_state=state,
        )

        self.assertEqual(omitted, "qualification_bridge_omitted")
        self.assertEqual(allowed, "")
        state.mark_qualification_bridge_presented()
        self.assertEqual(
            _shipkia_flow_response_violation(
                agent_text="Aapke business ya brand ka naam kya hai?",
                customer_text="dobara bataiye",
                previous_agent_text="",
                conversation_state=state,
            ),
            "",
        )

    def test_v5_captured_pytant_volume_cannot_be_reasked(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("haan", turn_id="consent")
        state.apply_deterministic_answers("rates janna chahta hoon", turn_id="intent")
        state.monthly_quantity_due = True
        state.apply_deterministic_answers(
            "Pytant",
            turn_id="monthly-volume",
            previous_agent_text="Aapki monthly shipments kitni hoti hain?",
        )

        violation = _shipkia_flow_response_violation(
            agent_text="Aapki monthly shipments kitni hoti hain?",
            customer_text="Pytant",
            previous_agent_text="Aapki monthly shipments kitni hoti hain?",
            conversation_state=state,
        )

        self.assertEqual(violation, "reasked_handled:monthly_shipments")

    def test_v5_flow_guard_blocks_ignored_benefits_query(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)

        violation = _shipkia_flow_response_violation(
            agent_text="Aapki company ka naam kya hai?",
            customer_text=(
                "Main rates check karna chahta hoon, ShipKia ka procedure kya hai aur kya benefit milega?"
            ),
            previous_agent_text="Aap rates check karna chahenge ya onboarding help chahiye?",
            conversation_state=state,
        )

        self.assertEqual(violation, "usp_ignored")

        specific_answer = _shipkia_flow_response_violation(
            agent_text=(
                "ShipKia ka dedicated account manager ticketing aur support mein help karta hai."
            ),
            customer_text="Aapki support facility ka kya benefit hai?",
            previous_agent_text="Aapko kis cheez mein help chahiye?",
            conversation_state=state,
        )
        self.assertEqual(specific_answer, "")

        flexible_general_answer = _shipkia_flow_response_violation(
            agent_text=(
                "ShipKia multiple courier partners ke shipments ek jagah manage karne mein help "
                "karta hai, aur ticketing ke liye dedicated account manager support milta hai."
            ),
            customer_text="ShipKia ke benefits aur working ke baare mein bataiye.",
            previous_agent_text="Aapko kis cheez mein help chahiye?",
            conversation_state=state,
        )
        self.assertEqual(flexible_general_answer, "")

        incomplete_general_answer = _shipkia_flow_response_violation(
            agent_text="ShipKia mein dedicated account manager support milta hai.",
            customer_text="ShipKia ke benefits aur working ke baare mein bataiye.",
            previous_agent_text="Aapko kis cheez mein help chahiye?",
            conversation_state=state,
        )
        self.assertEqual(incomplete_general_answer, "usp_ignored")

        unsupported_claim = _shipkia_flow_response_violation(
            agent_text=(
                "ShipKia multiple courier partners deta hai aur 50 percent savings guarantee karta hai."
            ),
            customer_text="ShipKia ke benefits aur working ke baare mein bataiye.",
            previous_agent_text="Aapko kis cheez mein help chahiye?",
            conversation_state=state,
        )
        self.assertEqual(unsupported_claim, "unsupported_usp_claim")

    def test_v5_flow_guard_blocks_spoken_onboarding_url(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.onboarding_link_due = True

        violation = _shipkia_flow_response_violation(
            agent_text=(
                "Aap auth.shipkia.com/signup par account create karke onboarding start kar sakte hain."
            ),
            customer_text="Okay, theek hai.",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
            conversation_state=state,
        )

        self.assertEqual(violation, "spoken_onboarding_url")

    def test_v5_flow_guard_blocks_premature_or_unexplained_move_forward(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.monthly_quantity_due = True

        premature = _shipkia_flow_response_violation(
            agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
            customer_text="200 se 250 shipments month ki",
            previous_agent_text="Aapki monthly shipments kitni rehti hain?",
            conversation_state=state,
        )
        self.assertEqual(premature, "premature_move_forward")

        state.monthly_quantity_due = False
        state.move_forward_question_due = True
        state.last_customer_dissatisfied = True
        state.primary_rate_amount = 35.05
        unexplained = _shipkia_flow_response_violation(
            agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
            customer_text="Aapne comparison aur prices explain nahi kare.",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
            conversation_state=state,
        )
        self.assertEqual(unexplained, "pricing_objection_ignored")

    def test_v5_flow_guard_blocks_questions_for_already_handled_context(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="intent")
        state.seed_context(
            {
                "business_name": "Randhawa Transport",
                "business_type": "B2C",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_shipping_rate": 35,
                "current_problem": "RTO issue",
                "pickup_location": "Bengaluru",
                "delivery_location": "Pune",
                "dead_weight": 3.5,
                "payment_type": "Prepaid",
                "monthly_shipments": 250,
            }
        )
        questions = {
            "business_name": "Aapki company ka naam kya hai?",
            "business_type": "Aapka business B2C hai ya D2C?",
            "current_shipping_arrangement": "Aap abhi kaunsa courier provider use karte hain?",
            "current_shipping_rate": "Aapka current shipping rate kitna hai?",
            "current_problem": "Shiprocket ke saath kya problem aa rahi hai?",
            "pickup_pincode": "Aap shipping kahaan se karna chahte hain?",
            "delivery_pincode": "Delivery ya drop location kahaan hai?",
            "dead_weight": "Package ka weight kitna hai?",
            "payment_type": "Shipment Prepaid hai ya COD?",
            "monthly_shipments": "Aapki monthly shipments kitni hain?",
        }

        for field, question in questions.items():
            with self.subTest(field=field):
                violation = _shipkia_flow_response_violation(
                    agent_text=question,
                    customer_text="Theek hai",
                    previous_agent_text="",
                    conversation_state=state,
                )
                self.assertEqual(violation, f"reasked_handled:{field}")

    def test_v5_flow_guard_allows_the_actual_pending_question(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="intent")
        state.seed_context({"business_name": "Randhawa Transport"})

        violation = _shipkia_flow_response_violation(
            agent_text="Aapka business B2C hai ya D2C?",
            customer_text="Randhawa Transport",
            previous_agent_text="Aapki company ka naam kya hai?",
            conversation_state=state,
        )

        self.assertEqual(state.pending_field(), "business_type")
        self.assertEqual(violation, "")

    def test_call_1665_guard_allows_answer_then_anything_else_checkpoint(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"payment_type": "Prepaid", "monthly_shipments": 5000})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("flat rates batao", turn_id="flat")
        state.mark_flat_catalog_presented()
        state.mark_pricing_verified("get_shipkia_flat_rates", payment_basis="Prepaid")

        violation = _shipkia_flow_response_violation(
            agent_text="Ye prepaid rates hain. Kya aap kuch aur jaanna chahenge?",
            customer_text="Order value 1000 hai.",
            previous_agent_text="Prepaid hai ya COD?",
            conversation_state=state,
        )

        self.assertEqual(violation, "")

    def test_call_1670_guard_blocks_repeated_resolution_close(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.unsatisfied_resolution_presented = True
        state.better_plan_close_presented = True

        violation = _shipkia_flow_response_violation(
            agent_text=(
                "Main aapki is problem ko apni team ke saath discuss karke aapko solution ya "
                "better plan deta hoon. Thank you for calling ShipKia."
            ),
            customer_text="Maine sabke rates pooche hain.",
            previous_agent_text=(
                "Main aapki is problem ko apni team ke saath discuss karke aapko solution ya "
                "better plan deta hoon. Thank you for calling ShipKia."
            ),
            conversation_state=state,
        )

        self.assertEqual(violation, "repeated_resolution_close")

        provider_state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
        )
        provider_state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        provider_state.apply_deterministic_answers("shipping rates", turn_id="intent")
        provider_state.seed_context(
            {
                "business_name": "Randhawa Transport",
                "business_type": "B2C",
                "current_shipping_arrangement": "Shipping Aggregator",
            }
        )
        provider_violation = _shipkia_flow_response_violation(
            agent_text="Aap abhi kaunsa courier ya shipping provider use karte hain?",
            customer_text="Shipping Aggregator",
            previous_agent_text="Aapka current shipping arrangement kya hai?",
            conversation_state=provider_state,
        )
        self.assertEqual(provider_state.pending_field(), "current_provider_name")
        self.assertEqual(provider_violation, "")

    def test_v5_flow_guard_blocks_skipping_unconfirmed_business_type(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Delhi to Mumbai rate", turn_id="intent")
        state.seed_context({"business_name": "Shanti Enterprises"})

        violation = _shipkia_flow_response_violation(
            agent_text="Theek hai, kya aap abhi koi courier company use kar rahe hain?",
            customer_text="to c",
            previous_agent_text="Aapka business B2C hai ya D2C?",
            conversation_state=state,
        )

        self.assertEqual(state.pending_field(), "business_type")
        self.assertEqual(violation, "skipped_pending:business_type")

    def test_v5_flow_guard_blocks_unauthorized_better_plan_from_unclear_audio(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"monthly_shipments": 500})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C rate batao", turn_id="rate")
        state.mark_pricing_verified("get_shipkia_starting_rate")

        violation = _shipkia_flow_response_violation(
            agent_text=(
                "Theek hai, hum aapke liye ek better plan team se discuss karke share karenge."
            ),
            customer_text="Tem.",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahte hain?",
            conversation_state=state,
        )

        self.assertTrue(state.anything_else_question_due)
        self.assertFalse(state.move_forward_question_due)
        self.assertFalse(state.better_plan_close_due)
        self.assertEqual(violation, "unauthorized_better_plan")

    async def test_call_1627_bare_route_does_not_switch_hinglish_call_to_english(self):
        self.assertEqual(
            _response_language_for_turn("Delhi to Mumbai", "Hinglish"),
            "Hinglish",
        )
        self.assertEqual(_response_language_for_turn("Shiprocket", "Hinglish"), "Hinglish")
        self.assertEqual(_response_language_for_turn("B2C", "Hinglish"), "Hinglish")
        self.assertEqual(
            _response_language_for_turn("I want the rates please", "Hinglish"),
            "English",
        )
        self.assertEqual(
            _response_language_for_turn("मुझे रेट्स जानना है।", "English"),
            "Hindi",
        )
        self.assertEqual(
            _response_language_for_turn("Hindi mein baat kijiye", "English"),
            "Hindi",
        )
        self.assertEqual(
            _response_language_for_turn("Mujhe rates batao", "English"),
            "Hinglish",
        )

    async def test_spoken_shipkia_rate_claim_detection_ignores_customer_comparison(self):
        self.assertEqual(_shipkia_rate_claim_amounts("ShipKia starting rate Rs 45 hai"), [45.0])
        self.assertEqual(_shipkia_rate_claim_amounts("Rate 45 rupaye se shuru hota hai"), [45.0])
        self.assertEqual(_shipkia_rate_claim_amounts("Shiprocket par aap Rs 32 de rahe hain"), [])
        self.assertEqual(
            _shipkia_rate_claim_amounts(
                "Prepaid flat rates check kar raha hun; COD order value Rs 1000 hai."
            ),
            [],
        )
        self.assertEqual(_assistant_pincode_claims("Noida se 530001 ka rate"), ["530001"])
        self.assertEqual(_assistant_single_zone_claims("Ye Zone D route hai"), ["D"])
        self.assertEqual(_assistant_single_zone_claims("Zones A-B aur C-F"), [])

    async def test_successful_pricing_forwarder_authorizes_only_returned_amounts(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rate", turn_id="intent")
        state.apply_deterministic_answers("Pan India rate", turn_id="pan-india")
        runtime = Mock()
        runtime.expect_realtime_tool_reply = Mock()
        forwarder = make_mcp_forwarder(
            "lookup_pincode_serviceability",
            "authorization-test",
            runtime=runtime,
            conversation_state=state,
        )
        _RouteFakeClientSession.captured_payloads = []

        with patch("livekit_agent.agent.aiohttp.ClientSession", _RouteFakeClientSession):
            result = json.loads(await forwarder({}))

        self.assertEqual(result["status"], "success")
        runtime.expect_realtime_tool_reply.assert_called_once_with()
        self.assertTrue(state.rate_claim_amounts_authorized([result["amount"]]))
        self.assertFalse(state.rate_claim_amounts_authorized([45.0]))

    async def test_v6_pan_india_returns_zone_a_then_monthly_despite_optional_gap(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates bataiye", turn_id="intent")
        state.seed_context({"business_name": "Acme"})
        state.apply_deterministic_answers("delivery Pan India hoti hai", turn_id="pan-india")
        forwarder = make_mcp_forwarder(
            "lookup_pincode_serviceability",
            "v6-pan-india-zone-a-test",
            conversation_state=state,
        )
        _RouteFakeClientSession.captured_payloads = []

        with patch("livekit_agent.agent.aiohttp.ClientSession", _RouteFakeClientSession):
            result = json.loads(await forwarder({}))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["zone"], "A")
        self.assertIn("monthly shipment quantity", result["post_rate_instruction"])
        self.assertNotIn("business type", result["post_rate_instruction"])

    async def test_v6_natural_consent_does_not_wait_for_phrase_gated_state(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        processor = type("Processor", (), {"schedule": Mock()})()
        with patch("livekit_agent.agent.Agent.__init__", return_value=None) as agent_init:
            assistant = ShipKiaAssistant(
                system_prompt="V6 sales prompt",
                personality="",
                context={},
                tools=[],
                available_tool_names=(),
                runtime=_Runtime(),
                conversation_state=state,
                turn_processor=processor,
            )

        class TurnContext:
            def __init__(self):
                self.messages = []

            def add_message(self, **message):
                self.messages.append(message)

        turn_context = TurnContext()
        message = type(
            "Message",
            (),
            {"text_content": "Haan ji, bilkul kar sakte hain, aage batao.", "id": "v6-consent"},
        )()

        await assistant.on_user_turn_completed(turn_context, message)

        processor.schedule.assert_called_once_with(
            "Haan ji, bilkul kar sakte hain, aage batao.",
            turn_id="v6-consent",
        )
        self.assertEqual(turn_context.messages, [])
        self.assertNotIn("Current authoritative call state", assistant._base_instructions)
        self.assertNotIn("finish the missing consultative discovery", assistant._base_instructions)
        initial_instructions = agent_init.call_args.kwargs["instructions"]
        self.assertNotIn("Current authoritative call state", initial_instructions)

    async def test_v6_flat_request_waits_for_discovery_and_keeps_verified_rates(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("flat rates chahiye", turn_id="intent")
        forwarder = make_mcp_forwarder(
            "get_shipkia_flat_rates",
            "v6-model-led-flat",
            conversation_state=state,
            backend_argument_names=frozenset({"response_scope", "payment_type"}),
        )
        _FlatFakeClientSession.captured_payload = None

        with patch("livekit_agent.agent.aiohttp.ClientSession", _FlatFakeClientSession):
            blocked = json.loads(await forwarder({}))

        self.assertEqual(blocked["status"], "qualification_required")
        self.assertEqual(blocked["required_next_question"], "business or brand name")
        self.assertNotIn("spoken_response_instruction", blocked)
        self.assertIsNone(_FlatFakeClientSession.captured_payload)

        state.seed_context(
            {
                "business_name": "Acme",
                "business_type": "D2C",
                "business_platform": "Shopify",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_shipping_rate": 40,
                "current_problem": "Support delays",
                "monthly_shipments": 1000,
            }
        )
        with patch("livekit_agent.agent.aiohttp.ClientSession", _FlatFakeClientSession):
            result = json.loads(await forwarder({}))

        self.assertEqual(result["status"], "success")
        self.assertEqual(
            _FlatFakeClientSession.captured_payload["params"]["arguments"],
            {"response_scope": "All", "payment_type": "Prepaid"},
        )
        self.assertTrue(state.rate_claim_amounts_authorized([76.58, 88.26, 103.84, 11.68]))

    async def test_v6_normal_route_rate_is_blocked_until_discovery_is_complete(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            model_led_flow=True,
            direct_onboarding_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.apply_deterministic_answers("Noida se Bangalore", turn_id="route")
        processor = type("Processor", (), {"wait_latest": AsyncMock()})()
        forwarder = make_mcp_forwarder(
            "lookup_pincode_serviceability",
            "v6-discovery-boundary",
            conversation_state=state,
            turn_processor=processor,
        )
        _RouteFakeClientSession.captured_payloads = []

        with patch("livekit_agent.agent.aiohttp.ClientSession", _RouteFakeClientSession):
            blocked = json.loads(
                await forwarder(
                    {"pickup_location": "Unknown", "delivery_location": "Unknown"}
                )
            )

        processor.wait_latest.assert_awaited_once()
        self.assertEqual(blocked["status"], "qualification_required")
        self.assertEqual(blocked["required_next_question"], "business or brand name")
        self.assertNotIn("spoken_response_instruction", blocked)
        self.assertEqual(_RouteFakeClientSession.captured_payloads, [])

        state.seed_context(
            {
                "business_name": "Acme",
                "business_type": "D2C",
                "business_platform": "Shopify",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_shipping_rate": 40,
                "current_problem": "Support delays",
                "monthly_shipments": 1000,
            }
        )
        with patch("livekit_agent.agent.aiohttp.ClientSession", _RouteFakeClientSession):
            result = json.loads(await forwarder({}))

        self.assertEqual(result["status"], "success")
        self.assertEqual(
            _RouteFakeClientSession.captured_payloads[0]["params"]["arguments"],
            {"pickup_location": "Noida", "delivery_location": "Bengaluru", "pan_india": False},
        )
        self.assertTrue(state.verified_rate_presented())
        self.assertEqual(state.pending_field(), "")
        self.assertFalse(state.qualification_bridge_due())
        self.assertIn("Kya aap kuch aur jaanna chahenge", result["post_rate_instruction"])
        self.assertNotIn("would like to move forward", result["post_rate_instruction"])

    async def test_v6_verified_rate_cannot_skip_discovery_or_offer_signup(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.authorize_rate_result({"status": "success", "amount": 31.15})
        state.mark_route_zone_verified("C", starting_presented=True)
        state.mark_pricing_verified("lookup_pincode_serviceability")
        self.assertEqual(state.pending_field(), "business_name")

        self.assertEqual(
            _shipkia_flow_response_violation(
                agent_text=(
                    "Delhi se Bengaluru ka starting rate Rs 31.15 hai. "
                    "Aapki monthly shipments kitni hoti hain?"
                ),
                customer_text="Delhi to Bangalore",
                previous_agent_text="",
                conversation_state=state,
            ),
            "skipped_pending:business_name",
        )
        self.assertEqual(
            _shipkia_flow_response_violation(
                agent_text="Main sign-up link WhatsApp par bhej dunga. Koi aur sawaal hai?",
                customer_text="ji bilkul",
                previous_agent_text="",
                conversation_state=state,
            ),
            "unauthorized_onboarding_link",
        )

    async def test_v6_route_question_matches_location_pending_and_unverified_claim_is_blocked(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")

        self.assertEqual(state.pending_field(), "business_name")
        self.assertEqual(
            _shipkia_flow_response_violation(
                agent_text=(
                    "Rates batane se pehle main aapse kuch zaroori details jaan leta hoon. "
                    "Aapke business ya brand ka naam kya hai?"
                ),
                customer_text="rates chahiye",
                previous_agent_text="",
                conversation_state=state,
            ),
            "",
        )
        state.seed_context(
            {
                "business_name": "Acme",
                "business_type": "D2C",
                "business_platform": "Shopify",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_shipping_rate": 40,
                "current_problem": "Support delays",
                "monthly_shipments": 1000,
            }
        )
        self.assertEqual(state.pending_field(), "pickup_location")
        self.assertEqual(
            _shipkia_flow_response_violation(
                agent_text="Aap shipments pickup kahan se karte hain?",
                customer_text="rates chahiye",
                previous_agent_text="",
                conversation_state=state,
            ),
            "",
        )
        self.assertEqual(
            _shipkia_flow_response_violation(
                agent_text="Is route par rates available hain.",
                customer_text="Noida se Delhi",
                previous_agent_text="",
                conversation_state=state,
            ),
            "unverified_route_rate_availability",
        )

        state.authorize_rate_result({"status": "success", "amount": 31.15})
        state.primary_rate_amount = 31.15
        state.rate_answer_owed = True
        self.assertEqual(
            _shipkia_flow_response_violation(
                agent_text="Pehle aap apne business ka naam batayein.",
                customer_text="Aapne rate nahi bataya",
                previous_agent_text="",
                conversation_state=state,
            ),
            "owed_rate_omitted",
        )
        self.assertEqual(
            _shipkia_flow_response_violation(
                agent_text="Sorry, verified starting rate Rs 31.15 hai, GST included.",
                customer_text="Aapne rate nahi bataya",
                previous_agent_text="",
                conversation_state=state,
            ),
            "",
        )

    async def test_v6_affirmative_only_name_reply_stays_unhandled_without_flow_correction(self):
        state = GatedConversationState(
            model_led_flow=True,
            direct_onboarding_flow=True,
        )
        state.apply_deterministic_answers(
            "Delhi to Bangalore ke rates jaanne hain",
            turn_id="route-request",
        )
        transitions = state.apply_deterministic_answers(
            "haan ji",
            turn_id="name-reply",
            previous_agent_text="Aapke business ya brand ka naam kya hai?",
        )
        semantic = state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    {
                        "field": "business_name",
                        "disposition": "answered",
                        "value": "haan ji",
                        "evidence": "haan ji",
                        "confidence": 1.0,
                    }
                ],
            },
            customer_text="haan ji",
            turn_id="name-reply-semantic",
            pending_field_at_turn_start="business_name",
        )

        violation = _shipkia_flow_response_violation(
            agent_text="Aapka business B2C hai, B2B hai, ya D2C?",
            customer_text="haan ji",
            previous_agent_text="Aapke business ya brand ka naam kya hai?",
            conversation_state=state,
        )

        self.assertEqual(violation, "")
        self.assertEqual(transitions, [])
        self.assertEqual(semantic, [])
        self.assertEqual(
            state.next_route_for_lookup(),
            {"pickup_location": "Delhi", "delivery_location": "Bengaluru"},
        )
        self.assertEqual(state.pending_field(), "business_name")
        self.assertFalse(state.is_handled("business_name"))

    def test_call_2011_acknowledged_problem_is_not_treated_as_a_reasked_question(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("rates check karna hai", turn_id="intent")
        state.mark_pricing_verified("lookup_pincode_serviceability")
        state.seed_context(
            {
                "business_name": "Harsh Enterprises",
                "business_type": "D2C",
                "business_platform": "Website",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_shipping_rate": 35,
                "current_problem": "Support mein problem hai",
            }
        )

        violation = _shipkia_flow_response_violation(
            agent_text=(
                "Support mein problem aa rahi hai, theek hai. ShipKia mein dedicated account "
                "manager support karta hai. Aapki monthly shipments kitni hoti hain?"
            ),
            customer_text="Mujhe support mein problem hai.",
            previous_agent_text="Shiprocket ke saath kya problem aa rahi hai?",
            conversation_state=state,
        )

        self.assertEqual(violation, "")

    def test_call_2011_business_platform_question_matches_pending_field(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("rates check karna hai", turn_id="intent")
        state.mark_pricing_verified("lookup_pincode_serviceability")
        state.seed_context({"business_name": "Harsh Enterprises", "business_type": "D2C"})

        violation = _shipkia_flow_response_violation(
            agent_text=(
                "Aap orders kahan se receive karte hain, jaise Shopify, marketplace, ya apni website?"
            ),
            customer_text="D2C",
            previous_agent_text="Aapka business B2C, B2B ya D2C hai?",
            conversation_state=state,
        )

        self.assertEqual(state.pending_field(), "business_platform")
        self.assertEqual(violation, "")

    def test_v6_advisory_flow_violation_does_not_start_a_correction_generator(self):
        self.assertFalse(
            _flow_violation_requires_correction(
                "reasked_handled:current_problem",
                model_led_flow=True,
            )
        )
        self.assertFalse(
            _flow_violation_requires_correction(
                "repeated_move_forward",
                model_led_flow=True,
            )
        )
        self.assertTrue(
            _flow_violation_requires_correction(
                "unauthorized_onboarding_link",
                model_led_flow=True,
            )
        )
        self.assertTrue(
            _flow_violation_requires_correction(
                "reasked_handled:current_problem",
                model_led_flow=False,
            )
        )

    async def test_route_correction_keeps_only_the_last_complete_pair(self):
        state = GatedConversationState(
            model_led_flow=True,
            direct_onboarding_flow=True,
        )

        state.apply_deterministic_answers(
            "Delhi to Bangalore nahi jana, Delhi to Kerala jana hai",
            turn_id="corrected-route",
        )

        self.assertEqual(
            state.requested_routes,
            [{"pickup_location": "Delhi", "delivery_location": "Kerala"}],
        )
        self.assertEqual(
            state.next_route_for_lookup(),
            {"pickup_location": "Delhi", "delivery_location": "Kerala"},
        )

    async def test_pending_provider_question_preserves_mixed_script_brand_generically(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            model_led_flow=True,
            direct_onboarding_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.seed_context(
            {
                "business_name": "Harsh Enterprises",
                "business_type": "D2C",
                "business_platform": "Website",
            }
        )
        self.assertEqual(state.pending_field(), "current_shipping_arrangement")

        state.apply_deterministic_answers(
            "Shiv रॉकेट",
            turn_id="provider",
            previous_agent_text="Abhi aap shipping ke liye kya use karte hain?",
        )

        self.assertEqual(state.value("current_shipping_arrangement"), "Other")
        self.assertEqual(state.value("current_provider_name"), "shiv रॉकेट")
        self.assertEqual(state.pending_field(), "current_shipping_rate")

    async def test_v6_generic_starting_rate_cannot_replace_retained_city_route(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            model_led_flow=True,
            direct_onboarding_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.seed_context(
            {
                "business_name": "Acme",
                "business_type": "D2C",
                "business_platform": "Shopify",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_shipping_rate": 40,
                "current_problem": "Support delays",
                "monthly_shipments": 1000,
                "pickup_location": "Delhi",
                "delivery_location": "Bengaluru",
            }
        )
        forwarder = make_mcp_forwarder(
            "get_shipkia_starting_rate",
            "v6-retained-route-starting-guard",
            conversation_state=state,
        )
        _StartingFakeClientSession.captured_payload = None

        with patch("livekit_agent.agent.aiohttp.ClientSession", _StartingFakeClientSession):
            result = json.loads(await forwarder({}))

        self.assertEqual(result["status"], "route_lookup_required")
        self.assertFalse(result["pricing_backend_called"])
        self.assertEqual(result["route"]["pickup_location"], "Delhi")
        self.assertEqual(result["route"]["delivery_location"], "Bengaluru")
        self.assertNotIn("spoken_response_instruction", result)
        self.assertIsNone(_StartingFakeClientSession.captured_payload)

    async def test_v6_route_lookup_reuses_saved_cities_without_pincodes(self):
        state = GatedConversationState(
            model_led_flow=True,
            direct_onboarding_flow=True,
        )
        state.seed_context(
            {
                "business_name": "Acme",
                "business_type": "D2C",
                "business_platform": "Shopify",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_shipping_rate": 40,
                "current_problem": "Support delays",
                "pickup_location": "Delhi",
                "delivery_location": "Bengaluru",
            }
        )
        forwarder = make_mcp_forwarder(
            "lookup_pincode_serviceability",
            "v6-retained-city-route",
            conversation_state=state,
        )
        _RouteFakeClientSession.captured_payloads = []

        with patch("livekit_agent.agent.aiohttp.ClientSession", _RouteFakeClientSession):
            result = json.loads(await forwarder({}))
            repeated = json.loads(
                await forwarder(
                    {
                        "pickup_location": "invented",
                        "delivery_location": "invented",
                    }
                )
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["zone"], "C")
        arguments = _RouteFakeClientSession.captured_payloads[0]["params"]["arguments"]
        self.assertEqual(arguments["pickup_location"], "Delhi")
        self.assertEqual(arguments["delivery_location"], "Bengaluru")
        self.assertNotIn("pickup_pincode", arguments)
        self.assertNotIn("delivery_pincode", arguments)
        self.assertEqual(repeated["zone"], "C")
        repeated_arguments = _RouteFakeClientSession.captured_payloads[1]["params"]["arguments"]
        self.assertEqual(repeated_arguments["pickup_location"], "Delhi")
        self.assertEqual(repeated_arguments["delivery_location"], "Bengaluru")

    async def test_v6_rate_interest_cannot_be_reopened_as_permission_question(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("rates chahiye", turn_id="rates")

        self.assertEqual(
            _shipkia_flow_response_violation(
                agent_text="Kya aap shipping rates check karna chahenge?",
                customer_text="North Star mera brand hai.",
                previous_agent_text="Aapke business ka naam kya hai?",
                conversation_state=state,
            ),
            "reopened_rate_intent",
        )

    async def test_v5_flat_catalog_duplicate_is_suppressed_without_backend_call(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("slide rate bata do", turn_id="flat")
        state.mark_flat_catalog_presented()
        forwarder = make_mcp_forwarder(
            "get_shipkia_flat_rates",
            "call-1623-flat-duplicate",
            conversation_state=state,
            backend_argument_names=frozenset({"response_scope", "payment_type"}),
        )
        _FlatFakeClientSession.captured_payload = None

        with patch("livekit_agent.agent.aiohttp.ClientSession", _FlatFakeClientSession):
            result = json.loads(await forwarder({"response_scope": "All"}))

        self.assertEqual(result["status"], "duplicate_suppressed")
        self.assertFalse(result["pricing_backend_called"])
        self.assertIn("complete Flat catalog", result["spoken_response_instruction"])
        self.assertIsNone(_FlatFakeClientSession.captured_payload)

    async def test_unresolved_pincode_zone_returns_only_general_22_fallback(self):
        result = _voice_safe_pincode_serviceability_result(
            {
                "status": "configuration_required",
                "zone": "C",
                "zone_verified": False,
                "fallback_starting_rate": {
                    "status": "success",
                    "response_type": "general_starting",
                    "amount": 22.0,
                    "currency": "INR",
                    "gst_inclusive": False,
                },
            }
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["response_type"], "general_starting")
        self.assertIsNone(result["zone"])
        self.assertFalse(result["zone_verified"])
        self.assertEqual(result["amount"], 22.0)
        self.assertIn("Do not name or imply any Zone A-F", result["spoken_response_instruction"])

    async def test_route_details_required_never_becomes_configuration_failure(self):
        result = _voice_safe_pincode_serviceability_result(
            {
                "status": "route_details_required",
                "missing_fields": ["delivery_pincode_or_location"],
            }
        )

        self.assertEqual(result["status"], "route_details_required")
        self.assertEqual(result["response_type"], "route_details_required")
        self.assertIn("Ask only for the missing", result["spoken_response_instruction"])
        self.assertNotIn("temporarily unavailable", result["spoken_response_instruction"])

    async def test_verified_route_returns_zone_starting_rate_immediately(self):
        result = _voice_safe_pincode_serviceability_result(
            {
                "status": "success",
                "zone": "A",
                "zone_verified": True,
                "serviceable": True,
                "resolution_basis": "same_shipping_cluster",
                "starting_rate": {
                    "status": "success",
                    "response_type": "zone_starting",
                    "zone": "A",
                    "amount": 22.07,
                    "currency": "INR",
                    "gst_inclusive": True,
                },
            }
        )

        self.assertEqual(result["response_type"], "zone_starting")
        self.assertEqual(result["zone"], "A")
        self.assertEqual(result["amount"], 22.07)
        self.assertIn("starting rate", result["spoken_response_instruction"])

    async def test_call_1675_duplicate_verified_route_lookup_is_suppressed(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        route = {"pickup_location": "Bareilly", "delivery_location": "Bangalore"}
        state.register_requested_route(route)
        state.mark_route_zone_verified("D", starting_presented=True, route_arguments=route)
        state.authorize_rate_result(
            {"status": "success", "response_type": "zone_starting", "zone": "D", "amount": 35.05}
        )
        state.mark_starting_rate_presented()
        forwarder = make_mcp_forwarder(
            "lookup_pincode_serviceability",
            "call-1675-duplicate-route",
            conversation_state=state,
        )
        _RouteFakeClientSession.captured_payloads = []

        with patch("livekit_agent.agent.aiohttp.ClientSession", _RouteFakeClientSession):
            result = json.loads(await forwarder({"pickup_location": "invented"}))

        self.assertEqual(result["status"], "duplicate_suppressed")
        self.assertFalse(result["pricing_backend_called"])
        self.assertEqual(result["zone"], "D")
        self.assertEqual(_RouteFakeClientSession.captured_payloads, [])
        self.assertIn("already verified", result["spoken_response_instruction"])

    async def test_close_stage_blocks_pricing_tool_without_backend_call(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.move_forward_question_due = True
        forwarder = make_mcp_forwarder(
            "lookup_pincode_serviceability",
            "call-1675-close-lock",
            conversation_state=state,
        )
        _RouteFakeClientSession.captured_payloads = []

        with patch("livekit_agent.agent.aiohttp.ClientSession", _RouteFakeClientSession):
            result = json.loads(await forwarder({}))

        self.assertEqual(result["status"], "close_stage_locked")
        self.assertFalse(result["pricing_backend_called"])
        self.assertEqual(_RouteFakeClientSession.captured_payloads, [])

    async def test_call_1677_duplicate_starting_lookup_keeps_provider_options(self):
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
                    {"courier": "Amazon", "service": "Amazon Standard", "amount": 36.34},
                ],
            }
        )
        forwarder = make_mcp_forwarder(
            "get_shipkia_starting_rate",
            "call-1677-starting-duplicate",
            conversation_state=state,
        )
        _StartingFakeClientSession.captured_payload = None

        with patch("livekit_agent.agent.aiohttp.ClientSession", _StartingFakeClientSession):
            result = json.loads(await forwarder({"zone": "invented"}))

        self.assertEqual(result["status"], "duplicate_suppressed")
        self.assertFalse(result["pricing_backend_called"])
        self.assertIsNone(_StartingFakeClientSession.captured_payload)
        self.assertEqual(len(state.verified_starting_options), 2)

    def test_call_1677_guard_requires_all_requested_provider_rates(self):
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
        customer_text = "Main sabke rates individually janna chahunga."
        state.apply_deterministic_answers(customer_text, turn_id="all-provider-rates")

        incomplete = _shipkia_flow_response_violation(
            agent_text="Shree Maruti ka starting rate Rs 31.15 hai. Kya aap kuch aur jaanna chahenge?",
            customer_text=customer_text,
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
            conversation_state=state,
        )
        complete = _shipkia_flow_response_violation(
            agent_text=(
                "Starting rates: Shree Maruti Surface Rs 31.15 aur Amazon Standard "
                "Rs 36.34."
            ),
            customer_text=customer_text,
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
            conversation_state=state,
        )

        self.assertEqual(incomplete, "provider_rates_incomplete")
        self.assertEqual(complete, "")

    def test_call_1677_guard_blocks_anything_else_before_verified_rate_and_quantity(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.mark_route_zone_verified("C", starting_presented=True)
        state.authorize_rate_result(
            {"status": "success", "response_type": "zone_starting", "zone": "C", "amount": 31.15}
        )
        state.mark_pricing_verified("lookup_pincode_serviceability")

        violation = _shipkia_flow_response_violation(
            agent_text="ShipKia mein multiple courier options hain. Kya aap kuch aur jaanna chahenge?",
            customer_text="Rate zyada lag raha hai.",
            previous_agent_text="Aapko Shiprocket ke saath kya problem aa rahi hai?",
            conversation_state=state,
        )

        self.assertEqual(violation, "verified_rate_omitted")

    def test_call_1681_services_answer_requires_complete_verified_usp_set(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        customer_text = "Aap kaun-kaun si services provide karte hain?"
        state.apply_deterministic_answers(customer_text, turn_id="services")

        incomplete = _shipkia_flow_response_violation(
            agent_text="ShipKia par multiple courier partners available hain.",
            customer_text=customer_text,
            previous_agent_text="Rates check karna chahenge ya onboarding help chahiye?",
            conversation_state=state,
        )
        complete = _shipkia_flow_response_violation(
            agent_text=(
                "ShipKia multiple courier partners ke shipments manage karta hai, dedicated "
                "account manager ticketing aur support mein help karta hai, WhatsApp order "
                "confirmation ke baad no response par call confirmation available hai, aur "
                "delivery NDR ke liye WhatsApp plus IVR follow-up workflow milta hai."
            ),
            customer_text=customer_text,
            previous_agent_text="Rates check karna chahenge ya onboarding help chahiye?",
            conversation_state=state,
        )

        self.assertEqual(incomplete, "usp_ignored")
        self.assertEqual(complete, "")

    def test_call_1681_yes_to_anything_else_cannot_repeat_same_question(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.anything_else_detail_due = True

        violation = _shipkia_flow_response_violation(
            agent_text="Kya aap kuch aur jaanna chahenge?",
            customer_text="Haan ji.",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
            conversation_state=state,
        )

        self.assertEqual(violation, "anything_else_detail_not_requested")

    def test_call_1681_provider_amounts_must_match_their_own_services(self):
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
                    {"courier": "E-Kart", "service": "E-Kart SURFACE", "amount": 76.58},
                ],
            }
        )
        state.apply_deterministic_answers("sabke rates batao", turn_id="all-rates")

        swapped = _shipkia_flow_response_violation(
            agent_text=(
                "Shree Maruti Surface Rs 31.15, Amazon Standard Rs 76.58 aur "
                "E-Kart Surface Rs 36.34."
            ),
            customer_text="sabke rates batao",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
            conversation_state=state,
        )
        exact = _shipkia_flow_response_violation(
            agent_text=(
                "Shree Maruti Surface Rs 31.15, Amazon Standard Rs 36.34 aur "
                "E-Kart Surface Rs 76.58."
            ),
            customer_text="sabke rates batao",
            previous_agent_text="Kya aap kuch aur jaanna chahenge?",
            conversation_state=state,
        )

        self.assertEqual(swapped, "provider_rates_incomplete")
        self.assertEqual(exact, "")

    def test_verified_route_contradiction_is_a_flow_violation(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.mark_route_zone_verified("D", starting_presented=True)

        violation = _shipkia_flow_response_violation(
            agent_text="Zone verify nahi hua, Bareilly ka pincode bataiye.",
            customer_text="Nahi.",
            previous_agent_text="Kya aap ShipKia ke saath aage badhna chahenge?",
            conversation_state=state,
        )

        self.assertEqual(violation, "contradicted_verified_route")

    def test_route_result_preserves_verified_provider_starting_options(self):
        result = _voice_safe_pincode_serviceability_result(
            {
                "status": "success",
                "zone": "D",
                "zone_verified": True,
                "serviceable": True,
                "starting_rate": {
                    "status": "success",
                    "response_type": "zone_starting",
                    "zone": "D",
                    "amount": 35.05,
                    "currency": "INR",
                    "gst_inclusive": True,
                    "available_courier_partners": ["Shree Maruti", "Amazon"],
                    "starting_rate_options": [
                        {
                            "courier": "Shree Maruti",
                            "service": "Shree Maruti Surface",
                            "amount": 35.05,
                            "weight_slab_g": 500,
                            "movement_type": "Forward",
                            "gst_inclusive": True,
                        },
                        {
                            "courier": "Amazon",
                            "service": "Amazon Shipping Standard",
                            "amount": 38.94,
                            "weight_slab_g": 500,
                            "movement_type": "Forward",
                            "gst_inclusive": True,
                        },
                    ],
                },
            }
        )

        self.assertEqual(result["available_courier_partners"], ["Shree Maruti", "Amazon"])
        self.assertEqual(
            [option["amount"] for option in result["starting_rate_options"]],
            [35.05, 38.94],
        )

    async def test_multi_route_forwarder_uses_each_queued_route_once(self):
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
        state.apply_deterministic_answers("rates check karne hain", turn_id="intent")
        state.register_requested_route(
            {"pickup_location": "Delhi", "delivery_location": "Bengaluru"}
        )
        state.register_requested_route(
            {"pickup_location": "Noida", "delivery_location": "Delhi"}
        )
        forwarder = make_mcp_forwarder(
            "lookup_pincode_serviceability",
            "multi-route-test",
            conversation_state=state,
        )
        _RouteFakeClientSession.captured_payloads = []

        with patch("livekit_agent.agent.aiohttp.ClientSession", _RouteFakeClientSession):
            first = json.loads(await forwarder({"pickup_location": "invented"}))
            second = json.loads(await forwarder({"pickup_location": "invented"}))

        first_args = _RouteFakeClientSession.captured_payloads[0]["params"]["arguments"]
        second_args = _RouteFakeClientSession.captured_payloads[1]["params"]["arguments"]
        self.assertEqual(first_args["delivery_location"], "Bengaluru")
        self.assertEqual(second_args["pickup_location"], "Noida")
        self.assertEqual(first["zone"], "C")
        self.assertEqual(second["zone"], "A")
        self.assertEqual(first["remaining_requested_routes"], 1)
        self.assertNotIn(
            "temporarily unavailable",
            first["spoken_response_instruction"].casefold(),
        )
        self.assertEqual(state.unresolved_route_count(), 0)

    async def test_v5_garbled_pan_india_bypasses_discovery_and_calls_zone_a_lookup(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("Achchi bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rate", turn_id="intent")
        state.apply_deterministic_answers("Par India ki", turn_id="pan-india")
        forwarder = make_mcp_forwarder(
            "lookup_pincode_serviceability",
            "pan-india-asr-test",
            conversation_state=state,
        )
        _RouteFakeClientSession.captured_payloads = []

        with patch("livekit_agent.agent.aiohttp.ClientSession", _RouteFakeClientSession):
            result = json.loads(await forwarder({}))

        arguments = _RouteFakeClientSession.captured_payloads[0]["params"]["arguments"]
        self.assertEqual(arguments, {"pan_india": True})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["zone"], "A")
        self.assertEqual(result["amount"], 22.07)

    async def test_v5_route_lookup_is_blocked_until_provider_problem_is_handled(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context(
            {
                "business_name": "H Enterprises",
                "business_type": "B2C",
                "current_shipping_arrangement": "Shipping Aggregator",
                "current_provider_name": "Shiprocket",
                "current_shipping_rate": 25,
            }
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rate", turn_id="intent")
        state.apply_deterministic_answers("Delhi to Noida", turn_id="route")
        forwarder = make_mcp_forwarder(
            "lookup_pincode_serviceability",
            "problem-boundary-test",
            conversation_state=state,
        )
        _RouteFakeClientSession.captured_payloads = []

        with patch("livekit_agent.agent.aiohttp.ClientSession", _RouteFakeClientSession):
            blocked = json.loads(await forwarder({}))

        self.assertEqual(blocked["status"], "qualification_required")
        self.assertEqual(
            blocked["required_next_question"],
            "main problem with the current shipping arrangement",
        )
        self.assertTrue(blocked["route_retained"])
        self.assertEqual(_RouteFakeClientSession.captured_payloads, [])

        state.apply_decision(
            field="current_problem",
            disposition="answered",
            value="Support issue",
            evidence="support issue",
            confidence=1.0,
            customer_text="support issue",
            turn_id="problem",
        )
        with patch("livekit_agent.agent.aiohttp.ClientSession", _RouteFakeClientSession):
            result = json.loads(await forwarder({}))

        arguments = _RouteFakeClientSession.captured_payloads[0]["params"]["arguments"]
        self.assertEqual(
            arguments,
            {"pickup_location": "Delhi", "delivery_location": "Noida", "pan_india": False},
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["zone"], "A")

    async def test_call_1681_repeated_blocked_route_lookup_is_suppressed(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Delhi to Mumbai rate", turn_id="intent-route")
        runtime = _Runtime()
        forwarder = make_mcp_forwarder(
            "lookup_pincode_serviceability",
            "call-1681-blocked-loop",
            runtime=runtime,
            conversation_state=state,
        )
        _RouteFakeClientSession.captured_payloads = []

        with patch("livekit_agent.agent.aiohttp.ClientSession", _RouteFakeClientSession):
            first = json.loads(await forwarder({}))
            repeated = json.loads(await forwarder({}))

        self.assertEqual(first["status"], "qualification_required")
        self.assertEqual(repeated["status"], "duplicate_suppressed")
        self.assertEqual(_RouteFakeClientSession.captured_payloads, [])
        self.assertEqual(len(runtime.tool_outcomes), 1)
        self.assertIn("Do not call this tool again", repeated["spoken_response_instruction"])

    async def test_v5_partial_route_lookup_is_blocked_before_backend_call(self):
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
        runtime = Mock()
        runtime.expect_realtime_tool_reply = Mock()
        forwarder = make_mcp_forwarder(
            "lookup_pincode_serviceability",
            "partial-route-test",
            runtime=runtime,
            conversation_state=state,
        )
        _RouteFakeClientSession.captured_payloads = []

        with patch("livekit_agent.agent.aiohttp.ClientSession", _RouteFakeClientSession):
            result = json.loads(
                await forwarder(
                    {"pickup_location": "Delhi", "delivery_location": "Mumbai"}
                )
            )

        self.assertEqual(result["status"], "route_details_required")
        self.assertEqual(result["required_next_question"], "delivery city or locality")
        runtime.expect_realtime_tool_reply.assert_not_called()
        self.assertEqual(_RouteFakeClientSession.captured_payloads, [])
        self.assertNotIn("unavailable", result["spoken_response_instruction"].casefold())

    async def test_processor_skips_semantic_spillover_after_deterministic_pending_answer(self):
        class _SpilloverGuard:
            def __init__(self):
                self.last_kwargs = {}

            async def classify(self, **kwargs):
                self.last_kwargs = kwargs
                return {
                    "turn_disposition": "answered",
                    "decisions": [
                        {
                            "field": "current_shipping_arrangement",
                            "disposition": "unknown",
                            "value": "Unknown",
                            "evidence": "B2C",
                            "confidence": 0.90,
                        }
                    ],
                }

        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"business_name": "Harsh Enterprises"})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates", turn_id="intent")
        guard = _SpilloverGuard()
        processor = GuardedTurnProcessor(
            conversation_state=state,
            answer_guard=guard,
            runtime=_Runtime(),
        )

        await processor.process("B2C", turn_id="business-type")

        self.assertEqual(guard.last_kwargs, {})
        self.assertEqual(state.value("business_type"), "B2C")
        self.assertFalse(state.is_handled("current_shipping_arrangement"))
        self.assertEqual(state.pending_field(), "current_shipping_arrangement")

    async def test_v5_direct_flat_catalog_forces_all_prepaid_without_inputs(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("flat rates batao", turn_id="flat-request")
        forwarder = make_mcp_forwarder(
            "get_shipkia_flat_rates",
            "v5-direct-flat-test",
            conversation_state=state,
            backend_argument_names=frozenset(
                {"response_scope", "payment_type", "dead_weight", "order_value"}
            ),
        )

        with patch("livekit_agent.agent.aiohttp.ClientSession", _FlatFakeClientSession):
            result = json.loads(await forwarder({"response_scope": "Starting"}))

        arguments = _FlatFakeClientSession.captured_payload["params"]["arguments"]
        self.assertEqual(arguments, {"response_scope": "All", "payment_type": "Prepaid"})
        self.assertEqual(result["status"], "success")
        self.assertIn("exactly two verified Flat-related options", result["spoken_response_instruction"])
        self.assertIn("Shadowfax Surface 5 KG", result["spoken_response_instruction"])
        self.assertTrue(state.flat_catalog_delivery_due)
        self.assertFalse(state.flat_catalog_due())

        complete = (
            "E-Kart Surface ke complete flat slabs Rs 76.58, Rs 88.26 aur Rs 103.84 hain. "
            "Shadowfax Surface 5 KG ka additional 1000 gram amount Rs 11.68 hai; iska base "
            "shipment rate zonal hai."
        )
        self.assertTrue(_flat_catalog_response_complete(complete, state))
        self.assertEqual(
            _shipkia_flow_response_violation(
                agent_text="E-Kart Surface ke flat slabs Rs 76.58, Rs 88.26 aur Rs 103.84 hain.",
                customer_text="flat rates batao",
                previous_agent_text="",
                conversation_state=state,
            ),
            "flat_catalog_omitted",
        )

    async def test_v5_model_cannot_switch_normal_call_to_flat_without_customer_evidence(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers(
            "shipping rates check karne hain",
            turn_id="rates",
        )
        self.assertEqual(state.requested_rate_type, "Normal")
        forwarder = make_mcp_forwarder(
            "get_shipkia_flat_rates",
            "v5-realtime-flat-test",
            conversation_state=state,
            backend_argument_names=frozenset({"response_scope", "payment_type"}),
        )

        with patch("livekit_agent.agent.aiohttp.ClientSession", _FlatFakeClientSession):
            result = json.loads(await forwarder({}))

        self.assertEqual(result["status"], "flat_rate_not_requested")
        self.assertFalse(result["pricing_backend_called"])
        self.assertEqual(state.requested_rate_type, "Normal")
        self.assertFalse(state.flat_catalog_presented)

    async def test_v6_model_cannot_switch_generic_rate_request_to_flat(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("shipping rates batao", turn_id="rates")
        state.seed_context(
            {
                "business_name": "Raj Enterprises",
                "business_type": "D2C",
                "business_platform": "Website",
                "current_shipping_arrangement": "Not Applicable",
                "current_shipping_rate": "Not Applicable",
                "current_problem": "Not Applicable",
            }
        )
        state.optional_ended_by = "business_name"
        forwarder = make_mcp_forwarder(
            "get_shipkia_flat_rates",
            "v6-generic-is-not-flat",
            conversation_state=state,
        )
        _FlatFakeClientSession.captured_payload = None

        with patch("livekit_agent.agent.aiohttp.ClientSession", _FlatFakeClientSession):
            result = json.loads(await forwarder({}))

        self.assertEqual(result["status"], "flat_rate_not_requested")
        self.assertFalse(result["pricing_backend_called"])
        self.assertIsNone(_FlatFakeClientSession.captured_payload)

    async def test_v6_explicit_flat_asr_request_runs_after_close_stage(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji", turn_id="consent")
        state.apply_deterministic_answers("shipping rates batao", turn_id="rates")
        state.seed_context(
            {
                "business_name": "Raj Enterprises",
                "business_type": "D2C",
                "business_platform": "Website",
                "current_shipping_arrangement": "Not Applicable",
                "current_shipping_rate": "Not Applicable",
                "current_problem": "Not Applicable",
                "monthly_shipments": 1500,
            }
        )
        state.optional_ended_by = "business_name"
        state.move_forward_decision = "Yes"
        state.onboarding_link_presented = True
        state.apply_deterministic_answers("Play it rate toh batao", turn_id="flat-after-close")
        forwarder = make_mcp_forwarder(
            "get_shipkia_flat_rates",
            "v6-flat-after-close",
            conversation_state=state,
        )
        _FlatFakeClientSession.captured_payload = None

        with patch("livekit_agent.agent.aiohttp.ClientSession", _FlatFakeClientSession):
            result = json.loads(await forwarder({}))

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["pricing_backend_called"])
        self.assertIsNotNone(_FlatFakeClientSession.captured_payload)

    async def test_post_volume_turn_blocks_multiple_pricing_tools_before_backend(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("flat rates batao", turn_id="flat")
        state.mark_flat_catalog_presented()
        state.mark_pricing_verified("get_shipkia_flat_rates")
        state.apply_deterministic_answers(
            "2000",
            turn_id="volume",
            previous_agent_text="Aapki approximate monthly shipment quantity kitni hai?",
        )
        self.assertTrue(state.pricing_close_locked())

        flat = make_mcp_forwarder(
            "get_shipkia_flat_rates",
            "post-volume-flat",
            conversation_state=state,
        )
        zonal = make_mcp_forwarder(
            "get_shipkia_flat_zonal_rates",
            "post-volume-flat-zonal",
            conversation_state=state,
        )
        _FlatFakeClientSession.captured_payload = None
        with patch("livekit_agent.agent.aiohttp.ClientSession", _FlatFakeClientSession):
            flat_result = json.loads(await flat({}))
            zonal_result = json.loads(await zonal({}))

        self.assertEqual(flat_result["status"], "close_stage_locked")
        self.assertEqual(zonal_result["status"], "close_stage_locked")
        self.assertFalse(flat_result["pricing_backend_called"])
        self.assertFalse(zonal_result["pricing_backend_called"])
        self.assertIsNone(_FlatFakeClientSession.captured_payload)

    async def test_v5_explicit_flat_zonal_uses_only_flat_zonal_catalog(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Rflat Zonal", turn_id="flat-zonal")
        forwarder = make_mcp_forwarder(
            "get_shipkia_flat_zonal_rates",
            "v5-flat-zonal-test",
            conversation_state=state,
        )

        with patch(
            "livekit_agent.agent.aiohttp.ClientSession",
            _FlatZonalFakeClientSession,
        ):
            result = json.loads(await forwarder({"payment_type": "COD"}))

        arguments = _FlatZonalFakeClientSession.captured_payload["params"]["arguments"]
        self.assertEqual(arguments, {"payment_type": "Prepaid"})
        self.assertEqual(result["status"], "success")
        self.assertIn("Zones A-B Rs 84.37", result["spoken_response_instruction"])
        self.assertTrue(state.flat_zonal_catalog_presented)
        self.assertTrue(state.flat_zonal_catalog_delivery_due)
        self.assertEqual(state.flat_zonal_additional_total, 38.94)

    async def test_latest_call_flat_zonal_is_complete_once_without_permission_question(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("flat zonal rates batao", turn_id="flat-zonal")
        state.seed_context({"monthly_shipments": 1000})
        forwarder = make_mcp_forwarder(
            "get_shipkia_flat_zonal_rates",
            "latest-call-flat-zonal-test",
            conversation_state=state,
        )

        with patch(
            "livekit_agent.agent.aiohttp.ClientSession",
            _FlatZonalFakeClientSession,
        ):
            result = json.loads(await forwarder({}))

        instruction = result["spoken_response_instruction"]
        self.assertIn("Zones A-B Rs 84.37", instruction)
        self.assertIn("Zones C-F Rs 109.03", instruction)
        self.assertIn("Rs 38.94", instruction)
        self.assertNotIn("Kya aap kuch aur", instruction)
        self.assertNotIn("Kya aap inmein se kisi ke rates", instruction)
        self.assertIn("Do not ask whether they want the rates", instruction)
        self.assertIn("stop without another question", result["post_rate_instruction"])

        incomplete = (
            "E-Kart Express ke Flat-Zonal rates Zones A-B aur C-F ke liye alag hain. "
            "Kya aap inmein se kisi ke rates jaanna chahte hain?"
        )
        self.assertEqual(
            _shipkia_flow_response_violation(
                agent_text=incomplete,
                customer_text="flat zonal rates batao",
                previous_agent_text="",
                conversation_state=state,
            ),
            "flat_zonal_catalog_omitted",
        )
        complete = (
            "E-Kart Express Flat-Zonal mein Zones A-B ka 500 gram rate Rs 84.37, "
            "Zones C-F ka Rs 109.03, aur additional 500 gram Rs 38.94 hai."
        )
        self.assertTrue(_flat_zonal_catalog_response_complete(complete, state))
        self.assertEqual(
            _shipkia_flow_response_violation(
                agent_text=complete,
                customer_text="flat zonal rates batao",
                previous_agent_text="",
                conversation_state=state,
            ),
            "",
        )
        state.mark_flat_zonal_catalog_delivered()
        self.assertFalse(state.flat_zonal_catalog_delivery_due)

    async def test_v5_model_cannot_infer_flat_zonal_from_ambiguous_speech(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("shipping rates chahiye", turn_id="rates")
        forwarder = make_mcp_forwarder(
            "get_shipkia_flat_zonal_rates",
            "v5-flat-zonal-block-test",
            conversation_state=state,
        )

        result = json.loads(await forwarder({}))

        self.assertEqual(result["status"], "flat_zonal_rate_not_requested")
        self.assertFalse(result["pricing_backend_called"])
        self.assertEqual(state.requested_rate_type, "Normal")

    def test_voice_flat_zonal_result_never_calls_it_all_zone_flat(self):
        result = _voice_flat_zonal_catalog_result(
            {
                "status": "success",
                "service": "E-Kart EXPRESS",
                "zone_groups": [
                    {"zone_group": "A-B", "total": 84.37},
                    {"zone_group": "C-F", "total": 109.03},
                ],
                "additional_weight": {"additional_weight_unit_g": 500, "total": 38.94},
            }
        )

        self.assertEqual(result["response_type"], "flat_zonal_all")
        self.assertIn("differs between groups", result["spoken_response_instruction"])
        self.assertIn("Do not call this all-zone Flat pricing", result["spoken_response_instruction"])

    async def test_v5_explicit_zone_forwards_directly_to_starting_rate(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone C ka rate batao", turn_id="zone-request")
        forwarder = make_mcp_forwarder(
            "get_shipkia_starting_rate",
            "v5-direct-zone-test",
            conversation_state=state,
        )

        with patch("livekit_agent.agent.aiohttp.ClientSession", _StartingFakeClientSession):
            result = json.loads(await forwarder({"zone": "A"}))

        arguments = _StartingFakeClientSession.captured_payload["params"]["arguments"]
        self.assertEqual(arguments, {"zone": "C"})
        self.assertEqual(result["status"], "success")
        self.assertFalse(state.starting_rate_due())

    async def test_v6_explicit_zone_uses_customer_zone_and_returns_rate_before_volume(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("Zone D ke rates batao", turn_id="zone-request")
        state.optional_ended_by = "business_name"
        forwarder = make_mcp_forwarder(
            "get_shipkia_starting_rate",
            "v6-direct-zone-test",
            conversation_state=state,
        )
        _StartingFakeClientSession.captured_payload = None

        with patch("livekit_agent.agent.aiohttp.ClientSession", _StartingFakeClientSession):
            result = json.loads(await forwarder({"zone": "A"}))

        arguments = _StartingFakeClientSession.captured_payload["params"]["arguments"]
        self.assertEqual(arguments, {"zone": "D"})
        self.assertEqual(result["status"], "success")
        self.assertIn("monthly shipment quantity", result["post_rate_instruction"])

    async def test_v6_explicit_zone_after_close_reaches_pricing_backend(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates chahiye", turn_id="intent")
        state.optional_ended_by = "business_name"
        state.move_forward_decision = "Yes"
        state.onboarding_link_presented = True
        state.apply_deterministic_answers(
            "Zone D ke rates batao",
            turn_id="zone-after-close",
        )
        forwarder = make_mcp_forwarder(
            "get_shipkia_starting_rate",
            "v6-zone-after-close",
            conversation_state=state,
        )
        _StartingFakeClientSession.captured_payload = None

        with patch("livekit_agent.agent.aiohttp.ClientSession", _StartingFakeClientSession):
            result = json.loads(await forwarder({"zone": "A"}))

        arguments = _StartingFakeClientSession.captured_payload["params"]["arguments"]
        self.assertEqual(arguments, {"zone": "D"})
        self.assertEqual(result["status"], "success")

    async def test_active_tools_expose_only_the_authorized_pricing_path(self):
        assistant = object.__new__(ShipKiaAssistant)
        assistant._conversation_state = GatedConversationState()
        assistant._tools_by_name = {
            "record_shipkia_call_progress": "record",
            "calculate_shipkia_rate": "normal",
            "get_shipkia_flat_rates": "flat",
            "get_shipkia_flat_zonal_rates": "flat-zonal",
            "get_shipkia_starting_rate": "starting",
        }
        assistant._rate_tool_enabled = False
        assistant._flat_tool_enabled = False
        assistant._flat_zonal_tool_enabled = False
        assistant._starting_tool_enabled = False
        self.assertEqual(assistant._active_tools(), ["record"])

        assistant._flat_tool_enabled = True
        self.assertEqual(assistant._active_tools(), ["record", "flat"])
        assistant._flat_tool_enabled = False
        assistant._flat_zonal_tool_enabled = True
        self.assertEqual(assistant._active_tools(), ["record", "flat-zonal"])
        assistant._flat_zonal_tool_enabled = False
        assistant._starting_tool_enabled = True
        self.assertEqual(assistant._active_tools(), ["record", "starting"])
        assistant._starting_tool_enabled = False
        assistant._rate_tool_enabled = True
        self.assertEqual(assistant._active_tools(), ["record", "normal"])

    async def test_v5_keeps_guarded_pricing_schemas_visible_during_realtime_turns(self):
        assistant = object.__new__(ShipKiaAssistant)
        assistant._conversation_state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
        )
        assistant._tools_by_name = {
            "record_shipkia_call_progress": "record",
            "calculate_shipkia_rate": "normal",
            "get_shipkia_flat_rates": "flat",
            "get_shipkia_flat_zonal_rates": "flat-zonal",
            "get_shipkia_starting_rate": "starting",
        }
        assistant._rate_tool_enabled = False
        assistant._flat_tool_enabled = False
        assistant._flat_zonal_tool_enabled = False
        assistant._starting_tool_enabled = False

        self.assertEqual(
            assistant._active_tools(),
            ["record", "normal", "flat", "flat-zonal", "starting"],
        )

    async def test_v5_both_catalogs_can_be_called_in_sequence(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("E-Kart ke rates bata do", turn_id="ekart")
        state.apply_deterministic_answers(
            "dono ke bata do",
            turn_id="both",
            previous_agent_text=(
                "E-Kart Surface ke Flat rates chahiye ya E-Kart Express ke Flat-Zonal rates?"
            ),
        )
        flat_forwarder = make_mcp_forwarder(
            "get_shipkia_flat_rates",
            "v5-both-flat-test",
            conversation_state=state,
            backend_argument_names=frozenset({"response_scope", "payment_type"}),
        )
        zonal_forwarder = make_mcp_forwarder(
            "get_shipkia_flat_zonal_rates",
            "v5-both-zonal-test",
            conversation_state=state,
        )

        with patch("livekit_agent.agent.aiohttp.ClientSession", _FlatFakeClientSession):
            flat_result = json.loads(await flat_forwarder({}))
        self.assertEqual(flat_result["status"], "success")
        self.assertIn("get_shipkia_flat_zonal_rates immediately", flat_result["spoken_response_instruction"])
        self.assertTrue(state.flat_zonal_catalog_due())

        with patch("livekit_agent.agent.aiohttp.ClientSession", _FlatZonalFakeClientSession):
            zonal_result = json.loads(await zonal_forwarder({}))
        self.assertEqual(zonal_result["status"], "success")
        self.assertFalse(state.flat_zonal_catalog_due())

    async def test_v5_call_1617_flat_zonal_success_suppresses_duplicate_calls(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("flat rates batao", turn_id="flat")
        state.mark_flat_catalog_presented()
        state.apply_deterministic_answers(
            "aur E-Card Express ke rate dono rate",
            turn_id="express",
        )
        forwarder = make_mcp_forwarder(
            "get_shipkia_flat_zonal_rates",
            "v5-call-1617-flat-zonal",
            conversation_state=state,
        )

        with patch("livekit_agent.agent.aiohttp.ClientSession", _FlatZonalFakeClientSession):
            first = json.loads(await forwarder({}))
        second = json.loads(await forwarder({}))

        self.assertEqual(first["status"], "success")
        self.assertIn("Zones A-B Rs 84.37", first["spoken_response_instruction"])
        self.assertIn("Zones C-F", first["spoken_response_instruction"])
        self.assertEqual(second["status"], "duplicate_suppressed")
        self.assertIn("do not ask which zone group", second["spoken_response_instruction"])

    async def test_v5_shadowfax_surface_starting_rate_uses_trusted_zone(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.seed_context({"zone": "C"})
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers(
            "Shadowfax Surface ka rate batao",
            turn_id="shadowfax",
        )
        forwarder = make_mcp_forwarder(
            "get_shipkia_starting_rate",
            "v5-shadowfax-surface",
            conversation_state=state,
            backend_argument_names=frozenset(
                {"zone", "courier_partner", "transport_mode"}
            ),
        )

        with patch(
            "livekit_agent.agent.aiohttp.ClientSession",
            _ShadowfaxStartingFakeClientSession,
        ):
            result = json.loads(await forwarder({"zone": "A"}))

        arguments = _ShadowfaxStartingFakeClientSession.captured_payload["params"]["arguments"]
        self.assertEqual(
            arguments,
            {
                "zone": "C",
                "courier_partner": "Shadowfax",
                "transport_mode": "Surface",
            },
        )
        self.assertEqual(result["status"], "success")
        self.assertIn("Shadowfax Surface 500 G", result["spoken_response_instruction"])
        self.assertIn("Rs 76.58", result["spoken_response_instruction"])
        self.assertFalse(state.shadowfax_surface_rate_due)

    async def test_v4_normal_rate_request_cannot_call_flat_tool(self):
        state = GatedConversationState(v4_strict_flow=True)
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("normal rates check karne hain", turn_id="intent")
        _FlatFakeClientSession.captured_payload = None
        forwarder = make_mcp_forwarder(
            "get_shipkia_flat_rates",
            "v4-normal-rate-test",
            conversation_state=state,
            backend_argument_names=frozenset({"response_scope"}),
        )

        with patch(
            "livekit_agent.agent.aiohttp.ClientSession",
            _FlatFakeClientSession,
        ):
            result = json.loads(await forwarder({"response_scope": "Starting"}))

        self.assertEqual(result["status"], "flat_rate_not_requested")
        self.assertFalse(result["pricing_backend_called"])
        self.assertIsNone(_FlatFakeClientSession.captured_payload)
        self.assertNotIn("76.58", result["spoken_response_instruction"])

    async def test_v4_flat_request_cannot_call_normal_calculator(self):
        state = GatedConversationState(v4_strict_flow=True)
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("flat rate check karna hai", turn_id="intent")
        forwarder = make_mcp_forwarder(
            "calculate_shipkia_rate",
            "v4-flat-normal-tool-test",
            conversation_state=state,
        )

        result = json.loads(await forwarder({}))

        self.assertEqual(result["status"], "normal_rate_not_requested")
        self.assertFalse(result["pricing_backend_called"])
        self.assertNotIn("22", result["spoken_response_instruction"])

    async def test_v4_flat_tool_collects_requirements_before_quoting(self):
        state = GatedConversationState()
        _FlatFakeClientSession.captured_payload = None
        forwarder = make_mcp_forwarder(
            "get_shipkia_flat_rates",
            "v4-flat-qualification-test",
            conversation_state=state,
            backend_argument_names=frozenset({"response_scope"}),
        )

        with patch(
            "livekit_agent.agent.aiohttp.ClientSession",
            _FlatFakeClientSession,
        ):
            result = json.loads(await forwarder({"response_scope": "Starting"}))

        self.assertEqual(result["status"], "qualification_required")
        self.assertEqual(result["next_missing_field"], "business_name")
        self.assertFalse(result["pricing_backend_called"])
        self.assertIsNone(_FlatFakeClientSession.captured_payload)
        self.assertIn("business or brand name", result["spoken_response_instruction"])
        self.assertNotIn("76.58", result["spoken_response_instruction"])

    async def test_v4_flat_tool_forwards_without_unknown_function_error(self):
        state = GatedConversationState(v4_strict_flow=True)
        state.seed_context(
            {
                "business_name": "Work Shop",
                "business_type": "D2C",
                "current_shipping_arrangement": "Own Arrangement",
                "current_shipping_rate": 40,
                "current_problem": "High Rates",
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
            }
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("flat rate check karna hai", turn_id="intent")
        forwarder = make_mcp_forwarder(
            "get_shipkia_flat_rates",
            "v4-flat-test",
            conversation_state=state,
            backend_argument_names=frozenset(
                {"response_scope", "dead_weight", "payment_type"}
            ),
        )

        with patch(
            "livekit_agent.agent.aiohttp.ClientSession",
            _FlatFakeClientSession,
        ):
            result = json.loads(await forwarder({"response_scope": "Matching"}))

        self.assertEqual(
            _FlatFakeClientSession.captured_payload["params"]["name"],
            "get_shipkia_flat_rates",
        )
        self.assertEqual(result["status"], "success")
        self.assertNotIn(
            "pickup_pincode", _FlatFakeClientSession.captured_payload["params"]["arguments"]
        )
        self.assertNotIn(
            "delivery_pincode", _FlatFakeClientSession.captured_payload["params"]["arguments"]
        )
        self.assertIn("exactly two verified Flat-related options", result["spoken_response_instruction"])
        self.assertIn("Shadowfax Surface 5 KG", result["spoken_response_instruction"])
        self.assertIn(
            "anything else",
            result["spoken_response_instruction"],
        )

    async def test_v4_flat_catalog_starting_response_asks_if_more_help_is_needed(self):
        state = GatedConversationState()
        result = _voice_flat_catalog_result(
            {
                "status": "success",
                "response_type": "flat_starting",
                "response_scope": "Starting",
                "currency": "INR",
                "payment_type": "Prepaid",
                "courier_partner": "E-Kart",
                "service": "E-Kart SURFACE",
                "starting_flat_rate": {
                    "min_weight_g": 0,
                    "max_weight_g": 500,
                    "shipping_charge": 64.9,
                    "cod_charge": 0.0,
                    "gst": 11.68,
                    "total": 76.58,
                },
                "flat_rate_options": [],
                "verified_flat_rate_count": 3,
            },
            conversation_state=state,
        )

        self.assertEqual(result["status"], "success")
        self.assertIn("Rs 76.58 se start hota hai", result["spoken_response_instruction"])
        self.assertIn("anything else", result["spoken_response_instruction"])
        self.assertFalse(result["excluded_additional_weight_components"])

    async def test_v6_later_flat_catalog_never_appends_premature_move_forward(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.seed_context({"monthly_shipments": 2000})
        # Reproduce the realtime race: the previous route rate is verified,
        # while async extraction has not yet raised post_rate_followup_active.
        state.verified_pricing_tool = "get_shipkia_starting_rate"
        state.post_rate_followup_active = False
        result = _voice_flat_catalog_result(
            {
                "status": "success",
                "response_type": "flat_starting",
                "response_scope": "Starting",
                "currency": "INR",
                "payment_type": "Prepaid",
                "courier_partner": "E-Kart",
                "service": "E-Kart SURFACE",
                "starting_flat_rate": {"total": 76.58},
                "flat_rate_options": [],
                "verified_flat_rate_count": 3,
            },
            conversation_state=state,
        )

        instruction = result["spoken_response_instruction"]
        self.assertIn("stop without asking another question", instruction)
        self.assertNotIn("aage badhna", instruction)
        self.assertNotIn("kuch aur", instruction)

    async def test_v4_flat_both_uses_prepaid_boundary_and_explains_cod_dependency(self):
        state = GatedConversationState(v4_strict_flow=True)
        state.seed_context(
            {
                "business_name": "Work Shop",
                "business_type": "D2C",
                "current_shipping_arrangement": "Own Arrangement",
                "current_shipping_rate": 40,
                "current_problem": "High Rates",
                "dead_weight": 0.5,
                "payment_type": "Both",
            }
        )
        state.apply_deterministic_answers("haan ji", turn_id="consent")
        state.apply_deterministic_answers("flat rate check karna hai", turn_id="intent")
        forwarder = make_mcp_forwarder(
            "get_shipkia_flat_rates",
            "v4-flat-both-test",
            conversation_state=state,
            backend_argument_names=frozenset(
                {"response_scope", "dead_weight", "payment_type"}
            ),
        )

        with patch("livekit_agent.agent.aiohttp.ClientSession", _FlatFakeClientSession):
            result = json.loads(await forwarder({"response_scope": "Matching"}))

        arguments = _FlatFakeClientSession.captured_payload["params"]["arguments"]
        self.assertEqual(arguments["payment_type"], "Prepaid")
        self.assertEqual(state.value("payment_type"), "Both")
        self.assertIn("COD rate depends on order value", result["spoken_response_instruction"])

    async def test_v4_flat_catalog_cod_requires_only_order_value(self):
        result = _voice_flat_catalog_result(
            {
                "status": "order_value_required",
                "response_type": "flat_unavailable",
            }
        )

        self.assertEqual(result["response_type"], "flat_cod_order_value_required")
        self.assertTrue(result["cod_order_value_required"])
        self.assertIn("Ask only", result["spoken_response_instruction"])
        self.assertNotIn("76.58", result["spoken_response_instruction"])

    async def test_v6_cod_flat_zonal_forwards_customer_amount_to_knowledge_base(self):
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
        state.apply_deterministic_answers(
            "2000",
            turn_id="order-value",
            previous_agent_text="Aapka COD order value kitna hai?",
        )
        forwarder = make_mcp_forwarder(
            "get_shipkia_flat_zonal_rates",
            "v6-cod-flat-zonal-test",
            conversation_state=state,
            backend_argument_names=frozenset({"payment_type", "order_value"}),
        )

        with patch(
            "livekit_agent.agent.aiohttp.ClientSession",
            _FlatZonalFakeClientSession,
        ):
            result = json.loads(await forwarder({}))

        arguments = _FlatZonalFakeClientSession.captured_payload["params"]["arguments"]
        self.assertEqual(arguments, {"payment_type": "COD", "order_value": 2000})
        self.assertEqual(result["status"], "success")

    def test_v6_cod_question_is_blocked_until_customer_requests_cod(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("flat zonal rates batao", turn_id="flat-zonal")

        violation = _shipkia_flow_response_violation(
            agent_text="Kya aap COD shipments karte hain?",
            customer_text="Flat zonal rates batao",
            previous_agent_text="",
            conversation_state=state,
        )
        self.assertEqual(violation, "unsolicited_cod_question")
        self.assertTrue(
            _flow_violation_requires_correction(violation, model_led_flow=True)
        )

        state.apply_deterministic_answers(
            "COD Flat-Zonal rates batao",
            turn_id="cod-flat-zonal",
        )
        allowed = _shipkia_flow_response_violation(
            agent_text="Aapka COD order value kitna hai?",
            customer_text="COD Flat-Zonal rates batao",
            previous_agent_text="",
            conversation_state=state,
        )
        self.assertNotEqual(allowed, "unsolicited_cod_question")

    async def test_processor_passes_previous_agent_question_to_guard(self):
        state = GatedConversationState()
        guard = _AnswerGuard()
        runtime = _Runtime()
        runtime.turns = [
            {
                "role": "AGENT",
                "text": "Aap kaun sa courier ya aggregator use kar rahe hain?",
            }
        ]
        processor = GuardedTurnProcessor(
            conversation_state=state,
            answer_guard=guard,
            runtime=runtime,
        )

        await processor.process("Work Shop", turn_id="context-turn")

        self.assertEqual(
            guard.last_kwargs["previous_agent_text"],
            "Aap kaun sa courier ya aggregator use kar rahe hain?",
        )

    async def test_native_realtime_duplicate_events_update_state_once(self):
        state = GatedConversationState()
        guard = _AnswerGuard()
        processor = GuardedTurnProcessor(
            conversation_state=state,
            answer_guard=guard,
            runtime=_Runtime(),
        )

        first = processor.schedule("Work Shop", turn_id="transcript")
        second = processor.schedule("Work Shop", turn_id="conversation-item")
        self.assertIs(first, second)
        await processor.wait_latest()

        self.assertEqual(guard.calls, 1)
        self.assertEqual(state.value("business_name"), "Work Shop")
        self.assertEqual(
            len([item for item in state.transitions if item.get("field") == "business_name"]),
            1,
        )

    async def test_clear_natural_consent_uses_fast_path_without_model_round_trip(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        guard = _AnswerGuard(delay=0.5)
        processor = GuardedTurnProcessor(
            conversation_state=state,
            answer_guard=guard,
            runtime=_Runtime(),
        )

        await processor.process("हां, फ्री हूं मैं।", turn_id="natural-consent")

        self.assertEqual(guard.calls, 0)
        self.assertEqual(state.value("conversation_consent"), "Accepted")
        self.assertEqual(state.pending_field(), "assistance_intent")

    async def test_deterministic_intent_is_visible_before_native_draft_runs(self):
        state = GatedConversationState(v4_strict_flow=True, v5_company_pair_flow=True)
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        guard = _AnswerGuard(delay=0.5)
        processor = GuardedTurnProcessor(
            conversation_state=state,
            answer_guard=guard,
            runtime=_Runtime(),
        )

        processor.schedule("Theek hai, rates bata do", turn_id="rates-intent")

        self.assertEqual(state.value("assistance_intent"), "Rates")
        self.assertEqual(state.pending_field(), "business_name")
        await processor.wait_latest()
        self.assertEqual(guard.calls, 0)

    async def test_pan_india_request_skips_classifier_and_unlocks_zone_a_lookup(self):
        state = GatedConversationState(
            v4_strict_flow=True,
            v5_company_pair_flow=True,
            direct_onboarding_flow=True,
            model_led_flow=True,
        )
        state.apply_deterministic_answers("ji bataiye", turn_id="consent")
        state.apply_deterministic_answers("rates bataiye", turn_id="intent")
        state.seed_context({"business_name": "Acme"})
        guard = _AnswerGuard(delay=0.5)
        processor = GuardedTurnProcessor(
            conversation_state=state,
            answer_guard=guard,
            runtime=_Runtime(),
        )

        await processor.process("delivery Pan India hoti hai", turn_id="pan-india")

        self.assertEqual(guard.calls, 0)
        self.assertEqual(state.pending_field(), "")
        self.assertEqual(
            _authorized_controlled_reply_tools(state),
            ["lookup_pincode_serviceability"],
        )

    async def test_rate_tool_waits_for_latest_native_turn_guard(self):
        state = GatedConversationState()
        processor = GuardedTurnProcessor(
            conversation_state=state,
            answer_guard=_AnswerGuard(delay=0.02),
            runtime=_Runtime(),
        )
        processor.schedule("Work Shop", turn_id="native-turn")
        forwarder = make_mcp_forwarder(
            "calculate_shipkia_rate",
            "console",
            conversation_state=state,
            turn_processor=processor,
        )

        result = json.loads(await forwarder({}))

        self.assertEqual(state.value("business_name"), "Work Shop")
        self.assertEqual(result["next_missing_field"], "business_type")

    async def test_model_arguments_cannot_bypass_pending_business_name(self):
        state = GatedConversationState()
        forwarder = make_mcp_forwarder(
            "calculate_shipkia_rate",
            "console",
            conversation_state=state,
            backend_argument_names=frozenset(
                {"pickup_pincode", "delivery_pincode", "dead_weight", "payment_type"}
            ),
        )

        result = json.loads(
            await forwarder(
                {
                    "business_name": "Invented",
                    "business_type": "Invented",
                    "current_shipping_arrangement": "Own Arrangement",
                    "current_shipping_rate": 50,
                    "current_problem": "Rates",
                    "pickup_pincode": "110001",
                    "delivery_pincode": "400001",
                    "dead_weight": 0.5,
                    "payment_type": "Prepaid",
                }
            )
        )

        self.assertEqual(result["status"], "qualification_required")
        self.assertEqual(result["next_missing_field"], "business_name")
        self.assertFalse(result["pricing_backend_called"])
        self.assertTrue(result["must_return_to_same_pending_question"])
        self.assertIn("brand name", result["spoken_response_instruction"])

    async def test_starting_mode_blocks_calculator_and_deduplicates_audit(self):
        state = GatedConversationState()
        state.apply_deterministic_answers("business pata nahi", turn_id="optional")
        state.apply_deterministic_answers(
            "pickup pincode nahi pata",
            turn_id="pickup",
        )
        runtime = _Runtime()
        forwarder = make_mcp_forwarder(
            "calculate_shipkia_rate",
            "console",
            runtime=runtime,
            conversation_state=state,
        )

        first = json.loads(await forwarder({"dead_weight": 0.5}))
        second = json.loads(await forwarder({"dead_weight": 0.5}))

        self.assertEqual(first["status"], "starting_rate_required")
        self.assertEqual(first["pricing_mode"], "general_starting")
        self.assertIn("get_shipkia_starting_rate", first["spoken_response_instruction"])
        self.assertEqual(second["status"], "starting_rate_required")
        self.assertEqual(len(runtime.tool_outcomes), 1)

    async def test_v4_both_starting_response_never_asks_permission(self):
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
        forwarder = make_mcp_forwarder(
            "get_shipkia_starting_rate",
            "v4-both-starting-test",
            conversation_state=state,
        )

        with patch(
            "livekit_agent.agent.aiohttp.ClientSession",
            _StartingFakeClientSession,
        ):
            result = json.loads(await forwarder({}))

        instruction = result["spoken_response_instruction"]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["pricing_trigger_field"], "payment_type_both")
        self.assertIn("Prepaid rates", instruction)
        self.assertIn("Rs 22", instruction)
        self.assertIn("COD rate order value", instruction)
        self.assertIn("Do not ask for permission", instruction)

    async def test_authoritative_rate_arguments_ignore_invented_weight_alias(self):
        merged, ignored = _authoritative_rate_request_arguments(
            {
                "shipment_weight_g": 500,
                "pickup_pincode": "invented",
                "rate_request_type": "Flat",
            },
            {
                "pickup_pincode": "201305",
                "delivery_pincode": "110001",
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
            },
            frozenset(
                {"pickup_pincode", "delivery_pincode", "dead_weight", "payment_type"}
            ),
        )

        self.assertEqual(merged["dead_weight"], 0.5)
        self.assertEqual(merged["pickup_pincode"], "201305")
        self.assertEqual(merged["rate_request_type"], "Flat")
        self.assertNotIn("shipment_weight_g", merged)
        self.assertEqual(ignored, ["shipment_weight_g"])

    async def test_payment_mode_is_never_forwarded_as_service_filter(self):
        for payment_value in ("COD", "Prepaid", "Both", "दोनों"):
            normalized, request_type = _normalize_rate_request_arguments(
                {
                    "service": payment_value,
                    "rate_request_type": "Normal",
                }
            )
            self.assertEqual(request_type, "Normal")
            self.assertNotIn("service", normalized)

    async def test_call_1411_weight_alias_reaches_pricing_backend(self):
        state = GatedConversationState()
        state.seed_context(
            {
                "business_name": "Bookso",
                "business_type": "B2C",
                "current_shipping_arrangement": "Own Arrangement",
                "current_shipping_rate": 40,
                "current_problem": "No Problem",
                "pickup_pincode": "201305",
                "delivery_pincode": "110001",
                "dead_weight": 0.5,
                "payment_type": "COD",
                "order_value": 5000,
            }
        )
        self.assertTrue(state.pricing_ready())
        forwarder = make_mcp_forwarder(
            "calculate_shipkia_rate",
            "console",
            conversation_state=state,
            backend_argument_names=frozenset(
                {
                    "pickup_pincode",
                    "delivery_pincode",
                    "dead_weight",
                    "payment_type",
                    "order_value",
                }
            ),
        )

        with patch(
            "livekit_agent.agent.aiohttp.ClientSession",
            _FakeClientSession,
        ):
            result = json.loads(
                await forwarder(
                    {
                        "shipment_weight_g": 500,
                        "rate_request_type": "Flat",
                        "payment_type": "COD",
                    }
                )
            )

        self.assertNotEqual(result["status"], "invalid_arguments")
        forwarded = _FakeClientSession.captured_payload["params"]["arguments"]
        self.assertEqual(forwarded["dead_weight"], 0.5)
        self.assertEqual(forwarded["payment_type"], "COD")
        self.assertEqual(forwarded["order_value"], 5000.0)
        self.assertNotIn("shipment_weight_g", forwarded)

    async def test_prepare_allows_explicit_unavailable_pin_but_not_missing_weight(self):
        backend_fields = frozenset({"delivery_pincode", "dead_weight", "payment_type"})
        prepared, _metadata, error = _prepare_rate_arguments(
            {
                "qualification_refused_field": "business_name",
                "pickup_pincode_status": "Unavailable",
                "delivery_pincode": "400001",
                "dead_weight": 1,
                "payment_type": "Prepaid",
            },
            backend_fields,
        )
        self.assertIsNone(error)
        self.assertEqual(prepared["dead_weight"], 1)
        self.assertNotIn("pickup_pincode", prepared)

        _prepared, _metadata, error = _prepare_rate_arguments(
            {
                "qualification_refused_field": "business_name",
                "pickup_pincode_status": "Unavailable",
                "delivery_pincode": "400001",
                "payment_type": "Prepaid",
            },
            backend_fields,
        )
        self.assertEqual(error["next_missing_field"], "dead_weight")

    async def test_no_pin_starting_rate_requires_exact_rate_caveat(self):
        result = _voice_safe_unknown_zone_result(
            {
                "status": "success",
                "zone_required": True,
                "payment_type": "Prepaid",
                "chargeable_weight_g": 1000,
                "eligible_rates": [
                    {
                        "courier_partner": "Courier A",
                        "service": "Surface",
                        "zone_breakdowns": {
                            "A": {"shipping_charge": 50, "gst": 9, "total": 59},
                            "F": {"shipping_charge": 90, "gst": 16.2, "total": 106.2},
                        },
                    }
                ],
            },
            route_basis={"dead_weight": 1, "payment_type": "Prepaid"},
        )

        self.assertTrue(result["verified_starting_rate_available"])
        self.assertTrue(result["pincode_unavailable_fallback"])
        self.assertFalse(result["pincodes_already_supplied"])
        self.assertIn("exact rate depends", result["message"])
        self.assertEqual(len(result["eligible_rates"]), 1)

    async def test_semantic_service_does_not_overwrite_exact_tool_service(self):
        state = GatedConversationState()
        state.apply_classifier_result(
            {
                "turn_disposition": "answered",
                "decisions": [
                    {
                        "field": "service",
                        "disposition": "answered",
                        "value": "Shadowfax Surface",
                        "evidence": "Shadowfax ka rate batao",
                        "confidence": 1.0,
                    }
                ],
            },
            customer_text="Shadowfax ka rate batao",
            turn_id="service-turn",
        )

        self.assertEqual(state.value("service"), "Shadowfax Surface")
        self.assertNotIn("service", STATE_MANAGED_RATE_FIELDS)
        self.assertNotIn("service", state.rate_arguments())

    async def test_selected_flat_component_returns_starting_rate_and_stops(self):
        result = _voice_selected_flat_service_result(
            {
                "status": "success",
                "zone_required": True,
                "payment_type": "Prepaid",
                "chargeable_weight_g": 500,
                "requested_selection": {
                    "service": "Shadowfax Surface 5 KG",
                },
                "flat_rate_options": [],
                "flat_additional_rate_options": [
                    {
                        "courier_partner": "Shadowfax",
                        "service": "Shadowfax Surface 5 KG",
                        "applies_after_weight_g": 10000,
                        "additional_weight_unit_g": 1000,
                        "flat_additional_rate_breakdown": {
                            "shipping_charge": 9.9,
                            "gst": 1.78,
                            "total": 11.68,
                        },
                    }
                ],
                "eligible_rates": [
                    {
                        "courier_partner": "Shadowfax",
                        "service": "Shadowfax Surface 5 KG",
                        "zone_breakdowns": {
                            "A": {"shipping_charge": 86.9, "gst": 15.64, "total": 102.54},
                            "F": {"shipping_charge": 207.9, "gst": 37.42, "total": 245.32},
                        },
                    }
                ],
            },
            route_basis={
                "pickup_pincode": "110001",
                "delivery_pincode": "560001",
                "dead_weight": 0.5,
                "payment_type": "Prepaid",
            },
            route_validation_note_required=True,
        )

        self.assertTrue(result["current_shipment_rate_available"])
        self.assertTrue(result["current_shipment_rate_is_starting"])
        self.assertEqual(result["current_shipment_rate"]["breakdown"]["total"], 102.54)
        self.assertIsNone(result["additional_weight_condition"])
        self.assertIn("then stop", result["message"])
        self.assertIn("do not ask any follow-up question", result["message"])
