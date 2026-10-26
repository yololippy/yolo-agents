"""Run the Yolo Agents trading host; IBKR remains explicitly read-only."""

import json
import os
import sys

from yolo_agents.host import create_parser, run_host, validate_host_arguments

from .api_extension import TradingAPIExtension
from .ibkr import IBKRAdapter
from .ibkr_config import ENV_PREFIX, IBKRConfigurationError, load_ibkr_config


def add_trading_arguments(parser) -> None:
    parser.add_argument("--ibkr-config", help="IBKR JSON 配置；不接受登录凭据")
    parser.add_argument("--ibkr-environment", choices=("paper", "live"))
    parser.add_argument("--ibkr-host")
    parser.add_argument("--ibkr-port", type=int)
    parser.add_argument("--ibkr-client-id", type=int)
    parser.add_argument("--ibkr-account")
    parser.add_argument("--ibkr-connect-timeout", type=float)


def load_broker(args, parser):
    """Load an explicit broker profile without opening a Gateway socket."""
    base = None
    if args.ibkr_config:
        try:
            from pathlib import Path
            base = json.loads(Path(args.ibkr_config).expanduser().read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            parser.error("--ibkr-config 必须是可读取的 JSON 配置文件，且不得包含登录凭据")
    overrides = {
        "environment": args.ibkr_environment,
        "host": args.ibkr_host,
        "port": args.ibkr_port,
        "client_id": args.ibkr_client_id,
        "account": args.ibkr_account,
        "connect_timeout": args.ibkr_connect_timeout,
    }
    overrides = {key: value for key, value in overrides.items() if value is not None}
    has_environment = any(key.startswith(ENV_PREFIX) for key in os.environ)
    if base is None and not overrides and not has_environment:
        return None
    try:
        return IBKRAdapter(load_ibkr_config(base, overrides=overrides))
    except IBKRConfigurationError as exc:
        parser.error(f"IBKR 配置无效：{exc}")


def main() -> int:
    parser = create_parser(__doc__)
    add_trading_arguments(parser)
    args = parser.parse_args()
    validate_host_arguments(args, parser)
    broker = load_broker(args, parser)
    try:
        return run_host(args, extension=TradingAPIExtension(broker), agent_name="trading-agent")
    except RuntimeError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
