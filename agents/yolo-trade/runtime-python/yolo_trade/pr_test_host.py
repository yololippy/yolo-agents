"""Short-lived loopback chat host for real Mac/Simulator PR acceptance.

Only this entry point composes the bounded provider reply with disposable chat
storage. It never starts the trading extension, broker, production Host, login
flow, or product tools. Product credentials stay in their original official
AccountManager store, including the cross-process refresh lease.
"""

import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

from yolo_agents.agent_runtime import (
    DemoRuntime, RuntimeConfigurationError, RuntimeStateError, _encode, _prompt,
)
from yolo_agents.api import APIError, ChatHTTPServer, Handler
from yolo_agents.auth import AuthError, DeviceAuth
from yolo_agents.service import ChatService
from yolo_agents.store import Store

from .product import AGENT_ID, DISPLAY_NAME

RUN_TTL_SECONDS = 600
MAX_MESSAGES = 16
MAX_TEXT = 2048


def _private_metadata(path, *, directory=False):
    """Inspect ownership/type/mode only; do not deserialize credential contents."""
    try:
        info = path.lstat()
    except OSError:
        raise RuntimeConfigurationError('Required private source is missing.') from None
    expected = 0o700 if directory else 0o600
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or stat.S_IMODE(info.st_mode) != expected
            or info.st_uid != os.getuid() or (not directory and info.st_nlink != 1)):
        raise RuntimeConfigurationError('Private source ownership or permissions are invalid.')
    return info


def _no_symlinks(path):
    path = Path(os.path.abspath(Path(path).expanduser()))
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise RuntimeConfigurationError('Symlink paths are not supported.')
    return path


def validate_credentials_directory(path):
    """Fail before AccountManager can create or repair any production files."""
    source = _no_symlinks(path)
    _private_metadata(source, directory=True)
    if any((parent / '.agent-owner.json').exists() or (parent / '.agent-owner.json').is_symlink()
           for parent in source.parents):
        raise RuntimeConfigurationError('Product credential directories cannot be nested.')
    marker = source / '.agent-owner.json'
    info = _private_metadata(marker)
    if info.st_size > 4096:
        raise RuntimeConfigurationError('Product identity marker is invalid.')
    try:
        fd = os.open(marker, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, encoding='utf-8') as file:
            identity = json.load(file)
    except (OSError, ValueError, UnicodeError):
        raise RuntimeConfigurationError('Product identity marker is invalid.') from None
    if identity != {'schema_version': 1, 'agent_id': AGENT_ID}:
        raise RuntimeConfigurationError('Credential source is not owned by this product.')
    directory = source / 'chatgpt-auth'
    _private_metadata(directory, directory=True)
    # Existing host identity is mandatory: creating a new authorization here is
    # prohibited. All present store files must retain their existing private mode.
    for name in ('credentials.json', 'host.json'):
        _private_metadata(directory / name)
    for child in directory.iterdir():
        _private_metadata(child)
    return source


def validate_run_id(value):
    try:
        parsed = UUID(value)
        if parsed.version != 4 or parsed.hex != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise RuntimeConfigurationError('run-id must be 32 lowercase hex characters from UUID v4.') from None
    return value


def test_prompt(run_id, platform):
    """Exact harmless prompt shared with the native UI acceptance runner."""
    validate_run_id(run_id)
    if platform not in ('macos', 'ios'):
        raise RuntimeConfigurationError('Unsupported test platform.')
    token = ('MACUI' if platform == 'macos' else 'IOSUI') + run_id
    return ('This is a harmless YoloTrade UI synchronization check. Reply with exactly '
            + token + ' and nothing else. Do not use any tools.')


def _write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=True)
        file.flush()
        os.fsync(file.fileno())


def prepare_run_directory(path, source):
    root = _no_symlinks(path)
    if root == source or source in root.parents or root in source.parents:
        raise RuntimeConfigurationError('Run and credential directories must be independent.')
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        _private_metadata(root, directory=True)
    _private_metadata(root, directory=True)
    if any(root.iterdir()):
        raise RuntimeConfigurationError('Run directory must be empty and cannot be reused.')
    # mkdir is the exclusive run claim, even if two starters observed an empty root.
    data = root / 'data'
    try:
        data.mkdir(mode=0o700)
    except FileExistsError:
        raise RuntimeConfigurationError('Run directory is already claimed.') from None
    return root, data


class RunDeviceAuth(DeviceAuth):
    """Run-scoped, one-use platform codes and expiring chat-only device tokens."""

    def __init__(self, path, run_id, deadline):
        self.run_id, self.deadline = validate_run_id(run_id), deadline
        super().__init__(path)
        os.chmod(path, 0o600)
        # Swift's UUID decoder requires the canonical 8-4-4-4-12 wire format.
        self.server_id = str(UUID(self.run_id))
        with self.connect() as db:
            db.execute("UPDATE settings SET value=? WHERE key='server_id'", (self.server_id,))
        self._issued_platforms = set()

    def _check_lifetime(self):
        if time.monotonic() >= self.deadline:
            raise AuthError('run_expired', '本次验收已结束。', 410)

    def issue_pairing_code(self, platform, ttl=RUN_TTL_SECONDS):
        self._check_lifetime()
        with self.lock:
            if platform in self._issued_platforms:
                raise RuntimeStateError('Each platform receives one pairing code per run.')
            raw = super().issue_pairing_code(platform, min(ttl, self.deadline - time.monotonic()))
            self._issued_platforms.add(platform)
            # DeviceAuth hashes the actual public code; do not keep an unbound code.
            from yolo_agents.auth import digest
            bound = self.run_id + '.' + raw
            self.codes[digest(bound)] = self.codes.pop(digest(raw))
            return bound

    def pair(self, code, name, platform, peer):
        self._check_lifetime()
        try:
            local = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            local = False
        if not local or not isinstance(code, str) or not code.startswith(self.run_id + '.'):
            raise AuthError('invalid_pairing_code', '配对码无效。', 403)
        with self.lock:
            result = super().pair(code, name, platform, peer)
            with self.connect() as db:
                db.execute("UPDATE devices SET permissions='chat:read,chat:write' WHERE id=?",
                           (result['device']['id'],))
            result['device']['permissions'] = ['chat:read', 'chat:write']
            return result

    def authenticate(self, authorization):
        self._check_lifetime()
        return super().authenticate(authorization)

    def reauthorize(self, caller):
        self._check_lifetime()
        return super().reauthorize(caller)

    def clear(self):
        with self.lock:
            self.codes.clear()
            with self.connect() as db:
                db.execute('DELETE FROM devices')


class SafePRRuntime(DemoRuntime):
    """Reuse only the base storage lifecycle; every reply delegates to the provider.

    DemoRuntime.stream_reply is never called. No tools, history or media are sent
    to the independent bounded Agent. Persisted state is disposable UI state.
    """

    mode = 'chatgpt'

    def __init__(self, store, delegate, catalog, deadline, run_id):
        super().__init__(store)
        self.delegate, self.deadline, self.run_id = delegate, deadline, run_id
        self.model_name = catalog['default_model']
        self.prompts = {test_prompt(run_id, platform) for platform in ('macos', 'ios')}

    def _check_lifetime(self):
        self._check_open()
        if time.monotonic() >= self.deadline:
            raise RuntimeStateError('PR acceptance run expired.')

    def list_models(self):
        self._check_lifetime()
        return self.delegate.list_models()

    def validate_model(self, model):
        self._check_lifetime()
        self.delegate.validate_model(model)

    def stream_reply(self, history, conversation_id, user_id='local-user', *, runtime_tools=None):
        self._check_lifetime()
        prompt, message_id = _prompt(history, conversation_id)
        if (prompt not in self.prompts or runtime_tools is not None
                or any(item.get('media') for item in history)):
            raise RuntimeConfigurationError('PR acceptance only supports tool-free text.')
        with self._lock(conversation_id):
            state = self._load(conversation_id, user_id)
            if state and state.get('last_user_message_id') == message_id:
                raise RuntimeStateError('An accepted input cannot be replayed.')
            conversation = self.store.get_conversation(conversation_id)
            reply = self.delegate.acceptance_reply(prompt, model=conversation.get('model'))
            text = reply.get('text')
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                raise RuntimeConfigurationError('Provider returned invalid bounded text.')
            # Save only after the real provider returned. No demo or retry fallback.
            self.store.save_agent_state(conversation_id, _encode({
                'schema_version': 1, 'mode': self.mode, 'user_id': user_id,
                'last_user_message_id': message_id, 'last_reply': text,
                'reply_status': 'completed',
            }))
            yield text


class PRChatService(ChatService):
    def __init__(self, store, runtime):
        super().__init__(store, runtime, workers=1)
        self.admission = threading.Lock()
        self.accepted = 0

    def send(self, conversation_id, content, client_message_id, *, caller=None, media_ids=()):
        self.runtime._check_lifetime()
        if (not isinstance(content, str) or not content.strip() or len(content) > MAX_TEXT
                or media_ids or content not in self.runtime.prompts):
            raise APIError(400, 'invalid_test_message', '验收仅支持短文本消息。')
        if (caller is not None and (caller.get('platform') not in ('macos', 'ios')
                or content != test_prompt(self.runtime.run_id, caller['platform']))):
            raise APIError(403, 'test_platform_mismatch', '消息与验收平台不匹配。')
        from yolo_agents.guardrails import check_text
        check_text(content)
        with self.admission:
            # Duplicates use the normal service contract without consuming budget.
            duplicate = any(m.get('client_message_id') == client_message_id
                            for m in self.store.list_messages(conversation_id))
            if self.accepted >= MAX_MESSAGES and not duplicate:
                raise APIError(429, 'test_message_limit', '本次验收消息已达上限。')
            result = super().send(conversation_id, content, client_message_id,
                                  caller=caller, media_ids=media_ids)
            if result['status'] == 'accepted':
                self.accepted += 1
            return result


class PRHandler(Handler):
    def route(self, method):
        self.server.auth._check_lifetime()
        path = urlsplit(self.path).path.rstrip('/')
        simple = {
            ('GET', '/v1/health'), ('POST', '/v1/devices/pair'),
            ('GET', '/v1/models'), ('GET', '/v1/conversations'),
            ('GET', '/v1/events'),
        }
        # Swift UUID.uuidString uses uppercase hex. Let the normal Handler
        # validate and normalize either case before looking up the conversation.
        message_path = re.fullmatch(r'/v1/conversations/[0-9a-fA-F-]{36}/messages', path)
        if (method, path) not in simple and not (message_path and method in ('GET', 'POST')):
            raise APIError(404, 'not_found', '接口不存在。')
        # Host validation also prevents browser-origin DNS rebinding into loopback.
        expected = f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host') != expected or self.headers.get('Origin'):
            raise APIError(403, 'loopback_required', '仅支持本地原生验收客户端。')
        return super().route(method)


class PRChatHTTPServer(ChatHTTPServer):
    def __init__(self, address, service, auth):
        if address[0] != '127.0.0.1' or address[1] != 0:
            raise RuntimeConfigurationError('PR host requires an ephemeral IPv4 loopback port.')
        super().__init__(address, service, auth)
        self.RequestHandlerClass = PRHandler

    def verify_request(self, request, client_address):
        return ipaddress.ip_address(client_address[0]).is_loopback


def create_session(run_dir, run_id, credentials_data_dir, *, ttl=RUN_TTL_SECONDS,
                   account_factory=None, runtime_factory=None):
    """Build an unserved session; dependency injection is for offline fixtures."""
    validate_run_id(run_id)
    if not 1 <= ttl <= RUN_TTL_SECONDS:
        raise RuntimeConfigurationError('Invalid run lifetime.')
    source = validate_credentials_directory(credentials_data_dir)
    root, data = prepare_run_directory(run_dir, source)
    stack = ExitStack()
    status = {'run_id': run_id, 'status': 'failed', 'business_tools_enabled': False}
    try:
        stack.callback(shutil.rmtree, data)
        # These imports do not install or borrow any host application's auth.
        from yolo_agents.chatgpt_auth import AccountManager
        from yolo_agents.chatgpt_runtime import ChatGPTRuntime
        account = (account_factory or AccountManager)(source / 'chatgpt-auth', DISPLAY_NAME)
        stack.callback(account.close)
        # An expired access token may be refreshed once by the normal provider
        # catalog call under its existing lease. Never start a login or retry an
        # uncertain refresh. Status and principal already honor store markers;
        # keep the explicit marker check at this boundary as well.
        if (account.status().get('status') not in ('authenticated', 'expired')
                or account.store.refresh_blocked() or not account.principal() or account.pending()):
            raise RuntimeConfigurationError('Product provider authorization is not ready.')
        store = Store(data / 'app.sqlite3')
        stack.callback(store.close)
        delegate = (runtime_factory or ChatGPTRuntime)(
            None, data, store, agent_name=AGENT_ID, app_name=DISPLAY_NAME, account_manager=account,
        )
        stack.callback(delegate.close)
        catalog = delegate.list_models()
        available = {item.get('id') for item in catalog.get('models', []) if isinstance(item, dict)}
        if (catalog.get('auth', {}).get('status') != 'authenticated'
                or not catalog.get('default_model') or catalog.get('default_model') not in available):
            raise RuntimeConfigurationError('Product provider model catalog is not ready.')
        deadline = time.monotonic() + ttl
        runtime = SafePRRuntime(store, delegate, catalog, deadline, run_id)
        stack.callback(runtime.close)
        auth = RunDeviceAuth(data / 'devices.sqlite3', run_id, deadline)
        stack.callback(auth.clear)
        service = PRChatService(store, runtime)
        service.agent_id, service.app_name = AGENT_ID, DISPLAY_NAME
        stack.callback(service.close)
        conversation = service.ensure_primary_conversation()
        store.update_conversation(conversation['id'], title='PR UI ' + run_id)
        server = PRChatHTTPServer(('127.0.0.1', 0), service, auth)
        stack.callback(server.server_close)
        session = {
            'schema_version': 1,
            'run_id': run_id,
            'endpoint': f'http://127.0.0.1:{server.server_port}',
            'test_conversation_id': conversation['id'],
            'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat(),
            'mac_pairing_code': auth.issue_pairing_code('macos', ttl),
            'ios_pairing_code': auth.issue_pairing_code('ios', ttl),
        }
        # Register cleanup before writing: ENOSPC or interruption must not leave
        # a partially written manifest containing still usable pairing codes.
        stack.callback((root / 'session.json').unlink, missing_ok=True)
        _write_private(root / 'session.json', session)
        stack.callback(setattr, auth, 'deadline', 0)
        status['status'] = 'closed'
        return PRSession(root, server, stack, status, deadline)
    except BaseException:
        stack.close()
        _write_private(root / 'report.json', status)
        raise


class PRSession:
    def __init__(self, root, server, stack, status, deadline):
        self.root, self.server, self.stack = root, server, stack
        self.status, self.deadline = status, deadline
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.stack.close()
        finally:
            _write_private(self.root / 'report.json', self.status)

    def serve(self):
        timer = threading.Timer(max(0, self.deadline - time.monotonic()), self.server.shutdown)
        timer.daemon = True
        timer.start()
        try:
            self.server.serve_forever(poll_interval=0.2)
        finally:
            timer.cancel()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--credentials-data-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    session = None
    old_handlers = {}

    def stop(signum, frame):
        raise KeyboardInterrupt

    try:
        for event in (signal.SIGTERM, signal.SIGINT):
            old_handlers[event] = signal.signal(event, stop)
        session = create_session(args.run_dir, args.run_id, args.credentials_data_dir)
        # Never print endpoint codes, credential values, exception messages or prompts.
        print(str(session.root / 'session.json'), flush=True)
        session.serve()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print('PR chat host unavailable; check product authorization and private paths.', file=sys.stderr)
        return 1
    finally:
        try:
            if session is not None:
                session.close()
        finally:
            for event, previous in old_handlers.items():
                signal.signal(event, previous)


if __name__ == '__main__':
    sys.exit(main())
