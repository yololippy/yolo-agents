"""Application-owned SIWC credentials and a loopback-only OAuth callback."""

import asyncio
import base64
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

from pydantic_ai.providers.openai_chatgpt import OpenAIChatGPTCredentials, OpenAIChatGPTOAuthFlow
from pydantic_ai.providers._oauth_provider import jwt_expires_at


def _claims(token):
    try:
        part = token.split('.')[1]
        if len(part) > 32768:
            return {}
        value = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except (AttributeError, IndexError, ValueError, TypeError):
        return {}


def _safe_text(value, limit=320):
    return value if isinstance(value, str) and len(value) <= limit and all(32 <= ord(c) != 127 for c in value) else None


class CredentialStore:
    """Atomic 0600 replacement and a process-wide lease around SDK refresh/use."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise RuntimeError("Invalid credential directory")
        os.chmod(self.directory, 0o700)
        self.path = self.directory / 'credentials.json'
        self._guard = threading.RLock()
        self._lease_lock = threading.Lock()
        self._refresh_active = False

    def _read(self, name):
        try:
            fd = os.open(self.directory / name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65536 or metadata.st_mode & 0o077:
                raise RuntimeError('Credentials have unsafe permissions or format')
            with os.fdopen(fd, 'r', encoding='utf-8', closefd=False) as file:
                return json.load(file)
        finally:
            os.close(fd)

    def _write(self, name, value):
        data = json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()
        fd, tmp = tempfile.mkstemp(prefix='.pending-', dir=self.directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.directory / name)
            dirfd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def read(self):
        with self._guard:
            try:
                data = self._read('credentials.json')
            except (OSError, ValueError, UnicodeError):
                raise RuntimeError('Stored credentials are unavailable') from None
            if data is None:
                return None
            try:
                result = OpenAIChatGPTCredentials(**data)
                if not all(isinstance(x, str) and x for x in (result.client_id, result.access_token, result.refresh_token)):
                    raise ValueError()
                return result
            except (TypeError, ValueError):
                raise RuntimeError('Stored credentials are invalid') from None

    def write(self, credentials):
        with self._guard:
            self._write('credentials.json', asdict(credentials))
            self._write('registration.json', {'client_id': credentials.client_id})
            self._remove_refresh_marker()

    def _remove_refresh_marker(self):
        try:
            (self.directory / 'refresh-pending.json').unlink()
        except FileNotFoundError:
            pass
        dirfd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        self._refresh_active = False

    def begin_refresh(self):
        with self._guard:
            if self.refresh_blocked():
                raise RuntimeError('Refresh outcome is uncertain')
            self._write('refresh-pending.json', {'pending': True})
            self._refresh_active = True

    def begin_authorization_save(self):
        # A new authorized grant may repair an older interrupted refresh. Mark
        # the credential swap before writing either of its on-disk files.
        with self._guard:
            self._write('refresh-pending.json', {'pending': True})
            self._refresh_active = True

    def refresh_failed(self):
        with self._guard:
            self._refresh_active = False

    def refresh_blocked(self):
        with self._guard:
            marker = self.directory / 'refresh-pending.json'
            return not self._refresh_active and (marker.exists() or marker.is_symlink())

    def remote_unconfirmed(self):
        with self._guard:
            marker = self.directory / 'revocation-unconfirmed.json'
            return marker.exists() or marker.is_symlink()

    def mark_remote_unconfirmed(self):
        with self._guard:
            self._write('revocation-unconfirmed.json', {'unconfirmed': True})

    async def load(self):
        credentials = self.read()
        if credentials is None:
            raise RuntimeError('ChatGPT sign-in required')
        return credentials

    async def save(self, credentials):
        try:
            self.write(credentials)
        except BaseException:
            self.refresh_failed()
            raise

    def registration(self):
        with self._guard:
            # The credentials file is committed first. If registration metadata
            # failed after an authorized exchange, reuse that exact issued ID.
            credentials = self._read('credentials.json')
            if isinstance(credentials, dict):
                value = credentials.get('client_id')
                if isinstance(value, str) and 0 < len(value) <= 256:
                    return value
            data = self._read('registration.json') or {}
            value = data.get('client_id') if isinstance(data, dict) else None
            return value if isinstance(value, str) and 0 < len(value) <= 256 else None

    def host_id(self):
        with self._guard:
            data = self._read('host.json')
            if data is None:
                value = uuid4().urn
                self._write('host.json', {'host_id': value})
                return value
            value = data.get('host_id') if isinstance(data, dict) else None
            try:
                valid = isinstance(value, str) and UUID(value).version == 4 and UUID(value).urn == value
            except (ValueError, AttributeError):
                valid = False
            if not valid:
                raise RuntimeError('Host identity is invalid')
            return value

    def clear(self):
        with self._guard:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._remove_refresh_marker()
            dirfd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)

    @contextmanager
    def lease(self):
        with self._lease_lock:
            fd = os.open(self.directory / 'refresh.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def revoke_session(credentials):
    """Best effort; never redirect a request containing a refresh token."""
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open('https://auth.openai.com/.well-known/openid-configuration', timeout=8) as response:
            metadata = json.loads(response.read(65537))
        endpoint = metadata['revocation_endpoint']
        url = urlsplit(endpoint)
        if url.scheme != 'https' or url.hostname != 'auth.openai.com' or url.port not in (None, 443) or url.username or url.password or url.query or url.fragment:
            return False
        body = urlencode({'token': credentials.refresh_token, 'token_type_hint': 'refresh_token',
                          'client_id': credentials.client_id}).encode()
        request = Request(endpoint, data=body, headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
        with opener.open(request, timeout=8) as response:
            return response.status == 200 and response.read(1) == b''
    except Exception:
        return False


class AccountManager:
    def __init__(self, directory, app_name):
        self.store = CredentialStore(directory)
        self.app_name = app_name
        self.host_id = self.store.host_id()
        self._guard = threading.RLock()
        self._pending = None
        self._last_error = None
        self.generation = 0

    def pending(self):
        with self._guard:
            if self._pending and time.monotonic() >= self._pending['deadline']:
                self._cancel_locked('login_expired')
            return self._pending is not None

    def status(self):
        with self._guard:
            pending = self.pending()
            credentials = self.store.read()
            fields = {'status': 'login_required', 'email': None, 'plan_type': None, 'method': 'chatgpt'}
            if credentials:
                claims = _claims(credentials.id_token)
                auth_claim = claims.get('https://api.openai.com/auth') or {}
                fields['email'] = _safe_text(claims.get('email'))
                fields['plan_type'] = _safe_text(auth_claim.get('chatgpt_plan_type'), 100) if isinstance(auth_claim, dict) else None
                expiry = jwt_expires_at(credentials.access_token)
                fields['status'] = ('permission_required' if not credentials.plan_usage_granted else
                                    'expired' if expiry and expiry <= datetime.now(timezone.utc) else 'authenticated')
            if pending:
                fields['status'] = 'login_pending'
            elif self.store.refresh_blocked() or self._last_error == 'credential_persistence_failed':
                fields['status'] = 'login_required'
            error_code = ('credential_persistence_failed' if self.store.refresh_blocked() else
                          self._last_error or ('remote_revocation_unconfirmed' if self.store.remote_unconfirmed() else None))
            if error_code:
                messages = {
                    'login_cancelled': '登录已取消。', 'login_expired': '登录已过期，请重新开始。',
                    'login_denied': '登录未获批准。', 'login_failed': '登录未完成，请重新尝试。',
                    'plan_usage_not_granted': '未授予 ChatGPT 套餐使用权限。',
                    'remote_revocation_unconfirmed': '本地已退出；远程撤销未确认，可在 ChatGPT 设置中断开应用。',
                    'credential_persistence_failed': '授权刷新结果未确认或未能保存，请重新登录。',
                }
                fields['error'] = {'code': error_code,
                                   'message': messages.get(error_code, '账号操作未完成。')}
            return fields

    def principal(self):
        if self._last_error == 'credential_persistence_failed' or self.store.refresh_blocked():
            return None
        credentials = self.store.read()
        if not credentials or not credentials.plan_usage_granted:
            return None
        subject = _claims(credentials.id_token).get('sub')
        if not isinstance(subject, str) or not subject:
            return None
        return hashlib.sha256((subject + '\0' + credentials.client_id).encode()).hexdigest()

    def _cancel_locked(self, error):
        pending, self._pending = self._pending, None
        self._last_error = error
        self.generation += 1
        if pending:
            # shutdown must never run on the serving thread itself.
            pending['timer'].cancel()
            def stop():
                pending['server'].shutdown()
                pending['server'].server_close()
            threading.Thread(target=stop, daemon=True).start()

    def cancel(self):
        with self._guard:
            if self._pending:
                self._cancel_locked('login_cancelled')
            return self.status()

    def start_login(self):
        with self._guard:
            if self.pending():
                raise RuntimeError('login_pending')
            manager = self
            class Callback(BaseHTTPRequestHandler):
                def setup(self):
                    self.request.settimeout(5)
                    super().setup()
                def log_message(self, *args):
                    pass
                def do_GET(self):
                    manager._callback(self)
            class Server(ThreadingHTTPServer):
                daemon_threads = True
            server = Server(('127.0.0.1', 0), Callback)
            server.timeout = 1
            try:
                flow = OpenAIChatGPTOAuthFlow(app_name=self.app_name, host_id=self.host_id,
                    client_id=self.store.registration(), redirect_uri=f'http://127.0.0.1:{server.server_port}/auth/callback')
                url = flow.authorization_url()
                parsed = urlsplit(url)
                if parsed.scheme != 'https' or parsed.hostname != 'auth.openai.com' or parsed.username or parsed.password:
                    raise ValueError('invalid authorization endpoint')
                login_id = str(uuid4())
                self.generation += 1
                self._pending = {'id': login_id, 'flow': flow, 'server': server, 'deadline': time.monotonic() + 600,
                                 'generation': self.generation, 'started': False}
                pending = self._pending
                def expire():
                    with self._guard:
                        if self._pending is pending:
                            self._cancel_locked('login_expired')
                pending['timer'] = threading.Timer(600, expire)
                pending['timer'].daemon = True
                self._last_error = None
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
                thread.start()
                pending['timer'].start()
                return {'login_id': login_id, 'auth_url': url, 'status': 'login_pending'}
            except BaseException:
                server.server_close()
                raise

    def _callback(self, handler):
        try:
            url = urlsplit(handler.path)
            values = parse_qs(url.query, keep_blank_values=True, max_num_fields=30)
            with self._guard:
                pending = self._pending
                if (not pending or self.pending() is False or url.path != '/auth/callback' or len(handler.path) > 8192
                        or any(len(items) != 1 for items in values.values())
                        or not secrets.compare_digest(values.get('state', [''])[0], pending['flow'].state)
                        or pending['started']):
                    handler.send_error(400)
                    return
                pending['started'] = True
                if values.get('error') or not values.get('code', [''])[0]:
                    self._cancel_locked('login_denied')
                    body = b'Authorization was not completed. You can close this tab.'
                    handler.send_response(200)
                    handler.send_header('Content-Length', str(len(body)))
                    handler.send_header('Cache-Control', 'no-store')
                    handler.end_headers()
                    handler.wfile.write(body)
                    return
            body = b'Processing authorization. You can close this tab.'
            handler.send_response(200)
            handler.send_header('Content-Length', str(len(body)))
            handler.send_header('Cache-Control', 'no-store')
            handler.end_headers()
            handler.wfile.write(body)
            try:
                credentials = asyncio.run(pending['flow'].exchange_code(values['code'][0], client_id=values.get('client_id', [None])[0]))
                applied = False
                with self._guard:
                    if self._pending is pending and time.monotonic() < pending['deadline']:
                        self.store.begin_authorization_save()
                        try:
                            self.store.write(credentials)
                        except BaseException:
                            self.store.refresh_failed()
                            raise
                        self._last_error = None if credentials.plan_usage_granted else 'plan_usage_not_granted'
                        self._pending = None
                        self.generation += 1
                        applied = True
                if not applied and not revoke_session(credentials):
                    with self._guard:
                        if self._pending is None:
                            self._last_error = 'remote_revocation_unconfirmed'
                            self.store.mark_remote_unconfirmed()
            except Exception:
                with self._guard:
                    if self._pending is pending:
                        self._pending = None
                        self._last_error = 'login_failed'
                        self.generation += 1
            finally:
                pending['timer'].cancel()
                pending['server'].shutdown()
                pending['server'].server_close()
        except Exception:
            # No request URL, OAuth values, or upstream errors are logged.
            return

    def logout(self):
        with self._guard:
            if self._pending:
                self._cancel_locked('login_cancelled')
            with self.store.lease():
                credentials = self.store.read()
                uncertain = self.store.refresh_blocked()
                confirmed = (revoke_session(credentials) if credentials else True) and not uncertain
                if not confirmed:
                    self.store.mark_remote_unconfirmed()
                self.store.clear()
            self.generation += 1
            self._last_error = None if confirmed else 'remote_revocation_unconfirmed'
            return self.status()

    def close(self):
        with self._guard:
            if self._pending:
                self._cancel_locked('login_cancelled')
