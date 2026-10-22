"""Font bounding boxes are not the baseline of upright inline math glyphs."""
import copy
import unittest

from paper_reader.pdf_text import numeric_script_words
from paper_reader.reader import word_lines


def word(text, x, top, baseline, size=10):
    chars = [{'text': char, 'x0': x + index * 5, 'x1': x + (index + 1) * 5,
              'top': top, 'bottom': top + size, 'size': size, 'upright': True,
              'matrix': (1, 0, 0, 1, x + index * 5, baseline)} for index, char in enumerate(text)]
    return {'text': text, 'x0': x, 'x1': x + len(text) * 5,
            'top': top, 'bottom': top + size, 'chars': chars}


class InlineMathOrderTest(unittest.TestCase):
    def test_fancy_h_and_parentheses_with_same_baseline_stay_in_source_order(self):
        # Real ResNet page 2: H box starts 7.07pt lower than (x), same text matrix baseline.
        words = [word('as', 125, 267.03, 517.16), word('H', 145.39, 274.44, 517.16),
                 word('(x),', 153.90, 267.03, 517.16), word('we', 180, 267.03, 517.16)]
        before = copy.deepcopy(words)
        rows = word_lines(numeric_script_words(words))
        self.assertEqual([[entry['text'] for entry in row] for row in rows], [['as', 'H', '(x),', 'we']])
        self.assertEqual(words, before)

    def test_equation_f_and_h_are_not_emitted_below_the_operators(self):
        words = [word('F', 166.69, 286.40, 505.21), word('(x)', 174.84, 279.32, 505.21),
                 word(':=', 191.41, 279.32, 505.21), word('H', 204.69, 286.40, 505.21),
                 word('(x)', 213.20, 279.32, 505.21), word('−x', 237, 279.32, 505.21)]
        rows = word_lines(numeric_script_words(words))
        self.assertEqual(''.join(entry['text'] for entry in rows[0]), 'F(x):=H(x)−x')
        self.assertEqual(sum(len(row) for row in rows), len(words))

    def test_detached_numeric_script_keeps_its_proven_left_base_baseline(self):
        words = [word('c', 50, 100, 680, size=16), word('2', 59, 90, 688, size=9)]
        rows = word_lines(numeric_script_words(words))
        self.assertEqual([[entry['text'] for entry in row] for row in rows], [['c', '²']])

    def test_missing_or_rotated_matrix_uses_existing_top_fallback_for_the_group(self):
        for change in ('missing', 'rotated'):
            words = [word('a', 50, 100, 680), word('b', 70, 100, 650)]
            if change == 'missing':
                words[1]['chars'][0].pop('matrix')
            else:
                words[1]['chars'][0]['matrix'] = (0, 1, -1, 0, 70, 650)
            with self.subTest(change=change):
                rows = word_lines(numeric_script_words(words))
                self.assertEqual([[entry['text'] for entry in row] for row in rows], [['a', 'b']])

    def test_distinct_matrix_baselines_remain_distinct_even_with_overlapping_boxes(self):
        words = [word('First', 50, 100, 680), word('Second', 50, 102, 668)]
        rows = word_lines(numeric_script_words(words))
        self.assertEqual([[entry['text'] for entry in row] for row in rows], [['First'], ['Second']])

    def test_expanded_ligature_keeps_geometry_without_rewriting_its_text(self):
        ligature = word('ﬁt', 110, 267.03, 517.16)
        ligature['text'] = 'fit'  # pdfplumber expands fi without changing the source glyph list.
        words = [ligature, word('H', 145.39, 274.44, 517.16), word('(x)', 153.90, 267.03, 517.16)]
        before = copy.deepcopy(words)
        rows = word_lines(numeric_script_words(words))
        self.assertEqual([[entry['text'] for entry in row] for row in rows], [['fit', 'H', '(x)']])
        self.assertEqual(words, before)


if __name__ == '__main__':
    unittest.main()
