"""No live source/model calls: replay a real HTTP-429 shape through production APIs."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from paper_reader.desktop_api import install_desktop
from paper_reader.settings import Settings
from paper_reader.storage import Database
from paper_reader.sources.arxiv import ArxivSource, SourceError
from paper_reader.sources.openalex import OpenAlexSource
from paper_reader.sources.errors import retry_deadline
from paper_reader.sync import sync_arxiv


def limited(header='3600'):
    headers = Message()
    if header is not None: headers['Retry-After'] = header
    return urllib.error.HTTPError('https://export.arxiv.org/api/query', 429, 'Too Many Requests', headers, None)


class SourceRateErrorTest(unittest.TestCase):
    def test_retry_after_seconds_and_http_date_use_same_utc_deadline(self):
        now = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)
        expected = now + timedelta(seconds=120)
        for header in ('120', format_datetime(expected, usegmt=True)):
            self.assertEqual(retry_deadline(header, now), (expected, 'server'))

    def test_invalid_missing_and_negative_headers_use_explicit_finite_fallback(self):
        now = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)
        for value in (None, '', '-1', '1.5', 'not-a-date'):
            with self.subTest(value=value):
                self.assertEqual(retry_deadline(value, now), (now + timedelta(minutes=15), 'fallback'))

    def test_zero_past_and_huge_values_never_create_immediate_or_shorter_wait(self):
        now = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)
        for value in ('0', format_datetime(now - timedelta(days=1), usegmt=True)):
            self.assertEqual(retry_deadline(value, now)[0], now + timedelta(seconds=3))
        self.assertEqual(retry_deadline('9' * 1000, now), (datetime.max.replace(tzinfo=timezone.utc), 'server_overflow'))

    def test_openalex_is_typed_separately_and_other_http_errors_are_not_cooldowns(self):
        with patch('urllib.request.urlopen', side_effect=limited()):
            with self.assertRaises(SourceError) as caught:
                OpenAlexSource('synthetic-test', None).fetch(keywords=[], max_results=1, days_back=1)
        self.assertEqual(caught.exception.source, 'openalex')
        self.assertEqual(caught.exception.status, 429)
        error = urllib.error.HTTPError('https://example.invalid', 403, 'Forbidden', Message(), None)
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(SourceError) as caught:
                ArxivSource('synthetic-test').fetch(categories=[], keywords=[])
        self.assertEqual(caught.exception.status, 403)
        self.assertIsNone(caught.exception.retry_at)

    def test_429_retains_typed_source_status_and_retry_deadline_without_retry(self):
        with patch('urllib.request.urlopen', side_effect=limited()) as network:
            with self.assertRaises(SourceError) as caught:
                ArxivSource('synthetic-test').fetch(categories=['cs.AI'], keywords=[])
        self.assertEqual(network.call_count, 1)
        self.assertEqual(getattr(caught.exception, 'status', None), 429)
        self.assertEqual(getattr(caught.exception, 'source', None), 'arxiv')
        self.assertIn('限流', str(caught.exception))
        deadline = datetime.fromisoformat(caught.exception.retry_at)
        self.assertGreater(deadline, datetime.now(timezone.utc) + timedelta(minutes=59))


class SourceCooldownAPITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='paper-source-cooldown-')
        root = Path(self.temp.name)
        self.settings = replace(Settings.from_env(), root_dir=root, data_dir=root / 'data',
            db_path=root / 'data/database.sqlite', papers_dir=root / 'data/papers', topics_path=root / 'topics.json')
        self.settings.ensure_dirs()
        self.open()
        self.service.save_config({'auto_backup': False, 'daily_enabled': False})
        self.pid, _ = self.db.upsert_paper({'source': 'local', 'source_id': 'synthetic-existing', 'title': 'Existing paper'})
        self.note = self.db.add_annotation(self.pid, {'page_number': 0, 'annotation_type': 'note', 'text': 'Existing note'})

    def open(self):
        self.db = Database(self.settings.db_path)
        self.app = FastAPI(); self.service = install_desktop(self.app, self.settings, self.db)
        self.client = TestClient(self.app)
        self.headers = {'X-Desktop-Token': self.client.get('/desktop/bootstrap').json()['token']}

    def close(self):
        self.service.shutdown(); self.client.close(); self.db.close()

    def tearDown(self):
        self.close(); self.temp.cleanup()

    def start(self, source='arxiv'):
        response = self.client.post('/desktop/sync', headers=self.headers, json={'source': source})
        self.assertEqual(response.status_code, 202)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.db.job_get('desktop-sync')
            if job['status'] != 'running': return job
            time.sleep(.01)
        self.fail('bounded source worker did not stop')

    def test_repeat_clicks_and_restart_do_not_reissue_429_request(self):
        with patch('urllib.request.urlopen', side_effect=limited()) as network:
            first = self.start()
            self.assertEqual(first.get('code'), 'source_rate_limited')
            self.assertEqual(first.get('http_status'), 429)
            self.assertIn('retry_at', first)
            for _ in range(3): self.start()
            self.assertEqual(network.call_count, 1)
            self.close(); self.open()
            restarted = self.start()
            self.assertEqual(network.call_count, 1)
            self.assertEqual(restarted['retry_at'], first['retry_at'])
            self.assertEqual(self.db.list_annotations(self.pid), [self.note])

    def test_all_source_cannot_bypass_cooldown_and_expiry_does_not_auto_retry(self):
        with patch('urllib.request.urlopen', side_effect=limited()) as network:
            first = self.start()
            self.start('all')
            self.assertEqual(network.call_count, 1)
            deadline = datetime.fromisoformat(first['retry_at'])
            self.app.state.source_cooldowns.clock = lambda: deadline + timedelta(seconds=1)
            self.assertEqual(network.call_count, 1)
            with patch('paper_reader.desktop_api.sync_arxiv', return_value={'fetched': 0}) as fetch:
                self.assertEqual(self.start()['status'], 'done')
                self.assertEqual(fetch.call_count, 1)
            self.assertEqual(network.call_count, 1)

    def test_fast_429_daily_attempt_is_still_marked_attempted_for_today(self):
        class InlineThread:
            def __init__(self, *, target, args, **kwargs): self.target, self.args = target, args
            def start(self): self.target(*self.args)
        now = datetime.now()
        self.service.save_config({'daily_enabled': True, 'daily_time': '00:00'})
        with patch('urllib.request.urlopen', side_effect=limited()) as network, \
                patch('paper_reader.desktop_api.threading.Thread', InlineThread):
            self.app.state.scheduler_tick(now)
            self.assertEqual(self.db.job_get('daily-sync').get('date'), now.strftime('%Y-%m-%d'))
            self.app.state.scheduler_tick(now)
            self.assertEqual(network.call_count, 1)

    def test_source_scope_and_internal_gate_are_not_duplicate_visible_tasks(self):
        with patch('urllib.request.urlopen', side_effect=limited()) as network:
            self.start()
            overview = self.client.get('/desktop/overview').json()
            self.assertEqual(overview['sync_cooldown']['source'], 'arxiv')
            self.assertFalse(any(t['key'].startswith('source-cooldown:') for t in self.client.get('/desktop/tasks').json()['items']))
            self.service.save_config({'sync_source': 'openalex'})
            self.assertIsNone(self.client.get('/desktop/overview').json()['sync_cooldown'])
            with patch('paper_reader.desktop_api.sync_openalex', return_value={'fetched': 0}) as alternative:
                self.assertEqual(self.start('openalex')['status'], 'done')
                self.assertEqual(alternative.call_count, 1)
            self.assertEqual(self.start()['code'], 'source_rate_limited')
            self.assertEqual(network.call_count, 1)

    def test_corrupt_persisted_deadline_repairs_once_without_sending(self):
        self.db.job_put('source-cooldown:arxiv', {'source': 'arxiv', 'retry_at': 'invalid'})
        with patch('urllib.request.urlopen', side_effect=AssertionError('must not send')) as network:
            first = self.start()
            second = self.start()
            self.assertEqual(first['retry_at'], second['retry_at'])
            self.assertEqual(first['retry_after_source'], 'fallback')
            self.assertEqual(network.call_count, 0)

    def test_lower_level_sync_cannot_bypass_recorded_desktop_cooldown(self):
        with patch('urllib.request.urlopen', side_effect=limited()) as network:
            self.start()
            with self.assertRaises(SourceError): sync_arxiv(self.settings, self.db)
            self.assertEqual(network.call_count, 1)


if __name__ == '__main__': unittest.main()
