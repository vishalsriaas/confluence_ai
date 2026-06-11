import unittest

import frappe

from confluence_ai.services.utils import parse_json_list, parse_json_object


class TestUtils(unittest.TestCase):
    def test_parse_json_object(self):
        self.assertEqual(parse_json_object('{"a": 1}'), {"a": 1})

    def test_parse_json_list(self):
        self.assertEqual(parse_json_list('[{"a": 1}]'), [{"a": 1}])

    def test_parse_json_object_rejects_list(self):
        with self.assertRaises(frappe.ValidationError):
            parse_json_object("[1, 2]")
