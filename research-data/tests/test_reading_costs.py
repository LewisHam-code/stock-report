import io,json,tempfile,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from stock_research import core,reading_costs as fees,annual_costs,report_cost_guard,models,foundation_budget,current_batch


class ReadingCostTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=patch.object(core,'VAULT',Path(self.temp.name));p.start();self.addCleanup(p.stop)
        p=patch('stock_research.model_timing.price_multiplier',return_value=1);p.start();self.addCleanup(p.stop)
    def cfg(self,doc='one'):
        return {**core.config(),'provider':'deepseek','model':'deepseek-v4-pro','single_model':True,'prefer_off_peak':False,
            'financial_reading_cost_policy':{'mode':'per_report_cap','limit_cny':5},'custom_base_url':False,
            '_financial_cost_context':{'id':doc,'company':'示例公司','code':'600000','period':'2026Q3'}}
    def test_five_inclusive_six_skipped_and_next_document_admitted(self):
        cfg=self.cfg()
        with patch.object(report_cost_guard,'cost',return_value=5):first=report_cost_guard.require_approved(cfg,'deep_report',{'max_tokens':1})
        fees.cancel_request(cfg,first)
        with patch.object(report_cost_guard,'cost',return_value=5.01):
            with self.assertRaises(core.ServiceError) as e:report_cost_guard.require_approved(cfg,'deep_report',{'max_tokens':1})
        self.assertEqual(e.exception.code,'report_cost_limit');self.assertEqual(fees.status(cfg)['skipped_count'],1)
        with patch.object(report_cost_guard,'cost',return_value=.1):
            self.assertIsNotNone(report_cost_guard.require_approved(self.cfg('next'),'deep_report',{'max_tokens':1}))
    def test_both_stages_and_model_switches_share_document_total(self):
        cfg=self.cfg();body={'max_tokens':10}
        with patch.object(report_cost_guard,'cost',return_value=3):first=fees.before_request(cfg,'financial_extraction',body)
        with patch.object(report_cost_guard,'cost',return_value=3):fees.record_usage(cfg,'financial_extraction',body,{'prompt_tokens':1,'completion_tokens':1},first)
        with patch.object(report_cost_guard,'cost',return_value=2.01):
            with self.assertRaises(core.ServiceError):fees.before_request({**cfg,'model':'deepseek-flash'},'deep_report',body)
        self.assertAlmostEqual(fees.status(cfg)['skipped'][0]['estimated_total_cny'],5.01)
    def test_fifty_fen_inclusive_and_both_stages_share_the_new_limit(self):
        cfg=self.cfg();cfg['financial_reading_cost_policy']['limit_cny']=.5
        with patch.object(report_cost_guard,'cost',return_value=.2):first=fees.before_request(cfg,'financial_extraction',{'max_tokens':1})
        with patch.object(report_cost_guard,'cost',return_value=.2):fees.record_usage(cfg,'financial_extraction',{}, {'prompt_tokens':1,'completion_tokens':1},first)
        with patch.object(report_cost_guard,'cost',return_value=.3):second=fees.before_request(cfg,'deep_report',{'max_tokens':1})
        fees.cancel_request(cfg,second)
        with patch.object(report_cost_guard,'cost',return_value=.3001):
            with self.assertRaises(core.ServiceError) as caught:fees.before_request(cfg,'deep_report',{'max_tokens':1})
        self.assertEqual(caught.exception.code,'report_cost_limit');self.assertIn('0.50元',str(caught.exception))

    def test_live_policy_blocks_stale_five_yuan_worker_before_transport(self):
        cfg={**self.cfg(),'_live_execution_policy':True}
        live={**cfg,'financial_reading_cost_policy':{'mode':'per_report_cap','limit_cny':.5}}
        with patch.object(models,'config',return_value=live),patch.object(models,'api_key',return_value='fixture'), \
             patch.object(report_cost_guard,'cost',return_value=.51),patch.object(models,'model_open') as transport:
            with self.assertRaises(core.ServiceError) as caught:models._completion_once('i','p',purpose='deep_report',settings=cfg)
        self.assertEqual(caught.exception.code,'report_cost_limit');transport.assert_not_called()

    def test_parallel_requests_cannot_reserve_more_than_document_cap(self):
        def reserve(_):
            try:return fees.before_request(self.cfg(),'financial_extraction',{'max_tokens':1})['kind']
            except core.ServiceError as e:return e.code
        with patch.object(report_cost_guard,'cost',return_value=3),ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reserve,range(2)))
        self.assertEqual(results.count('report_cost_limit'),1)
    def test_old_cost_hold_and_budget_do_not_stop_new_policy_but_ledgers_remain(self):
        cfg={**self.cfg(),'foundation_task':'budget','report_batch_task':'current-test'}
        core.atomic_json(annual_costs.home('current-test')/'状态.json',{'hold':{'reason':'three_consecutive_anomalies'}})
        core.atomic_json(foundation_budget.path(),{'id':'budget','phase':'complete','budget_cny':0})
        annual_costs.admit(cfg)
        with patch.object(current_batch,'authorized'):
            ticket=foundation_budget.reserve(cfg,'deep_report',{'max_tokens':1})
        self.assertIn(ticket,foundation_budget.load()['calls'])
        annual_costs.finish_report({**cfg,'_report_cost_policy':{}},{'document_id':'one'})
        self.assertIsNotNone(annual_costs.status('current-test')['hold'])
    def test_old_spend_and_unknown_usage_are_not_treated_as_free(self):
        cfg={**self.cfg(),'report_batch_task':'current-test'}
        core.atomic_json(annual_costs.ledger_path(annual_costs.home('current-test'),'one','deepseek-flash'),{'calls':{'old':{'actual_cost_cny':4.9}}})
        with patch.object(report_cost_guard,'cost',return_value=.11):
            with self.assertRaises(core.ServiceError):fees.before_request(cfg,'deep_report',{'max_tokens':1})
        cfg=self.cfg('unknown');first=fees.before_request(cfg,'deep_report',{'max_tokens':1000})
        fees.record_usage(cfg,'deep_report',{}, {},first)
        ledger=fees.read(fees.ledger_path('unknown'),{})
        self.assertEqual(ledger['calls'][first['request_id']]['cost_cny'],first['request_upper_cny'])
    def test_oversize_request_starts_no_transport_or_aggregate_reservation(self):
        with patch.object(models,'api_key',return_value='fixture'),patch.object(report_cost_guard,'cost',return_value=5.1), \
             patch.object(models,'model_open') as net,patch.object(foundation_budget,'reserve') as reserve:
            with self.assertRaises(core.ServiceError):models._completion_once('','公开财报',purpose='deep_report',settings=self.cfg())
        net.assert_not_called();reserve.assert_not_called()
    def test_pretransport_failure_releases_document_reservation(self):
        with patch.object(models,'api_key',return_value='fixture'),patch.object(foundation_budget,'reserve',side_effect=core.ServiceError('fixture','fixture')):
            with self.assertRaises(core.ServiceError):models._completion_once('','公开财报',purpose='deep_report',settings=self.cfg())
        calls=fees.read(fees.ledger_path('one'),{})['calls'].values()
        self.assertTrue(all(c['state']=='settled' and c['cost_cny']==0 for c in calls))
    def test_no_relative_baseline_needed_and_no_fallback_after_cost_skip(self):
        cfg=self.cfg();cfg.pop('single_model');cfg['model_profiles']=[{'id':'first','enabled':True,'provider':'deepseek','model':'deepseek-v4-pro'}, {'id':'second','enabled':True,'provider':'deepseek','model':'deepseek-flash'}]
        with patch.object(models,'_completion_once',side_effect=core.ServiceError('report_cost_limit','fixture')) as complete:
            with self.assertRaises(core.ServiceError):models.completion('','',purpose='deep_report',settings=cfg)
        self.assertEqual(complete.call_count,1)
        with patch.object(report_cost_guard,'estimate',side_effect=AssertionError('old relative guard')):
            self.assertIsNotNone(report_cost_guard.require_approved(self.cfg('baseline-free'),'deep_report',{'max_tokens':1}))

if __name__=='__main__':unittest.main()
