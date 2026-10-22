"""Numbers next to Chinese or PDF-glued Latin prose remain checkable."""
import unittest
from paper_reader.reader import quality_warnings


class NumericBoundaryTest(unittest.TestCase):
    def test_chinese_adjacent_numbers_compare_equal(self):
        self.assertEqual(quality_warnings('We use a batch size of 256 and rate 0.1.',
            '我们使用大小为256的小批量和0.1的学习率。'), [])
        self.assertEqual(quality_warnings('Fig. 3 and Table 1: 34 layers, 3.6 FLOPs.',
            '图3和表1：34层，3.6 FLOPs。'), [])

    def test_changed_chinese_numbers_still_warn(self):
        self.assertTrue(quality_warnings('Batch 256, rate 0.1.', '小批量128，学习率0.1。'))
        self.assertTrue(quality_warnings('Batch 256, rate 0.1.', '小批量256，学习率0.2。'))

    def test_glued_source_prose_and_model_identifiers_are_not_silently_ignored(self):
        self.assertEqual(quality_warnings('aplainnetworkwith34 parameterlayers', '具有34个参数层的普通网络'), [])
        self.assertTrue(quality_warnings('ResNet50', 'ResNet152'))
        self.assertTrue(quality_warnings('classes1000', '类别100'))

    def test_fragmented_graphic_digits_remain_warning(self):
        self.assertTrue(quality_warnings('s iz e : 2 2 4', '尺寸：224'))
        self.assertEqual(quality_warnings('32x32 and 60 × 10⁴', '32x32和60 × 10⁴'), [])


if __name__ == '__main__':
    unittest.main()
