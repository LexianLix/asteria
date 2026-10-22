"""Source-assisted numeric scripts in review text/tables, never inferred math/OCR.

The raw converter result is unchanged. Only script glyphs whose complete cell
character sequence agrees with the owned PDF snapshot can replace preview text.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pdfplumber
from pdfplumber.utils import extract_text

from .pdf_text import (FLAT_SCRIPTS, SCRIPT_CHARACTERS, char_key,
                       numeric_script_words, script_char_changes)
from .reader import file_hash


def _tokens(text):
    # Whitespace and equivalent dash glyphs may differ between PDF extractors.
    # Their spelling is NOT rewritten; the original converter text stays intact.
    return [(character.translate(FLAT_SCRIPTS).translate(str.maketrans('−–—', '---')), index)
            for index, character in enumerate(text) if not character.isspace()]


def _restore_text(original, source, candidate):
    old, extracted, scripted = _tokens(original), _tokens(source), _tokens(candidate)
    if [item[0] for item in old] != [item[0] for item in extracted]:
        return original, []
    if [item[0] for item in extracted] != [item[0] for item in scripted]:
        return original, []
    result = list(original)
    changes = []
    for (_, offset), (_, candidate_offset) in zip(old, scripted):
        replacement = candidate[candidate_offset]
        if replacement in SCRIPT_CHARACTERS and result[offset] != replacement:
            changes.append({'offset': offset, 'original': result[offset], 'replacement': replacement})
            result[offset] = replacement
    return ''.join(result), changes


def _scan_image_layer(page):
    # Be conservative with hybrid/scanned PDFs: an old hidden OCR layer must
    # never override freshly recognized text. Small illustrations are allowed.
    area = 0
    for image in page.images:
        left, right = max(0, image['x0']), min(page.width, image['x1'])
        top, bottom = max(0, image['top']), min(page.height, image['bottom'])
        area += max(0, right - left) * max(0, bottom - top)
    return area >= page.width * page.height * .5


def restore_complex_scripts(preview: dict, raw: dict, snapshot: Path, *, ocr=False) -> dict:
    """Return a separate preview with auditable, source-bound script replacements."""
    if file_hash(snapshot) != preview['source_hash']:
        raise ValueError('上下标核对的 PDF 摘要不匹配，未保存增强预览')
    result = copy.deepcopy(preview)
    report = {'version': 2, 'source_hash': preview['source_hash'], 'status': 'completed',
              'restored_cells': 0, 'restored_texts': 0, 'restored_characters': 0,
              'restored_cell_characters': 0, 'restored_text_characters': 0,
              'checked_pages': [], 'skipped_pages': [], 'unmatched_cells': [], 'unmatched_texts': []}
    result['numeric_script_restoration'] = report
    if ocr:
        report.update(status='skipped', reason='ocr-result-not-overridden-by-pdf-text-layer')
        return result
    by_page = {}
    for table in result['tables']:
        if len(table['provenance']) != 1:
            continue  # A cell bbox has no unambiguous page for a multi-page table.
        if any(cell.get('box') for cell in table['cells']):
            by_page.setdefault(table['provenance'][0]['page'], {'tables': [], 'texts': []})['tables'].append(table)
    for text in result.get('texts', []):
        provenance = text.get('provenance', [])
        if len(provenance) == 1 and text.get('text') and provenance[0].get('box'):
            by_page.setdefault(provenance[0]['page'], {'tables': [], 'texts': []})['texts'].append(text)
    with pdfplumber.open(snapshot) as pdf:
        for page_number, entries in by_page.items():
            reason = None
            if not 0 <= page_number < len(pdf.pages):
                reason = 'source-page-missing'
            else:
                page = pdf.pages[page_number]
                entry = raw['pages'].get(str(page_number + 1), raw['pages'].get(page_number + 1, {}))
                size = entry.get('size', {})
                if (abs(page.width - float(size.get('width', 0))) > .01
                        or abs(page.height - float(size.get('height', 0))) > .01 or page.rotation):
                    reason = 'page-size-or-rotation-mismatch'
                elif _scan_image_layer(page):
                    reason = 'possible-old-ocr-image-layer'
            if reason:
                report['skipped_pages'].append({'page': page_number, 'reason': reason})
                if 0 <= page_number < len(pdf.pages):
                    pdf.pages[page_number].close()
                continue
            report['checked_pages'].append(page_number)
            words = numeric_script_words(page.extract_words(x_tolerance=2, y_tolerance=3, return_chars=True))
            replacements = script_char_changes(words)
            glyphs = {script['_char_key']: {('word_offset' if key == 'offset' else key): value
                        for key, value in script.items() if key != '_char_key'}
                      for word in words for script in word.get('numeric_scripts', [])}
            def restore_item(item, box):
                left, top, right, bottom = [value * scale for value, scale in
                    zip(box, (page.width, page.height, page.width, page.height))]
                chars = [char for char in page.chars
                         if left <= (char['x0'] + char['x1']) / 2 < right
                         and top <= (char['top'] + char['bottom']) / 2 < bottom]
                source_glyphs = [glyphs[char_key(char)] for char in chars if char_key(char) in glyphs]
                if not source_glyphs:
                    return False, False
                source = extract_text(chars, x_tolerance=2, y_tolerance=3)
                candidate = extract_text([{**char, 'text': replacements.get(char_key(char), char['text'])}
                                          for char in chars], x_tolerance=2, y_tolerance=3)
                updated, changes = _restore_text(item['text'], source, candidate)
                if not changes:
                    return False, ([value[0] for value in _tokens(item['text'])]
                                   != [value[0] for value in _tokens(source)])
                item['original_text'] = item['text']
                item['text'] = updated
                item['script_restoration'] = {'method': 'source-glyph-geometry',
                    'source_hash': result['source_hash'], 'page': page_number, 'box': box,
                    'page_size': [page.width, page.height], 'source_text': source,
                    'source_scripted_text': candidate, 'changes': changes, 'glyphs': source_glyphs}
                report['restored_characters'] += len(changes)
                return True, False

            for text in entries['texts']:
                restored, unmatched = restore_item(text, text['provenance'][0]['box'])
                if restored:
                    report['restored_texts'] += 1
                    report['restored_text_characters'] += len(text['script_restoration']['changes'])
                elif unmatched:
                    report['unmatched_texts'].append({'ref': text.get('ref'), 'page': page_number,
                                                     'reason': 'source-text-mismatch'})
            for table in entries['tables']:
                restored_count = 0
                for cell in table['cells']:
                    if not cell.get('box') or not cell['text']:
                        continue
                    restored, unmatched = restore_item(cell, cell['box'])
                    if not restored:
                        if unmatched:
                            report['unmatched_cells'].append({'table_id': table['id'], 'row': cell['row'],
                                'column': cell['column'], 'reason': 'source-text-mismatch'})
                        continue
                    table['rows'][cell['row']][cell['column']] = cell['text']
                    restored_count += 1
                    report['restored_cell_characters'] += len(cell['script_restoration']['changes'])
                if restored_count:
                    table['numeric_script_restoration'] = {'restored_cells': restored_count}
                    table['math_warning'] = (f'已按原 PDF 字符位置恢复 {restored_count} 个单元格的数字上下标；'
                        '仍需核对原页，未重建一般公式或证明数值含义正确')
                    report['restored_cells'] += restored_count
            page.close()
    return result
