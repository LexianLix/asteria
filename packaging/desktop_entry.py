"""Standalone GUI entry; import the API only after selecting user-writable paths."""
import argparse
import ctypes
import sys
import traceback
from paper_reader.settings import Settings
from paper_reader.storage import DatabaseCompatibilityError


def main():
    parser = argparse.ArgumentParser(description='Asteria Windows desktop')
    parser.add_argument('--port', type=int, default=8788)
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--no-error-dialog', action='store_true', help='For owned automated QA: log startup errors and exit without a native error dialog')
    args = parser.parse_args()
    settings = Settings.from_env(); settings.ensure_dirs()
    log = (settings.data_dir / 'desktop.log').open('a', encoding='utf-8', buffering=1)
    sys.stdout = sys.stderr = log
    try:
        from paper_reader.desktop import launch
        launch(browser=args.browser, port=args.port)
    except Exception as exc:
        traceback.print_exc()
        reason = str(exc) if isinstance(exc, DatabaseCompatibilityError) else '桌面版启动失败。请检查文献库、配置和 Microsoft WebView2 Runtime。'
        if not args.no_error_dialog:
            ctypes.windll.user32.MessageBoxW(None, reason + '\n诊断日志：' + str(settings.data_dir / 'desktop.log'), 'Asteria', 0x10)
        return 1
    finally:
        log.flush()
    return 0


if __name__ == '__main__':
    sys.exit(main())
