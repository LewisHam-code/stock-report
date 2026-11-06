import json,tempfile,threading,time,unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from stock_research import core,current_batch as batch,report_corrections as corrections,annual_costs


def _spawn_error_probe(directory):
    # The actual spawn transport is tested against its own database, never the
    # live research DB or its task/fee controls.
    core.ROOT=Path(directory)/'spawn-root';core.VAULT=Path(directory)/'spawn-vault'
    error=core.ServiceError('report_cost_limit','fixture')
    with patch.object(batch,'settings',return_value={}),patch.object(batch,'admit',side_effect=error):
        return batch._isolated_process({'id':'isolated-fixture','code':'600000'})


class CurrentBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=patch.object(core,'VAULT',Path(self.temp.name));p.start();self.addCleanup(p.stop)
        p=patch('stock_research.annual_batch.exclusion',return_value=None);p.start();self.addCleanup(p.stop)
        self.rows=[dict(id=str(i),code=str(i).zfill(6),market='SZ',period='2026Q3',company='示例',title='2026年第三季度报告',
                        published_at='2026-10-19',source_url='https://static.cninfo.com.cn/'+str(i)+'.PDF') for i in range(5)]
    def task(self):
        core.atomic_json(batch.folder()/'控制.json',dict(id='current-test',enabled=True,policy_enabled=True,phase='ready',continuous_idle=True,
            workspace=core.digest(str(core.ROOT.resolve())),
            start_inclusive='2026-09-01',workers=2,states={},cursor=0,total=len(self.rows),cost_policy={}))
        core.atomic_json(batch.folder()/'队列.json',self.rows)
    def test_period_switches_to_latest_available_season(self):
        for month,expected in ((1,'2025FY'),(4,'2026Q1'),(7,'2026H1'),(10,'2026Q3')):
            self.assertEqual(batch.current_period(datetime(2026,month,1)),expected)
    def test_scope_rejects_old_year_and_not_latest_quarter(self):
        self.task()
        for period in ('2025FY','2024FY','2026H1'):self.assertFalse(batch.eligible({**self.rows[0],'period':period}))
        self.assertFalse(batch.eligible({**self.rows[0],'published_at':'2026-08-31'}))
        self.assertTrue(batch.eligible(self.rows[0]))
        self.assertEqual(batch.queue_scope()[1],('2026Q3','2026-09-01'))

    def test_temporary_database_cannot_adopt_another_workspaces_live_task(self):
        self.task()
        with patch.object(core,'ROOT',Path(self.temp.name)/'isolated-database'):
            self.assertFalse(batch.managed());self.assertFalse(batch.active());self.assertEqual(batch.queue_scope(),('',()))
    def test_exchange_alias_matches_but_corrected_later_version_does_not(self):
        row=self.rows[0]
        self.assertTrue(batch.same_version(row,{**row,'id':'other','source_url':'https://official.example/new.pdf'}))
        self.assertFalse(batch.same_version(row,{**row,'id':'new','source_url':'https://official.example/correction.pdf','published_at':'2026-10-20','title':'2026年第三季度报告（更正后）'}))
    def test_only_real_not_demo_reports_are_skipped(self):
        class Store:
            def rows(inner,*args):return [{**self.rows[0],'payload':json.dumps({'id':'demo','test_mode':True})}]
        self.assertIsNone(batch.interpreted(Store(),self.rows[0]))
    def test_two_lanes_run_concurrently_and_commit_only_on_coordinator(self):
        self.task();barrier=threading.Barrier(2);lock=threading.Lock();active=peak=0;ids=[];coordinator=threading.get_ident()
        def process(row):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active);ids.append(row['id'])
            if len(ids)<=2:barrier.wait(timeout=2)
            time.sleep(.01)
            with lock:active-=1
            return {'state':'already_read'}
        def commit(result):self.assertEqual(threading.get_ident(),coordinator)
        with patch.object(batch,'settings',return_value={}),patch.object(batch,'admit'),patch.object(batch,'_process',side_effect=process),patch.object(batch,'_commit',side_effect=commit):
            result=batch.tick(max_seconds=10)
        self.assertEqual(peak,2);self.assertEqual(len(ids),len(set(ids)));self.assertEqual(result['completed'],5);self.assertEqual(result['phase'],'complete')
    def test_bad_pdf_skips_without_stopping_following_documents(self):
        self.task();seen=[]
        def process(row):
            seen.append(row['id'])
            if row['id']=='0':raise core.ServiceError('pdf_text_parse_failed','fixture')
            return {'state':'already_read'}
        with patch.object(batch,'settings',return_value={}),patch.object(batch,'admit'),patch.object(batch,'_process',side_effect=process),patch.object(batch,'_commit'):
            result=batch.tick(max_seconds=10)
        self.assertEqual(len(seen),5);self.assertEqual(result['needs_review'],1);self.assertEqual(result['completed'],4)
    def test_ten_lanes_do_not_duplicate_jobs_and_publish_together(self):
        self.rows=[{**self.rows[0],'id':str(i),'code':str(i).zfill(6)} for i in range(20)]
        self.task()
        with batch.edit() as task:task['workers']=10
        barrier=threading.Barrier(10);seen=[];lock=threading.Lock();active=peak=0
        def process(row):
            nonlocal active,peak
            with lock:active+=1;peak=max(active,peak);seen.append(row['id'])
            barrier.wait(timeout=3)
            with lock:active-=1
            return {'state':'model_returned','report_id':'report-'+row['id']}
        with patch.object(batch,'settings',return_value={'quarter_publish_batch_size':5}),patch.object(batch,'admit'), \
             patch.object(batch,'_process',side_effect=process),patch.object(batch,'_commit') as commit, \
             patch.object(batch,'_publish_ready',return_value=.01) as publish:
            result=batch.tick(max_seconds=10)
        self.assertEqual(peak,10);self.assertEqual(len(seen),20);self.assertEqual(len(set(seen)),20)
        published=[ident for call in publish.call_args_list for ident in call.args[0]]
        self.assertEqual(len(published),20);self.assertEqual(len(set(published)),20)
        self.assertLess(publish.call_count,20);self.assertEqual(result['completed'],20)
        self.assertEqual(result['throughput']['completed'],20)
        self.assertTrue(all(c.kwargs=={'publish_now':False} for c in commit.call_args_list))

    def test_batch_publish_failure_preserves_unfinished_receipts_for_replay(self):
        self.task()
        with patch.object(batch,'settings',return_value={'quarter_publish_batch_size':5}),patch.object(batch,'admit'), \
             patch.object(batch,'_process',side_effect=lambda r:{'state':'model_returned','report_id':'report-'+r['id']}), \
             patch.object(batch,'_commit') as commit,patch.object(batch,'_publish_ready',side_effect=OSError('fixture')):
            result=batch.tick(max_seconds=10)
        self.assertEqual(result['phase'],'blocked');self.assertEqual(result['completed'],0)
        self.assertEqual(result['blocker'],'current_publication_failed');self.assertGreater(commit.call_count,0)
        with batch.edit() as task:task.update(enabled=True,phase='ready')
        with patch.object(batch,'settings',return_value={'quarter_publish_batch_size':5}),patch.object(batch,'admit'), \
             patch.object(batch,'_process',side_effect=lambda r:{'state':'already_read','report_id':'report-'+r['id']}), \
             patch.object(batch,'_commit'),patch.object(batch,'_publish_ready',return_value=.01) as publish:
            result=batch.tick(max_seconds=10)
        self.assertEqual(result['completed'],5);self.assertEqual(result['throughput']['completed'],0)
        self.assertEqual(sum(len(c.args[0]) for c in publish.call_args_list),5)

    def test_recent_archive_is_deferred_but_force_always_flushes(self):
        from unittest.mock import Mock
        self.task()
        with batch.edit() as task:task.update(archive_pending=True,last_archive={'at':core.stamp()})
        with patch.object(core,'config',return_value={'quarter_archive_interval_seconds':1800}), \
             patch.object(core,'Store',return_value=Mock()),patch('stock_research.vault_archive.sync') as archive:
            self.assertTrue(batch._flush_archive());archive.assert_not_called()
            self.assertTrue(batch.load()['archive_pending'])
            self.assertTrue(batch._flush_archive(force=True));archive.assert_called_once()
            self.assertFalse(batch.load()['archive_pending'])

    def test_preflight_balance_failure_starts_no_worker(self):
        self.task()
        with patch.object(batch,'settings',return_value={}),patch.object(batch,'admit',side_effect=core.ServiceError('quota_exhausted','fixture')),patch.object(batch,'_process') as process:
            result=batch.tick()
        process.assert_not_called();self.assertEqual(result['phase'],'balance_stopped');self.assertFalse(result['enabled'])
    def test_cost_skip_is_terminal_and_does_not_pause_next_document(self):
        self.task();seen=[]
        def process(row):
            seen.append(row['id'])
            if row['id']=='0':raise core.ServiceError('report_cost_limit','fixture')
            return {'state':'already_read'}
        with patch.object(batch,'settings',return_value={}),patch.object(batch,'admit'),patch.object(batch,'_process',side_effect=process),patch.object(batch,'_commit'):
            result=batch.tick(max_seconds=10);again=batch.tick(max_seconds=10)
        self.assertEqual(len(seen),5);self.assertEqual(result['cost_skipped'],1)
        self.assertEqual(result['completed'],4);self.assertEqual(result['phase'],'complete')
        self.assertEqual(again['cursor'],5)
    def test_isolated_protocol_preserves_receipt_cost_skip_and_timing(self):
        from concurrent.futures import Future
        import pickle
        with patch.object(batch,'_process',return_value={'state':'already_read','report_id':'kept'}):
            payload=pickle.loads(pickle.dumps(batch._isolated_process(self.rows[0])))
        future=Future();future.set_result(payload)
        self.assertEqual(batch._worker_result(future,True)['report_id'],'kept')
        for code in ('report_cost_limit','financial_proof_pending','model_off_peak_wait'):
            error=core.ServiceError(code,'fixture');error.timing={'resume_at':'2026-10-27T00:00:00+08:00'}
            with patch.object(batch,'_process',side_effect=error):payload=pickle.loads(pickle.dumps(batch._isolated_process(self.rows[0])))
            future=Future();future.set_result(payload)
            with self.assertRaises(core.ServiceError) as caught:batch._worker_result(future,True)
            self.assertEqual(caught.exception.code,code);self.assertEqual(caught.exception.timing,error.timing)
    def test_isolated_unexpected_error_is_not_misreported_as_done(self):
        from concurrent.futures import Future
        with patch.object(batch,'_process',side_effect=ValueError('fixture')):payload=batch._isolated_process(self.rows[0])
        future=Future();future.set_result(payload)
        with self.assertRaises(RuntimeError):batch._worker_result(future,True)
        self.assertTrue((batch.folder()/'问题'/(self.rows[0]['id']+'-工作进程异常.json')).is_file())

    def test_pdf_row_type_error_is_a_local_review_not_a_global_stop(self):
        namespace={};exec(compile('def fail(row):\n raise TypeError("blank PDF cell")\n','statement_evidence.py','exec'),namespace)
        with patch.object(batch,'_process',side_effect=namespace['fail']):result=batch._isolated_process(self.rows[0])
        self.assertEqual(result['error_code'],'financial_proof_pending');self.assertNotIn('unexpected',result)

    def test_draining_disabled_sibling_keeps_original_blocker(self):
        self.task()
        def process(row):
            with batch.edit() as task:task.update(enabled=False,phase='blocked',blocker='current_unexpected')
            raise core.ServiceError('current_batch_disabled','fixture')
        with patch.object(batch,'settings',return_value={}),patch.object(batch,'admit'),patch.object(batch,'_process',side_effect=process):
            result=batch.tick(max_seconds=10)
        self.assertEqual(result['phase'],'blocked');self.assertEqual(result['blocker'],'current_unexpected')
        self.assertFalse(result['enabled'])
    def test_actual_spawn_transports_error_using_only_isolated_database(self):
        from concurrent.futures import ProcessPoolExecutor,Future
        from multiprocessing import get_context
        with ProcessPoolExecutor(max_workers=1,mp_context=get_context('spawn')) as pool:
            value=pool.submit(_spawn_error_probe,self.temp.name).result(timeout=15)
        self.assertTrue((Path(self.temp.name)/'spawn-root/db/automation.sqlite').is_file())
        future=Future();future.set_result(value)
        with self.assertRaises(core.ServiceError) as caught:batch._worker_result(future,True)
        self.assertEqual(caught.exception.code,'report_cost_limit')
    def test_completed_slice_combines_archive_and_clears_dirty_marker(self):
        from unittest.mock import Mock
        self.task()
        def commit(result):
            with batch.edit() as task:task['archive_pending']=True
        isolated_store=Mock()
        with patch.object(batch,'settings',return_value={}),patch.object(batch,'admit'), \
             patch.object(batch,'_process',return_value={'state':'model_returned'}),patch.object(batch,'_commit',side_effect=commit), \
             patch.object(core,'Store',return_value=isolated_store),patch('stock_research.vault_archive.sync') as archive:
            result=batch.tick(max_seconds=10)
        self.assertEqual(result['completed'],5);archive.assert_called_once()
        self.assertFalse(batch.load()['archive_pending']);self.assertIn('last_archive',batch.load())
    def test_archive_failure_preserves_dirty_marker_and_starts_no_paid_work(self):
        from unittest.mock import Mock
        self.task()
        with batch.edit() as task:task['archive_pending']=True
        with patch.object(core,'Store',return_value=Mock()),patch('stock_research.vault_archive.sync',side_effect=OSError('fixture')), \
             patch.object(batch,'_process') as process:
            result=batch.tick()
        self.assertTrue(batch.load()['archive_pending']);process.assert_not_called()
        self.assertEqual(result['phase'],'blocked');self.assertFalse(result['enabled'])
        with batch.edit() as task:task.update(enabled=True,phase='ready')
        with patch.object(core,'Store',return_value=Mock()),patch('stock_research.vault_archive.sync') as archive, \
             patch.object(batch,'settings',return_value={}),patch.object(batch,'admit'), \
             patch.object(batch,'_process',return_value={'state':'already_read'}),patch.object(batch,'_commit'):
            resumed=batch.tick(max_seconds=10)
        archive.assert_called_once();self.assertEqual(resumed['completed'],5)
    def test_stop_preserves_latest_scope_and_cannot_auto_reenable(self):
        self.task();batch.stop()
        self.assertTrue(batch.managed());self.assertFalse(batch.active());self.assertFalse(batch.continuous())
    def test_quarter_uses_separate_period_and_ledger(self):
        cfg={'report_batch_task':'current-test','_report_cost_policy':{},'_financial_cost_context':{'period':'2026Q3'}}
        self.assertEqual(annual_costs.period_kind(cfg),'Q3')
        self.assertEqual(annual_costs.seed_name(annual_costs.task_id(cfg)),'财季规模平均基线.json')
        self.assertIn('财季费用',str(annual_costs.home('current-test')))
    def test_original_errata_replay_exactly_matches_effective_document(self):
        old='股东比例0.21%，净利润10万元';new='股东比例0.24%，净利润10万元'
        self.assertEqual(corrections.replay(old,corrections.patches(old,new)),new)
        self.assertEqual(corrections.classify('更正股东比例，其他内容不变',[{'text':old*20}],[{'text':new+old*19}]),'original_with_errata')
        self.assertEqual(corrections.classify('重新编制，其他内容不变',[{'text':old}],[{'text':new}]),'official_replacement')
    def test_errata_wrong_original_is_rejected(self):
        with self.assertRaises(core.ServiceError):corrections.replay('原文',[{'start':0,'end':1,'before':'错','after':'对'}])

    def test_announcement_observation_never_becomes_prediction_accuracy(self):
        from stock_research.filing_performance import observation
        rows={'adjustment':'qfq','bars':[{'date':'2026-10-'+str(d),'open':10,'close':11} for d in (12,13,14)]}
        result=observation({'generated_at':'2026-10-20'}, {'published_at':'2026-10-10'}, {'prices':rows},rows)
        outcome=result['horizons']['3'];self.assertEqual(outcome['entry_date'],'2026-10-12');self.assertEqual(outcome['return_pct'],10)
        self.assertFalse({'aligned','relative_aligned','evaluable'}&outcome.keys())

    def test_two_threads_cannot_reserve_the_same_remaining_budget(self):
        from concurrent.futures import ThreadPoolExecutor
        from stock_research import foundation_budget
        self.task();body={'messages':[],'max_tokens':100}
        maximum=(len(core.dumps(body).encode())*9+100*27)/1_000_000
        core.atomic_json(foundation_budget.path(),{'id':'budget-test','budget_cny':maximum*1.5,'phase':'complete'})
        cfg={'foundation_task':'budget-test','report_batch_task':'current-test','provider':'deepseek','model':'deepseek-v4-pro'}
        def reserve(_):
            try:return foundation_budget.reserve(cfg,'deep_report',body)
            except core.ServiceError as error:return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reserve,range(2)))
        self.assertEqual(results.count('foundation_budget_limit'),1)
        self.assertEqual(len(foundation_budget.load()['calls']),1)

if __name__=='__main__':unittest.main()
