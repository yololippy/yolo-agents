"""Offline SIWC, host isolation, and official PydanticAI provider tests."""

import base64
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import http.client
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
import httpx2
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from pydantic_ai.providers.openai_chatgpt import OpenAIChatGPTCredentials

from yolo_agents.agent_runtime import LoginRequiredError, RuntimeExecutionError, RuntimeStateError
from yolo_agents.chatgpt_auth import AccountManager, CredentialStore
from yolo_agents.chatgpt_runtime import ChatGPTRuntime
from yolo_agents.codex_runtime import CodexBusyError
from yolo_agents.host import create_parser, prepare_agent_directory
from yolo_agents.store import Store
from test_agent_runtime import fake_backend


def jwt(**claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'e30.' + payload + '.signature'


def credential(subject='subject-a', client='client-a', scope='chatgpt.tokens.use.direct', expired=False):
    return OpenAIChatGPTCredentials(client_id=client, access_token=jwt(exp=int(time.time()) + (-60 if expired else 3600)),
        refresh_token='fixture-refresh-secret', id_token=jwt(sub=subject, email='alice@example.test'), scope=scope)


class IdentityTests(unittest.TestCase):
    def test_distinct_defaults_and_cross_agent_nested_and_symlink_rejected(self):
        self.assertNotEqual(create_parser(agent_name='trading-agent').parse_args([]).data_dir,
                            create_parser(agent_name='b-agent', default_port=8766).parse_args([]).data_dir)
        with tempfile.TemporaryDirectory() as temp:
            a, b = Path(temp) / 'a', Path(temp) / 'b'
            prepare_agent_directory(a, 'trading-agent', 'Trading Agent')
            prepare_agent_directory(b, 'b-agent', 'B Agent')
            alias = Path(temp) / 'alias'
            alias.symlink_to(a, target_is_directory=True)
            for path in (a, alias, a / 'nested'):
                with self.subTest(path=path), self.assertRaises(RuntimeError):
                    prepare_agent_directory(path, 'b-agent', 'B Agent', adopt_existing=True)
            with self.assertRaises(RuntimeError):
                prepare_agent_directory(temp, 'trading-agent', adopt_existing=True)

    def test_unmarked_legacy_requires_explicit_adoption_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / 'app.sqlite3'
            data.write_bytes(b'legacy')
            with self.assertRaises(RuntimeError):
                prepare_agent_directory(temp, 'trading-agent')
            prepare_agent_directory(temp, 'trading-agent', adopt_existing=True)
            self.assertEqual(data.read_bytes(), b'legacy')
            with self.assertRaises(RuntimeError):
                prepare_agent_directory(temp, 'b-agent', adopt_existing=True)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = AccountManager(self.root / 'auth', 'Example Agent')
        self.addCleanup(self.manager.close)

    def test_credentials_permissions_host_identity_rotation_and_logout_failure(self):
        first_id = self.manager.host_id
        self.manager.store.write(credential())
        self.assertEqual(os.stat(self.manager.store.directory).st_mode & 0o777, 0o700)
        for name in ('host.json', 'registration.json', 'credentials.json'):
            self.assertEqual(os.stat(self.manager.store.directory / name).st_mode & 0o777, 0o600)
        self.assertEqual(AccountManager(self.root / 'auth', 'Example Agent').host_id, first_id)
        self.assertEqual(self.manager.status()['status'], 'authenticated')
        self.assertNotIn('fixture-refresh-secret', json.dumps(self.manager.status()))
        with patch('yolo_agents.chatgpt_auth.revoke_session', return_value=False):
            value = self.manager.logout()
        self.assertEqual(value['status'], 'login_required')
        self.assertEqual(value['error']['code'], 'remote_revocation_unconfirmed')
        self.assertIsInstance(value['error']['message'], str)
        self.assertIsNone(self.manager.store.read())
        self.assertEqual(self.manager.store.registration(), 'client-a')
        reopened = AccountManager(self.root / 'auth', 'Example Agent')
        self.assertEqual(reopened.status()['error']['code'], 'remote_revocation_unconfirmed')
        reopened.close()

    def test_expired_and_denied_plan_are_explicit(self):
        self.manager.store.write(credential(expired=True))
        self.assertEqual(self.manager.status()['status'], 'expired')
        self.manager.store.write(credential(scope='openid'))
        self.assertEqual(self.manager.status()['status'], 'permission_required')
        self.assertIsNone(self.manager.principal())

    def test_callback_state_cancel_and_client_registration_reuse(self):
        seen = []
        class Flow:
            def __init__(self, **kwargs):
                seen.append(kwargs)
                self.state, self.redirect_uri = 'random-fixture-state', kwargs['redirect_uri']
            def authorization_url(self):
                return 'https://auth.openai.com/oauth/authorize?' + 'state=' + self.state
            async def exchange_code(self, code, *, client_id=None):
                if code != 'ok':
                    raise RuntimeError('secret')
                return credential()
        with patch('yolo_agents.chatgpt_auth.OpenAIChatGPTOAuthFlow', Flow):
            started = self.manager.start_login()
            self.assertEqual(started['status'], 'login_pending')
            address = urlsplit(seen[0]['redirect_uri'])
            conn = http.client.HTTPConnection(address.hostname, address.port, timeout=2)
            conn.request('GET', '/auth/callback?state=wrong&code=ok')
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            conn.close()
            self.assertEqual(self.manager.status()['status'], 'login_pending')
            conn = http.client.HTTPConnection(address.hostname, address.port, timeout=2)
            conn.request('GET', '/auth/callback?state=random-fixture-state&code=ok&client_id=client-a')
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            conn.close()
            deadline = time.monotonic() + 2
            while self.manager.pending() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(self.manager.status()['status'], 'authenticated')
            again = self.manager.start_login()
            self.assertEqual(seen[-1]['client_id'], 'client-a')
            self.assertNotIn('id_token_hint', seen[-1])
            self.assertNotEqual(started['login_id'], again['login_id'])
            result = self.manager.cancel()
            self.assertEqual(result['error']['code'], 'login_cancelled')
            self.assertIn('message', result['error'])

    def test_real_sdk_oauth_flow_checks_nonce_before_persisting_credentials(self):
        for valid_nonce in (False, True):
            with self.subTest(valid_nonce=valid_nonce):
                started = self.manager.start_login()
                params = parse_qs(urlsplit(started['auth_url']).query)
                nonce = params['nonce'][0] if valid_nonce else 'incorrect-nonce'
                async def exchange(url, form, response, **kwargs):
                    self.assertEqual(form['code'], 'synthetic-code')
                    self.assertEqual(form['client_id'], 'oaiapp-fixture')
                    claims = {'iss': 'https://auth.openai.com', 'aud': 'oaiapp-fixture',
                              'sub': 'subject-a', 'nonce': nonce, 'email': 'alice@example.test',
                              'exp': int(time.time()) + 3600}
                    return SimpleNamespace(access_token=jwt(exp=int(time.time()) + 3600),
                        refresh_token='fixture-refresh-secret', id_token=jwt(**claims),
                        scope='openid chatgpt.tokens.use.direct')
                with patch('pydantic_ai.providers.openai_chatgpt._post_token_request', side_effect=exchange):
                    uri = urlsplit(params['redirect_uri'][0])
                    connection = http.client.HTTPConnection(uri.hostname, uri.port, timeout=2)
                    try:
                        connection.request('GET', '/auth/callback?state=' + params['state'][0] +
                                           '&code=synthetic-code&client_id=oaiapp-fixture')
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        response.read()
                    finally:
                        connection.close()
                    deadline = time.monotonic() + 2
                    while self.manager.pending() and time.monotonic() < deadline:
                        time.sleep(.01)
                status = self.manager.status()
                if valid_nonce:
                    self.assertEqual(status['status'], 'authenticated')
                    self.assertEqual(status['email'], 'alice@example.test')
                    self.assertEqual(self.manager.store.registration(), 'oaiapp-fixture')
                else:
                    self.assertEqual(status['error']['code'], 'login_failed')
                    self.assertIsNone(self.manager.store.read())

    def test_refresh_lease_serializes_multiple_threads_and_atomic_replacement(self):
        store = self.manager.store
        in_flight, maximum = [0], [0]
        def work(index):
            with store.lease():
                in_flight[0] += 1
                maximum[0] = max(maximum[0], in_flight[0])
                store.write(credential(client='client-' + str(index)))
                time.sleep(.01)
                in_flight[0] -= 1
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(work, range(5)))
        self.assertEqual(maximum[0], 1)
        self.assertTrue(store.read().client_id.startswith('client-'))
        self.assertFalse(list(store.directory.glob('.pending-*')))

    def test_interrupted_refresh_remains_blocked_across_restart(self):
        self.manager.store.write(credential())
        self.manager.store.begin_refresh()
        reopened = AccountManager(self.root / 'auth', 'Example Agent')
        try:
            self.assertEqual(reopened.status()['status'], 'login_required')
            self.assertEqual(reopened.status()['error']['code'], 'credential_persistence_failed')
            self.assertIsNone(reopened.principal())
        finally:
            reopened.close()

    def test_cancel_during_exchange_does_not_activate_late_credentials(self):
        entered, release, revoked = threading.Event(), threading.Event(), threading.Event()
        class Flow:
            def __init__(self, **kwargs):
                self.state, self.redirect_uri = 'state-fixture', kwargs['redirect_uri']
            def authorization_url(self):
                return 'https://auth.openai.com/oauth/authorize?state=state-fixture'
            async def exchange_code(self, code, *, client_id=None):
                entered.set()
                await asyncio.to_thread(release.wait, 3)
                return credential()
        def failed_revocation(_credential):
            revoked.set()
            return False
        with patch('yolo_agents.chatgpt_auth.OpenAIChatGPTOAuthFlow', Flow), \
             patch('yolo_agents.chatgpt_auth.revoke_session', side_effect=failed_revocation):
            self.manager.start_login()
            uri = urlsplit(self.manager._pending['flow'].redirect_uri)
            conn = http.client.HTTPConnection(uri.hostname, uri.port, timeout=2)
            try:
                conn.request('GET', '/auth/callback?state=state-fixture&code=ok&client_id=client-a')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.manager.cancel()['error']['code'], 'login_cancelled')
            finally:
                release.set()
                conn.close()
            self.assertTrue(revoked.wait(2))
            deadline = time.monotonic() + 2
            while self.manager.status().get('error', {}).get('code') != 'remote_revocation_unconfirmed' and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIsNone(self.manager.store.read())
            self.assertEqual(self.manager.status()['error']['code'], 'remote_revocation_unconfirmed')

    def test_partial_authorization_save_remains_blocked(self):
        class Flow:
            def __init__(self, **kwargs):
                self.state, self.redirect_uri = 'state-fixture', kwargs['redirect_uri']
            def authorization_url(self):
                return 'https://auth.openai.com/oauth/authorize?state=state-fixture'
            async def exchange_code(self, code, *, client_id=None):
                return credential()
        original = self.manager.store._write
        def fail_registration(name, value):
            if name == 'registration.json':
                raise OSError('fixture private error')
            return original(name, value)
        with patch('yolo_agents.chatgpt_auth.OpenAIChatGPTOAuthFlow', Flow), \
             patch.object(self.manager.store, '_write', side_effect=fail_registration):
            self.manager.start_login()
            uri = urlsplit(self.manager._pending['flow'].redirect_uri)
            conn = http.client.HTTPConnection(uri.hostname, uri.port, timeout=2)
            try:
                conn.request('GET', '/auth/callback?state=state-fixture&code=ok&client_id=client-a')
                response = conn.getresponse()
                response.read()
            finally:
                conn.close()
            deadline = time.monotonic() + 2
            while self.manager.pending() and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertEqual(self.manager.status()['status'], 'login_required')
        self.assertEqual(self.manager.status()['error']['code'], 'credential_persistence_failed')
        self.assertIsNone(self.manager.principal())


class ChatGPTRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'app.sqlite3')
        self.addCleanup(self.store.close)
        self.cid = self.store.create_conversation('Test', model='model-a')['id']
        self.backend = fake_backend()
        with patch('yolo_agents.agent_runtime._load_pydantic_backend', return_value=self.backend):
            self.runtime = ChatGPTRuntime(None, self.root, self.store, agent_name='b-agent', app_name='B Agent')
        self.addCleanup(self.runtime.close)
        self.runtime.account.store.write(credential())
        self.runtime._catalog_unlocked = lambda **kwargs: {'models': [{'id': 'model-a'}, {'id': 'model-b'}],
            'default_model': 'model-a', 'auth': {'status': 'authenticated'}}
        self.runtime._model_instance = lambda name: name
        self.runtime._additional_capabilities = lambda: []

    def user(self, text, cid=None):
        cid = cid or self.cid
        self.store.append_message(cid, 'user', text, run_id=str(uuid4()))
        return self.store.list_messages(cid)

    def test_model_change_uses_fresh_official_state_with_visible_product_context(self):
        history = self.user('first')
        self.assertEqual(''.join(self.runtime.stream_reply(history, self.cid)), '真实适配测试回复')
        self.store.append_message(self.cid, 'assistant', '真实适配测试回复')
        self.store.update_conversation(self.cid, model='model-b')
        history = self.user('second')
        self.assertEqual(''.join(self.runtime.stream_reply(history, self.cid)), '真实适配测试回复')
        self.assertEqual([agent.model for agent in self.backend.agents], ['model-a', 'model-b'])
        prompt, kwargs = self.backend.agents[-1].calls[-1]
        self.assertIn('first', prompt)
        self.assertIn('second', prompt)
        self.assertNotIn('conversation', kwargs)
        self.assertEqual(json.loads(self.store.get_agent_state(self.cid))['model_name'], 'model-b')

    def test_account_and_workspace_scope_memory_history_and_saved_state(self):
        first = self.runtime._namespace('local-user')
        history = self.user('hello')
        list(self.runtime.stream_reply(history, self.cid))
        self.runtime.account.store.write(credential(subject='subject-b', client='client-b'))
        second = self.runtime._namespace('local-user')
        self.assertNotEqual(first, second)
        with self.assertRaises(RuntimeStateError):
            list(self.runtime.stream_reply(self.user('next'), self.cid))
        other = self.store.create_conversation('New', model='model-a')['id']
        list(self.runtime.stream_reply(self.user('other', other), other))
        self.assertTrue((self.root / 'users' / first / 'memory.sqlite3').exists())
        self.assertTrue((self.root / 'users' / second / 'memory.sqlite3').exists())

    def test_same_subject_separate_issued_client_registrations_have_separate_namespaces(self):
        first = self.runtime._namespace('local-user')
        self.runtime.account.store.write(credential(subject='subject-a', client='client-b'))
        second = self.runtime._namespace('local-user')
        self.assertNotEqual(first, second)
        self.runtime.account.store.write(credential(subject='subject-a', client='client-a'))
        self.assertEqual(self.runtime._namespace('local-user'), first)

    def test_skills_snapshot_ignores_symlinked_files_and_other_agent_directory(self):
        skills = self.root / 'skills'
        skills.mkdir()
        (skills / 'local').mkdir()
        (skills / 'local' / 'SKILL.md').write_text('---\nname: local\ndescription: local\n---\nOwn notes')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'SKILL.md').write_text('foreign-secret')
        (skills / 'external').symlink_to(outside, target_is_directory=True)
        (skills / 'file-link').mkdir()
        (skills / 'file-link' / 'SKILL.md').symlink_to(outside / 'SKILL.md')
        self.runtime._prepare_skills()
        snapshot = Path(self.runtime._skills_snapshot.name)
        self.assertEqual((snapshot / 'local' / 'SKILL.md').read_text(), (skills / 'local' / 'SKILL.md').read_text())
        self.assertFalse((snapshot / 'external').exists())
        self.assertFalse((snapshot / 'file-link').exists())

    def test_account_mutation_rejected_while_catalog_or_generation_running(self):
        entered, release = threading.Event(), threading.Event()
        self.runtime._catalog_unlocked = lambda: (entered.set(), release.wait(3), {'models': [], 'default_model': None})[-1]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.runtime.list_models)
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(CodexBusyError):
                    self.runtime.logout()
            finally:
                release.set()
            future.result()

    def test_missing_login_never_generates_or_falls_back(self):
        self.runtime.account.store.clear()
        with self.assertRaises(LoginRequiredError):
            list(self.runtime.stream_reply(self.user('hi'), self.cid))
        self.assertEqual(self.backend.agents, [])

    def test_failed_stream_is_durably_claimed_and_same_input_never_replayed(self):
        history = self.user('do not replay')
        self.backend.fail_after_first = True
        with self.assertRaises(RuntimeExecutionError):
            list(self.runtime.stream_reply(history, self.cid))
        self.assertEqual(len(self.backend.agents[0].calls), 1)
        with self.assertRaises(RuntimeStateError):
            list(self.runtime.stream_reply(history, self.cid))
        self.assertEqual(len(self.backend.agents[0].calls), 1)

    def test_missing_memory_returns_tool_feedback_then_model_can_say_no_memory(self):
        from collections import deque
        from pydantic_ai import Agent
        from pydantic_ai.messages import ToolReturnPart
        from pydantic_ai.models.function import DeltaToolCall, FunctionModel

        requests = []
        actions = deque([('read_memory', {'file': 'MEMORY.md'}), '没有找到长期记忆。'])
        async def stream(messages, _info):
            requests.append(list(messages))
            action = actions.popleft()
            if isinstance(action, tuple):
                yield {0: DeltaToolCall(name=action[0], json_args=json.dumps(action[1]))}
            else:
                yield action

        # Real official Agent + Harness and an offline FunctionModel; no
        # provider network request, no seeded MEMORY.md and no fake tool result.
        model = FunctionModel(stream_function=stream)
        from yolo_agents.agent_runtime import _load_pydantic_backend
        self.runtime._backend = _load_pydantic_backend()
        self.runtime._backend.Agent = lambda _model, **kwargs: Agent(model, **kwargs)
        self.runtime._additional_capabilities = lambda: []
        with patch('pydantic_ai.models.ALLOW_MODEL_REQUESTS', False):
            self.assertEqual(''.join(self.runtime.stream_reply(self.user('请读取长期记忆'), self.cid)), '没有找到长期记忆。')
        self.assertEqual(len(requests), 2)
        retry_parts = [part for message in requests[-1] for part in message.parts if isinstance(part, ToolReturnPart)]
        self.assertEqual(len(retry_parts), 1)
        self.assertEqual(retry_parts[0].tool_name, 'read_memory')
        self.assertIn('no memory file', str(retry_parts[0].content).lower())
        self.assertEqual(self.runtime._retry_policy(), {'tools': 2, 'output': 0})

    def test_repeated_missing_memory_correction_is_bounded(self):
        from pydantic_ai import Agent
        from pydantic_ai.models.function import DeltaToolCall, FunctionModel
        from yolo_agents.agent_runtime import _load_pydantic_backend

        calls = []
        async def stream(messages, _info):
            calls.append(None)
            yield {0: DeltaToolCall(name='read_memory', json_args='{"file":"MEMORY.md"}')}
        model = FunctionModel(stream_function=stream)
        self.runtime._backend = _load_pydantic_backend()
        self.runtime._backend.Agent = lambda _model, **kwargs: Agent(model, **kwargs)
        self.runtime._additional_capabilities = lambda: []
        history = self.user('读取空记忆')
        with patch('pydantic_ai.models.ALLOW_MODEL_REQUESTS', False):
            with self.assertRaises(RuntimeExecutionError):
                list(self.runtime.stream_reply(history, self.cid))
        self.assertEqual(len(calls), 3)
        with self.assertRaises(RuntimeStateError):
            list(self.runtime.stream_reply(history, self.cid))
        self.assertEqual(len(calls), 3)

    def test_real_provider_streams_responses_and_lists_public_model_catalog_offline(self):
        root = self.root / 'real-sdk'
        root.mkdir()
        store = Store(root / 'app.sqlite3')
        self.addCleanup(store.close)
        cid = store.create_conversation('Real SDK', model='model-a')['id']
        runtime = ChatGPTRuntime(None, root, store, agent_name='b-agent', app_name='B Agent')
        self.addCleanup(runtime.close)
        runtime.account.store.write(credential())
        received, release, first_chunk = [], threading.Event(), threading.Event()
        response = {'id': 'resp-test', 'object': 'response', 'created_at': int(time.time()), 'model': 'model-a',
                    'status': 'completed', 'output': [{'id': 'msg-test', 'type': 'message', 'role': 'assistant',
                    'status': 'completed', 'content': [{'type': 'output_text', 'text': 'Hello world', 'annotations': []}]}],
                    'usage': {'input_tokens': 5, 'output_tokens': 2, 'total_tokens': 7,
                              'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}}}
        def frame(value):
            return ('data: ' + json.dumps(value) + '\n\n').encode()
        class Stream(httpx2.AsyncByteStream):
            async def __aiter__(self):
                initial = dict(response, status='in_progress', output=[])
                yield frame({'type': 'response.created', 'response': initial, 'sequence_number': 0})
                yield frame({'type': 'response.output_item.added', 'output_index': 0, 'sequence_number': 1,
                    'item': {'id': 'msg-test', 'type': 'message', 'role': 'assistant', 'status': 'in_progress', 'content': []}})
                yield frame({'type': 'response.content_part.added', 'item_id': 'msg-test', 'output_index': 0,
                    'content_index': 0, 'sequence_number': 2, 'part': {'type': 'output_text', 'text': '', 'annotations': []}})
                yield frame({'type': 'response.output_text.delta', 'item_id': 'msg-test', 'output_index': 0,
                    'content_index': 0, 'delta': 'Hello ', 'sequence_number': 3})
                await asyncio.to_thread(release.wait, 3)
                yield frame({'type': 'response.output_text.delta', 'item_id': 'msg-test', 'output_index': 0,
                    'content_index': 0, 'delta': 'world', 'sequence_number': 4})
                yield frame({'type': 'response.output_text.done', 'item_id': 'msg-test', 'output_index': 0,
                    'content_index': 0, 'text': 'Hello world', 'sequence_number': 5})
                yield frame({'type': 'response.output_item.done', 'output_index': 0, 'item': response['output'][0], 'sequence_number': 6})
                yield frame({'type': 'response.completed', 'response': response, 'sequence_number': 7})
                yield b'data: [DONE]\n\n'
        def handler(request):
            received.append((request.method, request.url.path, request.content, dict(request.headers)))
            if request.url.path == '/v1/models':
                return httpx2.Response(200, json={'models': [{'slug': 'model-a', 'display_name': 'Model A', 'visibility': 'list'},
                    {'slug': 'hidden', 'display_name': 'Hidden', 'visibility': 'hide'}]})
            return httpx2.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=Stream())
        async_client = httpx2.AsyncClient
        def mock_client(*args, **kwargs):
            return async_client(*args, transport=httpx2.MockTransport(handler), **kwargs)
        store.append_message(cid, 'user', 'hello', run_id=str(uuid4()))
        pieces = []
        def consume():
            for piece in runtime.stream_reply(store.list_messages(cid), cid):
                pieces.append(piece)
                first_chunk.set()
        with patch('yolo_agents.chatgpt_runtime.httpx2', SimpleNamespace(AsyncClient=mock_client, Timeout=httpx2.Timeout)):
            catalog = runtime.list_models()
            self.assertEqual([item['id'] for item in catalog['models']], ['model-a'])
            self.assertNotIn('alice@example.test', json.dumps(catalog))
            self.assertEqual(runtime._owned_loops, set())
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(consume)
                try:
                    ready = first_chunk.wait(2)
                    if not ready and future.done():
                        future.result()
                    self.assertTrue(ready, 'first delta should arrive before completion')
                    self.assertEqual(''.join(pieces), 'Hello ')
                finally:
                    release.set()
                future.result(timeout=4)
        self.assertEqual(''.join(pieces), 'Hello world')
        requests = [item for item in received if item[1] == '/v1/responses']
        self.assertEqual(len(requests), 1)
        body = json.loads(requests[0][2])
        self.assertTrue(body['stream'])
        self.assertFalse(body['store'])
        self.assertEqual(body['model'], 'model-a')
        self.assertEqual(requests[0][3]['authorization'], 'Bearer ' + runtime.account.store.read().access_token)
        self.assertNotIn('fixture-refresh-secret', json.dumps(body))
        self.assertEqual(sum(path == '/v1/models' for _, path, _, _ in received), 2)

    def test_real_provider_refresh_is_serial_across_worker_loops_and_persisted(self):
        root = self.root / 'refresh-sdk'
        root.mkdir()
        store = Store(root / 'app.sqlite3')
        self.addCleanup(store.close)
        runtime = ChatGPTRuntime(None, root, store, agent_name='b-agent', app_name='B Agent')
        self.addCleanup(runtime.close)
        runtime.account.store.write(credential(expired=True))
        calls = []
        def handler(request):
            calls.append((request.url.host, request.url.path))
            if request.url.host == 'auth.openai.com':
                return httpx2.Response(200, json={'access_token': jwt(exp=int(time.time()) + 3600),
                    'refresh_token': 'rotated-fixture-secret', 'scope': 'chatgpt.tokens.use.direct'})
            return httpx2.Response(200, json={'models': [{'slug': 'model-a', 'display_name': 'A', 'visibility': 'list'}]})
        actual_client = httpx2.AsyncClient
        def mock_client(*args, **kwargs):
            return actual_client(*args, transport=httpx2.MockTransport(handler), **kwargs)
        with patch('yolo_agents.chatgpt_runtime.httpx2', SimpleNamespace(AsyncClient=mock_client, Timeout=httpx2.Timeout)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: runtime.list_models(), range(2)))
        self.assertTrue(all(result['auth']['status'] == 'authenticated' for result in results))
        self.assertEqual(sum(host == 'auth.openai.com' for host, _ in calls), 1)
        self.assertEqual(runtime.account.store.read().refresh_token, 'rotated-fixture-secret')
        self.assertEqual(runtime.account.status()['status'], 'authenticated')

    def test_refresh_persistence_failure_has_safe_explicit_account_error(self):
        root = self.root / 'failed-refresh'
        root.mkdir()
        store = Store(root / 'app.sqlite3')
        self.addCleanup(store.close)
        runtime = ChatGPTRuntime(None, root, store, agent_name='b-agent', app_name='B Agent')
        self.addCleanup(runtime.close)
        runtime.account.store.write(credential(expired=True))
        calls = []
        def handler(request):
            calls.append(request.url.path)
            return httpx2.Response(200, json={'access_token': jwt(exp=int(time.time()) + 3600),
                'refresh_token': 'new-secret', 'scope': 'chatgpt.tokens.use.direct'})
        actual_client = httpx2.AsyncClient
        def mock_client(*args, **kwargs):
            return actual_client(*args, transport=httpx2.MockTransport(handler), **kwargs)
        with patch('yolo_agents.chatgpt_runtime.httpx2', SimpleNamespace(AsyncClient=mock_client, Timeout=httpx2.Timeout)), \
             patch.object(runtime.account.store, 'write', side_effect=OSError('fixture-secret')):
            catalog = runtime.list_models()
        self.assertEqual(catalog['auth']['status'], 'unavailable')
        error = runtime.account.status()['error']
        self.assertEqual(error['code'], 'credential_persistence_failed')
        self.assertIsInstance(error['message'], str)
        self.assertNotIn('fixture-secret', json.dumps(error))
        self.assertEqual(runtime.account.status()['status'], 'login_required')
        self.assertIsNone(runtime.account.principal())
        self.assertEqual(runtime.list_models()['auth']['status'], 'login_required')
        self.assertEqual(len(calls), 1)
        reopened = AccountManager(root / 'chatgpt-auth', 'B Agent')
        try:
            self.assertEqual(reopened.status()['error']['code'], 'credential_persistence_failed')
            self.assertIsNone(reopened.principal())
        finally:
            reopened.close()

    def test_failed_refresh_is_not_replayed_by_provider_or_after_restart(self):
        root = self.root / 'failed-grant'
        root.mkdir()
        store = Store(root / 'app.sqlite3')
        self.addCleanup(store.close)
        runtime = ChatGPTRuntime(None, root, store, agent_name='b-agent', app_name='B Agent')
        self.addCleanup(runtime.close)
        runtime.account.store.write(credential(expired=True))
        calls = []
        def handler(request):
            calls.append(request.url.path)
            return httpx2.Response(503, json={'error': 'temporary_failure'})
        actual_client = httpx2.AsyncClient
        def mock_client(*args, **kwargs):
            return actual_client(*args, transport=httpx2.MockTransport(handler), **kwargs)
        with patch('yolo_agents.chatgpt_runtime.httpx2', SimpleNamespace(AsyncClient=mock_client, Timeout=httpx2.Timeout)):
            self.assertEqual(runtime.list_models()['auth']['status'], 'unavailable')
            self.assertEqual(runtime.list_models()['auth']['status'], 'login_required')
        self.assertEqual(len(calls), 1)
        self.assertTrue(runtime.account.store.refresh_blocked())

    def test_refreshed_identity_must_match_existing_subject_and_client(self):
        root = self.root / 'wrong-refresh-identity'
        root.mkdir()
        store = Store(root / 'app.sqlite3')
        self.addCleanup(store.close)
        runtime = ChatGPTRuntime(None, root, store, agent_name='b-agent', app_name='B Agent')
        self.addCleanup(runtime.close)
        runtime.account.store.write(credential(expired=True))
        calls = []
        def handler(request):
            calls.append(request.url.path)
            return httpx2.Response(200, json={'access_token': jwt(exp=int(time.time()) + 3600),
                'refresh_token': 'new-fixture-secret', 'scope': 'chatgpt.tokens.use.direct',
                'id_token': jwt(iss='https://auth.openai.com', aud='client-a', sub='other-subject',
                                exp=int(time.time()) + 3600)})
        actual_client = httpx2.AsyncClient
        def mock_client(*args, **kwargs):
            return actual_client(*args, transport=httpx2.MockTransport(handler), **kwargs)
        with patch('yolo_agents.chatgpt_runtime.httpx2', SimpleNamespace(AsyncClient=mock_client, Timeout=httpx2.Timeout)):
            self.assertEqual(runtime.list_models()['auth']['status'], 'unavailable')
        self.assertEqual(len(calls), 1)
        self.assertTrue(runtime.account.store.refresh_blocked())
        self.assertIsNone(runtime.account.principal())


if __name__ == '__main__':
    unittest.main()
