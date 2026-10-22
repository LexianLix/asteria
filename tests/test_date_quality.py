"""Compare dates semantically without suppressing changed dates or other numbers."""
import unittest
from paper_reader.reader import quality_warnings


class DateQualityTest(unittest.TestCase):
    def test_equivalent_english_and_chinese_dates_are_not_numeric_errors(self):
        for source, translated in [
            ('Blog (March 5, 2026)', '博客（2026 年 3 月 5 日）'),
            ('Published Jan. 2nd, 2024, 120 samples.', '发表于2024年1月2日，共 120 个样本。'),
            ('5 March 2026', '2026年3月5日'),
            ('March 5,2026', '2026年3月5日'),
        ]:
            with self.subTest(source=source):
                self.assertEqual(quality_warnings(source, translated), [])

    def test_wrong_month_day_or_year_still_warns(self):
        for translated in ['2026年4月5日', '2026年3月6日', '2025年3月5日']:
            with self.subTest(translated=translated):
                self.assertTrue(quality_warnings('March 5, 2026', translated))

    def test_month_day_swap_is_not_hidden_by_same_number_multiset(self):
        self.assertTrue(quality_warnings('March 5, 2026', '2026年5月3日'))
        self.assertTrue(quality_warnings('March 5, 2026', 'May 5, 2026'))

    def test_dates_do_not_hide_changed_non_date_numbers(self):
        self.assertTrue(quality_warnings('March 5, 2026; 120 samples', '2026年3月5日；12 个样本'))

    def test_multiple_dates_and_duplicate_dates_are_compared(self):
        self.assertEqual(quality_warnings('March 5, 2026 and April 6, 2026', '2026年4月6日和2026年3月5日'), [])
        self.assertTrue(quality_warnings('March 5, 2026 and March 5, 2026', '2026年3月5日'))

    def test_invalid_calendar_dates_are_not_silently_normalized(self):
        self.assertTrue(quality_warnings('February 30, 2026', '2026年3月2日'))
        self.assertEqual(quality_warnings('February 29, 2024', '2024年2月29日'), [])

    def test_all_months_and_other_quality_checks_remain_active(self):
        for month, name in enumerate(('January', 'February', 'March', 'April', 'May', 'June',
                'July', 'August', 'September', 'October', 'November', 'December'), 1):
            with self.subTest(month=name):
                self.assertEqual(quality_warnings(f'{name} 12, 2026', f'2026年{month}月12日'), [])
        self.assertTrue(quality_warnings('March 5, 2026 [1] 256²', '2026年3月5日 [2] 256³'))


if __name__ == '__main__':
    unittest.main()
