"""Small provider adapter: typed failures, model discovery and usage accounting."""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


class ModelError(ValueError):
    def __init__(self, message: str, code: str = "format", retryable: bool = False, fatal: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.code, self.retryable, self.fatal = code, retryable, fatal
        self.retry_after = retry_after


class NoModelRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ModelError('模型接口要求重定向，为保护认证信息已拒绝；请填写最终接口地址', 'redirect', fatal=True)


def open_model_request(request, timeout):
    return urllib.request.build_opener(NoModelRedirect()).open(request, timeout=timeout)


def retry_delay(headers) -> float | None:
    value = headers.get('Retry-After') if headers else None
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


def token_count(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


@dataclass
class ModelResult:
    text: str
    usage: dict[str, int | None]
    elapsed: float


class ModelClient:
    def __init__(self, config: dict[str, Any]):
        self.cfg = config
        self.base = config["base_url"].rstrip("/")

    def request(self, suffix: str, data: dict | None = None, timeout: int = 120) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.cfg.get("api_key") and self.cfg["provider"] != "ollama":
            headers["Authorization"] = "Bearer " + self.cfg["api_key"]
        request = urllib.request.Request(self.base + suffix, json.dumps(data, ensure_ascii=False).encode() if data is not None else None,
                                         headers, method="POST" if data is not None else "GET")
        try:
            with open_model_request(request, timeout=timeout) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ModelError('模型返回内容超出安全长度限制', 'format', fatal=True)
                payload = json.loads(raw.decode())
            if not isinstance(payload, dict):
                raise ModelError("模型接口返回的 JSON 不是对象")
            return payload
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ModelError("模型认证失败，请检查 API Key 和权限", "auth", fatal=True) from exc
            if exc.code == 404:
                raise ModelError("模型或接口地址不存在，请检查模型名称和 API 基址", "model", fatal=True) from exc
            if exc.code == 429:
                try:
                    payload = json.loads(exc.read(32768))
                    detail = payload.get("error", {}) if isinstance(payload, dict) else {}
                    quota_codes = {"insufficient_quota", "quota_exceeded", "credit_balance_exhausted", "organization_spend_limit_exceeded",
                                   "project_spend_limit_exceeded", "organization_usage_limit_exceeded"}
                    quota = isinstance(detail, dict) and (detail.get("code") in quota_codes or detail.get("type") == "insufficient_quota")
                except (ValueError, OSError):
                    quota = False
                if quota:
                    raise ModelError("模型服务额度不足，请检查额度后再继续", "quota", fatal=True) from exc
                raise ModelError("模型服务暂时限流，将按退避策略重试", "rate_limit", retryable=True, retry_after=retry_delay(exc.headers)) from exc
            if exc.code >= 500:
                raise ModelError(f"模型服务暂时故障（HTTP {exc.code}）", "server", retryable=True, retry_after=retry_delay(exc.headers)) from exc
            raise ModelError(f"模型请求被拒绝（HTTP {exc.code}），请检查服务参数", "request", fatal=True) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelError("无法连接模型或请求超时，请检查模型服务和网络", "connection", retryable=True, fatal=True) from exc
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ModelError("模型接口返回了无法解析的内容", "format", retryable=True) from exc

    def models(self) -> list[str]:
        payload = self.request("/api/tags" if self.cfg["provider"] == "ollama" else "/models", timeout=15)
        key, name = ("models", "name") if self.cfg["provider"] == "ollama" else ("data", "id")
        values = payload.get(key)
        if not isinstance(values, list):
            raise ModelError("接口未返回有效模型列表")
        return sorted({item[name] for item in values if isinstance(item, dict) and isinstance(item.get(name), str)})

    def generate(self, prompt: str, *, system: str | None = None) -> ModelResult:
        start = time.monotonic()
        if self.cfg["provider"] == "ollama":
            data = {"model": self.cfg["model"], "prompt": prompt, "stream": False}
            if system:
                data["system"] = system
            payload = self.request("/api/generate", data)
            text = payload.get("response")
            usage = {"input_tokens": token_count(payload.get("prompt_eval_count")), "output_tokens": token_count(payload.get("eval_count"))}
        else:
            messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
            payload = self.request("/chat/completions", {"model": self.cfg["model"], "messages": messages, "stream": False})
            try:
                text = payload["choices"][0]["message"]["content"]
            except (KeyError, TypeError, IndexError) as exc:
                raise ModelError("模型没有返回可读的回答", retryable=True) from exc
            raw = payload.get("usage") or {}
            raw = raw if isinstance(raw, dict) else {}
            usage = {"input_tokens": token_count(raw.get("prompt_tokens")), "output_tokens": token_count(raw.get("completion_tokens"))}
        if not isinstance(text, str) or not text.strip():
            raise ModelError("模型返回空内容", retryable=True)
        return ModelResult(text.strip(), usage, round(time.monotonic() - start, 3))
