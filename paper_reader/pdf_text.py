"""Preserve unambiguous numeric script glyphs from PDF geometry, not inferred math."""
from __future__ import annotations

from collections import defaultdict
from statistics import median

from pdfplumber.utils import extract_text

SUPER = dict(zip('0123456789+-−=()', '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾'))
SUB = dict(zip('0123456789+-−=()', '₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎'))
SCRIPT_CHARACTERS = ''.join(dict.fromkeys([*SUPER.values(), *SUB.values()]))
FLAT_SCRIPTS = str.maketrans({value: key for mapping in (SUPER, SUB) for key, value in mapping.items() if key != '−'})


def char_key(char):
    return (char['x0'], char['x1'], char['top'], char['bottom'], char['text'])


def glyph_baseline(char):
    matrix = char.get('matrix')
    if (matrix and len(matrix) == 6 and abs(matrix[1]) < 1e-6 and abs(matrix[2]) < 1e-6
            and matrix[0] > 0 and matrix[3] > 0):
        return matrix[5]
    return None


def baseline(word):
    chars = word.get('chars', [])
    sizes = [c.get('size', 0) for c in chars]
    size = max(sizes, default=0)
    if size <= 0:
        return None
    base = [c for c in chars if c.get('size', 0) >= size * .95]
    positions = [glyph_baseline(c) for c in base]
    return (size, median(c['top'] for c in base), median(c['bottom'] for c in base),
            median(positions) if all(p is not None for p in positions) else None)


def numeric_script_words(words):
    """Return new word objects; source glyphs, boxes and word boundaries stay intact."""
    references, grid = [], defaultdict(list)
    for word in words:
        reference = baseline(word)
        references.append(reference)
        if reference:
            grid[(int(word['x1'] // 16), int(reference[1] // 16))].append((word, reference))
    result = []
    for word, own in zip(words, references):
        chars = word.get('chars', [])
        if not own or ''.join(c['text'] for c in chars) != word['text']:
            # Expanded ligatures may differ from the raw glyph spelling; this
            # forbids script replacement, not use of a valid upright baseline.
            result.append({**word, '_line_baseline': own[3]} if own else word)
            continue
        reference = own
        # A detached script can use a close left-hand base, never another column
        # or the nearest arbitrary line elsewhere on the page.
        if all(c['text'] in SUPER for c in chars):
            nearby = []
            x, y = int(word['x0'] // 16), int(word['top'] // 16)
            for gx in range(x - 2, x + 1):
                for gy in range(y - 2, y + 3):
                    for base_word, base in grid.get((gx, gy), []):
                        gap = word['x0'] - base_word['x1']
                        if base_word is not word and base[0] > own[0] / .82 and -.5 <= gap <= base[0] * .6:
                            if abs(word['top'] - base[1]) <= base[0] * .8:
                                nearby.append((gap, abs(word['top'] - base[1]), base))
            if nearby:
                reference = min(nearby, key=lambda item: (item[0], item[1]))[2]
        size, top, bottom, position = reference
        pieces, scripts = [], []
        offset = 0
        for char in chars:
            kind = None
            if char.get('size', size) <= size * .82 and char['text'] in SUPER:
                raised = glyph_baseline(char)
                if position is not None and raised is not None:
                    delta = raised - position
                    kind = 'superscript' if delta >= size * .08 else 'subscript' if delta <= -size * .08 else None
                elif char['bottom'] <= bottom - size * .18 and char['top'] <= top + size * .12:
                    kind = 'superscript'
                elif char['bottom'] >= bottom + size * .13 and char['top'] >= top + size * .3:
                    kind = 'subscript'
            replacement = (SUPER if kind == 'superscript' else SUB)[char['text']] if kind else char['text']
            pieces.append(replacement)
            if kind:
                scripts.append({'offset': offset, 'original': char['text'], 'replacement': replacement,
                    'kind': kind, 'raw_box': [char['x0'], char['top'], char['x1'], char['bottom']],
                    'size': char['size'], 'reference_size': size, '_char_key': char_key(char)})
            offset += len(replacement)
        result.append({**word, 'text': ''.join(pieces), 'numeric_scripts': scripts,
                       '_line_baseline': position})
    return result


def script_char_changes(words):
    return {item['_char_key']: item['replacement'] for word in words for item in word.get('numeric_scripts', [])}


def numeric_table_rows(rows, table, page, changes):
    """Preserve geometry scripts inside existing cells, without changing cell layout."""
    if not changes or isinstance(table, dict):
        return rows
    updated = [list(row) for row in rows]
    for index, row in enumerate(table.rows):
        for column, box in enumerate(row.cells):
            if not box or index >= len(updated) or column >= len(updated[index]):
                continue
            left, top, right, bottom = box
            chars = [c for c in page.chars if left <= (c['x0'] + c['x1']) / 2 < right
                     and top <= (c['top'] + c['bottom']) / 2 < bottom]
            if not any(char_key(c) in changes for c in chars):
                continue
            converted = [{**c, 'text': changes.get(char_key(c), c['text'])} for c in chars]
            updated[index][column] = extract_text(converted, x_tolerance=2, y_tolerance=3)
    return updated
