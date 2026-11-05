"""YoloTrade product identity owned by this specialization."""

from yolo_agents.agent_runtime import RuntimeCapabilities
from .runtime_tools import (
    CODEX_TRADING_PROPOSAL_INSTRUCTIONS, PAPER_DRAFT_TOOL, PAPER_DRAFT_TOOL_SCHEMA_VERSION,
    TRADING_TOOL_INSTRUCTIONS, PaperOrderStreamFilter, finish_trading_reply,
    pydantic_trading_tools, trading_dynamic_tools,
)

AGENT_ID = "yolo-trade"
DISPLAY_NAME = "YoloTrade"
DEFAULT_PORT = 8765

RUNTIME_CAPABILITIES = RuntimeCapabilities(
    instructions=(
        "本服务显式开放 IBKR Paper 只读查询和非执行 Paper 草稿创建。"
        "模型工具不能确认或执行订单；不得声称模型已经提交订单。"
        "订单授权、风险限额和实时账户状态不能由记忆或历史确认代替。"
        + TRADING_TOOL_INSTRUCTIONS
    ),
    pydantic_tools_factory=pydantic_trading_tools,
    codex_instructions=CODEX_TRADING_PROPOSAL_INSTRUCTIONS,
    codex_dynamic_tools_factory=trading_dynamic_tools,
    tool_schema_version=PAPER_DRAFT_TOOL_SCHEMA_VERSION,
    text_filter_factory=PaperOrderStreamFilter,
    extracted_tool_name=PAPER_DRAFT_TOOL,
    finish_reply=finish_trading_reply,
)
