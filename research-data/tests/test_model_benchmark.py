import io
import copy
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from stock_research import core,models
from stock_research.model_benchmark import charge


class ModelBenchmarkTests(unittest.TestCase):
    def test_cache_and_output_costs_use_the_selected_models_actual_rates(self):
        usage={'prompt_tokens':1000,'prompt_cache_hit_tokens':200,'completion_tokens':100,
               'completion_tokens_details':{'reasoning_tokens':50}}
        self.assertAlmostEqual(charge('deepseek-flash',usage),.001204)
        self.assertAlmostEqual(charge('deepseek-v4-pro',usage),.00498)
        self.assertAlmostEqual(charge('deepseek-v4-pro',usage,2),.00996)
        # Reasoning is already part of completion_tokens; never charge it twice.
        self.assertEqual(charge('deepseek-flash',usage),charge('deepseek-flash',{k:v for k,v in usage.items() if k!='completion_tokens_details'}))

    def test_missing_usage_is_never_claimed_to_cost_zero(self):
        with self.assertRaises(core.ServiceError):charge('deepseek-flash',{})

    def test_usage_is_observed_before_a_paid_response_fails_schema_validation(self):
        usage={'prompt_tokens':100,'completion_tokens':20,'prompt_cache_hit_tokens':0}
        response={'model':'deepseek-flash','usage':usage,'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
        observer=Mock();reply=Mock();cfg={**core.config(),'provider':'deepseek','model':'deepseek-flash','prefer_off_peak':False,'_model_usage_observer':observer,'_model_response_observer':reply}
        with tempfile.TemporaryDirectory() as directory:
            store=core.Store(Path(directory)/'test.sqlite');self.addCleanup(store.conn.close)
            with patch.object(models,'api_key',return_value='fixture-only'),patch.object(models,'model_open',return_value=io.StringIO(json.dumps(response))):
                with self.assertRaises(core.ServiceError):models._completion_once('','',schema={'type':'object','required':['missing']},store=store,settings=cfg)
        observer.assert_called_once_with('analysis',usage,'deepseek-flash')
        reply.assert_called_once_with('analysis',response)

    def test_authorized_benchmark_calls_are_separate_from_normal_daily_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            store=core.Store(Path(directory)/'test.sqlite');self.addCleanup(store.conn.close)
            store.reserve_api_call('benchmark:fixture:financial_extraction',10**9)
            self.assertEqual(store.api_calls_today(),0)
            store.reserve_api_call('comprehensive_report',1)
            self.assertEqual(store.api_calls_today(),1)

    def test_fresh_comparison_does_not_reuse_another_models_extraction(self):
        from stock_research import analysis
        cfg={**core.config(),'_benchmark_fresh_extraction':True}
        doc={'id':'fixture','sha256':'fixture','company':'fixture','code':'600000','market':'SH','period':'2025FY','published_at':core.stamp(),'source_url':'https://static.cninfo.com.cn/fixture.pdf'}
        extracted={'industry':'fixture','metrics':[],'sections':[],'risks':[],'coverage':{'full_text':True}}
        narrative={k:'' for k in ('conclusion','evidence','valuation','risks','valuation_context')}
        narrative.update(industry_outlook='unknown',industry_quotes=[],topics=[],catalysts=[],falsification=[],scenarios=[])
        used=[]
        def complete(*args,**kwargs):
            purpose=args[4];model=kwargs['settings']['model'];used.append((purpose,model))
            return copy.deepcopy(extracted if purpose=='financial_extraction' else narrative),{'provider':'deepseek','model':model,'test_mode':False}
        with tempfile.TemporaryDirectory() as directory:
            store=core.Store(Path(directory)/'test.sqlite');self.addCleanup(store.conn.close)
            key='extraction-v4-'+core.digest(['fixture',cfg['strategy_version'],cfg['max_document_chars'],core.digest('same text')])
            store.cache(key,{'data':{'industry':'wrong cached model'},'model':{}},hours=1)
            with patch.object(analysis,'excerpt',return_value=('same text',{'full_text':True})),patch.object(analysis,'verify_extraction',side_effect=lambda d,v,c:v),patch('stock_research.financial_evidence.enrich_extraction',side_effect=lambda s,d,v,**kw:v),patch.object(analysis,'completion',side_effect=complete),patch('stock_research.financial_facts.require_ready'):
                for model in ('deepseek-flash','deepseek-v4-pro'):
                    result=analysis.analyze(store,doc,{'_fixed_industry_documents':{'documents':[],'errors':[]}},settings={**cfg,'model':model})
                    self.assertFalse(result['input_optimization']['extraction_cache_hit'])
        self.assertEqual([m for p,m in used if p=='financial_extraction'],['deepseek-flash','deepseek-v4-pro'])
