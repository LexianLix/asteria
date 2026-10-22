"""Only synthetic, locally protected credentials; no real auth or inference."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_reader.chatgpt_auth import ChatGPTAuth, DIRECT_SCOPE, RESOURCE
from paper_reader.classification import ClassificationService, consent_path
from paper_reader.reader import ReaderService
from paper_reader.settings import Settings
from paper_reader.storage import Database


class AuthRootIsolationTest(unittest.TestCase):
    def test_default_preserves_legacy_auth_location(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(Settings.from_env().auth_root_dir)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = replace(Settings.from_env(), root_dir=root, auth_root_dir=None,
                data_dir=root / 'data', db_path=root / 'data/db.sqlite', papers_dir=root / 'data/papers')
            cfg.ensure_dirs(); db = Database(cfg.db_path); reader = ReaderService(cfg, db)
            try:
                self.assertEqual(reader.chatgpt_auth.directory, root / '.paper-reader-private')
            finally:
                reader.shutdown(); db.close()

    def test_explicit_auth_root_is_process_local_and_empty_value_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {'PAPER_READER_AUTH_ROOT': folder}):
                self.assertEqual(Settings.from_env().auth_root_dir, Path(folder).resolve())
        for invalid in ('', '   '):
            with patch.dict(os.environ, {'PAPER_READER_AUTH_ROOT': invalid}):
                with self.assertRaises(ValueError): Settings.from_env()

    def test_shared_login_does_not_share_classification_authorization_or_copy_tokens(self):
        with tempfile.TemporaryDirectory() as folder:
            shared, isolated = Path(folder) / 'own-app-login', Path(folder) / 'qa-library'
            auth = ChatGPTAuth(shared)
            auth._save({'version': 1, 'generation': 1, 'active': 'synthetic', 'registrations': {
                'synthetic': {'client_id': 'oaiapp_synthetic', 'access_token': 'qa-not-real',
                    'scopes': [DIRECT_SCOPE, 'resource.invoke'], 'welcome_acknowledged': True}}})
            formal = consent_path(shared); formal.write_text('{"enabled":false,"sentinel":"keep"}', encoding='utf-8')
            original_auth, original_consent = auth.credentials.read_bytes(), formal.read_bytes()
            cfg = replace(Settings.from_env(), root_dir=isolated, auth_root_dir=shared,
                data_dir=isolated / 'data', db_path=isolated / 'data/db.sqlite', papers_dir=isolated / 'data/papers')
            cfg.ensure_dirs(); db = Database(cfg.db_path); reader = ReaderService(cfg, db)
            reader.classification = ClassificationService(reader)
            try:
                reader.save_config({'provider': 'chatgpt', 'base_url': RESOURCE, 'model': 'qa-saved-model',
                    'daily_enabled': False, 'auto_backup': False})
                self.assertEqual(reader.config()['chatgpt_account_id'], 'synthetic')
                preview = reader.classification.preview()
                reader.classification.authorize(enabled=True, confirmed=True, preview_id=preview['preview_id'])
                self.assertTrue(reader.classification.status()['enabled'])
                self.assertTrue(consent_path(isolated).is_file())
                self.assertEqual(formal.read_bytes(), original_consent)
                reader.save_config({'model': 'qa-changed-model'})
                self.assertFalse(reader.classification.status()['enabled'])
                self.assertEqual(formal.read_bytes(), original_consent)
                self.assertEqual(auth.credentials.read_bytes(), original_auth)
                self.assertFalse(list(isolated.rglob('chatgpt-auth.json')))
                self.assertFalse(list(isolated.rglob('chatgpt-host.json')))
                self.assertNotIn('auth_root', json.dumps(reader.config(public=True)))
            finally:
                reader.shutdown(); db.close()

    def test_auth_updates_stay_at_explicit_root_not_library(self):
        with tempfile.TemporaryDirectory() as folder:
            shared, isolated = Path(folder) / 'own-app-login', Path(folder) / 'qa-library'
            auth = ChatGPTAuth(shared)
            auth._save({'version': 1, 'generation': 1, 'active': 'synthetic', 'registrations': {
                'synthetic': {'client_id': 'oaiapp_synthetic', 'access_token': 'qa-not-real',
                    'scopes': [DIRECT_SCOPE, 'resource.invoke'], 'welcome_acknowledged': False}}})
            cfg = replace(Settings.from_env(), root_dir=isolated, auth_root_dir=shared,
                data_dir=isolated / 'data', db_path=isolated / 'data/db.sqlite', papers_dir=isolated / 'data/papers')
            cfg.ensure_dirs(); db = Database(cfg.db_path); reader = ReaderService(cfg, db)
            try:
                before = auth.credentials.read_bytes()
                reader.chatgpt_auth.acknowledge_welcome('synthetic')
                self.assertNotEqual(auth.credentials.read_bytes(), before)
                self.assertFalse(auth.status()['accounts'][0]['needs_welcome'])
                self.assertFalse(list(isolated.rglob('chatgpt-auth.json')))
            finally:
                reader.shutdown(); db.close()
