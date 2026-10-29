"""Pinned real small-font chart labels must not interleave adjacent baselines."""
from pathlib import Path
from collections import Counter
import unittest
from unittest.mock import patch
from pdfplumber.utils import extract_words
from paper_reader.reader import extract_document, file_hash, refine_text_words
from public_samples import resnet_pdf

PDF = resnet_pdf()
DIGEST = '1e0651b6810ecba34a3dbc5b5b0209226f889004607c1f203540a48d64e5a93a'


class SmallLegendTextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PDF.exists():
            raise unittest.SkipTest('Optional pinned public PDF is not present; synthetic extraction regression still runs')
        if file_hash(PDF) != DIGEST:
            raise ValueError('Pinned public PDF changed')
        cls.page = extract_document(PDF, page_range=(8, 8))['pages'][0]
        cls.text = ' '.join(b['en'] for b in cls.page['blocks'])

    def test_no_interleaved_or_letter_spaced_plain_legend(self):
        self.assertNotRegex(self.text, r'\bp (?:p )?l (?:l )?a (?:a )?i (?:i )?n\b')

    def test_three_distinct_chart_legends_preserve_complete_model_names(self):
        self.assertEqual(self.text.count('plain-20'), 3)
        self.assertEqual(self.text.count('plain-56'), 3)

    def test_tightening_does_not_reorder_protected_table_math(self):
        page = extract_document(PDF, page_range=(5, 5))['pages'][0]
        block = next(b for b in page['blocks'] if 'maxpool' in b['en'])
        protected = ' '.join(block['en'][s['start']:s['end']] for s in block['protected_spans'])
        self.assertIn('3 × 3maxpool,stride2', protected)
        self.assertNotIn('\uf8ee×', protected)


class SmallLegendSyntheticTest(unittest.TestCase):
    def test_scientific_word_is_retained_exactly_and_bad_provenance_is_rejected(self):
        chars = [{'text': c, 'x0': 100 + i * 2, 'x1': 102 + i * 2,
            'top': 100, 'bottom': 104.6, 'size': 4.6, 'doctop': 100,
            'upright': True, 'matrix': (4.6, 0, 0, 4.6, 100 + i * 2, 688.4)} for i, c in enumerate('3×3')]
        word = {'text': '3×3', 'x0': 100, 'x1': 106, 'top': 100, 'bottom': 104.6,
            'chars': chars, '_line_baseline': 688.4, 'numeric_scripts': []}
        class Page: pass
        page = Page(); page.chars = list(chars)
        regions = [{'id': 'synthetic-table', 'kind': 'table', '_raw_box': [90, 90, 120, 120]}]
        result = refine_text_words(page, [word], regions)
        self.assertEqual(result, [word])
        self.assertIs(result[0], word)
        page.chars = chars[:-1]
        with self.assertRaisesRegex(ValueError, '字形来源不一致'):
            refine_text_words(page, [word], regions)

    def test_axis_tick_between_legend_baselines_cannot_interleave_letters(self):
        chars = []
        for text, x, top in [('plain-20', 238, 215), ('3', 68, 218), ('plain-56', 238, 221)]:
            for offset, letter in enumerate(text):
                chars.append({'object_type': 'char', 'text': letter, 'x0': x + offset * 2,
                    'x1': x + offset * 2 + 2, 'top': top, 'bottom': top + 4.6,
                    'doctop': top, 'upright': True, 'height': 4.6, 'width': 2,
                    'size': 4.6, 'fontname': 'synthetic', 'matrix': (4.6, 0, 0, 4.6, x + offset * 2, 792 - top - 3.6)})
        class Page:
            width, height = 612, 792
            def filter(self, predicate): return self
            def extract_words(self, **kwargs): return extract_words(chars, **kwargs)
        class Document:
            pages = [Page()]
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with patch('paper_reader.reader.file_hash', return_value='a' * 64), \
                patch('paper_reader.reader.pdfplumber.open', return_value=Document()), \
                patch('paper_reader.reader.detect_regions', return_value=([], [])):
            result = extract_document(Path('synthetic-unused.pdf'))['pages'][0]
        text = ' '.join(b['en'] for b in result['blocks'])
        self.assertIn('plain-20', text)
        self.assertIn('plain-56', text)
        self.assertEqual(Counter(text.replace(' ', '')), Counter(c['text'] for c in chars))


if __name__ == '__main__': unittest.main()
