import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stock_research import core,historical_reports as projection,tracking


class HistoricalPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name,value in (('VAULT',self.root/'vault'),('SITE',self.root/'site')):
            p=patch.object(core,name,value);p.start();self.addCleanup(p.stop)
        self.receipts=self.root/'receipts';self.receipts.mkdir()
        p=patch('stock_research.publication_market.capture',return_value={});p.start();self.addCleanup(p.stop)

    def fixture(self,role='flash',acceptance='accepted'):
        report={'id':'historical-'+role,'document_id':'122500001','version':role,'code':'600000','market':'SH',
                'company':'示例公司','industry':'示例行业','date':'2026-10-19','period':'2025FY','rating':'buy',
                'evidence_status':'sufficient','conclusion':'收入增长，现金流需核实。','score':80,
                'score_status':'calculated','score_coverage':100,'model':'deepseek-flash' if role=='flash' else 'deepseek-v4-pro',
                'generated_at':'2026-10-19T20:00:00+08:00','financial_analysis':{'metrics':[{'source_page':20,'quote':'原文证据'}]},
                '_generation_evidence':{'requests':{'secret':'private prompt'}},
                'document_provenance':{'source_url':'https://example.com/report.pdf','pdf':'private/fixture.pdf'},
                'model_steps':{'extraction':{'profile_id':'private-id','usage':{'prompt_tokens':123}}}}
        file=core.VAULT/'30_公司/SH/600000/历史年报解读'/(role+'.json');core.atomic_json(file,report)
        receipt={'state':'reviewed','report_id':report['id'],'document_id':report['document_id'],
                 'report_path':str(file.relative_to(core.VAULT)),'acceptance':acceptance}
        core.atomic_json(self.receipts/('SH600000-2025-'+role+'.json'),receipt)
        return file,report,receipt

    def test_completed_reports_become_searchable_without_paid_calls_or_changing_originals(self):
        file,original,_=self.fixture();before=file.read_bytes()
        with patch('stock_research.models.completion') as paid:
            projection.sync_receipts(self.receipts,'fixture',[1,0])
        paid.assert_not_called();self.assertEqual(file.read_bytes(),before)
        rows=projection.feed()['reports'];self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['period_label'],'2025年报')
        self.assertEqual(rows[0]['report_kind'],'historical_foundation')
        self.assertFalse(rows[0]['formal_tracking_eligible']);self.assertEqual(rows[0]['rating'],'unrated')

    def test_pro_and_flash_remain_inspectable_as_one_filing_row(self):
        self.fixture('flash');self.fixture('pro',acceptance='needs_review')
        projection.sync_receipts(self.receipts,'fixture',[1,1])
        data=projection.feed();self.assertEqual(len(data['reports']),1);self.assertEqual(data['version_count'],2)
        row=data['reports'][0];self.assertEqual(row['id'],'historical-pro');self.assertEqual(row['review_state'],'needs_review')
        self.assertEqual({v['role'] for v in row['review_versions']},{'flash_deep','pro_review'})
        for version in ('flash','pro'):
            detail=json.loads((core.SITE/'report-details'/('historical-'+version+'.json')).read_text())
            self.assertEqual(detail['financial_analysis']['metrics'][0]['source_page'],20)
            self.assertNotIn('_generation_evidence',detail);self.assertNotIn('model_steps',detail)
            self.assertNotIn('pdf',detail['document_provenance'])

    def test_unchanged_progress_and_new_progress_without_new_receipts_do_not_reread_reports(self):
        self.fixture();projection.sync_receipts(self.receipts,'fixture',[1])
        revision=projection.feed()['revision']
        with patch.object(projection,'_apply') as apply:
            projection.sync_receipts(self.receipts,'fixture',[1]);projection.sync_receipts(self.receipts,'fixture',[2])
        apply.assert_not_called();self.assertEqual(projection.feed()['revision'],revision)
        self.assertEqual(projection.feed(revision),{'revision':revision,'unchanged':True})

    def test_new_detail_is_added_incrementally_without_duplicate_existing_rows(self):
        self.fixture();projection.sync_receipts(self.receipts,'fixture',[1,0])
        self.fixture('pro');projection.sync_receipts(self.receipts,'fixture',[1,1])
        row=projection.feed()['reports'][0]
        self.assertEqual(len(projection.feed()['reports']),1);self.assertEqual(row['review_role'],'pro_review')

    def test_report_path_and_identity_mismatch_cannot_publish_other_local_documents(self):
        _,_,receipt=self.fixture()
        with self.assertRaises(ValueError):projection.publish_receipt({**receipt,'report_path':'../../private.json'})
        with self.assertRaises(ValueError):projection.publish_receipt({**receipt,'report_id':'other-report'})
        self.assertFalse((core.SITE/'historical-reports.json').exists())

    def test_historical_feed_does_not_modify_live_feed_or_tracking(self):
        _,_,receipt=self.fixture();live={'reports':[{'id':'live-quarter','period':'2026Q3'}]}
        core.atomic_json(core.SITE/'reports.json',live)
        projection.publish_receipt(receipt)
        self.assertEqual(json.loads((core.SITE/'reports.json').read_text()),live)
        historical=json.loads((core.SITE/'report-details/historical-flash.json').read_text())
        original=copy.deepcopy(historical)
        self.assertEqual(tracking.capture_baselines([historical],{}),{})
        self.assertEqual(historical,original)
