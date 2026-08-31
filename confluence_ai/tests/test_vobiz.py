from __future__ import annotations

import unittest

from confluence_ai.services.vobiz import normalize_vobiz_ai_transcript_labels


class TestVobizTranscript(unittest.TestCase):
    def test_normalizes_reversed_ai_call_labels(self):
        transcript = (
            "[AGENT]: hello\n"
            "[CUSTOMER]: Namaste, main Vaani bol rahi hoon.\n"
            "[AGENT]: price kya hai?"
        )

        normalized = normalize_vobiz_ai_transcript_labels(transcript)

        self.assertEqual(
            normalized,
            "[CUSTOMER]: hello\n"
            "[AGENT]: Namaste, main Vaani bol rahi hoon.\n"
            "[CUSTOMER]: price kya hai?",
        )

    def test_leaves_unlabelled_text_unchanged(self):
        self.assertEqual(normalize_vobiz_ai_transcript_labels("plain transcript"), "plain transcript")
