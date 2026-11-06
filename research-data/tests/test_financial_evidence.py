import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from stock_research.financial_evidence import table_metrics, enrich_extraction, aligned_summary_tables, annual_continuation, annual_summary_unit_continues,rounding_tolerance,values_agree
from stock_research.market_metrics import build_market_snapshot
from unittest.mock import patch


class EvidenceTests(unittest.TestCase):
    def test_summary_rounding_to_ten_thousand_yuan_does_not_create_false_conflicts(self):
        tolerance=rounding_tolerance('-25,446.17','ten_thousand_yuan')
        self.assertEqual(tolerance,50)
        summary={'standard_value':-254461700,'rounding_tolerance':tolerance}
        self.assertTrue(values_agree(summary,{'standard_value':-254461653.07}))
        self.assertFalse(values_agree(summary,{'standard_value':-254461600}))
        self.assertEqual(rounding_tolerance('1.00','yuan'),.005)
    def test_annual_quarter_breakdown_never_inherits_year_columns(self):
        annual=[['主要财务指标','2023年','2022年'],['基本每股收益（元）','1','2']]
        quarters=[['项目','第一季度','第二季度','第三季度','第四季度'],['营业收入','10','20','30','40']]
        continuation=[['经营活动产生的现金流量净额','-60','-40','-3','63']]
        result=table_metrics([(8,[annual]),(9,[quarters]),(10,[continuation])],'2023FY',{8:'yuan',9:'yuan',10:'yuan'})
        self.assertFalse(any(m['metric']=='operating_cashflow' for m in result))

    def test_cross_page_annual_roe_requires_the_proven_year_cell_band(self):
        table=SimpleNamespace(extract=lambda:[['主要财务指标','2024年','2023年']],
                              rows=[SimpleNamespace(cells=[(60,740,220,760),(220,740,300,760),(300,740,370,760)])])
        previous=SimpleNamespace(height=842,find_tables=lambda:[table])
        current=SimpleNamespace(extract_words=lambda:[{'text':'加权平均净资产收益率（%）','x0':64,'x1':195,'top':151},
            {'text':'5.69','x0':264,'x1':285,'top':151},{'text':'5.92','x0':330,'x1':352,'top':151}])
        result=annual_continuation(previous,current,'2024FY',6)
        self.assertEqual(result[0]['standard_value'],5.69)
        self.assertEqual(result[0]['corroboration_page'],5)

    def test_cross_page_unit_requires_annual_summary_context_on_both_pages(self):
        self.assertTrue(annual_summary_unit_continues('主要会计数据 单位：元','主要会计数据 2023年 2022年','2023FY'))
        self.assertFalse(annual_summary_unit_continues('单位：万元 其他资料','主要会计数据 2023年','2023FY'))
        self.assertFalse(annual_summary_unit_continues('主要会计数据 单位：元','其他现金流说明','2023FY'))
    def test_original_public_pdf_grids_recover_labels_and_period_columns(self):
        fixtures=json.loads((Path(__file__).parent/'fixtures/broken_summary_grids.json').read_text())
        expected=[(646552241.4,884962475.45),(111345574.29,151367166.64)]
        for fixture,(profit,cash) in zip(fixtures,expected):
            table=SimpleNamespace(bbox=fixture['bbox'],extract=lambda:fixture['raw'],rows=[SimpleNamespace(cells=r) for r in fixture['rows']])
            page=SimpleNamespace(find_tables=lambda:[table],crop=lambda box:SimpleNamespace(extract_words=lambda:fixture['words']))
            recovered=aligned_summary_tables(page,fixture['period'])
            metrics=table_metrics([(fixture['page'],recovered)],fixture['period'],{fixture['page']:'yuan'})
            by={(m['metric'],m['scope']):m for m in metrics}
            self.assertEqual(by['net_profit','cumulative']['standard_value'],profit)
            self.assertEqual(by['operating_cashflow','cumulative']['standard_value'],cash)
            self.assertNotEqual(by['net_profit','single_quarter']['standard_value'],profit)

    def test_cumulative_and_single_quarter_use_separate_columns(self):
        table=[['项目','本报告期','本报告期比上年同期增减','年初至报告期末','年初至报告期末比上年同期增减'],
               ['营业收入（元）','10.00','-5.0%','30.00','20%'],
               ['归属于上市公司股东的扣除非经常性损益\n的净利润（元）','2.00','10%','5.00','20%'],
               ['经营活动产生的现金流量净额（元）','—','—','-3.00','-200%']]
        values=table_metrics([(1,[table])],'2026Q3')
        by={(m['metric'],m['scope']):m for m in values}
        self.assertEqual(by['revenue','cumulative']['value'],30)
        self.assertEqual(by['revenue','single_quarter']['value'],10)
        self.assertEqual(by['adjusted_profit','cumulative']['value'],5)
        self.assertEqual(by['operating_cashflow','cumulative']['value'],-3)
        self.assertEqual(by['revenue','cumulative']['source_column'],4)
        self.assertNotIn(('operating_cashflow','single_quarter'),by)

    def test_sparse_wrapped_rows_and_headers_preserve_columns(self):
        table=[['',None,'本报告期',None,'本报告期比上年','年初至报告期末','年初至报告期末'],
               [None,None,None,None,'同期增减',None,'比上年同期增减'],
               ['', '归属于上市公司股东的扣除非经常性损','2',None,'10%','5','20%'],
               [None,'益的净利润（元）',None,None,None,None,None]]
        values=table_metrics([(1,[table])],'2026Q3')
        self.assertEqual([(m['scope'],m['value']) for m in values],[('single_quarter',2),('cumulative',5)])

    def test_cross_page_continuation_and_percentage_units(self):
        first=[['项目','本报告期','同比','年初至报告期末','同比'],['营业收入','10','5','30','8']]
        second=[['加权平均净资产收益率（%）','1.2','1.0','3.4','2.0']]
        values=table_metrics([(1,[first]),(2,[second])],'2026Q3',{1:'ten_thousand_yuan'})
        cumulative=next(m for m in values if m['metric']=='roe' and m['scope']=='cumulative')
        self.assertEqual(cumulative['standard_value'],3.4)
        self.assertEqual(cumulative['source_page'],2)
        self.assertEqual(next(m for m in values if m['metric']=='revenue')['standard_value'],100000)

    def test_no_guessed_monetary_unit_or_wrong_period_column(self):
        table=[['项目','本报告期','本报告期比上年同期增减','年初至报告期末','同比'],['营业收入','10','999','30','888']]
        self.assertEqual(table_metrics([(1,[table])],'2026Q3'),[])
        result=table_metrics([(1,[table])],'2026Q3',{1:'yuan'})
        self.assertEqual([m['value'] for m in result],[10,30])

    def test_annual_year_header_never_uses_previous_year_value(self):
        table=[['项目','2025年','2024年','本年比上年增减'],['营业收入（元）','30','20','50%']]
        values=table_metrics([(8,[table])],'2025FY')
        self.assertEqual([(m['scope'],m['value']) for m in values],[('cumulative',30)])
        self.assertEqual(values[0]['yoy_pct'],50)

    def test_repair_keeps_original_model_evidence_and_never_mutates_input(self):
        original={'metrics':[dict(metric='revenue',scope='cumulative',value=30,unit='yuan',verified=False,quote='营业收入 30')]}
        before=copy.deepcopy(original)
        proved=dict(metric='revenue',scope='cumulative',value=30,unit='yuan',standard_value=30,verified=True,verification_method='pdf_table_column')
        with patch('stock_research.financial_evidence.primary_metrics',return_value={'metrics':[proved],'status':'checked'}):
            result=enrich_extraction(None,{},original)
        self.assertEqual(original,before)
        self.assertTrue(result['metrics'][0]['verified'])
        self.assertEqual(result['metrics'][0]['original_model_quote'],'营业收入 30')
        self.assertEqual(result['metrics'][0]['repair_kind'],'verification_repair')

    def test_historical_price_snapshot_and_pe_bases_are_separate(self):
        quote={'price':10,'market_cap':1000,'pb':2,'asof':'2026-10-16','pe_ttm':12}
        extraction={'metrics':[{'metric':'net_profit','scope':'cumulative','standard_value':60,'period_end':'2026-09-30','verified':True}]}
        history=[{'period_end':'2025-12-31','net_profit':100,'disclosed_at':'2026-04-01'}, {'period_end':'2026-12-31','net_profit':200,'disclosed_at':'2027-04-01'}]
        result=build_market_snapshot(quote,extraction,history,'2026-10-17')
        self.assertEqual(result['price'],10)
        self.assertEqual(result['asof'],'2026-10-16')
        self.assertEqual(result['pe_static'],10)
        self.assertEqual(result['pe_dynamic'],12.5)
        self.assertEqual(result['pe_ttm'],12)
        self.assertNotIn('pe_dynamic',quote)


if __name__=='__main__':unittest.main()

class RepairedSnapshotTests(unittest.TestCase):
    def test_repaired_financials_refresh_without_changing_historical_quote(self):
        from stock_research.market_metrics import refresh_market_financials
        existing={'price':10,'market_cap':1000,'asof':'2026-10-16','pe_ttm':12,'pb':2,'eps':None,'gross_margin':None,'pe_dynamic':99,'total_shares':100,'financial_source':'https://example.org/history'}
        extraction={'metrics':[dict(metric='net_profit',scope='cumulative',standard_value=60,period_end='2026-09-30',verified=True),dict(metric='eps',scope='cumulative',standard_value=.6,period_end='2026-09-30',verified=True),dict(metric='gross_margin',scope='cumulative',standard_value=30,period_end='2026-09-30',verified=True)],'data_quality':{'checked_at':'2026-10-18'}}
        result=refresh_market_financials(existing,{'price':999,'asof':'2026-10-18'},extraction,known_at='2026-10-17')
        for key in ('price','market_cap','asof','pe_ttm','pb','total_shares','financial_source'):
            self.assertEqual(result[key],existing[key])
        self.assertEqual(result['eps'],.6)
        self.assertEqual(result['gross_margin'],30)
        self.assertEqual(result['pe_dynamic'],12.5)
        self.assertEqual(result['financial_checked_at'],'2026-10-18')
        self.assertIsNone(existing['eps'])
        self.assertEqual(existing['pe_dynamic'],99)
