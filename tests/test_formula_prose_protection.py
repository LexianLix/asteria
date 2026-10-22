"""Prose accidentally covered by a geometric formula box must remain translatable."""
import unittest
from paper_reader.scientific import translation_input, restore_protected


class FormulaProseProtectionTest(unittest.TestCase):
    def test_equation_reference_words_are_translatable(self):
        text = 'W s in Eqn.(1).'
        page = {'blocks': [{'id': 'b', 'en': text,
            'protected_spans': [{'start': 0, 'end': len(text), 'kind': 'formula'}]}]}
        source, replacements, local = translation_input(page)
        self.assertFalse(local)
        self.assertIn('in Eqn.(1)', source[0]['en'])
        self.assertIn('W s ', replacements['b'].values())
        self.assertEqual(restore_protected({'b': source[0]['en']}, replacements, local)['b'], text)

    def test_prose_linebreak_hyphen_is_not_forced_into_chinese(self):
        text = '(x)+x. Although both forms should be able to asymptot-'
        page = {'blocks': [{'id': 'b', 'en': text,
            'protected_spans': [{'start': 0, 'end': len(text), 'kind': 'formula'}]}]}
        source, replacements, local = translation_input(page)
        self.assertIn('asymptot-', source[0]['en'])
        self.assertNotIn('-', replacements['b'].values())
        self.assertEqual(restore_protected({'b': source[0]['en']}, replacements, local)['b'], text)

    def test_formula_box_does_not_freeze_long_prose_runs(self):
        for text, prose in [
            ('explicitly let these layers approximate a residual function', 'explicitly let these layers'),
            ('(x) := H (x) − x. The original function thus becomes', 'The original function thus becomes'),
            ('(x)+x. Although both forms should be able to asymptot-', 'Although both forms'),
            ('y = F (x, { W i } ) + W s x. (2) We can also use a square matrix W', 'We can also use a square matrix'),
            ('in Eqn.(1). But we will', 'But we will')]:
            with self.subTest(text=text):
                page = {'blocks': [{'id': 'b', 'en': text,
                    'protected_spans': [{'start': 0, 'end': len(text), 'kind': 'formula'}]}]}
                source, replacements, local = translation_input(page)
                self.assertFalse(local)
                self.assertIn(prose, source[0]['en'])
                self.assertEqual(restore_protected({'b': source[0]['en']}, replacements, local)['b'], text)

    def test_leading_footnote_does_not_freeze_joined_sentence(self):
        text = '²Thishypothesis,however,isstillanopenquestion.'
        page = {'blocks': [{'id': 'b', 'en': text, 'numeric_spans': [{'start': 0, 'end': len(text)}]}]}
        source, replacements, local = translation_input(page)
        self.assertFalse(local)
        self.assertIn('Thishypothesis', source[0]['en'])
        self.assertEqual(list(replacements['b'].values()), ['²'])

    def test_math_and_table_protection_are_not_discarded(self):
        for text, kind in [('y = F (x, { W i } ) + W s x. (2)', 'formula'),
                ('softmax alpha and beta', 'formula'),
                ('functions² = 4', 'formula'),
                ('The original function thus becomes', 'table')]:
            with self.subTest(text=text):
                page = {'blocks': [{'id': 'b', 'en': text,
                    'protected_spans': [{'start': 0, 'end': len(text), 'kind': kind}]}]}
                _, _, local = translation_input(page)
                self.assertEqual(local, {'b': text})

    def test_leading_math_names_remain_protected(self):
        for text in ['²Alpha', '²Alpha,', '²ResNet', '²MassModel']:
            with self.subTest(text=text):
                page = {'blocks': [{'id': 'b', 'en': text,
                    'numeric_spans': [{'start': 0, 'end': len(text)}]}]}
                _, _, local = translation_input(page)
                self.assertEqual(local, {'b': text})

    def test_symbols_survive_prose_exposure_without_mutating_metadata(self):
        text = 'y = F (x) + x. The original function thus becomes H (x).'
        span = {'start': 0, 'end': len(text), 'kind': 'formula'}
        page = {'blocks': [{'id': 'b', 'en': text, 'protected_spans': [span]}]}
        source, replacements, local = translation_input(page)
        self.assertEqual(span, {'start': 0, 'end': len(text), 'kind': 'formula'})
        self.assertIn('y = F (x) + x.', ''.join(replacements['b'].values()))
        self.assertIn('H (x).', ''.join(replacements['b'].values()))
        self.assertEqual(restore_protected({'b': source[0]['en']}, replacements, local)['b'], text)

    def test_invalid_ranges_rejected_before_narrowing(self):
        page = {'blocks': [{'id': 'b', 'en': 'The original function thus becomes',
            'protected_spans': [{'start': -1, 'end': 20, 'kind': 'formula'}]}]}
        with self.assertRaises(ValueError):
            translation_input(page)


if __name__ == '__main__':
    unittest.main()
