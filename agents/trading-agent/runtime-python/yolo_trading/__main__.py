"""Run the Yolo Agents trading host with opt-in Paper-only IBKR execution."""

import json
import os
from pathlib import Path
import sys

from yolo_agents.host import create_parser, prepare_agent_directory, run_host, validate_host_arguments

from .api_extension import TradingAPIExtension
from .ibkr import IBKRAdapter
from .ibkr_config import ENV_PREFIX, IBKRConfigurationError, load_ibkr_config
from .ibkr_lifecycle import IBKRBrokerLifecycle

AGENT_ID = "trading-agent"
APP_NAME = "Trading Agent"


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


def run_trading_host(args, broker) -> int:
    """Keep the broker private to this Agent and close it on every Host exit."""
    data_dir = getattr(args, "data_dir", None)
    if data_dir is not None:
        data_dir = prepare_agent_directory(data_dir, AGENT_ID, APP_NAME,
            adopt_existing=getattr(args, "adopt_existing_data", False))
        args.data_dir = data_dir
    lifecycle = IBKRBrokerLifecycle(broker) if broker is not None else None
    try:
        orders_path = Path(data_dir).expanduser().resolve() / "paper-orders.sqlite3" if data_dir is not None else None
        extension = TradingAPIExtension(lifecycle, orders_path=orders_path)
        if lifecycle is not None:
            lifecycle.start()
        return run_host(args, extension=extension, agent_name=AGENT_ID, app_name=APP_NAME)
    finally:
        if lifecycle is not None and not lifecycle.close():
            print("IBKR background shutdown could not be confirmed; check Gateway and Host process.", file=sys.stderr)


def build_parser():
    parser = create_parser(__doc__, agent_name=AGENT_ID, app_name=APP_NAME, default_port=8765)
    parser.set_defaults(mode="chatgpt")
    add_trading_arguments(parser)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    validate_host_arguments(args, parser)
    broker = load_broker(args, parser)
    try:
        return run_trading_host(args, broker)
    except RuntimeError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
