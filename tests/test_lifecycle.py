import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from paper_reader.lifecycle import DesktopLifecycle, WindowsInstance
from paper_reader.desktop import bind_loopback
from paper_reader.settings import ROOT_DIR, Settings


class FakeWindow:
    def __init__(self, flush=True):
        self.native = SimpleNamespace(Visible=True)
        self.flush_ok = flush
        self.destroyed = False
    def show(self): self.native.Visible = True
    def restore(self): pass
    def hide(self): self.native.Visible = False
    def destroy(self): self.destroyed = True
    def evaluate_js(self, script, callback): callback({'ok': self.flush_ok})


class FakeTray:
    def __init__(self): self.messages = []
    def notify(self, *args): self.messages.append(args)


class LifecycleTest(unittest.TestCase):
    def test_automatic_and_occupied_ports_reserve_loopback_high_ports(self):
        first = bind_loopback(0)
        try:
            address, port = first.getsockname()
            self.assertEqual(address, '127.0.0.1')
            self.assertTrue(49152 <= port <= 65535)
            first.listen(1)
            second = bind_loopback(port)
            try:
                self.assertNotEqual(second.getsockname()[1], port)
                self.assertTrue(49152 <= second.getsockname()[1] <= 65535)
            finally: second.close()
        finally: first.close()

    def test_unavailable_auto_ports_close_socket_instead_of_unsafe_fallback(self):
        with patch('paper_reader.desktop.socket.socket') as factory:
            factory.return_value.bind.side_effect = OSError('occupied')
            with self.assertRaisesRegex(OSError, '无法为桌面服务预留'): bind_loopback(0)
            self.assertEqual(factory.return_value.bind.call_count, 100)
            factory.return_value.close.assert_called_once()

    def test_invalid_port_is_rejected_before_creating_socket(self):
        with patch('paper_reader.desktop.socket.socket') as factory:
            for port in (-1, 65536):
                with self.assertRaises(ValueError): bind_loopback(port)
            factory.assert_not_called()

    def test_no_tray_never_hides_window(self):
        window = FakeWindow(); ctrl = DesktopLifecycle(window, lambda: {'close_to_tray': True})
        self.assertFalse(ctrl.hide()); self.assertTrue(window.native.Visible)
        self.assertFalse(ctrl.status()['tray_available'])

    def test_hide_reopen_keeps_process_and_window(self):
        window = FakeWindow(); ctrl = DesktopLifecycle(window, lambda: {})
        ctrl.tray = FakeTray()
        self.assertTrue(ctrl.hide()); self.assertFalse(ctrl.status()['native_visible'])
        ctrl.show(); self.assertTrue(ctrl.status()['native_visible'])
        self.assertFalse(window.destroyed)

    def test_close_to_tray_cancels_native_close(self):
        window = FakeWindow(); ctrl = DesktopLifecycle(window, lambda: {'close_to_tray': True})
        ctrl.tray = FakeTray()
        self.assertFalse(ctrl.closing()); self.assertTrue(ctrl.hidden); self.assertFalse(window.destroyed)

    def test_failed_flush_refuses_exit_and_reopens(self):
        window = FakeWindow(flush=False); ctrl = DesktopLifecycle(window, lambda: {})
        ctrl.loaded = True; ctrl.tray = FakeTray(); ctrl.hide()
        self.assertFalse(ctrl.exit()); self.assertFalse(window.destroyed)
        self.assertTrue(window.native.Visible); self.assertTrue(ctrl.tray.messages)

    def test_successful_flush_allows_exit(self):
        window = FakeWindow(); ctrl = DesktopLifecycle(window, lambda: {})
        ctrl.loaded = True
        self.assertTrue(ctrl.exit()); self.assertTrue(window.destroyed); self.assertTrue(ctrl.closing())

    def test_source_and_frozen_default_roots_are_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {'LOCALAPPDATA': folder}, clear=True):
                self.assertEqual(Settings.from_env().root_dir, ROOT_DIR)
                with patch.object(sys, 'frozen', True, create=True):
                    settings = Settings.from_env(); settings.ensure_dirs()
                    self.assertEqual(settings.root_dir, Path(folder) / 'Asteria')
                    self.assertTrue(settings.topics_path.is_file())
                    settings.topics_path.write_text(json.dumps({'private_setting': 'must survive upgrade'}), encoding='utf-8')
                    settings.ensure_dirs()
                    self.assertEqual(settings.topics()['private_setting'], 'must survive upgrade')

    @unittest.skipUnless(os.name == 'nt', 'Windows native named mutex and reopen event')
    def test_native_single_instance_signal(self):
        with tempfile.TemporaryDirectory() as folder:
            first = WindowsInstance(Path(folder)); second = WindowsInstance(Path(folder))
            try:
                self.assertTrue(first.primary); self.assertFalse(second.primary)
                second.reopen(); self.assertEqual(first.api.WaitForSingleObject(first.event, 1000), 0)
            finally:
                second.close(); first.close()


if __name__ == '__main__':
    unittest.main()
