"""Offline contracts for the disposable PR UI chat host; no real provider calls."""

import base64
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import http.client
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from pydantic_ai.providers.openai_chatgpt import OpenAIChatGPTCredentials

from yolo_agents.agent_runtime import (
    DemoRuntime, RuntimeConfigurationError, RuntimeProviderTimeoutError, RuntimeStateError,
)
from yolo_agents.auth import AuthError
from yolo_agents.chatgpt_auth import AccountManager
from yolo_agents.chatgpt_runtime import ChatGPTRuntime
from yolo_trade import pr_test_host as host


def fixture_jwt(**claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'e30.' + payload + '.fixture-signature'


def fixture_credentials(*, expired=False):
    return OpenAIChatGPTCredentials(
        client_id='pr-fixture-client',
        access_token=fixture_jwt(exp=int(time.time()) + (-60 if expired else 3600)),
        refresh_token='pr-fixture-refresh-secret',
        id_token=fixture_jwt(sub='pr-fixture-subject'), scope='chatgpt.tokens.use.direct',
    )


class PRHostTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'production'
        self.source.mkdir(mode=0o700)
        self.private_write(self.source / '.agent-owner.json',
                           json.dumps({'schema_version': 1, 'agent_id': 'yolo-trade'}))
        self.private_write(self.source / 'orders.sqlite3', 'unchanged fixture order ledger')
        self.private_write(self.source / 'app.sqlite3', 'unchanged production conversation data')
        self.account = AccountManager(self.source / 'chatgpt-auth', 'YoloTrade')
        self.account.store.write(fixture_credentials())
        self.addCleanup(self.account.close)
        # Normal provider use may create this existing official lease file.
        with self.account.store.lease():
            pass
        self.run_id = uuid4().hex
        self.run_dir = self.root / 'run'
        self.catalog = {'models': [{'id': 'fixture-model', 'display_name': 'Fixture', 'is_default': True}],
                        'default_model': 'fixture-model', 'catalog_only': True,
                        'auth': {'status': 'authenticated', 'method': 'chatgpt'}}
        self.catalog_calls, self.agent_calls, self.agents, self.clients_closed = [], [], [], []
        self.refresh_expired = False
        self.refreshed = 0
        self.output = None
        self.provider_error = None
        self.delegate = None
        self.account_factory = Mock(side_effect=self.borrow_account)
        self.runtime_factory = Mock(side_effect=self.make_delegate)

    @staticmethod
    def private_write(path, text):
        path.write_text(text)
        path.chmod(0o600)

    def snapshot(self):
        return {str(path.relative_to(self.source)): (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in self.source.rglob('*') if path.is_file()}

    def borrow_account(self, directory, app_name):
        self.assertEqual(directory, self.source / 'chatgpt-auth')
        self.assertEqual(app_name, 'YoloTrade')
        return self.account

    def make_delegate(self, model, data, store, **options):
        case = self

        class FixtureAgent:
            def __init__(self, model, **kwargs):
                self.model, self.options = model, kwargs
                case.agents.append(self)

            async def run(self, prompt, **kwargs):
                case.assertTrue(case.account.store._lease_lock.locked())
                case.agent_calls.append((prompt, kwargs))
                if case.provider_error:
                    raise case.provider_error
                token = prompt.split('Reply with exactly ', 1)[1].split(' ', 1)[0]
                return SimpleNamespace(output=case.output if case.output is not None else token)

        backend = SimpleNamespace(Agent=FixtureAgent,
                                  UsageLimits=lambda **kwargs: SimpleNamespace(kwargs=kwargs))
        with patch('yolo_agents.agent_runtime._load_pydantic_backend', return_value=backend):
            runtime = ChatGPTRuntime(model, data, store, **options)
        self.assertIs(runtime.account, self.account)
        self.assertEqual(runtime.runtime_capabilities.pydantic_tools(), [])
        self.assertFalse((data / 'chatgpt-auth').exists())

        def catalog(**kwargs):
            # This fixture replaces only the network/catalog operation. The real
            # runtime must hold the real official store's cross-process lease.
            case.assertTrue(case.account.store._lease_lock.locked())
            case.catalog_calls.append(kwargs)
            if case.refresh_expired and case.account.status()['status'] == 'expired':
                case.account.store.write(fixture_credentials())
                case.refreshed += 1
            return deepcopy(case.catalog)

        def model_instance(name):
            class Client:
                async def close(self):
                    case.clients_closed.append(name)
            return SimpleNamespace(client=Client())

        runtime._catalog_unlocked = catalog
        runtime._model_instance = model_instance
        self.delegate = runtime
        return runtime

    def create(self, **kwargs):
        return host.create_session(self.run_dir, self.run_id, self.source,
                                   account_factory=self.account_factory,
                                   runtime_factory=self.runtime_factory, **kwargs)

    def session(self):
        session = self.create()
        self.addCleanup(session.close)
        return session

    def start(self, session):
        thread = threading.Thread(target=session.server.serve_forever, daemon=True)
        thread.start()

        def stop():
            session.server.shutdown()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)

    def request(self, session, method, path, data=None, *, token=None, headers=None):
        request_headers = dict(headers or {})
        if token:
            request_headers['Authorization'] = 'Bearer ' + token
        body = None
        if data is not None:
            body = json.dumps(data)
            request_headers['Content-Type'] = 'application/json'
        connection = http.client.HTTPConnection('127.0.0.1', session.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def pair(self, session, manifest, platform):
        code = manifest['mac_pairing_code' if platform == 'macos' else 'ios_pairing_code']
        status, result = self.request(session, 'POST', '/v1/devices/pair', {
            'pairing_code': code, 'device_name': 'PR fixture ' + platform, 'platform': platform,
        })
        self.assertEqual(status, 201)
        self.assertEqual(result['server_id'], str(UUID(self.run_id)))
        self.assertRegex(result['server_id'], r'^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
        self.assertEqual(result['device']['permissions'], ['chat:read', 'chat:write'])
        return result['token']

    def wait_reply(self, session, cid, token, expected_count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, body = self.request(session, 'GET', f'/v1/conversations/{cid}/messages', token=token)
            self.assertEqual(status, 200)
            replies = [item for item in body['messages'] if item['role'] == 'assistant']
            if len(replies) >= expected_count:
                return body['messages']
            time.sleep(.01)
        self.fail('Fixture reply did not finish')

    def test_session_contract_has_unique_shared_primary_private_modes_and_cleanup(self):
        before = self.snapshot()
        session = self.session()
        manifest = json.loads((self.run_dir / 'session.json').read_text())
        self.assertEqual(set(manifest), {'schema_version', 'run_id', 'endpoint', 'mac_pairing_code',
                                        'ios_pairing_code', 'test_conversation_id', 'expires_at'})
        self.assertEqual(manifest['schema_version'], 1)
        self.assertEqual(manifest['run_id'], self.run_id)
        self.assertTrue(manifest['endpoint'].startswith('http://127.0.0.1:'))
        self.assertNotEqual(session.server.server_port, 8765)
        self.assertLess((datetime.fromisoformat(manifest['expires_at']) - datetime.now(timezone.utc)).total_seconds(), 601)
        self.assertEqual(session.server.service.agent_id, 'yolo-trade')
        self.assertEqual(session.server.service.app_name, 'YoloTrade')
        self.assertEqual(session.server.service.runtime.mode, 'chatgpt')
        self.assertIsNone(session.server.extension)
        primary = session.server.service.ensure_primary_conversation()
        self.assertEqual(primary['id'], manifest['test_conversation_id'])
        self.assertEqual(primary['title'], 'PR UI ' + self.run_id)
        for name in ('mac_pairing_code', 'ios_pairing_code'):
            self.assertTrue(manifest[name].startswith(self.run_id + '.'))
        self.assertNotEqual(manifest['mac_pairing_code'], manifest['ios_pairing_code'])
        self.assertEqual((self.run_dir.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((self.run_dir / 'data').stat().st_mode & 0o777), 0o700)
        self.assertEqual(((self.run_dir / 'session.json').stat().st_mode & 0o777), 0o600)
        self.assertFalse(list((self.run_dir / 'data').rglob('credentials.json')))
        self.assertIs(self.delegate.account.store, self.account.store)
        session.close()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual({p.name for p in self.run_dir.iterdir()}, {'report.json'})
        report_text = (self.run_dir / 'report.json').read_text()
        self.assertEqual(json.loads(report_text)['status'], 'closed')
        self.assertNotIn('pr-fixture-refresh-secret', report_text)
        self.assertNotIn(manifest['mac_pairing_code'], report_text)
        with self.assertRaises(RuntimeConfigurationError):
            self.create()

    def test_wrong_product_missing_credentials_modes_and_symlinks_fail_before_account_use(self):
        marker = self.source / '.agent-owner.json'
        original = marker.read_text()
        self.private_write(marker, json.dumps({'schema_version': 1, 'agent_id': 'yolo-beni'}))
        with self.assertRaises(RuntimeConfigurationError):
            self.create()
        self.private_write(marker, original)
        for path in (self.source, self.source / 'chatgpt-auth', marker,
                     self.account.store.path, self.source / 'chatgpt-auth/host.json'):
            previous = path.stat().st_mode & 0o777
            path.chmod(0o755 if path.is_dir() else 0o644)
            try:
                with self.subTest(path=path.name), self.assertRaises(RuntimeConfigurationError):
                    self.create()
            finally:
                path.chmod(previous)
        self.account.store.path.unlink()
        with self.assertRaises(RuntimeConfigurationError):
            self.create()
        self.account.store.write(fixture_credentials())
        alias = self.root / 'alias'
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(RuntimeConfigurationError):
            host.validate_credentials_directory(alias)
        with self.assertRaises(RuntimeConfigurationError):
            host.validate_credentials_directory(self.root / 'missing')
        self.account_factory.assert_not_called()
        self.assertFalse(self.run_dir.exists())

    def test_bad_run_identity_reuse_and_nested_directories_are_rejected(self):
        for value in ('', str(uuid4()), uuid4().hex.upper(), 'f' * 32, '../unsafe'):
            with self.subTest(value=value), self.assertRaises(RuntimeConfigurationError):
                host.create_session(self.run_dir, value, self.source)
        for destination in (self.source, self.source / 'nested', self.root):
            with self.subTest(destination=destination), self.assertRaises(RuntimeConfigurationError):
                host.prepare_run_directory(destination, self.source)
        self.run_dir.mkdir(mode=0o700)
        (self.run_dir / 'old-session').write_text('stale')
        with self.assertRaises(RuntimeConfigurationError):
            self.create()
        self.account_factory.assert_not_called()

    def test_expired_access_token_can_refresh_once_in_existing_lease_and_store(self):
        self.account.store.write(fixture_credentials(expired=True))
        self.assertEqual(self.account.status()['status'], 'expired')
        self.refresh_expired = True
        orders = (self.source / 'orders.sqlite3').read_bytes()
        session = self.session()
        self.assertEqual(self.refreshed, 1)
        self.assertEqual(len(self.catalog_calls), 1)
        self.assertEqual(self.account.status()['status'], 'authenticated')
        self.assertIs(self.delegate.account.store, self.account.store)
        self.assertFalse((self.run_dir / 'data/chatgpt-auth').exists())
        session.close()
        self.assertTrue(self.account.store.path.exists())
        self.assertEqual((self.source / 'orders.sqlite3').read_bytes(), orders)

    def test_unready_authorization_and_pending_or_uncertain_refresh_never_start_catalog(self):
        for status in ('login_required', 'permission_required', 'login_pending'):
            with self.subTest(status=status), tempfile.TemporaryDirectory(dir=self.root) as temp:
                self.run_dir = Path(temp) / 'run'
                with patch.object(self.account, 'status', return_value={'status': status}):
                    with self.assertRaises(RuntimeConfigurationError):
                        self.create()
                self.assertEqual({p.name for p in self.run_dir.iterdir()}, {'report.json'})
        for method in ('pending', 'refresh_blocked'):
            with self.subTest(method=method), tempfile.TemporaryDirectory(dir=self.root) as temp:
                self.run_dir = Path(temp) / 'run'
                target = self.account if method == 'pending' else self.account.store
                with patch.object(target, method, return_value=True):
                    with self.assertRaises(RuntimeConfigurationError):
                        self.create()
        self.runtime_factory.assert_not_called()
        self.assertEqual(self.catalog_calls, [])

    def test_missing_or_unauthenticated_catalog_cleans_up_without_demo_fallback(self):
        before = self.snapshot()
        for catalog in ({'models': [], 'default_model': None, 'auth': {'status': 'authenticated'}},
                        {'models': [{'id': 'fixture-model'}], 'default_model': 'fixture-model',
                         'auth': {'status': 'unavailable'}}):
            with self.subTest(catalog=catalog), tempfile.TemporaryDirectory(dir=self.root) as temp:
                self.run_dir = Path(temp) / 'run'
                self.catalog = catalog
                with self.assertRaises(RuntimeConfigurationError):
                    self.create()
                self.assertFalse((self.run_dir / 'data').exists())
                self.assertFalse((self.run_dir / 'session.json').exists())
                self.assertEqual(json.loads((self.run_dir / 'report.json').read_text())['status'], 'failed')
        self.assertEqual(self.agent_calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_partial_manifest_failure_removes_codes_and_temporary_data_only(self):
        before = self.snapshot()
        write = host._write_private

        def interrupted(path, value):
            if path.name == 'session.json':
                write(path, value)
                raise OSError('fixture interrupted manifest write')
            return write(path, value)

        with patch.object(host, '_write_private', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.create()
        self.assertEqual({path.name for path in self.run_dir.iterdir()}, {'report.json'})
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.delegate._closed)

    def test_one_use_platform_pairing_run_binding_loopback_and_expiry(self):
        session = self.session()
        manifest = json.loads((self.run_dir / 'session.json').read_text())
        auth = session.server.auth
        mac = manifest['mac_pairing_code']
        for code, platform, peer in ((mac, 'ios', '127.0.0.1'), (mac, 'macos', '192.0.2.1'),
                                     (uuid4().hex + mac[32:], 'macos', '127.0.0.1')):
            with self.subTest(platform=platform, peer=peer), self.assertRaises(AuthError):
                auth.pair(code, 'Fixture', platform, peer)
        result = auth.pair(mac, 'Mac', 'macos', '127.0.0.1')
        self.assertEqual(result['server_id'], str(UUID(self.run_id)))
        self.assertEqual(auth.authenticate('Bearer ' + result['token'])['permissions'], ['chat:read', 'chat:write'])
        with self.assertRaises(AuthError):
            auth.pair(mac, 'Mac again', 'macos', '127.0.0.1')
        with self.assertRaises(RuntimeStateError):
            auth.issue_pairing_code('macos')
        self.assertFalse(session.server.verify_request(None, ('192.0.2.1', 123)))
        self.assertTrue(session.server.verify_request(None, ('127.0.0.1', 123)))
        for address in (('0.0.0.0', 0), ('::1', 0), ('127.0.0.1', 8765)):
            with self.subTest(address=address), self.assertRaises(RuntimeConfigurationError):
                host.PRChatHTTPServer(address, session.server.service, auth)
        auth.deadline = 0
        for operation in (lambda: auth.authenticate('Bearer ' + result['token']),
                          lambda: auth.reauthorize(result['device']),
                          lambda: auth.pair(manifest['ios_pairing_code'], 'iOS', 'ios', '127.0.0.1')):
            with self.assertRaises(AuthError) as caught:
                operation()
            self.assertEqual(caught.exception.status, 410)

    def test_mac_and_ios_share_normal_chat_api_and_receive_real_bounded_delegate_text(self):
        before = self.snapshot()
        session = self.session()
        self.start(session)
        manifest = json.loads((self.run_dir / 'session.json').read_text())
        mac = self.pair(session, manifest, 'macos')
        ios = self.pair(session, manifest, 'ios')
        cid = manifest['test_conversation_id']
        for token in (mac, ios):
            status, body = self.request(session, 'GET', '/v1/conversations', token=token)
            self.assertEqual(status, 200)
            self.assertEqual(body['primary_conversation_id'], cid)
            self.assertEqual([item['id'] for item in body['conversations']], [cid])
        with patch.object(DemoRuntime, 'stream_reply', side_effect=AssertionError('demo forbidden')):
            for index, (platform, token, observer) in enumerate((('macos', mac, ios), ('ios', ios, mac)), 1):
                status, _ = self.request(session, 'POST', f'/v1/conversations/{cid}/messages', {
                    'content': host.test_prompt(self.run_id, platform), 'client_message_id': str(uuid4()),
                }, token=token)
                self.assertEqual(status, 202)
                messages = self.wait_reply(session, cid, observer, index)
                reply = messages[-1]
                self.assertEqual(reply['status'], 'completed')
                self.assertEqual(reply['content'], ('MACUI' if platform == 'macos' else 'IOSUI') + self.run_id)
        self.assertEqual(len(self.agent_calls), 2)
        for agent, (prompt, options) in zip(self.agents, self.agent_calls):
            self.assertEqual(set(agent.options), {'instructions', 'retries', 'output_type'})
            self.assertEqual(set(options), {'timeout', 'usage_limits', 'model_settings'})
            self.assertEqual(options['usage_limits'].kwargs['tool_calls_limit'], 0)
            self.assertEqual(options['usage_limits'].kwargs['request_limit'], 1)
            self.assertEqual(options['timeout'], 45)
        self.assertFalse((self.run_dir / 'data/skills').exists())
        self.assertFalse((self.run_dir / 'data/users').exists())
        self.assertEqual(self.snapshot(), before)
        status, body = self.request(session, 'GET', '/v1/events', token=ios)
        self.assertEqual(status, 200)
        self.assertTrue(any(event['type'] == 'assistant.delta' for event in body['events']))

    def test_http_rejects_arbitrary_other_run_prompts_management_media_and_browser_origins(self):
        session = self.session()
        self.start(session)
        manifest = json.loads((self.run_dir / 'session.json').read_text())
        token = self.pair(session, manifest, 'macos')
        cid = manifest['test_conversation_id']
        for content in ('buy something', host.test_prompt(uuid4().hex, 'macos'),
                        host.test_prompt(self.run_id, 'ios'), 'x' * 2049):
            status, _ = self.request(session, 'POST', f'/v1/conversations/{cid}/messages', {
                'content': content, 'client_message_id': str(uuid4()),
            }, token=token)
            self.assertIn(status, (400, 403))
        for method, path in (('GET', '/v1/account'), ('POST', '/v1/account/logout'),
                             ('POST', '/v1/account/login'), ('GET', '/v1/devices'),
                             ('GET', '/v1/trading/orders'), ('POST', '/v1/media'),
                             ('POST', '/v1/conversations'), ('PATCH', f'/v1/conversations/{cid}')):
            self.assertEqual(self.request(session, method, path, {}, token=token)[0], 404)
        for headers in ({'Origin': 'http://example.test'}, {'Host': 'example.test'}):
            self.assertEqual(self.request(session, 'GET', '/v1/health', headers=headers)[0], 403)
        self.assertEqual(self.agent_calls, [])
        self.assertEqual(session.server.service.store.list_messages(cid), [])

    def test_delegate_timeout_becomes_failed_message_without_retry_or_fake_reply(self):
        session = self.session()
        self.start(session)
        manifest = json.loads((self.run_dir / 'session.json').read_text())
        token = self.pair(session, manifest, 'macos')
        cid = manifest['test_conversation_id']
        self.provider_error = RuntimeProviderTimeoutError('fixture provider timeout')
        message = {'content': host.test_prompt(self.run_id, 'macos'), 'client_message_id': str(uuid4())}
        self.assertEqual(self.request(session, 'POST', f'/v1/conversations/{cid}/messages', message, token=token)[0], 202)
        result = self.wait_reply(session, cid, token, 1)
        self.assertEqual(result[-1]['status'], 'failed')
        run_id = result[-1]['run_id']
        status, body = self.request(session, 'GET', f'/v1/events?conversation_id={cid}', token=token)
        self.assertEqual(status, 200)
        run_events = [event for event in body['events'] if event['payload'].get('run_id') == run_id]
        terminal = [event for event in run_events
                    if event['type'] in ('assistant.failed', 'assistant.completed')]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]['type'], 'assistant.failed')
        self.assertEqual(terminal[0]['payload']['message'], result[-1])
        self.assertEqual(terminal[0]['payload']['error']['code'], 'agent_failed')
        self.assertFalse(any(event['type'] == 'assistant.delta' for event in run_events))
        self.assertNotIn('fixture provider timeout', json.dumps(terminal, ensure_ascii=False))
        self.assertNotIn('fixture provider timeout', result[-1]['content'])
        self.assertNotEqual(result[-1]['content'], 'MACUI' + self.run_id)
        self.assertIsNone(session.server.service.store.get_agent_state(cid))
        status, duplicate = self.request(session, 'POST', f'/v1/conversations/{cid}/messages', message, token=token)
        self.assertEqual(status, 202)
        self.assertEqual(duplicate['status'], 'duplicate')
        self.assertEqual(duplicate['run_id'], run_id)
        status, after = self.request(session, 'GET', f'/v1/events?conversation_id={cid}', token=token)
        self.assertEqual(status, 200)
        self.assertEqual(after['events'], body['events'])
        self.assertEqual(len(self.agent_calls), 1)
        self.assertEqual(self.clients_closed, ['fixture-model'])

    def test_message_budget_preserves_duplicates_and_expiry_rejects_new_requests(self):
        session = self.session()
        self.start(session)
        manifest = json.loads((self.run_dir / 'session.json').read_text())
        token = self.pair(session, manifest, 'macos')
        cid = manifest['test_conversation_id']
        path = f'/v1/conversations/{cid}/messages'
        message = {'content': host.test_prompt(self.run_id, 'macos'), 'client_message_id': str(uuid4())}
        with patch.object(host, 'MAX_MESSAGES', 1):
            self.assertEqual(self.request(session, 'POST', path, message, token=token)[0], 202)
            self.wait_reply(session, cid, token, 1)
            status, duplicate = self.request(session, 'POST', path, message, token=token)
            self.assertEqual(status, 202)
            self.assertEqual(duplicate['status'], 'duplicate')
            self.assertEqual(self.request(session, 'POST', path, dict(message, client_message_id=str(uuid4())), token=token)[0], 429)
        runtime = session.server.service.runtime
        runtime.deadline = 0
        with self.assertRaises(RuntimeStateError):
            list(runtime.stream_reply(session.server.service.store.list_messages(cid)[:1], cid))
        session.server.auth.deadline = 0
        self.assertEqual(self.request(session, 'GET', '/v1/health')[0], 410)
        self.assertEqual(len(self.agent_calls), 1)

    def test_deadline_shutdown_and_cli_signal_or_exception_cleanup_never_print_secrets(self):
        server = Mock()
        server.serve_forever.side_effect = lambda **kwargs: None
        stack = Mock()
        session = host.PRSession(self.run_dir, server, stack, {}, time.monotonic() + 60)
        with patch.object(host.threading, 'Timer') as timer:
            session.serve()
        timer.assert_called_once()
        self.assertIs(timer.call_args.args[1], server.shutdown)
        timer.return_value.start.assert_called_once()
        timer.return_value.cancel.assert_called_once()
        for error in (KeyboardInterrupt(), RuntimeError('pr-fixture-refresh-secret')):
            with self.subTest(error=type(error).__name__):
                fake = Mock(root=self.run_dir)
                fake.serve.side_effect = error
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(host, 'create_session', return_value=fake), \
                        patch.object(host.signal, 'signal') as signals, \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    status = host.main(['--run-dir', str(self.run_dir), '--run-id', self.run_id,
                                        '--credentials-data-dir', str(self.source)])
                self.assertEqual(status, 0 if isinstance(error, KeyboardInterrupt) else 1)
                fake.close.assert_called_once()
                self.assertEqual(stdout.getvalue(), str(self.run_dir / 'session.json') + '\n')
                self.assertNotIn('pr-fixture-refresh-secret', stdout.getvalue() + stderr.getvalue())
                self.assertEqual(signals.call_count, 4)


if __name__ == '__main__':
    unittest.main()
