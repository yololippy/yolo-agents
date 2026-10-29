"""Offline tests for official credential patterns and stream boundaries."""
import unittest
from decimal import Decimal
from types import SimpleNamespace

from yolo_agents.agent_runtime import RuntimeGuardrailError
from yolo_agents.guardrails import (
    CredentialStreamFilter, check_text, check_value, _tool_args, _tool_result, is_guardrail_error,
)
from pydantic_ai_harness.guardrails import InputBlocked, ToolBlocked
from pydantic_ai.messages import ToolReturn


class GuardrailTests(unittest.TestCase):
    def test_all_stream_split_points_and_single_character_deltas(self):
        secrets = [
            'sk-proj-' + 'A' * 32,
            'sk-ant-' + 'B' * 32,
            'AKIA' + 'A' * 16,
            'ghp_' + 'C' * 32,
            'xoxb-' + 'D' * 20,
            'eyJ' + 'A' * 12 + '.eyJ' + 'B' * 12 + '.' + 'C' * 12,
            '-----BEGIN PRIVATE KEY-----\n' + 'A' * 48 + '\n-----END PRIVATE KEY-----',
        ]
        for secret in secrets:
            check = lambda fragments: self._assert_blocks_without_prefix(secret, fragments)
            for cut in range(len(secret) + 1):
                with self.subTest(pattern=secret[:4], cut=cut):
                    check(['safe ' + secret[:cut], secret[cut:]])
            check(['safe '] + list(secret))

    def _assert_blocks_without_prefix(self, secret, fragments):
        guard, emitted = CredentialStreamFilter(), []
        with self.assertRaises(RuntimeGuardrailError):
            for fragment in fragments:
                emitted.extend(guard.feed(fragment))
            emitted.append(guard.finish())
        self.assertNotIn(secret[:4], ''.join(emitted))

    def test_safe_text_streams_and_round_trips(self):
        text = '你好，账户余额与 alice@example.com 都是普通文字。Hello world!'
        guard = CredentialStreamFilter()
        first = ''.join(guard.feed(text[:20]))
        self.assertTrue(first)
        rest = ''.join(guard.feed(text[20:])) + guard.finish()
        self.assertEqual(first + rest, text)
        check_text(text)

    def test_decimal_provider_metadata_is_a_safe_scalar(self):
        check_value({"usage": {"cost": Decimal("0.003396")}})

    def test_structured_tool_args_and_result_block(self):
        secret = 'ghp_' + 'Q' * 32
        for guard, info in [(_tool_args, SimpleNamespace(args={'content': ['ok', secret]})),
                            (_tool_result, SimpleNamespace(result={'content': secret}))]:
            with self.assertRaises(ToolBlocked) as error:
                guard(info)
            self.assertNotIn(secret, str(error.exception))
        with self.assertRaises(ToolBlocked):
            _tool_result(SimpleNamespace(result=ToolReturn('ok', content=[secret[:8], secret[8:]])))

    def test_nested_sdk_exception_is_detected(self):
        group = ExceptionGroup('outer', [ValueError('unrelated'), ExceptionGroup('inner', [InputBlocked('safe')])])
        self.assertTrue(is_guardrail_error(group))
        self.assertFalse(is_guardrail_error(ValueError('ordinary')))

    def test_output_guard_uses_official_block_verdict(self):
        from pydantic_ai import Agent
        from pydantic_ai.models.test import TestModel
        from pydantic_ai_harness import OutputGuardrail
        from pydantic_ai_harness.guardrails import OutputBlocked
        from yolo_agents.guardrails import _output

        secret = 'sk-proj-' + 'S' * 32
        agent = Agent(TestModel(call_tools=[], custom_output_text=secret),
                      capabilities=[OutputGuardrail(_output)])
        with self.assertRaises(OutputBlocked) as error:
            agent.run_sync('hello')
        self.assertNotIn(secret, str(error.exception))

    def test_oversized_ambiguous_prefix_fails_closed(self):
        guard = CredentialStreamFilter()
        with self.assertRaises(RuntimeGuardrailError):
            guard.feed('-----BEGIN ' + 'X' * 70000)


if __name__ == '__main__':
    unittest.main()
