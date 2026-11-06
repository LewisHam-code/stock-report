import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from stock_research import core,foundation_data as data,foundation_budget as budget,models
from stock_research.core import ServiceError,Store


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.patch=patch.object(core,'VAULT',self.root);self.patch.start();self.addCleanup(self.patch.stop)

    def test_six_annuals_and_twelve_non_future_quarters(self):
        from datetime import date
        annual,quarter=data.periods(date(2026,10,19))
        self.assertEqual(len(annual),6);self.assertEqual(len(quarter),12)
        self.assertEqual(annual[0],'2020-12-31');self.assertEqual(quarter[-1],'2026-09-30')
        self.assertNotIn('2026-12-31',quarter)
        self.assertEqual(data.quarter_ends(date(2026,6,30))[0],'2023-09-30')
        self.assertEqual(data.quarter_ends(date(2026,10,19),13)[0],'2023-09-30')

    def test_historical_annual_full_text_title_is_not_dropped(self):
        from stock_research.sources import report_period
        self.assertEqual(report_period('2023年年度报告全文'),'2023FY')
        self.assertEqual(report_period('某公司2024年年度报告全文（修订版）'),'2024FY')
        self.assertIsNone(report_period('2023年年度报告摘要'))
        self.assertEqual(report_period('境内同步披露公告-2026年中期报告'),'2026H1')
        self.assertIsNone(report_period('境内同步披露公告-2026年中期报告摘要'))
        self.assertIsNone(report_period('境内同步披露公告-2026年中期业绩公告'))

    def test_historical_discovery_uses_exact_identity_when_name_is_full_width(self):
        from stock_research import foundation_job as job,sources,securities
        doc={'code':'000888','market':'SZ','period':'2023FY','id':'fixture','published_at':'2024-04-01'}
        response={'announcements':[{}],'totalAnnouncement':1}
        with patch.object(job,'cninfo_identity',return_value='000888,gssz0000888'),patch.object(securities,'lookup',return_value=None),patch.object(sources,'query_announcements',return_value=response) as query,patch.object(sources,'document_from_record',return_value=doc):
            self.assertEqual(job.find_annual(Mock(),{'code':'000888','market':'SZ','company':'峨眉山Ａ','year':2023}),doc)
        self.assertEqual(query.call_args.kwargs,{'stock':'000888,gssz0000888','keyword':''})

    def seed_budget(self,limit=10):
        core.atomic_json(budget.path(),{'id':'fixture','phase':'pilot','budget_cny':limit,'calls':{}})

    def test_one_off_budget_requires_authorized_flash_and_reserves_before_spending(self):
        self.seed_budget(.01)
        cfg={'foundation_task':'fixture','provider':'deepseek','model':'deepseek-flash','prefer_off_peak':False}
        body={'messages':[{'content':'财报'}],'max_tokens':1000}
        ticket=budget.reserve(cfg,'financial_extraction',body)
        with self.assertRaises(ServiceError):budget.reserve(cfg,'financial_extraction',body)
        budget.settle(ticket,{'prompt_tokens':100,'completion_tokens':10,'prompt_cache_hit_tokens':20})
        state=budget.load();self.assertEqual(state['reserved_cny'],0)
        self.assertLess(state['estimated_spend_cny'],.01)
        with self.assertRaises(ServiceError):budget.reserve({**cfg,'model':'deepseek-v4-pro'},'financial_extraction',body)

    def test_unknown_usage_keeps_the_reserved_cost_instead_of_inventing_zero(self):
        self.seed_budget()
        ticket=budget.reserve({'foundation_task':'fixture','provider':'deepseek','model':'deepseek-flash'},'financial_extraction',{'max_tokens':100})
        maximum=budget.load()['calls'][ticket]['maximum'];budget.settle(ticket,{})
        self.assertEqual(budget.load()['estimated_spend_cny'],maximum)

    def test_database_rejection_before_network_releases_task_reservation(self):
        self.seed_budget()
        cfg={**core.config(),'foundation_task':'fixture','provider':'deepseek','model':'deepseek-flash','prefer_off_peak':False}
        store=Mock();store.setting.return_value={};store.reserve_api_call.side_effect=OSError('fixture')
        with patch.object(models,'api_key',return_value='fixture-only'),patch.object(models,'model_open') as network:
            with self.assertRaises(OSError):models._completion_once('','',store=store,settings=cfg)
            network.assert_not_called()
        self.assertEqual(budget.load()['reserved_cny'],0)
        self.assertEqual(budget.load()['estimated_spend_cny'],0)

    def test_validation_hold_does_not_indefinitely_pause_the_normal_strategy(self):
        from stock_research import foundation_job as job
        for phase,expected in (('pilot',True),('waiting_idle',True),('bulk',True),('needs_review',False),('source_or_model_blocked',False),('complete',False)):
            core.atomic_json(budget.path(),{'workspace':core.digest(str(core.ROOT.resolve())),'phase':phase})
            self.assertEqual(job.active(),expected)

    def test_focused_plan_prioritizes_watchlist_and_does_not_reuse_superseded_recommendations(self):
        from stock_research import foundation_job as job
        site=self.root/'data';site.mkdir()
        core.atomic_json(site/'reports.json',{'reports':[
            {'code':'600000','date':'2026-10-19','rating':'unrated'},
            {'code':'600000','date':'2026-10-18','rating':'buy','score_status':'calculated','evidence_status':'sufficient','score_coverage':100,'score':90}]})
        cfg={**core.config(),'watchlist':[{'code':'603132','market':'SH','company':'fixture'}]}
        with patch.object(core,'SITE',site),patch.object(core,'config',return_value=cfg):value=job.plan_focused()
        self.assertEqual(value['queued'],3)
        self.assertEqual(value['scope']['candidate_companies'],0)
        self.assertTrue(all(r['code']=='603132' for r in budget.load()['focused_queue']))

    def test_focused_peak_deferral_does_not_download_or_call_any_model(self):
        from stock_research import foundation_job as job
        with patch.object(job,'settings',return_value={}),patch('stock_research.model_timing.window',return_value={'deferred':True,'resume_at':'fixture'}),patch.object(job,'summarize',return_value={}),patch.object(job,'review') as review:
            self.assertEqual(job.run_focused()['waiting_for'],'fixture')
        review.assert_not_called()

    def test_focused_full_text_limit_rejects_before_paid_analysis(self):
        from stock_research import foundation_job as job,sources,analysis
        doc={'id':'fixture','sha256':'fixture','text_file':'fixture.json'}
        with patch.object(core,'Store'),patch.object(job,'settings',return_value={}),patch('stock_research.financial_evidence._text_record',return_value=(None,{'pages':[{}]})),patch.object(sources,'excerpt',return_value=('x'*900001,{'full_text':True})),patch.object(analysis,'analyze') as model:
            with self.assertRaises(ServiceError) as error:job.review({'document':doc},full_text=True)
        self.assertEqual(error.exception.code,'foundation_full_text_too_large')
        model.assert_not_called()

    def test_error_breaker_preserves_another_workers_finished_paid_result(self):
        from threading import Event
        from stock_research import foundation_job as job
        ready=Event()
        fixture={'phase':'pilot','pilot_target':50,'results':{},'candidates':[{'sample_index':i,'code':str(i)} for i in range(4)]}
        core.atomic_json(budget.path(),fixture)
        def review(candidate):
            if candidate['sample_index']==1:
                self.assertTrue(ready.wait(2));return {'state':'reviewed'}
            raise ServiceError('fixture','fixture')
        def summarize():
            value=budget.load()
            if sum(r['state']=='failed' for r in value['results'].values())==3:ready.set()
            return {'phase':value['phase']}
        with patch.object(job,'initialize',return_value=fixture),patch.object(job,'review',side_effect=review),patch.object(job,'summarize',side_effect=summarize),patch('builtins.print'):
            job.run_pilot()
        final=budget.load()
        self.assertEqual(final['phase'],'source_or_model_blocked')
        self.assertEqual(final['results']['1']['state'],'reviewed')

    def test_bootstrap_call_records_do_not_exhaust_normal_daily_quota(self):
        store=Store(self.root/'test.sqlite');self.addCleanup(store.conn.close)
        store.reserve_api_call('foundation:fixture:financial_extraction',10**9)
        self.assertEqual(store.api_calls_today(),0)
        store.reserve_api_call('financial_extraction',1)
        self.assertEqual(store.api_calls_today(),1)
        with self.assertRaises(ServiceError):store.reserve_api_call('analysis',1)

    def test_public_history_omits_repeated_lineage_and_withholds_conflicting_values(self):
        file=self.root/'30_公司/SH/600000/历史财务-结构化.json'
        core.atomic_json(file,{'code':'600000','market':'SH','asof':core.stamp(),'basis':'公开数据待核对','history':[
            {'period_end':'2025-12-31','revenue':100,'net_profit':10,'conflicts':['revenue'],'sources':{'income':{'large':'x'*1000}}}]})
        result=data.financial_for('600000','SH')['history'][0]
        self.assertIsNone(result['revenue']);self.assertEqual(result['net_profit'],10)
        self.assertNotIn('sources',result)

    def test_each_completed_page_is_reused_after_restart_without_network(self):
        value={'data':[{'SECUCODE':'600000.SH','REPORT_DATE':'2025-12-31'}],'count':1}
        with patch.object(data,'page',return_value=value) as net:
            data.collect_table('income','2025-12-31');data.collect_table('income','2025-12-31')
            net.assert_called_once()

    def test_extraction_and_deep_review_use_their_assigned_routes(self):
        cfg={**core.config(),'model_profiles':[
            {'id':'flash','provider':'deepseek','model':'deepseek-flash','enabled':True,'stages':['extraction']},
            {'id':'pro','provider':'deepseek','model':'deepseek-v4-pro','enabled':True,'stages':['deep_analysis']}]}
        with patch.object(models,'_completion_once',return_value=('ok',{})) as run:
            models.completion('','',purpose='deep_report',settings=cfg)
        self.assertEqual(run.call_args.args[-1]['profile_id'],'pro')
