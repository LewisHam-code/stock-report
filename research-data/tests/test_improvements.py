import copy
import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from stock_research import company_events, research_quality, tracking, valuation_inputs, pipeline
from stock_research.core import Store, ServiceError


class ImprovementTests(unittest.TestCase):
    def test_benchmark_is_shared_and_failure_cools_down_without_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'test.sqlite');self.addCleanup(store.conn.close)
            prices={'bars':[{'date':str(index),'open':100,'close':101} for index in range(61)]}
            with patch.object(pipeline,'daily_bars',return_value=prices) as fetch:
                self.assertEqual(pipeline.research_benchmark(store),prices)
                self.assertEqual(pipeline.research_benchmark(store),prices)
                self.assertEqual(fetch.call_count,1)
            self.assertEqual(store.rows('SELECT COUNT(*) n FROM api_calls')[0]['n'],0)
            other=Store(Path(directory)/'other.sqlite');self.addCleanup(other.conn.close)
            with patch.object(pipeline,'daily_bars',side_effect=ServiceError('source_network','offline')) as fetch:
                pipeline.research_benchmark(other);pipeline.research_benchmark(other)
                self.assertEqual(fetch.call_count,1)

    def test_ttm_uses_verified_originals_and_never_substitutes_missing_periods(self):
        documents=[{'id':period,'code':'600001','period':period,'published_at':at,'sha256':period,'source_url':'https://example.com/'+period}
                   for period,at in [('2026Q3','2026-10-18'),('2025FY','2026-04-20'),('2025Q3','2025-10-20')]]
        class Store:
            def rows(self,*args):return documents
        values={'2026Q3':(150,60),'2025FY':(100,40),'2025Q3':(80,30)}
        def proof(store,doc):
            return {'status':'checked','metrics':[
                {'metric':name,'standard_value':value,'scope':'cumulative','verified':True,'verification_method':'pdf_statement_column','source_page':3}
                for name,value in zip(('operating_cashflow','capex'),values[doc['period']])]}
        with patch('stock_research.financial_evidence.primary_metrics',side_effect=proof):
            data=valuation_inputs.verified_cashflow(Store(),'600001','2026-10-19')
            self.assertEqual(data['verified_ttm_cashflow'],170)
            self.assertEqual(data['verified_ttm_capex'],70)
            documents.pop()
            gap=valuation_inputs.verified_cashflow(Store(),'600001','2026-10-19')
            self.assertNotIn('verified_ttm_cashflow',gap)
            self.assertEqual(gap['ttm_cashflow_evidence']['missing_periods'],['2025Q3'])

    def test_quality_uses_latest_version_and_does_not_change_scores(self):
        report = {'id':'r1','document_id':'d1','generated_at':'2026-10-10','code':'600001','company':'示例',
                  'scoring':{'score':30,'status':'provisional','coverage_pct':70,'items':[
                      {'name':'同行盈利收益率比较','group':'估值','weight':10,'available':False,'missing_reason':'同业样本不足'},
                      {'name':'年度ROE','group':'质量','weight':8,'available':True}]}}
        newer = {**report,'id':'r2','generated_at':'2026-10-11'}
        before=copy.deepcopy([report,newer])
        payload=research_quality.build([report,newer])
        self.assertEqual(payload['report_count'],1)
        self.assertEqual(payload['factors'][0]['reports'],1)
        self.assertEqual(payload['reports'][0]['report_id'],'r2')
        self.assertEqual([report,newer],before)

    def test_ttm_discovered_cells_require_physical_contract_and_fact_identity(self):
        from stock_research.financial_facts import CONTRACT
        documents=[{'id':'annual','code':'600001','period':'2025FY','published_at':'2026-04-20','sha256':'a'*64,'source_url':'https://example.org/annual'}]
        class SampleStore:
            def rows(self,*args):return documents
        anchors={k:{'page':1} for k in ('value','label','period','unit','entity_evidence')};anchors['source_sha256']='a'*64
        proof={'status':'checked','contract':CONTRACT,'metrics':[{'metric':name,'scope':'cumulative','standard_value':v,'verified':True,'verification_method':'pdf_cell_candidate','source_page':1,'fact_id':name,'evidence':anchors} for name,v in [('operating_cashflow',100),('capex',20)]]}
        with patch('stock_research.financial_evidence.primary_metrics',return_value=proof):
            self.assertEqual(valuation_inputs.verified_cashflow(SampleStore(),'600001','2026-10-21')['verified_ttm_capex'],20)
            proof['metrics'][1].pop('fact_id')
            self.assertNotIn('verified_ttm_capex',valuation_inputs.verified_cashflow(SampleStore(),'600001','2026-10-21'))

    def test_event_dedup_and_uncertain_date_and_verification_are_preserved(self):
        event={'code':'600001','provider':'ths','category':'news','title':'测试新闻','source_url':'https://stock.10jqka.com.cn/a',
               'published_at':None,'verification_status':'source_metadata_only','captured_at':'2026-10-19'}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with ThreadPoolExecutor(max_workers=4) as pool:
                counts=list(pool.map(lambda _:company_events.append_events('600001','SH',[event],root),range(4)))
            self.assertEqual(sum(counts),1)
            path=root/'30_公司/SH/600001/事件/事件记录.jsonl'
            saved=[json.loads(line) for line in path.read_text().splitlines()]
            self.assertIsNone(saved[0]['published_at'])
            self.assertIsNone(saved[0]['effective_at'])
            self.assertEqual(saved[0]['verification_status'],'source_metadata_only')
            revised={**event,'title':'更正后的新闻','captured_at':'2026-10-20'}
            self.assertEqual(company_events.append_events('600001','SH',[revised],root),1)
            self.assertEqual(len(path.read_text().splitlines()),2)

    def test_saved_outcome_does_not_override_original_provisional_or_test_baseline(self):
        report={'id':'r1','code':'600001'}
        outcome={'state':'evaluated','evaluable':True,'aligned':True,'relative_aligned':True,'excess_pct':10,'benchmark_gap':False}
        original=copy.deepcopy(outcome)
        for flags in ({'score_status':'provisional'},{'score_status':'calculated','test_mode':True}):
            base={'report_id':'r1','code':'600001','company':'示例','generated_at':'2026-10-10',
                  'rating':'buy','score':80,'provenance':'on_time',**flags}
            with patch('stock_research.securities.lookup',return_value=None):
                data=tracking.build_tracking([report],{}, {},{'r1':base},'2026-10-19',outcomes={('r1',3):outcome})
            self.assertEqual(data['summary']['3']['evaluable'],0)
            self.assertIsNone(data['summary']['3']['alignment_pct'])
            self.assertEqual(data['summary']['3']['exploratory']['mature'],1)
        self.assertEqual(outcome,original)

    def test_grouping_uses_frozen_industry_and_version(self):
        report={'id':'r1','code':'600001','industry':'后来的行业'}
        base={'report_id':'r1','code':'600001','company':'示例','generated_at':'2026-10-10',
              'rating':'buy','score':80,'score_status':'calculated','provenance':'on_time',
              'industry':'原始行业','provider':'provider','model':'model','scoring_version':'original-v1'}
        result={'state':'evaluated','evaluable':True,'aligned':True,'relative_aligned':True,'excess_pct':10,'benchmark_gap':False}
        with patch('stock_research.securities.lookup',return_value=None):
            data=tracking.build_tracking([report],{}, {},{'r1':base},'2026-10-19',outcomes={('r1',3):result})
        self.assertEqual(data['summary']['3']['by_industry'][0]['group'],'原始行业')
        self.assertEqual(data['summary']['3']['by_version'][0]['group'],'original-v1')


if __name__ == '__main__':
    unittest.main()
