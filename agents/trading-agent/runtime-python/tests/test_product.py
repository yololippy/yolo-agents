import unittest

from yolo_trading.product import AGENT_ID, DISPLAY_NAME, ENGLISH_NAME


class ProductIdentityTests(unittest.TestCase):
    def test_trading_product_identity_is_centralized(self):
        self.assertEqual(AGENT_ID, "trading-agent")
        self.assertEqual(DISPLAY_NAME, "有路交易")
        self.assertEqual(ENGLISH_NAME, "Yolo Trade")


if __name__ == "__main__":
    unittest.main()
