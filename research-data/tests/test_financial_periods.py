import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_research.financial_periods import build, required_periods


def fact(period, value, scope='cumulative', name='net_profit', **extra):
    end={'Q1':'03-31','H1':'06-30','Q3':'09-30','FY':'12-31'}[period[4:]]
    return dict(metric=name, standard_value=value, unit='yuan', scope=scope, period_end=period[:4]+'-'+end,
                verified=True, fact_id=period+name+scope, source_page=2,
                evidence={'source_sha256':'a'*64,'entity':'listed_consolidated_summary','label':{'text':'营业收入' if name=='revenue' else name}}, **extra)


class FinancialPeriodsTests(unittest.TestCase):
    def net(self, period, filings):
        return next(r for r in build(period,filings)['rows'] if r['metric']=='net_profit')

    def test_q3_turnaround_is_not_hidden_by_ytd_loss(self):
        filings={'2026Q3':[fact('2026Q3',-5495.60),fact('2026Q3',29431752.01,'single_quarter')],
                 '2026H1':[fact('2026H1',-29437247.61)],'2026Q1':[fact('2026Q1',-3510681.86)],
                 '2025Q3':[fact('2025Q3',1860201.27,'single_quarter')]}
        original=copy.deepcopy(filings);r=self.net('2026Q3',filings)
        self.assertEqual(r['current']['standard_value'],29431752.01)
        self.assertEqual(r['previous']['standard_value'],-25926565.75)
        self.assertEqual(r['cumulative']['standard_value'],-5495.60)
        self.assertEqual(r['prior_year']['standard_value'],1860201.27)
        self.assertTrue(r['previous']['derived']);self.assertEqual(filings,original)

    def test_annual_q4_and_q1_previous_year_q4(self):
        filings={'2025FY':[fact('2025FY',100)],'2025Q3':[fact('2025Q3',70),fact('2025Q3',15,'single_quarter')],
                 '2026Q1':[fact('2026Q1',40)]}
        annual=self.net('2025FY',filings)
        self.assertEqual(annual['current']['standard_value'],30)
        self.assertEqual(annual['current']['method'],'全年减前三季度')
        self.assertEqual(annual['previous']['standard_value'],15)
        q1=self.net('2026Q1',filings)
        self.assertEqual(q1['current']['standard_value'],40)
        self.assertEqual(q1['previous']['standard_value'],30)

    def test_q2_yoy_uses_matching_single_quarters_not_two_cumulative_halves(self):
        filings={p:[fact(p,v)] for p,v in [('2026H1',50),('2026Q1',35),('2025H1',30),('2025Q1',10)]}
        r=self.net('2026H1',filings)
        self.assertEqual(r['current']['standard_value'],15)
        self.assertEqual(r['prior_year']['standard_value'],20)
        self.assertEqual(r['previous']['standard_value'],35)

    def test_missing_unverified_wrong_year_parent_and_ratio_never_become_quarter(self):
        for bad in [dict(verified=False),dict(period_end='2025-06-30'),dict(unit='percentage'),dict(fact_id=None),dict(evidence={'entity':'parent','source_sha256':'a'*64})]:
            m=fact('2026H1',100);m.update(bad)
            self.assertIsNone(self.net('2026H1',{'2026H1':[m],'2026Q1':[fact('2026Q1',30)]})['current'])
        self.assertIsNone(self.net('2026H1',{'2026H1':[fact('2026H1',100)]})['current'])

    def test_rounded_yoy_is_never_inverted_and_zero_is_not_missing(self):
        r=self.net('2026Q3',{'2026Q3':[fact('2026Q3',0,'single_quarter',yoy_pct=-100)]})
        self.assertEqual(r['current']['standard_value'],0)
        self.assertIsNone(r['prior_year'])

    def test_restatement_stops_cross_filing_subtraction(self):
        r=self.net('2026H1',{'2026H1':[fact('2026H1',100,restatement_warning=True)],'2026Q1':[fact('2026Q1',30)]})
        self.assertIsNone(r['current'])

    def test_operating_and_total_revenue_cannot_be_mixed(self):
        latest=fact('2026H1',100,name='revenue');prior=fact('2026Q1',30,name='revenue')
        prior['evidence']['label']['text']='营业总收入'
        result=build('2026H1',{'2026H1':[latest],'2026Q1':[prior]})
        self.assertIsNone(result['rows'][0]['current'])
        operating=fact('2026Q1',25,name='operating_revenue')
        result=build('2026H1',{'2026H1':[latest],'2026Q1':[prior,operating]})
        self.assertEqual(result['rows'][0]['current']['standard_value'],75)

    def test_targets_are_bounded_and_year_boundary_is_correct(self):
        self.assertEqual(required_periods('invalid'),[])
        self.assertEqual(required_periods('2026Q3'),['2026Q3','2026H1','2026Q1','2025Q3','2025H1','2025Q1'])
        self.assertIn('2025FY',required_periods('2026Q1'))
        self.assertIn('2025Q3',required_periods('2026Q1'))
        self.assertLessEqual(len(required_periods('2026FY')),8)

    def test_previous_quarter_and_q1_annual_are_distinct_periods(self):
        filings={'2026Q1':[fact('2026Q1',40)],'2025FY':[fact('2025FY',100)],'2025Q3':[fact('2025Q3',70),fact('2025Q3',15,'single_quarter')]}
        result=build('2026Q1',filings)
        self.assertEqual(result['annual_period'],'2025FY')
        self.assertEqual(next(m for m in result['annual_metrics'] if m['metric']=='net_profit')['standard_value'],100)
        previous=next(r for r in result['previous_rows'] if r['metric']=='net_profit')
        self.assertEqual(previous['current']['standard_value'],30)
        self.assertEqual(previous['previous']['standard_value'],15)

    def test_enrichment_rejects_future_other_company_and_wrong_source_hash(self):
        from stock_research.financial_periods import enrich
        doc={'code':'301221','market':'SZ','period':'2026Q3','sha256':'a'*64,'id':'current'}
        future={**doc,'period':'2026H1','published_at':'2026-10-23','id':'future'}
        other={**doc,'code':'600001','period':'2026Q1','published_at':'2026-04-01','id':'other'}
        class Store:
            def rows(self,*args):return [future,other]
        with patch('stock_research.research_inputs.supporting_index',return_value=Path('/nonexistent-test-support-index')),patch('stock_research.financial_evidence.primary_metrics') as proof:
            result=enrich(Store(),doc,{'metrics':[fact('2026Q3',30,'single_quarter')]},asof='2026-10-22')
            self.assertFalse(proof.called)
            r=next(x for x in result['rows'] if x['metric']=='net_profit')
            self.assertEqual(r['current']['standard_value'],30);self.assertIsNone(r['previous'])
            wrong=fact('2026Q3',30,'single_quarter');wrong['evidence']['source_sha256']='b'*64
            result=enrich(Store(),doc,{'metrics':[wrong]},asof='2026-10-22')
            self.assertIsNone(next(x for x in result['rows'] if x['metric']=='net_profit')['current'])


if __name__=='__main__': unittest.main()
