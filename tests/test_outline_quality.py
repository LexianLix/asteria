"""Inferred headings must not turn numeric body text into a TOC entry."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import unittest
from unittest.mock import patch

from paper_reader.reader import add_outline, extract_document
import test_desktop as fixtures
from public_samples import resnet_pdf


class OutlineQualityTest(unittest.TestCase):
    def test_real_numeric_body_line_is_not_a_heading(self):
        block = {'id': 'body-source-id', 'en': '8 images (i.e., 1 per GPU) and the Fast R-CNN step has a',
            'box': [.52, .1, .9, .13], 'type': 'heading'}
        doc = {'pages': [{'number': 9, 'blocks': [deepcopy(block)],
            'translated': {'body-source-id': '测试既有译文'}, 'error': None}], 'outline': [{'id': block['id']} ]}
        before = deepcopy(doc)
        add_outline(doc)
        self.assertEqual(doc['outline'], [])
        self.assertEqual(doc['pages'][0]['blocks'][0]['type'], 'paragraph')
        for key in ('id', 'en', 'box'):
            self.assertEqual(doc['pages'][0]['blocks'][0][key], before['pages'][0]['blocks'][0][key])
        self.assertEqual(doc['pages'][0]['translated'], before['pages'][0]['translated'])

    def test_numbered_sections_and_named_sections_remain(self):
        titles = ['1. Introduction', '4.2. CIFAR-10 and Analysis', '3. Deep Residual Learning',
            'Abstract', 'References', 'Conclusion', 'Appendix A', '5.Conclusion']
        doc = {'pages': [{'number': 0, 'blocks': [
            {'id': str(i), 'en': text, 'box': [0, 0, 1, .1]} for i, text in enumerate(titles)]}]}
        add_outline(doc)
        self.assertEqual([entry['title'] for entry in doc['outline']], titles)

    def test_typography_keeps_lowercase_section_but_rejects_capitalized_body(self):
        doc = {'pages': [{'number': 0, 'blocks': [
            {'id': 'title', 'en': '2 related work', 'outline_style': {'emphasized': True, 'larger': False}},
            {'id': 'body', 'en': '8 Images are used in each batch', 'outline_style': {'emphasized': False, 'larger': False}},
        ]}]}
        add_outline(doc)
        self.assertEqual([entry['id'] for entry in doc['outline']], ['title'])

    def test_real_appendix_headings_not_numeric_body(self):
        source = resnet_pdf()
        if not source.is_file(): self.skipTest('Pinned public parser sample not present')
        doc = extract_document(source, page_range=(10, 10))
        source_blocks = [(b['id'], b['en'], deepcopy(b['box'])) for b in doc['pages'][0]['blocks']]
        add_outline(doc, source_path=source)
        self.assertEqual([entry['title'] for entry in doc['outline']],
            ['A. Object Detection Baselines', 'B. Object Detection Improvements'])
        self.assertEqual([(b['id'], b['en'], b['box']) for b in doc['pages'][0]['blocks']], source_blocks)

    def test_public_document_with_seeded_translation_cache_is_not_rewritten(self):
        source = resnet_pdf()
        if not source.is_file(): self.skipTest('Pinned public parser sample not present')
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        document = extract_document(source)
        for page in document['pages']:
            page['translated'] = {block['id']: 'Regression fixture translation: ' + block['en'] for block in page['blocks']}
        self.assertEqual(len(document['pages']), 12)
        self.assertTrue(all(page.get('translated') for page in document['pages']))
        def protected(value):
            result = deepcopy(value)
            for key in ('outline', 'outline_version'): result.pop(key, None)
            for page in result['pages']:
                for block in page['blocks']:
                    block.pop('type', None); block.pop('outline_style', None)
            return result
        original = digest(protected(document))
        add_outline(document, source_path=source)
        self.assertEqual(digest(protected(document)), original)


class OutlineCacheTest(unittest.TestCase):
    setUp = fixtures.DesktopTest.setUp
    tearDown = fixtures.DesktopTest.tearDown

    def test_old_outline_refresh_preserves_cache_editor_notes_and_state_without_reextracting(self):
        pid = self.paper['id']; doc = self.service.document(pid)
        block = doc['pages'][0]['blocks'][0]
        block['en'] = '8 images (i.e., 1 per GPU) and the Fast R-CNN step has a'
        block['type'] = 'heading'
        doc['pages'][0]['translated'][block['id']] = '既有译文不得重发或覆盖'
        doc['outline'] = [{'id': block['id'], 'title': block['en'], 'page': 0}]
        doc.pop('outline_version', None)
        self.db.reader_put(pid, 'document', doc)
        self.db.reader_put(pid, 'editor', {'edits': {block['id']: {'zh': '手改译文'}}})
        self.db.reader_put(pid, 'state', {'last_page': 1, 'pdf_zoom': 1.15})
        note = self.db.add_annotation(pid, {'annotation_type': 'note', 'page_number': 0, 'text': '保留笔记'})
        before = deepcopy(doc)
        with patch('paper_reader.reader.extract_document', side_effect=AssertionError('Do not reparse the original layout')):
            updated = self.service.document(pid)
        self.assertFalse(any(entry['id'] == block['id'] for entry in updated['outline']))
        self.assertEqual(updated['outline_version'], 2)
        self.assertEqual(updated['pages'][0]['blocks'][0]['type'], 'paragraph')
        for old_page, new_page in zip(before['pages'], updated['pages']):
            self.assertEqual(old_page['translated'], new_page['translated'])
            self.assertEqual([(b['id'], b['en'], b['box']) for b in old_page['blocks']],
                [(b['id'], b['en'], b['box']) for b in new_page['blocks']])
        self.assertEqual(self.db.reader_get(pid, 'editor'), {'edits': {block['id']: {'zh': '手改译文'}}})
        self.assertEqual(self.db.reader_get(pid, 'state'), {'last_page': 1, 'pdf_zoom': 1.15})
        self.assertEqual(self.db.list_annotations(pid), [note])
        with patch('paper_reader.reader.add_outline', side_effect=AssertionError('Current outline is cached')):
            self.assertEqual(self.service.document(pid), updated)
