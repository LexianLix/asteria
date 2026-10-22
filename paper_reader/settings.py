from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    data_dir: Path
    db_path: Path
    papers_dir: Path
    topics_path: Path
    arxiv_user_agent: str
    openalex_mailto: str | None
    default_max_results: int
    ollama_url: str | None
    ollama_model: str
    # Optional local process configuration, never an HTTP/UI setting. Reference
    # this app's own durable login without sharing library/consent directories.
    auth_root_dir: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        default_root = (Path(os.getenv('LOCALAPPDATA') or Path.home() / 'AppData/Local') / 'Asteria') if getattr(sys, 'frozen', False) else ROOT_DIR
        root = Path(os.getenv("PAPER_READER_ROOT", str(default_root))).resolve()
        data_dir = Path(os.getenv("PAPER_READER_DATA", str(root / "data"))).resolve()
        topics_path = Path(os.getenv("PAPER_READER_TOPICS", str(root / "config" / "topics.json"))).resolve()
        auth_root = os.getenv('PAPER_READER_AUTH_ROOT')
        if auth_root is not None and not auth_root.strip():
            raise ValueError('PAPER_READER_AUTH_ROOT must be a non-empty application root')
        return cls(
            root_dir=root,
            data_dir=data_dir,
            db_path=data_dir / "database.sqlite",
            papers_dir=data_dir / "papers",
            topics_path=topics_path,
            arxiv_user_agent=os.getenv(
                "ARXIV_USER_AGENT",
                "Asteria/0.1 (personal open-source project)",
            ),
            openalex_mailto=os.getenv("OPENALEX_MAILTO") or None,
            default_max_results=int(os.getenv("PAPER_READER_MAX_RESULTS", "30")),
            ollama_url=os.getenv("OLLAMA_URL") or None,
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
            auth_root_dir=Path(auth_root).resolve() if auth_root is not None else None,
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.papers_dir.mkdir(parents=True, exist_ok=True)
        if getattr(sys, 'frozen', False) and self.topics_path == self.root_dir / 'config/topics.json':
            seed = ROOT_DIR / 'config/topics.json'
            if seed.is_file() and not self.topics_path.exists():
                self.topics_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with self.topics_path.open('x', encoding='utf-8') as target:
                        target.write(seed.read_text(encoding='utf-8'))
                except FileExistsError:
                    pass

    def topics(self) -> dict[str, Any]:
        if not self.topics_path.exists():
            return {"sources": {}, "tags": {}}
        with self.topics_path.open("r", encoding="utf-8") as file:
            return json.load(file)
