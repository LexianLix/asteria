"""Local scientific regions. Original PDF is authoritative, never reconstructed math."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from .pdf_text import SCRIPT_CHARACTERS, numeric_table_rows, script_char_changes

SCIENTIFIC_VERSION = 6
RELATION = re.compile(r'[=<>≤≥≈∝∈≔→←⇒]')
MATH_FONT = re.compile(r'cmmi|cmsy|cmex|msam|msbm|math|symbol', re.I)
TABLE_CAPTION = re.compile(r'^Table\s*(?:[0-9]+[a-z]?|[A-Z](?:\.[0-9]+)?)[.:\s]', re.I)
SCRIPT_ATOM = '[' + re.escape(SCRIPT_CHARACTERS) + ']+'
NUMBER_ATOM = r'[0-9]+(?:[.,][0-9]+)*(?:[eE][+-]?[0-9]+)?(?:' + SCRIPT_ATOM + r')?%?'
CELL_NUMBER = re.compile(r'(?<![A-Za-z0-9])[+-]?' + NUMBER_ATOM + r'(?:\s*(?:±|×|[-−–])\s*[+-]?' + NUMBER_ATOM + r')*|' + SCRIPT_ATOM)
PROSE_FOOTNOTE = re.compile(r'(?P<word>[A-Z]?[a-z]{4,})(?P<marker>[⁰¹²³⁴⁵⁶⁷⁸⁹]+)[,.;:!?]?')
LEADING_PROSE_FOOTNOTE = re.compile(r'(?P<marker>[⁰¹²³⁴⁵⁶⁷⁸⁹]+)(?P<word>[A-Z][a-z]{3,}(?:[,.;:!?][a-z]+)*[,.;:!?]?)')
# Some PDFs omit all spaces inside a raised-marker footnote's first run.
# Keep this separate from identifiers: a narrow sentence cue plus prose tail
# is required, and geometric formula/table coverage still takes precedence.
LEADING_FUSED_FOOTNOTE = re.compile(r'(?P<marker>[⁰¹²³⁴⁵⁶⁷⁸⁹]+)Wehave[a-z]{12,}(?:\([0-9]+×\))?[a-z]*-?')
NUMERIC_LAYER_COMPOUND = re.compile(r'(?P<number>[+-]?[0-9]+(?:\.[0-9]+)?[⁺⁻]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+)-layers?[,.;:!?]?')
PROSE_RUN = re.compile(r'[A-Za-z]+(?:[- ]+[A-Za-z]+){2,}-?')
EQUATION_REFERENCE = re.compile(r'\b(?:in|see|from|by|of|using)\s+Eqns?\.\s*\([0-9]+[a-z]?\)', re.I)
PROSE_CUES = {'the', 'we', 'these', 'this', 'although', 'can', 'let', 'is', 'are',
              'by', 'and', 'to', 'will', 'of', 'with', 'which', 'that', 'for'}
MATH_FUNCTIONS = {'softmax', 'sigmoid', 'tanh', 'relu', 'gelu', 'arcsin', 'arccos', 'arctan',
    'cosine', 'tangent', 'logit', 'logarithm', 'alpha', 'beta', 'gamma', 'delta',
    'epsilon', 'theta', 'lambda', 'omega', 'sigma'}


def box_overlap(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def word_lines(words):
    lines = []
    for word in sorted(words, key=lambda w: (round(w['top'] / 3), w['x0'])):
        if not lines or abs(lines[-1][0]['top'] - word['top']) > 4:
            lines.append([word])
        else:
            lines[-1].append(word)
    return [sorted(line, key=lambda w: w['x0']) for line in lines]


def normalized_box(box, page):
    return [round(max(0, min(1, value / size)), 5) for value, size in zip(box, [page.width, page.height, page.width, page.height])]


def region_id(digest, number, kind, box):
    key = hashlib.sha256(json.dumps([kind, box]).encode()).hexdigest()[:12]
    return f'{digest[:12]}-p{number}-{kind}-{key}'


def header_table(words, bbox, header_bottom):
    """Single-line headers plus repeated row labels, for multiline prose tables.

    This is not a merged-cell parser. Ambiguous headers fall back to text edges
    and the same cut-word safety check, rather than silently concatenating cells.
    """
    inside = [w for w in words if bbox[0] <= (w['x0'] + w['x1']) / 2 <= bbox[2]
              and bbox[1] <= (w['top'] + w['bottom']) / 2 <= bbox[3]]
    header = word_lines([w for w in inside if w['bottom'] <= header_bottom + 1])
    body = [w for w in inside if w['top'] >= header_bottom - 1]
    if len(header) != 1 or not body or sum(bool(re.search(r'[0-9]', w['text'])) for w in body) > len(body) * .25:
        return None
    groups = []
    for word in header[0]:
        if not groups or word['x0'] - groups[-1][-1]['x1'] > max(8, word.get('size', 10) * .9):
            groups.append([word])
        else:
            groups[-1].append(word)
    if not 2 <= len(groups) <= 10:
        return None
    starts = [group[0]['x0'] for group in groups]
    # Each header must have at least two independently aligned body lines.
    if any(len({round(w['top'] / 4) for w in body if abs(w['x0'] - start) < 3}) < 2 for start in starts):
        return None
    positions = [bbox[0], *(start - 3 for start in starts[1:]), bbox[2]]
    labels = word_lines([w for w in body if abs(w['x0'] - starts[0]) < 3])
    if len(labels) < 2:
        return None
    row_edges = [bbox[1], header_bottom, *(min(w['top'] for w in row) - 2 for row in labels[1:]), bbox[3]]
    rows = []
    for top, bottom in zip(row_edges, row_edges[1:]):
        row = []
        for left, right in zip(positions, positions[1:]):
            cell_words = [w for w in inside if left <= (w['x0'] + w['x1']) / 2 < right
                          and top <= (w['top'] + w['bottom']) / 2 < bottom]
            row.append('\n'.join(' '.join(w['text'] for w in line) for line in word_lines(cell_words)))
        rows.append(row)
    return {'rows': rows, 'boundaries': positions[1:-1], 'bbox': bbox}


def detect_regions(page, words, digest, number):
    """Conservative candidates; no OCR, model download, or remote requests."""
    tables = []
    diagnostics = []
    lines = word_lines(words)
    try:
        candidates = [(table, 'ruled', None) for table in page.find_tables({'vertical_strategy': 'lines_strict', 'horizontal_strategy': 'lines_strict'})]
        # Many papers use horizontal rules but no vertical borders. Restrict the
        # text strategy to a caption-adjacent, three-rule enclosure, not body prose.
        captions = [line for line in lines if TABLE_CAPTION.match(' '.join(w['text'] for w in line))]
        horizontal = [edge for edge in page.edges if edge.get('orientation') == 'h' and edge['x1'] - edge['x0'] > 60]
        horizontal.sort(key=lambda edge: edge['top'])
        for caption in captions:
            cy = min(w['top'] for w in caption)
            nearby = [e for e in horizontal if cy - page.height * .75 < e['top'] < cy + page.height * .75]
            for i, first in enumerate(nearby):
                aligned = [e for e in nearby[i:] if abs(e['x0'] - first['x0']) < 8 and abs(e['x1'] - first['x1']) < 8 and e['top'] - first['top'] < page.height * .75]
                # Adjacent tables can have the same rule widths. A caption is
                # a boundary, not another row between their aligned rules.
                # Use its first word so prose in the other column on the same
                # baseline cannot make that column's caption a boundary here.
                caption_tops = [min(w['top'] for w in line) for line in captions
                    if first['x0'] - page.width * .15 <= line[0]['x0'] <= first['x1']]
                ceiling = min((top for top in caption_tops if top > first['top']), default=page.height)
                aligned = [e for e in aligned if e['top'] < ceiling]
                if len(aligned) < 3:
                    continue
                bottom = aligned[-1]['top']
                # Do not associate this caption with an earlier/later table
                # across another caption, even when it is within 140 points.
                if any(bottom < top < cy or cy < top < first['top'] for top in caption_tops):
                    continue
                if bottom - first['top'] < 25 or min(abs(cy - first['top']), abs(cy - bottom)) > 140:
                    continue
                bbox = (max(0, first['x0'] - 2), max(0, first['top'] - 2), min(page.width, first['x1'] + 2), min(page.height, bottom + 2))
                area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                if any(enclosure and box_overlap(bbox, enclosure) > .6 * min(area,
                        (enclosure[2] - enclosure[0]) * (enclosure[3] - enclosure[1]))
                        for _, _, enclosure in candidates):
                    continue  # Already associated; try this caption's next table.
                if aligned[1]['top'] - first['top'] < 70:
                    structured = header_table(words, bbox, aligned[1]['top'])
                    if structured:
                        candidates.append((structured, 'caption-header-alignment', bbox))
                        break
                cropped = page.crop(bbox)
                finder = cropped.debug_tablefinder({'vertical_strategy': 'text', 'horizontal_strategy': 'text',
                                                     'min_words_vertical': 3, 'min_words_horizontal': 1})
                positions = sorted({round(edge['x0'], 2) for edge in finder.edges if edge.get('orientation') == 'v'})
                if len(positions) < 3:
                    continue
                # Inferred final text edge may clip units occurring in only two
                # rows. Use the physical enclosure as both outside boundaries.
                positions = [bbox[0], *positions[1:-1], bbox[2]]
                if any(right - left < 8 for left, right in zip(positions, positions[1:])):
                    continue
                row_edges = sorted({round(edge['top'], 3) for edge in finder.edges if edge.get('orientation') == 'h'})
                if len(row_edges) < 4:
                    continue
                found = cropped.find_tables({'vertical_strategy': 'explicit', 'explicit_vertical_lines': positions,
                                             'horizontal_strategy': 'explicit', 'explicit_horizontal_lines': row_edges})
                candidates.extend((table, 'caption-horizontal-rules', bbox) for table in found)
                break
        def candidate_area(candidate):
            table, _, enclosure = candidate
            bbox = enclosure or (table['bbox'] if isinstance(table, dict) else table.bbox)
            return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])
        # A few internal vertical rules can yield a small, apparently certain
        # grid inside a mostly horizontal-rule table. Choose its full physical
        # enclosure before overlap suppression, not whichever detector ran first.
        # The ordinary cut-word/missing-column gates still apply to that outer
        # candidate; an uncertain full table must not become a trusted subgrid.
        for table, method, enclosure in sorted(candidates, key=candidate_area, reverse=True):
            rows = table['rows'] if isinstance(table, dict) else table.extract(x_tolerance=2, y_tolerance=3)
            rows = numeric_table_rows(rows, table, page, script_char_changes(words))
            if method == 'caption-horizontal-rules':
                rows = [row for row in rows if any(cell and cell.strip() for cell in row)]
            width = max((len(row) for row in rows), default=0)
            filled = [str(cell) for row in rows for cell in row if cell and str(cell).strip()]
            if len(rows) < 2 or width < 2 or len(filled) < 4 or len(rows) * width > 2500:
                continue
            if method == 'caption-horizontal-rules' and sum(bool(re.search(r'[0-9]', cell)) for cell in filled) < 2:
                continue
            bbox = list(enclosure or table.bbox)
            if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > page.width * page.height * .8:
                continue
            if any(box_overlap(bbox, previous['_raw_box']) > .6 * min((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]), (previous['_raw_box'][2] - previous['_raw_box'][0]) * (previous['_raw_box'][3] - previous['_raw_box'][1])) for previous in tables):
                continue
            normalized = normalized_box(bbox, page)
            flags = []
            if method != 'ruled':
                boundaries = table['boundaries'] if isinstance(table, dict) else sorted({cell[0] for row in table.rows for cell in row.cells if cell})[1:]
                cut_words = [w for w in words if bbox[1] <= (w['top'] + w['bottom']) / 2 <= bbox[3]
                             and any(w['x0'] + 2 < edge < w['x1'] - 2 for edge in boundaries)]
                if cut_words:
                    flags.append('估算列边界穿过文字，单元格结构不可靠；只保留原表，不发送这些单元格给模型')
                positions = [bbox[0], *boundaries, bbox[2]]
                for left, right in zip(positions, positions[1:]):
                    cell_words = [w for w in words if left <= (w['x0'] + w['x1']) / 2 < right
                                  and bbox[1] <= (w['top'] + w['bottom']) / 2 <= bbox[3]]
                    if any(b['x0'] - a['x1'] > 24 and re.search(r'[0-9]', a['text']) and re.search(r'[0-9]', b['text'])
                           for line in word_lines(cell_words) for a, b in zip(line, line[1:])):
                        flags.append('同一估算单元格包含分隔较远的数值，可能漏分列；保留原表并停止单元格翻译')
                        break
            tables.append({'id': region_id(digest, number, 'table', normalized), 'kind': 'table', 'box': normalized,
                           '_raw_box': bbox, 'rows': rows, 'method': method,
                           'quality_flags': flags, 'cell_translation_allowed': not flags,
                           'text': '\n'.join(' | '.join(cell or '' for cell in row) for row in rows),
                           'note': '表格结构为本地估算；空白／合并单元格与跨页表格请核对原 PDF。数值原样保留。'})
    except Exception as exc:
        # Region detection must not make an otherwise readable PDF unusable.
        diagnostics.append('表格检测未完成：' + type(exc).__name__)
    tables.sort(key=lambda region: (region['_raw_box'][1], region['_raw_box'][0]))
    formulas = []
    for line in lines:
        text = ' '.join(w['text'] for w in line)
        if len(text) > 220 or not (RELATION.search(text) or re.search(r'[∑∫∏]', text)):
            continue
        bbox = [min(w['x0'] for w in line), min(w['top'] for w in line), max(w['x1'] for w in line), max(w['bottom'] for w in line)]
        if any(box_overlap(bbox, table['_raw_box']) > 0 for table in tables):
            continue
        chars = [c for c in page.chars if bbox[0] - 2 <= (c['x0'] + c['x1']) / 2 <= bbox[2] + 2 and bbox[1] - 7 <= (c['top'] + c['bottom']) / 2 <= bbox[3] + 7]
        math_count = sum(bool(MATH_FONT.search(c.get('fontname', ''))) for c in chars)
        prose_words = re.findall(r'[A-Za-z]{4,}', text)
        numbered = bool(re.search(r'\([0-9]+[a-z]?\)\s*$', text))
        center = (bbox[0] + bbox[2]) / 2
        centered = min(abs(center - page.width / 2), abs(center - page.width / 4), abs(center - 3 * page.width / 4)) < page.width * .1
        if (len(prose_words) > 3 and not numbered) or not (numbered or math_count >= max(2, len(chars) * .15) or centered and len(prose_words) <= 1):
            continue
        # Tiny inline subscripts (e.g. t=0) are not complete displayed equations.
        if not numbered and bbox[2] - bbox[0] < page.width * .06:
            continue
        if chars:
            bbox[1] = min(c['top'] for c in chars); bbox[3] = max(c['bottom'] for c in chars)
        bbox = [max(0, bbox[0] - 4), max(0, bbox[1] - 4), min(page.width, bbox[2] + 4), min(page.height, bbox[3] + 4)]
        normalized = normalized_box(bbox, page)
        formulas.append({'id': region_id(digest, number, 'formula', normalized), 'kind': 'formula', 'box': normalized,
                         '_raw_box': bbox, 'text': text, 'method': 'number-font-position-heuristic',
                         'note': '公式候选：展示原 PDF 区域，不是 LaTeX 重建；提取字符可能缺失上下标。'})
    # Do not list a summation subscript or a case branch as another whole equation.
    formulas = list({r['id']: r for r in formulas}.values())
    formulas = [r for r in formulas if not any(other is not r and
                box_overlap(r['_raw_box'], other['_raw_box']) >= .9 * (r['_raw_box'][2] - r['_raw_box'][0]) * (r['_raw_box'][3] - r['_raw_box'][1]) and
                (other['_raw_box'][2] - other['_raw_box'][0]) * (other['_raw_box'][3] - other['_raw_box'][1]) > (r['_raw_box'][2] - r['_raw_box'][0]) * (r['_raw_box'][3] - r['_raw_box'][1])
                for other in formulas)]
    for region in [*tables, *formulas]:
        region.setdefault('block_ids', [])
    return [*tables, *formulas], diagnostics


def protected_spans(group, text, regions):
    """Coordinates identify the exact character runs in an existing paragraph ID."""
    spans, offset = [], 0
    for word in group:
        x, y = (word['x0'] + word['x1']) / 2, (word['top'] + word['bottom']) / 2
        region = next((r for r in regions if r['_raw_box'][0] <= x <= r['_raw_box'][2] and r['_raw_box'][1] <= y <= r['_raw_box'][3]), None)
        if region:
            span = {'start': offset, 'end': min(len(text), offset + len(word['text'])), 'region_id': region['id'], 'kind': region['kind']}
            if spans and spans[-1]['region_id'] == span['region_id'] and span['start'] - spans[-1]['end'] <= 1:
                spans[-1]['end'] = span['end']
            else:
                spans.append(span)
        offset += len(word['text']) + 1
    return [span for span in spans if span['end'] > span['start']]


def preserve_regions(old, enriched):
    """Add layout without dropping cached translations, edits, or old block IDs."""
    for page in old['pages']:
        new_page = next((p for p in enriched['pages'] if p['number'] == page['number']), None)
        if not new_page:
            continue
        source = {b['id']: b for b in new_page['blocks']}
        page['scientific_regions'] = new_page.get('scientific_regions', [])
        page['scientific_diagnostics'] = new_page.get('scientific_diagnostics', [])
        for block in page['blocks']:
            match = source.get(block['id'])
            if match and match['en'] == block['en']:
                block['protected_spans'] = match.get('protected_spans', [])
                for key in ('numeric_scripts', 'numeric_spans'):
                    if key in match:
                        block[key] = match[key]
        valid_ids = {b['id'] for b in page['blocks']}
        for region in page['scientific_regions']:
            region['block_ids'] = [key for key in region['block_ids'] if key in valid_ids]
    old['scientific_version'] = SCIENTIFIC_VERSION
    return old


def is_numeric_cell(text):
    return bool(re.search(r'[0-9]', text)) and bool(re.fullmatch(r'[\s0-9.,%+\-−±()/eE×x*†‡—–]+', text))


def formula_translation_spans(text, span):
    """Subtract clear English prose accidentally enclosed by a formula crop.

    Layout candidates are intentionally conservative crops, not lexical math
    boundaries. Leave symbols and isolated identifiers protected; only expose
    runs of at least three ordinary words with a natural-language cue. Existing
    document metadata/crops/cached translations remain unchanged.
    """
    if span.get('kind') != 'formula':
        return [span]
    start, end = span['start'], span['end']
    exposed = []
    for match in PROSE_RUN.finditer(text[start:end]):
        words = list(re.finditer(r'[A-Za-z]+', match.group()))
        ordinary = [word for word in words if len(word.group()) >= 2]
        if len(ordinary) < 3 or not any(word.group().casefold() in PROSE_CUES for word in ordinary):
            continue
        if all(word.group().casefold() in MATH_FUNCTIONS | {'and', 'or', 'of'} for word in ordinary):
            continue
        # Do not expose leading/trailing one-letter mathematical identifiers.
        right = ordinary[-1].end()
        if match.group().endswith('-') and right == len(match.group()) - 1:
            right += 1  # A prose line-break hyphen is not an immutable math symbol.
        exposed.append((start + match.start() + ordinary[0].start(), start + match.start() + right))
    exposed.extend((start + match.start(), start + match.end())
                   for match in EQUATION_REFERENCE.finditer(text[start:end]))
    result, cursor = [], start
    for left, right in sorted(exposed):
        if cursor < left:
            result.append({**span, 'start': cursor, 'end': left})
        cursor = max(cursor, right)
    if cursor < end:
        result.append({**span, 'start': cursor, 'end': end})
    return result


def translation_input(page):
    """Keep math/table runs out of the model; translate textual table cells only."""
    source, replacements, local = [], {}, {}
    nonce = uuid.uuid4().hex[:12]
    for block_index, block in enumerate(page['blocks']):
        text, offset, pieces, keep = block['en'], 0, [], {}
        geometric = block.get('protected_spans', [])
        numeric = block.get('numeric_spans', [])
        # Validate the original metadata before narrowing any footnote span, so
        # invalid nested ranges cannot disappear during normalization/merging.
        if any(not (0 <= s['start'] < s['end'] <= len(text)) for s in [*geometric, *numeric]):
            raise ValueError('复杂内容保护范围无效，请重新提取文档')
        ranges = [part for span in geometric for part in formula_translation_spans(text, span)]
        for span in numeric:
            start, end = span['start'], span['end']
            match = PROSE_FOOTNOTE.fullmatch(text[start:end]) or LEADING_PROSE_FOOTNOTE.fullmatch(text[start:end])
            fused = LEADING_FUSED_FOOTNOTE.fullmatch(text[start:end]) if start == 0 else None
            tail_words = re.findall(r'[A-Za-z]{2,}', text[end:]) if fused else []
            compound = NUMERIC_LAYER_COMPOUND.fullmatch(text[start:end])
            if fused and len(tail_words) >= 5 and any(word.casefold() in PROSE_CUES for word in tail_words):
                ranges.append({'start': start, 'end': start + fused.end('marker')})
                # Expose glued English, not the footnote number or its literal
                # numeric multiplier. Never rewrite source/cache metadata.
                ranges.extend({'start': start + atom.start(), 'end': start + atom.end()}
                    for atom in re.finditer(r'[0-9]+×?', text[start:end]))
            elif match and re.split(r'[,.;:!?]', match['word'])[0].casefold() not in MATH_FUNCTIONS:
                # Long conventional prose with a trailing raised footnote can
                # be translated; protect its marker, not the word/punctuation.
                # Mixed-case model names, short variables, subscripts, numbers,
                # charges and known math functions retain the original span.
                ranges.append({'start': start + match.start('marker'), 'end': start + match.end('marker')})
            elif compound:
                # A numerical depth such as 10³ is immutable, but its ordinary
                # layer noun is translatable. Formula/table geometry still wins
                # when ranges are merged; do not generalize to chemical names,
                # variable bases, subscripts or arbitrary hyphenated identifiers.
                ranges.append({'start': start + compound.start('number'), 'end': start + compound.end('number')})
            else:
                ranges.append(span)
        # Reviewed/manual text may already contain explicit Unicode scripts even
        # when no geometric metadata is available. Preserve those literal runs.
        ranges.extend({'start': match.start(), 'end': match.end()} for match in CELL_NUMBER.finditer(text)
                      if any(char in SCRIPT_CHARACTERS for char in match.group()))
        if any(not (0 <= s['start'] < s['end'] <= len(text)) for s in ranges):
            raise ValueError('复杂内容保护范围无效，请重新提取文档')
        spans = []
        for span in sorted(ranges, key=lambda s: (s['start'], -s['end'])):
            if spans and span['start'] <= spans[-1]['end']:
                spans[-1]['end'] = max(spans[-1]['end'], span['end'])
            else:
                spans.append({'start': span['start'], 'end': span['end']})
        for i, span in enumerate(spans):
            start, end = span['start'], span['end']
            if not (offset <= start < end <= len(text)):
                raise ValueError('复杂内容保护范围无效，请重新提取文档')
            token = f'[[PR_KEEP_{nonce}_b{block_index}_{i}]]'
            pieces.extend([text[offset:start], token]); keep[token] = text[start:end]; offset = end
        pieces.append(text[offset:]); cleaned = ''.join(pieces)
        rest = cleaned
        for token in keep:
            rest = rest.replace(token, '')
        if keep and not re.search(r'[A-Za-z\u4e00-\u9fff]', rest):
            local[block['id']] = text
        else:
            source.append({'id': block['id'], 'en': cleaned})
            replacements[block['id']] = keep
    for region in page.get('scientific_regions', []):
        if region['kind'] != 'table' or not region.get('cell_translation_allowed', True):
            continue
        for row, cells in enumerate(region['rows']):
            for col, text in enumerate(cells):
                if text and text.strip():
                    key = f"{region['id']}-r{row}c{col}"
                    if is_numeric_cell(text) or not re.search(r'[A-Za-z\u4e00-\u9fff]', text):
                        local[key] = text
                    else:
                        pieces, keep, offset = [], {}, 0
                        for i, match in enumerate(CELL_NUMBER.finditer(text)):
                            token = f'[[PR_KEEP_{nonce}_{region["id"][-12:]}_r{row}c{col}_{i}]]'
                            pieces.extend([text[offset:match.start()], token]); keep[token] = match.group(); offset = match.end()
                        pieces.append(text[offset:])
                        source.append({'id': key, 'en': ''.join(pieces)})
                        replacements[key] = keep
    return source, replacements, local


def restore_protected(result, replacements, local):
    from .model_client import ModelError
    restored = dict(result)
    for key, tokens in replacements.items():
        value = restored[key]
        for token, original in tokens.items():
            if value.count(token) != 1:
                raise ModelError('模型改变了公式／表格保护标记，本页未保存，请重新确认后补译', 'format', retryable=True)
            value = value.replace(token, original)
        if '[[PR_KEEP_' in value:
            raise ModelError('模型返回未知保护标记，本页未保存', 'format', retryable=True)
        restored[key] = value
    return {**restored, **local}
