import copy
import unittest
from unittest.mock import patch
from paper_reader.complex_pdf import normalize_complex_document, normalized_box


DIGEST = 'a' * 64


def cell(text, row, column, row_span=1, column_span=1, header=False):
    return {'text': text, 'start_row_offset_idx': row, 'end_row_offset_idx': row + row_span,
            'start_col_offset_idx': column, 'end_col_offset_idx': column + column_span,
            'row_span': row_span, 'col_span': column_span, 'column_header': header}


def prov(page, top=700, bottom=500):
    return {'page_no': page, 'bbox': {'l': 30, 't': top, 'r': 570, 'b': bottom, 'coord_origin': 'BOTTOMLEFT'}}


def document():
    cells = [cell('Model', 0, 0, row_span=2, header=True),
             cell('Benchmarks', 0, 1, column_span=3, header=True),
             *[cell(text, 1, column, header=True) for column, text in enumerate(['Dev', 'Test', 'Latency'], 1)],
             *[cell(text, 2, column) for column, text in enumerate(['Alpha', '95%', '94%', '2.5 ms'])]]
    return {'pages': {str(page): {'size': {'width': 600, 'height': 800}} for page in (1, 2)},
            'texts': [{'self_ref': '#/texts/0', 'text': 'Table 1: Results', 'label': 'caption', 'prov': [prov(1, 735, 720)]},
                      {'self_ref': '#/texts/1', 'text': 'Table 1: Results (continued)', 'label': 'text', 'prov': [prov(2, 735, 720)]}],
            'tables': [{'self_ref': f'#/tables/{page - 1}', 'prov': [prov(page)],
                        'data': {'num_rows': 3, 'num_cols': 4, 'table_cells': copy.deepcopy(cells)}} for page in (1, 2)]}


class ComplexPdfTest(unittest.TestCase):
    def test_coordinate_origins_and_bad_geometry(self):
        raw = document()
        self.assertEqual(normalized_box(raw, prov(1)), [.05, .125, .95, .375])
        origin = prov(1); origin['bbox'].update(t=100, b=300, coord_origin='TOPLEFT')
        self.assertEqual(normalized_box(raw, origin), [.05, .125, .95, .375])
        for values in [{'t': float('nan')}, {'l': -20}, {'r': 800}, {'coord_origin': 'UNKNOWN'}, {'b': 750}]:
            invalid = prov(1); invalid['bbox'].update(values)
            with self.assertRaises(ValueError): normalized_box(raw, invalid)

    def test_merged_cells_and_provenance_retained_without_mutation(self):
        raw = document(); before = copy.deepcopy(raw)
        normalized = normalize_complex_document(raw, DIGEST)
        table = normalized['tables'][0]
        self.assertEqual(table['rows'][0], ['Model', 'Benchmarks', None, None])
        self.assertEqual(table['rows'][1], [None, 'Dev', 'Test', 'Latency'])
        self.assertEqual([(entry['row_span'], entry['column_span']) for entry in table['cells'][:2]], [(2, 1), (1, 3)])
        self.assertEqual(table['provenance'][0]['page'], 0)
        self.assertTrue(table['requires_source_review'])
        self.assertEqual(raw, before)
        self.assertEqual(normalized, normalize_complex_document(raw, DIGEST))
        self.assertNotEqual(table['id'], normalize_complex_document(raw, 'b' * 64)['tables'][0]['id'])

    def test_explicit_continuation_keeps_parts_not_flattened_guesses(self):
        normalized = normalize_complex_document(document(), DIGEST)
        self.assertEqual(len(normalized['table_links']), 1)
        link = normalized['table_links'][0]
        self.assertEqual((link['from_page'], link['to_page']), (1, 2))
        self.assertFalse(link['confirmed'])
        self.assertEqual(normalized['table_groups'][0]['parts'], [entry['id'] for entry in normalized['tables']])
        self.assertEqual(len(normalized['tables']), 2)

    def test_same_headers_do_not_silently_merge_unrelated_tables(self):
        for change in ('no_continuation', 'other_number', 'other_title', 'other_header', 'nonadjacent', 'ambiguous'):
            raw = document()
            if change == 'no_continuation': raw['texts'][1]['text'] = 'Table 1: Results'
            elif change == 'other_number': raw['texts'][1]['text'] = 'Table 2: Results (continued)'
            elif change == 'other_title': raw['texts'][1]['text'] = 'Table 1: Different results (continued)'
            elif change == 'other_header': raw['tables'][1]['data']['table_cells'][1]['text'] = 'Other benchmarks'
            elif change == 'nonadjacent':
                raw['pages']['3'] = raw['pages']['2']; raw['tables'][1]['prov'][0]['page_no'] = 3; raw['texts'][1]['prov'][0]['page_no'] = 3
            else:
                raw['tables'].insert(1, copy.deepcopy(raw['tables'][0])); raw['tables'][1]['self_ref'] = '#/tables/duplicate'
            with self.subTest(change=change):
                self.assertEqual(normalize_complex_document(raw, DIGEST)['table_links'], [])

    def test_continued_operation_is_not_a_continuation_marker(self):
        raw = document()
        raw['texts'][1]['text'] = 'Table 1: Risk map. Behavior change during continued operation.'
        normalized = normalize_complex_document(raw, DIGEST)
        self.assertFalse(normalized['tables'][1]['caption']['continued'])
        self.assertEqual(normalized['table_links'], [])
        self.assertEqual(normalized['tables'][1]['quality_flags'], [])

    def test_sentence_grid_is_flagged_and_column_alternative_is_review_only(self):
        raw = document(); raw['texts'] = []; raw['tables'] = raw['tables'][:1]
        raw['tables'][0]['data'] = {'num_rows': 6, 'num_cols': 2, 'table_cells': [
            cell(f'This is a long sentence in column {column}, row {row}.', row, column)
            for row in range(6) for column in range(2)]}
        table = normalize_complex_document(raw, DIGEST)['tables'][0]
        self.assertTrue(table['quality_flags'])
        self.assertEqual([entry['row'] for entry in table['prose_column_candidates'][0]], list(range(6)))
        self.assertTrue(table['requires_source_review'])

    def test_invalid_spans_and_overlaps_rejected_not_repaired(self):
        for change in ('bounds', 'span', 'overlap', 'noninteger'):
            raw = document(); cells = raw['tables'][0]['data']['table_cells']
            if change == 'bounds': cells[0]['end_row_offset_idx'] = 100
            elif change == 'span': cells[0]['col_span'] = 3
            elif change == 'overlap': cells.append(copy.deepcopy(cells[0]))
            else: cells[0]['start_row_offset_idx'] = 0.0
            with self.subTest(change=change), self.assertRaises(ValueError): normalize_complex_document(raw, DIGEST)

    def test_source_hash_required_and_conversion_is_network_free(self):
        with patch('socket.socket.connect', side_effect=AssertionError('Must not connect')):
            normalize_complex_document(document(), DIGEST)
        for digest in ('', 'short', 'z' * 64):
            with self.assertRaises(ValueError): normalize_complex_document(document(), digest)


if __name__ == '__main__':
    unittest.main()
