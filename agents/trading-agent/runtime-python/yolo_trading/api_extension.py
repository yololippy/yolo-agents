"""Trading-Agent-only additions to the shared Yolo Agents chat API."""

from yolo_agents.api import APIError


class TradingAPIExtension:
    """Keeps IBKR status and the execution boundary outside shared packages."""

    def __init__(self, broker=None):
        self.broker = broker

    def broker_health(self):
        if self.broker is not None:
            return self.broker.health()
        return {
            "state": "not_configured", "connected": False, "execution_enabled": False,
            "environment_verified": False, "data_complete": False,
            "blockers": [{"code": "not_configured", "message": "尚未选择 IBKR paper/live 环境。"}],
        }

    def public_health(self):
        return {"trade_execution_enabled": False, "broker": self.broker_health()}

    def route(self, handler, method, path, query, caller):
        if path == "/v1/broker/health" and method == "GET":
            handler.send_json(200, {"broker": self.broker_health()})
            return True
        if path.startswith(("/v1/orders", "/v1/approvals", "/v1/automations", "/v1/trades")):
            raise APIError(403, "trade_execution_disabled", "首版不开放交易执行、确认或交易预授权接口。")
        return False
