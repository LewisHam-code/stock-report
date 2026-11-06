import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from stock_research import core, model_timing as timing, models, pipeline
from stock_research.core import ServiceError, Store, TZ


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=TZ)


def cfg():
    return {'provider':'deepseek','model':'deepseek-v4-pro','prefer_off_peak':True,
            'max_output_tokens':1000,'daily_api_calls':32,'analysis_per_run':1,'download_per_run':1,
            'model_profiles':[{'id':'primary','provider':'deepseek','model':'deepseek-v4-pro','enabled':True,
                               'test_mode':False,'stages':['extraction','analysis']}]}


class TimingTests(unittest.TestCase):
    def test_peak_and_five_minute_guard_boundaries(self):
        for hour, deferred in [('08:54:59',False),('08:55:00',True),('11:59:59',True),('12:00:00',False),
                               ('13:54:59',False),('13:55:00',True),('17:59:59',True),('18:00:00',False)]:
            with self.subTest(hour=hour):
                self.assertEqual(timing.window(cfg(),at('2026-10-19T'+hour))['deferred'],deferred)
        self.assertTrue(timing.window(cfg(),datetime(2026,10,19,2,tzinfo=timezone.utc))['deferred'])
        self.assertIn('12:00',timing.window(cfg(),at('2026-10-19T09:00'))['resume_at'])

    def test_holidays_weekends_and_unknown_calendar_year(self):
        for day in ('2026-10-01','2026-10-10','2026-10-17'):
            self.assertFalse(timing.window(cfg(),at(day+'T10:00'))['deferred'])
        status=timing.window(cfg(),at('2027-01-04T10:00'))
        self.assertTrue(status['deferred'])
        self.assertFalse(status['holiday_calendar_known'])

    def test_other_providers_custom_endpoints_and_connection_checks_are_not_delayed(self):
        for changes in ({'provider':'openai'}, {'model':'unknown-model'}, {'base_url':'https://custom.example/v1'},
                        {'prefer_off_peak':False}):
            self.assertFalse(timing.window({**cfg(),**changes},at('2026-10-19T10:00'))['deferred'])
        self.assertFalse(timing.window(cfg(),at('2026-10-19T10:00'),purpose='connection_test')['deferred'])

    def test_price_wait_happens_before_budget_key_or_network_and_never_falls_back(self):
        settings=cfg();settings['model_profiles'].append({'id':'other','provider':'openai','model':'gpt-5-mini',
            'enabled':True,'test_mode':False,'stages':['extraction','analysis']})
        store=Mock()
        with patch.object(core,'now',return_value=at('2026-10-19T10:00')), \
             patch.object(models,'api_key') as key,patch.object(models,'model_open') as net:
            with self.assertRaises(ServiceError) as caught:
                models.completion('instruction','public filing',store=store,purpose='financial_extraction',settings=settings)
        self.assertEqual(caught.exception.code,timing.WAIT)
        store.reserve_api_call.assert_not_called();key.assert_not_called();net.assert_not_called()

    def test_each_request_rechecks_time_after_previous_stage_finished(self):
        settings={**cfg(),'single_model':True};store=Mock();store.setting.return_value={}
        response={'choices':[{'finish_reason':'stop','message':{'content':'OK'}}]}
        with patch.object(models,'api_key',return_value='test-only-placeholder'), \
             patch.object(models,'model_open',return_value=io.BytesIO(json.dumps(response).encode())) as net:
            with patch.object(core,'now',return_value=at('2026-10-19T08:54')):
                models.completion('i','p',store=store,purpose='financial_extraction',settings=settings)
            with patch.object(core,'now',return_value=at('2026-10-19T08:56')):
                with self.assertRaises(ServiceError) as caught:
                    models.completion('i','p',store=store,purpose='comprehensive_report',settings=settings)
        self.assertEqual(caught.exception.code,timing.WAIT)
        self.assertEqual(net.call_count,1);self.assertEqual(store.reserve_api_call.call_count,1)

    def test_live_idle_policy_overrides_stale_unrestricted_worker(self):
        stale={**cfg(),'single_model':True,'prefer_off_peak':False,'_live_execution_policy':True}
        with patch.object(models,'config',return_value={**stale,'prefer_off_peak':True}), \
             patch.object(core,'now',return_value=at('2026-10-27T10:00')),patch.object(models,'model_open') as net:
            with self.assertRaises(ServiceError) as caught:models.completion('i','p',purpose='deep_report',settings=stale)
        self.assertEqual(caught.exception.code,timing.WAIT);net.assert_not_called()

    def test_collection_continues_during_peak_and_unchanged_queue_resumes_at_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);db=root/'test.sqlite';store=Store(db)
            for n in (1,2):
                store.add_document(dict(id='122500000'+str(n),code='600000',company='示例公司',market='SH',
                    period='2026Q3',title='季度报告',published_at='2026-10-19',source_url='https://static.cninfo.com.cn/example.pdf'))
            store.execute("UPDATE documents SET state='parsed' WHERE id='1225000001'")
            store.conn.close()
            with patch.object(core,'ROOT',root),patch.object(core,'now',return_value=at('2026-10-19T10:00')) as clock, \
                 patch.object(pipeline,'Store',side_effect=lambda:Store(db)),patch.object(pipeline,'config',return_value=cfg()), \
                 patch.object(Store,'import_pilot',return_value=0),patch.object(pipeline,'api_key',return_value='placeholder'), \
                 patch.object(pipeline,'archive_pdf',return_value=('original.pdf','fingerprint',False)) as download, \
                 patch.object(pipeline,'extract_pdf',return_value=('original.pdf','fingerprint','text.json',2)), \
                 patch('stock_research.industry_sources.refresh_researched',return_value={}), \
                 patch.object(pipeline,'research_benchmark',return_value={'bars':[]}), \
                 patch.object(pipeline,'company_snapshot',return_value={}) as snapshots, \
                 patch('stock_research.valuation_inputs.verified_cashflow',return_value={}), \
                 patch('stock_research.analysis.analyze',return_value={'id':'fixture-report','version':'v1'}) as analyze, \
                 patch('stock_research.tracking.collect_tracking_prices',return_value={}) as prices, \
                 patch('stock_research.pdf_retention.cleanup_pdfs',return_value={}), \
                 patch('stock_research.publish.publish',return_value={}),patch('stock_research.publish.publish_status'), \
                 patch('stock_research.vault_archive.sync',return_value={}):
                result=pipeline.run(skip_scan=True)
                self.assertEqual(result['state'],'waiting');self.assertIn(timing.WAIT,result['blockers'])
                self.assertEqual(result['parsed'],1);self.assertEqual(result['errors'],[])
                download.assert_called_once();prices.assert_called_once();snapshots.assert_not_called();analyze.assert_not_called()
                store=Store(db)
                self.assertTrue(all(r['state']=='parsed' and r['attempts']==0 and r['error_code'] is None and r['next_retry'] is None
                                    for r in store.rows('SELECT state,attempts,error_code,next_retry FROM documents')))
                store.conn.close()
                clock.return_value=at('2026-10-19T18:00')
                resumed=pipeline.run(skip_scan=True)
                self.assertEqual(resumed['analyzed'],1);analyze.assert_called_once()
