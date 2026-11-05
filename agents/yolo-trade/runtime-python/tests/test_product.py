import unittest

from yolo_trade.__main__ import build_parser
from yolo_trade.product import (
    AGENT_ID, DEFAULT_PORT, DISPLAY_NAME, RUNTIME_CAPABILITIES,
)


class ProductIdentityTests(unittest.TestCase):
    def test_trading_product_identity_is_centralized(self):
        self.assertEqual(AGENT_ID, "yolo-trade")
        self.assertEqual(DISPLAY_NAME, "YoloTrade")
        self.assertEqual(DEFAULT_PORT, 8765)
        self.assertEqual(RUNTIME_CAPABILITIES.tool_schema_version, 5)
        self.assertEqual(RUNTIME_CAPABILITIES.extracted_tool_name, "create_paper_order_draft")
        self.assertTrue(RUNTIME_CAPABILITIES.pydantic_tools_factory)
        self.assertTrue(RUNTIME_CAPABILITIES.codex_dynamic_tools_factory)
        self.assertEqual(build_parser().parse_args([]).port, DEFAULT_PORT)


if __name__ == "__main__":
    unittest.main()
