"""Real ResNet footnote: a glued sentence must not become an immutable token."""
import unittest
from copy import deepcopy
from paper_reader.scientific import translation_input, restore_protected

TEXT = ('³Wehaveexperimentedwithmoretrainingiterations(3×)andstillob- '
    'served the degradation problem, suggesting that this problem cannot be '
    'feasiblyaddressedbysimplyusingmoreiterations.')


def fixture(text=TEXT, kind=None):
    prefix = text.split(' ', 1)[0]
    block = {'id': 'b', 'en': text, 'numeric_spans': [{'start': 0, 'end': len(prefix)}]}
    if kind: block['protected_spans'] = [{'start': 0, 'end': len(prefix), 'kind': kind}]
    return {'blocks': [block]}


class FusedFootnoteTest(unittest.TestCase):
    def test_real_glued_prefix_is_visible_and_marker_multiplier_remain_exact(self):
        page = fixture(); before = deepcopy(page)
        source, replacements, local = translation_input(page)
        self.assertFalse(local)
        self.assertIn('Wehaveexperimentedwithmoretrainingiterations(', source[0]['en'])
        self.assertIn('andstillob-', source[0]['en'])
        self.assertEqual(list(replacements['b'].values()), ['³', '3×'])
        tokens = list(replacements['b'])
        actual = restore_protected({'b': tokens[0] + '我们使用更多训练迭代（' + tokens[1] + '），仍观察到退化。'}, replacements, local)
        self.assertEqual(actual['b'], '³我们使用更多训练迭代（3×），仍观察到退化。')
        self.assertEqual(page, before)

    def test_geometric_formula_and_table_still_win_over_footnote_shape(self):
        for kind in ('formula', 'table'):
            with self.subTest(kind=kind):
                source, replacements, _ = translation_input(fixture(kind=kind))
                self.assertNotIn('Wehave', source[0]['en'])
                self.assertIn(TEXT.split(' ', 1)[0], replacements['b'].values())

    def test_scientific_and_unsupported_identifiers_are_not_exposed(self):
        for text in ('³WehaveVariable', '²H₂O', '²WeibullModel', '³WeHaveModel(3×)',
                '³Wehaveexperimentedwithmoretrainingiterations(3×)=y'):
            with self.subTest(text=text):
                source, _, local = translation_input(fixture(text))
                self.assertEqual(source, [])
                self.assertEqual(local, {'b': text})
