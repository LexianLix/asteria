"""Own-window lifecycle. No remote commands, arbitrary JS or file access API."""
from __future__ import annotations

import threading


def tray_image():
    from PIL import Image, ImageDraw
    image = Image.new('RGBA', (64, 64), '#eef2f6')
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((9, 5, 54, 59), radius=4, fill='#fff', outline='#3468b2', width=3)
    draw.line([(18, 47), (31, 18), (45, 47)], fill='#3468b2', width=4)
    draw.line((24, 35, 39, 35), fill='#3468b2', width=3)
    return image


class DesktopLifecycle:
    def __init__(self, window, config, *, changed=lambda value: None):
        self.window, self.config, self.changed = window, config, changed
        self.tray = None
        self.loaded = False
        self.hidden = False
        self.exiting = False
        self._exit_lock = threading.Lock()

    def status(self):
        native = getattr(self.window, 'native', None)
        return {'native': True, 'native_visible': bool(native.Visible) if native is not None else None, 'loaded': self.loaded,
                'tray_available': self.tray is not None, 'hidden': self.hidden,
                'exiting': self.exiting, 'close_to_tray': bool(self.config().get('close_to_tray'))}

    def close(self):
        self.window.destroy()  # Same native closing event as the window close button.

    def ui_snapshot(self):
        if not self.loaded:
            return {'ready': False}
        return self.window.evaluate_js('({ready:document.readyState,title:document.title,cards:document.querySelectorAll(".paper-card").length,status:document.getElementById("reader-status")?.textContent})')

    def show(self):
        if self.exiting:
            return
        self.window.show(); self.window.restore(); self.hidden = False
        self.changed(self.status())

    def hide(self):
        if self.tray is None or self.exiting:
            return False  # Never strand a hidden window without a tray.
        self.window.hide(); self.hidden = True
        self.changed(self.status()); return True

    def flush(self):
        if not self.loaded:
            return True
        complete, result = threading.Event(), {}
        def finished(value):
            result['value'] = value; complete.set()
        try:
            self.window.evaluate_js('window.paperReaderBeforeExit ? window.paperReaderBeforeExit() : Promise.resolve({ok:false})', finished)
            return complete.wait(8) and isinstance(result.get('value'), dict) and result['value'].get('ok') is True
        except Exception:
            return False

    def exit(self):
        if not self._exit_lock.acquire(blocking=False):
            return False
        try:
            if self.exiting:
                return True
            if not self.flush():
                self.show()
                if self.tray:
                    self.tray.notify('补存未完成，请关闭对话框、检查网络与保存状态后再退出。', 'Asteria 未退出')
                return False
            self.exiting = True; self.changed(self.status()); self.window.destroy(); return True
        finally:
            self._exit_lock.release()

    def closing(self):
        if self.exiting:
            return True
        if self.config().get('close_to_tray') and self.hide():
            return False
        # The close callback must return promptly; flush/destroy on another thread.
        threading.Thread(target=self.exit, daemon=True).start()
        return False

    def start_tray(self):
        try:
            import pystray
            ready = threading.Event()
            icon = pystray.Icon('Asteria', tray_image(), 'Asteria · 文献阅读', menu=pystray.Menu(
                pystray.MenuItem('打开阅读器', lambda icon, item: self.show(), default=True),
                pystray.MenuItem('隐藏到托盘（继续运行）', lambda icon, item: self.hide()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('退出程序（停止后台任务）', lambda icon, item: self.exit())))
            def setup(icon):
                icon.visible = True; ready.set()
            icon.run_detached(setup=setup)
            if not ready.wait(3):
                icon.stop(); return False
            self.tray = icon; self.changed(self.status()); return True
        except Exception:
            # Close still exits normally if tray dependencies/Explorer are unavailable.
            return False

    def stop(self):
        if self.tray:
            self.tray.stop(); self.tray = None


class WindowsInstance:
    """One native process per data root, with a same-session reopen signal."""
    def __init__(self, directory):
        import ctypes
        import hashlib
        from ctypes import wintypes
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateMutexW.restype = wintypes.HANDLE
        self.api.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateEventW.restype = wintypes.HANDLE
        self.api.SetEvent.argtypes = [wintypes.HANDLE]; self.api.SetEvent.restype = wintypes.BOOL
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]; self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]; self.api.CloseHandle.restype = wintypes.BOOL
        key = hashlib.sha256(str(directory.resolve()).casefold().encode()).hexdigest()[:24]
        self.mutex = self.api.CreateMutexW(None, False, 'Local\\Asteria-' + key)
        if not self.mutex:
            raise OSError(ctypes.get_last_error(), '无法建立桌面实例锁')
        self.primary = ctypes.get_last_error() != 183
        self.install_mutex = self.api.CreateMutexW(None, False, 'Local\\Asteria-Installer')
        if not self.install_mutex:
            self.api.CloseHandle(self.mutex); raise OSError('无法建立安装占用标记')
        self.event = self.api.CreateEventW(None, False, False, 'Local\\Asteria-Show-' + key)
        if not self.event:
            self.api.CloseHandle(self.install_mutex); self.api.CloseHandle(self.mutex); raise OSError('无法建立桌面唤醒事件')
        self.stopped = threading.Event()

    def reopen(self):
        self.api.SetEvent(self.event)

    def listen(self, show):
        def worker():
            while not self.stopped.is_set():
                if self.api.WaitForSingleObject(self.event, 250) == 0:
                    show()
        self.thread = threading.Thread(target=worker, daemon=True); self.thread.start()

    def close(self):
        self.stopped.set()
        if getattr(self, 'thread', None):
            self.thread.join(1)
        self.api.CloseHandle(self.event); self.api.CloseHandle(self.install_mutex); self.api.CloseHandle(self.mutex)
