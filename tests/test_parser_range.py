import tempfile
import unittest
from pathlib import Path
from paper_reader.reader import extract_document
from scientific_fixture import scientific_pdf


class ParserRangeTest(unittest.TestCase):
    def test_subset_preserves_source_hash_numbers_and_block_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fixture.pdf'; path.write_bytes(scientific_pdf())
            full = extract_document(path); selected = extract_document(path, page_range=(2, 2))
            self.assertEqual(selected['source_hash'], full['source_hash'])
            self.assertEqual(selected['pages'], [full['pages'][1]])

    def test_invalid_range_is_rejected_not_silently_relabelled(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fixture.pdf'; path.write_bytes(scientific_pdf())
            for value in [(0, 1), (2, 1), (1, 3)]:
                with self.assertRaises(ValueError): extract_document(path, page_range=value)


if __name__ == '__main__': unittest.main()
