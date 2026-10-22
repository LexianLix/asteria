"""ChatGPT plan inference: public Responses, complete SSE only, no billing fallback."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .chatgpt_auth import ChatGPTAuthError, RESOURCE
from .model_client import ModelError, ModelResult, open_model_request, token_count, retry_delay


LIMIT = 16 * 1024 * 1024


def plan_error(payload, status=None, request_id=None):
    detail = payload.get('error', {}) if isinstance(payload, dict) else {}
    code = detail.get('code') if isinstance(detail, dict) else None
    diagnostic = {'http_status': status, 'request_id': request_id,
                  'body_shape': 'error' if detail else 'detail' if isinstance(payload, dict) and 'detail' in payload else 'other'}
    if isinstance(code, str) and len(code) < 150:
        diagnostic['provider_code'] = code
    if code == 'subscription_sharing_usage_limit_exceeded':
        error = ModelError('ChatGPT 套餐或本应用用量已达限制，请在 ChatGPT 设置中管理用量', 'quota', fatal=True)
    elif code in ('subscription_sharing_usage_unavailable', 'subscription_sharing_user_unavailable') or status == 503:
        error = ModelError('ChatGPT 套餐服务暂时不可用，连接保留，可稍后重试', 'server', retryable=True)
    elif code in ('subscription_sharing_user_not_eligible', 'subscription_sharing_route_not_supported',
                  'chatpass_v2_scope_not_authorized', 'chatpass_v2_invalid_authorization_context') or status == 403:
        error = ModelError('当前 ChatGPT 账号或授权不允许此操作，请检查套餐权限；不会切换计费方式', 'permission', fatal=True)
    elif status == 401 or code == 'subscription_sharing_invalid_user':
        error = ModelError('ChatGPT 登录授权失效，请重新登录', 'auth', fatal=True)
    elif status == 429:
        error = ModelError('ChatGPT 服务暂时限流，请稍后重试', 'rate_limit', retryable=True)
    else:
        error = ModelError('ChatGPT 请求未完成，原内容保留；请检查模型或重新确认后重试', 'request', fatal=True)
    error.diagnostic = diagnostic
    return error


def read_sse(stream):
    """Do not commit deltas before a real completed response and completed status."""
    data, chunks, total = [], [], 0
    while True:
        line = stream.readline(1024 * 1024 + 1)
        total += len(line)
        if total > LIMIT or len(line) > 1024 * 1024:
            raise ModelError('ChatGPT 流式响应超限，未保存部分内容', 'format', fatal=True)
        if not line:
            raise ModelError('ChatGPT 流式连接中断，未保存部分内容，请明确重试', 'connection', fatal=True)
        if line.strip():
            if line.startswith(b'data:'):
                data.append(line[5:].strip())
            continue
        if not data:
            continue
        raw = b'\n'.join(data); data = []
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError('event not object')
        except (ValueError, UnicodeError):
            raise ModelError('ChatGPT 流式事件格式错误，未保存部分内容', 'format', fatal=True) from None
        kind = event.get('type')
        if kind == 'response.output_text.delta':
            delta = event.get('delta')
            if not isinstance(delta, str):
                raise ModelError('ChatGPT 文本片段格式错误', 'format', fatal=True)
            chunks.append(delta)
        elif kind in ('response.failed', 'error'):
            payload = event.get('response', event)
            raise plan_error(payload)
        elif kind == 'response.incomplete':
            raise ModelError('ChatGPT 输出不完整，原内容保留，请明确重试', 'incomplete', fatal=True)
        elif kind == 'response.completed':
            response = event.get('response', {})
            if response.get('status') != 'completed':
                raise ModelError('ChatGPT 完成状态无效，未保存结果', 'format', fatal=True)
            final = ''.join(content.get('text', '') for item in response.get('output', [])
                if isinstance(item, dict) and item.get('type') == 'message'
                for content in item.get('content', []) if isinstance(content, dict) and content.get('type') == 'output_text')
            partial = ''.join(chunks)
            if final and partial and final != partial:
                raise ModelError('ChatGPT 完成文本与流式片段不一致，未保存结果', 'format', fatal=True)
            text = final or partial
            if not text.strip():
                raise ModelError('ChatGPT 完成响应没有可读文字', 'format', fatal=True)
            usage = response.get('usage') or {}
            if not isinstance(usage, dict): usage = {}
            return text.strip(), {'input_tokens': token_count(usage.get('input_tokens')),
                                  'output_tokens': token_count(usage.get('output_tokens'))}


class ChatGPTModelClient:
    def __init__(self, cfg, auth, *, opener=None):
        self.cfg, self.auth, self.opener = cfg, auth, opener or open_model_request
        if cfg.get('base_url', '').rstrip('/') != RESOURCE:
            raise ModelError('ChatGPT 套餐仅使用固定官方接口地址', 'destination', fatal=True)
        self.account, self.generation = cfg.get('chatgpt_account_id'), cfg.get('chatgpt_generation')

    def _open(self, suffix, data=None):
        if suffix not in ('/models', '/responses'):
            raise ModelError('ChatGPT 套餐请求路径不支持', 'request', fatal=True)
        try:
            credential = self.auth.access_token(self.account, self.generation)
        except ChatGPTAuthError as exc:
            raise ModelError(str(exc), 'connection' if exc.code == 'connection' else 'auth', fatal=True) from None
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        request = urllib.request.Request(RESOURCE + suffix, body,
            {'Authorization': 'Bearer ' + credential, 'Content-Type': 'application/json',
             'Accept': 'text/event-stream' if data is not None else 'application/json'},
            method='POST' if data is not None else 'GET')
        try:
            return self.opener(request, timeout=120)
        except urllib.error.HTTPError as exc:
            try: payload = json.loads(exc.read(32768))
            except (ValueError, OSError): payload = {}
            error = plan_error(payload, exc.code, exc.headers.get('x-request-id') if exc.headers else None)
            error.retry_after = retry_delay(exc.headers)
            raise error from None
        except (urllib.error.URLError, OSError):
            raise ModelError('ChatGPT 连接失败，原内容保留，请检查网络后明确重试', 'connection', fatal=True) from None

    def _unchanged(self):
        status = self.auth.status()
        if status['active'] != self.account or status['generation'] != self.generation:
            raise ModelError('ChatGPT 连接已变化，结果未保存，请重新预览并确认', 'auth', fatal=True)

    def catalog(self):
        with self._open('/models') as response:
            raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ModelError('ChatGPT 模型目录超限', 'format', fatal=True)
        try:
            payload = json.loads(raw)
            values = payload['models']
            if not isinstance(values, list): raise ValueError('bad model list')
            result = []
            for value in values:
                if (isinstance(value, dict) and value.get('visibility') == 'list'
                        and isinstance(value.get('slug'), str) and value['slug']
                        and isinstance(value.get('display_name'), str)):
                    result.append({'id': value['slug'], 'name': value['display_name']})
        except (ValueError, KeyError, TypeError):
            raise ModelError('ChatGPT 没有返回有效账号模型目录', 'format', fatal=True) from None
        self._unchanged()
        return result

    def models(self):
        return [entry['id'] for entry in self.catalog()]

    def generate(self, prompt, *, system=None):
        started = time.monotonic()
        body = {'model': self.cfg['model'], 'input': [{'role': 'user', 'content': prompt}],
                'store': False, 'stream': True}
        if system: body['instructions'] = system
        try:
            with self._open('/responses', body) as response:
                text, usage = read_sse(response)
        except (OSError, UnicodeError):
            raise ModelError('ChatGPT 流式连接中断，原内容保留，请明确重试', 'connection', fatal=True) from None
        self._unchanged()
        return ModelResult(text, usage, time.monotonic() - started)
