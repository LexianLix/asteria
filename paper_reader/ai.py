from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class Enrichment:
    summary_zh: str | None
    tags: list[str]
    score: float | None


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None


class AIEnricher:
    def __init__(self, ollama_url: str | None, model: str, tag_rules: dict[str, list[str]]):
        self.ollama_url = ollama_url.rstrip("/") if ollama_url else None
        self.model = model
        self.tag_rules = tag_rules

    def _rule_tags(self, title: str, abstract: str, categories: list[str]) -> list[str]:
        haystack = f"{title} {abstract} {' '.join(categories)}".lower()
        tags = []
        for tag, words in self.tag_rules.items():
            if any(word.lower() in haystack for word in words):
                tags.append(tag)
        return tags

    def enrich(self, *, title: str, abstract: str, categories: list[str]) -> Enrichment:
        rule_tags = self._rule_tags(title, abstract, categories)
        if not self.ollama_url:
            return Enrichment(None, rule_tags, None)

        prompt = f"""你是严谨的科研文献助手。只根据给出的标题、摘要和分类工作，不要编造论文中不存在的事实。
输出严格 JSON，不要 Markdown：
{{"summary_zh":"150字以内中文摘要","tags":["中文标签"],"relevance":0.0}}
其中 relevance 是 0 到 1 的相关度；tags 最多 6 个。

标题：{title}
分类：{', '.join(categories)}
摘要：{abstract}
"""
        body = json.dumps(
            {"model": self.model, "prompt": prompt, "stream": False, "format": "json"},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.ollama_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
            parsed = _extract_json(str(payload.get("response", "")))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            parsed = None

        if not parsed:
            return Enrichment(None, rule_tags, None)
        raw_tags = parsed.get("tags")
        tags = [str(item).strip() for item in raw_tags if str(item).strip()] if isinstance(raw_tags, list) else []
        tags = list(dict.fromkeys([*tags, *rule_tags]))[:6]
        summary = str(parsed.get("summary_zh") or "").strip() or None
        try:
            score = float(parsed["relevance"])
        except (KeyError, TypeError, ValueError):
            score = None
        return Enrichment(summary, tags, max(0.0, min(1.0, score)) if score is not None else None)

    def translate(self, text: str, target_lang: str = "zh-CN") -> str | None:
        if not self.ollama_url:
            return None
        prompt = f"""将下面的学术文本翻译成{target_lang}。保持公式、变量名、引用编号和专业术语准确，不要解释，不要添加内容。

原文：
{text}
"""
        body = json.dumps(
            {"model": self.model, "prompt": prompt, "stream": False},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.ollama_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
            translated = str(payload.get("response", "")).strip()
            return translated or None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return None

