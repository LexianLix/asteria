from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Any

from .errors import SourceError, http_source_error


def _abstract_from_inverted_index(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    words: dict[int, str] = {}
    for word, positions in index.items():
        for position in positions:
            words[int(position)] = word
    return " ".join(words[position] for position in sorted(words))


def _doi_value(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    return value or None


class OpenAlexSource:
    name = "openalex"

    def __init__(self, user_agent: str, mailto: str | None = None, endpoint: str = "https://api.openalex.org/works"):
        self.user_agent = user_agent
        self.mailto = mailto
        self.endpoint = endpoint

    def fetch(
        self,
        *,
        keywords: list[str],
        max_results: int = 30,
        days_back: int = 2,
    ) -> list[dict[str, Any]]:
        today = date.today()
        start_date = today - timedelta(days=max(1, days_back))
        params: dict[str, str | int] = {
            "filter": f"from_publication_date:{start_date.isoformat()},to_publication_date:{today.isoformat()}",
            "per-page": max(1, min(max_results, 200)),
            "sort": "publication_date:desc",
            "select": "id,doi,title,publication_date,authorships,primary_location,best_oa_location,open_access,topics,abstract_inverted_index,is_retracted",
        }
        clean_keywords = [item.strip() for item in keywords if item.strip()]
        if clean_keywords:
            params["search"] = " OR ".join(clean_keywords)
        if self.mailto:
            params["mailto"] = self.mailto
        request = urllib.request.Request(
            f"{self.endpoint}?{urllib.parse.urlencode(params)}",
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error = http_source_error('openalex', exc)
            exc.close()
            raise error from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise SourceError(f"OpenAlex 请求失败: {exc}") from exc

        records: list[dict[str, Any]] = []
        for item in payload.get("results", []):
            if item.get("is_retracted") or not item.get("id") or not item.get("title"):
                continue
            work_id = str(item["id"]).rstrip("/").rsplit("/", 1)[-1]
            primary = item.get("primary_location") or {}
            best_oa = item.get("best_oa_location") or {}
            locations = [best_oa, primary]
            pdf_url = next(
                (
                    str(location.get("pdf_url"))
                    for location in locations
                    if location.get("is_oa") and location.get("pdf_url")
                ),
                None,
            )
            landing_url = next(
                (
                    str(location.get("landing_page_url"))
                    for location in locations
                    if location.get("landing_page_url")
                ),
                str(item["id"]),
            )
            topic_names = [
                str(topic.get("display_name"))
                for topic in (item.get("topics") or [])[:5]
                if topic.get("display_name")
            ]
            authors = [
                str((authorship.get("author") or {}).get("display_name"))
                for authorship in (item.get("authorships") or [])
                if (authorship.get("author") or {}).get("display_name")
            ]
            published = item.get("publication_date")
            records.append(
                {
                    "source": self.name,
                    "source_id": work_id,
                    "doi": _doi_value(item.get("doi")),
                    "title": str(item.get("title", "")).strip(),
                    "abstract": _abstract_from_inverted_index(item.get("abstract_inverted_index")),
                    "authors": authors,
                    "published_at": f"{published}T00:00:00Z" if published else None,
                    "updated_at": item.get("updated_date"),
                    "categories": topic_names,
                    "pdf_url": pdf_url,
                    "abs_url": landing_url,
                    "license": best_oa.get("license") or primary.get("license"),
                }
            )
        return records

