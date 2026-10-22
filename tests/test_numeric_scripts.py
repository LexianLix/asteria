import unittest
from pathlib import Path
from unittest.mock import patch

from paper_reader.reader import extract_document, quality_warnings
from paper_reader.scientific import translation_input, restore_protected
from paper_reader.model_client import ModelError
from paper_reader.pdf_text import numeric_script_words, numeric_table_rows, script_char_changes
from types import SimpleNamespace
from test_reading_order import PDF


def char(text, x, top, size):
    return {'text': text, 'x0': x, 'x1': x + size * .5, 'top': top,
            'bottom': top + size, 'size': size, 'upright': True}


def word(chars):
    return {'text': ''.join(c['text'] for c in chars), 'x0': min(c['x0'] for c in chars),
            'x1': max(c['x1'] for c in chars), 'top': min(c['top'] for c in chars),
            'bottom': max(c['bottom'] for c in chars), 'chars': chars}


def extract(words):
    with patch('paper_reader.reader.file_hash', return_value='b' * 64), \
         patch('paper_reader.reader.pdfplumber.open', return_value=PDF(words)), \
         patch('paper_reader.reader.detect_regions', return_value=([], [])):
        return extract_document(Path('unused.pdf'))['pages'][0]


class NumericScriptsTest(unittest.TestCase):
    def test_raised_small_digit_preserves_a_squared_number(self):
        chars = [char(t, 50 + i * 4, 100, 8) for i, t in enumerate('256')]
        chars.append(char('2', 62, 98.8, 6))
        block = extract([word(chars)])['blocks'][0]
        self.assertEqual(block['en'], '256²')
        self.assertEqual(block['numeric_scripts'][0]['original'], '2')
        self.assertEqual(block['numeric_scripts'][0]['replacement'], '²')
        self.assertEqual(chars[-1]['text'], '2', 'Do not mutate source glyph objects')

    def test_negative_exponent_and_numeric_subscript(self):
        exponent = [char('1', 50, 100, 8), char('0', 54, 100, 8),
                    char('−', 58, 98.8, 6), char('4', 61, 98.8, 6)]
        subscript = [char('β', 90, 100, 8), char('1', 94, 104, 6)]
        self.assertEqual(extract([word(exponent), word(subscript)])['blocks'][0]['en'], '10⁻⁴ β₁')

    def test_small_digit_on_baseline_and_distant_footnote_are_not_rewritten(self):
        same_baseline = [char('2', 50, 100, 8), char('5', 54, 100, 8),
                         char('6', 58, 100, 8), char('2', 62, 102, 6)]
        words = [word(same_baseline), word([char('9', 400, 98.8, 6)])]
        self.assertEqual(extract(words)['blocks'][0]['en'], '2562 9')

    def test_detached_but_adjacent_exponent_uses_the_base_word(self):
        words = [word([char('c', 50, 100, 16)]), word([char('2', 59, 97.5, 9)])]
        self.assertEqual(extract(words)['blocks'][0]['en'], 'c ²')

    def test_translation_warning_detects_removed_or_changed_script(self):
        for translated in ['Resolution 256', 'Resolution 256³']:
            self.assertTrue(any('上下标' in w for w in quality_warnings('Resolution 256²', translated)))
        self.assertEqual(quality_warnings('Resolution 256²', '分辨率 256²'), [])

    def test_inline_script_is_kept_exactly_without_sending_flattened_number(self):
        words = [word([char(t, 50 + i * 4, 100, 8) for i, t in enumerate('Size')]),
                 word([char('2', 80, 100, 8), char('5', 84, 100, 8),
                       char('6', 88, 100, 8), char('2', 92, 98.8, 6)])]
        page = extract(words)
        source, replacements, local = translation_input(page)
        self.assertEqual(len(source), 1)
        key = source[0]['id']
        self.assertNotIn('2562', source[0]['en'])
        self.assertNotIn('256²', source[0]['en'])
        token = next(iter(replacements[key]))
        self.assertEqual(restore_protected({key: '大小 ' + token}, replacements, local)[key], '大小 256²')
        with self.assertRaises(ModelError):
            restore_protected({key: '大小 2562'}, replacements, local)

    def test_overlapping_formula_and_script_ranges_produce_one_keep_marker(self):
        page = {'blocks': [{'id': 'formula', 'en': 'Equation x² = 4',
            'protected_spans': [{'start': 9, 'end': 15}], 'numeric_spans': [{'start': 9, 'end': 11}]}]}
        source, replacements, _ = translation_input(page)
        self.assertEqual(len(replacements['formula']), 1)
        self.assertEqual(source[0]['en'].count('[[PR_KEEP_'), 1)

    def test_invalid_nested_range_is_not_hidden_by_overlap_merging(self):
        page = {'blocks': [{'id': 'bad', 'en': 'x²', 'protected_spans': [{'start': 0, 'end': 2}],
                            'numeric_spans': [{'start': 1, 'end': 0}]}]}
        with self.assertRaises(ValueError): translation_input(page)

    def test_pdf_matrix_distinguishes_lowered_index_from_font_metrics(self):
        main = char('β', 50, 162.58, 7.97)
        main['matrix'] = (1, 0, 0, 1, 50, 620)
        lowered = char('1', 54, 165.29, 5.98)
        lowered['matrix'] = (1, 0, 0, 1, 54, 618.8)
        self.assertEqual(extract([word([main, lowered])])['blocks'][0]['en'], 'β₁')
        same_baseline = dict(lowered, matrix=(1, 0, 0, 1, 54, 620))
        self.assertEqual(extract([word([main, same_baseline])])['blocks'][0]['en'], 'β1')

    def test_existing_table_cell_uses_the_same_geometric_script_conversion(self):
        chars = [char(t, 100 + i * 4, 100, 8) for i, t in enumerate('256')]
        chars.append(char('2', 112, 98.8, 6))
        for c in chars:
            c.update(doctop=c['top'], fontname='QA', height=c['size'], width=c['x1']-c['x0'])
        words = numeric_script_words([word(chars)])
        table = SimpleNamespace(rows=[SimpleNamespace(cells=[(40, 90, 95, 130), (98, 90, 130, 130)])])
        rows = [['Resolution', '2562']]
        changed = numeric_table_rows(rows, table, SimpleNamespace(chars=chars), script_char_changes(words))
        self.assertEqual(changed, [['Resolution', '256²']])
        self.assertEqual(rows, [['Resolution', '2562']])

    def test_textual_table_cell_protects_exponent_and_parameter_index(self):
        page = {'blocks': [], 'scientific_regions': [{'id': 'table', 'kind': 'table',
            'rows': [['Resolution 256²–1024²', 'Parameter β₁ and rate 10⁻⁴']]}]}
        source, replacements, local = translation_input(page)
        for item in source:
            self.assertFalse(any(c in item['en'] for c in '²₁⁻⁴'))
        result = restore_protected({s['id']: s['en'] for s in source}, replacements, local)
        self.assertEqual(result['table-r0c0'], page['scientific_regions'][0]['rows'][0][0])
        self.assertEqual(result['table-r0c1'], page['scientific_regions'][0]['rows'][0][1])

    def test_manually_corrected_script_without_geometry_is_still_preserved(self):
        page = {'blocks': [{'id': 'reviewed', 'en': 'Resolution 256²–1024² and index β₁'}]}
        source, replacements, local = translation_input(page)
        self.assertFalse(any(c in source[0]['en'] for c in '²₁'))
        result = restore_protected({'reviewed': source[0]['en']}, replacements, local)
        self.assertEqual(result['reviewed'], page['blocks'][0]['en'])


if __name__ == '__main__': unittest.main()
