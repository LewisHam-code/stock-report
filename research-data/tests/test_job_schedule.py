import json,tempfile,unittest
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
from stock_research import core,job_schedule as schedule,model_timing,scheduled_jobs,disclosure_intake


def at(hour,minute=0,day=20):return datetime(2026,10,day,hour,minute,tzinfo=core.TZ)

class JobScheduleTests(unittest.TestCase):
    def test_disclosures_every_fifteen_minutes_including_nights_weekends(self):
        for clock in (at(2),at(16),at(23),at(2,day=24)):
            slot=schedule.disclosure_slot(clock)
            self.assertEqual(slot['interval_seconds'],900)
            self.assertFalse(schedule.disclosure_due({'slot':slot['slot']},clock+timedelta(minutes=14)))
            self.assertTrue(schedule.disclosure_due({'slot':slot['slot']},clock+timedelta(minutes=15)))
    def test_restart_and_midnight_use_durable_slots(self):
        clock=at(23,59);state={'slot':schedule.disclosure_slot(clock)['slot']}
        self.assertFalse(schedule.disclosure_due(state,clock))
        self.assertTrue(schedule.disclosure_due(state,clock+timedelta(minutes=1)))
    def test_market_lunch_closed_holidays_and_once_after_close(self):
        for clock in (at(9,29),at(11,30),at(12),at(15),at(15,34),at(10,day=24),at(10,day=1)):
            self.assertIsNone(schedule.market_slot(clock))
        for clock in (at(9,30),at(11,29),at(13),at(14,59),at(15,35)):
            self.assertTrue(schedule.market_due({},clock))
        state={'market_slot':schedule.market_slot(at(15,35))['slot']}
        self.assertFalse(schedule.market_due(state,at(23)))
    def test_actual_tariff_does_not_include_admission_guard(self):
        cfg={'provider':'deepseek','model':'deepseek-v4-pro','prefer_off_peak':False}
        self.assertEqual(model_timing.price_multiplier(cfg,at(8,57)),1)
        self.assertEqual(model_timing.price_multiplier(cfg,at(9)),2)
        self.assertEqual(model_timing.price_multiplier(cfg,at(18)),1)
        self.assertFalse(model_timing.window(cfg,at(10))['deferred'])
    def test_model_free_scanning_is_not_blocked_by_pdf_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=core.Store(root/'fixture.sqlite')
            row=dict(id='1226000001',market='SH',code='600001',company='示例',period='2026Q3',title='第三季度报告',published_at='2026-10-20T00:00:00+08:00',source_url='https://static.cninfo.com.cn/public.PDF')
            def scan(*args):
                store.add_document(row)
                return {'new':1,'sources':[],'pages':3,'examined':1}
            with patch.object(core,'VAULT',root/'vault'),patch.object(core,'Store',return_value=store),patch.object(core,'now',return_value=at(2)),patch('stock_research.securities.refresh'),patch('stock_research.sources.collect',side_effect=scan),patch('stock_research.pipeline.retire_delisted'),patch('stock_research.current_batch.enqueue_discovered'),patch.object(disclosure_intake,'_download') as download,patch('stock_research.models.completion') as model:
                result=disclosure_intake.run()
            self.assertEqual(result['new'],1);download.assert_not_called();model.assert_not_called()
            logs=list((root/'vault'/'00_基础资料'/'财报采集调度'/'发现记录').glob('*.json'))
            self.assertEqual(json.loads(logs[0].read_text())['documents'][0]['publication_time_precision'],'date')
    def test_independent_service_lanes_and_no_wake_for_complete_queue(self):
        server=SimpleNamespace();cfg={'automatic_enabled':True,'prefer_off_peak':False}
        with patch.object(core,'config',return_value=cfg),patch.object(schedule,'disclosure_due',return_value=True),patch.object(schedule,'market_due',return_value=True),patch('stock_research.current_batch.load',return_value={'phase':'complete'}),patch('stock_research.current_batch.managed',return_value=True),patch('stock_research.current_batch.active',return_value=False),patch.object(scheduled_jobs,'start',return_value=True) as start:
            scheduled_jobs.poll(server,1000)
            self.assertEqual([c.args[1] for c in start.call_args_list],['_disclosure_thread','_tracking_thread'])
    def test_zero_count_limit_still_records_every_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=core.Store(Path(tmp)/'test.sqlite')
            try:
                for _ in range(3):store.reserve_api_call('connection_test',0)
                self.assertEqual(store.api_calls_today(),3)
                with self.assertRaises(core.ServiceError):store.reserve_api_call('connection_test',3)
            finally:store.conn.close()

    def test_idle_queue_reenters_ready_without_disabling_idle_preference(self):
        from stock_research import current_batch
        with tempfile.TemporaryDirectory() as tmp:
            cfg={'automatic_enabled':True,'prefer_off_peak':True,'provider':'deepseek','model':'deepseek-v4-pro'}
            with patch.object(core,'VAULT',Path(tmp)),patch.object(core,'ROOT',Path(tmp)/'runtime'),patch.object(core,'config',return_value=cfg):
                core.atomic_json(current_batch.folder()/'控制.json',{'id':'fixture','policy_enabled':True,'workspace':core.digest(str(core.ROOT.resolve())),'enabled':True,'phase':'waiting_idle','continuous_idle':True})
                with patch.object(schedule,'disclosure_due',return_value=False),patch.object(schedule,'market_due',return_value=False), \
                     patch.object(current_batch,'settings',return_value=cfg),patch.object(scheduled_jobs,'start',return_value=True) as start, \
                     patch.object(core,'now',return_value=at(18)):
                    scheduled_jobs.poll(SimpleNamespace(),1000)
                self.assertEqual(current_batch.load()['phase'],'ready');self.assertTrue(cfg['prefer_off_peak'])
                self.assertIn('_wake_thread',[c.args[1] for c in start.call_args_list])

if __name__=='__main__':unittest.main()
