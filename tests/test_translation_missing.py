"""Production worker fills missing entries without resending cached body text."""
import json
import tempfile
import time
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from paper_reader.desktop_api import install_desktop
from paper_reader.model_client import ModelClient, ModelResult
from paper_reader.reader import page_complete
from paper_reader.settings import Settings
from paper_reader.storage import Database
from test_desktop import sample_pdf


class MissingTranslationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        cfg = replace(Settings.from_env(), root_dir=root, data_dir=root / 'data',
            db_path=root / 'data/db.sqlite', papers_dir=root / 'data/papers', topics_path=root / 'topics.json')
        cfg.ensure_dirs(); self.db = Database(cfg.db_path)
        self.app = FastAPI(); self.reader = install_desktop(self.app, cfg, self.db)
        self.client = TestClient(self.app)
        self.headers = {'X-Desktop-Token': self.client.get('/desktop/bootstrap').json()['token']}
        current = self.reader.config()
        configured = self.client.put('/desktop/config', headers=self.headers, json={
            'provider': current['provider'], 'base_url': current['base_url'],
            'model': current['model'], 'retry_count': 0})
        self.assertEqual(configured.status_code, 200)
        self.pid = self.client.post('/desktop/import', headers=self.headers,
            files={'file': ('missing.pdf', sample_pdf(), 'application/pdf')}).json()['id']
        self.doc = self.reader.document(self.pid)
        self.page = self.doc['pages'][0]
        self.page['translated'] = {b['id']: '已有真实缓存不得重发：' + b['en'] for b in self.page['blocks']}
        self.page.update(model=self.reader.config()['model'], provider=self.reader.config()['provider'])
        self.before = deepcopy(self.page['translated'])

    def tearDown(self):
        self.reader.shutdown(); self.client.close(); self.db.close(); self.temp.cleanup()

    def run_page(self, *, force=False):
        self.db.reader_put(self.pid, 'document', self.doc)
        response = self.client.post(f'/desktop/papers/{self.pid}/translation', headers=self.headers,
            json={'scope': 'page', 'page_number': 0, 'force': force})
        self.assertEqual(response.status_code, 202)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.db.job_get(f'translate:{self.pid}')
            if job['status'] not in ('queued', 'running', 'cancelling'):
                return job, self.db.reader_get(self.pid, 'document')['pages'][0]
            time.sleep(.01)
        self.fail('Worker did not stop')

    def table(self, rows):
        self.page['scientific_regions'] = [{'id': 'new-table', 'kind': 'table', 'rows': rows,
            'cell_translation_allowed': True, 'box': [0, 0, 1, 1], 'block_ids': []}]

    def test_only_new_cells_reach_model_and_cached_body_survives(self):
        self.table([['test', '62.1']])
        captured = []
        def answer(prompt, **kwargs):
            source = json.loads(prompt.split('\n', 1)[1]); captured.extend(source)
            return ModelResult(json.dumps([{'id': b['id'], 'zh': '测试'} for b in source]), {}, .01)
        with patch.object(ModelClient, 'generate', side_effect=answer):
            job, page = self.run_page()
        self.assertEqual([b['id'] for b in captured], ['new-table-r0c0'])
        self.assertEqual(job['status'], 'done')
        self.assertTrue(page_complete(page))
        self.assertEqual({k: page['translated'][k] for k in self.before}, self.before)
        self.assertEqual(page['translated']['new-table-r0c1'], '62.1')

    def test_numeric_only_missing_entries_make_no_model_request(self):
        self.table([['62.1', '-']])
        with patch.object(ModelClient, 'generate') as generate:
            job, page = self.run_page()
        generate.assert_not_called()
        self.assertEqual(job['status'], 'done')
        self.assertTrue(page_complete(page))
        self.assertEqual({k: page['translated'][k] for k in self.before}, self.before)

    def test_force_still_retranslates_cached_body(self):
        captured = []
        def answer(prompt, **kwargs):
            source = json.loads(prompt.split('\n', 1)[1]); captured.extend(source)
            return ModelResult(json.dumps([{'id': b['id'], 'zh': '明确重译'} for b in source]), {}, .01)
        with patch.object(ModelClient, 'generate', side_effect=answer):
            job, page = self.run_page(force=True)
        self.assertEqual({b['id'] for b in captured}, set(self.before))
        self.assertEqual(job['status'], 'done')
        self.assertTrue(all(v == '明确重译' for v in page['translated'].values()))

    def test_bad_response_does_not_overwrite_existing_cache(self):
        self.table([['test', '62.1']])
        with patch.object(ModelClient, 'generate', return_value=ModelResult('[]', {}, .01)):
            job, page = self.run_page()
        self.assertEqual(job['status'], 'partial')
        self.assertEqual(page['translated'], self.before)

    def test_blank_cached_entry_is_missing_not_complete(self):
        key = next(iter(self.before)); self.page['translated'][key] = '  '
        self.assertFalse(page_complete(self.page))
        captured = []
        def answer(prompt, **kwargs):
            source = json.loads(prompt.split('\n', 1)[1]); captured.extend(source)
            return ModelResult(json.dumps([{'id': b['id'], 'zh': '补齐空值'} for b in source]), {}, .01)
        with patch.object(ModelClient, 'generate', side_effect=answer):
            job, page = self.run_page()
        self.assertEqual([b['id'] for b in captured], [key])
        self.assertEqual(job['status'], 'done'); self.assertTrue(page_complete(page))


if __name__ == '__main__':
    unittest.main()
