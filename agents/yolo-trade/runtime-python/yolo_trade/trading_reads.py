"""Strict public DTO boundary for direct trading reads; no model dependency."""

from datetime import datetime
from decimal import Decimal
import re


def text(value, pattern, limit, *, nullable=False):
    if value is None and nullable:
        return None
    if (not isinstance(value, str) or not 1 <= len(value) <= limit
            or re.fullmatch(pattern, value) is None
            or re.search(r"\b(?:DU|DUR|U)[0-9]{5,}\b", value)):
        raise ValueError()
    return value


def number(value, *, nullable=False, positive=False, nonnegative=False):
    if value is None and nullable:
        return None
    text(value, r"-?(?:0|[1-9][0-9]{0,12})(?:\.[0-9]{1,18})?", 48)
    result = Decimal(value)
    if (not result.is_finite() or abs(result) > Decimal("1000000000000")
            or (positive and result <= 0) or (nonnegative and result < 0)):
        raise ValueError()
    return value


def con_id(value):
    if type(value) is not int or not 0 < value <= 2147483647:
        raise ValueError()
    return value


def timestamp(value):
    text(value, r"[0-9T:.+Z-]+", 40)
    if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError()
    return value


def identity(row):
    if not isinstance(row, dict):
        raise ValueError()
    if row.get("sec_type") != "STK" or row.get("exchange") != "SMART" or row.get("currency") != "USD":
        raise ValueError()
    return {"con_id": con_id(row.get("con_id")),
            "symbol": text(row.get("symbol"), r"[A-Z][A-Z0-9.]*", 15),
            "primary_exchange": text(row.get("primary_exchange"), r"[A-Z][A-Z0-9.]*", 24),
            "sec_type": "STK", "exchange": "SMART", "currency": "USD"}


def public_read(kind, result, *, expected_id=None):
    if not isinstance(result, dict):
        raise ValueError()
    as_of = timestamp(result.get("as_of"))
    if kind == "quote":
        row = identity(result)
        if row["con_id"] != expected_id or result.get("data_type") not in ("live", "frozen", "delayed", "delayed_frozen"):
            raise ValueError()
        values = {key: number(result.get(key), nullable=True, positive=True) for key in ("bid", "ask", "last", "close")}
        if not any(values.values()):
            raise ValueError()
        return {"quote": {**row, **values, "data_type": result["data_type"], "as_of": as_of}}
    key, maximum = ("positions", 500) if kind == "positions" else ("instruments", 16)
    rows = result.get(key)
    if (not isinstance(rows, list) or len(rows) > maximum or type(result.get("count")) is not int
            or result["count"] != len(rows)):
        raise ValueError()
    normalized = []
    for row in rows:
        if kind == "search":
            normalized.append({**identity(row), "name": text(row.get("name"), r"[^\x00-\x1f\x7f]+", 160, nullable=True)})
        else:
            normalized.append({"con_id": con_id(row.get("con_id")),
                "symbol": text(row.get("symbol"), r"[A-Z0-9 ._-]+", 32),
                "sec_type": text(row.get("sec_type"), r"[A-Z]+", 12),
                "currency": text(row.get("currency"), r"[A-Z]+", 8),
                **{key: text(row.get(key), r"[A-Z0-9.]+", 24, nullable=True) for key in ("exchange", "primary_exchange")},
                **{key: number(row.get(key), nullable=key not in ("quantity", "average_cost"),
                               nonnegative=key in ("average_cost", "market_price")) for key in (
                    "quantity", "average_cost", "market_price", "market_value", "unrealized_pnl", "realized_pnl")}})
    return {key: normalized, "count": len(normalized), "as_of": as_of}
