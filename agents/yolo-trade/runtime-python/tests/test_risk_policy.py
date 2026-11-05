"""Unit tests for the product-private, fail-closed risk policy."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from yolo_trade.risk_policy import (
    BUNDLED_RISK_POLICY_PATH, DEFAULT_RISK_POLICY, RiskPolicyConfigurationError,
    RiskPolicyError, load_risk_policy,
)


NOW = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)
CONTEXT = {"config_id": "a" * 64, "session_id": "session-1"}


def intent(symbol="MSFT", **overrides):
    value = {
        "account_selection": CONTEXT["config_id"],
        "contract": {"symbol": symbol, "sec_type": "STK", "exchange": "SMART", "currency": "USD"},
        "side": "BUY", "quantity": 10, "limit_price": "100", "tif": "DAY", "order_type": "LMT",
    }
    value.update(overrides)
    return value


def facts(symbol="MSFT", **overrides):
    timestamp = NOW.isoformat()
    value = {
        "context": dict(CONTEXT),
        "quote": {"symbol": symbol, "data_type": "live", "bid": "99", "ask": "100",
                  "last": "99.5", "close": "98", "as_of": timestamp},
        "positions": {"positions": [], "count": 0, "as_of": timestamp},
        "open_orders": {"orders": [], "count": 0, "as_of": timestamp},
        "account": {"net_liquidation": "100000", "gross_position_value": "1000", "as_of": timestamp},
    }
    value.update(overrides)
    return value


def option_sale(symbol, right, quantity=1):
    return {"contract": {"symbol": symbol, "sec_type": "OPT", "right": right},
            "side": "SELL", "quantity": quantity}


class RiskPolicyTests(unittest.TestCase):
    def assess(self, policy=DEFAULT_RISK_POLICY, request=None, snapshot=None, daily="0"):
        return policy.assess(request or intent(), snapshot or facts(),
                             daily_submitted_notional=daily,
                             expected_context=CONTEXT, now=NOW)

    def test_single_bundled_configuration_contains_requested_policy(self):
        policy = load_risk_policy()
        self.assertEqual(policy, DEFAULT_RISK_POLICY)
        self.assertEqual(policy.max_order_notional_usd, "5000")
        self.assertEqual(policy.max_projected_gross_exposure_net_liquidation_multiple, "1.3")
        self.assertEqual(policy.max_symbol_concentration_percent, "25")
        self.assertEqual(policy.max_daily_submitted_notional_usd, "10000")
        self.assertEqual(policy.max_quote_deviation_percent, "1")
        self.assertEqual(policy.max_data_age_seconds, 30)
        self.assertEqual(policy.allowed_market_data_types,
                         ("live", "frozen", "delayed", "delayed_frozen"))
        self.assertEqual(policy.monetary_limit_exempt_symbols, ("AAPL",))
        self.assertEqual(policy.short_put_allowed_underlyings, ("AAPL", "KO"))
        self.assertEqual(policy.covered_call_shares_per_contract, 100)

    def test_invalid_external_configuration_has_no_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "risk.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaises(RiskPolicyConfigurationError):
                load_risk_policy(path)

    def test_complete_external_configuration_can_replace_the_single_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            value = json.loads(BUNDLED_RISK_POLICY_PATH.read_text(encoding="utf-8"))
            value["version"] = "paper-test"
            value["limits"]["max_quote_deviation_percent"] = "2"
            path = Path(folder) / "risk.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            policy = load_risk_policy(path)
        self.assertEqual(policy.version, "paper-test")
        self.assertEqual(policy.max_quote_deviation_percent, "2")
        self.assertNotEqual(policy.policy_id, DEFAULT_RISK_POLICY.policy_id)

    def test_approved_decision_uses_dynamic_gross_limit_and_is_account_redacted(self):
        result = self.assess()
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["policy"]["version"], "paper-v1-2026-11-05")
        self.assertEqual(len(result["policy"]["policy_id"]), 64)
        self.assertEqual(result["metrics"]["order_notional_usd"], "1000")
        self.assertEqual(result["metrics"]["projected_gross_exposure_usd"], "2000")
        self.assertEqual(result["metrics"]["projected_gross_exposure_limit_usd"], "130000")
        self.assertEqual(result["metrics"]["symbol_concentration_percent"], "1")
        self.assertTrue(result["monetary_limits_applied"])
        self.assertNotIn("account", str(result).lower())

    def test_every_numeric_hard_limit_has_a_stable_failure_code(self):
        cases = (
            (replace(DEFAULT_RISK_POLICY, max_order_notional_usd="999"), intent(), "0",
             "risk_order_notional_limit"),
            (replace(DEFAULT_RISK_POLICY,
                     max_projected_gross_exposure_net_liquidation_multiple="0.01999"),
             intent(), "0", "risk_total_exposure_limit"),
            (replace(DEFAULT_RISK_POLICY, max_symbol_concentration_percent="0.9"), intent(), "0",
             "risk_concentration_limit"),
            (replace(DEFAULT_RISK_POLICY, max_daily_submitted_notional_usd="1499"), intent(), "500",
             "risk_daily_limit"),
            (DEFAULT_RISK_POLICY, intent(limit_price="102"), "0", "risk_quote_deviation_limit"),
        )
        for policy, request, daily, code in cases:
            with self.subTest(code=code), self.assertRaises(RiskPolicyError) as caught:
                self.assess(policy, request=request, daily=daily)
            self.assertEqual(caught.exception.code, code)

    def test_aapl_skips_only_monetary_limits(self):
        request = intent("AAPL", quantity=1000)
        result = self.assess(request=request, snapshot=facts("AAPL"), daily="999999")
        self.assertFalse(result["monetary_limits_applied"])
        self.assertEqual(result["metrics"]["order_notional_usd"], "100000")

        request["limit_price"] = "102"
        with self.assertRaises(RiskPolicyError) as caught:
            self.assess(request=request, snapshot=facts("AAPL"), daily="999999")
        self.assertEqual(caught.exception.code, "risk_quote_deviation_limit")

    def test_positions_and_open_orders_are_included_conservatively(self):
        snapshot = facts(
            positions={"positions": [{"symbol": "MSFT", "quantity": "5"}],
                       "count": 1, "as_of": NOW.isoformat()},
            open_orders={"orders": [{"symbol": "MSFT", "side": "SELL", "remaining_quantity": "2",
                                      "limit_price": "101"}], "count": 1, "as_of": NOW.isoformat()},
        )
        result = self.assess(snapshot=snapshot)
        self.assertEqual(result["metrics"]["projected_gross_exposure_usd"], "2202")
        self.assertEqual(result["metrics"]["symbol_concentration_percent"], "1.3")

    def test_stale_or_mismatched_facts_fail_closed(self):
        stale = facts()
        stale["quote"]["as_of"] = (NOW - timedelta(seconds=31)).isoformat()
        with self.assertRaises(RiskPolicyError) as caught:
            self.assess(snapshot=stale)
        self.assertEqual(caught.exception.code, "risk_data_stale")

        wrong = facts()
        wrong["context"] = {"config_id": "b" * 64, "session_id": "session-2"}
        with self.assertRaises(RiskPolicyError) as caught:
            self.assess(snapshot=wrong)
        self.assertEqual(caught.exception.code, "risk_data_invalid")

    def test_live_delayed_and_frozen_market_data_are_allowed(self):
        for data_type in ("live", "frozen", "delayed", "delayed_frozen"):
            snapshot = facts()
            snapshot["quote"]["data_type"] = data_type
            with self.subTest(data_type=data_type):
                self.assertEqual(self.assess(snapshot=snapshot)["market_data_type"], data_type)

    def test_only_aapl_and_ko_may_sell_puts(self):
        for symbol in ("AAPL", "KO"):
            DEFAULT_RISK_POLICY.validate_option_sale(option_sale(symbol, "P"), facts(symbol))
        with self.assertRaises(RiskPolicyError) as caught:
            DEFAULT_RISK_POLICY.validate_option_sale(option_sale("MSFT", "P"), facts())
        self.assertEqual(caught.exception.code, "risk_put_sale_not_allowed")

    def test_short_calls_require_100_unreserved_shares_per_contract(self):
        snapshot = facts("AAPL", positions={"positions": [
            {"symbol": "AAPL", "sec_type": "STK", "quantity": "99"}],
            "count": 1, "as_of": NOW.isoformat()})
        with self.assertRaises(RiskPolicyError) as caught:
            DEFAULT_RISK_POLICY.validate_option_sale(option_sale("AAPL", "C"), snapshot)
        self.assertEqual(caught.exception.code, "risk_uncovered_call_not_allowed")

        snapshot["positions"]["positions"][0]["quantity"] = "200"
        snapshot["open_orders"]["orders"] = [{
            "symbol": "AAPL", "sec_type": "OPT", "right": "C", "side": "SELL",
            "remaining_quantity": "1",
        }]
        DEFAULT_RISK_POLICY.validate_option_sale(option_sale("AAPL", "C"), snapshot)
        with self.assertRaises(RiskPolicyError):
            DEFAULT_RISK_POLICY.validate_option_sale(option_sale("AAPL", "C", quantity=2), snapshot)


if __name__ == "__main__":
    unittest.main()
