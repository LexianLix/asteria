"""Caption boundaries must separate adjacent horizontal-rule tables."""
import io
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from reportlab.pdfgen import canvas
from paper_reader.reader import extract_document
from paper_reader.scientific import preserve_regions, translation_input
from public_samples import resnet_pdf


def adjacent_tables_pdf(*, captions_above=False):
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(595, 842))
    pdf.setFont('Times-Roman', 12)
    for top, number in [(740, 1), (570, 2)]:
        for y in (top, top - 24, top - 54, top - 84, top - 114):
            pdf.line(60, y, 480, y)
        for row, cells in enumerate([
                ('Method', 'Error'), ('Alpha', '8.43'), ('Beta', '7.89'), ('Gamma', '5.71')]):
            for x, text in zip((68, 320), cells):
                pdf.drawString(x, top - 17 - row * 30, text)
        pdf.drawString(60, top + 20 if captions_above else top - 136,
                       f'Table {number}. Error rates of single-model results on the validation set.')
    pdf.save()
    return stream.getvalue()


class AdjacentTablesTest(unittest.TestCase):
    def test_captions_above_tables_also_separate_enclosures(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'above.pdf'
            path.write_bytes(adjacent_tables_pdf(captions_above=True))
            page = extract_document(path)['pages'][0]
        tables = [r for r in page['scientific_regions'] if r['kind'] == 'table']
        self.assertEqual(len(tables), 2)
        self.assertTrue(all('Table ' not in r['text'] and 'Gamma' in r['text'] for r in tables))

    def test_real_pipeline_keeps_each_caption_outside_both_tables(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'adjacent.pdf'
            path.write_bytes(adjacent_tables_pdf())
            page = extract_document(path)['pages'][0]
        tables = [r for r in page['scientific_regions'] if r['kind'] == 'table']
        self.assertEqual(len(tables), 2)
        self.assertTrue(all('Table ' not in r['text'] for r in tables))
        self.assertTrue(all('Gamma' in r['text'] and '5.71' in r['text'] for r in tables))
        source, _, _ = translation_input(page)
        self.assertNotIn('8.43', str(source))
        self.assertNotIn('5.71', str(source))
        captions = [b for b in page['blocks'] if b['en'].startswith('Table ')]
        self.assertEqual(len(captions), 2)
        for block in captions:
            self.assertEqual(block['protected_spans'], [])
            self.assertIn({'id': block['id'], 'en': block['en']}, source)

    def test_fixed_resnet_caption_exposed_without_changing_old_cache(self):
        pdf = resnet_pdf()
        if not pdf.is_file():
            self.skipTest('Optional public ResNet regression PDF not downloaded')
        fresh = extract_document(pdf, page_range=(6, 6))
        key = '1e0651b6810e-p5-66551fe3f67a'
        caption = next(b for b in fresh['pages'][0]['blocks'] if b['id'] == key)
        source, _, _ = translation_input(fresh['pages'][0])
        self.assertIn({'id': key, 'en': caption['en']}, source)
        self.assertEqual(caption['protected_spans'], [])
        tables = [r for r in fresh['pages'][0]['scientific_regions'] if r['kind'] == 'table']
        self.assertEqual(len(tables), 3)
        self.assertTrue(all('Table 4.' not in r['text'] for r in tables))
        old = deepcopy(fresh)
        old_caption = next(b for b in old['pages'][0]['blocks'] if b['id'] == key)
        old_caption['protected_spans'] = [{'start': 0, 'end': len(caption['en']),
            'kind': 'table', 'region_id': 'previous-oversized-region'}]
        old['pages'][0]['translated'][key] = caption['en']
        before_translation = deepcopy(old['pages'][0]['translated'])
        result = preserve_regions(old, fresh)
        self.assertEqual(result['pages'][0]['translated'], before_translation)
        self.assertEqual(next(b for b in result['pages'][0]['blocks'] if b['id'] == key)['protected_spans'], [])


if __name__ == '__main__':
    unittest.main()
