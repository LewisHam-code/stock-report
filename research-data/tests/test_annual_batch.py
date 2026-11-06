import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from stock_research import annual_batch as batch,core,foundation_budget as budget,models


class AnnualBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        p=patch.object(core,'VAULT',self.root);p.start();self.addCleanup(p.stop)
        core.atomic_json(budget.path(),{'id':'shared','phase':'complete','budget_cny':1000,'calls':{}})

    def seed(self):
        row={'id':'SH600000-2025','year':2025,'market':'SH','code':'600000','company':'fixture','board':'fixture','fixed_year':True}
        core.atomic_json(batch.folder()/'队列.json',[row])
        with batch.edit() as task:
            task.update(id='fixture',phase='ready',enabled=True,workspace=core.digest(str(core.ROOT.resolve())),
                        cursor=0,total=1,reviewed=0,accepted=0,downloaded=0,needs_review=0,failed=0,unavailable=0,
                        pro_reviewed=0,pro_exception_review=True)
        return row

    def cfg(self,pro=False):
        return {'foundation_task':'shared','one_off_annual_task':'fixture','provider':'deepseek','profile_id':'fixture',
                'model':'deepseek-v4-pro' if pro else 'deepseek-flash','_foundation_exception_review':pro,'prefer_off_peak':True}

    def test_absent_control_is_inert(self):
        with patch.object(batch,'settings') as settings:
            self.assertFalse(batch.active());batch.tick()
            settings.assert_not_called()

    def test_bad_pdf_is_recorded_and_next_report_runs_without_retry(self):
        row=self.seed();next_row={**row,'id':'SH600001-2025','code':'600001'}
        core.atomic_json(batch.folder()/'队列.json',[row,next_row])
        with batch.edit() as task:task['total']=2
        core.atomic_json(batch._receipt(next_row),{'acceptance':'accepted','exception_reasons':[]})
        failure=core.ServiceError('pdf_text_parse_failed','fixture');failure.details={'page':2,'parser_error':'KeyError'}
        with patch.object(batch,'settings',return_value=self.cfg()),patch.object(batch,'admit'),patch.object(batch,'exclusion',return_value=None),patch.object(batch,'_review',side_effect=failure) as review:
            result=batch.tick()
        review.assert_called_once()
        self.assertEqual(result['cursor'],2);self.assertEqual(result['reviewed'],1)
        self.assertEqual(result['phase'],'complete')
        issue=json.loads((batch.folder()/'问题'/(row['id']+'.json')).read_text())
        self.assertEqual(issue['details']['page'],2)
        self.assertEqual(issue['state'],'needs_review')

    def test_status_rechecks_idle_time_without_mutation_or_starting_work(self):
        self.seed()
        with batch.edit() as task:task.update(phase='waiting_idle',blocker='model_off_peak_wait')
        before=(batch.folder()/'控制.json').read_bytes()
        with patch('stock_research.model_timing.window',return_value={'enabled':True,'deferred':False}),patch.object(batch,'tick') as tick:
            result=batch.status()
        self.assertEqual(result['time_policy'],{'enabled':True,'deferred':False})
        self.assertEqual(result['phase'],'waiting_idle')
        self.assertEqual((batch.folder()/'控制.json').read_bytes(),before)
        tick.assert_not_called()

    def test_stop_disables_only_one_off_task_and_next_model_request(self):
        self.seed();old=budget.load();batch.stop()
        self.assertEqual(budget.load(),old)
        with self.assertRaises(core.ServiceError):batch.authorized(self.cfg())
        with patch.object(models,'api_key') as key,patch.object(models,'model_open') as network:
            with self.assertRaises(core.ServiceError):
                models._completion_once('','',settings=self.cfg())
        key.assert_not_called();network.assert_not_called()

    def test_peak_defers_before_discovery_or_paid_work(self):
        self.seed();error=core.ServiceError('model_off_peak_wait','fixture');error.timing={'resume_at':'fixture'}
        with patch.object(batch,'settings',return_value=self.cfg()),patch.object(batch,'admit',side_effect=error),patch.object(batch,'_review') as review:
            batch.tick()
        self.assertEqual(batch.load()['phase'],'waiting_idle');self.assertTrue(batch.active())
        review.assert_not_called()

    def test_archive_hides_and_disables_entry_without_deleting_queue_or_daily_data(self):
        self.seed();old=budget.load();queue=(batch.folder()/'队列.json').read_bytes()
        result=batch.archive()
        self.assertTrue(result['archived']);self.assertFalse(batch.active())
        self.assertEqual((batch.folder()/'队列.json').read_bytes(),queue);self.assertEqual(budget.load(),old)

    def test_empty_balance_stops_first_attempt_and_does_not_retry_hourly(self):
        self.seed()
        with patch.object(batch,'settings',return_value=self.cfg()),patch.object(batch,'balance_available',return_value=False) as net,patch('stock_research.model_timing.require_idle'),patch.object(batch,'_review') as review:
            batch.tick();batch.tick()
        net.assert_called_once();review.assert_not_called()
        self.assertEqual(batch.load()['phase'],'balance_stopped')

    def test_finished_receipt_is_reused_after_interruption_and_auto_disables(self):
        row=self.seed();core.atomic_json(batch._receipt(row),{'acceptance':'accepted','exception_reasons':[]})
        with patch.object(batch,'settings',return_value=self.cfg()),patch.object(batch,'admit'),patch.object(batch,'_review') as review:
            result=batch.tick()
        review.assert_not_called();self.assertEqual(result['reviewed'],1)
        self.assertEqual(result['phase'],'complete');self.assertFalse(result['enabled'])

    def test_flash_result_count_survives_price_wait_before_pro_without_double_count(self):
        row=self.seed();core.atomic_json(batch._receipt(row),{'acceptance':'needs_review','exception_reasons':['conflict']})
        error=core.ServiceError('model_off_peak_wait','fixture');error.timing={'resume_at':'fixture'}
        with patch.object(batch,'settings',return_value=self.cfg()),patch.object(batch,'admit'),patch.object(batch,'_review',side_effect=error):
            batch.tick();self.assertEqual(batch.load()['reviewed'],1)
            batch.tick();self.assertEqual(batch.load()['reviewed'],1)
        self.assertEqual(batch.load()['cursor'],0)

    def test_pro_is_paid_only_for_an_explicit_exception_and_correct_rates(self):
        self.seed();cfg=self.cfg(pro=True)
        with patch('stock_research.model_timing.window',return_value={'deferred':False}),patch('stock_research.model_timing.price_multiplier',return_value=1):
            ticket=budget.reserve(cfg,'financial_extraction',{'max_tokens':100})
            budget.settle(ticket,{'prompt_tokens':1000,'completion_tokens':100,'prompt_cache_hit_tokens':200})
        self.assertAlmostEqual(budget.load()['estimated_spend_cny'],(200*.15+800*4.5+100*13.5)/1e6)
        with self.assertRaises(core.ServiceError):budget.reserve({**cfg,'_foundation_exception_review':False},'deep_report',{})

    def test_ledger_compaction_preserves_spend_for_budget_protection(self):
        task=budget.load();task['calls']={str(i):{'state':'usage_returned','cost':1,'maximum':2} for i in range(200)}
        budget._compact(task)
        self.assertEqual(len(task['calls']),0);self.assertEqual(budget.committed(task),200)
        self.assertEqual(budget.settled_cost(task),200)
        self.assertTrue((budget.folder()/'调用账本'/task['call_archives'][0]['file']).is_file())

    def test_initialization_keeps_previous_task_and_checks_new_listings(self):
        old=budget.load()
        core.atomic_json(core.VAULT/'00_基础资料'/'证券名录.json',{'securities':[
            {'market':'SH','code':'600000','company':'fixture','status':'listed','listed_at':'1999-01-01'},
            {'market':'SH','code':'603000','company':'new','status':'listed','listed_at':'2026-01-01'},
            {'market':'SZ','code':'000001','company':'old','status':'delisted'}]})
        with patch.object(core,'config',return_value={'watchlist':[]}):result=batch.initialize(2025)
        self.assertEqual(result['total'],2);self.assertEqual(result['excluded_new_listings'],0)
        self.assertEqual(budget.load(),old)
        self.assertEqual(batch.initialize(2025)['id'],result['id'])

    def test_normal_model_work_has_no_one_off_balance_dependency(self):
        cfg={**core.config(),'single_model':True,'prefer_off_peak':False}
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'OK'}}]}).encode()
        with patch.object(batch,'admit') as admit,patch.object(models,'api_key',return_value='fixture-only'),patch.object(models,'model_open',return_value=response):
            value,_=models._completion_once('','',settings=cfg)
        self.assertEqual(value,'OK');admit.assert_not_called()

class ReviewRoutingTests(unittest.TestCase):
    def test_parser_conflicts_or_failed_checks_never_purchase_another_model_read(self):
        for result in ({'conflicts':['revenue'],'critical_corrections':4},
                       {'failed_checks':[{'passed':False}],'critical_corrections':4},
                       {'parser_status':'pdf_table_check_failed','critical_corrections':4}):
            routed=batch.review_routing(result)
            self.assertEqual(routed['exception_reasons'],[])
            self.assertTrue(routed['local_review_reasons'])

    def test_multiple_verified_model_corrections_can_receive_pro_review(self):
        result=batch.review_routing({'critical_corrections':3,'parser_status':'checked'})
        self.assertTrue(result['exception_reasons']);self.assertFalse(result['local_review_reasons'])
