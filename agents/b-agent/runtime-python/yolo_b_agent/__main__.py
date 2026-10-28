"""Run B Agent with its own ChatGPT authorization, memory and skills."""

import sys

from yolo_agents.host import create_parser, run_host, validate_host_arguments

AGENT_ID = "b-agent"
APP_NAME = "B Agent"


def build_parser():
    parser = create_parser(__doc__, agent_name=AGENT_ID, app_name=APP_NAME, default_port=8766)
    parser.set_defaults(mode="chatgpt")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    validate_host_arguments(args, parser)
    try:
        return run_host(args, agent_name=AGENT_ID, app_name=APP_NAME)
    except RuntimeError:
        parser.error("B Agent 启动失败；请检查数据目录归属、实例锁和模型配置。")


if __name__ == "__main__":
    sys.exit(main())
