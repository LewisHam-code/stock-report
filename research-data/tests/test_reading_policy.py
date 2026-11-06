import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from stock_research import analysis, core, sources, foundation_job, foundation_budget, vault_archive
from stock_research.report_content import VERSION


class ReadingPolicyTests(unittest.TestCase):
    def test_corrected_report_context_reaches_model_boundary_without_real_requests(self):
        cfg={**core.config(),'reuse_verified_pdf_metrics':False,'compact_financial_tables':False,
             '_benchmark_fresh_extraction':True}
        context={'effective_version':'更正后','changes':['修正披露错误']}
        doc={'id':'fixture-corrected','sha256':'fixture','company':'示例公司','code':'600001','market':'SH',
             'period':'2026Q3','published_at':'2026-10-20','correction_context':context}
        with patch('stock_research.securities.price_enabled',return_value=True), \
             patch.object(analysis,'excerpt',return_value=('公开财报文本',{'full_text':True})), \
             patch('stock_research.financial_evidence.primary_metrics',return_value={}), \
             patch('stock_research.financial_facts.reconcile',return_value={}), \
             patch('stock_research.financial_facts.require_ready'), \
             patch.object(analysis,'completion',side_effect=core.ServiceError('fixture_boundary','本地测试停止')) as model:
            with self.assertRaises(core.ServiceError) as error:
                analysis.analyze(Mock(),doc,{},settings=cfg)
        self.assertEqual(error.exception.code,'fixture_boundary')
        model.assert_called_once()
        prompt=model.call_args.args[1]
        import json
        self.assertEqual(json.loads(prompt.split('\n',1)[0].split('：',1)[1]),context)
        self.assertIn('公开财报文本',prompt)

    def test_production_cannot_downgrade_to_partial_coarse_reading(self):
        cfg={**core.config(),'reading_mode':'initial'}
        with patch.object(analysis,'excerpt',return_value=('partial',{'full_text':False})),patch.object(analysis,'completion') as model:
            with self.assertRaises(core.ServiceError) as error:
                analysis.analyze(None,{}, {},settings=cfg)
        self.assertEqual(error.exception.code,'deep_reading_requires_partition')
        self.assertEqual(cfg['reading_mode'],'initial')
        model.assert_not_called()

    def test_legacy_coarse_roles_are_merged_without_enabling_disabled_models(self):
        cfg={**core.config(),'model_profiles':[{'id':'fixture','provider':'deepseek','model':'deepseek-flash',
             'enabled':False,'stages':['extraction','analysis','deep_analysis']}]}
        result=core.model_profiles(cfg)[0]
        self.assertEqual(result['stages'],['extraction','deep_analysis'])
        self.assertFalse(result['enabled'])
    def test_only_reviewed_admin_omissions_allow_substantive_complete_coverage(self):
        coverage={'full_text':False,'all_source_pages_selected':True,'filter_version':VERSION,
                  'omissions':[{'reason':'目录及备查文件目录','page':4}]}
        self.assertTrue(sources.substantive_coverage_complete(coverage))
        for change in ({'all_source_pages_selected':False},{'filter_version':'unknown'},
                       {'omissions':[{'reason':'业务介绍','page':9}]},{'omissions':[]}):
            self.assertFalse(sources.substantive_coverage_complete({**coverage,**change}))
        self.assertTrue(sources.substantive_coverage_complete({'full_text':True}))

    def test_partial_deep_reading_stops_before_paying_a_model(self):
        cfg={**core.config(),'reading_mode':'deep','max_document_chars':10}
        with patch.object(analysis,'excerpt',return_value=('a partial page',{'full_text':False})),patch.object(analysis,'completion') as model:
            with self.assertRaises(core.ServiceError) as error:
                analysis.analyze(None,{}, {},settings=cfg)
        self.assertEqual(error.exception.code,'deep_reading_requires_partition')
        model.assert_not_called()

    def test_foundation_inherits_reading_policy_without_changing_its_model_or_budget(self):
        cfg={**core.config(),'reading_mode':'deep','max_document_chars':600000,'max_output_tokens':16384,
             'model_profiles':[{'id':'fixture','provider':'deepseek','model':'deepseek-flash','enabled':False,'stages':['extraction','analysis']}]}
        before=copy.deepcopy(cfg)
        with patch.object(core,'config',return_value=cfg),patch.object(foundation_budget,'load',return_value={'id':'fixture','phase':'focused'}):
            selected=foundation_job.settings()
        self.assertEqual(selected['reading_mode'],'deep')
        self.assertEqual(selected['model'],'deepseek-flash')
        self.assertTrue(selected['single_model'])
        self.assertEqual(selected['foundation_task'],'fixture')
        self.assertEqual(cfg,before)

    def test_reading_policy_is_recoverable_in_vault_without_credentials(self):
        cfg={'reading_mode':'deep','omit_administrative_content':True,'reuse_verified_pdf_metrics':False,
             'initial_report_thinking':'low','max_document_chars':600000,'api_key':'fixture-private'}
        saved=vault_archive.safe_config(cfg)
        self.assertNotIn('api_key',saved)
        for key in cfg.keys()-{'api_key'}:
            self.assertEqual(saved[key],cfg[key])


if __name__=='__main__':unittest.main()
