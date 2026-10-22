import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from paper_reader.settings import Settings
from paper_reader.sync import download_pdf


class DownloadTest(unittest.TestCase):
    def test_non_pdf_is_not_marked_as_downloaded(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = replace(Settings.from_env(), papers_dir=Path(folder))
            paper = {"source": "test", "source_id": "123", "pdf_url": "https://example.com/paper.pdf"}
            with patch("urllib.request.urlopen", return_value=io.BytesIO(b"<html>not a PDF</html>")):
                with self.assertRaisesRegex(RuntimeError, "不是 PDF"):
                    download_pdf(settings, paper)
            self.assertFalse(list(Path(folder).rglob("*.pdf")))
            self.assertFalse(list(Path(folder).rglob("*.part")))

    def test_download_is_atomic_and_existing_pdf_is_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = replace(Settings.from_env(), papers_dir=Path(folder))
            paper = {"source": "test", "source_id": "123", "pdf_url": "https://example.com/paper.pdf"}
            with patch("urllib.request.urlopen", return_value=io.BytesIO(b"%PDF-test-content")) as network:
                path = download_pdf(settings, paper)
                self.assertEqual(path.read_bytes(), b"%PDF-test-content")
                self.assertEqual(download_pdf(settings, paper), path)
                self.assertEqual(network.call_count, 1)
            self.assertFalse(list(Path(folder).rglob("*.part")))
