"""Pure adapter for optional local Docling results; never overwrite reader data.

Raw conversion success does not establish correct layout. Keep page provenance,
explicit merged cells and uncertainty, including prose misclassified as a table.
This module does not import Docling, download models or call a remote service.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any


COMPLEX_VERSION = 2
CAPTION = re.compile(r'^\s*Table\s+(\d+[a-z]?|[A-Z](?:\.\d+)?)\b[.:\s]*(.*)', re.I)
# A caption mentioning "continued operation" is not a continuation marker.
CONTINUED = re.compile(r'\(\s*(?:continued|cont\.?)\s*\)|(?:^|[—–:-])\s*continued\s*$', re.I)


def _clean(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def _size(raw: dict, page: int) -> tuple[float, float]:
    entry = raw['pages'].get(str(page), raw['pages'].get(page, {}))
    width, height = float(entry['size']['width']), float(entry['size']['height'])
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        raise ValueError('复杂解析结果页尺寸无效')
    return width, height


def normalized_box(raw: dict, provenance: dict) -> list[float]:
    """Convert PDF points to the reader's top-left normalized page coordinates."""
    width, height = _size(raw, provenance['page_no'])
    box = provenance['bbox']
    left, top, right, bottom = (float(box[key]) for key in ('l', 't', 'r', 'b'))
    if box.get('coord_origin') == 'BOTTOMLEFT':
        top, bottom = height - top, height - bottom
    elif box.get('coord_origin') != 'TOPLEFT':
        raise ValueError('复杂解析坐标原点不支持')
    if not all(math.isfinite(value) for value in (left, top, right, bottom)) or not left < right or not top < bottom:
        raise ValueError('复杂解析区域范围无效')
    # Small backend rounding errors outside page edges are tolerated, not grossly invalid boxes.
    if left < -2 or top < -2 or right > width + 2 or bottom > height + 2:
        raise ValueError('复杂解析区域超出原 PDF 页面')
    converted = [round(max(0, min(1, value / size)), 5)
                 for value, size in zip((left, top, right, bottom), (width, height, width, height))]
    if converted[0] >= converted[2] or converted[1] >= converted[3]:
        raise ValueError('复杂解析区域没有可显示的原页范围')
    return converted


def _provenance(raw: dict, item: dict) -> list[dict]:
    return [{'page': prov['page_no'] - 1, 'box': normalized_box(raw, prov)} for prov in item.get('prov', [])]


def _table_cells(raw: dict, item: dict) -> tuple[list[dict], list[list[str | None]]]:
    data = item['data']; row_count, column_count = data['num_rows'], data['num_cols']
    if type(row_count) is not int or type(column_count) is not int or not 0 < row_count <= 2000 or not 0 < column_count <= 100:
        raise ValueError('复杂解析表格行列数无效或超限')
    rows = [[None] * column_count for _ in range(row_count)]
    occupied = set(); cells = []
    for cell in data.get('table_cells', []):
        r0, r1, c0, c1 = [cell[key] for key in ('start_row_offset_idx', 'end_row_offset_idx',
                                              'start_col_offset_idx', 'end_col_offset_idx')]
        if (not all(type(value) is int for value in (r0, r1, c0, c1))
            or not 0 <= r0 < r1 <= row_count or not 0 <= c0 < c1 <= column_count
            or cell.get('row_span') != r1 - r0 or cell.get('col_span') != c1 - c0):
            raise ValueError('复杂解析合并单元格范围无效')
        positions = {(row, column) for row in range(r0, r1) for column in range(c0, c1)}
        if positions & occupied:
            raise ValueError('复杂解析单元格范围重叠，不能作为可靠表格')
        occupied.update(positions)
        text = cell.get('text', '')
        if not isinstance(text, str):
            raise ValueError('复杂解析单元格文字无效')
        rows[r0][c0] = text
        converted = {'row': r0, 'column': c0, 'row_span': r1 - r0, 'column_span': c1 - c0,
                     'text': text, 'column_header': bool(cell.get('column_header')),
                     'row_header': bool(cell.get('row_header')), 'section_header': bool(cell.get('row_section'))}
        if cell.get('bbox') and item.get('prov'):
            converted['box'] = normalized_box(raw, {'page_no': item['prov'][0]['page_no'], 'bbox': cell['bbox']})
        cells.append(converted)
    return sorted(cells, key=lambda cell: (cell['row'], cell['column'])), rows


def _near_caption(raw: dict, table: dict, texts: list[dict]) -> dict | None:
    if not table.get('provenance'):
        return None
    source = table['provenance'][0]; width, height = _size(raw, source['page'] + 1)
    candidates = []
    for text in texts:
        match = CAPTION.match(text['text'])
        if not match:
            continue
        for prov in text['provenance']:
            if prov['page'] != source['page']:
                continue
            left, top, right, bottom = prov['box']; tl, tt, tr, tb = source['box']
            overlap = max(0, min(right, tr) - max(left, tl)) * width
            distance = max(0, tt - bottom, top - tb) * height
            if overlap > min((tr - tl) * width, (right - left) * width) * .25 and distance <= 85:
                candidates.append((distance, {'number': match.group(1).casefold(), 'text': text['text'],
                    'title': _clean(CONTINUED.sub('', match.group(2))).strip(' :.-').casefold(),
                    'continued': bool(CONTINUED.search(match.group(2))), 'provenance': prov}))
    return min(candidates, key=lambda entry: entry[0])[1] if candidates else None


def _prose_like(table: dict) -> bool:
    """Flag a narrow, uncaptained sentence grid, not arbitrary prose tables."""
    cells = table['cells']
    return (not table.get('caption') and table['columns'] == 2 and table['row_count'] >= 5
            and len(cells) >= table['row_count'] * 1.8
            and sum(len(cell['text']) >= 30 and bool(re.search(r'[.!?]\s*$', cell['text']))
                    for cell in cells) / max(1, len(cells)) >= .8)


def _header_signature(table: dict) -> list[tuple]:
    header = [cell for cell in table['cells'] if cell['column_header']]
    if not header:
        return []
    return [(cell['row'], cell['column'], cell['row_span'], cell['column_span'], _clean(cell['text']).casefold())
            for cell in header]


def normalize_complex_document(raw: dict[str, Any], source_hash: str) -> dict[str, Any]:
    if not re.fullmatch(r'[a-f0-9]{64}', source_hash):
        raise ValueError('复杂解析必须绑定完整 PDF 摘要')
    texts = []
    for item in raw.get('texts', []):
        if not isinstance(item.get('text'), str):
            raise ValueError('复杂解析正文文字无效')
        texts.append({'ref': item.get('self_ref'), 'label': item.get('label'), 'text': item['text'],
                      'provenance': _provenance(raw, item)})
    tables = []
    for item in raw.get('tables', []):
        provenance = _provenance(raw, item)
        if not provenance:
            raise ValueError('复杂解析表格缺少原 PDF 页码和位置')
        cells, rows = _table_cells(raw, item)
        key = repr((item.get('self_ref'), provenance)).encode()
        table = {'id': f'{source_hash[:12]}-complex-{hashlib.sha256(key).hexdigest()[:12]}',
                 'provenance': provenance, 'cells': cells, 'rows': rows,
                 'row_count': item['data']['num_rows'], 'columns': item['data']['num_cols'],
                 'quality_flags': [], 'requires_source_review': True}
        table['caption'] = _near_caption(raw, table, texts)
        if _prose_like(table):
            table['quality_flags'].append('疑似把双栏正文当成表格；需核对原页，不能直接作为表格翻译')
            table['prose_column_candidates'] = [
                [cell for cell in cells if cell['column'] == column and cell['column_span'] == 1]
                for column in range(table['columns'])]
        if any(re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]|\(cid:\d+\)', cell['text']) for cell in cells):
            table['quality_flags'].append('包含无法可靠解释的字符或符号，请核对原 PDF')
        # TableFormer spans do not restore superscript/subscript semantic structure.
        table['math_warning'] = '科学符号和上下标未重建；原式、单位和数值含义以原 PDF 为准'
        tables.append(table)
    tables.sort(key=lambda table: (table['provenance'][0]['page'], table['provenance'][0]['box'][1]))
    links = []; groups = []
    for table in tables:
        caption = table['caption']
        if not caption or not caption['continued'] or table['quality_flags']:
            continue
        current = table['provenance'][0]
        previous = [prior for prior in tables if prior['caption'] and not prior['quality_flags']
                    and prior['caption']['number'] == caption['number']
                    and prior['caption']['title'] == caption['title']
                    and prior['provenance'][-1]['page'] == current['page'] - 1
                    and prior['columns'] == table['columns']
                    and abs(prior['provenance'][-1]['box'][0] - current['box'][0]) < .03
                    and abs(prior['provenance'][-1]['box'][2] - current['box'][2]) < .03
                    and _header_signature(prior) and _header_signature(prior) == _header_signature(table)]
        if len(previous) != 1:
            table['quality_flags'].append('续表标记存在，但缺少唯一匹配的前页表格；未自动合并')
            continue
        prior = previous[0]
        links.append({'from_id': prior['id'], 'to_id': table['id'], 'from_page': prior['provenance'][-1]['page'] + 1,
                      'to_page': current['page'] + 1, 'confirmed': False,
                      'evidence': '相邻页同编号、明确续表字样、相同列数/页宽/合并表头',
                      'requires_source_review': True})
        group = next((group for group in groups if group['parts'][-1] == prior['id']), None)
        if group:
            group['parts'].append(table['id'])
        else:
            groups.append({'caption_number': caption['number'], 'parts': [prior['id'], table['id']],
                           'requires_source_review': True})
    return {'source_hash': source_hash, 'complex_version': COMPLEX_VERSION, 'texts': texts,
            'tables': tables, 'table_links': links, 'table_groups': groups,
            'warning': '可选增强结果，需要核对原页；不替换已保存正文、译文或笔记，不代表公式重建。'}
