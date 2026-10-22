"""Explicit mock structure, only for lifecycle/UI checks, not parser accuracy."""
import copy


def mock_complex_result(start=1, end=2, ocr=False):
    def prov(page, top=711, bottom=580):
        return {'page_no': page, 'bbox': {'l': 45, 't': top, 'r': 550, 'b': bottom, 'coord_origin': 'BOTTOMLEFT'}}
    def cell(text, row, column, row_span=1, col_span=1, header=False):
        return {'text': text, 'start_row_offset_idx': row, 'end_row_offset_idx': row + row_span,
            'start_col_offset_idx': column, 'end_col_offset_idx': column + col_span,
            'row_span': row_span, 'col_span': col_span, 'column_header': header}
    pages = {str(page): {'size': {'width': 595, 'height': 842}} for page in range(start, end + 1)}
    tables, texts = [], []
    for index, page in enumerate(range(start, end + 1)):
        texts.append({'self_ref': f'#/texts/{index}', 'label': 'text',
            'text': 'Table 1: Synthetic results' + (' (continued)' if index else ''), 'prov': [prov(page, 743, 728)]})
        values = ['Alpha', '95%', '94%', '2.5 ms'] if not index else ['Gamma', '88%', '87%', '3.2 ms']
        cells = [cell('Model', 0, 0, row_span=2, header=True), cell('Synthetic benchmarks', 0, 1, col_span=3, header=True),
            *[cell(value, 1, column, header=True) for column, value in enumerate(['Dev accuracy', 'Test accuracy', 'Latency'], 1)],
            *[cell(value, 2, column) for column, value in enumerate(values)]]
        tables.append({'self_ref': f'#/tables/{index}', 'prov': [prov(page)],
            'data': {'num_rows': 3, 'num_cols': 4, 'table_cells': copy.deepcopy(cells)}})
    return {'document': {'pages': pages, 'texts': texts, 'tables': tables}, 'versions': {'docling': 'qa-mock-not-real-parser'},
            'backend': 'qa-mock', 'ocr': ocr, 'remote_inference': False, 'blocked_network_attempts': 0}
