"""Regression cases taken from public, original PDF character positions."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from stock_research.statement_evidence import statement_metrics,period_column
from stock_research.financial_evidence import number,metric_name,table_metrics,enrich_extraction

class OriginalPage:
    def __init__(self,data):self.width=data['width'];self.height=data['height'];self.lines=data['lines'];self.tables=data.get('tables',[])
    def find_tables(self):
        return [SimpleNamespace(bbox=t['bbox'],extract=lambda t=t:copy.deepcopy(t['raw']),rows=[SimpleNamespace(cells=r) for r in t['rows']]) for t in self.tables]
    def crop(self,box):
        return SimpleNamespace(chars=[c for line in self.lines for c in line['chars'] if box[0]<=(c['x0']+c['x1'])/2<box[2] and box[1]<=(c['top']+c['bottom'])/2<box[3]])
    def extract_text_lines(self,return_chars=False):
        return copy.deepcopy(self.lines) if return_chars else [{k:v for k,v in row.items() if k!='chars'} for row in self.lines]

class StatementEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures=json.loads((Path(__file__).parent/'fixtures/statement_line_layouts.json').read_text())

    def test_printed_pdf_cells_replace_a_text_geometry_clipped_last_digit(self):
        from stock_research import statement_evidence as evidence
        header=['项目','2023年12月31日','2022年12月31日']
        raw=[header,['资产总计','2,523,528,860.19','2,199,005,264.86']]
        table=SimpleNamespace(bbox=(0,50,500,200))
        page=SimpleNamespace(height=842,find_tables=lambda:[table])
        fallback={'top':40,'rows':[header,['资产总计','2,523,528,860.1','2,199,005,264.86']],'centers':[200,400]}
        with patch.object(evidence,'page_lines',return_value=[{'top':10,'text':'合并资产负债表'},{'top':20,'text':'单位：元'}]),patch.object(evidence,'line_table',return_value=(fallback,{'header_page':1})),patch.object(evidence,'reconstructed_table',return_value=(raw,[200,400])):
            metrics,issues,_=evidence.statement_metrics([(1,page)],'2023FY')
        assets=next(m for m in metrics if m['metric']=='total_assets')
        self.assertEqual(assets['standard_value'],2523528860.19)
        self.assertEqual(assets['source_layout'],'pdf_cells')
        self.assertEqual(assets['replaced_line_geometry_value'],2523528860.1)

    def test_blank_pdf_cell_is_missing_not_zero_and_quote_accepts_none(self):
        from stock_research import statement_evidence as evidence
        raw=[['项目','2023年12月31日','2022年12月31日'],['资产总计',None,'100.00']]
        page=SimpleNamespace(height=842,find_tables=lambda:[SimpleNamespace(bbox=(0,50,500,200))])
        with patch.object(evidence,'page_lines',return_value=[{'top':10,'text':'合并资产负债表'},{'top':20,'text':'单位：元'}]), \
             patch.object(evidence,'line_table',return_value=(None,None)),patch.object(evidence,'reconstructed_table',return_value=(raw,[200,400])):
            metrics,issues,_=evidence.statement_metrics([(1,page)],'2023FY')
        self.assertFalse(any(m['metric']=='total_assets' for m in metrics))
        issue=next(r for r in issues if r.get('metric')=='total_assets')
        self.assertEqual(issue['source_status'],'blank');self.assertEqual(issue['quote'],'资产总计 |  | 100.00')

    def test_original_columns_wrapped_labels_and_cross_page_header(self):
        for fixture in self.fixtures:
            with self.subTest(code=fixture['code']):
                metrics,issues,_=statement_metrics([(p['number'],OriginalPage(p)) for p in fixture['pages']],fixture['period'])
                values={m['metric']:m for m in metrics}
                for name,expected in fixture['expected'].items():
                    self.assertAlmostEqual(values[name]['standard_value'],expected,places=2)
                    self.assertTrue(values[name]['verified'])
                    self.assertIsNotNone(values[name]['header_source_page'])
                    self.assertIsNotNone(values[name]['unit_source_page'])
                if fixture['code']=='603132':
                    self.assertEqual(values['capex']['header_source_page'],12)
                    self.assertEqual(values['capex']['source_page'],13)
                if fixture['code']=='603466':
                    self.assertNotIn('capex',values)
                    self.assertTrue(any(i['metric']=='capex' and i['source_status']=='blank' for i in issues))
                if fixture['code']=='002416':
                    self.assertAlmostEqual(values['gross_margin']['standard_value'],(44143755503.25-41752731811.47)/44143755503.25*100)
                    self.assertEqual(values['gross_margin']['verification_method'],'derived_from_verified')

    def test_no_zero_filling_and_accounting_negative_formats(self):
        for text,expected in [('0',0),('０．００',0),('（１，２３４．５６）',-1234.56),('−5.4',-5.4),('1,234.50',1234.5),('19.16%',19.16)]:
            self.assertEqual(number(text),expected)
        for text in ['',None,'—','不适用','未披露','1.2.3']:
            self.assertIsNone(number(text))

    def test_printed_periods_reject_previous_year_and_wrong_quarter(self):
        for text,period in [('2026年1—9月','2026Q3'),('2026年半年度','2026H1'),('2025年度','2025FY'),('2026年9月30日','2026Q3')]:
            self.assertTrue(period_column(text,period),(text,period))
        for text,period in [('2025年1—9月','2026Q3'),('2026年6月30日','2026Q3'),('2026年1-6月','2026Q3'),('本期比上年同期增减','2026Q3')]:
            self.assertFalse(period_column(text,period),(text,period))

    def test_roe_label_is_not_adjusted_profit(self):
        self.assertEqual(metric_name('加权平均净资产收益率（依据归属于上市公司股东的净利润计算）'),'roe')
        self.assertIsNone(metric_name('加权平均净资产收益率（依据扣除非经常性损益后的净利润计算）'))
        self.assertEqual(metric_name('归属于上市公司股东的扣除非经常性损益后的净利润'),'adjusted_profit')

    def test_annual_end_header_is_not_glued_to_roe_value(self):
        rows=[['项目','2025年','2024年','本年比上年增减'],['加权平均净资产收益率','19.16%','17.81%','1.35%'],['','2025年末','2024年末','本年末比上年末增减'],['资产总额（元）','100','90','11.11%']]
        values={m['metric']:m for m in table_metrics([(11,[rows])],'2025FY')}
        self.assertEqual(values['roe']['standard_value'],19.16)
        self.assertEqual(values['total_assets']['standard_value'],100)

    def test_continuation_after_diluted_eps_preserves_roe_only(self):
        first=[['项目','本报告期','同比','年初至报告期末','同比'],['基本每股收益','-0.01','-50%','-0.02','-50%']]
        second=[['稀释每股收益','-0.02','-70%','-0.03','-70%'],['加权平均净资产收益率','-0.2','-1','-0.78','-1']]
        values=table_metrics([(1,[first]),(2,[second])],'2026Q3')
        self.assertEqual(next(m['value'] for m in values if m['metric']=='roe' and m['scope']=='cumulative'),-0.78)
        self.assertEqual(next(m['value'] for m in values if m['metric']=='eps' and m['scope']=='cumulative'),-0.02)

    def test_source_conflict_invalidates_previously_verified_model_fact(self):
        original={'metrics':[dict(metric='revenue',scope='cumulative',value=100,standard_value=100,unit='yuan',verified=True)]}
        proof={'metrics':[dict(metric='revenue',scope='cumulative',value=100,standard_value=None,unit='yuan',verified=False,verification_status='conflict')], 'status':'checked','conflicts':[{'metric':'revenue'}]}
        with patch('stock_research.financial_evidence.primary_metrics',return_value=proof):
            result=enrich_extraction(None,{},original)
        self.assertFalse(result['metrics'][0]['verified'])
        self.assertIsNone(result['metrics'][0]['standard_value'])
        self.assertEqual(result['data_quality']['status'],'review')
        self.assertTrue(original['metrics'][0]['verified'])

    def test_source_blank_is_explicit_missing_and_never_zero(self):
        proof={'metrics':[],'status':'checked','statement_issues':[{'metric':'capex','source_status':'blank','source_page':11}]}
        with patch('stock_research.financial_evidence.primary_metrics',return_value=proof):
            result=enrich_extraction(None,{}, {'metrics':[]})
        missing=next(m for m in result['data_quality']['missing'] if m['metric']=='capex')
        self.assertEqual(missing['status'],'source_blank')
        self.assertEqual(missing['source_pages'],[11])
        self.assertFalse(result['metrics'])

    def test_not_applicable_diluted_eps_does_not_corrupt_basic_eps_row(self):
        table=[['项目','本报告期','同比','年初至报告期末','同比'],['基本每股收益（元/股）','0.26','73.33','0.78','44.44'],['稀释每股收益（元/股）','不适用','不适用','不适用','不适用']]
        values=table_metrics([(1,[table])],'2026Q3')
        self.assertEqual([(m['scope'],m['value']) for m in values],[('single_quarter',.26),('cumulative',.78)])

if __name__=='__main__':unittest.main()

class QualityGateTests(unittest.TestCase):
    def test_failed_statement_or_accounting_check_is_a_rating_blocker(self):
        from stock_research.strategy import evaluate
        quality={'parser_status':'pdf_table_check_failed','conflicts':[{'metric':'revenue'}],'failed_checks':[{'name':'资产=负债+所有者权益','passed':False}]}
        result=evaluate({'code':'600000','company':'示例公司','period':'2026Q3'}, {'metrics':[],'data_quality':quality,'coverage':{'full_text':True}}, {}, {})
        for text in ['财报原文对应期间数值存在冲突，尚待复核','财报勾稽校验未通过，尚待复核','财报原表核对尚未成功完成']:
            self.assertIn(text,result['missing'])
        self.assertEqual(result['rating'],'unrated')

class AnnualAuditRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures=json.loads((Path(__file__).parent/'fixtures/annual_audit_layouts.json').read_text())

    def test_complete_amounts_and_footer_years_do_not_shift_statement_period(self):
        for f in self.fixtures:
            with self.subTest(code=f['code']):
                result,_,_=statement_metrics([(p['number'],OriginalPage(p)) for p in f['pages']],f['period'])
                metrics={m['metric']:m for m in result}
                for name,value in f['expected'].items():
                    self.assertTrue(metrics[name]['verified'])
                    self.assertAlmostEqual(metrics[name]['standard_value'],value,places=2)

    def test_cross_page_summary_keeps_header_full_labels_and_units(self):
        from stock_research.financial_evidence import summary_page_tables
        expected={'000027':{'adjusted_profit':1872712552.62,'roe':4.66,'eps':.31},
                  '000017':{'adjusted_profit':40804629.49,'roe':11.30,'eps':.06}}
        for f in self.fixtures:
            continuation=None;tables=[]
            for p in f['summary']:
                rows,continuation=summary_page_tables(OriginalPage(p),f['period'],continuation)
                tables.append((p['number'],rows))
            metrics={m['metric']:m for m in table_metrics(tables,f['period'])}
            for name,value in expected[f['code']].items():
                self.assertEqual(metrics[name]['standard_value'],value)
            # An adjusted profit label completed on page 8 still points to
            # the page containing its actual amount (page 7 for 000017).
            if f['code']=='000017':self.assertEqual(metrics['adjusted_profit']['source_page'],7)

    def test_malformed_thousands_groups_or_multiple_values_are_not_repaired_as_numbers(self):
        for value in ('03,342,043.542,0','17,010,849.299,6','100\n200','1,23','1,234,56'):
            self.assertIsNone(number(value),value)
