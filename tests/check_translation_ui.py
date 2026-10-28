"""Check the isolated --translation-gaps fixture with a headless browser."""
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
import psutil
from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:52287')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--launch-fixture', action='store_true', help='Own a fresh direct Python server and its cleanup')
    args = parser.parse_args()
    if not args.launch_fixture:
        return run_check(args)
    for _ in range(100):
        port = 49152 + secrets.randbelow(16384)
        try:
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', port))
            break
        except OSError:
            continue
    else:
        raise RuntimeError('No free fixture port')
    args.base_url = f'http://127.0.0.1:{port}'
    environment = dict(os.environ)
    environment.pop('PAPER_READER_AUTH_ROOT', None)
    process = subprocess.Popen([sys.executable, '-m', 'tests.ui_fixture_server', '--port', str(port), '--translation-gaps'],
        cwd=Path(__file__).resolve().parents[1], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    graceful = False
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 25
        while True:
            if process.poll() is not None:
                raise RuntimeError('Owned fixture server exited before readiness')
            try:
                with opener.open(args.base_url + '/health', timeout=1) as response:
                    assert response.status == 200
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError('Owned fixture server did not become ready')
                time.sleep(.1)
        assert any(c.status == psutil.CONN_LISTEN and c.laddr.port == port and c.pid == process.pid
                   for c in psutil.net_connections(kind='tcp') if c.laddr), 'Port is not owned by the fixture process'
        run_check(args)
    finally:
        if process.poll() is None:
            try:
                with opener.open(args.base_url + '/desktop/bootstrap', timeout=3) as response:
                    token = json.load(response)['token']
                request = urllib.request.Request(args.base_url + '/desktop/qa/shutdown', data=b'{}',
                    headers={'X-Desktop-Token': token, 'Content-Type': 'application/json'}, method='POST')
                with opener.open(request, timeout=3):
                    pass
                process.wait(timeout=10)
                graceful = True
            except Exception:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
    report_path = args.output / 'report.json'
    result = json.loads(report_path.read_text(encoding='utf-8'))
    result.update(owned_fixture_process_exited=True, fixture_graceful_cleanup=graceful)
    report_path.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'owned_fixture_process_exited': True, 'fixture_graceful_cleanup': graceful}))


def run_check(args):
    args.output.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(args.base_url)
            page.wait_for_load_state('networkidle')
            page.locator('.paper-card').first.click()
            expect(page.locator('#page-stage')).to_be_visible()
            page.locator('[data-mode="zh"]').click()
            expect(page.locator('#text-page')).to_be_visible()
            expect(page.locator('#job-text')).to_contain_text('仍有缺项')
            expect(page.locator('#translate-button')).to_have_text('补译缺项')
            before = page.request.get(args.base_url + '/desktop/papers/1/document').json()
            blank = next(block for block in before['pages'][0]['blocks']
                         if before['pages'][0]['translated'].get(block['id']) == '   ')
            expect(page.locator(f'[data-block-id="{blank["id"]}"] .block-text')).to_have_text(blank['en'])
            expect(page.locator('#text-page')).to_contain_text('本页缓存尚有缺项')
            page.locator('#translation-check-button').click()
            dialog = page.locator('#translation-check-dialog')
            expect(dialog).to_be_visible()
            expect(page.locator('#translation-check-summary')).to_contain_text('文字待翻译 2 项')
            expect(page.locator('#translation-check-summary')).to_contain_text('可本地补齐 2 项')
            expect(dialog).to_contain_text('不代表译文准确')
            page.screenshot(path=str(args.output / 'translation-gaps-before.png'), full_page=True)
            page.locator('#repair-local-translation').click()
            expect(page.locator('#translation-check-summary')).to_contain_text('可本地补齐 0 项')
            expect(page.locator('#translation-check-summary')).to_contain_text('文字待翻译 2 项')
            expect(page.locator('#repair-local-translation')).to_be_disabled()
            after = page.request.get(args.base_url + '/desktop/papers/1/document').json()
            for old, new in zip(before['pages'], after['pages']):
                assert old['blocks'] == new['blocks']
                assert all(new['translated'][key] == value for key, value in old['translated'].items())
            assert after['pages'][0]['translated']['qa-new-table-r0c1'] == '62.1'
            assert after['pages'][0]['translated']['qa-new-table-r0c2'] == '-'
            assert 'qa-new-table-r0c0' not in after['pages'][0]['translated']
            assert page.request.get(args.base_url + '/desktop/qa/model-calls').json()['count'] == 0
            page.screenshot(path=str(args.output / 'translation-gaps-after.png'), full_page=True)
            dialog.get_by_role('button', name='关闭译文检查').click()
            expect(page.locator('#job-text')).to_contain_text('仍有缺项')
            # A second check remains usable and cannot create another repair.
            page.locator('#translation-check-button').click()
            expect(page.locator('#translation-check-summary')).to_contain_text('可本地补齐 0 项')
            expect(page.locator('#repair-local-translation')).to_be_disabled()
            page.set_viewport_size({'width': 800, 'height': 900})
            expect(dialog).to_be_visible()
            bounds = dialog.bounding_box()
            assert bounds and bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= 801
            page.screenshot(path=str(args.output / 'translation-gaps-narrow.png'), full_page=True)
            assert not errors, errors
            result = {'result': 'passed', 'app_version': '0.1.1', 'model_requests': 0,
                'source_and_existing_cache_preserved': True, 'page_errors': errors,
                'checks': ['stale-job-warning', 'blank-cache-original-fallback', 'page-gap-counts',
                           'local-only-repair', 'remaining-text-gaps-visible', 'repeat-check', 'narrow-dialog']}
            (args.output / 'report.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
            print(json.dumps(result))
        finally:
            browser.close()


if __name__ == '__main__':
    main()
