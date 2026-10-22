from __future__ import annotations

import argparse
import json

from .settings import Settings
from .storage import Database
from .sync import sync_all, sync_arxiv, sync_openalex


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Asteria local backend")
    sub = parser.add_subparsers(dest="command", required=True)

    sync_parser = sub.add_parser("sync", help="同步 arXiv 论文")
    sync_parser.add_argument("--source", choices=("arxiv", "openalex", "all"), default="arxiv")
    sync_parser.add_argument("--download-pdfs", action="store_true", help="同时下载开放 PDF")
    sync_parser.add_argument("--max-results", type=int, default=None, help="最多获取多少篇")

    serve_parser = sub.add_parser("serve", help="启动 FastAPI 服务")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8787)

    stats_parser = sub.add_parser("stats", help="显示本地论文数量")
    stats_parser.set_defaults()
    desktop_parser = sub.add_parser("desktop", help="打开桌面文献阅读器")
    desktop_parser.add_argument("--browser", action="store_true", help="改用浏览器打开，不使用原生窗口")
    desktop_parser.add_argument("--port", type=int, default=8788)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "desktop":
        from .desktop import launch
        launch(browser=args.browser, port=args.port)
    elif args.command == "sync":
        settings = Settings.from_env()
        settings.ensure_dirs()
        database = Database(settings.db_path)
        if args.source == "openalex":
            result = sync_openalex(
                settings,
                database,
                download_pdfs=args.download_pdfs,
                max_results=args.max_results,
            )
        elif args.source == "all":
            result = sync_all(
                settings,
                database,
                download_pdfs=args.download_pdfs,
                max_results=args.max_results,
            )
        else:
            result = sync_arxiv(
                settings,
                database,
                download_pdfs=args.download_pdfs,
                max_results=args.max_results,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "serve":
        import uvicorn
        from .api import app

        uvicorn.run(app, host=args.host, port=args.port)
    elif args.command == "stats":
        settings = Settings.from_env()
        database = Database(settings.db_path)
        _, total = database.list_papers(limit=1)
        print(json.dumps({"papers": total, "database": str(settings.db_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
