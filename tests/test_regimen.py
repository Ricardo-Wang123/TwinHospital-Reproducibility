from copy import deepcopy
import unittest

from sandbox.experiments.linked.regimen import normalize_frequency


class FrequencyEncodingTests(unittest.TestCase):
    def setUp(self):
        self.codes = {'QD': 'once-daily', 'BID': 'twice-daily', 'TID': 'three-daily',
                      'QID': 'four-daily', 'Q12H': 'interval-12h', 'Q8H': 'interval-8h',
                      'Q6H': 'interval-6h', 'PRN': 'as-needed'}

    def test_real_failure_aliases_preserve_original_and_dictionary(self):
        original = deepcopy(self.codes)
        for raw, canonical in [('twice daily', 'BID'), ('每日一次', 'QD'), ('  TWICE   DAILY  ', 'BID'),
                               ('每日2次', 'BID'), ('ＢＩＤ', 'BID'), ('q.d.', 'QD')]:
            with self.subTest(raw=raw):
                result = normalize_frequency(raw, self.codes)
                self.assertEqual(result['original'], raw)
                self.assertEqual(result['canonical'], canonical)
                self.assertFalse(result['dose_or_schedule_changed'])
                self.assertEqual(result['clinical_appropriateness'], 'not_assessed')
        self.assertEqual(self.codes, original)

    def test_interval_semantics_are_not_collapsed_to_daily_counts(self):
        self.assertEqual(normalize_frequency('every 12 hours', self.codes)['canonical'], 'Q12H')
        self.assertEqual(normalize_frequency('Q12H', self.codes)['canonical'], 'Q12H')
        self.assertEqual(normalize_frequency('每8小时一次', self.codes)['canonical'], 'Q8H')
        with self.assertRaises(ValueError):
            normalize_frequency('every 12 hours', {'BID': 'twice-daily'})

    def test_prn_ranges_and_unknowns_are_not_inferred(self):
        self.assertEqual(normalize_frequency('PRN', self.codes)['canonical'], 'PRN')
        for raw in ('once or twice daily', '1-2/day', 'BID PRN', 'twice daily as needed',
                    '按需每日1-2次', 'as needed', '每日一次睡前', 'every other day', '', None, 2):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_frequency(raw, self.codes)

    def test_known_alias_requires_native_support(self):
        with self.assertRaises(ValueError):
            normalize_frequency('twice daily', {'QD': 'once-daily'})


if __name__ == '__main__':
    unittest.main()
