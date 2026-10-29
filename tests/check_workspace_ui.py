"""Exercise workspace transitions in an owned fixture, never a personal library.

python -B -m tests.check_workspace_ui --output <external report directory>
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import expect, sync_playwright


def check(base, output):
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base); page.wait_for_load_state('networkidle')
            bootstrap = page.request.get(base + '/desktop/bootstrap').json()
            assert bootstrap['config']['model'] == 'isolated-mock-model', 'Not an isolated fixture'
            headers = {'X-Desktop-Token': bootstrap['token']}
            title = page.locator('.paper-card').first.get_attribute('aria-label')
            expect(page.locator('.app-shell')).to_have_attribute('data-view', 'library')
            page.locator('#today-button').click()
            expect(page.locator('#today-workspace')).to_be_visible()
            expect(page.locator('.today-paper')).to_have_count(3)
            page.locator('.today-paper-title').filter(has_text=title).click()
            expect(page.locator('.library')).to_be_hidden()
            expect(page.locator('#notes-panel')).to_be_hidden()
            expect(page.locator('#page-total')).to_have_text('2')
            page.locator('#pdf-continuous').check()
            expect(page.locator('#pdf-flow')).to_be_visible()
            page.locator('#page-input').fill('2'); page.locator('#page-input').press('Enter')
            page.locator('#notes-toggle').click()
            page.locator('#note-input').fill('Workspace regression: keep saved note')
            page.locator('#save-note').click()
            expect(page.locator('#save-status')).to_contain_text('笔记已保存')
            page.locator('#note-input').fill('Workspace regression: keep draft')
            page.locator('#back-library').click()
            expect(page.locator('#pdf-flow .pdf-flow-page')).to_have_count(0)
            page.locator('#resume-reading').click()
            expect(page.locator('#pdf-flow')).to_be_visible()
            expect(page.locator('#page-input')).to_have_value('2')
            expect(page.locator('#note-input')).to_have_value('Workspace regression: keep draft')
            expect(page.locator('#notes-list')).to_contain_text('keep saved note')
            page.keyboard.press('Control+k')
            expect(page.locator('#search')).to_be_focused()
            page.locator('#search').fill('Quiet')
            expect(page.locator('#paper-list[aria-busy="false"] .paper-card')).to_have_count(1)
            pending = []
            def hold_state(route):
                if route.request.method == 'GET': pending.append(route)
                else: route.continue_()
            page.route('**/desktop/papers/1/state', hold_state)
            page.locator('.paper-card').click()
            expect(page.locator('#page-input')).to_be_disabled()
            expect(page.locator('#pdf-continuous')).to_be_disabled()
            page.locator('#back-library').click()
            page.wait_for_timeout(100)
            assert page.request.get(base + '/desktop/papers/1/state').json()['last_page'] == 1
            assert len(pending) == 1
            pending[0].continue_()
            expect(page.locator('#page-total')).to_have_text('2')
            expect(page.locator('.app-shell')).to_have_attribute('data-view', 'library')
            page.keyboard.press('ArrowLeft')
            page.locator('#resume-reading').click()
            expect(page.locator('#page-input')).to_have_value('2')
            expect(page.locator('#note-input')).to_have_value('Workspace regression: keep draft')
            for width in (1000, 1280, 1920):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
            page.screenshot(path=str(output / 'reading.png'))
            assert not errors, errors
            return {'passed': True, 'fixture_only': True, 'notes_and_draft_preserved': True,
                    'continuous_pdf_resumed': True, 'delayed_metadata_and_hidden_keyboard_preserve_page': True,
                    'viewport_widths': [1000, 1280, 1440, 1920], 'browser_errors': errors}
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    options.output.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        port = 49152 + secrets.randbelow(16384)
        try:
            with socket.socket() as reservation: reservation.bind(('127.0.0.1', port))
            break
        except OSError: continue
    else: raise RuntimeError('No free fixture port')
    base = f'http://127.0.0.1:{port}'
    environment = dict(os.environ); environment.pop('PAPER_READER_AUTH_ROOT', None)
    process = subprocess.Popen([sys.executable, '-B', '-m', 'tests.ui_fixture_server', '--port', str(port)],
        cwd=Path(__file__).resolve().parents[1], env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        deadline = time.monotonic() + 25
        while True:
            if process.poll() is not None: raise RuntimeError('Owned fixture exited before readiness')
            try:
                with opener.open(base + '/health', timeout=1): break
            except OSError:
                if time.monotonic() >= deadline: raise RuntimeError('Owned fixture startup timed out')
                time.sleep(.1)
        report = check(base, options.output)
    finally:
        if process.poll() is None:
            try:
                with opener.open(base + '/desktop/bootstrap', timeout=3) as response: token = json.load(response)['token']
                request = urllib.request.Request(base + '/desktop/qa/shutdown', data=b'{}',
                    headers={'X-Desktop-Token': token, 'Content-Type': 'application/json'}, method='POST')
                with opener.open(request, timeout=3): pass
                process.wait(timeout=10)
            except Exception:
                if process.poll() is None: process.terminate(); process.wait(timeout=10)
    report['owned_fixture_exited'] = process.poll() is not None
    (options.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__': main()
