"""Current-layout cache gaps must be visible and locally repairable without AI."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from paper_reader.model_client import ModelClient
import test_translation_missing as fixtures


class TranslationCompletenessTest(unittest.TestCase):
    setUp = fixtures.MissingTranslationTest.setUp
    tearDown = fixtures.MissingTranslationTest.tearDown
    table = fixtures.MissingTranslationTest.table

    def test_complete_cache_still_exposes_numeric_differences_and_respects_manual_edit(self):
        for page in self.doc['pages']:
            page['translated'] = {block['id']: block['en'] for block in page['blocks']}
        target = self.doc['pages'][0]['blocks'][0]
        target['en'] = 'The metric is 12 [3].'
        self.doc['pages'][0]['translated'][target['id']] = '指标为 21 [4]。'
        self.db.reader_put(self.pid, 'document', self.doc)
        original = deepcopy(self.doc)
        result = self.client.get(f'/desktop/papers/{self.pid}/document').json()['translation_status']
        self.assertEqual(result['pages'][0]['state'], 'complete')
        self.assertEqual(result['review_items'], 1)
        self.assertEqual(result['pages'][0]['review_items'][0]['id'], target['id'])
        self.db.reader_put(self.pid, 'editor', {'edits': {target['id']: {'zh': '指标为 12 [3]。'}}})
        result = self.client.get(f'/desktop/papers/{self.pid}/document').json()['translation_status']
        self.assertEqual(result['review_items'], 0)
        self.assertEqual(self.db.reader_get(self.pid, 'document'), original)

    def seed_gaps(self):
        self.table([['Accuracy', '62.1', '-']])
        self.db.reader_put(self.pid, 'document', self.doc)
        self.db.job_put(f'translate:{self.pid}', {'status': 'done', 'done': 2, 'total': 2})

    def test_current_layout_gaps_visible_despite_old_completed_job(self):
        self.seed_gaps()
        response = self.client.get(f'/desktop/papers/{self.pid}/document')
        self.assertEqual(response.status_code, 200)
        self.assertIn('translation_status', response.json(), 'Current-layout completion metadata is absent')
        page = response.json()['translation_status']['pages'][0]
        self.assertEqual(page['state'], 'partial')
        self.assertEqual(page['missing_text'], 1)
        self.assertEqual(page['missing_local'], 2)

    def test_local_repair_keeps_text_gaps_and_existing_values_without_a_model(self):
        self.seed_gaps()
        before = deepcopy(self.db.reader_get(self.pid, 'document'))
        with patch.object(ModelClient, 'generate', side_effect=AssertionError('Must not call AI')):
            response = self.client.post(f'/desktop/papers/{self.pid}/translation/local-repair',
                                        headers=self.headers, json={'page_number': 0})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['added_local'], 2)
        page = self.db.reader_get(self.pid, 'document')['pages'][0]
        self.assertEqual({key: page['translated'][key] for key in self.before}, self.before)
        self.assertEqual(page['translated']['new-table-r0c1'], '62.1')
        self.assertEqual(page['translated']['new-table-r0c2'], '-')
        self.assertNotIn('new-table-r0c0', page['translated'])
        self.assertEqual(page['blocks'], before['pages'][0]['blocks'])
        self.assertEqual(response.json()['translation_status']['pages'][0]['missing_text'], 1)
        self.assertEqual(response.json()['translation_status']['pages'][0]['missing_local'], 0)

    def test_repair_is_idempotent_and_preserves_notes_editor_state_and_job(self):
        self.seed_gaps()
        self.db.reader_put(self.pid, 'editor', {'edits': {'manual': {'zh': 'Keep manual edit'}}})
        self.db.reader_put(self.pid, 'state', {'last_page': 1, 'pdf_zoom': 1.2})
        response = self.client.post(f'/desktop/papers/{self.pid}/annotations', headers=self.headers,
            json={'client_id': 'local-repair-note', 'page_number': 0, 'annotation_type': 'note', 'text': 'Keep note'})
        self.assertEqual(response.status_code, 201)
        before = {key: self.db.reader_get(self.pid, key) for key in ('editor', 'state')}
        notes = self.db.list_annotations(self.pid)
        job = self.db.job_get(f'translate:{self.pid}')
        with patch.object(self.reader, 'model_client', side_effect=AssertionError('No model client')):
            first = self.client.post(f'/desktop/papers/{self.pid}/translation/local-repair', headers=self.headers, json={})
            second = self.client.post(f'/desktop/papers/{self.pid}/translation/local-repair', headers=self.headers, json={})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['added_local'], 0)
        self.assertEqual({key: self.db.reader_get(self.pid, key) for key in before}, before)
        self.assertEqual(self.db.list_annotations(self.pid), notes)
        self.assertEqual(self.db.job_get(f'translate:{self.pid}'), job)

    def test_existing_nonblank_numeric_value_is_not_silently_replaced(self):
        self.seed_gaps()
        self.doc['pages'][0]['translated']['new-table-r0c1'] = 'User-retained value'
        self.db.reader_put(self.pid, 'document', self.doc)
        response = self.client.post(f'/desktop/papers/{self.pid}/translation/local-repair', headers=self.headers, json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['added_local'], 1)
        self.assertEqual(self.db.reader_get(self.pid, 'document')['pages'][0]['translated']['new-table-r0c1'], 'User-retained value')

    def test_repair_rejects_missing_token_bad_page_and_active_worker(self):
        self.seed_gaps()
        path = f'/desktop/papers/{self.pid}/translation/local-repair'
        self.assertEqual(self.client.post(path, json={}).status_code, 403)
        self.assertEqual(self.client.post(path, headers={**self.headers, 'Origin': 'https://example.invalid'}, json={}).status_code, 403)
        for page in (-1, 999):
            self.assertEqual(self.client.post(path, headers=self.headers, json={'page_number': page}).status_code, 422)
        self.reader._active[self.pid] = object()
        try:
            self.assertEqual(self.client.post(path, headers=self.headers, json={}).status_code, 409)
        finally:
            self.reader._active.pop(self.pid)

    def test_invalid_protection_blocks_repair_without_partial_writes(self):
        self.seed_gaps()
        self.doc['pages'][1]['blocks'][0]['protected_spans'] = [{'start': 0, 'end': 99999, 'kind': 'formula'}]
        self.db.reader_put(self.pid, 'document', self.doc)
        before = deepcopy(self.db.reader_get(self.pid, 'document'))
        response = self.client.get(f'/desktop/papers/{self.pid}/document')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['translation_status']['blocked_pages'], 1)
        response = self.client.post(f'/desktop/papers/{self.pid}/translation/local-repair', headers=self.headers, json={})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.db.reader_get(self.pid, 'document'), before)

    def test_empty_and_stale_entries_do_not_inflate_completion(self):
        self.seed_gaps()
        self.doc['pages'][0]['translated']['new-table-r0c0'] = '   '
        self.doc['pages'][0]['translated']['stale-block'] = 'Old entry'
        self.db.reader_put(self.pid, 'document', self.doc)
        response = self.client.get(f'/desktop/papers/{self.pid}/document')
        page = response.json()['translation_status']['pages'][0]
        self.assertEqual(page['missing_text'], 1)
        self.assertEqual(page['cached'], len(self.before))

    def test_status_is_not_persisted_as_document_content(self):
        self.seed_gaps()
        before = deepcopy(self.db.reader_get(self.pid, 'document'))
        self.assertEqual(self.client.get(f'/desktop/papers/{self.pid}/document').status_code, 200)
        self.assertEqual(self.db.reader_get(self.pid, 'document'), before)

    def test_protected_whole_formula_is_restored_locally_without_authentication(self):
        text = 'x² + y² = z²'
        self.page['blocks'].append({'id': 'protected-formula', 'en': text, 'box': [.1, .8, .9, .9],
            'protected_spans': [{'start': 0, 'end': len(text), 'kind': 'formula'}]})
        self.db.reader_put(self.pid, 'document', self.doc)
        with patch.object(self.reader, 'check_chatgpt_destination', side_effect=AssertionError('No AI authorization needed')):
            response = self.client.post(f'/desktop/papers/{self.pid}/translation/local-repair', headers=self.headers,
                                        json={'page_number': 0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['added_local'], 1)
        self.assertEqual(self.db.reader_get(self.pid, 'document')['pages'][0]['translated']['protected-formula'], text)

    def test_index_failure_rolls_back_the_document_and_does_not_lose_old_cache(self):
        self.seed_gaps()
        before = deepcopy(self.db.reader_get(self.pid, 'document'))
        with patch.object(self.db, '_index_paper', side_effect=RuntimeError('Injected index failure')):
            with self.assertRaisesRegex(RuntimeError, 'Injected index failure'):
                self.client.post(f'/desktop/papers/{self.pid}/translation/local-repair', headers=self.headers, json={})
        self.assertEqual(self.db.reader_get(self.pid, 'document'), before)


if __name__ == '__main__':
    unittest.main()
