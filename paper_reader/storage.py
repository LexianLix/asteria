from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from .identity import work_key
from . import __version__

SCHEMA_VERSION = 1
SEARCH_INDEX_VERSION = 1


class DatabaseCompatibilityError(ValueError):
    """A startup/recovery refusal safe to display without document contents."""


def check_schema_version(connection):
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    if not 0 <= version <= SCHEMA_VERSION:
        raise DatabaseCompatibilityError(f'文献库版本 {version} 超出本程序支持范围 0–{SCHEMA_VERSION}；请使用兼容的新程序，未修改文献库')
    return version


def execute_schema(connection, script):
    """Unlike executescript, do not implicitly commit the migration transaction."""
    statement = ''
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ''
    if statement.strip():
        raise ValueError('数据库迁移 SQL 缺少完整语句，未提交')


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Small SQLite wrapper for the single-user local installation."""

    def __init__(self, path: Path):
        path = Path(path).absolute()
        # Refuse a future database before WAL, initialization or search-index writes.
        if path.is_file():
            # Even mode=ro may create WAL/SHM files. Reject a future main-header
            # version first; still probe SQLite afterwards to see committed live WAL.
            with path.open('rb') as stream:
                header = stream.read(100)
            if header.startswith(b'SQLite format 3\x00') and len(header) >= 64:
                version = int.from_bytes(header[60:64], 'big', signed=True)
                if not 0 <= version <= SCHEMA_VERSION:
                    raise DatabaseCompatibilityError(f'文献库版本 {version} 超出本程序支持范围 0–{SCHEMA_VERSION}；请使用兼容的新程序，未修改文献库')
            probe = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            try:
                check_schema_version(probe)
            finally:
                probe.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.create_function('paper_work_key', 2, work_key, deterministic=True)
        self.migration_snapshot = None
        try:
            check_schema_version(self._connection)
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self.initialize()
            self.rebuild_search(only_stale=True)
        except Exception:
            self._connection.close()
            raise

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS papers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            doi TEXT,
            title TEXT NOT NULL,
            abstract TEXT NOT NULL DEFAULT '',
            authors_json TEXT NOT NULL DEFAULT '[]',
            published_at TEXT,
            updated_at TEXT,
            categories_json TEXT NOT NULL DEFAULT '[]',
            pdf_url TEXT,
            abs_url TEXT,
            license TEXT,
            local_pdf_path TEXT,
            ai_summary_zh TEXT,
            ai_tags_json TEXT NOT NULL DEFAULT '[]',
            ai_score REAL,
            created_at TEXT NOT NULL,
            updated_local_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(source, source_id)
        );
        CREATE INDEX IF NOT EXISTS idx_papers_published ON papers(published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);

        CREATE TABLE IF NOT EXISTS sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            fetched INTEGER NOT NULL DEFAULT 0,
            inserted INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0,
            downloaded INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            page_number INTEGER NOT NULL,
            annotation_type TEXT NOT NULL,
            color TEXT,
            text TEXT,
            x REAL,
            y REAL,
            width REAL,
            height REAL,
            path_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_annotations_paper ON annotations(paper_id, page_number);

        CREATE TABLE IF NOT EXISTS translations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            source_hash TEXT NOT NULL,
            target_lang TEXT NOT NULL,
            source_text TEXT NOT NULL,
            translated_text TEXT NOT NULL,
            model TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(paper_id, source_hash, target_lang)
        );
        CREATE TABLE IF NOT EXISTS reader_data (
            paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY(paper_id, kind)
        );
        CREATE TABLE IF NOT EXISTS desktop_jobs (
            job_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS annotation_ops (
            op_id TEXT PRIMARY KEY,
            paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            annotation_id INTEGER,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        """
        with self._lock:
            version = check_schema_version(self._connection)
            if version == SCHEMA_VERSION:
                return
            snapshot = None
            self._connection.execute('BEGIN IMMEDIATE')
            try:
                # Another process may have completed the same migration while we waited.
                if check_schema_version(self._connection) == SCHEMA_VERSION:
                    self._connection.rollback()
                    return
                tables = {r[0] for r in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                if tables:
                    if 'papers' not in tables:
                        raise ValueError('已有数据库不是可识别的文献库，未执行迁移')
                    snapshot = self._migration_backup(version)
                execute_schema(self._connection, schema)
                self._migrate_v1(snapshot)
                if self._connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or self._connection.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('迁移后数据库完整性检查失败，已回滚')
                self._connection.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
                self._connection.commit()
                self.migration_snapshot = snapshot
            except Exception:
                self._connection.rollback()
                raise

    def _migration_backup(self, version):
        """Hold the write reservation; a separate read connection snapshots committed data."""
        directory = self.path.parent / 'migration-snapshots'
        directory.mkdir(exist_ok=True)
        target = directory / f'schema-{version}-to-{SCHEMA_VERSION}-{uuid.uuid4().hex}.sqlite'
        source = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or destination.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('迁移前快照验证失败，未执行迁移')
            if destination.execute('PRAGMA user_version').fetchone()[0] != version:
                raise ValueError('迁移前快照版本不一致，未执行迁移')
            return str(target)
        finally:
            destination.close(); source.close()

    def _migrate_v1(self, snapshot):
        self._connection.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, app_version TEXT NOT NULL, migrated_at TEXT NOT NULL, recovery_snapshot TEXT)')
        self._connection.execute('INSERT INTO schema_migrations VALUES(?,?,?,?)', (1, __version__, utc_now(), snapshot))

    def schema_version(self):
        with self._lock:
            return check_schema_version(self._connection)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def upsert_paper(self, paper: dict[str, Any]) -> tuple[int, bool]:
        now = utc_now()
        values = {
            "source": paper["source"],
            "source_id": paper["source_id"],
            "doi": paper.get("doi"),
            "title": paper.get("title", "").strip(),
            "abstract": paper.get("abstract", "").strip(),
            "authors_json": json.dumps(paper.get("authors", []), ensure_ascii=False),
            "published_at": paper.get("published_at"),
            "updated_at": paper.get("updated_at"),
            "categories_json": json.dumps(paper.get("categories", []), ensure_ascii=False),
            "pdf_url": paper.get("pdf_url"),
            "abs_url": paper.get("abs_url"),
            "license": paper.get("license"),
            "local_pdf_path": paper.get("local_pdf_path"),
            "ai_summary_zh": paper.get("ai_summary_zh"),
            "ai_tags_json": json.dumps(paper.get("ai_tags", []), ensure_ascii=False),
            "ai_score": paper.get("ai_score"),
        }
        with self._lock:
            existing = self._connection.execute(
                "SELECT id FROM papers WHERE source = ? AND source_id = ?",
                (values["source"], values["source_id"]),
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO papers (
                    source, source_id, doi, title, abstract, authors_json,
                    published_at, updated_at, categories_json, pdf_url, abs_url,
                    license, local_pdf_path, ai_summary_zh, ai_tags_json, ai_score,
                    created_at, updated_local_at, last_seen_at
                ) VALUES (
                    :source, :source_id, :doi, :title, :abstract, :authors_json,
                    :published_at, :updated_at, :categories_json, :pdf_url, :abs_url,
                    :license, :local_pdf_path, :ai_summary_zh, :ai_tags_json, :ai_score,
                    :created_at, :updated_local_at, :last_seen_at
                )
                ON CONFLICT(source, source_id) DO UPDATE SET
                    doi = COALESCE(excluded.doi, papers.doi),
                    title = excluded.title,
                    abstract = excluded.abstract,
                    authors_json = excluded.authors_json,
                    published_at = COALESCE(excluded.published_at, papers.published_at),
                    updated_at = COALESCE(excluded.updated_at, papers.updated_at),
                    categories_json = excluded.categories_json,
                    pdf_url = COALESCE(excluded.pdf_url, papers.pdf_url),
                    abs_url = COALESCE(excluded.abs_url, papers.abs_url),
                    license = COALESCE(excluded.license, papers.license),
                    local_pdf_path = COALESCE(excluded.local_pdf_path, papers.local_pdf_path),
                    ai_summary_zh = COALESCE(excluded.ai_summary_zh, papers.ai_summary_zh),
                    ai_tags_json = CASE WHEN excluded.ai_tags_json = '[]' THEN papers.ai_tags_json ELSE excluded.ai_tags_json END,
                    ai_score = COALESCE(excluded.ai_score, papers.ai_score),
                    updated_local_at = excluded.updated_local_at,
                    last_seen_at = excluded.last_seen_at
                """,
                {**values, "created_at": now, "updated_local_at": now, "last_seen_at": now},
            )
            row = self._connection.execute(
                "SELECT id FROM papers WHERE source = ? AND source_id = ?",
                (values["source"], values["source_id"]),
            ).fetchone()
            self._connection.commit()
            return int(row["id"]), existing is None

    @staticmethod
    def _decode_paper(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result.pop('work_rank', None)
        for field, fallback in (
            ("authors_json", []),
            ("categories_json", []),
            ("ai_tags_json", []),
        ):
            raw = result.pop(field, None)
            try:
                result[field.removesuffix("_json")] = json.loads(raw or "null") or fallback
            except json.JSONDecodeError:
                result[field.removesuffix("_json")] = fallback
        return result

    def get_paper(self, paper_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
            return self._decode_paper(row)

    def list_papers(
        self,
        *,
        offset: int = 0,
        limit: int = 30,
        query: str | None = None,
        tag: str | None = None,
        only_downloaded: bool = False,
        starred: bool = False,
        status: str | None = None,
        folder: str | None = None,
        group_doi: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        args: list[Any] = []
        if query:
            needle = '%' + query.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            clauses.append("""(title LIKE ? ESCAPE '\\' OR abstract LIKE ? ESCAPE '\\' OR authors_json LIKE ? ESCAPE '\\'
                OR EXISTS (SELECT 1 FROM reader_data r WHERE r.paper_id=papers.id AND r.kind='search' AND json_extract(r.payload,'$.text') LIKE ? ESCAPE '\\')
                OR EXISTS (SELECT 1 FROM annotations a WHERE a.paper_id=papers.id AND a.text LIKE ? ESCAPE '\\'))""")
            args.extend([needle] * 5)
        if tag:
            clauses.append("""(EXISTS (SELECT 1 FROM json_each(papers.ai_tags_json) t WHERE t.value=?) OR EXISTS
                (SELECT 1 FROM reader_data r,json_each(r.payload,'$.tags') t WHERE r.paper_id=papers.id AND r.kind='state' AND t.value=?))""")
            args.extend([tag, tag])
        if only_downloaded:
            clauses.append("local_pdf_path IS NOT NULL")
        if starred:
            clauses.append("EXISTS (SELECT 1 FROM reader_data r WHERE r.paper_id=papers.id AND r.kind='state' AND json_extract(r.payload,'$.starred')=1)")
        if status:
            clauses.append("COALESCE((SELECT json_extract(r.payload,'$.status') FROM reader_data r WHERE r.paper_id=papers.id AND r.kind='state'),'unread')=?")
            args.append(status)
        if folder:
            clauses.append("EXISTS (SELECT 1 FROM reader_data r WHERE r.paper_id=papers.id AND r.kind='state' AND json_extract(r.payload,'$.folder')=?)")
            args.append(folder)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            if group_doi:
                # Apply filters before grouping: a note found in another version stays reachable.
                total = self._connection.execute(f'SELECT COUNT(DISTINCT paper_work_key(doi,id)) FROM papers{where}', args).fetchone()[0]
                # Select only representative IDs first, instead of window-sorting full abstracts.
                cte = f"WITH representatives AS (SELECT MIN(papers.id) AS id FROM papers{where} GROUP BY paper_work_key(doi,id))"
                rows = self._connection.execute(cte + ' SELECT papers.* FROM papers JOIN representatives ON papers.id=representatives.id ORDER BY COALESCE(published_at,updated_local_at) DESC,papers.id ASC LIMIT ? OFFSET ?', [*args, limit, offset]).fetchall()
                return [self._decode_paper(row) for row in rows], int(total)
            total = self._connection.execute(f"SELECT COUNT(*) FROM papers{where}", args).fetchone()[0]
            rows = self._connection.execute(
                f"SELECT * FROM papers{where} ORDER BY COALESCE(published_at, updated_local_at) DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            return [self._decode_paper(row) for row in rows], int(total)

    def work_versions(self, paper_id: int) -> list[dict]:
        with self._lock:
            paper = self.get_paper(paper_id)
            if not paper:
                return []
            rows = self._connection.execute('SELECT * FROM papers WHERE paper_work_key(doi,id)=? ORDER BY id',
                                            (work_key(paper['doi'], paper_id),)).fetchall()
            return [self._decode_paper(row) for row in rows]

    def work_records(self) -> list[dict]:
        with self._lock:
            return [self._decode_paper(row) for row in self._connection.execute('SELECT * FROM papers ORDER BY id')]

    def overview_records(self) -> list[dict]:
        """One consistent, narrow metadata snapshot; do not load abstracts or documents."""
        with self._lock:
            rows = self._connection.execute("""SELECT p.id,p.doi,p.local_pdf_path,p.ai_tags_json,
                r.payload AS reader_state_payload FROM papers p
                LEFT JOIN reader_data r ON r.paper_id=p.id AND r.kind='state' ORDER BY p.id""").fetchall()
        records = []
        for row in rows:
            paper = self._decode_paper(row)
            paper['reader_state'] = json.loads(paper.pop('reader_state_payload') or '{}')
            records.append(paper)
        return records

    def work_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute('SELECT paper_work_key(doi,id),COUNT(*) FROM papers GROUP BY paper_work_key(doi,id)')
            return dict(rows)

    def update_ai(self, paper_id: int, *, summary_zh: str | None, tags: list[str], score: float | None) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE papers SET ai_summary_zh = ?, ai_tags_json = ?, ai_score = ?, updated_local_at = ? WHERE id = ?",
                (summary_zh, json.dumps(tags, ensure_ascii=False), score, utc_now(), paper_id),
            )
            self._connection.commit()

    def update_local_pdf_path(self, paper_id: int, path: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE papers SET local_pdf_path = ?, updated_local_at = ? WHERE id = ?",
                (path, utc_now(), paper_id),
            )
            self._connection.commit()

    def start_sync(self, source: str) -> int:
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO sync_runs(source, started_at) VALUES (?, ?)", (source, utc_now())
            )
            self._connection.commit()
            return int(cursor.lastrowid)

    def finish_sync(self, run_id: int, **fields: Any) -> None:
        allowed = {"fetched", "inserted", "updated", "downloaded", "failed", "error"}
        values = {key: value for key, value in fields.items() if key in allowed}
        values["finished_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._lock:
            self._connection.execute(
                f"UPDATE sync_runs SET {assignments} WHERE id = ?",
                [*values.values(), run_id],
            )
            self._connection.commit()

    def list_sync_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def add_annotation(self, paper_id: int, annotation: dict[str, Any], *, _commit: bool = True) -> dict[str, Any]:
        now = utc_now()
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT INTO annotations(
                    paper_id, page_number, annotation_type, color, text,
                    x, y, width, height, path_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    paper_id,
                    annotation["page_number"],
                    annotation["annotation_type"],
                    annotation.get("color"),
                    annotation.get("text"),
                    annotation.get("x"),
                    annotation.get("y"),
                    annotation.get("width"),
                    annotation.get("height"),
                    json.dumps(annotation.get("path", []), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            if _commit:
                self._connection.commit()
            return self.get_annotation(int(cursor.lastrowid)) or {}

    def get_annotation(self, annotation_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["path"] = json.loads(result.pop("path_json") or "[]")
        return result

    def reader_get(self, paper_id: int, kind: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM reader_data WHERE paper_id=? AND kind=?", (paper_id, kind)
            ).fetchone()
        return json.loads(row[0]) if row else {}

    def reader_put(self, paper_id: int, kind: str, payload: dict[str, Any]) -> None:
        self.reader_put_many(paper_id, {kind: payload})

    def reader_put_many(self, paper_id: int, payloads: dict[str, dict[str, Any]]) -> None:
        """Commit a source switch, its history and search index as one transaction."""
        encoded = {kind: json.dumps(value, ensure_ascii=False) for kind, value in payloads.items()}
        with self._lock:
            try:
                for kind, value in encoded.items():
                    self._connection.execute(
                        "INSERT INTO reader_data VALUES(?,?,?) ON CONFLICT(paper_id,kind) DO UPDATE SET payload=excluded.payload",
                        (paper_id, kind, value),
                    )
                if {'document', 'editor'} & payloads.keys():
                    self._index_paper(paper_id)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _index_paper(self, paper_id: int) -> None:
        """Search only actual source/translation text, not IDs, hashes or geometry."""
        document = self.reader_get(paper_id, 'document')
        editor = self.reader_get(paper_id, 'editor')
        texts = []
        for page in document.get('pages', []):
            texts.extend(b.get('en', '') for b in page.get('blocks', []))
            texts.extend(page.get('translated', {}).values())
            for region in page.get('scientific_regions', []):
                texts.append(region.get('text', ''))
        texts.extend(edit.get('zh', '') for edit in editor.get('edits', {}).values())
        self._connection.execute("INSERT INTO reader_data VALUES(?,?,?) ON CONFLICT(paper_id,kind) DO UPDATE SET payload=excluded.payload",
                                 (paper_id, 'search', json.dumps({'index_version': SEARCH_INDEX_VERSION, 'text': '\n'.join(texts)}, ensure_ascii=False)))

    def refresh_search(self, paper_id: int) -> None:
        """Refresh one derived cache atomically, without rebuilding the whole library."""
        with self._lock:
            try:
                self._index_paper(paper_id)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def rebuild_search(self, *, only_stale: bool = False) -> None:
        with self._lock:
            if only_stale:
                ids = [row[0] for row in self._connection.execute("""
                    SELECT DISTINCT source.paper_id FROM reader_data source
                    LEFT JOIN reader_data cache ON cache.paper_id=source.paper_id AND cache.kind='search'
                    WHERE source.kind IN ('document','editor') AND
                        CASE WHEN cache.payload IS NULL OR NOT json_valid(cache.payload) THEN 1
                             ELSE COALESCE(json_extract(cache.payload,'$.index_version'),-1) != ?
                                  OR COALESCE(json_type(cache.payload,'$.text'),'missing') != 'text' END
                    """, (SEARCH_INDEX_VERSION,))]
            else:
                ids = [row[0] for row in self._connection.execute("SELECT DISTINCT paper_id FROM reader_data WHERE kind IN ('document','editor')")]
            try:
                for paper_id in ids:
                    self._index_paper(paper_id)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def job_get(self, key: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("SELECT payload FROM desktop_jobs WHERE job_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {"status": "idle"}

    def jobs_get(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        """One bounded snapshot for overview status/gates, not one query per key."""
        unique = list(dict.fromkeys(keys))
        if not unique: return {}
        if len(unique) > 100: raise ValueError('Too many job keys for one snapshot')
        with self._lock:
            rows = self._connection.execute(
                'SELECT job_key,payload FROM desktop_jobs WHERE job_key IN (' + ','.join('?' for _ in unique) + ')', unique).fetchall()
        result = {key: {'status': 'idle'} for key in unique}
        result.update({row[0]: json.loads(row[1]) for row in rows})
        return result

    def job_put(self, key: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO desktop_jobs VALUES(?,?) ON CONFLICT(job_key) DO UPDATE SET payload=excluded.payload",
                (key, json.dumps(payload, ensure_ascii=False)),
            )
            self._connection.commit()

    def pending_classification(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute("SELECT id FROM papers WHERE source IN ('arxiv','openalex') "
                "AND trim(abstract) != '' AND COALESCE(ai_summary_zh,'') = '' "
                "ORDER BY created_at DESC,id DESC LIMIT ?", (limit,)).fetchall()
            return [self.get_paper(row['id']) for row in rows]

    def pending_classification_count(self) -> int:
        with self._lock:
            return self._connection.execute("SELECT COUNT(*) FROM papers WHERE source IN ('arxiv','openalex') "
                "AND trim(abstract) != '' AND COALESCE(ai_summary_zh,'') = ''").fetchone()[0]

    def jobs_recover(self) -> None:
        with self._lock:
            rows = self._connection.execute("SELECT job_key,payload FROM desktop_jobs").fetchall()
            for row in rows:
                value = json.loads(row[1])
                if value.get("status") in ("queued", "running", "cancelling"):
                    message = ("上次摘要与分类已中断；需重新授权继续，已完成结果保留" if row[0] == 'classification' else
                               "上次索引已中断；可点击继续，已完成文献的索引与进度仍保留" if row[0] == 'index-fulltext' else
                               "上次运行已中断；可点击继续，已完成的译文仍保留")
                    value.update(status="paused", error=message)
                    self.job_put(row[0], value)

    def add_annotation_once(self, paper_id: int, op_id: str, annotation: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("SELECT * FROM annotation_ops WHERE op_id=?", (op_id,)).fetchone()
            if row:
                if row["paper_id"] != paper_id:
                    raise ValueError("操作 ID 已用于其他论文")
                if row["deleted"]:
                    return {"deleted": True, "id": row["annotation_id"]}
                return self.get_annotation(row["annotation_id"]) or {}
            try:
                result = self.add_annotation(paper_id, annotation, _commit=False)
                self._connection.execute("INSERT INTO annotation_ops VALUES(?,?,?,0)", (op_id, paper_id, result["id"]))
                self._connection.commit()
                return result
            except Exception:
                self._connection.rollback()
                raise

    def delete_annotation(self, paper_id: int, annotation_id: int) -> bool:
        with self._lock:
            cursor = self._connection.execute("DELETE FROM annotations WHERE id=? AND paper_id=?", (annotation_id, paper_id))
            self._connection.execute(
                "UPDATE annotation_ops SET deleted=1 WHERE annotation_id=? AND paper_id=?", (annotation_id, paper_id)
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def list_annotations(self, paper_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM annotations WHERE paper_id = ? ORDER BY page_number, id", (paper_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["path"] = json.loads(item.pop("path_json") or "[]")
            result.append(item)
        return result

    def get_translation(self, paper_id: int, source_text: str, target_lang: str) -> dict[str, Any] | None:
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM translations WHERE paper_id = ? AND source_hash = ? AND target_lang = ?",
                (paper_id, source_hash, target_lang),
            ).fetchone()
        return dict(row) if row else None

    def save_translation(
        self,
        paper_id: int,
        source_text: str,
        target_lang: str,
        translated_text: str,
        model: str | None,
    ) -> None:
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO translations(paper_id, source_hash, target_lang, source_text, translated_text, model, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(paper_id, source_hash, target_lang) DO UPDATE SET
                    translated_text = excluded.translated_text,
                    model = excluded.model,
                    created_at = excluded.created_at
                """,
                (paper_id, source_hash, target_lang, source_text, translated_text, model, utc_now()),
            )
            self._connection.commit()
