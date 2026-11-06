import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from stock_research import core,report_cost_guard as guard,models

class CostGuardTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.patch=patch.object(core,'VAULT',Path(self.temp.name));self.patch.start();self.addCleanup(self.patch.stop)
  self.old={'model':'deepseek-flash','messages':[{'role':'user','content':'原文'*1000}],'max_tokens':1000}
  self.row={'report_id':'old','document_id':'doc','period':'2025FY','stage':'extraction','model':'deepseek-flash',
            'input_bytes':guard.input_size(self.old),'input_tokens':1000,'output_tokens':100,'cache_hit_tokens':0,
            'output_cap':1000,'body_fingerprint':core.digest(self.old)}
  self.baseline={'baseline_id':'pinned','rows':[self.row]}
  core.atomic_json(guard.folder()/'基线.json',self.baseline)
  self.cfg={'model':'deepseek-flash','financial_reading_cost_policy':{'mode':'relative_change'},'_financial_cost_context':{'id':'doc','period':'2025FY'}}
 def test_decreases_do_not_require_permission(self):
  body={**self.old,'messages':[{'role':'user','content':'短文'}]}
  result=guard.require_approved(self.cfg,'financial_extraction',body)
  self.assertLess(result['increase_ratio'],1)
  self.assertFalse((guard.folder()/'待确认.json').exists())
 def test_more_than_ten_percent_stops_before_transport_and_budget_reservation(self):
  cfg={**core.config(),**self.cfg,'provider':'deepseek','single_model':True,'prefer_off_peak':False}
  with patch.object(models,'api_key',return_value='test'),patch.object(models,'model_open') as network,patch('stock_research.foundation_budget.reserve') as reserve:
   with self.assertRaises(core.ServiceError) as e:models._completion_once('', '原文'*10000,store=None,purpose='financial_extraction',settings=cfg)
  self.assertEqual(e.exception.code,'report_cost_approval_required');network.assert_not_called();reserve.assert_not_called()
 def test_stage_increase_is_compared_with_whole_report_price(self):
  self.baseline['rows'].append({**self.row,'stage':'narrative','input_tokens':10000})
  bigger={**self.old,'messages':[{'role':'user','content':'原文'*1100}]}
  result=guard.estimate(self.cfg,'financial_extraction',bigger,self.baseline)
  self.assertLessEqual(result['increase_ratio'],1.1)
 def test_actual_overrun_holds_future_requests_without_discarding_returned_result(self):
  forecast=guard.require_approved(self.cfg,'financial_extraction',self.old)
  guard.record_usage(self.cfg,'financial_extraction',self.old,{'prompt_tokens':1000,'completion_tokens':1000},forecast)
  with self.assertRaises(core.ServiceError):guard.require_approved(self.cfg,'financial_extraction',self.old)
 def test_missing_baseline_and_unknown_model_do_not_silently_bypass_guard(self):
  with self.assertRaises(core.ServiceError):guard.estimate({**self.cfg,'model':'unknown'},'financial_extraction',self.old,self.baseline)
 def test_unknown_actual_usage_holds_instead_of_assuming_zero_cost(self):
  forecast=guard.require_approved(self.cfg,'financial_extraction',self.old)
  guard.record_usage(self.cfg,'financial_extraction',self.old,{},forecast)
  with self.assertRaises(core.ServiceError):guard.require_approved(self.cfg,'financial_extraction',self.old)
 def test_higher_output_limit_alone_does_not_block_a_predicted_decrease(self):
  body={**self.old,'messages':[{'role':'user','content':'短文'}],'max_tokens':2000}
  result=guard.require_approved(self.cfg,'financial_extraction',body)
  self.assertTrue(result['output_cap_increased']);self.assertLess(result['increase_ratio'],1)
  self.assertFalse((guard.folder()/'待确认.json').exists())
 def test_cap_only_hold_from_an_old_worker_is_archived_and_rechecked(self):
  hold={'state':'requires_approval','output_cap_increased':True,'old_report_cost_cny':1,
        'estimated_report_cost_cny':.25,'increase_ratio':.25}
  core.atomic_json(guard.folder()/'待确认.json',hold)
  result=guard.require_approved(self.cfg,'financial_extraction',self.old)
  self.assertLessEqual(result['increase_ratio'],1.1)
  self.assertFalse((guard.folder()/'待确认.json').exists())
  archived=[p for p in (guard.folder()/'保护记录').glob('*.json') if not p.name.endswith('.resolution.json')]
  self.assertEqual(len(archived),1);self.assertEqual(json.loads(archived[0].read_text()),hold)
 def test_real_unknown_inconsistent_and_current_policy_holds_are_never_auto_released(self):
  legacy={'state':'requires_approval','output_cap_increased':True,'old_report_cost_cny':1,
          'estimated_report_cost_cny':.25,'increase_ratio':.25}
  for change in ({'estimated_report_cost_cny':1.2,'increase_ratio':1.2},
                 {'state':'actual_increase_requires_approval'}, {'state':'usage_unknown_requires_review'},
                 {'estimated_report_cost_cny':1.5}, {'policy_version':guard.POLICY_VERSION}):
   with self.subTest(change=change):
    core.atomic_json(guard.folder()/'待确认.json',{**legacy,**change})
    with self.assertRaises(core.ServiceError):guard.require_approved(self.cfg,'financial_extraction',self.old)
    self.assertTrue((guard.folder()/'待确认.json').exists())
 def test_longer_document_is_not_a_method_price_increase_when_both_inputs_match(self):
  longer={**self.old,'messages':[{'role':'user','content':'原文'*6000}]}
  cfg={**self.cfg,'_cost_prior_body':longer}
  result=guard.require_approved(cfg,'financial_extraction',longer)
  self.assertEqual(result['comparison_basis'],'same_document_prior_input')
  self.assertEqual(result['increase_ratio'],1)
  self.assertGreater(result['old_cost_cny'],guard.cost('deepseek-flash',1000,100))
 def test_true_same_document_input_increase_still_blocks(self):
  cfg={**self.cfg,'_cost_prior_body':self.old}
  longer={**self.old,'messages':[{'role':'user','content':'原文'*6000}]}
  with self.assertRaises(core.ServiceError):guard.require_approved(cfg,'financial_extraction',longer)
 def test_old_cross_document_prediction_can_be_recomputed_but_actual_hold_cannot(self):
  cfg={**self.cfg,'_cost_prior_body':self.old}
  hold={'state':'requires_approval','policy_version':2,'increase_ratio':1.2,'document_id':'doc','model':'deepseek-flash',
        'stage':'extraction','body_fingerprint':core.digest(self.old),'baseline_id':'pinned'}
  core.atomic_json(guard.folder()/'待确认.json',hold)
  self.assertEqual(guard.require_approved(cfg,'financial_extraction',self.old)['increase_ratio'],1)
  core.atomic_json(guard.folder()/'待确认.json',{**hold,'state':'actual_increase_requires_approval'})
  with self.assertRaises(core.ServiceError):guard.require_approved(cfg,'financial_extraction',self.old)
 def test_cached_extraction_does_not_get_charged_as_an_unperformed_stage_in_actual_total(self):
  self.baseline['rows'].append({**self.row,'stage':'narrative'})
  core.atomic_json(guard.folder()/'基线.json',self.baseline)
  cfg={**self.cfg,'_extraction_cache_hit':True}
  forecast=guard.require_approved(cfg,'deep_report',self.old)
  guard.record_usage(cfg,'deep_report',self.old,{'prompt_tokens':1000,'completion_tokens':300},forecast)
  self.assertFalse((guard.folder()/'待确认.json').exists())
