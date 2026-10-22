"""Typed public-source failures; never retain response bodies, URLs or credentials."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import re

DEFAULT_COOLDOWN_SECONDS = 15 * 60
SOURCE_LABELS = {'arxiv': 'arXiv', 'openalex': 'OpenAlex'}


class SourceError(RuntimeError):
    def __init__(self, message, *, source=None, status=None, retry_at=None, retry_after_source=None):
        super().__init__(message)
        self.source, self.status, self.retry_at = source, status, retry_at
        self.retry_after_source = retry_after_source


def retry_deadline(value, now=None):
    """Honor seconds/date headers, never shorten huge valid server delays."""
    now = now or datetime.now(timezone.utc)
    minimum = now + timedelta(seconds=3)
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r'[0-9]+', value):
            digits = value.lstrip('0') or '0'
            try:
                if len(digits) > 12: raise OverflowError()
                return max(minimum, now + timedelta(seconds=int(digits))), 'server'
            except OverflowError:
                return datetime.max.replace(tzinfo=timezone.utc), 'server_overflow'
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None: date = date.replace(tzinfo=timezone.utc)
            return max(minimum, date.astimezone(timezone.utc)), 'server'
        except (TypeError, ValueError, OverflowError, IndexError):
            pass
    return now + timedelta(seconds=DEFAULT_COOLDOWN_SECONDS), 'fallback'


def rate_limit_message(source, retry_at, origin):
    label = SOURCE_LABELS[source]
    stamp = datetime.fromisoformat(retry_at).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    basis = '来源未提供有效等待时间，本机暂缓 15 分钟。' if origin == 'fallback' else ''
    return f'{label} 暂时限流（HTTP 429）。{basis}最早可在 {stamp} 再试；冷却期间不会发送请求，也不会自动重试。'


def http_source_error(source, error):
    if error.code == 429:
        until, origin = retry_deadline(error.headers.get('Retry-After') if error.headers else None)
        retry_at = until.isoformat()
        return SourceError(rate_limit_message(source, retry_at, origin), source=source,
            status=429, retry_at=retry_at, retry_after_source=origin)
    return SourceError(f'{SOURCE_LABELS[source]} 请求失败（HTTP {error.code}）', source=source, status=error.code)
