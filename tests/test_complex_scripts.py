import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_reader.complex_pdf import normalize_complex_document
from paper_reader.complex_scripts import restore_complex_scripts
from paper_reader.reader import file_hash
from paper_reader.reviewed_reading import candidates
from complex_scripts_fixture import numeric_pdf_and_document


class ComplexScriptsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'numeric-qa.pdf'
        content, self.raw = numeric_pdf_and_document()
        self.path.write_bytes(content)
        self.digest = file_hash(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def restore(self, raw=None, **kwargs):
        raw = self.raw if raw is None else raw
        preview = normalize_complex_document(raw, self.digest)
        return restore_complex_scripts(preview, raw, self.path, **kwargs)

    def test_only_matching_scripts_change_not_cells_spans_ids_or_original_values(self):
        raw_before = copy.deepcopy(self.raw)
        before = normalize_complex_document(self.raw, self.digest)
        preview_before = copy.deepcopy(before)
        with patch('socket.socket.connect', side_effect=AssertionError('Must not connect')):
            result = restore_complex_scripts(before, self.raw, self.path)
        self.assertEqual(result['tables'][0]['rows'], [['256²', '10⁻⁴', 'b₁', '2562']])
        self.assertEqual(self.raw, raw_before)
        self.assertEqual(before, preview_before)
        self.assertEqual(result['tables'][0]['id'], before['tables'][0]['id'])
        self.assertEqual(result['tables'][0]['quality_flags'], before['tables'][0]['quality_flags'])
        for new, old in zip(result['tables'][0]['cells'], before['tables'][0]['cells']):
            for key in ('row', 'column', 'row_span', 'column_span', 'box'):
                self.assertEqual(new[key], old[key])
        self.assertNotIn('original_text', result['tables'][0]['cells'][3])
        evidence = result['tables'][0]['cells'][1]['script_restoration']
        self.assertEqual(evidence['source_hash'], self.digest)
        self.assertEqual(evidence['page'], 0)
        self.assertEqual(evidence['page_size'], [600, 800])
        self.assertEqual(len(evidence['changes']), 2)
        self.assertEqual(result['numeric_script_restoration']['restored_cells'], 3)
        self.assertEqual(result['numeric_script_restoration']['restored_characters'], 4)
        self.assertTrue(result['tables'][0]['requires_source_review'])

    def test_wrong_hash_refuses_without_mutating_preview(self):
        preview = normalize_complex_document(self.raw, 'b' * 64)
        before = copy.deepcopy(preview)
        with self.assertRaises(ValueError):
            restore_complex_scripts(preview, self.raw, self.path)
        self.assertEqual(preview, before)

    def text_raw(self, text='2562', column=0):
        raw = copy.deepcopy(self.raw)
        box = copy.deepcopy(raw['tables'][0]['data']['table_cells'][column]['bbox'])
        raw['texts'] = [{'self_ref': '#/texts/0', 'text': text, 'label': 'formula',
                         'prov': [{'page_no': 1, 'bbox': box}]}]
        raw['tables'] = []
        return raw

    def test_text_only_formula_restores_without_changing_identity_geometry_or_raw(self):
        raw = self.text_raw()
        before = copy.deepcopy(raw)
        with patch('socket.socket.connect', side_effect=AssertionError('Must not connect')):
            restored = self.restore(raw)
        text = restored['texts'][0]
        self.assertEqual(text['text'], '256²')
        self.assertEqual(text['original_text'], '2562')
        self.assertEqual(raw, before)
        original = normalize_complex_document(raw, self.digest)['texts'][0]
        for field in ('ref', 'label', 'provenance'):
            self.assertEqual(text[field], original[field])
        self.assertEqual(text['script_restoration']['source_hash'], self.digest)
        self.assertEqual(text['script_restoration']['page'], 0)
        self.assertEqual(restored['numeric_script_restoration']['restored_texts'], 1)
        self.assertEqual(restored['numeric_script_restoration']['restored_cells'], 0)
        self.assertEqual(restored['numeric_script_restoration']['restored_characters'], 1)
        self.assertEqual(restored['numeric_script_restoration']['restored_cell_characters'], 0)
        self.assertEqual(restored['numeric_script_restoration']['restored_text_characters'], 1)
        candidate = candidates(restored, 0)[0]
        original_candidate = candidates(normalize_complex_document(raw, self.digest), 0)[0]
        self.assertEqual(candidate['id'], original_candidate['id'])
        self.assertEqual(candidate['numeric_script_restoration']['original_text'], '2562')
        self.assertEqual(candidate['numeric_script_restoration']['restored_text'], '256²')

    def test_text_formula_mismatches_ambiguous_regions_and_ocr_are_not_guessed(self):
        for case in ('wrong_text', 'wrong_column', 'ambiguous', 'ocr'):
            raw = self.text_raw(text='5122' if case == 'wrong_text' else '2562',
                                column=1 if case == 'wrong_column' else 0)
            if case == 'ambiguous':
                raw['texts'][0]['prov'].append(copy.deepcopy(raw['texts'][0]['prov'][0]))
            with self.subTest(case=case):
                restored = self.restore(raw, ocr=case == 'ocr')
                self.assertEqual(restored['texts'][0]['text'], raw['texts'][0]['text'])
                self.assertNotIn('script_restoration', restored['texts'][0])
                self.assertEqual(restored['numeric_script_restoration']['restored_texts'], 0)

    def test_mismatched_digit_is_not_corrected_or_moved_to_another_column(self):
        self.raw['tables'][0]['data']['table_cells'][0]['text'] = '5122'
        result = self.restore()
        self.assertEqual(result['tables'][0]['rows'][0], ['5122', '10⁻⁴', 'b₁', '2562'])
        self.assertNotIn('original_text', result['tables'][0]['cells'][0])
        self.assertEqual(result['numeric_script_restoration']['unmatched_cells'][0]['column'], 0)
        self.assertEqual(result['numeric_script_restoration']['restored_cells'], 2)

    def test_bbox_from_another_column_does_not_rewrite_text(self):
        cells = self.raw['tables'][0]['data']['table_cells']
        cells[0]['bbox'] = copy.deepcopy(cells[1]['bbox'])
        result = self.restore()
        self.assertEqual(result['tables'][0]['cells'][0]['text'], '2562')
        self.assertNotIn('script_restoration', result['tables'][0]['cells'][0])

    def test_equivalent_cell_coordinate_origins_recover_the_same_characters(self):
        top_left = self.restore()
        for cell in self.raw['tables'][0]['data']['table_cells']:
            box = cell['bbox']
            box.update(t=800 - box['t'], b=800 - box['b'], coord_origin='BOTTOMLEFT')
        bottom_left = self.restore()
        self.assertEqual(top_left['tables'][0]['rows'], bottom_left['tables'][0]['rows'])
        self.assertEqual(top_left['tables'][0]['cells'][0]['box'], bottom_left['tables'][0]['cells'][0]['box'])

    def test_ocr_and_hidden_image_text_layers_are_never_source_overridden(self):
        original = normalize_complex_document(self.raw, self.digest)['tables'][0]['rows']
        explicit = self.restore(ocr=True)
        self.assertEqual(explicit['tables'][0]['rows'], original)
        self.assertEqual(explicit['numeric_script_restoration']['status'], 'skipped')
        content, self.raw = numeric_pdf_and_document(image_layer=True)
        self.path.write_bytes(content)
        self.digest = file_hash(self.path)
        detected = self.restore()
        self.assertEqual(detected['tables'][0]['rows'], original)
        self.assertEqual(detected['numeric_script_restoration']['restored_cells'], 0)
        self.assertEqual(detected['numeric_script_restoration']['skipped_pages'][0]['reason'],
                         'possible-old-ocr-image-layer')

    def test_mismatched_page_size_missing_boxes_and_ambiguous_provenance_skip(self):
        for change in ('size', 'no_boxes', 'multi_provenance'):
            raw = copy.deepcopy(self.raw)
            if change == 'size':
                raw['pages']['1']['size']['width'] = 601
            elif change == 'no_boxes':
                for cell in raw['tables'][0]['data']['table_cells']:
                    cell.pop('bbox')
            else:
                raw['tables'][0]['prov'].append(copy.deepcopy(raw['tables'][0]['prov'][0]))
            with self.subTest(change=change):
                result = self.restore(raw)
                self.assertEqual(result['tables'][0]['rows'], [['2562', '10-4', 'b1', '2562']])
                self.assertEqual(result['numeric_script_restoration']['restored_cells'], 0)

    def test_whitespace_and_non_script_punctuation_keep_original_spelling(self):
        cells = self.raw['tables'][0]['data']['table_cells']
        cells[0]['text'] = '256 2'
        cells[1]['text'] = '10 −4'
        result = self.restore()
        self.assertEqual(result['tables'][0]['rows'][0][:2], ['256 ²', '10 ⁻⁴'])


if __name__ == '__main__':
    unittest.main()
