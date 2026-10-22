"""A partial inner grid must not suppress its table's full enclosure."""
import unittest
from pathlib import Path
from paper_reader.scientific import detect_regions, preserve_regions, word_lines
from paper_reader.reader import extract_document


class Candidate:
    def __init__(self, bbox, rows):
        self.bbox, self.values = bbox, rows
    def extract(self, **kwargs):
        return self.values


class Page:
    width, height = 600, 800
    edges, chars = [], []
    def __init__(self, tables): self.tables = tables
    def find_tables(self, settings): return self.tables


class NestedTableTest(unittest.TestCase):
    def test_upgrade_keeps_old_values_and_rebinds_same_source_anchors(self):
        from copy import deepcopy
        text = 'system mAP car tv baseline 85.6 89.9 86.8'
        old = {'pages': [{'number': 0, 'blocks': [{'id': 'body', 'en': text, 'box': [0, 0, 1, 1],
            'protected_spans': [{'start': 7, 'end': 14, 'kind': 'table', 'region_id': 'old-inner'}]}],
            'translated': {'body': '保留原有译文', 'old-inner-r0c0': '旧单元格缓存'}}]}
        fresh = deepcopy(old)
        fresh['pages'][0]['scientific_regions'] = [{'id': 'new-outer', 'kind': 'table', 'block_ids': ['body']}]
        fresh['pages'][0]['blocks'][0]['protected_spans'] = [{'start': 0, 'end': len(text),
            'kind': 'table', 'region_id': 'new-outer'}]
        result = preserve_regions(deepcopy(old), fresh)
        self.assertEqual(result['pages'][0]['translated'], old['pages'][0]['translated'])
        self.assertEqual(result['pages'][0]['blocks'][0]['id'], 'body')
        self.assertEqual(result['pages'][0]['blocks'][0]['en'], text)
        self.assertEqual(result['pages'][0]['blocks'][0]['protected_spans'][0]['region_id'], 'new-outer')

    def test_outer_table_wins_independently_of_detector_order(self):
        outer = Candidate((50, 100, 550, 160), [['system', 'mAP', 'car', 'tv'], ['baseline', '85.6', '89.9', '86.8']])
        inner = Candidate((150, 100, 300, 160), [['mAP', 'car'], ['85.6', '89.9']])
        for candidates in ([inner, outer], [outer, inner]):
            with self.subTest(inner_first=candidates[0] is inner):
                regions, diagnostics = detect_regions(Page(candidates), [], 'synthetic', 0)
                self.assertEqual(diagnostics, [])
                self.assertEqual(len(regions), 1)
                self.assertEqual(regions[0]['_raw_box'], list(outer.bbox))
                self.assertEqual(regions[0]['rows'], outer.values)

    def test_nonoverlapping_tables_are_not_dropped_by_size_preference(self):
        one = Candidate((50, 100, 550, 160), [['name', 'score'], ['Alpha', '85.6']])
        two = Candidate((100, 200, 300, 250), [['name', 'score'], ['Beta', '86.8']])
        regions, diagnostics = detect_regions(Page([two, one]), [], 'synthetic', 0)
        self.assertEqual(diagnostics, [])
        self.assertEqual(len(regions), 2)

    def test_real_wide_tables_retain_all_24_columns_and_ambiguous_header_is_gated(self):
        path = Path(__file__).resolve().parents[1] / 'data/parser-qa/reading-order/resnet-1512.03385.pdf'
        if not path.is_file(): self.skipTest('Optional pinned public PDF unavailable')
        page = extract_document(path, page_range=(11, 11))['pages'][0]
        tables = [r for r in page['scientific_regions'] if r['kind'] == 'table']
        wide = [r for r in tables if r['rows'][0][0] == 'system']
        self.assertEqual(len(wide), 2)
        for table in wide:
            self.assertEqual(len(table['rows']), 4)
            self.assertTrue(all(len(row) == 24 for row in table['rows']))
            self.assertEqual(table['rows'][0][-1], 'tv')
            self.assertTrue(table['cell_translation_allowed'])
        self.assertEqual(wide[0]['rows'][-1][3], '85.6')
        self.assertEqual(wide[0]['rows'][-1][-1], '86.8')
        self.assertEqual(wide[1]['rows'][-1][3], '83.8')
        self.assertEqual(wide[1]['rows'][-1][-1], '80.0')
        import pdfplumber
        from paper_reader.pdf_text import numeric_script_words
        with pdfplumber.open(path) as pdf:
            original = pdf.pages[10]
            words = numeric_script_words(original.extract_words(x_tolerance=2, y_tolerance=3, return_chars=True))
            for table in wide:
                box = [x * size for x, size in zip(table['box'], [original.width, original.height] * 2)]
                inside = [w for w in words if box[0] <= (w['x0'] + w['x1']) / 2 <= box[2]
                          and box[1] <= (w['top'] + w['bottom']) / 2 <= box[3]]
                original_rows = [[w['text'] for w in line] for line in word_lines(inside)]
                actual_rows = [(' '.join(c or '' for c in row)).split() for row in table['rows']]
                self.assertEqual(actual_rows, original_rows)
        merged = next(r for r in tables if r['rows'][0][0] == 'training data')
        self.assertFalse(merged['cell_translation_allowed'])
        self.assertTrue(merged['quality_flags'])


if __name__ == '__main__': unittest.main()
