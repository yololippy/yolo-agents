"""Fail-closed, YoloTrade-private Paper order risk policy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping


BUNDLED_RISK_POLICY_PATH = Path(__file__).with_name("risk-policy.json")
MARKET_DATA_TYPES = ("live", "frozen", "delayed", "delayed_frozen")


class RiskPolicyError(ValueError):
    """A stable public failure code without broker or account details."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RiskPolicyConfigurationError(ValueError):
    """A safe startup error that never echoes configuration contents."""


def _decimal(value: object, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise RiskPolicyError("risk_data_invalid")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise RiskPolicyError("risk_data_invalid") from None
    if (not number.is_finite() or abs(number) > Decimal("1000000000000")
            or (positive and number <= 0) or (nonnegative and number < 0)):
        raise RiskPolicyError("risk_data_invalid")
    return number


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _timestamp(value: object, now: datetime, max_age_seconds: int) -> None:
    if not isinstance(value, str):
        raise RiskPolicyError("risk_data_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RiskPolicyError("risk_data_invalid") from None
    if parsed.tzinfo is None:
        raise RiskPolicyError("risk_data_invalid")
    age = (now - parsed.astimezone(timezone.utc)).total_seconds()
    if age < -5 or age > max_age_seconds:
        raise RiskPolicyError("risk_data_stale")


def _symbols(value: object, field_name: str) -> tuple[str, ...]:
    if (not isinstance(value, list) or len(value) != len(set(value))
            or any(not isinstance(item, str) or re.fullmatch(r"[A-Z][A-Z0-9.]{0,14}", item) is None
                   for item in value)):
        raise RiskPolicyConfigurationError(f"{field_name} is invalid")
    return tuple(value)


@dataclass(frozen=True)
class RiskPolicy:
    """Immutable thresholds. Decimal values are strings for exact audit hashes."""

    schema_version: int
    version: str
    max_order_notional_usd: str
    max_projected_gross_exposure_net_liquidation_multiple: str
    max_symbol_concentration_percent: str
    max_daily_submitted_notional_usd: str
    max_quote_deviation_percent: str
    max_data_age_seconds: int
    allowed_market_data_types: tuple[str, ...]
    monetary_limit_exempt_symbols: tuple[str, ...]
    short_put_allowed_underlyings: tuple[str, ...]
    covered_call_shares_per_contract: int

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported risk policy schema")
        if (not isinstance(self.version, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.version) is None):
            raise ValueError("risk policy version is required")
        for field_name in (
            "max_order_notional_usd",
            "max_projected_gross_exposure_net_liquidation_multiple",
            "max_symbol_concentration_percent",
            "max_daily_submitted_notional_usd",
            "max_quote_deviation_percent",
        ):
            try:
                value = _decimal(getattr(self, field_name), positive=True)
            except RiskPolicyError:
                raise ValueError(f"{field_name} must be a positive decimal string") from None
            object.__setattr__(self, field_name, _text(value))
        if _decimal(self.max_symbol_concentration_percent) > 100:
            raise ValueError("max_symbol_concentration_percent must not exceed 100")
        if type(self.max_data_age_seconds) is not int or not 1 <= self.max_data_age_seconds <= 300:
            raise ValueError("max_data_age_seconds must be between 1 and 300")
        if (not isinstance(self.allowed_market_data_types, tuple)
                or not self.allowed_market_data_types
                or len(self.allowed_market_data_types) != len(set(self.allowed_market_data_types))
                or any(item not in MARKET_DATA_TYPES for item in self.allowed_market_data_types)):
            raise ValueError("allowed_market_data_types is invalid")
        for value, name in (
            (self.monetary_limit_exempt_symbols, "monetary_limit_exempt_symbols"),
            (self.short_put_allowed_underlyings, "short_put_allowed_underlyings"),
        ):
            if (not isinstance(value, tuple) or len(value) != len(set(value))
                    or any(not isinstance(item, str)
                           or re.fullmatch(r"[A-Z][A-Z0-9.]{0,14}", item) is None for item in value)):
                raise ValueError(f"{name} is invalid")
        if (type(self.covered_call_shares_per_contract) is not int
                or self.covered_call_shares_per_contract <= 0):
            raise ValueError("covered_call_shares_per_contract must be a positive integer")

    @property
    def policy_id(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def audit(self) -> dict[str, Any]:
        return {"policy_id": self.policy_id, **asdict(self)}

    def validate_option_sale(self, intent: Mapping[str, Any], snapshot: Mapping[str, Any]) -> None:
        """Enforce configured short-option rules without enabling option execution."""
        contract = intent.get("contract") if isinstance(intent, Mapping) else None
        if not isinstance(contract, Mapping) or contract.get("sec_type") != "OPT" or intent.get("side") != "SELL":
            return
        symbol, right, quantity = contract.get("symbol"), contract.get("right"), intent.get("quantity")
        if (not isinstance(symbol, str) or right not in ("P", "C")
                or type(quantity) is not int or quantity <= 0):
            raise RiskPolicyError("risk_data_invalid")
        if right == "P":
            if symbol not in self.short_put_allowed_underlyings:
                raise RiskPolicyError("risk_put_sale_not_allowed")
            return

        positions = snapshot.get("positions") if isinstance(snapshot, Mapping) else None
        open_orders = snapshot.get("open_orders") if isinstance(snapshot, Mapping) else None
        position_rows = positions.get("positions") if isinstance(positions, Mapping) else None
        order_rows = open_orders.get("orders") if isinstance(open_orders, Mapping) else None
        if not isinstance(position_rows, list) or not isinstance(order_rows, list):
            raise RiskPolicyError("risk_data_invalid")
        shares = Decimal("0")
        for row in position_rows:
            if not isinstance(row, Mapping):
                raise RiskPolicyError("risk_data_invalid")
            if row.get("symbol") == symbol and row.get("sec_type") == "STK":
                shares += _decimal(row.get("quantity"))
        reserved_contracts = Decimal("0")
        for row in order_rows:
            if not isinstance(row, Mapping):
                raise RiskPolicyError("risk_data_invalid")
            if (row.get("symbol") == symbol and row.get("sec_type") == "OPT"
                    and row.get("right") == "C" and row.get("side") == "SELL"):
                reserved_contracts += _decimal(row.get("remaining_quantity"), nonnegative=True)
        required = (reserved_contracts + quantity) * self.covered_call_shares_per_contract
        if shares < required:
            raise RiskPolicyError("risk_uncovered_call_not_allowed")

    def assess(
        self,
        intent: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        *,
        daily_submitted_notional: object,
        expected_context: Mapping[str, str],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Evaluate one stock order using account-redacted, freshly read broker facts."""
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if (not isinstance(intent, Mapping) or not isinstance(snapshot, Mapping)
                or snapshot.get("context") != dict(expected_context)):
            raise RiskPolicyError("risk_data_invalid")
        quote = snapshot.get("quote")
        positions = snapshot.get("positions")
        open_orders = snapshot.get("open_orders")
        account = snapshot.get("account")
        if (not isinstance(quote, Mapping) or not isinstance(positions, Mapping)
                or not isinstance(open_orders, Mapping) or not isinstance(account, Mapping)):
            raise RiskPolicyError("risk_data_invalid")
        for value in (quote, positions, open_orders, account):
            _timestamp(value.get("as_of"), now, self.max_data_age_seconds)

        contract = intent.get("contract")
        symbol = contract.get("symbol") if isinstance(contract, Mapping) else None
        side, quantity = intent.get("side"), intent.get("quantity")
        if (not isinstance(symbol, str) or contract.get("sec_type") != "STK"
                or side not in ("BUY", "SELL") or type(quantity) is not int or quantity <= 0):
            raise RiskPolicyError("risk_data_invalid")
        data_type = quote.get("data_type")
        if quote.get("symbol") != symbol or data_type not in self.allowed_market_data_types:
            raise RiskPolicyError("risk_market_data_not_allowed")
        reference = _decimal(quote.get("ask") if side == "BUY" else quote.get("bid"), positive=True)
        limit_price = _decimal(intent.get("limit_price"), positive=True)
        order_notional = limit_price * quantity
        deviation_percent = abs(limit_price - reference) / reference * 100

        position_rows = positions.get("positions")
        order_rows = open_orders.get("orders")
        if not isinstance(position_rows, list) or not isinstance(order_rows, list):
            raise RiskPolicyError("risk_data_invalid")
        projected_quantity = Decimal("0")
        for row in position_rows:
            if not isinstance(row, Mapping):
                raise RiskPolicyError("risk_data_invalid")
            if row.get("symbol") == symbol:
                projected_quantity += _decimal(row.get("quantity"))
        pending_notional = Decimal("0")
        for row in order_rows:
            if not isinstance(row, Mapping):
                raise RiskPolicyError("risk_data_invalid")
            remaining = _decimal(row.get("remaining_quantity"), nonnegative=True)
            price = _decimal(row.get("limit_price"), positive=True)
            pending_notional += remaining * price
            if row.get("symbol") == symbol:
                row_side = row.get("side")
                if row_side not in ("BUY", "SELL"):
                    raise RiskPolicyError("risk_data_invalid")
                projected_quantity += remaining if row_side == "BUY" else -remaining
        projected_quantity += quantity if side == "BUY" else -quantity

        net_liquidation = _decimal(account.get("net_liquidation"), positive=True)
        gross_position_value = _decimal(account.get("gross_position_value"), nonnegative=True)
        projected_gross = gross_position_value + pending_notional + order_notional
        gross_limit = net_liquidation * _decimal(
            self.max_projected_gross_exposure_net_liquidation_multiple, positive=True)
        symbol_exposure = abs(projected_quantity) * reference
        concentration_percent = symbol_exposure / net_liquidation * 100
        daily_total = _decimal(daily_submitted_notional, nonnegative=True) + order_notional
        monetary_limits_applied = symbol not in self.monetary_limit_exempt_symbols

        checks = []
        if monetary_limits_applied:
            checks.extend((
                (order_notional, _decimal(self.max_order_notional_usd), "risk_order_notional_limit"),
                (projected_gross, gross_limit, "risk_total_exposure_limit"),
                (concentration_percent, _decimal(self.max_symbol_concentration_percent), "risk_concentration_limit"),
                (daily_total, _decimal(self.max_daily_submitted_notional_usd), "risk_daily_limit"),
            ))
        checks.append((deviation_percent, _decimal(self.max_quote_deviation_percent),
                       "risk_quote_deviation_limit"))
        for actual, maximum, code in checks:
            if actual > maximum:
                raise RiskPolicyError(code)

        return {
            "status": "approved",
            "policy": self.audit(),
            "evaluated_at": now.isoformat().replace("+00:00", "Z"),
            "market_data_type": data_type,
            "monetary_limits_applied": monetary_limits_applied,
            "metrics": {
                "order_notional_usd": _text(order_notional),
                "projected_gross_exposure_usd": _text(projected_gross),
                "projected_gross_exposure_limit_usd": _text(gross_limit),
                "symbol_concentration_percent": _text(concentration_percent),
                "daily_submitted_notional_usd": _text(daily_total),
                "quote_deviation_percent": _text(deviation_percent),
                "reference_price": _text(reference),
            },
        }


def _strict_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def load_risk_policy(path: str | Path | None = None) -> RiskPolicy:
    """Load the single product policy document; malformed data has no fallback."""
    source = BUNDLED_RISK_POLICY_PATH if path is None else Path(path).expanduser()
    try:
        value = json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=_strict_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))
        if not isinstance(value, dict) or set(value) != {
                "schema_version", "version", "limits", "market_data", "symbol_overrides", "option_sales"}:
            raise ValueError
        limits, market, symbols, options = (value["limits"], value["market_data"],
                                             value["symbol_overrides"], value["option_sales"])
        if (not isinstance(limits, dict) or set(limits) != {
                "max_order_notional_usd", "max_projected_gross_exposure_net_liquidation_multiple",
                "max_symbol_concentration_percent", "max_daily_submitted_notional_usd",
                "max_quote_deviation_percent", "max_data_age_seconds"}
                or not isinstance(market, dict) or set(market) != {"allowed_types"}
                or not isinstance(symbols, dict) or set(symbols) != {"monetary_limit_exempt"}
                or not isinstance(options, dict) or set(options) != {
                    "short_put_allowed_underlyings", "covered_call_shares_per_contract"}):
            raise ValueError
        allowed = market["allowed_types"]
        decimal_fields = set(limits) - {"max_data_age_seconds"}
        if (any(not isinstance(limits[name], str) for name in decimal_fields)
                or type(limits["max_data_age_seconds"]) is not int
                or not isinstance(allowed, list) or len(allowed) != len(set(allowed))
                or any(item not in MARKET_DATA_TYPES for item in allowed)):
            raise ValueError
        return RiskPolicy(
            schema_version=value["schema_version"], version=value["version"],
            allowed_market_data_types=tuple(allowed),
            monetary_limit_exempt_symbols=_symbols(symbols["monetary_limit_exempt"], "monetary_limit_exempt"),
            short_put_allowed_underlyings=_symbols(options["short_put_allowed_underlyings"], "short_put_allowed_underlyings"),
            covered_call_shares_per_contract=options["covered_call_shares_per_contract"],
            **limits,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, RiskPolicyConfigurationError):
        raise RiskPolicyConfigurationError("YoloTrade 风控配置无效或不可读取。") from None


DEFAULT_RISK_POLICY = load_risk_policy()
