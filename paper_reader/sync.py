from __future__ import annotations

import re
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .ai import AIEnricher
from .settings import Settings
from .sources.arxiv import ArxivSource
from .sources.openalex import OpenAlexSource
from .storage import Database
from .source_cooldowns import SourceCooldowns
from .sources.errors import SourceError, rate_limit_message


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "paper"


def download_pdf(settings: Settings, paper: dict[str, Any]) -> Path:
    url = paper.get("pdf_url")
    if not url:
        raise RuntimeError("论文没有开放 PDF 地址")
    target_dir = settings.papers_dir / safe_filename(str(paper["source"]))
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{safe_filename(str(paper['source_id']))}.pdf"
    if target.exists() and target.stat().st_size > 0:
        with target.open("rb") as existing:
            if existing.read(5) == b"%PDF-":
                return target
    request = urllib.request.Request(url, headers={"User-Agent": settings.arxiv_user_agent})
    temporary: Path | None = None
    try:
        with urllib.request.urlopen(request, timeout=90) as response, tempfile.NamedTemporaryFile(
            mode="wb", dir=target_dir, suffix=".part", delete=False
        ) as output:
            temporary = Path(output.name)
            total = 0
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                total += len(chunk)
                if total > 100 * 1024 * 1024:
                    raise RuntimeError("PDF 超过 100 MB，已停止下载")
                output.write(chunk)
        with temporary.open("rb") as downloaded:
            if downloaded.read(5) != b"%PDF-":
                raise RuntimeError("来源返回的内容不是 PDF；没有保存为已下载文献")
        temporary.replace(target)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"PDF 下载失败: {exc}") from exc
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return target


def _process_records(
    settings: Settings,
    database: Database,
    *,
    source_name: str,
    records: list[dict[str, Any]],
    enricher: AIEnricher,
    download_pdfs: bool,
    run_id: int | None = None,
) -> dict[str, Any]:
    run_id = run_id or database.start_sync(source_name)
    fetched = len(records)
    inserted = updated = downloaded = failed = 0
    new_works = new_source_versions = 0
    errors: list[str] = []
    for record in records:
        try:
            paper_id, was_inserted = database.upsert_paper(record)
            inserted += int(was_inserted)
            updated += int(not was_inserted)
            if was_inserted:
                if len(database.work_versions(paper_id)) == 1:
                    new_works += 1
                else:
                    new_source_versions += 1
            current = database.get_paper(paper_id)
            needs_ai = bool(
                current
                and (
                    was_inserted
                    or (settings.ollama_url and not current.get("ai_summary_zh"))
                    or not current.get("ai_tags")
                )
            )
            if needs_ai:
                enrichment = enricher.enrich(
                    title=record["title"],
                    abstract=record["abstract"],
                    categories=record["categories"],
                )
                database.update_ai(
                    paper_id,
                    summary_zh=enrichment.summary_zh,
                    tags=enrichment.tags,
                    score=enrichment.score,
                )
            if download_pdfs:
                current = database.get_paper(paper_id)
                if current and not current.get("local_pdf_path") and current.get("pdf_url"):
                    path = download_pdf(settings, current)
                    database.update_local_pdf_path(paper_id, str(path.relative_to(settings.root_dir)))
                    downloaded += 1
        except Exception as exc:  # one bad paper must not cancel the batch
            failed += 1
            errors.append(f"{record.get('source_id', '?')}: {exc}")
    database.finish_sync(
        run_id,
        fetched=fetched,
        inserted=inserted,
        updated=updated,
        downloaded=downloaded,
        failed=failed,
        error="; ".join(errors[:10]) or None,
    )
    return {
        "run_id": run_id,
        "source": source_name,
        "fetched": fetched,
        "inserted": inserted,
        "updated": updated,
        "new_works": new_works,
        "new_source_versions": new_source_versions,
        "downloaded": downloaded,
        "failed": failed,
        "errors": errors[:10],
    }


def _sync_source(
    settings: Settings,
    database: Database,
    *,
    source_name: str,
    fetcher: Callable[[], list[dict[str, Any]]],
    download_pdfs: bool,
) -> dict[str, Any]:
    cooldowns = getattr(database, 'source_cooldowns', None) or SourceCooldowns(database)
    blocked = cooldowns.active([source_name])
    if blocked:
        raise SourceError(rate_limit_message(source_name, blocked['retry_at'], blocked.get('retry_after_source')),
            source=source_name, status=429, retry_at=blocked['retry_at'], retry_after_source=blocked.get('retry_after_source'))
    run_id = database.start_sync(source_name)
    try:
        records = fetcher()
    except SourceError as exc:
        cooldowns.record(exc)
        database.finish_sync(run_id, failed=1, error=str(exc))
        raise
    except Exception as exc:
        database.finish_sync(run_id, failed=1, error=str(exc))
        raise
    return _process_records(
        settings,
        database,
        source_name=source_name,
        records=records,
        enricher=AIEnricher(settings.ollama_url, settings.ollama_model, settings.topics().get("tags", {})),
        download_pdfs=download_pdfs,
        run_id=run_id,
    )


def sync_arxiv(
    settings: Settings,
    database: Database,
    *,
    download_pdfs: bool = False,
    max_results: int | None = None,
) -> dict[str, Any]:
    source_config = settings.topics().get("sources", {}).get("arxiv", {})
    source = ArxivSource(settings.arxiv_user_agent)
    return _sync_source(
        settings,
        database,
        source_name="arxiv",
        fetcher=lambda: source.fetch(
            categories=list(source_config.get("categories", [])),
            keywords=list(source_config.get("keywords", [])),
            max_results=max_results or int(source_config.get("max_results", settings.default_max_results)),
        ),
        download_pdfs=download_pdfs,
    )


def sync_openalex(
    settings: Settings,
    database: Database,
    *,
    download_pdfs: bool = False,
    max_results: int | None = None,
) -> dict[str, Any]:
    source_config = settings.topics().get("sources", {}).get("openalex", {})
    source = OpenAlexSource(settings.arxiv_user_agent, settings.openalex_mailto)
    return _sync_source(
        settings,
        database,
        source_name="openalex",
        fetcher=lambda: source.fetch(
            keywords=list(source_config.get("keywords", [])),
            days_back=int(source_config.get("days_back", 2)),
            max_results=max_results or int(source_config.get("max_results", settings.default_max_results)),
        ),
        download_pdfs=download_pdfs,
    )


def sync_all(
    settings: Settings,
    database: Database,
    *,
    download_pdfs: bool = False,
    max_results: int | None = None,
) -> list[dict[str, Any]]:
    results = [sync_arxiv(settings, database, download_pdfs=download_pdfs, max_results=max_results)]
    openalex_config = settings.topics().get("sources", {}).get("openalex", {})
    if openalex_config.get("enabled", False):
        results.append(sync_openalex(settings, database, download_pdfs=download_pdfs, max_results=max_results))
    return results
