"""Geometry suggestions for reviewed paragraphs, never automatic adoption."""
from __future__ import annotations

import math


REVIEW_ORDER_VERSION = 1


def _box(item):
    box = item.get('box')
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in box):
        return None
    left, top, right, bottom = box
    return box if 0 <= left < right <= 1 and 0 <= top < bottom <= 1 else None


def _rows(items):
    rows = []
    for item in sorted(items, key=lambda item: (item['box'][1], item['box'][0])):
        if not rows or abs(item['box'][1] - rows[-1][0]['box'][1]) > .0035:
            rows.append([item])
        else:
            rows[-1].append(item)
    return [item for row in rows for item in sorted(row, key=lambda item: item['box'][0])]


def _column_evidence(items):
    core = [item for item in items if (item.get('label') in ('text', 'paragraph')
            or item.get('label', '').startswith('疑似正文'))
            and .12 <= item['box'][2] - item['box'][0] <= .49]
    groups = []
    for item in sorted(core, key=lambda item: item['box'][0]):
        if not groups or item['box'][0] - groups[-1][0]['box'][0] > .035:
            groups.append([item])
        else:
            groups[-1].append(item)
    groups = [group for group in groups if len(group) >= 2]
    evidence = []
    for index, left in enumerate(groups):
        for right in groups[index + 1:]:
            left_edge, right_edge = max(item['box'][2] for item in left), min(item['box'][0] for item in right)
            overlap = (min(max(item['box'][3] for item in left), max(item['box'][3] for item in right))
                       - max(min(item['box'][1] for item in left), min(item['box'][1] for item in right)))
            if .012 < right_edge - left_edge < .3 and overlap >= .03:
                evidence.append((min(len(left), len(right)), overlap, (left_edge + right_edge) / 2, left + right))
    if not evidence:
        return None
    _, _, gutter, core = max(evidence, key=lambda entry: entry[:2])
    return gutter, min(item['box'][1] for item in core), max(item['box'][3] for item in core)


def _bands(items, regions, depth=0):
    evidence = _column_evidence(items) if depth < 4 else None
    if not evidence:
        return _rows(items)
    gutter, body_top, body_bottom = evidence
    bands = []
    for item in items:
        left, top, right, bottom = item['box']
        # A caption/axis label can start above the first prose paragraph in
        # just one column. Adjacent items are NOT automatically header bands.
        if (left < gutter - .003 and right > gutter + .003
                or bottom < body_top - .04 or top > body_bottom + .04):
            bands.append([top, bottom])
    for box in regions:
        if box[0] < gutter - .003 and box[2] > gutter + .003:
            bands.append([box[1], box[3]])
    merged = []
    for top, bottom in sorted(bands):
        if merged and top <= merged[-1][1] + .0035:
            merged[-1][1] = max(merged[-1][1], bottom)
        else:
            merged.append([top, bottom])
    # Do not split/reassign a paragraph straddling a horizontal separator.
    if any((item['box'][1] < top < item['box'][3] or item['box'][1] < bottom < item['box'][3])
           and not top <= item['box'][1] <= item['box'][3] <= bottom
           for top, bottom in merged for item in items):
        return _rows(items)
    result, remaining = [], list(items)
    for top, bottom in merged:
        before, inside, after = [], [], []
        for item in remaining:
            center = (item['box'][1] + item['box'][3]) / 2
            (before if center < top else inside if center <= bottom else after).append(item)
        result.extend(_bands([item for item in before if item['box'][0] < gutter], [], depth + 1))
        result.extend(_bands([item for item in before if item['box'][0] >= gutter], [], depth + 1))
        result.extend(_rows(inside))
        remaining = after
    result.extend(_bands([item for item in remaining if item['box'][0] < gutter], [], depth + 1))
    result.extend(_bands([item for item in remaining if item['box'][0] >= gutter], [], depth + 1))
    return result


def order_candidates(items, *, table_regions=()):
    """Keep every object/ID/text intact; suggest bands, then left/right columns."""
    if any(_box(item) is None for item in items):
        return list(items)
    regions = [box for box in table_regions if _box({'box': box}) is not None]
    headers = [item for item in items if item.get('label') == 'page_header']
    footers = [item for item in items if item.get('label') == 'page_footer']
    body = [item for item in items if item.get('label') not in ('page_header', 'page_footer')]
    return _rows(headers) + _bands(body, regions) + _rows(footers)
