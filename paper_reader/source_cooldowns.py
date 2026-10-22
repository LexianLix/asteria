"""Persistent, source-scoped desktop gates. Expiry never initiates a request."""
from datetime import datetime, timedelta, timezone
from .sources.errors import DEFAULT_COOLDOWN_SECONDS, SOURCE_LABELS, rate_limit_message


class SourceCooldowns:
    def __init__(self, database, clock=None):
        self.db = database
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def active(self, sources, records=None):
        now = self.clock()
        result = []
        for source in sources:
            if source not in SOURCE_LABELS: continue
            key = 'source-cooldown:' + source
            value = self.db.job_get(key) if records is None else records.get(key, {'status': 'idle'})
            if not value.get('retry_at'): continue
            try:
                until = datetime.fromisoformat(value['retry_at'])
                if until.tzinfo is None: raise ValueError('timezone required')
            except (ValueError, TypeError, OverflowError):
                # A damaged imported derived record cannot silently permit a
                # burst. Repair it once to a finite conservative fallback.
                until = now + timedelta(seconds=DEFAULT_COOLDOWN_SECONDS)
                value = {'source': source, 'status': 'cooldown', 'retry_at': until.isoformat(),
                    'retry_after_source': 'fallback'}
                self.db.job_put(key, value)
            if until > now:
                result.append({**value, 'source': source})
        return max(result, key=lambda row: datetime.fromisoformat(row['retry_at'])) if result else None

    def record(self, error):
        if error.status != 429 or error.source not in SOURCE_LABELS: return None
        until = datetime.fromisoformat(error.retry_at)
        previous = self.active([error.source])
        if previous and datetime.fromisoformat(previous['retry_at']) >= until:
            return previous
        value = {'source': error.source, 'status': 'cooldown', 'http_status': 429,
            'retry_at': error.retry_at, 'retry_after_source': error.retry_after_source}
        self.db.job_put('source-cooldown:' + error.source, value)
        return value

    def failed_job(self, value, requested_source):
        return {'status': 'failed', 'code': 'source_rate_limited', 'source': requested_source,
            'rate_limited_source': value['source'], 'http_status': 429, 'retry_at': value['retry_at'],
            'retry_after_source': value.get('retry_after_source'), 'cooldown': True,
            'error': rate_limit_message(value['source'], value['retry_at'], value.get('retry_after_source')),
            'updated_at': self.clock().isoformat()}
