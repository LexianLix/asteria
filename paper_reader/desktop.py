"""Start a loopback-only server and a real WebView2 desktop window."""
from __future__ import annotations

import socket
import threading
import time
import webbrowser
import json
import os
import sys
import secrets

import uvicorn


def bind_loopback(port: int):
    """Keep a bound socket; automatic ports use the browser-safe dynamic range."""
    if not 0 <= port <= 65535:
        raise ValueError('桌面端口必须在 0–65535 范围内')
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if port:
        try:
            sock.bind(('127.0.0.1', port))
            return sock
        except OSError:
            pass
    try:
        # OS-assigned port 0 can select a low port blocked by Chromium/WebView2.
        # Do not disable that policy; reserve an available high port instead.
        for _ in range(100):
            try:
                sock.bind(('127.0.0.1', 49152 + secrets.randbelow(16384)))
                return sock
            except OSError:
                continue
        raise OSError('无法为桌面服务预留可用的本机端口')
    except Exception:
        sock.close()
        raise


def launch(*, browser: bool = False, port: int = 8788) -> None:
    from .settings import Settings
    from .lifecycle import DesktopLifecycle, WindowsInstance
    settings = Settings.from_env(); settings.ensure_dirs()
    instance = WindowsInstance(settings.data_dir) if os.name == 'nt' else None
    if instance and not instance.primary:
        instance.reopen(); instance.close(); return
    try:
        _run(settings, instance, browser, port, DesktopLifecycle)
    finally:
        if instance:
            instance.close()


def _run(settings, instance, browser, port, lifecycle_class):
    from .api import app

    app.state.desktop_only = True
    sock = bind_loopback(port)
    actual_port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{actual_port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=actual_port, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    lifecycle = None
    ready_path = settings.data_dir / 'desktop-ready.json'
    record_lock = threading.Lock()
    def record(status):
        status = {'pid': os.getpid(), 'url': url, 'frozen': bool(getattr(sys, 'frozen', False)), **status}
        with record_lock:
            temporary = ready_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(status, ensure_ascii=False), encoding='utf-8'); temporary.replace(ready_path)
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("本地阅读服务启动失败")
            time.sleep(.1)
        print(f"Asteria: {url}", flush=True)
        if browser:
            if instance:
                instance.listen(lambda: webbrowser.open(url))
            webbrowser.open(url)
            print("浏览器模式：关闭此终端或按 Ctrl+C 停止服务。", flush=True)
            while thread.is_alive():
                thread.join(.5)
        else:
            try:
                import webview
            except ImportError as exc:
                raise RuntimeError('请先运行 python -m pip install -e ".[desktop]"；或使用 desktop --browser') from exc
            if os.name == 'nt':
                from .windows_webview import configure_windows_runtime
                configure_windows_runtime(webview)
            window = webview.create_window("Asteria · 文献阅读", url, width=1440, height=940, min_size=(1000, 680),
                                          background_color="#f5f7fa", text_select=True)
            lifecycle = lifecycle_class(window, app.state.reader_service.config, changed=record)
            app.state.desktop_lifecycle = lifecycle
            window.events.closing += lifecycle.closing
            def on_loaded():
                # Ensure a hidden terminal launch does not leave the interactive window hidden.
                lifecycle.loaded = True; lifecycle.show()
                status = {**lifecycle.status(), "title": window.evaluate_js("document.title"),
                          "ready": window.evaluate_js("document.readyState"), "window": "loaded"}
                record(status)
            window.events.loaded += on_loaded
            def started():
                lifecycle.start_tray()
                if instance:
                    instance.listen(lifecycle.show)
            webview.start(started, gui='edgechromium' if os.name == 'nt' else None,
                          private_mode=False, storage_path=str(settings.data_dir / "webview"))
    except KeyboardInterrupt:
        pass
    finally:
        if lifecycle:
            lifecycle.stop()
        app.state.reader_service.shutdown()
        server.should_exit = True
        thread.join(timeout=4)
        sock.close()
        record({'window': 'stopped', 'native': lifecycle is not None, 'exiting': True})
