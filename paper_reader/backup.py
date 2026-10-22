"""Verified local snapshots. Restore never deletes original PDF files."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .reader import file_hash, pdf_path
from .settings import Settings
from .storage import Database, utc_now, check_schema_version

MAX_TOTAL = 10 * 1024 ** 3
MAX_ENTRY = 512 * 1024 ** 2
REQUIRED_TABLES = {"papers", "reader_data", "annotations", "translations", "annotation_ops", "desktop_jobs", "sync_runs"}


def check_database(path: Path) -> dict:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        check_schema_version(connection)
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("备份数据库完整性检查失败")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("备份数据库含无效关联")
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not REQUIRED_TABLES.issubset(tables):
            raise ValueError("备份缺少文献或笔记数据表")
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') LIMIT 1").fetchone():
            raise ValueError("备份不能包含触发器或视图")
        columns = {r[1] for r in connection.execute("PRAGMA table_info(papers)")}
        if not {"id", "title", "source", "source_id", "local_pdf_path"}.issubset(columns):
            raise ValueError("备份论文表格式不正确")
        papers = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        notes = connection.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
        for raw, in connection.execute("SELECT payload FROM reader_data"):
            if not isinstance(json.loads(raw), dict):
                raise ValueError("备份阅读数据格式不正确")
        return {"papers": papers, "annotations": notes}
    finally:
        connection.close()


class BackupManager:
    def __init__(self, settings: Settings, database: Database):
        self.settings, self.db = settings, database
        self.directory = settings.data_dir / "backups"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def resolve(self, name: str) -> Path:
        if not re.fullmatch(r"backup-[A-Za-z0-9._-]+\.zip", name):
            raise ValueError("无效的备份文件名")
        path = (self.directory / name).resolve()
        if not path.is_relative_to(self.directory.resolve()) or not path.is_file():
            raise ValueError("备份不存在")
        return path

    def list(self) -> list[dict]:
        return [{"name": p.name, "size": p.stat().st_size, "modified_at": datetime.fromtimestamp(p.stat().st_mtime).isoformat()}
                for p in sorted(self.directory.glob("backup-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)]

    def import_file(self, stream) -> dict:
        """Copy to an owned temporary path, validate, then expose as a local snapshot."""
        with self._lock:
            name = f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-import.zip"
            target = self.directory / name
            temporary = target.with_suffix('.part')
            try:
                count = 0
                with temporary.open('xb') as output:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        count += len(chunk)
                        if count > MAX_TOTAL:
                            raise ValueError('备份文件超出大小限制')
                        output.write(chunk)
                with tempfile.TemporaryDirectory(prefix='paper-import-verify-', dir=self.settings.data_dir) as folder:
                    self._stage(temporary, Path(folder))
                temporary.replace(target)
                return {'name': name, 'size': count, **self.verify(name)}
            except (zipfile.BadZipFile, KeyError, TypeError, sqlite3.DatabaseError, json.JSONDecodeError) as exc:
                raise ValueError('备份损坏或格式不正确') from exc
            finally:
                temporary.unlink(missing_ok=True)

    def create(self, *, automatic: bool = False) -> dict:
        with self._lock, tempfile.TemporaryDirectory(prefix="paper-backup-", dir=self.settings.data_dir) as folder:
            snapshot = Path(folder) / "database.sqlite"
            destination = sqlite3.connect(snapshot)
            try:
                with self.db._lock:
                    self.db._connection.backup(destination)
            finally:
                destination.close()
            summary = check_database(snapshot)
            source = sqlite3.connect(snapshot)
            source.row_factory = sqlite3.Row
            try:
                papers = [dict(row) for row in source.execute("SELECT * FROM papers WHERE local_pdf_path IS NOT NULL")]
            finally:
                source.close()
            entries = {"database.sqlite": snapshot}
            pdfs = {}
            for paper in papers:
                name = f"pdfs/{paper['id']}.pdf"
                entries[name] = pdf_path(self.settings, paper)
                pdfs[str(paper['id'])] = name
            topics = Path(folder) / "topics.json"
            topics.write_text(json.dumps(self.settings.topics(), ensure_ascii=False), encoding="utf-8")
            entries["topics.json"] = topics
            config_path = self.settings.data_dir / "desktop-settings.json"
            config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
            config = {k: v for k, v in config.items() if k not in ("api_key", "api_key_protected")}
            config_file = Path(folder) / "settings.json"
            config_file.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            entries["settings.json"] = config_file
            manifest = {"schema": 1, "created_at": utc_now(), "pdfs": pdfs, "summary": summary,
                        "secrets_included": False, "entries": {name: {"size": p.stat().st_size, "sha256": file_hash(p)} for name, p in entries.items()}}
            label = "auto" if automatic else "manual"
            name = f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{label}.zip"
            target = self.directory / name
            temporary = target.with_suffix(".part")
            try:
                with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
                    for entry, path in entries.items():
                        archive.write(path, entry)
                    archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
                temporary.replace(target)
                verified = self.verify(name)
                return {"name": name, "size": target.stat().st_size, **verified}
            except Exception:
                temporary.unlink(missing_ok=True)
                raise

    def _stage(self, path: Path, directory: Path) -> dict:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [i.filename for i in infos]
            if len(names) != len(set(names)) or len(names) > 100000 or "manifest.json" not in names:
                raise ValueError("备份存在重复文件或缺少清单")
            if sum(i.file_size for i in infos) > MAX_TOTAL or any(i.file_size > MAX_ENTRY for i in infos):
                raise ValueError("备份解压大小超出安全限制")
            for info in infos:
                name = info.filename
                if name not in {"manifest.json", "database.sqlite", "settings.json", "topics.json"} and not re.fullmatch(r"pdfs/[1-9]\d*\.pdf", name):
                    raise ValueError("备份包含越界或未知文件")
                if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                    raise ValueError("备份不能包含符号链接")
            if archive.getinfo("manifest.json").file_size > 4 * 1024 ** 2:
                raise ValueError("备份清单过大")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("secrets_included") is not False or not isinstance(manifest.get("entries"), dict):
                raise ValueError("备份清单格式不正确")
            if set(names) != {*manifest["entries"], "manifest.json"}:
                raise ValueError("备份内容与清单不一致")
            if not {"database.sqlite", "settings.json", "topics.json"}.issubset(manifest["entries"]):
                raise ValueError("备份缺少必要文件")
            for name, metadata in manifest["entries"].items():
                info = archive.getinfo(name)
                if not isinstance(metadata, dict) or metadata.get("size") != info.file_size:
                    raise ValueError("备份文件长度与清单不一致")
                target = directory / name
                target.parent.mkdir(parents=True, exist_ok=True)
                digest, count = hashlib.sha256(), 0
                with archive.open(name) as input_file, target.open("wb") as output_file:
                    for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                        count += len(chunk)
                        if count > info.file_size or count > MAX_ENTRY:
                            raise ValueError("备份解压长度异常")
                        digest.update(chunk)
                        output_file.write(chunk)
                if digest.hexdigest() != metadata.get("sha256"):
                    raise ValueError("备份文件摘要校验失败，不能恢复")
            summary = check_database(directory / "database.sqlite")
            if summary != manifest.get("summary"):
                raise ValueError("备份文献统计与清单不一致")
            pdfs = manifest.get("pdfs", {})
            if not isinstance(pdfs, dict) or set(pdfs.values()) != {n for n in names if n.startswith("pdfs/")}:
                raise ValueError("PDF 清单不完整")
            connection = sqlite3.connect(directory / "database.sqlite")
            try:
                expected = {str(row[0]) for row in connection.execute("SELECT id FROM papers WHERE local_pdf_path IS NOT NULL")}
            finally:
                connection.close()
            if set(pdfs) != expected or any(name != f"pdfs/{pid}.pdf" for pid, name in pdfs.items()):
                raise ValueError("备份 PDF 与数据库不对应")
            for name in pdfs.values():
                with (directory / name).open("rb") as file:
                    if file.read(5) != b"%PDF-":
                        raise ValueError("备份包含无效 PDF")
            for name in ("settings.json", "topics.json"):
                if not isinstance(json.loads((directory / name).read_text(encoding="utf-8")), dict):
                    raise ValueError("备份设置格式不正确")
            cfg = json.loads((directory / "settings.json").read_text(encoding="utf-8"))
            if any(k in cfg for k in ("api_key", "api_key_protected")):
                raise ValueError("备份不能携带模型密钥")
            allowed = {"provider", "base_url", "model", "daily_enabled", "daily_time", "sync_source", "retry_count", "retry_delay", "auto_backup", 'close_to_tray'}
            if set(cfg) - allowed or cfg.get("provider", "ollama") not in ("ollama", "openai", "chatgpt") or cfg.get("sync_source", "arxiv") not in ("arxiv", "openalex", "all"):
                raise ValueError("备份模型设置包含未知字段或选项")
            for key in ("daily_enabled", "auto_backup", 'close_to_tray'):
                if key in cfg and type(cfg[key]) is not bool:
                    raise ValueError("备份开关设置不正确")
            for key, upper in (("retry_count", 5), ("retry_delay", 30)):
                if key in cfg and (type(cfg[key]) not in (int, float) or not 0 <= cfg[key] <= upper or (key == "retry_count" and type(cfg[key]) is not int)):
                    raise ValueError("备份重试设置不正确")
            if "base_url" in cfg:
                url = urlsplit(cfg["base_url"])
                if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
                    raise ValueError("备份服务地址不正确")
                if cfg.get('provider') == 'chatgpt' and cfg['base_url'].rstrip('/') != 'https://api.openai.com/v1':
                    raise ValueError('ChatGPT 备份服务地址必须是固定官方接口')
            if "model" in cfg and (not isinstance(cfg["model"], str) or not 1 <= len(cfg["model"].strip()) <= 200):
                raise ValueError("备份模型名称不正确")
            if "daily_time" in cfg:
                datetime.strptime(cfg["daily_time"], "%H:%M")
            topics = json.loads((directory / "topics.json").read_text(encoding="utf-8"))
            for key in ("categories", "keywords"):
                if key in topics and (not isinstance(topics[key], list) or len(topics[key]) > 50 or any(not isinstance(v, str) or not v.strip() or len(v) > 150 for v in topics[key])):
                    raise ValueError("备份采集方向不正确")
            if not isinstance(topics.get("sources", {}), dict) or not isinstance(topics.get("tags", {}), dict):
                raise ValueError("备份来源或分类设置不正确")
            for source in topics.get("sources", {}).values():
                if not isinstance(source, dict):
                    raise ValueError("备份来源配置不正确")
                for key in ("categories", "keywords"):
                    if key in source and (not isinstance(source[key], list) or len(source[key]) > 50 or any(not isinstance(v, str) or not v.strip() or len(v) > 150 for v in source[key])):
                        raise ValueError("备份来源采集方向不正确")
            return manifest

    def verify(self, name: str) -> dict:
        with self._lock, tempfile.TemporaryDirectory(prefix="paper-verify-", dir=self.settings.data_dir) as folder:
            try:
                manifest = self._stage(self.resolve(name), Path(folder))
                return {"valid": True, "created_at": manifest["created_at"], **manifest["summary"], "pdfs": len(manifest["pdfs"])}
            except (zipfile.BadZipFile, KeyError, TypeError, sqlite3.DatabaseError, json.JSONDecodeError) as exc:
                raise ValueError("备份损坏或格式不正确") from exc

    def restore(self, name: str) -> dict:
        with self._lock, tempfile.TemporaryDirectory(prefix="paper-restore-", dir=self.settings.data_dir) as folder:
            stage = Path(folder)
            manifest = self._stage(self.resolve(name), stage)
            # Always create a verified rollback archive BEFORE changing the current library.
            recovery = self.create()
            before_db = sqlite3.connect(stage / "before.sqlite")
            config_path = self.settings.data_dir / "desktop-settings.json"
            previous = {path: path.read_bytes() if path.exists() else None for path in (config_path, self.settings.topics_path)}
            with self.db._lock:
                self.db._connection.backup(before_db)
                source = sqlite3.connect(stage / "database.sqlite")
                restored = self.settings.papers_dir / ("restored-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
                restored.mkdir(parents=True, exist_ok=False)
                try:
                    for pid, entry in manifest["pdfs"].items():
                        target = restored / f"{pid}.pdf"
                        shutil.copyfile(stage / entry, target)
                        local = str(target.relative_to(self.settings.root_dir)) if target.is_relative_to(self.settings.root_dir) else str(target)
                        source.execute("UPDATE papers SET local_pdf_path=? WHERE id=?", (local, int(pid)))
                    source.commit()
                    source.backup(self.db._connection)
                    # A validated legacy snapshot must be migrated before accepting writes.
                    self.db.initialize()
                    # An imported archive's derived cache is not authoritative even
                    # when its format marker is current. Rebuild from restored content.
                    self.db.rebuild_search()
                    restored_cfg = json.loads((stage / "settings.json").read_text(encoding="utf-8"))
                    current_cfg = json.loads(previous[config_path] or b"{}")
                    if all(restored_cfg.get(k) == current_cfg.get(k) for k in ("provider", "base_url")) and current_cfg.get("api_key_protected"):
                        restored_cfg["api_key_protected"] = current_cfg["api_key_protected"]
                    for path, data in ((config_path, json.dumps(restored_cfg, ensure_ascii=False).encode()),
                                       (self.settings.topics_path, (stage / "topics.json").read_bytes())):
                        path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = path.with_suffix(".restore-tmp")
                        temporary.write_bytes(data)
                        temporary.replace(path)
                    self.db.jobs_recover()
                except Exception:
                    before_db.backup(self.db._connection)
                    for path, data in previous.items():
                        if data is not None:
                            path.write_bytes(data)
                        else:
                            path.unlink(missing_ok=True)
                    raise
                finally:
                    source.close()
                    before_db.close()
            return {"restored": True, "recovery_backup": recovery["name"], **manifest["summary"], "pdfs": len(manifest["pdfs"])}
