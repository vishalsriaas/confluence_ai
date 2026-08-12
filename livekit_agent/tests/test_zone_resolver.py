import unittest

from confluence_ai.services.shipkia_zones import resolve_shipkia_zone


class TestShipkiaZoneResolver(unittest.TestCase):
    def test_noida_to_delhi_pincodes_resolve_ncr_zone_a(self):
        result = resolve_shipkia_zone(
            {"pickup_pincode": "201305", "delivery_pincode": "110001"}
        )
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["zone_verified"])
        self.assertEqual(result["zone"], "A")
        self.assertEqual(result["resolution_basis"], "same_shipping_cluster")

    def test_delhi_to_noida_locations_resolve_ncr_zone_a(self):
        result = resolve_shipkia_zone(
            {"pickup_location": "Delhi", "delivery_location": "Noida"}
        )
        self.assertEqual(result["zone"], "A")

    def test_pan_india_uses_zone_a_starting_policy(self):
        result = resolve_shipkia_zone({"pan_india": True})
        self.assertEqual(result["zone"], "A")
        self.assertEqual(result["rate_scope"], "starting_only")
        self.assertEqual(result["resolution_basis"], "pan_india_zone_a_starting_policy")

    def test_delhi_to_mumbai_is_metro_zone_c(self):
        result = resolve_shipkia_zone(
            {"pickup_location": "Delhi", "delivery_location": "Mumbai"}
        )
        self.assertEqual(result["zone"], "C")

    def test_bareilly_to_delhi_is_domestic_interstate_zone_d(self):
        result = resolve_shipkia_zone(
            {"pickup_location": "Bareilly", "delivery_location": "Delhi"}
        )
        self.assertEqual(result["zone"], "D")
        self.assertEqual(result["resolution_basis"], "domestic_interstate")

    def test_unknown_valid_pincodes_still_resolve_by_postal_prefix_policy(self):
        same_region = resolve_shipkia_zone(
            {"pickup_pincode": "123456", "delivery_pincode": "129999"}
        )
        cross_region = resolve_shipkia_zone(
            {"pickup_pincode": "123456", "delivery_pincode": "823456"}
        )
        self.assertEqual(same_region["zone"], "B")
        self.assertEqual(cross_region["zone"], "D")


if __name__ == "__main__":
    unittest.main()
