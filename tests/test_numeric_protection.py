"""Numeric protection must not freeze ordinary prose attached to a footnote."""
import unittest
from paper_reader.scientific import translation_input, restore_protected


class NumericProtectionTest(unittest.TestCase):
    def test_prose_word_with_footnote_stays_translatable(self):
        text = 'competitions¹, where we won.'
        page = {'blocks': [{'id': 'b', 'en': text,
            'numeric_scripts': [{'start': 12, 'end': 13, 'kind': 'superscript'}],
            'numeric_spans': [{'start': 0, 'end': 14}]}]}
        source, replacements, local = translation_input(page)
        self.assertFalse(local)
        self.assertIn('competitions', source[0]['en'])
        keep = replacements['b']
        self.assertEqual(list(keep.values()), ['¹'])
        token = next(iter(keep))
        restored = restore_protected({'b': '竞赛' + token + '，我们获胜。'}, replacements, local)
        self.assertEqual(restored['b'], '竞赛¹，我们获胜。')

    def test_scientific_exponent_and_formula_remain_exact(self):
        text = 'The complexity is 10³ and H₂O remains.'
        page = {'blocks': [{'id': 'b', 'en': text,
            'numeric_spans': [{'start': 18, 'end': 21}, {'start': 26, 'end': 29}]}]}
        source, replacements, local = translation_input(page)
        self.assertIn('10³', replacements['b'].values())
        self.assertIn('H₂O', replacements['b'].values())
        result = restore_protected({'b': source[0]['en']}, replacements, local)
        self.assertEqual(result['b'], text)

    def test_formula_region_overrides_prose_shaped_word(self):
        text = 'functions² = 4'
        page = {'blocks': [{'id': 'b', 'en': text, 'numeric_spans': [{'start': 0, 'end': 10}],
            'protected_spans': [{'start': 0, 'end': len(text), 'kind': 'formula'}]}]}
        _, _, local = translation_input(page)
        self.assertEqual(local['b'], text)

    def test_short_variables_model_names_functions_and_subscripts_stay_protected(self):
        for text in ['x²', 'β₁', 'ResNet¹,', 'softmax²', 'alpha²', 'competitions₁']:
            with self.subTest(text=text):
                page = {'blocks': [{'id': 'b', 'en': text, 'numeric_spans': [{'start': 0, 'end': len(text)}]}]}
                _, _, local = translation_input(page)
                self.assertEqual(local['b'], text)


if __name__ == '__main__':
    unittest.main()
