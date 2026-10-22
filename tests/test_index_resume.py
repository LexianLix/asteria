"""Index lifecycle through the actual API; gates replace timing guesses."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from paper_reader.desktop_api import install_desktop
from paper_reader.settings import Settings
from paper_reader.storage import Database
from test_desktop import sample_pdf


class IndexResumeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.settings = replace(Settings.from_env(), root_dir=root, data_dir=root / 'data',
            db_path=root / 'data/database.sqlite', papers_dir=root / 'data/papers', topics_path=root / 'topics.json')
        self.settings.ensure_dirs(); self.db = Database(self.settings.db_path)
        self.ids, self.paths = [], {}
        for number in range(3):
            path = self.settings.papers_dir / f'index-qa-{number}.pdf'; path.write_bytes(sample_pdf())
            pid, _ = self.db.upsert_paper({'source': 'index-qa', 'source_id': str(number), 'title': f'Index QA {number}',
                'local_pdf_path': str(path.relative_to(root))})
            self.ids.append(pid); self.paths[pid] = path
        self.release = threading.Event()
        self.connect()

    def connect(self):
        self.app = FastAPI(); self.service = install_desktop(self.app, self.settings, self.db)
        self.service.save_config({'auto_backup': False, 'daily_enabled': False})
        self.client = TestClient(self.app)
        self.headers = {'X-Desktop-Token': self.client.get('/desktop/bootstrap').json()['token']}

    def tearDown(self):
        self.release.set(); self.client.post('/desktop/index/cancel', headers=self.headers)
        self.wait(); self.service.shutdown(); self.client.close(); self.db.close(); self.temp.cleanup()

    def wait(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.db.job_get('index-fulltext')
            if job.get('status') not in ('queued', 'running', 'cancelling'): return job
            time.sleep(.01)
        self.fail('index worker did not finish')

    def paused(self):
        entered = threading.Event(); original = self.service.document
        def gated(pid, **kwargs):
            if pid == self.ids[1]:
                entered.set()
                if not self.release.wait(4): raise RuntimeError('QA gate timed out')
            return original(pid, **kwargs)
        with patch.object(self.service, 'document', side_effect=gated):
            self.assertEqual(self.client.post('/desktop/index', headers=self.headers).status_code, 200)
            self.assertTrue(entered.wait(3))
            self.client.post('/desktop/index/cancel', headers=self.headers)
            self.release.set(); job = self.wait()
        self.assertEqual(job['status'], 'paused'); self.assertEqual(job['done'], 2)
        return job

    def test_resume_skips_completed_and_preserves_progress(self):
        self.paused()
        with patch.object(self.service, 'document', wraps=self.service.document) as documents:
            self.client.post('/desktop/index', headers=self.headers); job = self.wait()
        self.assertEqual(job['status'], 'done'); self.assertEqual(job['done'], 3)
        self.assertEqual([call.args[0] for call in documents.call_args_list], [self.ids[2]], 'Completed papers were reprocessed')

    def test_restart_retains_checkpoint_and_continues_unfinished(self):
        before = self.paused()
        self.service.shutdown(); self.client.close(); self.db.close(); self.db = Database(self.settings.db_path); self.connect()
        self.assertEqual(self.db.job_get('index-fulltext')['done'], before['done'])
        with patch.object(self.service, 'document', wraps=self.service.document) as documents:
            self.client.post('/desktop/index', headers=self.headers); job = self.wait()
        self.assertEqual(job['status'], 'done'); self.assertEqual(job['done'], 3)
        self.assertEqual([call.args[0] for call in documents.call_args_list], [self.ids[2]])

    def test_changed_completed_pdf_is_reprocessed_not_blindly_skipped(self):
        self.paused()
        # An actual valid changed PDF: blank metadata bytes leave PDF parsing valid.
        with self.paths[self.ids[0]].open('ab') as stream: stream.write(b'\n% changed owned QA file\n')
        with patch.object(self.service, 'document', wraps=self.service.document) as documents:
            self.client.post('/desktop/index', headers=self.headers); job = self.wait()
        self.assertEqual(job['status'], 'done'); self.assertEqual(job['done'], 3)
        self.assertEqual([call.args[0] for call in documents.call_args_list], [self.ids[0], self.ids[2]])

    def test_cancel_acknowledges_waiting_for_current_document(self):
        entered = threading.Event(); original = self.service.document
        def gated(pid):
            if pid == self.ids[1]:
                entered.set()
                self.assertTrue(self.release.wait(4))
            return original(pid)
        with patch.object(self.service, 'document', side_effect=gated):
            self.client.post('/desktop/index', headers=self.headers); self.assertTrue(entered.wait(3))
            paused = self.client.post('/desktop/index/cancel', headers=self.headers).json()
            self.assertEqual(paused['status'], 'cancelling'); self.assertEqual(paused['done'], 1)
            self.assertEqual(set(paused['completed']), {str(self.ids[0])})
            public = next(t for t in self.client.get('/desktop/tasks').json()['items'] if t['key'] == 'index-fulltext')
            self.assertNotIn('completed', public); self.assertEqual(public['done'], 1)
            self.release.set(); self.assertEqual(self.wait()['status'], 'paused')

    def test_partial_failure_retries_only_failed_paper(self):
        original = self.service.document
        def failed(pid):
            if pid == self.ids[1]: raise ValueError('Controlled parser failure')
            return original(pid)
        with patch.object(self.service, 'document', side_effect=failed):
            self.client.post('/desktop/index', headers=self.headers); job = self.wait()
        self.assertEqual(job['status'], 'partial'); self.assertEqual(job['done'], 2)
        self.assertEqual(job['failed_ids'], [self.ids[1]])
        self.assertNotIn(str(self.ids[1]), job['completed'])
        with patch.object(self.service, 'document', wraps=original) as documents:
            self.client.post('/desktop/index', headers=self.headers); job = self.wait()
        self.assertEqual(job['status'], 'done'); self.assertEqual(job['failed_ids'], [])
        self.assertEqual([call.args[0] for call in documents.call_args_list], [self.ids[1]])

    def test_corrupt_completed_cache_is_rebuilt_not_skipped(self):
        self.paused(); self.db.reader_put(self.ids[0], 'search', {'index_version': -1, 'text': 'stale'})
        with patch.object(self.service, 'document', wraps=self.service.document) as documents:
            self.client.post('/desktop/index', headers=self.headers); job = self.wait()
        self.assertEqual(job['status'], 'done')
        self.assertEqual([call.args[0] for call in documents.call_args_list], [self.ids[0], self.ids[2]])
        self.assertIn('Numbers', self.db.reader_get(self.ids[0], 'search')['text'])

    def test_interrupted_recovery_retains_exact_checkpoint(self):
        job = self.paused(); job['status'] = 'running'; self.db.job_put('index-fulltext', job)
        self.service.shutdown(); self.client.close(); self.db.close(); self.db = Database(self.settings.db_path); self.connect()
        recovered = self.db.job_get('index-fulltext')
        self.assertEqual(recovered['status'], 'paused'); self.assertEqual(recovered['done'], 2)
        self.assertEqual(recovered['completed'], job['completed'])
        self.assertIn('索引', recovered['error'])

    def test_current_marker_but_changed_cache_is_reprocessed(self):
        self.paused()
        self.db.reader_put(self.ids[0], 'search', {'index_version': 1, 'text': 'Wrong cached text despite current format'})
        with patch.object(self.service, 'document', wraps=self.service.document) as documents:
            self.client.post('/desktop/index', headers=self.headers); job = self.wait()
        self.assertEqual(job['status'], 'done')
        self.assertEqual([call.args[0] for call in documents.call_args_list], [self.ids[0], self.ids[2]])
        self.assertIn('Numbers', self.db.reader_get(self.ids[0], 'search')['text'])

    def test_single_cache_refresh_failure_rolls_back_written_payload(self):
        self.service.document(self.ids[0]); before = self.db.reader_get(self.ids[0], 'search')
        def write_then_fail(pid):
            self.db._connection.execute("UPDATE reader_data SET payload=? WHERE paper_id=? AND kind='search'", ('{"text":"partial write"}', pid))
            raise RuntimeError('Controlled refresh failure after a real SQL write')
        with patch.object(self.db, '_index_paper', side_effect=write_then_fail), self.assertRaises(RuntimeError):
            self.db.refresh_search(self.ids[0])
        self.assertEqual(self.db.reader_get(self.ids[0], 'search'), before)
        self.assertFalse(self.db._connection.in_transaction)

    def test_checked_legacy_body_is_not_reparsed_on_index_resume(self):
        from paper_reader.reader import PARSER_VERSION
        legacy = self.service.document(self.ids[0])
        legacy['parser_version'] = 2
        legacy.pop('extraction_checked_version', None)
        legacy['pages'][0]['blocks'][0]['id'] = 'legacy-source-anchor'
        legacy['pages'][0]['blocks'][0]['en'] = 'Old parser body kept for user review'
        self.db.reader_put(self.ids[0], 'document', legacy)
        self.client.post('/desktop/index', headers=self.headers)
        self.assertEqual(self.wait()['status'], 'done')
        checked = self.db.reader_get(self.ids[0], 'document')
        self.assertEqual(checked['parser_version'], 2)
        self.assertEqual(checked['extraction_checked_version'], PARSER_VERSION)
        with patch.object(self.service, 'document', wraps=self.service.document) as documents:
            self.client.post('/desktop/index', headers=self.headers)
            self.assertEqual(self.wait()['status'], 'done')
        documents.assert_not_called()
