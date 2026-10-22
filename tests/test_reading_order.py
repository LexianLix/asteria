import unittest
from pathlib import Path
from unittest.mock import patch

from paper_reader.reader import extract_document


def word(text, x0, x1, top, height=10):
    return {'text': text, 'x0': x0, 'x1': x1, 'top': top, 'bottom': top + height}


class Page:
    width, height = 612, 792

    def __init__(self, words): self.words = words
    def filter(self, predicate): return self
    def extract_words(self, **kwargs): return self.words


class PDF:
    def __init__(self, words): self.pages = [Page(words)]
    def __enter__(self): return self
    def __exit__(self, *args): pass


def extract(words):
    with patch('paper_reader.reader.file_hash', return_value='a' * 64), \
         patch('paper_reader.reader.pdfplumber.open', return_value=PDF(words)), \
         patch('paper_reader.reader.detect_regions', return_value=([], [])):
        return extract_document(Path('unused.pdf'))['pages'][0]


def body(top=250, count=14):
    result = []
    for index in range(count):
        y = top + index * 14
        for label, start in [('L', 50), ('R', 309)]:
            for k, (a, b) in enumerate([(0, 45), (50, 95), (100, 150), (155, 237)]):
                result.append(word(f'{label}{index:02}-{k}', start+a, start+b, y))
    return result


class ReadingOrderTest(unittest.TestCase):
    def test_full_width_caption_without_a_gutter_crossing_word(self):
        words = body(100) + [word('Figure', 50, 83, 330), word('4.', 87, 95, 330),
            word('Caption', 100, 200, 330), word('Left', 280, 296, 330),
            word('Right', 302, 345, 330), word('text', 350, 550, 330)] + body(400)
        blocks = [b['en'] for b in extract(words)['blocks']]
        caption = next(text for text in blocks if 'Figure 4.' in text)
        self.assertIn('Left Right text', caption)
        self.assertNotIn('L00-', caption)
        self.assertCountEqual(' '.join(blocks).split(), [w['text'] for w in words])

    def test_wide_caption_continuations_and_short_tail_stay_together(self):
        words = body(100) + [word('Figure', 50, 83, 330), word('4.', 87, 95, 330),
            word('Caption', 100, 200, 330), word('Left', 280, 296, 330),
            word('Right', 302, 345, 330), word('text', 350, 550, 330),
            word('continued', 50, 220, 344), word('to', 280, 296, 344),
            word('other', 302, 345, 344), word('side', 350, 550, 344),
            word('last', 50, 70, 358), word('sentence.', 75, 140, 358)] + body(400)
        blocks = [b['en'] for b in extract(words)['blocks']]
        caption = next(text for text in blocks if 'Figure 4.' in text)
        self.assertIn('continued to other side last sentence.', caption)
        self.assertNotIn('L00-', caption)
        self.assertCountEqual(' '.join(blocks).split(), [w['text'] for w in words])

    def test_caption_ending_does_not_absorb_following_left_paragraph(self):
        words = body(100) + [word('Figure', 50, 83, 330), word('4.', 87, 95, 330),
            word('Caption', 100, 200, 330), word('Left', 280, 296, 330),
            word('Right', 302, 345, 330), word('ends.', 350, 550, 330),
            word('Following', 50, 100, 344), word('paragraph', 105, 160, 344)] + body(400)
        blocks = [b['en'] for b in extract(words)['blocks']]
        caption = next(text for text in blocks if 'Figure 4.' in text)
        self.assertNotIn('Following', caption)
        self.assertCountEqual(' '.join(blocks).split(), [w['text'] for w in words])

    def test_small_raised_footnote_starts_separate_block(self):
        words = [word('y=F(x)+x.', 50, 120, 680),
            word('Here', 125, 150, 680), word('x', 155, 162, 680),
            word('and', 165, 182, 680), word('y', 185, 192, 680),
            word('denotes', 50, 90, 694),
            word('²Thishypothesis,however,isstillanopenquestion.', 50, 230, 706, 8),
            word('See[28].', 235, 270, 706, 8)]
        blocks = [b['en'] for b in extract(words)['blocks']]
        note = next(text for text in blocks if 'Thishypothesis' in text)
        self.assertNotIn('denotes', note)
        self.assertNotIn('Here', note)
        self.assertCountEqual(' '.join(blocks).split(), [w['text'] for w in words])

    def test_small_figure_labels_do_not_shift_body_gutter(self):
        labels = [word(f'conv{i}', 240, 252, 100 + i * 6, 6) for i in range(45)]
        right = [w for w in body(400) if w['text'].startswith('R')]
        caption = []
        for row in range(4):
            for col, (a, b) in enumerate([(50, 95), (100, 145), (150, 200), (205, 287)]):
                caption.append(word('Figure3' if row == col == 0 else f'caption{row}-{col}', a, b, 400 + row * 14, 9))
        words = labels + right + caption
        blocks = [b['en'] for b in extract(words)['blocks']]
        figure = next(text for text in blocks if 'Figure3' in text)
        self.assertNotIn('R00-0', figure)
        self.assertIn('caption3-3', figure)
        self.assertCountEqual(' '.join(blocks).split(), [w['text'] for w in words])

    def test_right_figure_labels_do_not_shift_body_gutter(self):
        labels = [word(f'conv{i}', 360, 372, 100 + i * 6, 6) for i in range(45)]
        left = [w for w in body(400) if w['text'].startswith('L')]
        caption = []
        for row in range(4):
            for col, (a, b) in enumerate([(309, 354), (359, 404), (409, 459), (464, 546)]):
                caption.append(word('Figure3' if row == col == 0 else f'caption{row}-{col}', a, b, 400 + row * 14, 9))
        words = labels + left + caption
        blocks = [b['en'] for b in extract(words)['blocks']]
        figure = next(text for text in blocks if 'Figure3' in text)
        self.assertNotIn('L00-0', figure)
        self.assertIn('caption3-3', figure)
        self.assertCountEqual(' '.join(blocks).split(), [w['text'] for w in words])

    def test_full_width_title_authors_before_columns_and_footer_last(self):
        title = [word(t, a, b, 105, 14) for t, a, b in [
            ('Deep', 153, 184), ('Residual', 188, 241), ('Learning', 245, 301.5),
            ('for', 305, 323), ('Image', 327, 365), ('Recognition', 369, 442)]]
        authors = [word(t, a, b, 152, 12) for t, a, b in [
            ('Kaiming', 136, 178), ('He', 181, 195), ('Xiangyu', 222, 263),
            ('Zhang', 266, 297), ('Shaoqing', 324, 369), ('Ren', 372, 391),
            ('Jian', 418, 437), ('Sun', 440, 459)]]
        words = title + authors + [word('Microsoft', 249, 296, 169),
            word('Research', 299, 343, 169)] + body() + [word('1', 296, 301, 765)]
        page = extract(words)
        texts = [b['en'] for b in page['blocks']]
        self.assertIn('Deep Residual Learning for Image Recognition', texts)
        self.assertTrue(any('Kaiming He Xiangyu Zhang Shaoqing Ren Jian Sun' in text for text in texts))
        flat = ' '.join(texts).split()
        self.assertLess(flat.index('Sun'), flat.index('L00-0'))
        self.assertLess(flat.index('L13-3'), flat.index('R00-0'))
        self.assertEqual(flat[-1], '1')
        self.assertCountEqual(flat, [w['text'] for w in words])

    def test_same_baseline_columns_are_not_interleaved(self):
        words = body()
        flat = ' '.join(b['en'] for b in extract(words)['blocks']).split()
        self.assertLess(flat.index('L13-3'), flat.index('R00-0'))
        self.assertCountEqual(flat, [w['text'] for w in words])

    def test_middle_full_width_band_preserves_column_order_on_both_sides(self):
        words = body(100) + [word('Full-width', 245, 310, 330),
            word('caption', 314, 365, 330)] + body(400)
        page = extract(words)
        blocks = [b['en'] for b in page['blocks']]
        self.assertIn('Full-width caption', blocks)
        before, after = ' '.join(blocks).split('Full-width caption')
        for text in (before, after):
            self.assertLess(text.index('L13-3'), text.index('R00-0'))
        self.assertCountEqual(' '.join(blocks).split(), [w['text'] for w in words])

    def test_single_column_author_baselines_keep_horizontal_order(self):
        words = [word('Llion', 100, 130, 100.9), word('Jones*', 134, 180, 99.5),
            word('Aidan', 220, 251, 100.9), word('Gomez*', 255, 309, 99.5)]
        self.assertEqual(extract(words)['blocks'][0]['en'], 'Llion Jones* Aidan Gomez*')


if __name__ == '__main__': unittest.main()
