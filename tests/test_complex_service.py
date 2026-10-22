import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

import psutil
from fastapi import FastAPI
from fastapi.testclient import TestClient
from paper_reader.desktop_api import install_desktop
from paper_reader.backup import BackupManager
from paper_reader.reader import file_hash
from paper_reader.settings import Settings
from paper_reader.storage import Database
from complex_fixture import mock_complex_result
from complex_scripts_fixture import numeric_pdf_and_document
from scientific_fixture import scientific_pdf


class ComplexServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.settings = replace(Settings.from_env(), root_dir=root, data_dir=root / 'data', db_path=root / 'data/db.sqlite', papers_dir=root / 'data/papers', topics_path=root / 'topics.json')
        self.settings.ensure_dirs(); self.db = Database(self.settings.db_path)
        self.app = FastAPI(); self.reader = install_desktop(self.app, self.settings, self.db)
        self.parser = self.app.state.complex_parser; self.client = TestClient(self.app)
        self.headers = {'X-Desktop-Token': self.client.get('/desktop/bootstrap').json()['token']}
        self.pid = self.client.post('/desktop/import', headers=self.headers, files={'file': ('scientific.pdf', scientific_pdf())}).json()['id']
        self.doc = self.reader.document(self.pid)
        self.runtime = patch.object(self.parser, 'runtime', return_value={'available': True})
        self.paths = patch.object(self.parser, '_paths', return_value=(Path(sys.executable), root, 'docling_parse'))
        self.runtime.start(); self.paths.start()

    def tearDown(self):
        self.reader.shutdown(); self.runtime.stop(); self.paths.stop(); self.client.close(); self.db.close(); self.temp.cleanup()

    def wait(self):
        deadline = time.monotonic() + 8
        while self.parser._active and time.monotonic() < deadline: time.sleep(.02)
        self.assertFalse(self.parser._active)
        return self.db.job_get(f'complex:{self.pid}')

    def success(self, snapshot, output, paths, job, cancel):
        self.assertEqual(file_hash(snapshot), self.doc['source_hash'])
        return mock_complex_result(job['start_page'], job['end_page'], job['ocr'])

    def start(self, **values):
        return self.client.post(f'/desktop/papers/{self.pid}/complex', headers=self.headers,
                                json={'start_page': 1, 'end_page': 2, **values})

    def test_success_cache_and_reversible_confirmation_preserve_primary_data(self):
        self.db.reader_put(self.pid, 'editor', {'edits': {'user': {'zh': '保留手动译文'}}})
        self.db.add_annotation(self.pid, {'page_number': 0, 'annotation_type': 'note', 'text': '保留笔记'})
        before = copy.deepcopy(self.db.reader_get(self.pid, 'document'))
        with patch.object(self.parser, '_execute', side_effect=self.success):
            self.assertEqual(self.start().status_code, 202); self.assertEqual(self.wait()['status'], 'done')
        result = self.client.get(f'/desktop/papers/{self.pid}/complex').json()
        preview = result['preview']; link = preview['table_links'][0]
        body = {'source_hash': preview['source_hash'], 'revision': preview['revision'],
                'from_id': link['from_id'], 'to_id': link['to_id']}
        confirmed = self.client.post(f'/desktop/papers/{self.pid}/complex/table-link', headers=self.headers, json=body)
        self.assertEqual(confirmed.status_code, 200); self.assertTrue(confirmed.json()['table_links'][0]['confirmed'])
        self.assertEqual(self.client.post(f'/desktop/papers/{self.pid}/complex/table-link', headers=self.headers, json=body).status_code, 409)
        body.update(revision=confirmed.json()['revision'], confirmed=False)
        self.assertFalse(self.client.post(f'/desktop/papers/{self.pid}/complex/table-link', headers=self.headers, json=body).json()['table_links'][0]['confirmed'])
        self.assertEqual(self.db.reader_get(self.pid, 'document'), before)
        self.assertEqual(self.db.reader_get(self.pid, 'editor')['edits']['user']['zh'], '保留手动译文')
        self.assertEqual(self.db.list_annotations(self.pid)[0]['text'], '保留笔记')
        self.assertIn('complex:' + str(self.pid), [item['key'] for item in self.client.get('/desktop/tasks').json()['items']])

    def test_source_geometry_restores_preview_scripts_without_replacing_reader_data(self):
        content, raw = numeric_pdf_and_document()
        self.pid = self.client.post('/desktop/import', headers=self.headers,
            files={'file': ('numeric-vector-qa.pdf', content)}).json()['id']
        self.doc = self.reader.document(self.pid)
        before = copy.deepcopy(self.db.reader_get(self.pid, 'document'))
        self.db.reader_put(self.pid, 'editor', {'edits': {'user': {'zh': '保留手动译文'}}})
        self.db.add_annotation(self.pid, {'page_number': 0, 'annotation_type': 'note', 'text': '保留笔记'})
        worker_before = copy.deepcopy(raw)

        def execute(snapshot, output, paths, job, cancel):
            self.assertEqual(file_hash(snapshot), self.doc['source_hash'])
            return {'document': raw, 'backend': 'qa-structured-worker', 'ocr': False,
                    'remote_inference': False, 'blocked_network_attempts': 0}

        with patch.object(self.parser, '_execute', side_effect=execute):
            self.assertEqual(self.start(end_page=1).status_code, 202)
            self.assertEqual(self.wait()['status'], 'done')
        preview = self.parser.preview(self.pid)
        self.assertEqual(preview['tables'][0]['rows'], [['256²', '10⁻⁴', 'b₁', '2562']])
        restored = preview['tables'][0]['cells'][0]
        self.assertEqual(restored['original_text'], '2562')
        self.assertEqual(restored['script_restoration']['source_hash'], self.doc['source_hash'])
        self.assertEqual(preview['numeric_script_restoration']['restored_cells'], 3)
        self.assertEqual(raw, worker_before)
        self.assertEqual(self.db.reader_get(self.pid, 'document'), before)
        self.assertEqual(self.db.reader_get(self.pid, 'editor')['edits']['user']['zh'], '保留手动译文')
        self.assertEqual(self.db.list_annotations(self.pid)[0]['text'], '保留笔记')
        manager = BackupManager(self.settings, self.db)
        archive = manager.create()
        self.db.reader_put(self.pid, 'complex-preview', {})
        manager.restore(archive['name'])
        self.assertEqual(self.parser.preview(self.pid), preview)

    def test_failure_and_cancel_keep_previous_preview(self):
        with patch.object(self.parser, '_execute', side_effect=self.success):
            self.start(); self.wait()
        before = self.db.reader_get(self.pid, 'complex-preview')
        with patch.object(self.parser, '_execute', side_effect=ValueError('模拟失败')):
            self.start(); self.assertEqual(self.wait()['status'], 'failed')
        self.assertEqual(self.db.reader_get(self.pid, 'complex-preview'), before)

        entered = threading.Event()
        def delayed(*args):
            entered.set(); args[-1].wait(5); raise InterruptedError('模拟取消')
        with patch.object(self.parser, '_execute', side_effect=delayed):
            self.start(); self.assertTrue(entered.wait(2))
            self.client.post(f'/desktop/papers/{self.pid}/complex/cancel', headers=self.headers)
            self.assertEqual(self.wait()['status'], 'cancelled')
        self.assertEqual(self.db.reader_get(self.pid, 'complex-preview'), before)

    def test_actual_backup_restore_keeps_preview_and_confirmation(self):
        with patch.object(self.parser, '_execute', side_effect=self.success): self.start(); self.wait()
        preview = self.parser.preview(self.pid); link = preview['table_links'][0]
        self.parser.confirm_link(self.pid, preview['source_hash'], link['from_id'], link['to_id'], preview['revision'])
        expected = self.parser.preview(self.pid)
        manager = BackupManager(self.settings, self.db); archive = manager.create()
        self.db.reader_put(self.pid, 'complex-preview', {})
        manager.restore(archive['name'])
        self.assertEqual(self.parser.preview(self.pid), expected)

    def test_restore_admission_and_duplicate_start_while_worker_live(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(*args):
            entered.set(); release.wait(5); return self.success(*args)
        with patch.object(self.parser, '_execute', side_effect=delayed):
            first = self.start(); self.assertTrue(entered.wait(2))
            self.assertEqual(self.start().json()['run_id'], first.json()['run_id'])
            response = self.client.post('/desktop/backups/restore', headers=self.headers, json={'name': 'backup-absent.zip', 'confirmation': 'RESTORE'})
            self.assertEqual(response.status_code, 409)
            self.assertFalse(self.app.state.maintenance)
            release.set(); self.wait()

    def test_changed_source_and_wrong_pages_never_publish(self):
        for change in ('pages', 'source', 'network'):
            def invalid(*args):
                result = self.success(*args)
                if change == 'pages': result['document']['pages'].pop('2')
                elif change == 'network': result['blocked_network_attempts'] = 1
                else:
                    path = self.settings.root_dir / self.db.get_paper(self.pid)['local_pdf_path']
                    path.write_bytes(path.read_bytes() + b'\n%changed')
                return result
            with self.subTest(change=change), patch.object(self.parser, '_execute', side_effect=invalid):
                self.start(); self.assertEqual(self.wait()['status'], 'failed')
                self.assertEqual(self.db.reader_get(self.pid, 'complex-preview'), {})
            if change == 'source':
                path = self.settings.root_dir / self.db.get_paper(self.pid)['local_pdf_path']
                path.write_bytes(scientific_pdf()); self.doc = self.reader.document(self.pid)

    def test_stale_preview_and_confirmation_rejected_after_source_change(self):
        with patch.object(self.parser, '_execute', side_effect=self.success): self.start(); self.wait()
        preview = self.db.reader_get(self.pid, 'complex-preview'); link = preview['table_links'][0]
        path = self.settings.root_dir / self.db.get_paper(self.pid)['local_pdf_path']; path.write_bytes(path.read_bytes() + b'\n%changed')
        self.assertEqual(self.client.get(f'/desktop/papers/{self.pid}/complex').status_code, 409)
        response = self.client.post(f'/desktop/papers/{self.pid}/complex/table-link', headers=self.headers,
            json={'source_hash': preview['source_hash'], 'revision': preview['revision'], 'from_id': link['from_id'], 'to_id': link['to_id']})
        self.assertEqual(response.status_code, 409)

    def test_missing_runtime_and_range_fail_without_starting_a_process(self):
        with patch.object(self.parser, '_execute') as execute:
            for values in ({'start_page': 2, 'end_page': 1}, {'end_page': 3}, {'start_page': 0}):
                self.assertEqual(self.start(**values).status_code, 422)
            with patch.object(self.parser, 'runtime', return_value={'available': False, 'reason': '未配置环境'}):
                self.assertEqual(self.start().status_code, 422)
            execute.assert_not_called()
        self.assertEqual(self.client.post(f'/desktop/papers/{self.pid}/complex', json={'start_page': 1}).status_code, 403)

    def test_actual_owned_process_tree_is_cancelled(self):
        entered = threading.Event(); cancel = threading.Event(); caught = []
        real_popen = subprocess.Popen
        with tempfile.TemporaryDirectory(prefix='paper-complex-process-') as folder:
            output = Path(folder) / 'result.json'
            def launch(command, **kwargs):
                command = [sys.executable, str(Path(__file__).with_name('complex_process_fixture.py')), '--output', str(output)]
                entered.set(); return real_popen(command, **kwargs)
            def run():
                try: self.parser._execute(Path(folder) / 'input.pdf', output, (Path(sys.executable), Path(folder), 'docling_parse'), {'start_page': 1, 'end_page': 1, 'ocr': False}, cancel)
                except Exception as exc: caught.append(exc)
            with patch('paper_reader.complex_service.subprocess.Popen', side_effect=launch):
                thread = threading.Thread(target=run); thread.start(); self.assertTrue(entered.wait(2))
                deadline = time.monotonic() + 5
                while not output.with_suffix('.pid').exists() and time.monotonic() < deadline: time.sleep(.05)
                ids = json.loads(output.with_suffix('.pid').read_text(encoding='utf-8'))
                time.sleep(.2); cancel.set(); thread.join(8)
                self.assertFalse(thread.is_alive()); self.assertIsInstance(caught[0], InterruptedError)
                self.assertFalse(psutil.pid_exists(ids['parent'])); self.assertFalse(psutil.pid_exists(ids['child']))


if __name__ == '__main__': unittest.main()
