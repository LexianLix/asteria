"""Upgrade acceptance in isolated libraries. Never restores the user's database."""
import hashlib
import io
import json
import sqlite3
import threading
import tempfile
import time
import unittest
import urllib.error
import zipfile
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from paper_reader.backup import BackupManager
from paper_reader.desktop_api import install_desktop
from paper_reader.model_client import ModelClient, ModelError, ModelResult, retry_delay, token_count
from paper_reader.reader import quality_warnings
from paper_reader.secret_store import protect, reveal
from paper_reader.settings import Settings
from paper_reader.storage import Database
from test_desktop import sample_pdf


class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = replace(Settings.from_env(), root_dir=root, data_dir=root / 'data', db_path=root / 'data/db.sqlite',
                                papers_dir=root / 'data/papers', topics_path=root / 'topics.json')
        self.settings.ensure_dirs()
        self.db = Database(self.settings.db_path)
        self.app = FastAPI()
        self.service = install_desktop(self.app, self.settings, self.db)
        self.client = TestClient(self.app)
        self.headers = {'X-Desktop-Token': self.client.get('/desktop/bootstrap').json()['token']}
        self.paper = self.client.post('/desktop/import', headers=self.headers, files={'file': ('test.pdf', sample_pdf(), 'application/pdf')}).json()
        self.pid = self.paper['id']
        self.backups = BackupManager(self.settings, self.db)

    def tearDown(self):
        self.service.shutdown()
        self.client.close()
        self.db.close()
        self.temp.cleanup()

    def wait(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.service._lock:
                if self.pid not in self.service._active:
                    return self.db.job_get(f'translate:{self.pid}')
            time.sleep(.01)
        self.fail('worker did not finish')

    def test_current_page_retries_and_failed_retranslation_preserves_manual_edits(self):
        doc = self.service.document(self.pid)
        block = doc['pages'][1]['blocks'][0]
        result = lambda page, cfg: {b['id']: 'new ' + b['en'] for b in page['blocks']}
        self.service.save_config({'retry_delay': 0})
        with patch.object(self.service, 'translate_page', side_effect=[ModelError('temporary', 'rate_limit', retryable=True), result(doc['pages'][1], {})]) as model:
            self.service.start(self.pid, 'page', 1)
            job = self.wait()
            self.assertEqual(job['status'], 'done')
            self.assertEqual(model.call_count, 2)
            self.assertEqual(job['attempts']['1'], 2)
        doc = self.service.document(self.pid)
        self.assertFalse(doc['pages'][0]['translated'])
        response = self.client.patch(f'/desktop/papers/{self.pid}/editor', headers=self.headers, json={'block_id': block['id'], 'zh': '用户修订译文'})
        self.assertEqual(response.status_code, 200)
        with patch.object(self.service, 'translate_page', side_effect=ModelError('quota', 'quota', fatal=True)) as model:
            self.service.start(self.pid, 'page', 1, True)
            self.assertEqual(self.wait()['status'], 'partial')
            self.assertEqual(model.call_count, 1)
        with patch.object(self.service, 'translate_page', side_effect=result) as model:
            self.service.start(self.pid, 'failed')
            self.assertEqual(self.wait()['status'], 'done')
            self.assertEqual(model.call_count, 1)
        self.assertEqual(self.db.reader_get(self.pid, 'editor')['edits'][block['id']]['zh'], '用户修订译文')

    def test_snippet_cache_depends_on_glossary_and_manual_conflict_is_visible(self):
        with patch.object(ModelClient, 'generate', return_value=ModelResult('120 个样本 [1]', {}, .1)) as model:
            self.assertFalse(self.service.translate_snippet(self.pid, '120 samples [1]')['cached'])
            self.assertTrue(self.service.translate_snippet(self.pid, '120 samples [1]')['cached'])
            self.client.patch(f'/desktop/papers/{self.pid}/editor', headers=self.headers, json={'glossary': [{'en': 'samples', 'zh': '样本'}]})
            self.assertFalse(self.service.translate_snippet(self.pid, '120 samples [1]')['cached'])
            self.assertEqual(model.call_count, 2)
        doc = self.service.document(self.pid)
        page, block = doc['pages'][0], doc['pages'][0]['blocks'][0]
        self.client.patch(f'/desktop/papers/{self.pid}/editor', headers=self.headers, json={'block_id': block['id'], 'zh': '修订'})
        page['translated'][block['id']] = 'changed machine translation'
        self.db.reader_put(self.pid, 'document', doc)
        self.assertTrue(self.client.get(f'/desktop/papers/{self.pid}/editor').json()['edits'][block['id']]['conflict'])
        self.assertTrue(quality_warnings('120 samples [1]', '12 个样本 [2]'))
        self.assertFalse(quality_warnings('120 samples [1]', '120 个样本 [1]'))

    def test_windows_dpapi_and_address_change_never_reuses_secret(self):
        encrypted = protect('unit-test-secret')
        self.assertNotIn('unit-test-secret', encrypted)
        self.assertEqual(reveal(encrypted), 'unit-test-secret')
        self.service.save_config({'provider': 'openai', 'base_url': 'https://example.org/v1', 'api_key': 'unit-test-secret'})
        raw = (self.settings.data_dir / 'desktop-settings.json').read_text()
        self.assertNotIn('unit-test-secret', raw)
        self.assertNotIn('"api_key":', raw)
        self.assertTrue(self.service.config(public=True)['has_api_key'])
        self.service.save_config({'base_url': 'https://different.example/v1'})
        self.assertFalse(self.service.config(public=True)['has_api_key'])
        self.service.save_config({'api_key': 'must-not-keep', 'clear_api_key': True})
        self.assertFalse(self.service.config(public=True)['has_api_key'])

    def test_backup_actual_restore_preserves_pdf_notes_translation_and_drafts(self):
        doc = self.service.document(self.pid)
        doc['pages'][0]['translated'] = {b['id']: '原始译文' for b in doc['pages'][0]['blocks']}
        self.db.reader_put(self.pid, 'document', doc)
        self.db.reader_put(self.pid, 'draft', {'text': '未提交草稿'})
        self.client.post(f'/desktop/papers/{self.pid}/annotations', headers=self.headers,
                         json={'client_id': 'backup-test-note', 'page_number': 0, 'annotation_type': 'note', 'text': '需要恢复的笔记'})
        original = self.settings.root_dir / self.paper['local_pdf_path']
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        backup = self.backups.create()
        with zipfile.ZipFile(self.backups.resolve(backup['name'])) as archive:
            self.assertNotIn('api_key_protected', archive.read('settings.json').decode())
        self.db.reader_put(self.pid, 'document', {'changed': True})
        self.db.reader_put(self.pid, 'draft', {'text': '后来草稿'})
        self.db.delete_annotation(self.pid, self.db.list_annotations(self.pid)[0]['id'])
        restored = self.backups.restore(backup['name'])
        self.assertTrue(restored['restored'])
        self.assertTrue(self.backups.verify(restored['recovery_backup'])['valid'])
        self.assertEqual(self.db.reader_get(self.pid, 'document'), doc)
        self.assertEqual(self.db.reader_get(self.pid, 'draft')['text'], '未提交草稿')
        self.assertEqual(self.db.list_annotations(self.pid)[0]['text'], '需要恢复的笔记')
        self.assertTrue(original.exists())
        target = self.settings.root_dir / self.db.get_paper(self.pid)['local_pdf_path']
        self.assertNotEqual(original, target)
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), digest)

    def mutate_backup(self, transform):
        backup = self.backups.create()
        path = self.backups.resolve(backup['name'])
        with zipfile.ZipFile(path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        transform(entries)
        with zipfile.ZipFile(path, 'w') as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        return backup['name']

    def test_bad_backup_is_rejected_before_current_library_changes(self):
        for mutation in (
            lambda e: e.update({'../escape.txt': b'bad'}),
            lambda e: e.update({'database.sqlite': b'broken'}),
            lambda e: e.update({'manifest.json': b'[]'}),
        ):
            with self.subTest(mutation=mutation):
                name = self.mutate_backup(mutation)
                before = self.db.get_paper(self.pid)
                with self.assertRaises(ValueError):
                    self.backups.restore(name)
                self.assertEqual(self.db.get_paper(self.pid), before)
                self.assertFalse((self.settings.data_dir / 'escape.txt').exists())

    def test_backup_with_rehashed_secret_is_still_rejected(self):
        def mutate(entries):
            entries['settings.json'] = b'{"api_key":"malicious"}'
            manifest = json.loads(entries['manifest.json'])
            manifest['entries']['settings.json'] = {'size': len(entries['settings.json']), 'sha256': hashlib.sha256(entries['settings.json']).hexdigest()}
            entries['manifest.json'] = json.dumps(manifest).encode()
        name = self.mutate_backup(mutate)
        with self.assertRaisesRegex(ValueError, '密钥'):
            self.backups.restore(name)

    def test_import_backup_is_verified_and_does_not_restore_automatically(self):
        snapshot = self.backups.create()
        content = self.backups.resolve(snapshot['name']).read_bytes()
        self.db.reader_put(self.pid, 'draft', {'text': '导入不能覆盖'})
        response = self.client.post('/desktop/backups/import', headers=self.headers, files={'file': ('export.zip', content, 'application/zip')})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['valid'])
        self.assertNotEqual(response.json()['name'], snapshot['name'])
        self.assertEqual(self.db.reader_get(self.pid, 'draft')['text'], '导入不能覆盖')
        bad = self.client.post('/desktop/backups/import', headers=self.headers, files={'file': ('bad.zip', b'not zip')})
        self.assertEqual(bad.status_code, 422)
        self.assertFalse(list(self.backups.directory.glob('*.part')))

    def test_task_center_includes_translation_progress_and_backup(self):
        self.db.job_put(f'translate:{self.pid}', {'status': 'paused', 'done': 1, 'total': 2, 'failed_pages': [1]})
        response = self.client.post('/desktop/backups', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        tasks = {row['key']: row for row in self.client.get('/desktop/tasks').json()['items']}
        self.assertEqual(tasks[f'translate:{self.pid}']['paper_id'], self.pid)
        self.assertEqual(tasks[f'translate:{self.pid}']['done'], 1)
        self.assertEqual(tasks['manual-backup']['status'], 'done')

    def test_library_metadata_and_full_text_search_include_second_page_and_edits(self):
        url = f'/desktop/papers/{self.pid}/state'
        meta = self.client.patch(url, headers=self.headers, json={'folder': '研究资料', 'tags': ['自定义标签', '重点'], 'status': 'done'})
        self.assertEqual(meta.status_code, 200, meta.text)
        self.assertEqual(self.client.get('/desktop/papers?folder=研究资料&status=done&tag=重点').json()['total'], 1)
        self.assertEqual(self.client.get('/desktop/papers?status=unread').json()['total'], 0)
        self.assertEqual(self.client.get('/desktop/papers?folder=不存在').json()['total'], 0)
        doc = self.service.document(self.pid)
        block = doc['pages'][1]['blocks'][-1]
        self.client.patch(f'/desktop/papers/{self.pid}/editor', headers=self.headers,
                         json={'block_id': block['id'], 'zh': '跨页全文检索的独特中文修订词'})
        result = self.client.get('/desktop/papers?q=独特中文修订词').json()
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['page'], 1)
        self.assertEqual(result['items'][0]['search_hit']['page'], 1)
        self.assertEqual(result['items'][0]['search_hit']['language'], 'zh')
        self.assertEqual(self.client.get('/desktop/papers?q=Numbers').json()['total'], 1)
        self.assertEqual(self.client.get('/desktop/papers?q=%25').json()['total'], 0)
        overview = self.client.get('/desktop/overview').json()
        self.assertEqual(overview['folders'], ['研究资料'])
        self.assertEqual(overview['statuses']['done'], 1)
        self.assertIn('自定义标签', overview['tags'])

    def test_index_job_extracts_unopened_local_pdfs_without_model_calls(self):
        self.assertFalse(self.db.reader_get(self.pid, 'document'))
        with patch.object(ModelClient, 'generate', side_effect=AssertionError('index must not call AI')):
            self.client.post('/desktop/index', headers=self.headers)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = self.db.job_get('index-fulltext')
                if job['status'] == 'done':
                    break
                time.sleep(.01)
            self.assertEqual(job['status'], 'done', job)
        self.assertEqual(self.client.get('/desktop/papers?q=citations').json()['total'], 1)

    def test_daily_backup_runs_once_and_disabled_or_maintenance_does_not_write(self):
        now = datetime(2026, 10, 3, 15, 30)
        self.app.state.scheduler_tick(now)
        items = self.backups.list()
        self.assertEqual(len(items), 1)
        self.assertIn('-auto.zip', items[0]['name'])
        self.assertTrue(self.backups.verify(items[0]['name'])['valid'])
        self.app.state.scheduler_tick(now)
        self.assertEqual(len(self.backups.list()), 1)
        self.service.save_config({'auto_backup': False})
        self.app.state.scheduler_tick(datetime(2026, 10, 4, 15, 30))
        self.assertEqual(len(self.backups.list()), 1)
        self.service.save_config({'auto_backup': True})
        self.app.state.maintenance = True
        self.app.state.scheduler_tick(datetime(2026, 10, 4, 15, 30))
        self.assertEqual(len(self.backups.list()), 1)
        self.app.state.maintenance = False

    def test_zip_symlink_duplicate_and_size_limit_are_rejected(self):
        name = self.backups.create()['name']
        with patch('paper_reader.backup.MAX_TOTAL', 10):
            with self.assertRaisesRegex(ValueError, '大小'):
                self.backups.verify(name)
        path = self.backups.resolve(name)
        info = zipfile.ZipInfo('pdfs/999.pdf')
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr(info, 'target.pdf')
        with self.assertRaisesRegex(ValueError, '符号链接'):
            self.backups.verify(name)
        name = self.backups.create()['name']
        with zipfile.ZipFile(self.backups.resolve(name), 'a') as archive:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                archive.writestr('manifest.json', '{}')
        with self.assertRaisesRegex(ValueError, '重复'):
            self.backups.verify(name)

    def test_restore_failure_rolls_back_database_and_settings(self):
        backup = self.backups.create()
        self.db.reader_put(self.pid, 'draft', {'text': '当前库不能丢'})
        self.service.save_config({'model': 'before-model'})
        before_config = (self.settings.data_dir / 'desktop-settings.json').read_bytes()
        with patch.object(self.db, 'jobs_recover', side_effect=RuntimeError('simulated apply failure')):
            with self.assertRaisesRegex(RuntimeError, 'simulated'):
                self.backups.restore(backup['name'])
        self.assertEqual(self.db.reader_get(self.pid, 'draft')['text'], '当前库不能丢')
        self.assertEqual((self.settings.data_dir / 'desktop-settings.json').read_bytes(), before_config)

    def test_model_http_failures_are_typed_and_do_not_expose_response(self):
        cfg = {'provider': 'openai', 'base_url': 'https://example.org/v1', 'model': 'test', 'api_key': 'secret'}
        for status, body, code, retryable in ((401, b'secret', 'auth', False), (429, b'{"error":{"code":"insufficient_quota"}}', 'quota', False),
                                            (429, b'{}', 'rate_limit', True), (503, b'secret', 'server', True)):
            with self.subTest(status=status, code=code):
                error = urllib.error.HTTPError(cfg['base_url'], status, 'error', {}, io.BytesIO(body))
                with patch('paper_reader.model_client.open_model_request', side_effect=error):
                    with self.assertRaises(ModelError) as caught:
                        ModelClient(cfg).models()
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertNotIn('secret', str(caught.exception))

    def test_model_redirect_is_rejected_without_forwarding_authentication(self):
        received = []
        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get('Authorization'))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":[]}')
            def log_message(self, *args):
                pass
        target = ThreadingHTTPServer(('127.0.0.1', 0), Target)
        class Redirect(Target):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{target.server_port}/models')
                self.end_headers()
        origin = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        for server in (target, origin):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = ModelClient({'provider': 'openai', 'base_url': f'http://127.0.0.1:{origin.server_port}', 'model': 'test', 'api_key': 'unit-secret'})
            with self.assertRaisesRegex(ModelError, '重定向'):
                client.models()
            self.assertEqual(received, [])
        finally:
            for server in (target, origin):
                server.shutdown()
                server.server_close()

    def test_usage_missing_is_unknown_and_retry_after_is_respected(self):
        self.assertIsNone(token_count(None))
        self.assertIsNone(token_count('123'))
        self.assertEqual(token_count(123), 123)
        self.assertEqual(retry_delay({'Retry-After': '3'}), 3)
        self.assertIsNone(retry_delay({'Retry-After': 'inf'}))
        error = urllib.error.HTTPError('https://example.org', 429, 'error', {'Retry-After': '3'}, io.BytesIO(b'{"error":{"code":"slow_down"}}'))
        with patch('paper_reader.model_client.open_model_request', side_effect=error):
            with self.assertRaises(ModelError) as caught:
                ModelClient({'provider': 'openai', 'base_url': 'https://example.org', 'model': 'test'}).models()
        self.assertEqual(caught.exception.retry_after, 3)


if __name__ == '__main__':
    unittest.main()
