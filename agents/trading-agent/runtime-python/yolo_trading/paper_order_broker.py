"""Single synchronous, thread-safe lifecycle seam; never imports an IBKR SDK.

The lifecycle owns its event loop and repeats all gates immediately on that loop.
Timeout/disconnect after submit/cancel must raise or report unknown, never retry.
No account number, arbitrary SDK object, exception text or raw event crosses here.
"""

from typing import Protocol


class PaperOrderBroker(Protocol):
    def paper_order_snapshot(self) -> dict:
        """Return environment, config_id, session_id, account_verified,
        permissions_verified, environment_verified, execution_enabled,
        read_only, connected, data_complete and health_stale.
        config_id is the configured target's opaque identifier, never an account.
        """

    def preview_paper_order(self, intent: dict, *, order_ref: str, context: dict) -> dict:
        """Return qualified_contract (con_id/symbol/sec_type/exchange/currency),
        and what_if (complete/accepted/currency/initial_margin_change/
        maintenance_margin_change/commission). Monetary values are decimal text.
        This qualifies exactly one contract and runs what-if, never transmits.
        """

    def submit_paper_order(self, intent: dict, *, order_ref: str, preview: dict, context: dict) -> dict:
        """Call once using stable order_ref; return status, broker_order_id,
        filled_quantity and optional average_fill_price. Do not retry internally.
        All account/contract/permission/session checks run on the owner loop.
        """

    def cancel_paper_order(self, *, order_ref: str, broker_order_id: str, context: dict) -> dict:
        """Cancel only this owned order once; PendingCancel is not Cancelled.
        Return the same normalized result shape as submit. No global cancel.
        """


class BrokerBoundaryError(Exception):
    pass


def snapshot(broker, selection=None):
    """Fail closed if the lifecycle does not implement the entire order seam."""
    methods = ("paper_order_snapshot", "preview_paper_order", "submit_paper_order", "cancel_paper_order")
    if broker is None or any(not callable(getattr(broker, name, None)) for name in methods):
        raise BrokerBoundaryError("paper_facade_unavailable")
    try:
        value = broker.paper_order_snapshot()
        if not isinstance(value, dict) or value.get("environment") != "paper":
            raise BrokerBoundaryError("paper_only")
        required = ("account_verified", "permissions_verified", "environment_verified", "execution_enabled",
                    "connected", "data_complete")
        if (any(value.get(key) is not True for key in required) or value.get("read_only") is not False
                or value.get("health_stale") is not False):
            raise BrokerBoundaryError("broker_not_ready")
        config_id, session_id = value.get("config_id"), value.get("session_id")
        if (not isinstance(config_id, str) or len(config_id) != 64
                or any(char not in "0123456789abcdef" for char in config_id)
                or not isinstance(session_id, str) or not 1 <= len(session_id) <= 100
                or any(ord(char) < 33 or ord(char) > 126 for char in session_id)):
            raise BrokerBoundaryError("broker_not_ready")
        if selection is not None and selection != config_id:
            raise BrokerBoundaryError("account_selection_mismatch")
        return {"config_id": config_id, "session_id": session_id}
    except BrokerBoundaryError:
        raise
    except Exception:
        raise BrokerBoundaryError("broker_not_ready") from None
