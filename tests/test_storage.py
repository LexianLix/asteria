import tempfile
import unittest
from pathlib import Path

from paper_reader.storage import Database


class StorageTest(unittest.TestCase):
    def test_upsert_is_idempotent_and_decodes_json(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "db.sqlite")
            paper = {
                "source": "test",
                "source_id": "p-1",
                "title": "A paper",
                "abstract": "Abstract",
                "authors": ["A"],
                "categories": ["cs.AI"],
                "ai_tags": ["测试"],
            }
            first_id, first_inserted = db.upsert_paper(paper)
            second_id, second_inserted = db.upsert_paper(paper)
            self.assertEqual(first_id, second_id)
            self.assertTrue(first_inserted)
            self.assertFalse(second_inserted)
            result = db.get_paper(first_id)
            self.assertEqual(result["authors"], ["A"])
            self.assertEqual(result["ai_tags"], ["测试"])
            db.close()

    def test_annotation_path_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "db.sqlite")
            paper_id, _ = db.upsert_paper({"source": "test", "source_id": "p-1", "title": "A"})
            annotation = db.add_annotation(
                paper_id,
                {
                    "page_number": 0,
                    "annotation_type": "ink",
                    "path": [{"x": 0.1, "y": 0.2}],
                },
            )
            self.assertEqual(annotation["path"][0]["x"], 0.1)
            db.close()


if __name__ == "__main__":
    unittest.main()
