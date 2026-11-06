import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from datetime import datetime,timedelta

from stock_research import core,publication_market as publication,market_sources,market_metrics,annual_batch,sources,analysis,disclosure_sources


class PublicationMarketTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=patch.object(core,'VAULT',Path(self.temp.name));p.start();self.addCleanup(p.stop)
        p=patch('stock_research.securities.price_enabled',return_value=True);p.start();self.addCleanup(p.stop)
        self.report={'id':'fixture-report','code':'600000','market':'SH','generated_at':core.stamp(),'financial_analysis':{'metrics':[]}}
        self.quote={'code':'600000','provider':'eastmoney','price':10,'market_cap':1000,'asof':'2026-10-19T15:00:00+08:00',
                    'pe_ttm':12,'pe_dynamic_source':14,'pe_static_source':18,'pb':2,'cross_validation':{'status':'matched'}}

    def test_capture_occurs_after_analysis_without_ai_and_never_overwrites_original_inputs(self):
        original=copy.deepcopy(self.report)
        with patch.object(market_sources,'latest_quote',return_value=self.quote) as fetch,patch('stock_research.models.completion') as paid:
            view=publication.attach(self.report,collect=True)
            repeated=publication.attach(self.report,collect=True)
        paid.assert_not_called();fetch.assert_called_once();self.assertEqual(self.report,original)
        self.assertEqual(view['market_at_publication']['price'],10)
        self.assertEqual(view['market_publication_context']['kind'],'publication')
        self.assertEqual(view['market_publication_context'],repeated['market_publication_context'])
        self.assertTrue(publication.path(self.report).is_file())

    def test_repaired_eps_updates_presentation_without_recapturing_historical_price(self):
        with patch.object(market_sources,'latest_quote',return_value=self.quote):publication.capture(self.report)
        original=publication.path(self.report).read_bytes()
        corrected={**self.report,'financial_analysis':{'fact_contract':'financial-facts-v1','data_quality':{'analysis_ready':False},
            'metrics':[{'metric':'eps','scope':'cumulative','verified':True,'fact_id':'proof','standard_value':.35,'period_end':'2026-09-30'}]}}
        with patch.object(market_sources,'latest_quote') as network:
            result=publication.attach(corrected)
        network.assert_not_called();self.assertEqual(result['market_at_publication']['eps'],.35)
        self.assertEqual(result['market_at_publication']['price'],10)
        self.assertEqual(publication.path(self.report).read_bytes(),original)

    def test_old_report_is_explicitly_backfilled_and_keeps_its_old_market_snapshot(self):
        report={**self.report,'generated_at':(core.now()-timedelta(days=1)).isoformat(),'market_at_analysis':{'price':8,'asof':'2026-10-18'}}
        with patch.object(market_sources,'latest_quote',return_value=self.quote):view=publication.attach(report,collect=True)
        self.assertEqual(view['market_publication_context']['kind'],'backfill')
        self.assertEqual(view['market_at_analysis']['price'],8);self.assertEqual(view['market_at_publication']['price'],10)

    def test_late_retry_after_failed_initial_capture_is_a_supplement(self):
        prior={'state':'unavailable','kind':'publication','attempts':1,'retry_after':(core.now()-timedelta(minutes=1)).isoformat()}
        core.atomic_json(publication.path(self.report),prior)
        with patch.object(market_sources,'latest_quote',return_value=self.quote):result=publication.capture(self.report,backfill=True)
        self.assertEqual(result['kind'],'backfill');self.assertEqual(result['state'],'available')

    def test_two_versions_reuse_company_quote_but_keep_distinct_capture_records(self):
        with patch.object(market_sources,'latest_quote',return_value=self.quote) as fetch:
            first=publication.capture(self.report);second=publication.capture({**self.report,'id':'pro-version'})
        fetch.assert_called_once();self.assertNotEqual(first['report_id'],second['report_id'])

    def test_failed_source_does_not_block_report_publication_or_retry_on_every_refresh(self):
        with patch.object(market_sources,'latest_quote',side_effect=core.ServiceError('quote_unavailable','fixture')) as fetch:
            first=publication.attach(self.report,collect=True);second=publication.attach(self.report,collect=True)
        fetch.assert_called_once();self.assertEqual(first['market_publication_context']['state'],'unavailable')
        self.assertNotIn('market_at_publication',second)

    def test_provider_pe_ratios_are_not_replaced_by_historical_report_annualization(self):
        fa={'metrics':[{'metric':'net_profit','scope':'cumulative','verified':True,'standard_value':100,'period_end':'2025-12-31'}]}
        result=market_metrics.build_market_snapshot(self.quote,fa,[{'period_end':'2025-12-31','net_profit':100}])
        self.assertEqual(result['pe_dynamic'],14);self.assertEqual(result['pe_static'],18)
        self.assertEqual(result['pe_dynamic_calculated'],10)
        self.assertEqual(result['dynamic_pe_basis'],'provider');self.assertIsNone(result['dynamic_pe_period'])
        loss=market_metrics.build_market_snapshot({**self.quote,'pe_dynamic_source':-3},fa)
        self.assertIsNone(loss['pe_dynamic'])

    def test_retired_stock_makes_no_quote_download_or_model_requests(self):
        with patch('stock_research.securities.price_enabled',return_value=False),patch.object(sources,'request') as network,patch.object(market_sources,'eastmoney_quote') as primary,patch.object(analysis,'completion') as model:
            for action in (lambda:market_sources.latest_quote('600000','SH'),lambda:market_sources.daily_bars('600000','SH'),
                           lambda:sources.archive_pdf({'code':'600000'}),lambda:analysis.analyze(None,{'code':'600000'},{})):
                with self.assertRaises(core.ServiceError):action()
            self.assertEqual(disclosure_sources.add_disclosure(Mock(),{'code':'600000'}),0)
        network.assert_not_called();primary.assert_not_called();model.assert_not_called()

    def test_st_exclusion_is_specific_to_one_off_task_and_uses_latest_security_state(self):
        with patch('stock_research.securities.lookup',return_value={'code':'600000','company':'*ST示例','status':'listed','risk_flag':'*ST'}):
            self.assertEqual(annual_batch.exclusion({'code':'600000','company':'旧简称'}),'annual_st_excluded')
        with patch('stock_research.securities.lookup',return_value={'code':'600000','company':'示例','status':'delisted'}):
            self.assertEqual(annual_batch.exclusion({'code':'600000'}),'security_delisted')
        with patch('stock_research.securities.lookup',return_value={'code':'600000','company':'示例','status':'listed','risk_flag':''}):
            self.assertIsNone(annual_batch.exclusion({'code':'600000'}))
