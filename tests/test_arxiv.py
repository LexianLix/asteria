import unittest

from paper_reader.sources.arxiv import _normalise_id, build_query
from paper_reader.sources.openalex import _abstract_from_inverted_index, _doi_value


class ArxivHelpersTest(unittest.TestCase):
    def test_normalise_id_removes_version(self):
        self.assertEqual(_normalise_id("http://arxiv.org/abs/2401.01234v3"), "2401.01234")

    def test_query_contains_categories_and_keywords(self):
        query = build_query(["cs.AI", "cs.CL"], ["large language model"])
        self.assertIn("cat:cs.AI", query)
        self.assertIn('all:"large language model"', query)

    def test_openalex_abstract_and_doi_helpers(self):
        self.assertEqual(_abstract_from_inverted_index({"hello": [1], "world": [0]}), "world hello")
        self.assertEqual(_doi_value("https://doi.org/10.1234/example"), "10.1234/example")


if __name__ == "__main__":
    unittest.main()
