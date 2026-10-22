import copy
import json
import threading
import time
import unittest
from unittest.mock import patch

import test_complex_service as fixtures
from paper_reader.backup import BackupManager
from paper_reader.reader import PARSER_VERSION, ReaderService, extract_document
from paper_reader.reviewed_reading import candidates
from paper_reader.scientific import SCIENTIFIC_VERSION


class ReviewedReadingTest(unittest.TestCase):
    setUp = fixtures.ComplexServiceTest.setUp
    tearDown = fixtures.ComplexServiceTest.tearDown
    wait = fixtures.ComplexServiceTest.wait
    success = fixtures.ComplexServiceTest.success
    start = fixtures.ComplexServiceTest.start

    def prepare(self):
        with patch.object(self.parser, '_execute', side_effect=self.success):
            self.start(ocr=True); self.assertEqual(self.wait()['status'], 'done')
        return self.review()

    def review(self, page=0):
        response = self.client.get(f'/desktop/papers/{self.pid}/reading-source/{page}')
        self.assertEqual(response.status_code, 200)
        return response.json()

    def body(self, review):
        return {key: review[key] for key in ('source_hash', 'document_token', 'preview_revision')} | {
            'confirmed': True, 'blocks': [{'candidate_id': c['id'], 'text': c['text']} for c in review['candidates']]}

    def adopt(self, body, page=0):
        return self.client.post(f'/desktop/papers/{self.pid}/reading-source/{page}/adopt', headers=self.headers, json=body)

    def switch(self, version, review=None):
        review = review or self.review()
        return self.client.post(f'/desktop/papers/{self.pid}/reading-source/0/switch', headers=self.headers,
            json={key: review[key] for key in ('source_hash', 'document_token')} | {'version': version})

    def test_geometry_default_order_is_review_only_and_adoption_still_uses_user_order(self):
        review = self.prepare()
        preview = self.parser.preview(self.pid)
        entries = [('L1', [.10, .20, .40, .25]), ('R1', [.60, .20, .90, .25]),
                   ('L2', [.10, .30, .40, .35]), ('R2', [.60, .30, .90, .35])]
        preview['texts'] = [{'ref': f'#/texts/{index}', 'label': 'text', 'text': text,
            'provenance': [{'page': 0, 'box': box}]} for index, (text, box) in enumerate(entries)]
        preview['tables'] = []
        self.db.reader_put(self.pid, 'complex-preview', preview)
        stored = copy.deepcopy(preview)
        primary = copy.deepcopy(self.db.reader_get(self.pid, 'document'))
        ordered = self.review()
        self.assertEqual([entry['text'] for entry in ordered['candidates']], ['L1', 'L2', 'R1', 'R2'])
        self.assertEqual(self.db.reader_get(self.pid, 'complex-preview'), stored)
        self.assertEqual(self.db.reader_get(self.pid, 'document'), primary)
        self.assertEqual({item['id'] for item in ordered['candidates']},
                         {item['id'] for item in candidates(preview, 0)})
        body = self.body(ordered)
        body['blocks'].reverse()  # Explicit reviewer edits always win over the suggestion.
        adopted = self.adopt(body)
        self.assertEqual(adopted.status_code, 200)
        self.assertEqual([block['en'] for block in adopted.json()['document']['pages'][0]['blocks']],
                         ['R2', 'R1', 'L2', 'L1'])
        self.assertEqual(self.switch('original').status_code, 200)
        self.assertEqual(self.reader.document(self.pid)['pages'][0], primary['pages'][0])

    def test_adoption_translation_edit_anchor_and_versions_roundtrip(self):
        review = self.prepare(); original = copy.deepcopy(self.reader.document(self.pid)['pages'][0])
        old_id = original['blocks'][0]['id']
        self.db.reader_put(self.pid, 'editor', {'edits': {old_id: {'zh': '原段手改', 'base': 'old'}}})
        note = self.db.add_annotation(self.pid, {'page_number': 0, 'annotation_type': 'text-highlight',
            'text': json.dumps({'block_id': old_id, 'quote': original['blocks'][0]['en']})})
        result = self.adopt(self.body(review)); self.assertEqual(result.status_code, 200)
        adopted = result.json()['document']['pages'][0]; key = adopted['blocks'][0]['id']
        self.assertNotEqual(key, old_id); self.assertEqual(adopted['translated'], {})
        version = adopted['reading_source']['version']
        doc = self.reader.document(self.pid); doc['pages'][0]['translated'][key] = '已完成译文'
        self.db.reader_put(self.pid, 'document', doc)
        editor = self.db.reader_get(self.pid, 'editor'); editor['edits'][key] = {'zh': '采用后的手改', 'base': 'new'}
        self.db.reader_put(self.pid, 'editor', editor)
        self.assertEqual(self.switch('original').status_code, 200)
        self.assertEqual(self.reader.document(self.pid)['pages'][0], original)
        self.assertEqual(self.db.list_annotations(self.pid)[0], note)
        self.assertEqual(self.switch(version).status_code, 200)
        self.assertEqual(self.reader.document(self.pid)['pages'][0]['translated'][key], '已完成译文')
        self.assertEqual(self.db.reader_get(self.pid, 'editor')['edits'][old_id]['zh'], '原段手改')
        self.assertEqual(self.db.reader_get(self.pid, 'editor')['edits'][key]['zh'], '采用后的手改')

    def test_exact_anchors_migrate_changed_text_does_not_reuse_translation(self):
        review = self.prepare(); body = self.body(review)
        first = self.adopt(body).json()['document']['pages'][0]; key = first['blocks'][0]['id']
        doc = self.reader.document(self.pid); doc['pages'][0]['translated'][key] = '旧缓存'
        self.db.reader_put(self.pid, 'document', doc)
        body = self.body(self.review()); body['blocks'].append({'text': 'A reviewed missing paragraph 256².'})
        adopted = self.adopt(body).json()['document']['pages'][0]
        self.assertEqual(adopted['blocks'][0]['id'], key); self.assertEqual(adopted['translated'][key], '旧缓存')
        self.assertNotIn(adopted['blocks'][1]['id'], adopted['translated'])
        body = self.body(self.review()); body['blocks'][0]['text'] += ' corrected'
        changed = self.adopt(body).json()['document']['pages'][0]
        self.assertNotEqual(changed['blocks'][0]['id'], key); self.assertEqual(changed['translated'], {})

    def test_partial_migration_is_not_mistaken_for_complete_translation(self):
        self.prepare(); body = self.body(self.review())
        first = self.adopt(body).json()['document']['pages'][0]; key = first['blocks'][0]['id']
        doc = self.reader.document(self.pid); doc['pages'][0]['translated'][key] = '部分缓存'
        self.db.reader_put(self.pid, 'document', doc)
        body = self.body(self.review()); body['blocks'].append({'text': 'New paragraph requiring translation.'})
        self.adopt(body)
        def translate(page, cfg):
            return {b['id']: '模拟译文 ' + b['en'] for b in page['blocks']}
        with patch.object(self.reader, 'translate_page', side_effect=translate) as model:
            self.reader.start(self.pid, scope='page', page_number=0)
            deadline = time.monotonic() + 3
            while self.reader._active and time.monotonic() < deadline: time.sleep(.02)
            self.assertFalse(self.reader._active); self.assertEqual(model.call_count, 1)
        self.assertEqual(self.db.job_get(f'translate:{self.pid}')['status'], 'done')
        current = self.reader.document(self.pid)['pages'][0]
        self.assertTrue(all(b['id'] in current['translated'] for b in current['blocks']))

    def test_old_source_preview_and_document_tokens_are_rejected(self):
        review = self.prepare(); body = self.body(review)
        self.assertEqual(self.adopt(body).status_code, 200)
        self.assertEqual(self.adopt(body).status_code, 409)
        self.assertEqual(self.switch('original', review).status_code, 409)
        body = self.body(self.review())
        preview = self.parser.preview(self.pid); link = preview['table_links'][0]
        self.parser.confirm_link(self.pid, preview['source_hash'], link['from_id'], link['to_id'], preview['revision'])
        self.assertEqual(self.adopt(body).status_code, 409)
        body = self.body(self.review())
        path = self.settings.root_dir / self.db.get_paper(self.pid)['local_pdf_path']
        path.write_bytes(path.read_bytes() + b'\n%changed')
        self.assertEqual(self.adopt(body).status_code, 409)
        self.assertEqual(self.switch('original', review).status_code, 409)

    def test_active_parser_or_translation_blocks_adoption_and_switch(self):
        body = self.body(self.prepare()); self.adopt(body)
        for active in (self.reader._active, self.parser._active):
            active[self.pid] = threading.Event()
            try:
                self.assertEqual(self.adopt(self.body(self.review())).status_code, 409)
                self.assertEqual(self.switch('original').status_code, 409)
            finally: active.pop(self.pid)

    def test_validation_token_page_ownership_and_explicit_confirmation(self):
        body = self.body(self.prepare()); baseline = self.db.reader_get(self.pid, 'document')
        for bad in ({**body, 'confirmed': False}, {**body, 'blocks': []},
                    {**body, 'blocks': [body['blocks'][0], body['blocks'][0]]},
                    {**body, 'blocks': [{'candidate_id': self.review(1)['candidates'][0]['id'], 'text': 'Other page'}]},
                    {**body, 'blocks': [{'text': '   '}]}):
            self.assertEqual(self.adopt(bad).status_code, 422)
        self.assertEqual(self.client.post(f'/desktop/papers/{self.pid}/reading-source/0/adopt', json=body).status_code, 403)
        self.assertEqual(self.db.reader_get(self.pid, 'document'), baseline)
        self.assertEqual(self.db.reader_get(self.pid, 'reading-history'), {})
        self.assertEqual(self.switch('absent').status_code, 422)

    def test_atomic_switch_rolls_back_history_document_and_search(self):
        body = self.body(self.prepare())
        before = {kind: self.db.reader_get(self.pid, kind) for kind in ('document', 'reading-history', 'search')}
        with patch.object(self.db, '_index_paper', side_effect=RuntimeError('Simulated write failure')):
            with self.assertRaises(RuntimeError): self.adopt(body)
        for kind, value in before.items(): self.assertEqual(self.db.reader_get(self.pid, kind), value)

    def test_actual_backup_restore_restart_and_parser_upgrade_preserve_review(self):
        self.adopt(self.body(self.prepare())); expected = self.reader.document(self.pid)
        manager = BackupManager(self.settings, self.db); archive = manager.create()
        self.switch('original'); manager.restore(archive['name'])
        self.assertEqual(self.reader.document(self.pid), expected)
        new_reader = ReaderService(self.settings, self.db)
        doc = self.db.reader_get(self.pid, 'document'); doc['parser_version'] = -1; doc['scientific_version'] = -1
        self.db.reader_put(self.pid, 'document', doc)
        upgraded = new_reader.document(self.pid, force_scientific=True)
        self.assertEqual(upgraded['pages'][0]['blocks'], expected['pages'][0]['blocks'])
        self.assertEqual(upgraded['pages'][0]['reading_source'], expected['pages'][0]['reading_source'])
        self.assertEqual(self.switch('original').status_code, 200)

    def test_changed_legacy_body_can_adopt_new_extraction_without_docling_and_switch_back(self):
        legacy = copy.deepcopy(self.doc)
        legacy['parser_version'] = 2
        legacy.pop('extraction_checked_version', None)
        block = legacy['pages'][0]['blocks'][0]
        block['id'] = 'legacy-split-title'
        block['en'] = 'Old split heading'
        legacy['pages'][0]['translated'][block['id']] = '旧分组译文'
        self.db.reader_put(self.pid, 'document', legacy)
        editor = {'edits': {block['id']: {'zh': '保留手改译文'}}}
        self.db.reader_put(self.pid, 'editor', editor)
        annotation = self.db.add_annotation(self.pid, {'page_number': 0, 'annotation_type': 'text-highlight',
            'text': json.dumps({'block_id': block['id'], 'quote': block['en']})})
        with patch('paper_reader.reader.extract_document', wraps=extract_document) as extraction:
            upgraded = self.reader.document(self.pid)
            self.reader.document(self.pid)
            self.assertEqual(extraction.call_count, 1)
        self.assertEqual(upgraded['parser_version'], 2)
        self.assertEqual(upgraded['extraction_checked_version'], PARSER_VERSION)
        self.assertEqual(upgraded['pages'][0]['blocks'], legacy['pages'][0]['blocks'])
        self.assertEqual(upgraded['pages'][0]['translated'], legacy['pages'][0]['translated'])
        review = self.review()
        self.assertEqual(review['candidate_source'], 'extraction')
        body = self.body(review) | {'candidate_source': 'extraction'}
        adopted = self.adopt(body)
        self.assertEqual(adopted.status_code, 200, adopted.text)
        page = adopted.json()['document']['pages'][0]
        self.assertEqual([b['en'] for b in page['blocks']], [b['en'] for b in self.doc['pages'][0]['blocks']])
        self.assertNotIn('legacy-split-title', page['translated'])
        self.assertEqual(self.db.reader_get(self.pid, 'editor'), editor)
        self.assertEqual(self.db.list_annotations(self.pid), [annotation])
        version = page['reading_source']['version']
        self.assertEqual(self.switch('original').status_code, 200)
        restored = self.reader.document(self.pid)['pages'][0]
        self.assertEqual(restored['blocks'], upgraded['pages'][0]['blocks'])
        self.assertEqual(restored['translated'], legacy['pages'][0]['translated'])
        self.assertEqual(self.switch(version).status_code, 200)
        self.assertEqual(self.reader.document(self.pid)['pages'][0], page)
        review = self.review()
        stale = self.body(review) | {'candidate_source': 'extraction', 'preview_revision': review['preview_revision'] + 1}
        self.assertEqual(self.adopt(stale).status_code, 409)

    def test_extraction_upgrade_remains_available_alongside_an_enhanced_preview(self):
        self.prepare()
        doc = self.reader.document(self.pid)
        doc['parser_version'] = 2
        doc.pop('extraction_checked_version', None)
        doc['pages'][0]['blocks'][0]['en'] = 'Old grouped source text'
        self.db.reader_put(self.pid, 'document', doc)
        review = self.review()
        self.assertEqual(review['candidate_source'], 'enhanced')
        self.assertTrue(review['extraction_candidates'])
        body = self.body(review) | {'candidate_source': 'extraction',
            'preview_revision': review['extraction_revision'],
            'blocks': [{'candidate_id': c['id'], 'text': c['text']} for c in review['extraction_candidates']]}
        result = self.adopt(body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['document']['pages'][0]['blocks'][0]['en'], self.doc['pages'][0]['blocks'][0]['en'])

    def test_parser_only_upgrade_preserves_active_scientific_links_until_adoption(self):
        legacy = copy.deepcopy(self.doc)
        legacy['extraction_checked_version'] = PARSER_VERSION - 1
        legacy['parser_version'] = PARSER_VERSION - 1
        legacy['scientific_version'] = SCIENTIFIC_VERSION
        page = legacy['pages'][0]
        anchor = page['blocks'][0]['id']
        page['scientific_regions'] = [{'id': 'old-region', 'kind': 'formula',
            'box': page['blocks'][0]['box'], 'block_ids': [anchor]}]
        page['translated'][anchor] = '保留旧译文'
        self.db.reader_put(self.pid, 'document', legacy)
        enriched = copy.deepcopy(self.doc)
        enriched['pages'][0]['blocks'][0]['id'] = 'new-parser-anchor'
        enriched['pages'][0]['blocks'][0]['en'] = 'Corrected extraction candidate'
        enriched['pages'][0]['scientific_regions'] = [{'id': 'new-region', 'kind': 'formula',
            'box': page['blocks'][0]['box'], 'block_ids': ['new-parser-anchor']}]
        with patch('paper_reader.reader.extract_document', return_value=enriched) as extraction:
            upgraded = self.reader.document(self.pid)
            self.assertEqual(upgraded['pages'], legacy['pages'])
            self.assertEqual(upgraded['parser_version'], PARSER_VERSION - 1)
            self.assertEqual(upgraded['extraction_checked_version'], PARSER_VERSION)
            self.assertEqual(upgraded['extraction_candidates']['0'], enriched['pages'][0])
            self.reader.document(self.pid)
            self.assertEqual(extraction.call_count, 1)

    def test_scientific_version_upgrade_and_explicit_refresh_still_update_regions(self):
        for force in (False, True):
            with self.subTest(force=force):
                legacy = copy.deepcopy(self.doc)
                legacy['scientific_version'] = SCIENTIFIC_VERSION if force else SCIENTIFIC_VERSION - 1
                legacy['pages'][0]['scientific_regions'] = []
                self.db.reader_put(self.pid, 'document', legacy)
                enriched = copy.deepcopy(self.doc)
                anchor = legacy['pages'][0]['blocks'][0]['id']
                regions = [{'id': 'refreshed-region', 'kind': 'formula',
                    'box': legacy['pages'][0]['blocks'][0]['box'], 'block_ids': [anchor]}]
                enriched['pages'][0]['scientific_regions'] = regions
                with patch('paper_reader.reader.extract_document', return_value=enriched):
                    upgraded = self.reader.document(self.pid, force_scientific=force)
                self.assertEqual(upgraded['pages'][0]['scientific_regions'], regions)
                self.assertEqual(upgraded['scientific_version'], SCIENTIFIC_VERSION)
                self.assertEqual(upgraded['pages'][0]['blocks'], legacy['pages'][0]['blocks'])


if __name__ == '__main__': unittest.main()
