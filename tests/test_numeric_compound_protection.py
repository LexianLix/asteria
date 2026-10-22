"""A numerical power must not freeze an ordinary hyphenated layer noun."""
import unittest
from paper_reader.scientific import translation_input, restore_protected


def fixture(text, kind=None):
    block = {'id': 'b', 'en': text, 'numeric_spans': [{'start': 0, 'end': len(text)}]}
    if kind:
        block['protected_spans'] = [{'start': 0, 'end': len(text), 'kind': kind}]
    return {'blocks': [block]}


class NumericCompoundProtectionTest(unittest.TestCase):
    def test_layer_noun_visible_while_power_is_exact(self):
        source, replacements, local = translation_input(fixture('10³-layer'))
        self.assertFalse(local)
        self.assertIn('-layer', source[0]['en'])
        self.assertEqual(list(replacements['b'].values()), ['10³'])
        token = next(iter(replacements['b']))
        self.assertEqual(restore_protected({'b': token + '层'}, replacements, local)['b'], '10³层')

    def test_signed_power_plural_and_punctuation(self):
        for text, prefix, suffix in [('10⁻³-layer', '10⁻³', '-layer'),
                ('10³-layers,', '10³', '-layers,'), ('1.5²-layer.', '1.5²', '-layer.')]:
            with self.subTest(text=text):
                source, replacements, local = translation_input(fixture(text))
                self.assertFalse(local)
                self.assertIn(suffix, source[0]['en'])
                self.assertEqual(list(replacements['b'].values()), [prefix])
                self.assertEqual(restore_protected({'b': source[0]['en']}, replacements, local)['b'], text)

    def test_geometric_formula_or_table_still_protects_whole_expression(self):
        for kind in ('formula', 'table'):
            with self.subTest(kind=kind):
                _, _, local = translation_input(fixture('10³-layer', kind))
                self.assertEqual(local, {'b': '10³-layer'})

    def test_identifiers_chemistry_subscripts_and_unknown_suffixes_unchanged(self):
        for text in ('H₂O-layer', 'x³-layer', '10₃-layer', '10³-Layer', '10³-layervar', '10³-x', '10³-layer⁻¹'):
            with self.subTest(text=text):
                _, _, local = translation_input(fixture(text))
                self.assertEqual(local, {'b': text})

    def test_duplicate_compounds_get_independent_exact_markers(self):
        text = '10³-layer and 10³-layer'
        page = {'blocks': [{'id': 'b', 'en': text,
            'numeric_spans': [{'start': 0, 'end': 9}, {'start': 14, 'end': 23}]}]}
        source, replacements, local = translation_input(page)
        self.assertEqual(source[0]['en'].count('-layer'), 2)
        self.assertEqual(list(replacements['b'].values()), ['10³', '10³'])
        self.assertEqual(restore_protected({'b': source[0]['en']}, replacements, local)['b'], text)

    def test_invalid_original_range_rejected_before_narrowing(self):
        page = fixture('10³-layer')
        page['blocks'][0]['numeric_spans'][0]['end'] += 10
        with self.assertRaises(ValueError):
            translation_input(page)


if __name__ == '__main__': unittest.main()
