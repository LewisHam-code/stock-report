import io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from stock_research import core,annual_costs as fees,models


class AnnualCostTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  p=patch.object(core,'VAULT',Path(self.tmp.name));p.start();self.addCleanup(p.stop)
  self.policy={'hard_limits':{'deepseek-flash':1,'deepseek-v4-pro':5},'increase_ratio':1.1,'consecutive_reports':3,
               'min_samples':5,'neighbor_count':10,'sample_window':20}
  rows=[{'document_id':'seed'+str(i),'model':'deepseek-flash','stage':'narrative','reading_mode':'deep','period_kind':'FY',
         'source_pages':50,'input_tokens':1000,'output_tokens':1000} for i in range(6)]
  rows +=[{**r,'document_id':r['document_id']+'large','source_pages':500,'input_tokens':10000,'output_tokens':5000} for r in rows]
  core.atomic_json(fees.root()/'年报规模平均基线.json',{'rows':rows})
 def cfg(self,doc='doc'):
  return {'model':'deepseek-flash','one_off_annual_task':'annual-test','reading_mode':'deep','_annual_cost_policy':self.policy,
          'financial_reading_cost_policy':{'mode':'relative_change'},
          '_cost_run_id':doc,'_financial_cost_context':{'id':doc,'page_count':50,'source_text_chars':10000},'_extraction_cache_hit':True}
 def complete(self,doc,inputs=1000,outputs=1000,pages=50):
  cfg=self.cfg(doc);cfg['_financial_cost_context']['page_count']=pages
  body={'model':cfg['model'],'messages':[{'role':'user','content':'公开财报'}],'max_tokens':8000}
  f=fees.before_request(cfg,'deep_report',body)
  fees.record_usage(cfg,'deep_report',body,{'prompt_tokens':inputs,'completion_tokens':outputs},f)
  fees.mark_valid(f);fees.finish_report(cfg,{'id':'report-'+doc,'document_id':doc})
  return fees.status(cfg['one_off_annual_task'])
 def test_normal_peak_tariff_is_billed_but_not_an_abnormal_increase(self):
  for doc in ('peak-one','peak-two','peak-three'):
   with patch('stock_research.model_timing.price_multiplier',return_value=2):state=self.complete(doc)
  row=state['streams']['deepseek-flash:deep']['recent_reports'][-1]
  self.assertAlmostEqual(row['actual_cost_cny'],row['same_rate_cost_cny']*2)
  self.assertFalse(row['anomaly']);self.assertIsNone(state['hold'])
 def test_peak_request_upper_bound_is_reserved_at_peak_tariff(self):
  cfg=self.cfg();cfg['_annual_cost_policy']['hard_limits']['deepseek-flash']=.05
  with patch('stock_research.model_timing.price_multiplier',return_value=2):
   with self.assertRaises(core.ServiceError):fees.before_request(cfg,'deep_report',{'max_tokens':8000,'messages':[]})
  self.assertGreater(fees.status('annual-test')['hold']['next_request_upper_cny'],.05)
 def test_larger_annual_cost_is_normal_when_input_scale_matches(self):
  small=self.complete('small');large=self.complete('large',10000,5000,500)
  items=large['streams']['deepseek-flash:deep']['recent_reports']
  self.assertGreater(items[1]['actual_cost_cny'],items[0]['actual_cost_cny'])
  self.assertFalse(items[1]['anomaly']);self.assertIsNone(large['hold'])
 def test_only_third_consecutive_abnormal_report_pauses(self):
  self.assertIsNone(self.complete('one',outputs=4000)['hold'])
  self.assertIsNone(self.complete('two',outputs=4000)['hold'])
  state=self.complete('three',outputs=4000)
  self.assertEqual(state['hold']['reason'],'three_consecutive_anomalies')
  self.assertEqual(state['hold']['consecutive_anomalies'],3)
 def test_price_drop_resets_streak_without_pause(self):
  self.complete('high',outputs=4000);state=self.complete('low',outputs=500)
  self.assertEqual(state['streams']['deepseek-flash:deep']['consecutive_anomalies'],0)
  self.assertIsNone(self.complete('high2',outputs=4000)['hold'])
 def test_one_document_cannot_count_as_three_reports(self):
  for _ in range(3):state=self.complete('same',outputs=4000)
  self.assertIsNone(state['hold'])
  self.assertLessEqual(state['streams']['deepseek-flash:deep']['consecutive_anomalies'],1)
 def test_hard_cap_blocks_before_any_model_transport(self):
  cfg=self.cfg();cfg['_annual_cost_policy']['hard_limits']['deepseek-flash']=.001
  with self.assertRaises(core.ServiceError):fees.before_request(cfg,'deep_report',{'max_tokens':8000,'messages':[]})
  self.assertEqual(fees.status('annual-test')['hold']['reason'],'hard_limit')
 def test_unknown_usage_pauses_immediately(self):
  cfg=self.cfg();body={'max_tokens':1000,'messages':[]};f=fees.before_request(cfg,'deep_report',body)
  fees.transport_finished(cfg,'deep_report',body,{},f)
  self.assertEqual(fees.status('annual-test')['hold']['reason'],'usage_unknown')
 def test_pro_cap_includes_flash_spending_on_same_document(self):
  cfg=self.cfg();base=fees.home('annual-test')
  core.atomic_json(fees.ledger_path(base,'doc','deepseek-flash'),{'calls':{'old':{'actual_cost_cny':.9}}})
  cfg['model']='deepseek-v4-pro';cfg['_annual_cost_policy']['hard_limits']['deepseek-v4-pro']=1
  with self.assertRaises(core.ServiceError):fees.before_request(cfg,'deep_report',{'max_tokens':16000,'messages':[]})
  self.assertEqual(fees.status('annual-test')['hold']['already_paid_cny'],.9)
 def test_third_anomaly_blocks_next_report_admission(self):
  for doc in ('one','two','three'):self.complete(doc,outputs=4000)
  with self.assertRaises(core.ServiceError) as caught:fees.admit(self.cfg('four'))
  self.assertEqual(caught.exception.code,'annual_cost_hold')
 def test_missing_samples_accumulate_safely_under_hard_cap(self):
  core.atomic_json(fees.root()/'年报规模平均基线.json',{'rows':[]})
  state=self.complete('warmup',outputs=4000)
  row=state['streams']['deepseek-flash:deep']['recent_reports'][0]
  self.assertFalse(row['baseline_ready']);self.assertFalse(row['anomaly']);self.assertIsNone(state['hold'])
 def test_seed_rejects_coarse_or_partial_reports(self):
  row={'id':'r','document_id':'d','period':'2025FY','model':'deepseek-flash','financial_analysis':{'coverage':{'full_text':True,'total_pages':50}},
       'model_steps':{'extraction':{'model':'deepseek-flash','usage':{'prompt_tokens':1000,'completion_tokens':100}}},
       '_generation_evidence':{'requests':{'extraction':{'messages':[{'role':'system','content':'本轮为全文精读'}]}}}}
  self.assertEqual(len(fees.seed_from_reports([row])['rows']),1)
  row['_generation_evidence']['requests']['extraction']['messages'][0]['content']='重点页选读'
  self.assertEqual(fees.seed_from_reports([row])['rows'],[])
 def test_live_transport_gate_checks_hard_cap_before_reserving_or_sending(self):
  cfg={**core.config(),**self.cfg(),'provider':'deepseek','prefer_off_peak':False,'single_model':True}
  cfg['_annual_cost_policy']['hard_limits']['deepseek-flash']=.001
  with patch.object(models,'api_key',return_value='test'),patch('stock_research.annual_batch.admit'), \
       patch('stock_research.foundation_budget.reserve') as reserve,patch.object(models,'model_open') as network:
   with self.assertRaises(core.ServiceError) as caught:models._completion_once('','公开年报',purpose='deep_report',settings=cfg)
  self.assertEqual(caught.exception.code,'annual_cost_hold');reserve.assert_not_called();network.assert_not_called()
 def test_valid_model_response_is_recorded_once_and_eligible_for_average(self):
  cfg={**core.config(),**self.cfg('api-success'),'provider':'deepseek','prefer_off_peak':False,'single_model':True}
  response={'model':'deepseek-flash','usage':{'prompt_tokens':1000,'completion_tokens':1000},
            'choices':[{'finish_reason':'stop','message':{'content':'OK'}}]}
  with patch.object(models,'api_key',return_value='test'),patch('stock_research.annual_batch.admit'), \
       patch('stock_research.foundation_budget.reserve',return_value=None),patch.object(models,'model_open',return_value=io.StringIO(json.dumps(response))):
   text,_=models._completion_once('','公开年报',purpose='deep_report',settings=cfg)
  self.assertEqual(text,'OK')
  fees.finish_report(cfg,{'id':'report-api','document_id':'api-success'})
  state=fees.status('annual-test');self.assertIsNone(state['hold'])
  path=fees.ledger_path(fees.home('annual-test'),'api-success','deepseek-flash')
  ledger=json.loads(path.read_text());self.assertEqual(len(ledger['calls']),1)
  self.assertTrue(next(iter(ledger['calls'].values()))['valid_response'])
