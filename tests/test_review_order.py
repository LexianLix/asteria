import copy
import unittest

from paper_reader.review_order import order_candidates


def item(text, box, label='text'):
    return {'id': text, 'text': text, 'box': box, 'label': label}


def columns(top=.20):
    return [item('L1', [.10, top, .40, top + .04]), item('R1', [.60, top, .90, top + .04]),
            item('L2', [.10, top + .10, .40, top + .14]), item('R2', [.60, top + .10, .90, top + .14])]


class ReviewOrderTest(unittest.TestCase):
    def texts(self, items, **kwargs):
        return [entry['text'] for entry in order_candidates(items, **kwargs)]

    def test_columns_footer_and_tail_equation_keep_every_source_object_once(self):
        body = columns()
        values = [item('Footer', [.10, .95, .90, .98], 'page_footer'),
                  item('Equation', [.30, .60, .43, .64], 'section_header'),
                  item('(1)', [.80, .60, .85, .64]), *body]
        before = copy.deepcopy(values)
        result = order_candidates(values)
        self.assertEqual([entry['text'] for entry in result], ['L1', 'L2', 'R1', 'R2', 'Equation', '(1)', 'Footer'])
        self.assertEqual(values, before)
        self.assertCountEqual([id(entry) for entry in result], [id(entry) for entry in values])

    def test_title_and_author_rows_precede_columns_with_baseline_jitter(self):
        values = columns() + [item('Title', [.20, .05, .80, .08], 'section_header'),
            item('Right author', [.60, .110, .75, .13], 'section_header'),
            item('Left author', [.20, .112, .35, .132], 'section_header')]
        self.assertEqual(self.texts(values), ['Title', 'Left author', 'Right author', 'L1', 'L2', 'R1', 'R2'])

    def test_full_width_heading_separates_column_bands(self):
        before = columns(.15)
        after = [{**entry, 'id': entry['id'] + 'b', 'text': entry['text'] + 'b'} for entry in columns(.65)]
        heading = item('New section', [.10, .50, .90, .54], 'section_header')
        values = [*before, *after, heading]
        self.assertEqual(self.texts(values), ['L1', 'L2', 'R1', 'R2', 'New section', 'L1b', 'L2b', 'R1b', 'R2b'])

    def test_full_width_real_table_region_is_a_barrier_not_flattened_text(self):
        before = columns(.15)
        after = [{**entry, 'id': entry['id'] + 'b', 'text': entry['text'] + 'b'} for entry in columns(.65)]
        values = [*before, *after]
        self.assertEqual(self.texts(values, table_regions=[[.08, .45, .92, .60]]),
                         ['L1', 'L2', 'R1', 'R2', 'L1b', 'L2b', 'R1b', 'R2b'])
        self.assertEqual(len(order_candidates(values)), len(values))

    def test_off_center_gutter_is_not_assumed_to_be_half_the_page(self):
        values = [item('L1', [.05, .20, .29, .24]), item('R1', [.36, .20, .74, .24]),
                  item('L2', [.05, .30, .29, .34]), item('R2', [.36, .30, .74, .34])]
        self.assertEqual(self.texts(values), ['L1', 'L2', 'R1', 'R2'])

    def test_single_column_uses_vertical_order_without_inventing_columns(self):
        values = [item('Second', [.10, .40, .90, .48]), item('First', [.10, .20, .90, .28])]
        self.assertEqual(self.texts(values), ['First', 'Second'])

    def test_right_figure_labels_above_body_do_not_turn_abstract_into_a_full_width_band(self):
        values = [item('Title', [.20, .05, .80, .08], 'section_header'),
                  item('Authors', [.20, .17, .80, .20]),
                  item('Abstract', [.10, .272, .22, .284], 'section_header'),
                  item('axis', [.61, .280, .62, .37]), *columns(.30)]
        self.assertEqual(self.texts(values), ['Title', 'Authors', 'Abstract', 'L1', 'L2', 'axis', 'R1', 'R2'])

    def test_bad_geometry_keeps_original_order_without_dropping_a_paragraph(self):
        for box in (None, [], [.1, .2, float('nan'), .3], [.5, .3, .2, .4], [-.1, .2, .4, .3]):
            values = columns()
            values[0]['box'] = box
            with self.subTest(box=box):
                self.assertEqual(order_candidates(values), values)

    def test_column_labels_do_not_force_two_vertically_disjoint_sections(self):
        values = [item('First1', [.10, .10, .40, .14]), item('Second1', [.60, .70, .90, .74]),
                  item('First2', [.10, .20, .40, .24]), item('Second2', [.60, .80, .90, .84])]
        self.assertEqual(self.texts(values), ['First1', 'First2', 'Second1', 'Second2'])


if __name__ == '__main__':
    unittest.main()
