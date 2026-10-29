"""Narrow, application-owned tools that a chat runtime may expose.

The model can create a non-executable Paper order draft.  It never receives
broker credentials, account identifiers, or confirm/submit capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
import logging
import re
import threading
from typing import Any, Callable, Literal, Mapping
from uuid import UUID


PAPER_DRAFT_TOOL = "create_paper_order_draft"
PAPER_DRAFT_TOOL_SCHEMA_VERSION = 3
PAPER_DRAFT_DESCRIPTION = (
    "Create a non-executable Paper stock limit-order draft for review in the Mac app. "
    "Call this tool whenever the user asks to create a Paper draft and explicitly supplied an "
    "unambiguous ticker, BUY/SELL side, "
    "whole-share quantity, and limit price. Never infer or guess a price. If any field, "
    "especially limit_price, is missing, ask the user for it instead of calling this tool. "
    "This tool cannot preview, confirm, submit, cancel, or place an order."
)
PAPER_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string", "pattern": r"^[A-Z][A-Z0-9.]{0,14}$"},
        "side": {"type": "string", "enum": ["BUY", "SELL"]},
        "quantity": {"type": "integer", "minimum": 1, "maximum": 100000},
        "limit_price": {"type": "string", "pattern": r"^[1-9][0-9]{0,6}(?:\.[0-9]{1,4})?$"},
    },
    "required": ["symbol", "side", "quantity", "limit_price"],
    "additionalProperties": False,
}

TRADING_TOOL_INSTRUCTIONS = (
    "The create_paper_order_draft dynamic tool is available in this conversation. You MUST call it "
    "when the current user asks to create a Paper draft and explicitly supplies all four required "
    "fields. Do not say the tool is unavailable unless a tool call actually returns a failure. "
    "Use create_paper_order_draft only to create a non-executable Paper LMT/DAY stock draft. "
    "The user's message or trusted conversation must explicitly contain the ticker or an unambiguous "
    "company name, BUY/SELL intent, whole-share quantity, and limit price. Never obtain or invent a "
    "price from model knowledge. If the user says, for example, 'buy two shares of Tesla' without a "
    "limit price, ask for the limit price and do not call the tool. After a successful call, state that "
    "only a draft was created and that What-if, confirmation, and submission remain separate explicit "
    "actions in the Mac Paper Orders screen. The application will append the trusted paper-order "
    "artifact after a successful call; never write, copy, or invent a paper-order fenced block or "
    "order_id yourself. If the tool is unavailable or fails, do not claim a draft exists. You cannot "
    "confirm, submit, cancel, or place any order."
)
CODEX_TRADING_PROPOSAL_INSTRUCTIONS = (
    "Do not call a tool for Paper drafts. When the current user asks to create a Paper draft and "
    "explicitly supplies ticker, BUY/SELL side, whole-share quantity, and limit price, end your "
    "response with exactly one fenced paper-order-proposal JSON block containing only type, symbol, "
    "side, quantity, and limit_price. The type must be paper_order_proposal, quantity must be an "
    "integer, and limit_price must be a string copied from the current user's message. Example: "
    "```paper-order-proposal\n{\"type\":\"paper_order_proposal\",\"symbol\":\"TSLA\","
    "\"side\":\"BUY\",\"quantity\":2,\"limit_price\":\"1\"}\n```. "
    "Do not claim the draft exists; say only that the request is being prepared. The application "
    "validates the proposal and may append a trusted draft card. If any required field is missing, "
    "ask for it and do not emit the block. Never emit a paper-order block or order_id."
)

_MAX_MODEL_ORDER_BLOCK = 65536
_LOGGER = logging.getLogger(__name__)


def paper_draft_dynamic_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "name": PAPER_DRAFT_TOOL,
        "description": PAPER_DRAFT_DESCRIPTION,
        "inputSchema": PAPER_DRAFT_SCHEMA,
    }


def validate_paper_draft_arguments(arguments: object) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != set(PAPER_DRAFT_SCHEMA["required"]):
        raise ValueError("invalid Paper draft arguments")
    symbol, side = arguments.get("symbol"), arguments.get("side")
    quantity, limit_price = arguments.get("quantity"), arguments.get("limit_price")
    if (not isinstance(symbol, str) or re.fullmatch(r"[A-Z][A-Z0-9.]{0,14}", symbol) is None
            or side not in ("BUY", "SELL") or type(quantity) is not int or not 1 <= quantity <= 100000
            or not isinstance(limit_price, str)
            or re.fullmatch(r"[1-9][0-9]{0,6}(?:\.[0-9]{1,4})?", limit_price) is None):
        raise ValueError("invalid Paper draft arguments")
    return {"symbol": symbol, "side": side, "quantity": quantity, "limit_price": limit_price}


def _explicit_limit_price(messages: tuple[Mapping[str, Any], ...], limit_price: str) -> bool:
    """Require current-user evidence; never let a model supply a remembered/guessed price."""
    if not messages:
        return False
    latest = messages[-1]
    if latest.get("role") != "user" or not isinstance(latest.get("content"), str):
        return False
    content = latest["content"].strip()
    try:
        expected = Decimal(limit_price)
    except InvalidOperation:
        return False
    number_pattern = re.compile(r"(?<![A-Za-z0-9.])([1-9][0-9]{0,6}(?:\.[0-9]{1,4})?)(?![A-Za-z0-9.])")
    for match in number_pattern.finditer(content):
        try:
            same = Decimal(match.group(1)) == expected
        except InvalidOperation:
            same = False
        if not same:
            continue
        before = content[max(0, match.start() - 24):match.start()].casefold()
        after = content[match.end():match.end() + 12].casefold()
        if (re.search(r"(?:limit(?:\s+price)?|price|at|@|\$|usd)\s*$", before)
                or re.match(r"^\s*(?:usd|美元|美金|元|限价|价格|价位)", after)
                or re.search(r"(?:限价|价格|价位|按|以|挂(?:单)?(?:在|到)?)\s*$", before)):
            return True
    sole = re.fullmatch(
        r"\s*(?:\$|usd\s*)?([1-9][0-9]{0,6}(?:\.[0-9]{1,4})?)\s*(?:usd|美元|美金|元)?\s*",
        content, re.IGNORECASE,
    )
    if sole is None or Decimal(sole.group(1)) != expected or len(messages) < 2:
        return False
    previous = messages[-2]
    return (previous.get("role") == "assistant" and isinstance(previous.get("content"), str)
            and re.search(r"limit(?:\s+price)?|price|限价|价格", previous["content"], re.IGNORECASE) is not None)


@dataclass
class RuntimeToolContext:
    """Trusted request identity plus one application-owned draft callback."""

    conversation_id: str
    user_message_id: str
    run_id: str
    caller: Mapping[str, Any]
    trusted_messages: tuple[Mapping[str, Any], ...]
    create_paper_draft: Callable[..., Mapping[str, Any]]
    reauthorize: Callable[[], Mapping[str, Any]]
    _guard: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _artifact: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _attempted: bool = field(default=False, init=False, repr=False)

    def invoke(self, tool: str, arguments: object) -> dict[str, str]:
        if tool != PAPER_DRAFT_TOOL:
            raise ValueError("unknown runtime tool")
        proposal = validate_paper_draft_arguments(arguments)
        self._attempted = True
        if not _explicit_limit_price(self.trusted_messages, proposal["limit_price"]):
            return {
                "status": "needs_information", "missing_fields": ["limit_price"],
                "message": "请明确提供本次 Paper LMT 草稿的限价；不会猜测价格。",
            }
        with self._guard:
            if self._artifact is not None:
                same = all(
                    self._artifact[key] == proposal[key]
                    for key in ("symbol", "side", "quantity", "limit_price")
                )
                if same:
                    return {"status": "draft_created", "order_id": self._artifact["order_id"], "state": "draft"}
                return {"status": "draft_unavailable", "message": "本轮已创建另一个 Paper 草稿，未创建第二个草稿。"}
            try:
                result = self.create_paper_draft(
                    proposal=proposal,
                    conversation_id=self.conversation_id,
                    user_message_id=self.user_message_id,
                    run_id=self.run_id,
                    caller=dict(self.caller),
                    reauthorize=self.reauthorize,
                )
                if not isinstance(result, Mapping):
                    raise ValueError
                order_id = str(UUID(result["order_id"]))
                if result.get("status") != "draft_created" or result.get("state") != "draft":
                    raise ValueError
            except Exception as exc:
                # Expose only a small application-owned error vocabulary.  Raw
                # exception text may contain account or provider details and is
                # never returned to the model.
                code = getattr(exc, "code", None)
                safe_codes = {
                    "account_selection_mismatch", "broker_not_ready", "idempotency_conflict",
                    "invalid_order", "order_state_conflict", "paper_facade_unavailable",
                    "paper_only", "permission_revoked",
                }
                result = {
                    "status": "draft_unavailable",
                    "message": "Paper 草稿未创建，请在 Mac 订单界面检查权限和字段后重试。",
                }
                if code in safe_codes:
                    result["error_code"] = code
                _LOGGER.warning("Paper draft callback failed (%s)", code if code in safe_codes else type(exc).__name__)
                return result
            self._artifact = {
                "type": "paper_order_draft", "order_id": order_id,
                "symbol": proposal["symbol"], "side": proposal["side"],
                "quantity": proposal["quantity"], "limit_price": proposal["limit_price"],
                "environment": "paper", "order_type": "LMT", "tif": "DAY",
            }
            return {"status": "draft_created", "order_id": order_id, "state": "draft"}

    def trusted_artifact(self) -> dict[str, Any] | None:
        with self._guard:
            return None if self._artifact is None else dict(self._artifact)

    def attempted(self) -> bool:
        with self._guard:
            return self._attempted

    def codex_result(self, tool: str, arguments: object) -> dict[str, Any]:
        result = self.invoke(tool, arguments)
        return {
            "contentItems": [{"type": "inputText", "text": json.dumps(result, ensure_ascii=False)}],
            "success": result["status"] == "draft_created",
        }


def _is_order_json(body: str) -> bool:
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, RecursionError):
        return False
    return isinstance(value, dict) and value.get("type") in ("paper_order_draft", "paper_order_proposal")


def _model_proposal(body: str) -> dict[str, Any] | None:
    try:
        value = json.loads(body)
        if not isinstance(value, dict) or set(value) != {
                "type", "symbol", "side", "quantity", "limit_price"}:
            return None
        if value.pop("type") != "paper_order_proposal":
            return None
        return validate_paper_draft_arguments(value)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return None


class PaperOrderStreamFilter:
    """Suppress model-authored order cards while preserving ordinary text streaming."""

    def __init__(self):
        self.pending = ""
        self._proposal: dict[str, Any] | None = None
        self._proposal_conflict = False

    def feed(self, text: str) -> list[str]:
        if not isinstance(text, str):
            raise TypeError("trading reply delta must be text")
        self.pending += text
        output: list[str] = []
        while self.pending:
            start = self.pending.find("```")
            if start < 0:
                cut = max(0, len(self.pending) - 2)
                if cut:
                    output.append(self.pending[:cut])
                    self.pending = self.pending[cut:]
                break
            if start:
                output.append(self.pending[:start])
                self.pending = self.pending[start:]
                continue
            newline = self.pending.find("\n")
            if newline < 0:
                if len(self.pending) > 64:
                    output.append(self.pending[0])
                    self.pending = self.pending[1:]
                    continue
                break
            language = self.pending[3:newline].strip().casefold()
            if language not in ("paper-order", "paper-order-proposal", "json"):
                output.append(self.pending[:newline + 1])
                self.pending = self.pending[newline + 1:]
                continue
            end = self.pending.find("```", newline + 1)
            if end < 0:
                if len(self.pending) > _MAX_MODEL_ORDER_BLOCK:
                    raise ValueError("model-authored order artifact exceeded the safety limit")
                break
            block, self.pending = self.pending[:end + 3], self.pending[end + 3:]
            body = block[newline + 1:-3]
            if language == "paper-order-proposal":
                proposal = _model_proposal(body)
                if proposal is None or (self._proposal is not None and self._proposal != proposal):
                    self._proposal_conflict = True
                    self._proposal = None
                elif not self._proposal_conflict:
                    self._proposal = proposal
            elif language == "json" and not _is_order_json(body):
                output.append(block)
        return output

    def finish(self) -> str:
        pending, self.pending = self.pending, ""
        if not pending:
            return ""
        newline = pending.find("\n")
        if pending.startswith("```") and newline >= 0:
            language = pending[3:newline].strip().casefold()
            if language in ("paper-order", "paper-order-proposal", "json"):
                # An incomplete typed block cannot be trusted and is never useful as a final card.
                return ""
        return pending

    def proposal(self) -> dict[str, Any] | None:
        if self._proposal_conflict or self._proposal is None:
            return None
        return dict(self._proposal)


def trusted_paper_order_block(tools: object) -> str | None:
    artifact = tools.trusted_artifact() if isinstance(tools, RuntimeToolContext) else None
    if artifact is None:
        return None
    return "```paper-order\n" + json.dumps(artifact, ensure_ascii=False, separators=(",", ":")) + "\n```"


def finish_trading_reply(cleaned: str, tools: object) -> str:
    """Append exactly one application-owned card after streamed, filtered model text."""
    block = trusted_paper_order_block(tools)
    if block is not None:
        return ("\n\n" if cleaned else "") + block
    if isinstance(tools, RuntimeToolContext) and tools.attempted():
        return ("\n\n" if cleaned else "") + "Paper 草稿未创建。"
    return "" if cleaned.strip() else "Paper 草稿未创建。"


def finalize_trading_reply(text: str, tools: object) -> str:
    """Non-streaming equivalent used by tests and recovery paths."""
    stream_filter = PaperOrderStreamFilter()
    chunks = stream_filter.feed(text)
    chunks.append(stream_filter.finish())
    cleaned = "".join(chunks)
    return cleaned + finish_trading_reply(cleaned, tools)


def pydantic_paper_draft_tool():
    """Build the pinned PydanticAI tool lazily for optional-runtime installs."""
    from pydantic_ai import Tool

    def create_paper_order_draft(
        ctx,
        symbol: str,
        side: Literal["BUY", "SELL"],
        quantity: int,
        limit_price: str,
    ) -> dict[str, str]:
        """Create a non-executable Paper stock limit-order draft for Mac review."""
        tools = getattr(ctx.deps, "runtime_tools", None)
        if not isinstance(tools, RuntimeToolContext):
            return {"status": "draft_unavailable", "message": "Paper 草稿入口当前不可用。"}
        return tools.invoke(PAPER_DRAFT_TOOL, {
            "symbol": symbol, "side": side, "quantity": quantity, "limit_price": limit_price,
        })

    return Tool(
        create_paper_order_draft,
        takes_ctx=True,
        name=PAPER_DRAFT_TOOL,
        description=PAPER_DRAFT_DESCRIPTION,
        require_parameter_descriptions=False,
        sequential=True,
    )
