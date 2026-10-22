"""No registry mutation, runtime download, GUI or native loader needed here."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from paper_reader.windows_webview import configure_windows_runtime


class WebViewRuntimeTest(unittest.TestCase):
    def test_verified_installation_works_without_registry_discovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for version in ('155.0.4283.60', '155.0.4283.62'):
                (root / version).mkdir(); (root / version / 'msedgewebview2.exe').touch()
            view = SimpleNamespace(settings={'WEBVIEW2_RUNTIME_PATH': None})
            calls = []
            def probe(path):
                calls.append(path)
                return path.name if path else None
            selected = configure_windows_runtime(view, roots=[root], probe=probe)
            self.assertEqual(selected, str(root / '155.0.4283.62'))
            self.assertEqual(view.settings['WEBVIEW2_RUNTIME_PATH'], selected)
            self.assertEqual(calls, [root / '155.0.4283.62'])

    def test_invalid_latest_candidate_is_skipped_not_assumed_working(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for version in ('155.0.0.1', '155.0.0.2', '85.0.0.1'):
                (root / version).mkdir(); (root / version / 'msedgewebview2.exe').touch()
            view = SimpleNamespace(settings={})
            selected = configure_windows_runtime(view, roots=[root],
                probe=lambda p: p.name if p and p.name != '155.0.0.2' else None)
            self.assertEqual(selected, str(root / '155.0.0.1'))

    def test_missing_runtime_fails_instead_of_selecting_legacy_engine(self):
        view = SimpleNamespace(settings={})
        with self.assertRaisesRegex(RuntimeError, 'WebView2'):
            configure_windows_runtime(view, roots=[], probe=lambda p: None)
        self.assertEqual(view.settings, {})

    def test_standard_discovery_works_without_a_known_install_folder(self):
        view = SimpleNamespace(settings={})
        self.assertIsNone(configure_windows_runtime(view, roots=[], probe=lambda p: '155.0.0.1'))
        self.assertEqual(view.settings, {})

    def test_invalid_explicit_runtime_is_not_silently_replaced(self):
        view = SimpleNamespace(settings={'WEBVIEW2_RUNTIME_PATH': 'explicit-qa-path'})
        with self.assertRaisesRegex(RuntimeError, 'WebView2'):
            configure_windows_runtime(view, roots=[], probe=lambda p: None)
        self.assertEqual(view.settings['WEBVIEW2_RUNTIME_PATH'], 'explicit-qa-path')
