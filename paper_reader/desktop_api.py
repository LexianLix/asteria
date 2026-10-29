from __future__ import annotations

import hashlib
import json
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import date as CalendarDate, datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import pdfplumber
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .reader import ReaderBusyError, ReaderService, PARSER_VERSION, SCIENTIFIC_VERSION, file_hash, pdf_path, quality_warnings, translation_cache_status
from .reading_quality import reading_quality
from . import __version__
from .complex_service import ComplexParserService
from .reviewed_reading import ReviewedReadingService
from .model_client import ModelClient, ModelError
from .chatgpt_auth import ChatGPTAuthError
from .chatgpt_model import ChatGPTModelClient
from .backup import BackupManager
from .assistant import PaperAssistant, ContextRequest, AssistantRequest, FIELD_NAMES
from .identity import normalize_doi, work_key
from .discovery import grouped_records, daily_brief
from .sources.errors import SourceError
from .source_cooldowns import SourceCooldowns
from .settings import Settings
from .storage import Database, SEARCH_INDEX_VERSION, utc_now
from .sync import sync_all, sync_arxiv, sync_openalex
from .classification import ClassificationService


class DesktopConfig(BaseModel):
    provider: Literal["ollama", "openai", "chatgpt"] = "ollama"
    base_url: str = Field(max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=2000)
    daily_enabled: bool = False
    daily_time: str = "09:00"
    sync_source: Literal["arxiv", "openalex", "all"] = "arxiv"
    retry_count: int = Field(default=2, ge=0, le=5)
    retry_delay: float = Field(default=1, ge=0, le=30)
    auto_backup: bool = True
    close_to_tray: bool = False
    clear_api_key: bool = False

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("请输入不带账号、查询参数的 HTTP(S) 接口地址")
        return value.rstrip("/")

    @field_validator("daily_time")
    @classmethod
    def validate_time(cls, value: str) -> str:
        datetime.strptime(value, "%H:%M")
        return value


class ChatGPTLoginRequest(BaseModel):
    registration_id: str | None = Field(default=None, min_length=64, max_length=64, pattern=r'^[a-f0-9]+$')
    reconsent: bool = False


class ChatGPTSelection(BaseModel):
    registration_id: str = Field(min_length=64, max_length=64, pattern=r'^[a-f0-9]+$')


class ReaderState(BaseModel):
    starred: bool | None = None
    last_page: int | None = Field(default=None, ge=0)
    pdf_offset: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    pdf_zoom: float | None = Field(default=None, ge=.5, le=2.5, allow_inf_nan=False)
    pdf_continuous: bool | None = None
    status: Literal["unread", "reading", "done"] | None = None
    folder: str | None = Field(default=None, max_length=100)
    tags: list[str] | None = Field(default=None, max_length=30)

    @field_validator('tags')
    @classmethod
    def validate_tags(cls, value):
        if value is not None and any(not t.strip() or len(t) > 100 for t in value):
            raise ValueError('标签不能为空或超过 100 字')
        return list(dict.fromkeys(t.strip() for t in value)) if value is not None else None


class DesktopAnnotation(BaseModel):
    client_id: str = Field(min_length=8, max_length=128)
    page_number: int = Field(ge=0)
    annotation_type: Literal["note", "ink", "highlight", "text-highlight"]
    text: str | None = Field(default=None, max_length=20000)
    color: str = Field(default="#e4bd45", pattern=r"^#[0-9a-fA-F]{6}$")
    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)
    width: float | None = Field(default=None, ge=0, le=1)
    height: float | None = Field(default=None, ge=0, le=1)
    path: list[dict[str, float]] = Field(default_factory=list, max_length=20000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value):
        for point in value:
            if not all(key in point and 0 <= point[key] <= 1 for key in ("x", "y")):
                raise ValueError("笔迹坐标必须在 0 到 1 之间")
        return value


class DesktopSync(BaseModel):
    source: Literal["arxiv", "openalex", "all"] = "arxiv"
    max_results: int = Field(default=30, ge=1, le=200)


class ClassificationConsent(BaseModel):
    enabled: bool
    confirmed: bool = False
    preview_id: str | None = Field(default=None, max_length=64)


class TopicsUpdate(BaseModel):
    categories: list[str] = Field(max_length=50)
    keywords: list[str] = Field(max_length=50)

    @field_validator("categories", "keywords")
    @classmethod
    def validate_words(cls, values):
        if any(not v.strip() or len(v) > 150 for v in values):
            raise ValueError("分类或关键词为空或过长")
        return [v.strip() for v in values]


class TranslationScope(BaseModel):
    scope: Literal["all", "page", "failed"] = "all"
    page_number: int | None = Field(default=None, ge=0)
    force: bool = False
    chatgpt_account_id: str | None = Field(default=None, min_length=64, max_length=64)
    chatgpt_generation: int | None = Field(default=None, ge=0)
    chatgpt_model: str | None = Field(default=None, min_length=1, max_length=200)


class LocalTranslationRepair(BaseModel):
    page_number: int | None = Field(default=None, ge=0)


class SnippetRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    chatgpt_account_id: str | None = Field(default=None, min_length=64, max_length=64)
    chatgpt_generation: int | None = Field(default=None, ge=0)
    chatgpt_model: str | None = Field(default=None, min_length=1, max_length=200)


class ComplexParseRequest(BaseModel):
    start_page: int = Field(default=1, ge=1)
    end_page: int | None = Field(default=None, ge=1)
    ocr: bool = False


class ComplexLinkRequest(BaseModel):
    source_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    from_id: str = Field(min_length=1, max_length=128)
    to_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    confirmed: bool = True


class ReadingSourceBase(BaseModel):
    source_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    document_token: str = Field(pattern=r'^[a-f0-9]{64}$')


class ReviewedBlock(BaseModel):
    candidate_id: str | None = Field(default=None, min_length=1, max_length=150)
    text: str = Field(min_length=1, max_length=10000)


class AdoptReadingRequest(ReadingSourceBase):
    preview_revision: int = Field(ge=1)
    candidate_source: Literal['enhanced', 'extraction'] = 'enhanced'
    confirmed: bool = False
    blocks: list[ReviewedBlock] = Field(min_length=1, max_length=500)


class SwitchReadingRequest(ReadingSourceBase):
    version: str = Field(min_length=1, max_length=100)


class BackupRestore(BaseModel):
    name: str
    confirmation: Literal["RESTORE"]


class DraftsRequest(BaseModel):
    drafts: dict[str, str] = Field(default_factory=dict, max_length=10000)


class ReaderEditor(BaseModel):
    glossary: list[dict[str, str]] | None = Field(default=None, max_length=200)
    block_id: str | None = Field(default=None, max_length=150)
    zh: str | None = Field(default=None, max_length=20000)


class ResearchEdit(BaseModel):
    revision: int = Field(ge=0)
    fields: dict[str, str] = Field(default_factory=dict, max_length=4)
    thoughts: str = Field(default='', max_length=18000)

    @field_validator('fields')
    @classmethod
    def validate_fields(cls, values):
        if set(values) - set(FIELD_NAMES) or any(len(v) > 16000 for v in values.values()):
            raise ValueError('研究卡片字段不正确或内容过长')
        return values


def install_desktop(app: FastAPI, settings: Settings, db: Database) -> ReaderService:
    service = ReaderService(settings, db)
    source_cooldowns = SourceCooldowns(db)
    db.source_cooldowns = source_cooldowns  # Shared with the lower-level/legacy sync entry points.
    app.state.source_cooldowns = source_cooldowns  # Internal deterministic QA clock, not an HTTP override.
    def requested_sources(source):
        if source != 'all': return [source]
        return ['arxiv'] + (['openalex'] if settings.topics().get('sources', {}).get('openalex', {}).get('enabled') else [])
    classifier = ClassificationService(service)
    service.classification = classifier
    app.state.classification = classifier
    complex_parser = ComplexParserService(service)
    service.complex_parser = complex_parser
    app.state.complex_parser = complex_parser
    app.state.chatgpt_login = service.chatgpt_login
    reviewed_reading = ReviewedReadingService(service, complex_parser)
    @app.get('/desktop/runtime')
    def desktop_runtime():
        import sys
        controller = getattr(app.state, 'desktop_lifecycle', None)
        return {**(controller.status() if controller else {'native': False, 'tray_available': False, 'hidden': False}),
                'frozen': bool(getattr(sys, 'frozen', False)), 'executable': str(Path(sys.executable)), 'data_directory': str(settings.data_dir),
                'app_version': __version__, 'database_version': db.schema_version()}

    @app.get('/desktop/window-state')
    def desktop_window_state():
        controller = getattr(app.state, 'desktop_lifecycle', None)
        return controller.ui_snapshot() if controller else {'ready': False, 'native': False}

    @app.post('/desktop/window/{action}', status_code=202)
    def desktop_window(action: Literal['show', 'hide', 'close', 'exit'], tasks: BackgroundTasks):
        controller = getattr(app.state, 'desktop_lifecycle', None)
        if not controller:
            raise HTTPException(409, '此操作只适用于原生桌面窗口')
        if action == 'hide' and not controller.tray:
            raise HTTPException(409, '托盘不可用，不能隐藏阅读窗口')
        tasks.add_task(getattr(controller, action))
        return {'accepted': action}
    assistant = PaperAssistant(service)
    backups = BackupManager(settings, db)
    token = secrets.token_urlsafe(32)
    app.state.reader_service = service
    app.state.desktop_only = False
    app.state.maintenance = False
    stop = threading.Event()
    sync_lock = threading.Lock()
    index_cancel = threading.Event()
    index_lock = threading.Lock()
    index_state_lock = threading.RLock()
    admission_lock = threading.Lock()
    active_writes = 0
    restore_admitted = False

    @app.middleware("http")
    async def protect_desktop(request, call_next):
        nonlocal active_writes, restore_admitted
        protected = request.url.path.startswith("/desktop/") or app.state.desktop_only
        if protected:
            if request.url.hostname not in ("localhost", "127.0.0.1", "testserver"):
                return JSONResponse({"detail": "桌面服务只接受本机访问"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
            if request.method not in ("GET", "HEAD", "OPTIONS") and not secrets.compare_digest(request.headers.get("x-desktop-token", ""), token):
                return JSONResponse({"detail": "请重新打开桌面应用"}, status_code=403)
            if request.method not in ("GET", "HEAD", "OPTIONS") and app.state.maintenance:
                return JSONResponse({"detail": "正在恢复文献库，请等待完成"}, status_code=409)
        writing = protected and request.method not in ("GET", "HEAD", "OPTIONS")
        restoring = writing and request.url.path == "/desktop/backups/restore"
        if writing:
            with admission_lock:
                if restore_admitted or (restoring and active_writes):
                    return JSONResponse({"detail": "有保存或模型请求正在执行，请完成后再恢复"}, status_code=409)
                if restoring:
                    restore_admitted = True
                else:
                    active_writes += 1
        try:
            return await call_next(request)
        finally:
            if writing:
                with admission_lock:
                    if restoring:
                        restore_admitted = False
                    else:
                        active_writes -= 1

    def require_paper(paper_id: int) -> dict:
        paper = db.get_paper(paper_id)
        if not paper:
            raise HTTPException(404, "论文不存在")
        return paper

    @app.post('/desktop/papers/{paper_id}/assistant/preview')
    def preview_assistant(paper_id: int, value: ContextRequest):
        require_paper(paper_id)
        try:
            return assistant.prepare(paper_id, value)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post('/desktop/papers/{paper_id}/assistant')
    def ask_assistant(paper_id: int, value: AssistantRequest):
        require_paper(paper_id)
        try:
            return assistant.run(paper_id, value)
        except ModelError as exc:
            raise HTTPException(502, {'message': str(exc), 'code': exc.code}) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/desktop/papers/{paper_id}/discussion')
    def discussions(paper_id: int, limit: int = Query(default=50, ge=1, le=200)):
        require_paper(paper_id)
        items = db.reader_get(paper_id, 'discussion').get('items', [])
        return {'items': items[-limit:], 'total': len(items)}

    @app.get('/desktop/papers/{paper_id}/research-card')
    def get_research_card(paper_id: int):
        require_paper(paper_id)
        return {'revision': 0, 'fields': {}, 'thoughts': '', **db.reader_get(paper_id, 'research-card')}

    @app.patch('/desktop/papers/{paper_id}/research-card')
    def edit_research_card(paper_id: int, value: ResearchEdit):
        require_paper(paper_id)
        with db._lock:
            card = db.reader_get(paper_id, 'research-card')
            if card.get('revision', 0) != value.revision:
                raise HTTPException(409, '研究卡片已被其他窗口修改，当前草稿保留在本机；请核对两个版本后再保存')
            card.update(fields=value.fields, thoughts=value.thoughts, revision=value.revision + 1, updated_at=utc_now())
            db.reader_put(paper_id, 'research-card', card)
            return card

    @app.get('/desktop/papers/{paper_id}/assistant/source/{block_id}')
    def assistant_source(paper_id: int, block_id: str, source_hash: str = Query(min_length=64, max_length=64)):
        require_paper(paper_id)
        doc = service.document(paper_id)
        if doc['source_hash'] != source_hash:
            raise HTTPException(409, '该回答对应旧版 PDF，无法定位到当前版本；请重新核对来源')
        for page in doc['pages']:
            for block in page['blocks']:
                if block['id'] == block_id:
                    return {'page': page['number'], 'block': block}
        raise HTTPException(404, '原文段落已变化，不能将旧出处定位到新段落')

    @app.get('/desktop/papers/{paper_id}/research-card.md')
    def export_research_card(paper_id: int):
        paper = require_paper(paper_id)
        card = db.reader_get(paper_id, 'research-card')
        labels = {'problem': '研究问题', 'method': '方法', 'result': '结果', 'limitations': '局限'}
        lines = ['# ' + paper['title'], '', 'AI 内容与手动整理均需核对原文。', '']
        generated = card.get('generated', {})
        for name in FIELD_NAMES:
            lines.extend(['## ' + labels[name], ''])
            if name in card.get('fields', {}):
                lines.extend(['手动整理：', card['fields'][name], ''])
            for claim in generated.get('fields', {}).get(name, []):
                lines.append(f"AI {claim['kind']}：{claim['text']}")
                for cite in claim['sources']:
                    lines.append(f"来源第 {cite['page'] + 1} 页（{cite['id']}）：{cite['quote']}")
            lines.append('')
        lines.extend(['## 我的想法', '', card.get('thoughts', ''), ''])
        return Response('\n'.join(lines), media_type='text/markdown; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="research-{paper_id}.md"'})

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).parent / "web" / "index.html")

    @app.get("/desktop/bootstrap")
    def bootstrap():
        return {"token": token, "config": service.config(public=True), "version": __version__}

    @app.get("/desktop/papers")
    def papers(q: str | None = None, tag: str | None = None, downloaded: bool = False, starred: bool = False,
               status: Literal['unread', 'reading', 'done'] | None = None, folder: str | None = None,
               page: int = Query(1, ge=1), page_size: int = Query(40, ge=1, le=100)):
        items, total = db.list_papers(offset=(page - 1) * page_size, limit=page_size,
                                      query=q, tag=tag, only_downloaded=downloaded, starred=starred, status=status, folder=folder, group_doi=True)
        counts = db.work_counts()
        for item in items:
            item["reader_state"] = db.reader_get(item["id"], "state")
            item['versions_count'] = counts.get(work_key(item.get('doi'), item['id']), 1)
            item['normalized_doi'] = normalize_doi(item.get('doi'))
            if q:
                needle = q.strip().casefold()
                doc = db.reader_get(item['id'], 'document')
                edits = db.reader_get(item['id'], 'editor').get('edits', {})
                found = False
                for source_page in doc.get('pages', []):
                    for block in source_page.get('blocks', []):
                        for language, text in (('en', block['en']), ('zh', edits.get(block['id'], {}).get('zh', source_page.get('translated', {}).get(block['id'], '')))):
                            position = text.casefold().find(needle)
                            if position >= 0:
                                item['search_hit'] = {'page': source_page['number'], 'block_id': block['id'], 'language': language,
                                                      'excerpt': text[max(0, position - 35):position + len(q) + 90]}
                                found = True
                                break
                        if found:
                            break
                    if found:
                        break
                    for region in source_page.get('scientific_regions', []):
                        source_texts = [('en', region.get('text', ''))]
                        if region['kind'] == 'table':
                            source_texts += [('zh', source_page['translated'].get(f"{region['id']}-r{r}c{c}", ''))
                                             for r, row in enumerate(region['rows']) for c, _ in enumerate(row)]
                        for language, text in source_texts:
                            position = text.casefold().find(needle)
                            if position >= 0:
                                item['search_hit'] = {'page': source_page['number'], 'block_id': region['id'], 'region_id': region['id'],
                                                      'language': language, 'excerpt': text[max(0, position - 35):position + len(q) + 90]}
                                found = True; break
                        if found:
                            break
                    if found:
                        break
        return {"items": items, "total": total, "page": page}

    @app.get("/desktop/overview")
    def overview():
        rows = db.overview_records()
        sources = requested_sources(service.config().get('sync_source', 'arxiv'))
        jobs = db.jobs_get(['desktop-sync', 'classification', *['source-cooldown:' + source for source in sources]])
        groups = grouped_records(rows)
        tags = {}
        favorites = 0
        folders = set()
        statuses = {'unread': 0, 'reading': 0, 'done': 0}
        for versions in groups:
            metas = [row['reader_state'] for row in versions]
            favorites += any(meta.get('starred') for meta in metas)
            folders.update(meta['folder'] for meta in metas if meta.get('folder'))
            statuses[metas[0].get('status', 'unread')] += 1
            work_tags = {tag for row, meta in zip(versions, metas) for tag in [*row['ai_tags'], *meta.get('tags', [])]}
            for tag in work_tags:
                tags[tag] = tags.get(tag, 0) + 1
        return {"total": len(groups), 'source_records': len(rows), "downloaded": sum(any(r['local_pdf_path'] for r in group) for group in groups), "starred": favorites,
                "tags": tags, "folders": sorted(folders), "statuses": statuses,
                "sync_job": jobs['desktop-sync'],
                'sync_cooldown': source_cooldowns.active(sources, jobs),
                "classification_job": jobs['classification'], "sync_runs": db.list_sync_runs(5)}

    @app.get('/desktop/papers/{paper_id}/versions')
    def source_versions(paper_id: int):
        paper = require_paper(paper_id)
        versions = db.work_versions(paper_id)
        for version in versions:
            version['normalized_doi'] = normalize_doi(version.get('doi'))
            version['reader_state'] = db.reader_get(version['id'], 'state')
            version['annotations_count'] = len(db.list_annotations(version['id']))
            version['has_translation'] = any(p.get('translated') for p in db.reader_get(version['id'], 'document').get('pages', []))
            version['has_research_card'] = bool(db.reader_get(version['id'], 'research-card'))
        return {'normalized_doi': normalize_doi(paper.get('doi')), 'items': versions,
                'note': '相同 DOI 归为一条文献，所有来源记录、PDF、译文与笔记独立保留。不同 PDF 的页码／段落可能不同，不自动搬移标注。'}

    def brief_data(specific_date, offset_minutes, page, page_size):
        day = specific_date or datetime.now(timezone(timedelta(minutes=offset_minutes))).date()
        return daily_brief(db, settings.topics(), day, offset_minutes, page, page_size)

    @app.get('/desktop/discovery/brief')
    def brief(specific_date: CalendarDate | None = Query(None, alias='date'),
              utc_offset_minutes: int = Query(0, ge=-840, le=840), page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100)):
        return brief_data(specific_date, utc_offset_minutes, page, page_size)

    @app.get('/desktop/discovery/brief.md')
    def export_brief(specific_date: CalendarDate | None = Query(None, alias='date'), utc_offset_minutes: int = Query(0, ge=-840, le=840)):
        data = brief_data(specific_date, utc_offset_minutes, 1, None)
        lines = [f"# 每日论文简报 · {data['date']}", '', data['note'], '',
                 f"新增文献 {data['total']} 篇，新增来源版本 {data['new_source_versions']} 条；本地导入 {data['local_imports']} 篇。", '']
        # One record snapshot, not repeated pagination over a changing library.
        for item in data['items']:
            lines.extend(['## ' + item['title'], '', f"来源：{', '.join(item['sources'])}；版本：{item['versions_count']}；文献 ID：{item['id']}",
                          'DOI：' + (item['normalized_doi'] or '未提供'), '匹配原因：' + '；'.join(item['match_reasons']), '',
                          'AI 摘要 · 请核对原文' if item['brief_is_ai'] else '来源摘要 · 非 AI 生成', item['brief_text'], ''])
        return Response('\n'.join(lines), media_type='text/markdown; charset=utf-8', headers={'Content-Disposition': f"attachment; filename=brief-{data['date']}.md"})

    @app.get("/desktop/papers/{paper_id}/state")
    def get_state(paper_id: int):
        require_paper(paper_id)
        return db.reader_get(paper_id, 'state')

    def save_index_job(job):
        with index_state_lock:
            saved = dict(job)
            if index_cancel.is_set() and saved['status'] in ('queued', 'running'):
                saved['status'] = 'cancelling'
            db.job_put('index-fulltext', saved)

    def index_cache_hash(cache):
        return hashlib.sha256(json.dumps(cache, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def completed_index_valid(pid, checkpoint):
        try:
            paper = db.get_paper(pid)
            doc = db.reader_get(pid, 'document')
            cache = db.reader_get(pid, 'search')
            return (isinstance(checkpoint, dict) and isinstance(paper, dict) and isinstance(doc, dict) and isinstance(cache, dict)
                    and isinstance(checkpoint.get('source_hash'), str)
                    and doc.get('source_hash') == checkpoint['source_hash']
                    and doc.get('extraction_checked_version', doc.get('parser_version')) == PARSER_VERSION
                    and doc.get('scientific_version') == SCIENTIFIC_VERSION and 'outline' in doc
                    and cache.get('index_version') == SEARCH_INDEX_VERSION and isinstance(cache.get('text'), str)
                    and index_cache_hash(cache) == checkpoint.get('search_hash')
                    and file_hash(pdf_path(settings, paper)) == checkpoint['source_hash'])
        except (OSError, ValueError, TypeError):
            return False

    def build_index(job):
        try:
            with db._lock:
                ids = [r[0] for r in db._connection.execute('SELECT id FROM papers WHERE local_pdf_path IS NOT NULL ORDER BY id')]
            checkpoints = job.get('completed', {})
            checkpoints = checkpoints if isinstance(checkpoints, dict) else {}
            completed = {str(pid): checkpoints[str(pid)] for pid in ids if str(pid) in checkpoints}
            errors = {}
            job.update(status='running', completed=completed, done=len(completed), total=len(ids),
                       failed_ids=[], error=None, updated_at=utc_now())
            save_index_job(job)
            for pid in ids:
                if index_cancel.is_set():
                    break
                sid = str(pid)
                if sid in completed and completed_index_valid(pid, completed[sid]):
                    continue
                completed.pop(sid, None)
                job.update(done=len(completed), paper_id_current=pid, updated_at=utc_now())
                save_index_job(job)
                try:
                    doc = service.document(pid)
                    db.refresh_search(pid)
                    completed[sid] = {'source_hash': doc['source_hash'], 'search_hash': index_cache_hash(db.reader_get(pid, 'search'))}
                except Exception as exc:
                    errors[sid] = f'文献 {pid}：{exc}'
                job.update(done=len(completed), failed_ids=[int(s) for s in errors],
                           error='; '.join(errors.values()) or None, updated_at=utc_now())
                save_index_job(job)
            job.update(status='paused' if index_cancel.is_set() else 'partial' if errors else 'done',
                       done=len(completed), updated_at=utc_now())
        except Exception as exc:
            job.update(status='failed', error=str(exc), updated_at=utc_now())
        finally:
            # Publishing terminal state and releasing admission are atomic to API callers.
            with index_state_lock:
                try:
                    save_index_job(job)
                finally:
                    index_lock.release()

    @app.post('/desktop/index')
    def start_index():
        with index_state_lock:
            if index_lock.acquire(blocking=False):
                job = db.job_get('index-fulltext')
                job.update(status='queued', updated_at=utc_now())
                index_cancel.clear()
                try:
                    save_index_job(job)
                    threading.Thread(target=build_index, args=(job,), daemon=True).start()
                except Exception:
                    index_lock.release()
                    raise
        return db.job_get('index-fulltext')

    @app.post('/desktop/index/cancel')
    def cancel_index():
        with index_state_lock:
            index_cancel.set()
            job = db.job_get('index-fulltext')
            if index_lock.locked() and job['status'] in ('queued', 'running', 'cancelling'):
                job.update(status='cancelling', updated_at=utc_now())
                db.job_put('index-fulltext', job)
            return job

    @app.patch("/desktop/papers/{paper_id}/state")
    def save_state(paper_id: int, state: ReaderState):
        require_paper(paper_id)
        with db._lock:
            current = db.reader_get(paper_id, "state")
            updates = state.model_dump(exclude_none=True)
            # Automatic reading activity must consult the latest stored status
            # atomically; a delayed scroll save cannot undo manual completion.
            if 'last_page' in updates and 'status' not in updates and current.get('status') != 'done':
                updates['status'] = 'reading'
            current.update(updates)
            current["updated_at"] = utc_now()
            db.reader_put(paper_id, "state", current)
        return current

    @app.get("/desktop/papers/{paper_id}/document")
    def document(paper_id: int):
        require_paper(paper_id)
        try:
            doc = service.document(paper_id)
            return {**doc, 'translation_status': translation_cache_status(doc, db.reader_get(paper_id, 'editor')),
                    'reading_quality': reading_quality(doc)}
        except Exception as exc:
            raise HTTPException(422, f"无法解析 PDF：{exc}") from exc

    @app.post('/desktop/papers/{paper_id}/translation/local-repair')
    def repair_local_translation(paper_id: int, request: LocalTranslationRepair | None = None):
        require_paper(paper_id)
        try:
            return service.repair_local_translation(paper_id, (request or LocalTranslationRepair()).page_number)
        except ReaderBusyError as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/desktop/papers/{paper_id}/translation", status_code=202)
    def start_translation(paper_id: int, request: TranslationScope | None = None):
        paper = require_paper(paper_id)
        if not paper.get("local_pdf_path"):
            raise HTTPException(409, "请先下载 PDF")
        try:
            return service.start(paper_id, **(request or TranslationScope()).model_dump())
        except ReaderBusyError as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post('/desktop/papers/{paper_id}/scientific/reanalyse')
    def reanalyse_scientific(paper_id: int):
        require_paper(paper_id)
        try:
            return service.document(paper_id, force_scientific=True)
        except ReaderBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(422, f'复杂内容重新检测失败，原内容仍保留：{exc}') from exc

    @app.get('/desktop/complex/runtime')
    def complex_runtime(ocr: bool = False):
        return complex_parser.runtime(ocr)

    @app.get('/desktop/papers/{paper_id}/complex')
    def complex_status(paper_id: int):
        require_paper(paper_id)
        try:
            return {'job': db.job_get(f'complex:{paper_id}'), 'preview': complex_parser.preview(paper_id)}
        except ReaderBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post('/desktop/papers/{paper_id}/complex', status_code=202)
    def start_complex(paper_id: int, value: ComplexParseRequest):
        require_paper(paper_id)
        try:
            return complex_parser.start(paper_id, **value.model_dump())
        except ReaderBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post('/desktop/papers/{paper_id}/complex/cancel')
    def cancel_complex(paper_id: int):
        require_paper(paper_id)
        return complex_parser.cancel(paper_id)

    @app.post('/desktop/papers/{paper_id}/complex/table-link')
    def confirm_complex_link(paper_id: int, value: ComplexLinkRequest):
        require_paper(paper_id)
        try:
            return complex_parser.confirm_link(paper_id, **value.model_dump())
        except ReaderBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/desktop/papers/{paper_id}/translation")
    def translation_job(paper_id: int):
        require_paper(paper_id)
        return db.job_get(f"translate:{paper_id}")

    @app.get('/desktop/papers/{paper_id}/reading-source/{page}')
    def review_reading(paper_id: int, page: int):
        require_paper(paper_id)
        try:
            return reviewed_reading.review(paper_id, page)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post('/desktop/papers/{paper_id}/reading-source/{page}/adopt')
    def adopt_reading(paper_id: int, page: int, value: AdoptReadingRequest):
        require_paper(paper_id)
        try:
            return reviewed_reading.adopt(paper_id, page, **value.model_dump())
        except ReaderBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post('/desktop/papers/{paper_id}/reading-source/{page}/switch')
    def switch_reading(paper_id: int, page: int, value: SwitchReadingRequest):
        require_paper(paper_id)
        try:
            return reviewed_reading.switch(paper_id, page, **value.model_dump())
        except ReaderBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/desktop/papers/{paper_id}/translation/cancel")
    def cancel_translation(paper_id: int):
        require_paper(paper_id)
        return service.cancel(paper_id)

    @app.post("/desktop/papers/{paper_id}/translate-selection")
    def translate_selection(paper_id: int, request: SnippetRequest):
        require_paper(paper_id)
        try:
            binding = request.model_dump(exclude={'text'}, exclude_none=True)
            return service.translate_snippet(paper_id, request.text, **binding)
        except ReaderBusyError as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/desktop/papers/{paper_id}/editor")
    def get_editor(paper_id: int):
        require_paper(paper_id)
        editor = db.reader_get(paper_id, "editor")
        if editor.get("edits"):
            doc = service.document(paper_id)
            for page in doc["pages"]:
                for block in page["blocks"]:
                    if edit := editor["edits"].get(block["id"]):
                        edit["conflict"] = edit["base"] != hashlib.sha256(page["translated"].get(block["id"], "").encode()).hexdigest()
                        edit["warnings"] = quality_warnings(block["en"], edit["zh"])
        return editor

    @app.patch("/desktop/papers/{paper_id}/editor")
    def edit_translation(paper_id: int, value: ReaderEditor):
        require_paper(paper_id)
        with service._lock, service._extract_lock:
            editor = db.reader_get(paper_id, "editor")
            if value.glossary is not None:
                if any(not term.get("en", "").strip() or not term.get("zh", "").strip() or len(term.get("en", "")) > 200 or len(term.get("zh", "")) > 200 for term in value.glossary):
                    raise HTTPException(422, "术语需要填写英文和中文，每项不超过 200 字")
                editor["glossary"] = [{"en": t["en"].strip(), "zh": t["zh"].strip()} for t in value.glossary]
            if value.block_id:
                doc = service.document(paper_id)
                matching = [(p, b) for p in doc["pages"] for b in p["blocks"] if b["id"] == value.block_id]
                if not matching or value.zh is None:
                    raise HTTPException(422, "段落不存在或没有译文内容")
                page, block = matching[0]
                base = hashlib.sha256(page["translated"].get(value.block_id, "").encode()).hexdigest()
                editor.setdefault("edits", {})[value.block_id] = {"zh": value.zh, "base": base, "at": utc_now()}
            db.reader_put(paper_id, "editor", editor)
            return editor

    @app.get("/desktop/drafts")
    def get_drafts():
        with db._lock:
            rows = db._connection.execute("SELECT paper_id,payload FROM reader_data WHERE kind='draft'").fetchall()
        return {str(row[0]): json.loads(row[1]).get("text", "") for row in rows}

    @app.post("/desktop/drafts")
    def save_drafts(value: DraftsRequest):
        if any(not k.isdigit() or len(v) > 18000 for k, v in value.drafts.items()):
            raise HTTPException(422, "草稿格式或长度不正确")
        for pid, text in value.drafts.items():
            if db.get_paper(int(pid)):
                db.reader_put(int(pid), "draft", {"text": text, "at": utc_now()})
        return {"saved": True}

    @app.post("/desktop/papers/{paper_id}/annotations", status_code=201)
    def annotation(paper_id: int, value: DesktopAnnotation):
        require_paper(paper_id)
        try:
            return db.add_annotation_once(paper_id, value.client_id, value.model_dump(exclude={"client_id"}))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/desktop/papers/{paper_id}/annotations/{annotation_id}", status_code=204)
    def delete_annotation(paper_id: int, annotation_id: int):
        require_paper(paper_id)
        db.delete_annotation(paper_id, annotation_id)
        return Response(status_code=204)

    @app.get("/desktop/papers/{paper_id}/notes.md")
    def export_notes(paper_id: int):
        paper = require_paper(paper_id)
        lines = [f"# {paper['title']}", "", f"来源：{paper.get('abs_url') or paper['source']}", ""]
        for note in db.list_annotations(paper_id):
            lines.extend([f"## 第 {note['page_number'] + 1} 页 · {note['annotation_type']}", note.get("text") or "（原页图形标注）", ""])
        return Response("\n".join(lines), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="paper-{paper_id}-notes.md"'})

    @app.post("/desktop/import", status_code=201)
    def import_pdf(file: UploadFile = File(...)):
        directory = settings.papers_dir / "local"
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f"{secrets.token_hex(16)}.upload"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("wb") as target:
                while chunk := file.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > 100 * 1024 * 1024:
                        raise HTTPException(413, "PDF 不能超过 100 MB")
                    digest.update(chunk)
                    target.write(chunk)
            with temporary.open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise HTTPException(422, "请选择有效的 PDF 文件")
            try:
                with pdfplumber.open(temporary) as pdf:
                    if not pdf.pages:
                        raise ValueError("没有页面")
            except Exception as exc:
                raise HTTPException(422, "PDF 损坏或已加密，无法打开") from exc
            source_id = digest.hexdigest()
            path = directory / f"{source_id}.pdf"
            temporary.replace(path)
            relative = str(path.relative_to(settings.root_dir)) if path.is_relative_to(settings.root_dir) else str(path)
            title = Path((file.filename or "本地论文.pdf").replace("\\", "/")).stem[:300]
            # Re-importing the same bytes preserves an existing title and all user data.
            with db._lock:
                existing = db._connection.execute("SELECT id FROM papers WHERE source='local' AND source_id=?", (source_id,)).fetchone()
                if existing:
                    paper_id = existing[0]
                    db.update_local_pdf_path(paper_id, relative)
                else:
                    paper_id, _ = db.upsert_paper({"source": "local", "source_id": source_id, "title": title,
                                                  "local_pdf_path": relative, "ai_tags": ["本地导入"]})
            return require_paper(paper_id)
        finally:
            temporary.unlink(missing_ok=True)
            file.file.close()

    @app.get("/desktop/config")
    def get_config():
        return service.config(public=True)

    @app.put("/desktop/config")
    def put_config(value: DesktopConfig):
        try:
            return service.save_config(value.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/desktop/chatgpt/status')
    def chatgpt_status():
        try: return service.chatgpt_login.status()
        except ChatGPTAuthError as exc:
            raise HTTPException(422, {'message': str(exc), 'code': exc.code}) from None

    def admit_chatgpt_change():
        if service._active:
            raise HTTPException(409, '翻译仍在执行，请暂停并等待当前请求结束后再切换 ChatGPT 连接')

    @app.post('/desktop/chatgpt/login')
    def chatgpt_login(value: ChatGPTLoginRequest):
        with service._lock:
            admit_chatgpt_change()
            try:
                return service.chatgpt_login.start(registration_id=value.registration_id, reconsent=value.reconsent)
            except (ValueError, OSError):
                raise HTTPException(422, '无法启动 ChatGPT 登录，请检查系统浏览器和网络后重试') from None

    @app.post('/desktop/chatgpt/cancel')
    def chatgpt_cancel():
        return service.chatgpt_login.cancel()

    @app.post('/desktop/chatgpt/select')
    def chatgpt_select(value: ChatGPTSelection):
        with service._lock:
            admit_chatgpt_change()
            service.chatgpt_login.cancel()
            try: return service.chatgpt_auth.select(value.registration_id)
            except ChatGPTAuthError as exc:
                raise HTTPException(422, {'message': str(exc), 'code': exc.code}) from None

    @app.post('/desktop/chatgpt/logout')
    def chatgpt_logout():
        with service._lock:
            admit_chatgpt_change()
            service.chatgpt_login.cancel()
            return service.chatgpt_auth.logout()

    @app.post('/desktop/chatgpt/usage')
    def chatgpt_usage():
        import webbrowser
        if not webbrowser.open('https://chatgpt.com/settings/usage'):
            raise HTTPException(422, '无法打开系统浏览器，请在 ChatGPT 设置中查看用量')
        return {'opened': True}

    @app.post('/desktop/chatgpt/welcome')
    def chatgpt_welcome(value: ChatGPTSelection):
        try: return service.chatgpt_auth.acknowledge_welcome(value.registration_id)
        except ChatGPTAuthError as exc:
            raise HTTPException(409, {'message': str(exc), 'code': exc.code}) from None

    def probe_config(value: DesktopConfig | None) -> dict:
        cfg = service.config()
        if value:
            changed = value.provider != cfg["provider"] or value.base_url != cfg["base_url"]
            values = value.model_dump(exclude_none=True)
            cfg.update(values)
            if (changed and value.api_key is None) or value.clear_api_key:
                cfg["api_key"] = ""
        if cfg['provider'] == 'chatgpt':
            status = service.chatgpt_auth.status()
            cfg.update(api_key='', chatgpt_account_id=status['active'], chatgpt_generation=status['generation'])
        return cfg

    @app.post("/desktop/model/models")
    def list_models(value: DesktopConfig | None = None):
        try:
            cfg = probe_config(value)
            client = service.model_client(cfg) if cfg['provider'] == 'chatgpt' else ModelClient(cfg)
            if isinstance(client, ChatGPTModelClient):
                catalog = client.catalog()
                return {'models': [entry['id'] for entry in catalog], 'model_catalog': catalog, 'base_url': cfg['base_url']}
            return {"models": client.models(), "base_url": cfg["base_url"]}
        except ModelError as exc:
            raise HTTPException(502, {"message": str(exc), "code": exc.code}) from exc

    @app.post("/desktop/model/test")
    def test_model(value: DesktopConfig | None = None):
        try:
            cfg = probe_config(value)
            client = service.model_client(cfg) if cfg['provider'] == 'chatgpt' else ModelClient(cfg)
            result = client.generate("将下面一句话忠实翻译成简体中文，只输出译文：This study evaluates three methods using 120 samples [1].")
            return {"ok": True, "translated_text": result.text, "model": cfg["model"], "usage": result.usage, "elapsed": result.elapsed}
        except ModelError as exc:
            raise HTTPException(502, {"message": str(exc), "code": exc.code}) from exc

    @app.get("/desktop/backups")
    def list_backups():
        return {"items": backups.list(), "job": db.job_get("auto-backup")}

    @app.post("/desktop/backups")
    def create_backup():
        db.job_put("manual-backup", {"status": "running", "updated_at": utc_now()})
        try:
            result = backups.create()
            db.job_put("manual-backup", {"status": "done", "updated_at": utc_now(), "name": result["name"]})
            return result
        except (ValueError, OSError) as exc:
            db.job_put("manual-backup", {"status": "failed", "updated_at": utc_now(), "error": str(exc)})
            raise HTTPException(422, str(exc)) from exc

    @app.post("/desktop/backups/import")
    def import_backup(file: UploadFile = File(...)):
        try:
            return backups.import_file(file.file)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            file.file.close()

    @app.get("/desktop/tasks")
    def list_tasks():
        with db._lock:
            rows = db._connection.execute("SELECT job_key,payload FROM desktop_jobs").fetchall()
        items = []
        for row in rows:
            key, job = row[0], json.loads(row[1])
            if key.startswith('source-cooldown:'):
                continue  # Derived gate; the user-facing sync job carries its error/deadline.
            paper_job = key.startswith(('translate:', 'complex:'))
            paper = db.get_paper(int(key.split(":", 1)[1])) if paper_job else None
            if paper_job and not paper:
                continue
            title = paper["title"] if paper else {"desktop-sync": "论文采集", "classification": "AI 摘要与分类", "manual-backup": "手动备份", "auto-backup": "每日备份", "index-fulltext": "全文检索索引", "scheduler-error": "计划任务异常"}.get(key, key)
            if key.startswith('complex:'):
                title = '增强解析 · ' + title
            # Durable per-file fingerprints are internal; don't resend a large map on every UI poll.
            public_job = {field: value for field, value in job.items() if field != 'completed'}
            items.append({"key": key, "title": title, "paper_id": paper["id"] if paper else None, **public_job})
        return {"items": sorted(items, key=lambda item: item.get("updated_at", ""), reverse=True)[:200]}

    @app.get("/desktop/backups/{name}/download")
    def download_backup(name: str):
        try:
            return FileResponse(backups.resolve(name), media_type="application/zip", filename=name)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/desktop/backups/{name}/verify")
    def verify_backup(name: str):
        try:
            return backups.verify(name)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/desktop/backups/restore")
    def restore_backup(value: BackupRestore):
        with service._lock:
            if service._active or complex_parser._active or classifier._active is not None or sync_lock.locked() or index_lock.locked():
                raise HTTPException(409, "请先暂停翻译并等待采集完成，再恢复备份")
            app.state.maintenance = True
        try:
            with service._extract_lock:
                return backups.restore(value.name)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            app.state.maintenance = False

    @app.get("/desktop/topics")
    def get_topics():
        return settings.topics().get("sources", {}).get("arxiv", {})

    @app.put("/desktop/topics")
    def put_topics(value: TopicsUpdate):
        topics = settings.topics()
        topics.setdefault("sources", {}).setdefault("arxiv", {}).update(value.model_dump())
        settings.topics_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = settings.topics_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(topics, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(settings.topics_path)
        return get_topics()

    @app.get('/desktop/classification')
    def classification_status():
        return classifier.status()

    @app.post('/desktop/classification/preview')
    def classification_preview():
        return classifier.preview()

    @app.post('/desktop/classification/consent')
    def classification_consent(value: ClassificationConsent):
        try:
            return classifier.authorize(**value.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/desktop/classification/run', status_code=202)
    def classification_run():
        with service._lock:
            if not classifier.status()['enabled']:
                raise HTTPException(409, '请先核对发送范围并授权摘要与分类')
            return classifier.start()

    def run_sync(value: DesktopSync):
        try:
            fn = {"arxiv": sync_arxiv, "openalex": sync_openalex, "all": sync_all}[value.source]
            # Desktop classification uses only the explicitly authorized saved
            # model, never an unrelated inherited OLLAMA_URL. CLI remains intact.
            result = fn(replace(settings, ollama_url=None), db, max_results=value.max_results)
            db.job_put("desktop-sync", {"status": "done", "result": result, "updated_at": utc_now()})
            try:
                classifier.start()
            except Exception:
                db.job_put('classification', {'status': 'paused', 'code': 'initialization',
                    'error': '采集已完成，但分类未启动；请检查连接后重新授权', 'updated_at': utc_now()})
        except SourceError as exc:
            cooldown = source_cooldowns.record(exc)
            if cooldown:
                db.job_put('desktop-sync', source_cooldowns.failed_job(cooldown, value.source))
            else:
                db.job_put('desktop-sync', {'status': 'failed', 'code': 'source_http_error' if exc.status else 'source_error',
                    'http_status': exc.status, 'source': value.source, 'error': str(exc), 'updated_at': utc_now()})
        except Exception as exc:
            db.job_put("desktop-sync", {"status": "failed", "error": str(exc), "updated_at": utc_now()})
        finally:
            sync_lock.release()

    def start_sync(value: DesktopSync):
        if sync_lock.acquire(blocking=False):
            handed_off = False
            try:
                cooldown = source_cooldowns.active(requested_sources(value.source))
                if cooldown:
                    db.job_put('desktop-sync', source_cooldowns.failed_job(cooldown, value.source))
                    return {**db.job_get('desktop-sync'), 'attempt_started': False}
                db.job_put("desktop-sync", {"status": "running", "source": value.source, "updated_at": utc_now()})
                threading.Thread(target=run_sync, args=(value,), daemon=True).start()
                handed_off = True
                return {**db.job_get('desktop-sync'), 'attempt_started': True}
            finally:
                if not handed_off:
                    sync_lock.release()
        return {**db.job_get("desktop-sync"), 'attempt_started': False}

    @app.post("/desktop/sync", status_code=202)
    def sync(value: DesktopSync):
        return start_sync(value)

    def scheduler_tick(now: datetime | None = None):
        # Exposed on app.state only for deterministic scheduler tests, not as an HTTP API.
        with service._lock:
            if app.state.maintenance:
                return
            cfg = service.config()
            current = now or datetime.now()
            today = current.strftime("%Y-%m-%d")
            if cfg["daily_enabled"] and current.strftime("%H:%M") >= cfg["daily_time"]:
                if db.job_get("daily-sync").get("date") != today and not sync_lock.locked():
                    job = start_sync(DesktopSync(source=cfg["sync_source"]))
                    if job.get('attempt_started'):
                        db.job_put("daily-sync", {"date": today})
            if cfg["auto_backup"] and db.job_get("auto-backup").get("date") != today:
                db.job_put("auto-backup", {"status": "running", "updated_at": utc_now()})
                result = backups.create(automatic=True)
                db.job_put("auto-backup", {"date": today, "status": "done", "updated_at": utc_now(), **result})

    app.state.scheduler_tick = scheduler_tick

    def scheduler():
        while not stop.wait(30):
            try:
                scheduler_tick()
            except Exception as exc:
                db.job_put("scheduler-error", {"status": "failed", "error": str(exc), "updated_at": utc_now()})

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def desktop_lifespan(application):
        async with previous_lifespan(application):
            stop.clear()
            threading.Thread(target=scheduler, daemon=True).start()
            try:
                yield
            finally:
                stop.set()
                index_cancel.set()
                service.shutdown()

    app.router.lifespan_context = desktop_lifespan

    app.mount("/assets", StaticFiles(directory=Path(__file__).parent / "web"), name="desktop-assets")
    return service
