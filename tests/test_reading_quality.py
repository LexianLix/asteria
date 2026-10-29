"""Reading diagnostics expose extraction problems without rewriting source data."""
from copy import deepcopy
import unittest

from paper_reader.reading_quality import reading_quality
from tests import test_desktop as fixtures


def block(text, key='body', **extra):
    return {'id': key, 'en': text, 'box': [.1, .1, .9, .5], **extra}


class ReadingQualityTest(unittest.TestCase):
    def test_fragmented_embedded_text_is_reported_with_source_anchor(self):
        text = ' '.join('documentlayoutanalysis' * 4)
        document = {'pages': [{'number': 6, 'blocks': [block(text)], 'translated': {'body': '保留译文'}}]}
        before = deepcopy(document)
        result = reading_quality(document)
        self.assertEqual(result['review_pages'], 1)
        self.assertEqual(result['pages'][0]['issues'][0]['code'], 'fragmented_text')
        self.assertEqual(result['pages'][0]['issues'][0]['block_ids'], ['body'])
        self.assertEqual(result['pages'][0]['page_number'], 6)
        self.assertEqual(document, before)

    def test_formula_variables_and_protected_scientific_content_are_not_prose(self):
        formula = ' '.join('abcdefghijklmnopqrstuvwxyz' * 3)
        document = {'pages': [{'number': 0, 'blocks': [block(formula, protected_spans=[
            {'start': 0, 'end': len(formula), 'kind': 'formula'}])]}]}
        self.assertEqual(reading_quality(document)['review_pages'], 0)

    def test_replacement_glyphs_are_reported_but_short_labels_are_not_fragmented(self):
        document = {'pages': [{'number': 0, 'blocks': [
            block('The glyphs \ufffd and \uffff could not be decoded.'),
            block('A B C D E F', key='labels')]}]}
        result = reading_quality(document)
        self.assertEqual([issue['code'] for issue in result['pages'][0]['issues']], ['unreadable_glyphs'])

    def test_blank_and_image_only_pages_do_not_claim_ocr_or_success(self):
        result = reading_quality({'pages': [{'number': 0, 'blocks': []}]})
        self.assertEqual(result['no_text_pages'], 1)
        self.assertEqual(result['review_pages'], 1)
        self.assertEqual(result['pages'][0]['issues'][0]['code'], 'no_text')

    def test_normal_bilingual_prose_has_no_rule_alert_and_no_quality_score(self):
        result = reading_quality({'pages': [{'number': 0, 'blocks': [
            block('We evaluate the method on three datasets. 本研究在三个数据集上评估该方法。')]}]})
        self.assertEqual(result['review_pages'], 0)
        self.assertNotIn('score', result)


class ReadingQualityApiTest(unittest.TestCase):
    setUp = fixtures.DesktopTest.setUp
    tearDown = fixtures.DesktopTest.tearDown

    def test_diagnostics_are_derived_and_preserve_cached_content_and_notes(self):
        pid = self.paper['id']
        document = self.service.document(pid)
        target = document['pages'][0]['blocks'][0]
        target['en'] = ' '.join('documentlayoutanalysis' * 4)
        document['pages'][0]['translated'][target['id']] = '已有手改译文'
        self.db.reader_put(pid, 'document', document)
        self.db.add_annotation(pid, {'annotation_type': 'note', 'page_number': 0, 'text': '保留阅读笔记'})
        cached = deepcopy(self.db.reader_get(pid, 'document'))
        notes = self.db.list_annotations(pid)
        result = self.client.get(f'/desktop/papers/{pid}/document')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['reading_quality']['review_pages'], 1)
        self.assertEqual(self.db.reader_get(pid, 'document'), cached)
        self.assertEqual(self.db.list_annotations(pid), notes)
        self.assertNotIn('reading_quality', cached)


if __name__ == '__main__':
    unittest.main()
