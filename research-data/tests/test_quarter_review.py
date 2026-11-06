import copy
import unittest
from stock_research.financial_periods import build as bridge
from stock_research.quarter_review import build, model_view
from test_financial_periods import fact


class QuarterReviewTests(unittest.TestCase):
    def source(self):
        periods = {
            '2026Q1': [13236777393.86, 3660674643.20, 3400165713.98, 5563194631.76, 8285655074.11],
            '2026H1': [30197583541.33, 9727818696.50, 9209887019.74, 11964338495.35, 17042539798.70],
            '2026Q3': [47087860645.48, 14316756859.87, 14127666746.15, 18800459588.86, 26919033105.64]}
        return {p: [fact(p, v, name=k) for k, v in zip(('revenue', 'net_profit', 'adjusted_profit', 'operating_cashflow', 'operating_cost'), values)] for p, values in periods.items()}

    def test_baofeng_verified_quarter_decline_and_margin_are_not_lost(self):
        filings = self.source(); original = copy.deepcopy(filings)
        review = build(bridge('2026Q3', filings), asof='2026-10-23')
        self.assertIn('归母净利润环比下降24.36%', review['summary'])
        self.assertIn('扣非净利润环比下降15.35%', review['summary'])
        self.assertIn('48.37%降至41.53%', review['summary'])
        self.assertIn('6.84个百分点', review['summary'])
        self.assertEqual(filings, original)
        compact = model_view(review)
        self.assertTrue(compact['source_facts'])
        self.assertIn('operating_cost', [m['metric'] for m in compact['source_facts'].values()])

    def test_missing_or_unverified_or_restatement_never_becomes_decline(self):
        for mutation in ({'verified': False}, {'restatement_warning': True}, {'period_end': '2025-06-30'}):
            filings = self.source()
            for m in filings['2026H1']: m.update(mutation)
            review = build(bridge('2026Q3', filings))
            self.assertEqual(review['summary'], '')
            self.assertIn('net_profit', review['missing'])
            self.assertIn('gross_margin', review['missing'])

    def test_negative_base_is_loss_narrowing_not_growth_and_turnaround_is_named(self):
        for now, before, wording in [(-5, -10, '减亏50.00%'), (-15, -10, '亏损扩大50.00%'), (3, -10, '扭亏为盈'), (-3, 10, '由盈转亏')]:
            result = build(bridge('2026H1', {'2026Q1': [fact('2026Q1', before)], '2026H1': [fact('2026H1', now + before)]}))
            self.assertIn(wording, result['summary'])
            self.assertNotIn('增长', result['summary'])

    def test_zero_base_has_no_infinite_rate(self):
        result = build(bridge('2026H1', {'2026Q1': [fact('2026Q1', 0)], '2026H1': [fact('2026H1', -3)]}))
        self.assertIn('由盈转亏', result['summary'])
        self.assertIsNone(result['changes'][0]['qoq_pct'])

    def test_financial_total_revenue_cannot_manufacture_gross_margin(self):
        filings = self.source()
        for metrics in filings.values():
            for m in metrics:
                if m['metric'] == 'revenue': m['evidence']['label']['text'] = '营业总收入'
        result = bridge('2026Q3', filings)
        self.assertIsNone(result['gross_margin']['current'])
        self.assertIn('gross_margin', build(result)['missing'])

    def test_model_input_preserves_dedicated_review_without_promoting_old_history(self):
        from stock_research.analysis import narrative_input
        review = model_view(build(bridge('2026Q3', self.source())))
        payload = narrative_input({'company': '测试公司', 'period': '2026Q3'}, {'metrics': []}, {'_quarter_review_input': review}, {}, {})
        self.assertEqual(payload['quarter_review'], review)
        self.assertEqual(payload['financial'], {'metrics': []})


if __name__ == '__main__': unittest.main()
