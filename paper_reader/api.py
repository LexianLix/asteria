from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .ai import AIEnricher
from .settings import Settings
from .storage import Database
from .sync import download_pdf, sync_all, sync_arxiv, sync_openalex
from .reader import pdf_path
from . import __version__


settings = Settings.from_env()
settings.ensure_dirs()
database = Database(settings.db_path)
app = FastAPI(title="Asteria API", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"https?://(?:localhost|127\.0\.0\.1)(?::\d+)?",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SyncRequest(BaseModel):
    source: Literal["arxiv", "openalex", "all"] = "arxiv"
    download_pdfs: bool = False
    max_results: int | None = Field(default=None, ge=1, le=200)


class AnnotationRequest(BaseModel):
    page_number: int = Field(ge=0)
    annotation_type: str = Field(min_length=1, max_length=32)
    color: str | None = None
    text: str | None = None
    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)
    width: float | None = Field(default=None, ge=0, le=1)
    height: float | None = Field(default=None, ge=0, le=1)
    path: list[dict[str, float]] = Field(default_factory=list)


class TranslationRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    target_lang: str = Field(default="zh-CN", min_length=2, max_length=16)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": app.version, "ai_enabled": bool(settings.ollama_url)}


@app.get("/papers")
def list_papers(
    q: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    only_downloaded: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    items, total = database.list_papers(
        offset=(page - 1) * page_size,
        limit=page_size,
        query=q,
        tag=tag,
        only_downloaded=only_downloaded,
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/papers/{paper_id}")
def get_paper(paper_id: int) -> dict[str, Any]:
    paper = database.get_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="论文不存在")
    return paper


def _run_sync(source: str, download_pdfs: bool, max_results: int | None) -> None:
    if source == "openalex":
        sync_openalex(settings, database, download_pdfs=download_pdfs, max_results=max_results)
    elif source == "all":
        sync_all(settings, database, download_pdfs=download_pdfs, max_results=max_results)
    else:
        sync_arxiv(settings, database, download_pdfs=download_pdfs, max_results=max_results)


@app.post("/sync", status_code=202)
def start_sync(request: SyncRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    background_tasks.add_task(_run_sync, request.source, request.download_pdfs, request.max_results)
    return {"status": "accepted", "source": request.source}


@app.get("/sync/runs")
def sync_runs(limit: int = Query(default=20, ge=1, le=100)) -> list[dict[str, Any]]:
    return database.list_sync_runs(limit)


@app.post("/papers/{paper_id}/download")
def download_paper(paper_id: int) -> dict[str, Any]:
    paper = database.get_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="论文不存在")
    try:
        path = download_pdf(settings, paper)
        relative = str(path.relative_to(settings.root_dir))
        database.update_local_pdf_path(paper_id, relative)
        return {"paper_id": paper_id, "local_pdf_path": relative}
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/papers/{paper_id}/pdf")
def get_pdf(paper_id: int) -> FileResponse:
    paper = database.get_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="论文不存在")
    local_path = paper.get("local_pdf_path")
    if not local_path:
        raise HTTPException(status_code=404, detail={"message": "PDF 尚未下载", "pdf_url": paper.get("pdf_url")})
    try:
        path = pdf_path(settings, paper)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="application/pdf", filename=f"{paper['source_id']}.pdf")


@app.get("/papers/{paper_id}/annotations")
def list_annotations(paper_id: int) -> list[dict[str, Any]]:
    if not database.get_paper(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    return database.list_annotations(paper_id)


@app.post("/papers/{paper_id}/annotations", status_code=201)
def add_annotation(paper_id: int, request: AnnotationRequest) -> dict[str, Any]:
    if not database.get_paper(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    return database.add_annotation(paper_id, request.model_dump())


@app.post("/papers/{paper_id}/translate")
def translate(paper_id: int, request: TranslationRequest) -> dict[str, Any]:
    if not database.get_paper(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    cached = database.get_translation(paper_id, request.text, request.target_lang)
    if cached:
        return {"translated_text": cached["translated_text"], "cached": True, "model": cached["model"]}
    enricher = AIEnricher(settings.ollama_url, settings.ollama_model, settings.topics().get("tags", {}))
    translated = enricher.translate(request.text, request.target_lang)
    if not translated:
        raise HTTPException(status_code=503, detail="未配置可用的 Ollama，或翻译请求失败")
    database.save_translation(paper_id, request.text, request.target_lang, translated, settings.ollama_model)
    return {"translated_text": translated, "cached": False, "model": settings.ollama_model}


from .desktop_api import install_desktop

install_desktop(app, settings, database)
