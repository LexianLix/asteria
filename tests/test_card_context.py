"""No-inference regressions for bounded, balanced research-card evidence selection."""
from types import SimpleNamespace
import unittest
from paper_reader.assistant import PaperAssistant, ContextRequest


class CardContextTest(unittest.TestCase):
    def prepare(self, records, selected='abstract', kind='card'):
        pages = [{'number': n, 'blocks': [b for b in records if b['page'] == n]} for n in range(5)]
        doc = {'source_hash': 'a' * 64, 'pages': pages}
        cfg = {'provider': 'openai', 'model': 'offline-fixture', 'base_url': 'http://127.0.0.1:1'}
        reader = SimpleNamespace(db=None, document=lambda pid: doc, config=lambda **kw: cfg)
        return PaperAssistant(reader).prepare(1, ContextRequest(kind=kind, block_id=selected,
            question='按原文整理问题、方法、结果和局限；缺少依据的字段明确未知。'))

    @staticmethod
    def block(identifier, page, text):
        return {'id': identifier, 'page': page, 'box': [0, 0, 1, 1], 'en': text}

    def crowded(self):
        return [self.block('abstract', 0, 'Abstract. This paper introduces a new method.'),
            *[self.block(f'result-{n}', 1, 'Result experiment evaluation benchmark baseline accuracy performance. ' * 4)
              for n in range(35)],
            self.block('method', 2, 'Method. The architecture uses a residual framework.'),
            self.block('challenge', 4, 'Translation-related edits remain relatively challenging, indicating room for further improvement.'),
            self.block('overfit', 4, 'The deeper network is worse on testing. We argue that this is because of overfitting.')]

    def test_limitations_and_method_not_crowded_out_by_repeated_results(self):
        preview = self.prepare(self.crowded())
        ids = {b['id'] for b in preview['sources']}
        self.assertTrue({'abstract', 'method', 'challenge', 'overfit'} <= ids, ids)
        self.assertLessEqual(preview['characters'], 40000)
        self.assertLessEqual(preview['included_blocks'], 32)
        self.assertTrue(preview['partial'])

    def test_output_order_exact_text_and_preview_binding_remain_stable(self):
        records = self.crowded()
        first, second = self.prepare(records), self.prepare(records)
        self.assertEqual(first['preview_id'], second['preview_id'])
        chosen = set(b['id'] for b in first['sources'])
        self.assertEqual(first['sources'], [b for b in records if b['id'] in chosen])
        records[-1]['en'] += ' New source limitation.'
        self.assertNotEqual(first['preview_id'], self.prepare(records)['preview_id'])

    def test_whole_blocks_and_selected_source_survive_large_budget_pressure(self):
        records = [self.block('abstract', 0, 'Abstract: ' + 'x' * 20000),
            *[self.block(f'm-{n}', 2, 'Method architecture training ' + 'm' * 9000) for n in range(8)],
            self.block('limits', 4, 'Limitations. Generalization is not tested and remains an open problem.')]
        preview = self.prepare(records)
        ids = {b['id'] for b in preview['sources']}
        self.assertTrue({'abstract', 'limits'} <= ids)
        self.assertLessEqual(preview['characters'], 40000)
        self.assertEqual(next(b['en'] for b in preview['sources'] if b['id'] == 'abstract'), records[0]['en'])

    def test_short_limitation_heading_keeps_plain_continuation_as_evidence(self):
        records = self.crowded()[:-2] + [self.block('heading', 3, '5.5 Limitations'),
            self.block('body', 4, 'Only three random seeds were used. This is a narrow simulated setting.')]
        ids = {b['id'] for b in self.prepare(records)['sources']}
        self.assertTrue({'heading', 'body'} <= ids)


if __name__ == '__main__':
    unittest.main()
