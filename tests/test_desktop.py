import io
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from paper_reader import __version__
from paper_reader.desktop_api import install_desktop
from paper_reader.reader import ReaderService, extract_document
from paper_reader.settings import Settings
from paper_reader.storage import Database


def sample_pdf():
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    for n in range(2):
        pdf.saveState()
        pdf.translate(20, 400)
        pdf.rotate(90)
        pdf.drawString(0, 0, "ROTATED-MARGIN-STAMP")
        pdf.restoreState()
        pdf.setFont("Helvetica", 16)
        pdf.drawString(60, 760, f"{n + 1} Research reading")
        pdf.setFont("Helvetica", 11)
        pdf.drawString(60, 710, "This is a test document. Numbers and citations must remain unchanged.")
        pdf.drawString(60, 692, "Local notes should survive a model failure and a restarted translation job.")
        pdf.showPage()
    pdf.save()
    return output.getvalue()


class DesktopTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = replace(Settings.from_env(), root_dir=root, data_dir=root / "data", db_path=root / "data/db.sqlite",
                                papers_dir=root / "data/papers", topics_path=root / "topics.json")
        self.settings.ensure_dirs()
        self.db = Database(self.settings.db_path)
        self.app = FastAPI()
        self.service = install_desktop(self.app, self.settings, self.db)
        self.client = TestClient(self.app)
        self.headers = {"X-Desktop-Token": self.client.get("/desktop/bootstrap").json()["token"]}
        self.paper = self.client.post("/desktop/import", headers=self.headers,
                                      files={"file": ("unit-test.pdf", sample_pdf(), "application/pdf")}).json()

    def tearDown(self):
        self.service.shutdown()
        self.client.close()
        self.db.close()
        self.temp.cleanup()

    def wait_job(self, paper_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.db.job_get(f"translate:{paper_id}")
            if job["status"] not in ("queued", "running", "cancelling"):
                return job
            time.sleep(.01)
        self.fail("translation worker did not finish")

    def test_desktop_reports_canonical_application_and_database_versions(self):
        self.assertEqual(self.client.get('/desktop/bootstrap').json()['version'], __version__)
        runtime = self.client.get('/desktop/runtime').json()
        self.assertEqual(runtime['app_version'], __version__)
        self.assertEqual(runtime['database_version'], self.db.schema_version())

    def test_import_dedup_and_reject_non_pdf(self):
        # ReportLab varies creation IDs between renders, so replay exact bytes from stored source.
        original = (self.settings.root_dir / self.paper["local_pdf_path"]).read_bytes()
        duplicate = self.client.post("/desktop/import", headers=self.headers,
                                     files={"file": ("renamed.pdf", original, "application/pdf")})
        self.assertEqual(duplicate.json()["id"], self.paper["id"])
        self.assertEqual(duplicate.json()["title"], self.paper["title"])
        bad = self.client.post("/desktop/import", headers=self.headers, files={"file": ("bad.pdf", b"not a pdf")})
        self.assertEqual(bad.status_code, 422)
        self.assertFalse(list(self.settings.papers_dir.rglob("*.upload")))

    def test_source_ids_and_translation_are_separate_from_notes(self):
        path = self.settings.root_dir / self.paper["local_pdf_path"]
        doc = extract_document(path)
        again = extract_document(path)
        self.assertEqual(len(doc["pages"]), 2)
        self.assertEqual(doc, again)
        self.assertTrue(doc["pages"][0]["blocks"])
        self.assertNotIn("ROTATED", " ".join(b["en"] for p in doc["pages"] for b in p["blocks"]))
        for page in doc["pages"]:
            for block in page["blocks"]:
                self.assertTrue(all(0 <= v <= 1 for v in block["box"]))

    def test_annotation_replay_delete_tombstone_and_validation(self):
        url = f"/desktop/papers/{self.paper['id']}/annotations"
        body = {"client_id": "test-operation-01", "page_number": 0, "annotation_type": "ink", "path": [{"x": .1, "y": .2}]}
        first = self.client.post(url, headers=self.headers, json=body)
        second = self.client.post(url, headers=self.headers, json=body)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(len(self.db.list_annotations(self.paper["id"])), 1)
        self.client.delete(url + f"/{first.json()['id']}", headers=self.headers)
        self.assertTrue(self.client.post(url, headers=self.headers, json=body).json()["deleted"])
        self.assertEqual(self.db.list_annotations(self.paper["id"]), [])
        body["path"] = [{"x": 2, "y": 0}]
        self.assertEqual(self.client.post(url, headers=self.headers, json=body).status_code, 422)

    def test_settings_do_not_return_secret_and_cross_site_writes_fail(self):
        config = {"provider": "openai", "base_url": "https://example.com/v1", "model": "test-model", "api_key": "test-secret"}
        response = self.client.put("/desktop/config", headers=self.headers, json=config)
        self.assertNotIn("api_key", response.json())
        self.assertTrue(response.json()["has_api_key"])
        config.pop("api_key")
        self.client.put("/desktop/config", headers=self.headers, json=config)
        self.assertEqual(self.service.config()["api_key"], "test-secret")
        self.assertEqual(self.client.put("/desktop/config", json=config).status_code, 403)
        self.assertEqual(self.client.put("/desktop/config", headers={**self.headers, "Origin": "https://evil.example"}, json=config).status_code, 403)
        self.assertEqual(self.client.get("/desktop/bootstrap", headers={"Origin": "https://evil.example"}).status_code, 403)
        config["base_url"] = "file:///tmp/model"
        self.assertEqual(self.client.put("/desktop/config", headers=self.headers, json=config).status_code, 422)

    def test_favorites_filter_and_export(self):
        pid = self.paper["id"]
        self.client.patch(f"/desktop/papers/{pid}/state", headers=self.headers, json={"starred": True, "last_page": 1})
        self.assertEqual(self.client.get("/desktop/papers?starred=true").json()["total"], 1)
        self.assertEqual(self.client.get("/desktop/overview").json()["starred"], 1)
        self.db.add_annotation(pid, {"page_number": 1, "annotation_type": "note", "text": "A persistent note"})
        exported = self.client.get(f"/desktop/papers/{pid}/notes.md")
        self.assertIn("A persistent note", exported.text)
        self.assertIn("第 2 页", exported.text)

    def test_translation_failure_resume_preserves_notes(self):
        pid = self.paper["id"]
        note = self.db.add_annotation(pid, {"page_number": 0, "annotation_type": "note", "text": "Keep this note"})
        calls = []
        def translator(page, cfg):
            calls.append(page["number"])
            if page["number"] == 1:
                raise ValueError("simulated model failure")
            return {b["id"]: "测试译文" for b in page["blocks"]}
        with patch.object(ReaderService, "translate_page", side_effect=translator):
            self.service.start(pid)
            job = self.wait_job(pid)
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["done"], 1)
        with patch.object(ReaderService, "translate_page", side_effect=lambda page, cfg: {b["id"]: "补译" for b in page["blocks"]}) as model:
            self.service.start(pid)
            job = self.wait_job(pid)
            self.assertEqual(model.call_count, 1)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["done"], 2)
        self.assertEqual(self.db.get_annotation(note["id"])["text"], "Keep this note")

    def test_cancel_waits_for_current_page_and_does_not_request_next(self):
        started, release = threading.Event(), threading.Event()
        pid = self.paper["id"]
        def translator(page, cfg):
            started.set()
            release.wait(3)
            return {b["id"]: "已完成的这一页" for b in page["blocks"]}
        with patch.object(ReaderService, "translate_page", side_effect=translator) as model:
            self.service.start(pid)
            self.assertTrue(started.wait(3))
            self.service.cancel(pid)
            release.set()
            job = self.wait_job(pid)
            self.assertEqual(model.call_count, 1)
        self.assertEqual(job["status"], "paused")
        self.assertEqual(job["done"], 1)

    def test_restart_requires_explicit_resume(self):
        self.db.job_put("translate:1", {"status": "running", "done": 1})
        self.db.jobs_recover()
        self.assertEqual(self.db.job_get("translate:1")["status"], "paused")

    def test_model_result_must_cover_exact_source_ids(self):
        page = {"blocks": [{"id": "p1", "en": "Some source"}]}
        cfg = self.service.config()
        payload = io.BytesIO(json.dumps({"response": '[{"id":"wrong","zh":"错位"}]'}).encode())
        with patch("paper_reader.model_client.open_model_request", return_value=payload):
            with self.assertRaisesRegex(ValueError, "ID 不一致"):
                ReaderService.translate_page(page, cfg)


if __name__ == "__main__":
    unittest.main()
