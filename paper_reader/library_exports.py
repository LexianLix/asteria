"""Plain-text exports for saved annotations and source metadata."""
from datetime import date
import json
import re
from urllib.parse import urlsplit

from .identity import normalize_doi


NOTE_LABELS = {'note': '笔记', 'text-highlight': '文字高亮', 'ink': '笔迹', 'highlight': '区域高亮'}


def annotation_content(kind, text):
    if kind == 'text-highlight' and text:
        try:
            anchor = json.loads(text)
            if isinstance(anchor, dict) and isinstance(anchor.get('quote'), str):
                return anchor['quote'], anchor.get('block_id')
        except ValueError:
            pass
    return text or '', None


def notes_markdown(items):
    lines = ['# 阅读笔记', '']
    for item in items:
        title = ' '.join(item['paper_title'].split())
        lines.extend([f"## {title}", '',
                      f"第 {item['page_number'] + 1} 页 · {NOTE_LABELS.get(item['annotation_type'], '标注')}", '',
                      item['content'] or '（原页图形标注）', ''])
    return '\n'.join(lines)


def _line(value):
    return ' '.join((value or '').split())


def _publication_date(value):
    try:
        return date.fromisoformat((value or '')[:10])
    except ValueError:
        return None


_BIB_SPECIAL = {'\\': r'\textbackslash{}', '{': r'\{', '}': r'\}', '%': r'\%', '&': r'\&', '#': r'\#',
                '_': r'\_', '$': r'\$', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}'}


def _bib(value):
    return ''.join(_BIB_SPECIAL.get(char, char) for char in value)


def citation_export(papers, fmt):
    records = []
    for paper in papers:
        title = _line(paper['title'])
        authors = [_line(author) for author in paper.get('authors', []) if author and _line(author)]
        published = _publication_date(paper.get('published_at'))
        date_text = paper.get('published_at') or ''
        year = str(published.year) if published else date_text if re.fullmatch(r'[1-9][0-9]{3}', date_text) else ''
        doi = normalize_doi(paper.get('doi'))
        url = _line(paper.get('abs_url'))
        try:
            link = urlsplit(url)
            url = url if link.scheme in ('http', 'https') and link.hostname and ' ' not in url else ''
        except ValueError:
            url = ''
        if fmt == 'bibtex':
            fields = [('title', _bib(title))]
            if authors:
                fields.append(('author', ' and '.join(
                    '{' + _bib(name) + '}' if re.search(r'\s+and\s+', name, re.I) else _bib(name) for name in authors)))
            fields.extend((key, _bib(value)) for key, value in [('year', year), ('doi', doi), ('url', url)] if value)
            body = ',\n'.join(f'  {key} = {{{value}}}' for key, value in fields)
            records.append(f"@misc{{asteria{paper['id']},\n{body}\n}}")
        else:
            fields = [('TY', 'GEN'), ('TI', title)]
            fields.extend(('AU', author) for author in authors)
            fields.extend((key, value) for key, value in [('PY', year), ('DA', published.strftime('%Y/%m/%d') if published else ''),
                                                         ('DO', doi), ('UR', url)] if value)
            records.append('\r\n'.join([*(f'{key}  - {value}' for key, value in fields), 'ER  -']))
    separator = '\n' if fmt == 'bibtex' else '\r\n'
    return (separator * 2).join(records) + separator
