from __future__ import annotations

import json
from pathlib import Path
from unittest import TestCase


CASES_PATH = Path(__file__).resolve().parents[1] / "evals" / "shipkia_voice_cases.json"


class TestShipKiaVoiceEvaluationFixtures(TestCase):
    def test_fixed_evaluation_suite_has_24_required_scenarios(self):
        cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))

        self.assertEqual(len(cases), 24)
        self.assertEqual(len({case["id"] for case in cases}), 24)
        categories = {case["category"] for case in cases}
        self.assertTrue(
            {
                "opening",
                "crm_context",
                "language",
                "provider",
                "rate_comparison",
                "rate_tool",
                "claims",
                "off_topic",
                "conversation",
                "reliability",
            }.issubset(categories)
        )
        for case in cases:
            self.assertTrue(case["title"])
            self.assertTrue(case["customer_turns"])
            self.assertTrue(case["expected"])
            self.assertIsInstance(case["forbidden"], list)

        corpus = json.dumps(cases).lower()
        for required in (
            "direct courier",
            "shipping aggregator",
            "shipcart",
            "rate-lower",
            "rate-equal",
            "rate-higher",
            "dedicated-manager",
            "ticketing-support",
            "qualified-rto",
            "harmless-off-topic",
            "unsafe-off-topic",
            "silence-model-recovery",
            "disconnect-reconnect",
        ):
            self.assertIn(required, corpus)
