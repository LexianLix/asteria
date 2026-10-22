"""Local daily brief: no model calls, scraping, or automatic data mutation."""
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from .identity import normalize_doi, work_key


def grouped_records(records: list[dict]) -> list[list[dict]]:
    groups = {}
    for paper in records:
        groups.setdefault(work_key(paper.get('doi'), paper['id']), []).append(paper)
    return list(groups.values())


def collected_at(paper: dict) -> datetime:
    value = datetime.fromisoformat(paper['created_at'])
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def match_reasons(paper: dict, topics: dict) -> list[str]:
    text = (paper.get('title', '') + '\n' + paper.get('abstract', '')).casefold()
    reasons = []
    for source in topics.get('sources', {}).values():
        for keyword in source.get('keywords', []):
            if keyword.casefold() in text:
                reasons.append('关键词匹配：' + keyword)
        for category in source.get('categories', []):
            if category in paper.get('categories', []):
                reasons.append('来源分类：' + category)
    return list(dict.fromkeys(reasons))[:6] or ['未找到配置关键词的直接匹配；请自行判断相关性']


def daily_brief(db, topics: dict, day: date, offset_minutes: int, page: int = 1, page_size: int | None = 30) -> dict:
    zone = timezone(timedelta(minutes=offset_minutes))
    groups = grouped_records(db.work_records())
    counts, items, added_versions, imported = Counter(), [], 0, 0
    for versions in groups:
        first = min(collected_at(p) for p in versions)
        local_day = first.astimezone(zone).date()
        counts[local_day.isoformat()] += 1
        today_versions = [p for p in versions if collected_at(p).astimezone(zone).date() == day]
        added_versions += max(0, len(today_versions) - (1 if local_day == day else 0))
        if local_day != day:
            continue
        paper = dict(versions[0])
        reasons = list(dict.fromkeys(reason for p in versions for reason in match_reasons(p, topics)))
        matched = [reason for reason in reasons if reason.startswith(('关键词匹配：', '来源分类：'))]
        paper.update(reader_state=db.reader_get(paper['id'], 'state'), versions_count=len(versions),
                     normalized_doi=normalize_doi(paper.get('doi')), collected_at=first.astimezone(zone).isoformat(),
                     match_reasons=(matched or reasons)[:6],
                     sources=list(dict.fromkeys(p['source'] for p in versions)))
        # Prefer a stored summary, but label it as AI; never silently generate one.
        summary = next((p.get('ai_summary_zh') for p in versions if p.get('ai_summary_zh')), None)
        abstract = next((p.get('abstract') for p in versions if p.get('abstract')), None)
        paper.update(brief_text=summary or abstract or '来源未提供摘要；可打开来源或 PDF 阅读。',
                     brief_is_ai=bool(summary))
        imported += paper['source'] == 'local'
        items.append(paper)
    items.sort(key=lambda p: (p['collected_at'], p['id']), reverse=True)
    start = (page - 1) * page_size if page_size is not None else 0
    selected = items[start:start + page_size] if page_size is not None else items
    return {'date': day.isoformat(), 'utc_offset_minutes': offset_minutes, 'items': selected,
            'total': len(items), 'page': page, 'page_size': page_size, 'new_source_versions': added_versions,
            'local_imports': imported, 'days': [{'date': d, 'count': counts[d]} for d in sorted(counts, reverse=True)[:90]],
            'note': '按首次进入本机文献库的日期统计，不是发表日期；相同 DOI 归为一条文献，来源版本与各自笔记独立保留。简报不调用模型。'}
