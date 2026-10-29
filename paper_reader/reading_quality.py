"""Derived, local-only extraction diagnostics; never a semantic quality score."""
import re


_LETTER_RUN = re.compile(r'(?<![A-Za-z])(?:[A-Za-z]\s+){14,}[A-Za-z](?![A-Za-z])')
_WORDS = re.compile(r'[A-Za-z]+')
_BAD_GLYPHS = re.compile('[\ufffd\ufffe\uffff]')


def _prose(block):
    text = block.get('en', '')
    # Equations and extracted tables routinely contain isolated variable names.
    # Retain offsets while masking only validated protected ranges.
    for span in block.get('protected_spans', []):
        start, end = span.get('start'), span.get('end')
        if type(start) is int and type(end) is int and 0 <= start < end <= len(text):
            text = text[:start] + ' ' * (end - start) + text[end:]
    return text


def reading_quality(document):
    """Report obvious missing/fragmented text without changing cached anchors.

    Embedded vector thumbnails can expose thousands of tiny glyphs as prose.
    Their origin cannot be established from text alone, so flag for review;
    never discard the content, guess a reconstruction, or claim completeness.
    """
    pages = []
    for page in document.get('pages', []):
        blocks = page.get('blocks', [])
        issues = []
        if not any(block.get('en', '').strip() for block in blocks):
            issues.append({'code': 'no_text', 'message': '未提取到文字；请查看原页，扫描件可使用增强解析与 OCR。', 'block_ids': []})
        fragmented, unreadable = [], []
        for block in blocks:
            prose = _prose(block)
            words = _WORDS.findall(prose)
            if len(words) >= 40 and sum(len(word) == 1 for word in words) / len(words) >= .55 and _LETTER_RUN.search(prose):
                fragmented.append(block['id'])
            if len(_BAD_GLYPHS.findall(block.get('en', ''))) >= 2:
                unreadable.append(block['id'])
        if fragmented:
            issues.append({'code': 'fragmented_text', 'message': '部分英文被逐字拆开，可能来自微小图中文字或提取顺序异常；请核对原页后再翻译。', 'block_ids': fragmented})
        if unreadable:
            issues.append({'code': 'unreadable_glyphs', 'message': '部分字形无法解码；请核对原页，必要时使用增强解析与 OCR。', 'block_ids': unreadable})
        pages.append({'page_number': page['number'], 'issues': issues})
    return {'total_pages': len(pages), 'review_pages': sum(bool(page['issues']) for page in pages),
            'no_text_pages': sum(any(issue['code'] == 'no_text' for issue in page['issues']) for page in pages),
            'pages': pages}
