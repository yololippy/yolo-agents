import unittest

from yolo_trade.product import AGENT_ID, DISPLAY_NAME, ENGLISH_NAME


class ProductIdentityTests(unittest.TestCase):
    def test_trading_product_identity_is_centralized(self):
        self.assertEqual(AGENT_ID, "yolo-trade")
        self.assertEqual(DISPLAY_NAME, "YoloTrade")
        self.assertEqual(ENGLISH_NAME, "YoloTrade")


if __name__ == "__main__":
    unittest.main()
