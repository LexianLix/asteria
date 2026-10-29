"""Automatic collection must distinguish an attempt from its real outcome."""
from datetime import datetime
import threading
import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

import test_source_cooldowns as fixtures


class InlineThread:
    def __init__(self, *, target, args, **kwargs): self.target, self.args = target, args
    def start(self): self.target(*self.args)


class DailyCollectionTest(unittest.TestCase):
    setUp = fixtures.SourceCooldownAPITest.setUp
    tearDown = fixtures.SourceCooldownAPITest.tearDown
    open = fixtures.SourceCooldownAPITest.open
    close = fixtures.SourceCooldownAPITest.close

    def test_fast_completion_is_persisted_and_is_not_overwritten_as_attempted(self):
        self.service.save_config({'daily_enabled': True, 'daily_time': '00:00'})
        now = datetime(2026, 10, 29, 12)
        with patch('paper_reader.desktop_api.sync_arxiv', return_value={'fetched': 0}) as fetch, \
             patch('paper_reader.desktop_api.threading.Thread', InlineThread):
            self.app.state.scheduler_tick(now)
            self.app.state.scheduler_tick(now)
            self.assertEqual(fetch.call_count, 1)
        daily = self.db.job_get('daily-sync')
        self.assertEqual(daily['status'], 'done')
        self.assertEqual(daily['date'], '2026-10-29')
        self.assertIn('started_at', daily)
        self.assertFalse(any(item['key'] == 'daily-sync' for item in self.client.get('/desktop/tasks').json()['items']))

    def test_failure_survives_restart_and_does_not_retry_automatically_today(self):
        self.service.save_config({'daily_enabled': True, 'daily_time': '00:00'})
        now = datetime(2026, 10, 29, 12)
        with patch('paper_reader.desktop_api.sync_arxiv', side_effect=RuntimeError('Controlled offline failure')) as fetch, \
             patch('paper_reader.desktop_api.threading.Thread', InlineThread):
            self.app.state.scheduler_tick(now)
            self.assertEqual(self.db.job_get('daily-sync')['status'], 'failed')
            self.close(); self.open()
            self.app.state.scheduler_tick(now)
            self.assertEqual(fetch.call_count, 1)
        overview = self.client.get('/desktop/overview').json()
        self.assertEqual(overview['daily_sync']['status'], 'failed')
        self.assertTrue(overview['daily_enabled'])
        self.assertEqual(self.db.list_annotations(self.pid), [self.note])

    def test_before_due_time_and_disabled_collection_do_not_send(self):
        with patch('paper_reader.desktop_api.sync_arxiv', side_effect=AssertionError('Must not send')):
            self.app.state.scheduler_tick(datetime(2026, 10, 29, 12), include_backup=False)
            self.service.save_config({'daily_enabled': True, 'daily_time': '09:00'})
            self.app.state.scheduler_tick(datetime(2026, 10, 29, 8), include_backup=False)
        self.assertEqual(self.db.job_get('daily-sync')['status'], 'idle')

    def test_startup_catches_up_due_collection_without_waiting_for_periodic_tick(self):
        self.service.save_config({'daily_enabled': True, 'daily_time': '00:00'})
        called = threading.Event()
        def collect(*args, **kwargs): called.set(); return {'fetched': 0}
        with patch('paper_reader.desktop_api.sync_arxiv', side_effect=collect):
            with TestClient(self.app):
                self.assertTrue(called.wait(3), 'Startup collection waited for the 30-second scheduler tick')
                deadline = time.monotonic() + 3
                while self.db.job_get('daily-sync').get('status') == 'running' and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(self.db.job_get('daily-sync')['status'], 'done')


if __name__ == '__main__': unittest.main()
