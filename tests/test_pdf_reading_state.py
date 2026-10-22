"""Actual local API persistence and validation of source-PDF reading position."""
from dataclasses import replace
import math
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from paper_reader.desktop_api import install_desktop, ReaderState
from paper_reader.settings import Settings
from paper_reader.storage import Database


class PdfReadingStateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.settings = replace(Settings.from_env(), root_dir=root, data_dir=root / 'data',
            db_path=root / 'data/database.sqlite', papers_dir=root / 'data/papers', topics_path=root / 'topics.json')
        self.settings.ensure_dirs(); self.db = Database(self.settings.db_path)
        self.pid, _ = self.db.upsert_paper({'source': 'reading-qa', 'source_id': 'position', 'title': 'QA only'})
        self.app = FastAPI(); self.service = install_desktop(self.app, self.settings, self.db)
        self.client = TestClient(self.app)
        self.headers = {'X-Desktop-Token': self.client.get('/desktop/bootstrap').json()['token']}
        self.path = f'/desktop/papers/{self.pid}/state'

    def tearDown(self):
        self.service.shutdown(); self.client.close(); self.db.close(); self.temp.cleanup()

    def test_position_zoom_and_mode_survive_actual_database_reopen(self):
        payload = {'last_page': 15, 'pdf_offset': .37, 'pdf_zoom': 1.15, 'pdf_continuous': True}
        response = self.client.patch(self.path, headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200)
        self.service.shutdown(); self.client.close(); self.db.close()
        self.db = Database(self.settings.db_path)
        saved = self.db.reader_get(self.pid, 'state')
        for key, value in payload.items(): self.assertEqual(saved[key], value)

    def test_partial_position_update_retains_notes_metadata_and_done_status(self):
        self.db.reader_put(self.pid, 'state', {'status': 'done', 'starred': True, 'tags': ['keep'], 'folder': 'keep'})
        response = self.client.patch(self.path, headers=self.headers, json={'last_page': 9, 'pdf_offset': .8})
        self.assertEqual(response.status_code, 200)
        for key, value in {'status': 'done', 'starred': True, 'tags': ['keep'], 'folder': 'keep'}.items():
            self.assertEqual(response.json()[key], value)

    def test_out_of_range_values_do_not_change_existing_state(self):
        self.db.reader_put(self.pid, 'state', {'last_page': 1, 'pdf_offset': .25})
        before = self.db.reader_get(self.pid, 'state')
        for payload in ({'pdf_offset': -.01}, {'pdf_offset': 1.01}, {'pdf_zoom': .49}, {'pdf_zoom': 2.51}):
            self.assertEqual(self.client.patch(self.path, headers=self.headers, json=payload).status_code, 422)
            self.assertEqual(self.db.reader_get(self.pid, 'state'), before)

    def test_non_finite_positions_are_rejected_by_actual_request_model(self):
        for field in ('pdf_offset', 'pdf_zoom'):
            for value in (math.nan, math.inf, -math.inf):
                with self.assertRaises(ValidationError): ReaderState.model_validate({field: value})

    def test_position_write_still_requires_desktop_token(self):
        self.assertEqual(self.client.patch(self.path, json={'pdf_offset': .5}).status_code, 403)
        self.assertNotIn('pdf_offset', self.db.reader_get(self.pid, 'state'))

    def test_automatic_position_marks_unread_as_reading_without_sending_stale_status(self):
        self.db.reader_put(self.pid, 'state', {'status': 'unread'})
        response = self.client.patch(self.path, headers=self.headers, json={'last_page': 1, 'pdf_offset': .4})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'reading')
        # An earlier queued position arriving after an explicit completion must
        # observe the latest DB status, not downgrade the paper back to reading.
        self.client.patch(self.path, headers=self.headers, json={'status': 'done'})
        response = self.client.patch(self.path, headers=self.headers, json={'last_page': 2, 'pdf_offset': .2})
        self.assertEqual(response.json()['status'], 'done')

    def test_explicit_manual_status_can_still_change_done_with_position(self):
        self.db.reader_put(self.pid, 'state', {'status': 'done'})
        response = self.client.patch(self.path, headers=self.headers, json={'last_page': 2, 'status': 'unread'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'unread')


if __name__ == '__main__': unittest.main()
