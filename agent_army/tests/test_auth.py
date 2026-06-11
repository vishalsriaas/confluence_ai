import unittest

from agent_army.services.auth import sign_payload, verify_signature


class TestAuthHelpers(unittest.TestCase):
    def test_hmac_signature_roundtrip(self):
        body = '{"hello":"world"}'
        signature = sign_payload("secret", body)

        self.assertTrue(verify_signature("secret", body, signature))
        self.assertFalse(verify_signature("wrong", body, signature))
        self.assertFalse(verify_signature("secret", body, "bad"))
