import copy
import unittest
from stock_research.model_inputs import financial_table_view
from stock_research.financial_facts import CONTRACT


class FinancialTableInputTests(unittest.TestCase):
    def sample(self):
        return {'fact_contract':CONTRACT,'metrics':[
            {'metric':'revenue','scope':'cumulative','period_end':'2025-12-31','value':-1200.05,'standard_value':-12000500,
             'unit':'ten_thousand_yuan','source_page':7,'quote':'营业收入 | -1,200.05万元\n本年值','verified':True,'fact_id':'source'},
            {'metric':'roe','scope':'cumulative','value':99,'quote':'不能使用的候选99%','verified':False,'verification_status':'unresolved'}],
            'sections':[{'analysis':'审计意见、关联交易和业务风险原文保留'}],
            'data_quality':{'expected_count':13,'verified_count':1,'status':'review','analysis_ready':True,
                            'missing':[{'metric':'roe','status':'parser_unresolved'}],
                            'not_applicable':[],'checks':[{'name':'资产=负债+权益','passed':True,'difference':0,'tolerance':.02}]}}

    def test_signed_value_unit_period_page_and_quote_are_preserved_once(self):
        raw=self.sample();before=copy.deepcopy(raw);view=financial_table_view(raw)
        table=view['verified_metrics_markdown']
        for value in ('-1200.05','ten_thousand_yuan','2025-12-31','|7|','营业收入 \\| -1,200.05万元<br>本年值'):
            self.assertIn(value,table)
        self.assertEqual(view['metrics'][0]['metric'],'roe')
        self.assertNotIn('value',view['metrics'][0]);self.assertNotIn('quote',view['metrics'][0])
        self.assertEqual(view['sections'],raw['sections']);self.assertEqual(raw,before)
        self.assertEqual(view['data_quality']['missing'],raw['data_quality']['missing'])

    def test_legacy_unverified_flags_do_not_gain_trust_by_changing_format(self):
        raw=self.sample();raw.pop('fact_contract')
        self.assertNotIn('verified_metrics_markdown',financial_table_view(raw))

    def test_per_share_cashflow_is_not_total_cashflow(self):
        from stock_research.financial_evidence import metric_name
        self.assertIsNone(metric_name('每股经营活动产生的现金流量净额'))
        self.assertEqual(metric_name('经营活动产生的现金流量净额'),'operating_cashflow')
