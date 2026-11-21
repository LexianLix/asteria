import json
import time
import unittest
from copy import deepcopy
from unittest.mock import patch

from paper_reader.model_client import ModelClient, ModelResult
from paper_reader.reader import translation_continuations
import test_translation_missing as fixtures


class ReadingAndExportTest(unittest.TestCase):
    setUp = fixtures.MissingTranslationTest.setUp
    tearDown = fixtures.MissingTranslationTest.tearDown

    def test_selected_retranslation_sends_only_its_source_and_keeps_other_data(self):
        block = self.page['blocks'][0]
        key = block['id']
        block['en'] = 'The team won 1st place [12] in the ImageNet benchmark and'
        block['type'] = 'paragraph'
        self.page['blocks'][1]['en'] = 'evaluated alternative networks separately.'
        self.page['blocks'][1]['type'] = 'paragraph'
        block['box'] = [.1, .7, .45, .95]
        self.page['blocks'][1]['box'] = [.55, .1, .9, .35]
        self.page['error'] = 'Unrelated page error'
        self.db.reader_put(self.pid, 'document', self.doc)
        editor = {'edits': {key: {'zh': '我的译文'}}}
        self.db.reader_put(self.pid, 'editor', editor)
        note = self.db.add_annotation(self.pid, {'page_number': 0, 'annotation_type': 'note', 'text': '笔记'})
        before = deepcopy(self.page['translated'])
        requests = []

        def respond(prompt, **kwargs):
            source = json.loads(prompt.split('\n', 1)[1])
            requests.extend(source)
            self.assertEqual(len(source), 1)
            self.assertEqual(source[0]['fragment'], 'next')
            self.assertNotIn('alternative networks', prompt)
            return ModelResult(json.dumps([{'id': key, 'zh': '新译文 ' + source[0]['en']}]), {}, .01)

        payload = {'scope': 'blocks', 'page_number': 0, 'block_ids': [key], 'source_hash': self.doc['source_hash']}
        with patch.object(ModelClient, 'generate', side_effect=respond):
            result = self.client.post(f'/desktop/papers/{self.pid}/translation', headers=self.headers, json=payload)
            self.assertEqual(result.status_code, 202)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = self.db.job_get(f'translate:{self.pid}')
                if job['status'] not in ('queued', 'running', 'cancelling'):
                    break
                time.sleep(.01)
            self.assertEqual(job['status'], 'done', job)
        self.assertEqual(len(requests), 1)
        self.assertNotIn('[12]', requests[0]['en'])
        self.assertNotIn('1st', requests[0]['en'])
        after = self.db.reader_get(self.pid, 'document')['pages'][0]
        self.assertRegex(after['translated'][key], r'第1名\s+\[12\]')
        self.assertEqual({k: v for k, v in after['translated'].items() if k != key},
                         {k: v for k, v in before.items() if k != key})
        self.assertEqual(after['error'], 'Unrelated page error')
        self.assertEqual(self.db.reader_get(self.pid, 'editor'), editor)
        self.assertEqual(self.db.list_annotations(self.pid), [note])
        with patch.object(ModelClient, 'generate') as generate:
            result = self.client.post(f'/desktop/papers/{self.pid}/translation', headers=self.headers,
                                      json={**payload, 'block_ids': ['stale']})
            self.assertEqual(result.status_code, 422)
            generate.assert_not_called()

    def test_cross_page_fragment_does_not_link_headings(self):
        previous = {'id': 'first', 'box': [.1, .7, .9, .9], 'en': 'The long results show that this architecture performs'}
        following = {'id': 'second', 'box': [.1, .1, .9, .3], 'en': 'better on this benchmark.'}
        pages = [{'blocks': [previous]}, {'blocks': [following]}]
        self.assertEqual(translation_continuations(pages, 0), {'first': 'next'})
        self.assertEqual(translation_continuations(pages, 1), {'second': 'previous'})
        following['type'] = 'heading'
        self.assertEqual(translation_continuations(pages, 1), {})

    def test_notes_filter_uses_extracted_quote_and_exports_its_original_page(self):
        self.db.add_annotation(self.pid, {'page_number': 0, 'annotation_type': 'note', 'text': '留在原处'})
        self.db.add_annotation(self.pid, {'page_number': 1, 'annotation_type': 'text-highlight',
            'text': json.dumps({'quote': '异构架构', 'block_id': 'source-anchor'}, ensure_ascii=False)})
        result = self.client.get('/desktop/notes', params={'q': '异构', 'kind': 'text-highlight'}).json()
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['block_id'], 'source-anchor')
        export = self.client.get('/desktop/notes.md', params={'q': '异构'})
        self.assertIn('第 2 页', export.text)
        self.assertIn('异构架构', export.text)
        self.assertNotIn('source-anchor', export.text)
        self.assertNotIn('留在原处', export.text)

    def test_citations_escape_fields_and_leave_missing_metadata_out(self):
        other, _ = self.db.upsert_paper({'source': 'test', 'source_id': 'metadata', 'title': '{Paper} & 100%',
            'authors': ['Ada Lovelace'], 'doi': 'https://doi.org/10.1234/Case', 'published_at': '2020-02-07'})
        bib = self.client.post('/desktop/citations/export', headers=self.headers,
                               json={'paper_ids': [self.pid, other, other], 'format': 'bibtex'})
        self.assertEqual(bib.status_code, 200)
        self.assertEqual(bib.text.count('@misc'), 2)
        self.assertEqual(bib.text.count('year ='), 1)
        self.assertIn(r'\{Paper\} \& 100\%', bib.text)
        self.assertIn('10.1234/case', bib.text)
        ris = self.client.post('/desktop/citations/export', headers=self.headers,
                               json={'paper_ids': [self.pid, other], 'format': 'ris'})
        self.assertEqual(ris.text.count('ER  -'), 2)
        self.assertIn('PY  - 2020', ris.text)
        self.assertIn('AU  - Ada Lovelace', ris.text)
