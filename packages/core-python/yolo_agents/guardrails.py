"""Credential guards using the pinned Harness detectors and capabilities."""
from dataclasses import fields, is_dataclass
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

import regex
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering
from pydantic_ai.messages import ToolReturn
from pydantic_ai_harness import GuardrailResult, InputGuardrail, OutputGuardrail, ToolGuardrail
from pydantic_ai_harness.guardrails import InputBlocked, ToolBlocked, GuardrailError
from pydantic_ai_harness.guardrails.detectors import DEFAULT_SECRET_PATTERNS, secret_data, for_tool_result_text
from .agent_runtime import RuntimeGuardrailError

_SAFE = '检测到可能的凭据，已阻止本次请求。'
_detector = secret_data()
_stream_pattern = regex.compile('|'.join('(?:' + p + ')' for p in DEFAULT_SECRET_PATTERNS.values()))

def check_text(text):
    if not isinstance(text, str) or _detector(text).action != 'allow':
        raise RuntimeGuardrailError(_SAFE)

def _strings(value, seen=None):
    if seen is None:
        seen = set()
    if isinstance(value, str):
        yield value
    elif value is None or isinstance(value, (int, float, Decimal, bool, bytes, date, datetime, Enum)):
        return
    elif id(value) not in seen:
        seen.add(id(value))
        if isinstance(value, Mapping):
            for key, item in value.items():
                yield from _strings(key, seen)
                yield from _strings(item, seen)
        elif isinstance(value, (Sequence, set)):
            for item in value:
                yield from _strings(item, seen)
        elif is_dataclass(value) and not isinstance(value, type):
            for field in fields(value):
                yield from _strings(getattr(value, field.name), seen)
        elif hasattr(value, 'model_dump'):
            yield from _strings(value.model_dump(mode='json'), seen)
        else:
            raise RuntimeGuardrailError(_SAFE)

def check_value(value):
    for text in _strings(value):
        check_text(text)

def _input(text):
    try:
        check_text(text)
    except RuntimeGuardrailError:
        raise InputBlocked(_SAFE) from None
    return True

def _tool_args(info):
    try:
        check_value(info.args)
    except RuntimeGuardrailError:
        raise ToolBlocked('tool', _SAFE) from None
    return True

def _tool_result(info):
    try:
        if isinstance(info.result, ToolReturn) and for_tool_result_text(_detector)(info).action != 'allow':
            raise RuntimeGuardrailError(_SAFE)
        check_value(info.result)
    except RuntimeGuardrailError:
        raise ToolBlocked('tool', _SAFE) from None
    return True

def _output(value):
    try:
        check_text(value)
    except RuntimeGuardrailError:
        return GuardrailResult.block(_SAFE)
    return GuardrailResult.allow()

class ContextGuard(AbstractCapability):
    def __init__(self, position):
        self.position = position

    def get_ordering(self):
        return CapabilityOrdering(position=self.position)

    async def before_model_request(self, ctx, request_context):
        try:
            check_value(request_context.messages)
            params = request_context.model_request_parameters
            check_value(params.instruction_parts)
            check_value(params.function_tools)
        except RuntimeGuardrailError:
            raise InputBlocked(_SAFE) from None
        return request_context

def capabilities():
    return [ContextGuard('outermost'), InputGuardrail(_input, parallel=False),
            ToolGuardrail(guard=_tool_args, result_guard=_tool_result),
            OutputGuardrail(_output), ContextGuard('innermost')]

def is_guardrail_error(exc):
    pending = [exc]
    seen = set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        if isinstance(error, (GuardrailError, RuntimeGuardrailError)):
            return True
        pending.extend(getattr(error, 'exceptions', ()))
        if error.__cause__ is not None:
            pending.append(error.__cause__)
        if error.__context__ is not None:
            pending.append(error.__context__)
    return False

class CredentialStreamFilter:
    """Keep possible token prefixes until they cannot form an official match."""
    def __init__(self):
        self.pending = ''
        self.previous = ''

    def feed(self, text):
        result = []
        for offset in range(0, len(text), 4096):
            self.pending += text[offset:offset + 4096]
            try:
                match = _stream_pattern.search(self.previous + self.pending, partial=True, timeout=0.2)
            except TimeoutError:
                raise RuntimeGuardrailError(_SAFE) from None
            if match and not match.partial:
                raise RuntimeGuardrailError(_SAFE)
            cut = max(0, match.start() - len(self.previous)) if match else len(self.pending)
            safe, self.pending = self.pending[:cut], self.pending[cut:]
            if safe:
                self.previous = safe[-1:]
                result.append(safe)
            if len(self.pending) > 65536:
                raise RuntimeGuardrailError(_SAFE)
        return result

    def finish(self):
        check_text(self.previous + self.pending)
        result, self.pending = self.pending, ''
        return result
