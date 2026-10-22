"""Official SIWC public-client protocol. Never reuse another app's credentials.

Authorization URLs and token records stay backend-only. This protocol layer is
separate from browser/callback lifecycle and from paper-content requests.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from urllib.parse import urlencode, urlsplit
import urllib.error
import urllib.request
import uuid

import jwt

from .secret_store import protect, reveal


ISSUER = 'https://auth.openai.com'
AUTHORIZE = ISSUER + '/api/accounts/authorize'
TOKEN = ISSUER + '/api/accounts/oauth/token'
JWKS = ISSUER + '/.well-known/jwks.json'
DISCOVERY = ISSUER + '/.well-known/openid-configuration'
RESOURCE = 'https://api.openai.com/v1'
SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
DIRECT_SCOPE = 'chatgpt.tokens.use.direct'
TOKEN_FIELDS = ('access_token', 'refresh_token', 'id_token', 'expires_at')


class ChatGPTAuthError(ValueError):
    def __init__(self, message, code='auth', *, permanent=False):
        super().__init__(message)
        self.code, self.permanent = code, permanent


class NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ChatGPTAuthError('认证接口重定向已拒绝，请稍后重试', 'redirect')


def auth_request(url, form=None, *, empty=False):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname != 'auth.openai.com' or parts.port not in (None, 443) or parts.username or parts.query or parts.fragment:
        raise ChatGPTAuthError('认证目标不是受信的 OpenAI 地址', 'destination')
    headers = {'Accept': 'application/json'}
    data = None
    if form is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        data = urlencode(form).encode()
    request = urllib.request.Request(url, data, headers, method='POST' if form is not None else 'GET')
    try:
        with urllib.request.build_opener(NoAuthRedirect()).open(request, timeout=15) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ChatGPTAuthError('认证响应超限', 'format')
            if empty and not raw:
                return {}
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ChatGPTAuthError('认证响应格式无效', 'format')
            return payload
    except urllib.error.HTTPError as exc:
        code = 'auth'
        try:
            body = json.loads(exc.read(16384))
            value = body.get('error')
            value = value.get('code') if isinstance(value, dict) else value
            if value in ('invalid_grant', 'invalid_refresh_token', 'refresh_token_expired',
                         'refresh_token_invalidated', 'refresh_token_reused', 'invalid_client'):
                code = value
        except (ValueError, OSError, AttributeError):
            pass
        raise ChatGPTAuthError('ChatGPT 认证请求失败，请重新登录或稍后重试', code,
                               permanent=code != 'auth') from None
    except (urllib.error.URLError, OSError) as exc:
        raise ChatGPTAuthError('无法连接 ChatGPT 认证服务，请检查网络后重试', 'connection') from None
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ChatGPTAuthError):
            raise
        raise ChatGPTAuthError('认证响应格式无效', 'format') from None


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                prefix=path.name + '-', suffix='.tmp', delete=False) as stream:
            name = Path(stream.name)
            os.chmod(name, 0o600)
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        os.chmod(path, 0o600)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)


@dataclass(repr=False)
class AuthorizationAttempt:
    redirect_uri: str
    host_id: str
    client_id: str
    expected_subject: str | None = None
    registration_id: str | None = None
    id_token_hint: str | None = field(default=None, repr=False)
    reconsent: bool = False
    generation: int = 0
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(48), repr=False)
    expires_at: float = field(default_factory=lambda: time.time() + 600)
    used: bool = False
    cancelled: bool = False
    _lock: object = field(default_factory=threading.Lock, repr=False)

    def authorization_url(self):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('=')
        params = {'client_id': self.client_id, 'ext_agent_host_id': self.host_id,
                  'redirect_uri': self.redirect_uri, 'response_type': 'code', 'scope': SCOPES,
                  'resource': RESOURCE, 'state': self.state, 'nonce': self.nonce,
                  'code_challenge_method': 'S256', 'code_challenge': challenge}
        if self.client_id == 'dynamic_agent_client':
            params['agent_name_hint'] = 'Asteria'
        elif self.id_token_hint:
            params['id_token_hint'] = self.id_token_hint
        if self.reconsent:
            params['prompt'] = 'consent'
        return AUTHORIZE + '?' + urlencode(params)

    def consume(self, query):
        with self._lock:
            if self.used or self.cancelled or time.time() >= self.expires_at:
                raise ChatGPTAuthError('登录回调已过期或已使用，请重新登录', 'state')
            supplied = query.get('state')
            if not isinstance(supplied, str) or not secrets.compare_digest(supplied, self.state):
                raise ChatGPTAuthError('登录回调校验失败', 'state')
            self.used = True
            if query.get('error'):
                raise ChatGPTAuthError('你取消了 ChatGPT 授权，未发送论文', 'denied')
            code = query.get('code')
            client = query.get('client_id', self.client_id)
            if not isinstance(code, str) or not code or len(code) > 8192:
                raise ChatGPTAuthError('回调缺少有效授权码', 'callback')
            if (not isinstance(client, str) or not client or len(client) > 256
                    or client == 'dynamic_agent_client'):
                raise ChatGPTAuthError('注册回调没有颁发客户端标识', 'client')
            if self.client_id != 'dynamic_agent_client' and client != self.client_id:
                raise ChatGPTAuthError('回调客户端与所选连接不一致，原连接保留', 'client')
            return code, client

    def cancel(self):
        with self._lock:
            self.cancelled = True


class ChatGPTAuth:
    def __init__(self, root: Path, *, transport=auth_request, activation_guard=nullcontext):
        # Sibling of data/, intentionally outside library backup/restore.
        self.directory = Path(root) / '.paper-reader-private'
        self.credentials = self.directory / 'chatgpt-auth.json'
        self.host_file = self.directory / 'chatgpt-host.json'
        self.transport = transport
        self.activation_guard = activation_guard
        self._lock = threading.RLock()

    def _load(self):
        if not self.credentials.exists():
            return {'version': 1, 'generation': 0, 'active': None, 'registrations': {}}
        try:
            wrapper = json.loads(self.credentials.read_text(encoding='utf-8'))
            data = json.loads(reveal(wrapper['protected']))
            if (data.get('version') != 1 or type(data.get('generation')) is not int
                    or data['generation'] < 0 or not isinstance(data.get('registrations'), dict)):
                raise ValueError('invalid connection store')
            return data
        except (ValueError, KeyError, OSError):
            raise ChatGPTAuthError('无法读取本机 ChatGPT 加密连接，请重新登录', 'storage') from None

    def _save(self, data):
        try:
            _atomic_json(self.credentials, {'version': 1, 'protected': protect(json.dumps(data, ensure_ascii=False))})
        except (ValueError, OSError):
            raise ChatGPTAuthError('无法安全保存 ChatGPT 连接；未启用明文回退', 'storage') from None

    def host_id(self):
        with self._lock:
            if self.host_file.exists():
                return json.loads(self.host_file.read_text(encoding='utf-8'))['host_id']
            host = 'urn:uuid:' + str(uuid.uuid4())
            _atomic_json(self.host_file, {'host_id': host})
            return host

    def begin(self, callback_uri, *, registration_id=None, reconsent=False):
        parts = urlsplit(callback_uri)
        if (parts.scheme != 'http' or parts.hostname != '127.0.0.1' or parts.path != '/auth/callback'
                or not parts.port or parts.username or parts.query or parts.fragment):
            raise ChatGPTAuthError('登录必须使用本机回调地址', 'callback')
        with self._lock:
            data = self._load()
            selected = data['registrations'].get(registration_id) if registration_id else None
            if registration_id and not selected:
                raise ChatGPTAuthError('所选 ChatGPT 连接不存在', 'client')
            return AuthorizationAttempt(callback_uri, self.host_id(),
                selected['client_id'] if selected else 'dynamic_agent_client',
                expected_subject=selected['subject'] if selected else None,
                registration_id=registration_id, id_token_hint=selected.get('id_token') if selected else None,
                reconsent=reconsent, generation=data['generation'])

    def _identity(self, token, client, nonce=None, subject=None):
        try:
            header = jwt.get_unverified_header(token)
            if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str):
                raise ValueError('unsupported identity key')
            keys = [key for key in self.transport(JWKS)['keys']
                    if key.get('kid') == header['kid'] and key.get('kty') == 'RSA'
                    and key.get('use', 'sig') == 'sig' and key.get('alg', 'RS256') == 'RS256']
            if len(keys) != 1:
                raise ValueError('identity key ambiguous')
            key = jwt.PyJWK.from_dict(keys[0], algorithm='RS256').key
            claims = jwt.decode(token, key, algorithms=['RS256'], audience=client, issuer=ISSUER,
                leeway=5, options={'require': ['iss', 'aud', 'exp', 'iat', 'sub']})
            if not isinstance(claims['sub'], str) or not claims['sub']:
                raise ValueError('missing subject')
            if nonce is not None and (not isinstance(claims.get('nonce'), str)
                    or not secrets.compare_digest(claims['nonce'], nonce)):
                raise ValueError('nonce mismatch')
            if subject is not None and claims['sub'] != subject:
                raise ValueError('selected identity changed')
            return claims
        except (jwt.PyJWTError, ValueError, KeyError, TypeError, AttributeError):
            raise ChatGPTAuthError('ChatGPT 登录身份校验失败，原连接未替换', 'identity') from None

    @staticmethod
    def _token_values(payload, previous=None):
        access, expiry = payload.get('access_token'), payload.get('expires_in')
        if (not isinstance(access, str) or not access or payload.get('token_type', '').lower() != 'bearer'
                or type(expiry) is not int or not 0 < expiry <= 86400):
            raise ChatGPTAuthError('授权没有返回有效使用凭据', 'format')
        scope = payload.get('scope')
        if scope is not None and not isinstance(scope, str):
            raise ChatGPTAuthError('授权范围格式无效', 'format')
        scopes = scope.split() if scope is not None else (previous or {}).get('scopes', [])
        refresh = payload.get('refresh_token', (previous or {}).get('refresh_token'))
        if refresh is not None and (not isinstance(refresh, str) or not refresh):
            raise ChatGPTAuthError('刷新凭据格式无效', 'format')
        return {'access_token': access, 'refresh_token': refresh, 'expires_at': time.time() + expiry,
                'scopes': list(dict.fromkeys(scopes)), 'token_type': 'Bearer'}

    def complete(self, attempt, query):
        code, client = attempt.consume(query)
        payload = self.transport(TOKEN, {'grant_type': 'authorization_code', 'client_id': client,
            'code': code, 'code_verifier': attempt.verifier, 'redirect_uri': attempt.redirect_uri, 'resource': RESOURCE})
        claims = self._identity(payload.get('id_token'), client, attempt.nonce, attempt.expected_subject)
        values = self._token_values(payload)
        identity = hashlib.sha256(json.dumps([ISSUER, client, claims['sub']]).encode()).hexdigest()
        with self.activation_guard(), self._lock:
            data = self._load()
            if attempt.cancelled or data['generation'] != attempt.generation:
                raise ChatGPTAuthError('登录尝试已取消或连接已变化，原连接保留', 'changed')
            old = data['registrations'].get(attempt.registration_id)
            if attempt.registration_id and (not old or old['client_id'] != client or old['subject'] != claims['sub']):
                raise ChatGPTAuthError('所选连接已变化，请重新登录', 'client')
            data['registrations'][identity] = {**values, 'issuer': ISSUER, 'client_id': client,
                'subject': claims['sub'], 'email': claims.get('email') if isinstance(claims.get('email'), str) else None,
                'id_token': payload['id_token'], 'host_id': attempt.host_id,
                'welcome_acknowledged': data['registrations'].get(identity, {}).get('welcome_acknowledged', False)}
            data.update(active=identity, generation=data['generation'] + 1)
            self._save(data)
            return self.status()

    def status(self):
        with self._lock:
            data = self._load()
            records = [{'id': key, 'label': record.get('email') or 'ChatGPT 连接',
                'client_suffix': record['client_id'][-6:], 'active': key == data['active'],
                'signed_in': bool(record.get('access_token')),
                'needs_welcome': bool(record.get('access_token')) and DIRECT_SCOPE in record.get('scopes', [])
                    and not record.get('welcome_acknowledged', False),
                'plan_authorized': {DIRECT_SCOPE, 'resource.invoke'}.issubset(record.get('scopes', []))
                    and bool(record.get('access_token'))}
                for key, record in data['registrations'].items()]
            return {'accounts': records, 'active': data['active'], 'generation': data['generation']}

    @contextmanager
    def commit_guard(self, expected_id, expected_generation):
        with self._lock:
            data = self._load()
            if data['active'] != expected_id or data['generation'] != expected_generation:
                raise ChatGPTAuthError('ChatGPT 连接已变化，结果未保存，请重新确认', 'changed')
            yield

    def acknowledge_welcome(self, registration_id):
        with self._lock:
            data = self._load()
            if data['active'] != registration_id or registration_id not in data['registrations']:
                raise ChatGPTAuthError('连接已变化，请重新查看套餐授权状态', 'changed')
            data['registrations'][registration_id]['welcome_acknowledged'] = True
            self._save(data)
            return self.status()

    def select(self, registration_id):
        with self._lock:
            data = self._load()
            if registration_id not in data['registrations']:
                raise ChatGPTAuthError('所选连接不存在', 'client')
            data.update(active=registration_id, generation=data['generation'] + 1)
            self._save(data)
            return self.status()

    def access_token(self, expected_id, expected_generation):
        with self._lock:
            data = self._load()
            if data['active'] != expected_id or data['generation'] != expected_generation:
                raise ChatGPTAuthError('ChatGPT 连接已变化，请重新确认发送内容', 'changed')
            record = data['registrations'].get(expected_id, {})
            if not {DIRECT_SCOPE, 'resource.invoke'}.issubset(record.get('scopes', [])) or not record.get('access_token'):
                raise ChatGPTAuthError('请先登录并授权使用 ChatGPT 套餐', 'scope')
            if record['expires_at'] <= time.time() + 60:
                if not record.get('refresh_token'):
                    raise ChatGPTAuthError('ChatGPT 登录已过期，请重新登录', 'expired')
                try:
                    payload = self.transport(TOKEN, {'grant_type': 'refresh_token', 'client_id': record['client_id'],
                        'refresh_token': record['refresh_token'], 'resource': RESOURCE})
                except ChatGPTAuthError as exc:
                    if exc.permanent and exc.code != 'invalid_client':
                        for key in TOKEN_FIELDS: record.pop(key, None)
                        data['generation'] += 1
                        self._save(data)
                    raise
                if payload.get('id_token'):
                    self._identity(payload['id_token'], record['client_id'], subject=record['subject'])
                updated = self._token_values(payload, record)
                record.update(updated)
                if payload.get('id_token'): record['id_token'] = payload['id_token']
                self._save(data)
                if not {DIRECT_SCOPE, 'resource.invoke'}.issubset(record['scopes']):
                    raise ChatGPTAuthError('ChatGPT 套餐授权已取消，请重新授权', 'scope')
            return record['access_token']

    def logout(self):
        with self._lock:
            data = self._load()
            record = data['registrations'].get(data['active'], {})
            confirmed = not bool(record.get('refresh_token'))
            if record.get('refresh_token'):
                try:
                    metadata = self.transport(DISCOVERY)
                    if metadata.get('issuer') != ISSUER:
                        raise ChatGPTAuthError('认证发行方不匹配', 'destination')
                    endpoint = metadata.get('revocation_endpoint')
                    if not isinstance(endpoint, str) or urlsplit(endpoint).hostname != 'auth.openai.com':
                        raise ChatGPTAuthError('撤销地址无效', 'destination')
                    self.transport(endpoint, {'token': record['refresh_token'], 'token_type_hint': 'refresh_token',
                        'client_id': record['client_id']}, empty=True)
                    confirmed = True
                except ChatGPTAuthError:
                    confirmed = False
            for key in TOKEN_FIELDS: record.pop(key, None)
            data['generation'] += 1
            self._save(data)
            return {'remote_revocation_confirmed': confirmed,
                    'message': '本机已退出。' if confirmed else '本机已退出，远程撤销未确认；可在 ChatGPT 设置中断开应用。'}
