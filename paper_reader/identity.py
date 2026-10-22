"""Conservative DOI comparison. Never move annotations between PDF versions."""
import re
from urllib.parse import unquote, urlsplit

ASCII_LOWER = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')


def normalize_doi(raw: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    lower = value.translate(ASCII_LOWER)
    if lower.startswith(('http://', 'https://')):
        try:
            url = urlsplit(value)
            hostname = url.hostname
        except ValueError:
            return None
        if hostname not in ('doi.org', 'dx.doi.org', 'www.doi.org') or url.username or url.password or url.query or url.fragment:
            return None
        try:
            value = unquote(url.path.lstrip('/'), errors='strict')
        except UnicodeDecodeError:
            return None
    elif lower.startswith('doi:'):
        value = value[4:].strip()
    elif lower.startswith('urn:doi:'):
        value = value[8:].strip()
    if not value.isprintable() or not re.fullmatch(r'10\.[0-9]+(?:\.[0-9]+)*/[^\s\x00-\x1f\x7f]+', value):
        return None
    # DOI comparison folds Basic Latin only. Punctuation may be part of a suffix.
    return value.translate(ASCII_LOWER)


def work_key(doi: str | None, paper_id: int) -> str:
    normalized = normalize_doi(doi)
    return 'doi:' + normalized if normalized else 'paper:' + str(paper_id)
