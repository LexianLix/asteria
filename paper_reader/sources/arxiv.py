from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any
from .errors import SourceError, http_source_error


ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"


def _text(element: ET.Element | None) -> str:
    return " ".join((element.text or "").split()) if element is not None else ""


def _normalise_id(raw_id: str) -> str:
    value = raw_id.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"v\d+$", "", value)


def build_query(categories: list[str], keywords: list[str]) -> str:
    parts: list[str] = []
    if categories:
        parts.append("(" + " OR ".join(f"cat:{category}" for category in categories) + ")")
    if keywords:
        terms = []
        for keyword in keywords:
            clean = keyword.strip().replace('"', "")
            if clean:
                terms.append(f'all:"{clean}"' if " " in clean else f"all:{clean}")
        if terms:
            parts.append("(" + " OR ".join(terms) + ")")
    return " AND ".join(parts) or "all:*"


class ArxivSource:
    name = "arxiv"

    def __init__(self, user_agent: str, endpoint: str = "https://export.arxiv.org/api/query"):
        self.user_agent = user_agent
        self.endpoint = endpoint

    def fetch(
        self,
        *,
        categories: list[str],
        keywords: list[str],
        max_results: int = 30,
        days_back: int | None = None,
    ) -> list[dict[str, Any]]:
        query = build_query(categories, keywords)
        params = urllib.parse.urlencode(
            {
                "search_query": query,
                "start": 0,
                "max_results": max(1, min(max_results, 200)),
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        request = urllib.request.Request(
            f"{self.endpoint}?{params}",
            headers={"User-Agent": self.user_agent, "Accept": "application/atom+xml"},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            error = http_source_error('arxiv', exc)
            exc.close()
            raise error from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SourceError(f"arXiv 请求失败: {exc}") from exc

        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise SourceError("arXiv 返回内容不是有效 Atom XML") from exc

        threshold = None
        if days_back is not None:
            threshold = datetime.now(timezone.utc) - timedelta(days=days_back)
        records: list[dict[str, Any]] = []
        for entry in root.findall(f"{{{ATOM}}}entry"):
            raw_id = _text(entry.find(f"{{{ATOM}}}id"))
            source_id = _normalise_id(raw_id)
            published = _text(entry.find(f"{{{ATOM}}}published"))
            if threshold and published:
                try:
                    published_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    if published_dt < threshold:
                        continue
                except ValueError:
                    pass
            links = entry.findall(f"{{{ATOM}}}link")
            abs_url = raw_id
            pdf_url = None
            for link in links:
                if link.attrib.get("rel") == "alternate":
                    abs_url = link.attrib.get("href", abs_url)
                if link.attrib.get("title") == "pdf":
                    pdf_url = link.attrib.get("href")
            if not pdf_url and source_id:
                pdf_url = f"https://arxiv.org/pdf/{source_id}.pdf"
            records.append(
                {
                    "source": self.name,
                    "source_id": source_id,
                    "doi": _text(entry.find(f"{{{ARXIV}}}doi")) or None,
                    "title": _text(entry.find(f"{{{ATOM}}}title")),
                    "abstract": _text(entry.find(f"{{{ATOM}}}summary")),
                    "authors": [
                        _text(author.find(f"{{{ATOM}}}name"))
                        for author in entry.findall(f"{{{ATOM}}}author")
                    ],
                    "published_at": published or None,
                    "updated_at": _text(entry.find(f"{{{ATOM}}}updated")) or None,
                    "categories": [
                        item.attrib.get("term", "")
                        for item in entry.findall(f"{{{ATOM}}}category")
                        if item.attrib.get("term")
                    ],
                    "pdf_url": pdf_url,
                    "abs_url": abs_url,
                    "license": _text(entry.find(f"{{{ARXIV}}}license")) or None,
                }
            )
        return records

