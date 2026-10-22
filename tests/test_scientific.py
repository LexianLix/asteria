import copy
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from paper_reader.desktop_api import install_desktop
from paper_reader.model_client import ModelClient, ModelError, ModelResult
from paper_reader.reader import ReaderService, extract_document
from paper_reader.scientific import TABLE_CAPTION, is_numeric_cell, restore_protected, translation_input
from paper_reader.settings import Settings
from paper_reader.storage import Database
from scientific_fixture import qualitative_pdf, scientific_pdf


class ScientificTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.settings = replace(Settings.from_env(), root_dir=root, data_dir=root / 'data', db_path=root / 'data/db.sqlite', papers_dir=root / 'data/papers', topics_path=root / 'topics.json')
        self.settings.ensure_dirs(); self.db = Database(self.settings.db_path)
        self.app = FastAPI(); self.reader = install_desktop(self.app, self.settings, self.db); self.client = TestClient(self.app)
        self.headers = {'X-Desktop-Token': self.client.get('/desktop/bootstrap').json()['token']}
        self.pid = self.client.post('/desktop/import', headers=self.headers, files={'file': ('scientific.pdf', scientific_pdf())}).json()['id']
        self.doc = self.reader.document(self.pid)

    def tearDown(self):
        self.reader.shutdown(); self.client.close(); self.db.close(); self.temp.cleanup()

    def answer(self, prompt, **kwargs):
        source = json.loads(prompt.split('\n', 1)[1])
        return ModelResult(json.dumps([{'id': b['id'], 'zh': '（模拟译文，仅供验收）' + b['en']} for b in source]), {}, .01)

    def test_table_formula_regions_and_original_coordinates(self):
        regions = self.doc['pages'][0]['scientific_regions']
        self.assertTrue(any(r['kind'] == 'formula' for r in regions))
        tables = [r for r in regions if r['kind'] == 'table']
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]['rows'], [['Model', 'Accuracy', 'Latency'], ['Alpha', '95%', '2.5 ms'], ['Beta', '90%', '1.7 ms']])
        for region in regions:
            self.assertTrue(region['block_ids'])
            x0, y0, x1, y1 = region['box']; self.assertTrue(0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1)
        protected = [b for b in self.doc['pages'][0]['blocks'] if b['protected_spans']]
        self.assertTrue(protected)
        for block in protected:
            for span in block['protected_spans']:
                self.assertTrue(0 <= span['start'] < span['end'] <= len(block['en']))

    def test_horizontal_rule_table_and_prose_not_equation(self):
        page = self.doc['pages'][1]
        tables = [r for r in page['scientific_regions'] if r['kind'] == 'table']
        self.assertTrue(tables, page['scientific_diagnostics'])
        self.assertTrue(any('85%' in str(r['rows']) for r in tables))
        self.assertEqual(tables[0]['rows'], [['Metric', 'Baseline', 'Ours'], ['Accuracy', '80%', '85%'], ['Latency', '4.2 ms', '3.1 ms']])
        self.assertFalse(any('Ordinary prose' in r['text'] for r in page['scientific_regions'] if r['kind'] == 'formula'))

    def test_translation_excludes_math_and_numeric_cells(self):
        page = self.doc['pages'][0]
        source, replacements, local = translation_input(page)
        serialized = json.dumps(source)
        self.assertNotIn('95%', serialized); self.assertNotIn('90%', serialized)
        self.assertNotIn('2.5 ms', serialized); self.assertNotIn('1.7 ms', serialized)
        self.assertTrue(replacements); self.assertTrue(local)
        with patch.object(ModelClient, 'generate', side_effect=self.answer):
            translated = ReaderService.translate_page(page, self.reader.config())
        for block in page['blocks']:
            for span in block['protected_spans']:
                self.assertIn(block['en'][span['start']:span['end']], translated[block['id']])
            self.assertNotIn('[[PR_KEEP_', translated[block['id']])
        table = next(r for r in page['scientific_regions'] if r['kind'] == 'table')
        self.assertEqual(translated[table['id'] + '-r1c1'], '95%')
        self.assertIn('2.5', translated[table['id'] + '-r1c2'])

    def test_changed_protection_token_rejects_translation(self):
        page = self.doc['pages'][0]
        def corrupt(prompt, **kwargs):
            answer = self.answer(prompt).text.replace('[[PR_KEEP_', '[[BROKEN_')
            return ModelResult(answer, {}, .01)
        with patch.object(ModelClient, 'generate', side_effect=corrupt):
            with self.assertRaises(ModelError) as error:
                ReaderService.translate_page(page, self.reader.config())
        self.assertTrue(error.exception.retryable)
        self.assertEqual(page['translated'], {})

    def test_additive_cache_upgrade_and_reanalysis_keep_user_data(self):
        old = copy.deepcopy(self.doc); old.pop('scientific_version')
        for page in old['pages']:
            page.pop('scientific_regions'); page.pop('scientific_diagnostics')
            for block in page['blocks']:
                block.pop('protected_spans'); page['translated'][block['id']] = '已有译文：' + block['en']
        self.db.reader_put(self.pid, 'document', old)
        bid = old['pages'][0]['blocks'][0]['id']
        editor = {'glossary': [{'en': 'model', 'zh': '模型'}], 'edits': {bid: {'zh': '手动内容必须保留', 'machine_hash': 'old'}}}
        self.db.reader_put(self.pid, 'editor', editor)
        upgraded = self.reader.document(self.pid)
        for page, prior in zip(upgraded['pages'], old['pages']):
            self.assertEqual(page['translated'], prior['translated'])
            self.assertEqual([b['id'] for b in page['blocks']], [b['id'] for b in prior['blocks']])
        with patch.object(ModelClient, 'generate') as generate:
            response = self.client.post(f'/desktop/papers/{self.pid}/scientific/reanalyse', headers=self.headers)
            self.assertEqual(response.status_code, 200)
            generate.assert_not_called()
        self.assertEqual(self.db.reader_get(self.pid, 'editor'), editor)
        self.assertEqual(response.json()['pages'][0]['translated'], old['pages'][0]['translated'])

    def test_numeric_cells_and_pure_formula_need_no_model(self):
        for text in ['95%', '2.5', '1e-3', '90 ± 2', '1/4']:
            self.assertTrue(is_numeric_cell(text))
        self.assertFalse(is_numeric_cell('95 ms'))
        text = 'E = m c 2 (1)'
        page = {'blocks': [{'id': 'equation', 'en': text, 'protected_spans': [{'start': 0, 'end': len(text), 'kind': 'formula', 'region_id': 'math'}]}], 'scientific_regions': []}
        with patch.object(ModelClient, 'generate') as generate:
            result = ReaderService.translate_page(page, self.reader.config())
            self.assertEqual(result, {'equation': text}); generate.assert_not_called()

    def test_tall_qualitative_table_keeps_multiline_rows(self):
        pid = self.client.post('/desktop/import', headers=self.headers, files={'file': ('qualitative.pdf', qualitative_pdf())}).json()['id']
        page = self.reader.document(pid)['pages'][0]
        table = next(r for r in page['scientific_regions'] if r['kind'] == 'table')
        self.assertEqual(table['method'], 'caption-header-alignment')
        self.assertEqual(len(table['rows']), 6)
        self.assertEqual(table['rows'][0], ['Risk', 'Mechanism', 'Control'])
        self.assertEqual(table['rows'][1], ['Drift', 'Persistent behavior\nchanges over time.', 'Review authority\nand consequences.'])
        self.assertEqual(table['rows'][-1][0], 'Replication')
        self.assertTrue(table['cell_translation_allowed'])
        self.assertNotIn('Body prose', table['text'])
        for text in ['Table1:', 'table 2:', 'Table A.1:', 'Table 3a:']:
            self.assertIsNotNone(TABLE_CAPTION.match(text))
        self.assertIsNone(TABLE_CAPTION.match('Tabletop evaluation is useful.'))

    def test_split_merged_header_is_flagged_and_never_sent(self):
        pid = self.client.post('/desktop/import', headers=self.headers, files={'file': ('merged.pdf', scientific_pdf(unsafe_header=True))}).json()['id']
        page = self.reader.document(pid)['pages'][1]
        table = next(r for r in page['scientific_regions'] if r['kind'] == 'table')
        self.assertFalse(table['cell_translation_allowed'])
        self.assertTrue(table['quality_flags'])
        source, replacements, local = translation_input(page)
        self.assertFalse(any(b['id'].startswith(table['id'] + '-r') for b in source))
        self.assertFalse(any(key.startswith(table['id'] + '-r') for key in local))
        self.assertNotIn('Combined results', json.dumps(source))
        self.assertTrue(replacements or local)

    def test_detector_failure_preserves_text_readability(self):
        with patch('pdfplumber.page.Page.find_tables', side_effect=RuntimeError('isolated detector failure')):
            doc = self.reader.document(self.pid, force_scientific=True)
        self.assertTrue(doc['pages'][0]['blocks'])
        self.assertIn('RuntimeError', doc['pages'][0]['scientific_diagnostics'][0])
        self.assertTrue(any(r['kind'] == 'formula' for r in doc['pages'][0]['scientific_regions']))

    def test_translated_cell_has_its_own_search_location(self):
        page = self.doc['pages'][0]
        table = next(r for r in page['scientific_regions'] if r['kind'] == 'table')
        page['translated'][table['id'] + '-r1c0'] = '单元格稀有测试术语'
        self.db.reader_put(self.pid, 'document', self.doc)
        response = self.client.get('/desktop/papers', params={'q': '稀有测试术语'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['items'][0]['search_hit']['region_id'], table['id'])
        self.assertEqual(response.json()['items'][0]['search_hit']['page'], 0)

    def test_numeric_expressions_and_punctuation_remain_one_token(self):
        values = ['1,234.5', '95 ± 2', '2–5', '1e-3', '-2.5 × 10']
        page = {'blocks': [], 'scientific_regions': [{'id': 'numbers', 'kind': 'table', 'rows': [[value + ' ms' for value in values]]}]}
        source, replacements, local = translation_input(page)
        self.assertEqual(len(source), len(values))
        for entry, value in zip(source, values):
            self.assertEqual(list(replacements[entry['id']].values()), [value])
            self.assertNotIn(value, entry['en'])
        restored = restore_protected({entry['id']: entry['en'].replace(' ms', ' 毫秒') for entry in source}, replacements, local)
        self.assertEqual(list(restored.values()), [value + ' 毫秒' for value in values])

    def test_reanalysis_cannot_race_active_translation_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(prompt, **kwargs):
            entered.set()
            release.wait(5)
            return self.answer(prompt, **kwargs)
        self.reader.save_config({'model': 'isolated-mock-model'})
        with patch.object(ModelClient, 'generate', side_effect=delayed):
            self.reader.start(self.pid, scope='page', page_number=0)
            try:
                self.assertTrue(entered.wait(3))
                response = self.client.post(f'/desktop/papers/{self.pid}/scientific/reanalyse', headers=self.headers)
                self.assertEqual(response.status_code, 409)
                self.assertIn('等待当前页结束', response.json()['detail'])
            finally:
                release.set()
                deadline = time.monotonic() + 5
                while self.pid in self.reader._active and time.monotonic() < deadline:
                    time.sleep(.01)
            self.assertNotIn(self.pid, self.reader._active)
        self.assertEqual(self.db.job_get(f'translate:{self.pid}')['status'], 'done')
        translated = self.db.reader_get(self.pid, 'document')['pages'][0]['translated']
        response = self.client.post(f'/desktop/papers/{self.pid}/scientific/reanalyse', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['pages'][0]['translated'], translated)

    def test_editor_and_document_share_lock_order(self):
        order = []
        class RecordingLock:
            def __init__(self, name):
                self.name, self.lock = name, threading.RLock()
            def __enter__(self):
                order.append(self.name); self.lock.acquire(); return self
            def __exit__(self, *args):
                self.lock.release()
        bid = self.doc['pages'][0]['blocks'][0]['id']
        with patch.object(self.reader, '_lock', RecordingLock('state')), patch.object(self.reader, '_extract_lock', RecordingLock('extract')):
            response = self.client.patch(f'/desktop/papers/{self.pid}/editor', headers=self.headers, json={'block_id': bid, 'zh': '手动修改与结构检测统一锁顺序'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(order, ['state', 'extract', 'state', 'extract'])


if __name__ == '__main__':
    unittest.main()
