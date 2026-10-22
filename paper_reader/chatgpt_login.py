"""Backend-only loopback callback lifecycle for official ChatGPT authorization."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .chatgpt_auth import ChatGPTAuthError


class ChatGPTLogin:
    def __init__(self, auth, *, open_browser=webbrowser.open):
        self.auth, self.open_browser = auth, open_browser
        self._lock = threading.RLock()
        self._attempt = self._server = self._thread = None
        self._state = {'status': 'idle', 'message': ''}

    def status(self):
        with self._lock:
            return {**self._state, 'connection': self.auth.status()}

    def start(self, *, registration_id=None, reconsent=False):
        self.cancel()
        owner = self

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Never log the callback URL / authorization code.

            def do_GET(self):
                parts = urlsplit(self.path)
                with owner._lock:
                    attempt = owner._attempt
                if (parts.path != '/auth/callback' or len(self.path) > 16000 or not attempt
                        or self.headers.get('Host') != urlsplit(attempt.redirect_uri).netloc):
                    self.send_error(400, 'Invalid callback')
                    return
                try:
                    values = parse_qs(parts.query, keep_blank_values=True, max_num_fields=12)
                    if any(len(value) != 1 for value in values.values()):
                        raise ChatGPTAuthError('回调参数重复，未接受登录', 'callback')
                    query = {key: value[0] for key, value in values.items()}
                    with owner._lock:
                        if owner._attempt is not attempt or attempt.used or owner._state['status'] != 'waiting':
                            self.send_error(400, 'Callback already handled')
                            return
                        if attempt.cancelled:
                            raise ChatGPTAuthError('登录尝试已变化', 'changed')
                        owner._state = {'status': 'exchanging', 'message': '正在核对 OpenAI 登录授权…'}
                    owner.auth.complete(attempt, query)
                    with owner._lock:
                        if owner._attempt is attempt:
                            owner._state = {'status': 'connected', 'message': '登录完成，请返回论文软件选择模型。'}
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/plain; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Referrer-Policy', 'no-referrer')
                    self.send_header('Content-Security-Policy', "default-src 'none'")
                    self.end_headers()
                    self.wfile.write('ChatGPT 授权已完成，请返回 Asteria。'.encode())
                except ChatGPTAuthError as exc:
                    with owner._lock:
                        if owner._attempt is attempt:
                            owner._state = {'status': 'failed', 'message': str(exc), 'code': exc.code}
                    self.send_error(400, 'Sign-in not completed; return to Asteria')

        server = ThreadingHTTPServer(('127.0.0.1', 0), Callback)
        server.daemon_threads = True
        uri = f'http://127.0.0.1:{server.server_port}/auth/callback'
        try:
            attempt = self.auth.begin(uri, registration_id=registration_id, reconsent=reconsent)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
            with self._lock:
                self._attempt, self._server, self._thread = attempt, server, thread
                self._state = {'status': 'waiting', 'message': '请在系统浏览器登录并确认授权；尚未发送论文。'}
            thread.start()
            if not self.open_browser(attempt.authorization_url()):
                raise ChatGPTAuthError('无法打开系统浏览器，请检查默认浏览器后重新登录', 'browser')
            threading.Thread(target=self._expire, args=(attempt,), daemon=True).start()
            return self.status()
        except Exception:
            if self._server is server:
                self.cancel()
            else:
                server.server_close()
            raise

    def _expire(self, attempt):
        # Short waits allow cancel/shutdown to end this owned timer promptly.
        while time.time() < attempt.expires_at:
            with self._lock:
                if self._attempt is not attempt or self._state['status'] != 'waiting':
                    return
            time.sleep(.5)
        self.cancel(expected_attempt=attempt)
        with self._lock:
            if self._attempt is None and self._state['status'] == 'cancelled':
                self._state = {'status': 'expired', 'message': '登录等待已超时，请重新登录。'}

    def cancel(self, *, expected_attempt=None):
        with self._lock:
            if expected_attempt is not None and self._attempt is not expected_attempt:
                return self.status()
            server, thread, attempt = self._server, self._thread, self._attempt
            self._server = self._thread = self._attempt = None
            if attempt:
                attempt.cancel()
            if self._state['status'] in ('waiting', 'exchanging'):
                self._state = {'status': 'cancelled', 'message': '已取消本次登录，原连接和论文保留。'}
        if server:
            server.shutdown()
            server.server_close()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        return self.status()

    shutdown = cancel
