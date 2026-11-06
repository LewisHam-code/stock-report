import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stock_research import core,vault_archive as archive
from stock_research.core import Store,ServiceError


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'runtime';self.vault=Path(self.temp.name)/'vault'
        self.store=Store(self.root/'db/automation.sqlite');self.addCleanup(self.store.conn.close)
        self.cfg={'watchlist':[{'code':'600001','company':'示例','market':'SH','added_at':'2026-10-19'}],
                  'automatic_enabled':True,'strategy_version':'original-rule',
                  'model_profiles':[{'id':'p1','model':'test','provider':'deepseek','enabled':True,'key_env':'PRIVATE_REFERENCE'}]}
        self.store.add_document({'id':'1225000001','code':'600001','company':'示例','market':'SH','period':'2026Q3',
                                 'title':'报告','published_at':'2026-10-19','source_url':'https://example.com/source.pdf'})
        self.store.execute('INSERT INTO reports VALUES(?,?,?,?,?)',('r1','1225000001','v1',json.dumps({'id':'r1','rating':'unrated'}),'2026-10-19'))
        self.store.execute('INSERT INTO snapshots VALUES(?,?,?)',('600001','2026-10-19',json.dumps({'quote':{'price':10}})))
        self.store.cache('proof-key',{'original':'原表证据','value':100},hours=24)
        self.store.execute('INSERT INTO api_calls(created_at,purpose,state) VALUES(?,?,?)',('2026-10-19','test','ok'))
        self.store.set('original-setting',{'data':True})
        (self.root/'text').mkdir();(self.root/'text/original.json').write_text('{"pages":[{"page":1,"text":"原文"}]}')
        (self.root/'db/tracking-baselines.json').write_text('{"r1":{"rating":"unrated","provenance":"backfilled"}}')

    def sync(self):return archive.sync(self.store,self.vault,self.root,self.cfg)

    def test_round_trip_preserves_every_table_text_watchlist_and_stops_autorun(self):
        result=self.sync()
        original=self.store.rows('SELECT * FROM reports')
        recovered=archive.restore_isolated(Path(self.temp.name)/'recovered',self.vault)
        self.assertTrue(recovered['ok']);self.assertTrue(all(recovered['tables_verified'].values()))
        self.assertEqual(recovered['texts_verified'],1)
        self.assertEqual(recovered['historical_input_gaps'],1)
        self.assertEqual(self.store.rows('SELECT * FROM reports'),original)
        cfg=json.loads((Path(self.temp.name)/'recovered/automation.json').read_text())
        self.assertFalse(cfg['automatic_enabled']);self.assertFalse(cfg['model_profiles'][0]['enabled'])
        self.assertNotIn('key_env',cfg['model_profiles'][0])
        self.assertEqual(cfg['watchlist'],self.cfg['watchlist'])
        self.assertFalse(list(self.vault.rglob('*.pdf')))

    def test_no_change_means_no_new_objects_or_rewrite(self):
        first=self.sync();manifest=self.vault/archive.ARCHIVE/'归档清单.json'
        mtime=manifest.stat().st_mtime_ns
        second=self.sync()
        self.assertFalse(second['changed']);self.assertEqual(second['new_objects'],0)
        self.assertEqual(manifest.stat().st_mtime_ns,mtime)

    def test_corrupt_blob_is_rejected_before_creating_a_restore(self):
        self.sync()
        blob=next(self.vault.rglob('*.json.gz'));blob.write_bytes(b'bad')
        target=Path(self.temp.name)/'recovered'
        with self.assertRaises((ServiceError,OSError,EOFError)):
            archive.restore_isolated(target,self.vault)
        self.assertFalse(target.exists())

    def test_refuses_existing_target_and_sensitive_fields(self):
        self.sync()
        target=Path(self.temp.name)/'recovered';target.mkdir();(target/'keep.txt').write_text('keep')
        with self.assertRaises(ServiceError):archive.restore_isolated(target,self.vault)
        self.assertEqual((target/'keep.txt').read_text(),'keep')
        self.store.cache('unsafe',{'api_key':'test-only'},hours=1)
        with self.assertRaises(ServiceError) as caught:self.sync()
        self.assertEqual(caught.exception.code,'archive_secret_field')

    def test_recent_watchlist_is_recoverable_without_full_archive_refresh(self):
        self.sync()
        archive.sync_watchlist({'watchlist':[],'pdf_unwatched_at':{'600001':'2026-10-20'}},self.vault)
        archive.restore_isolated(Path(self.temp.name)/'recovered',self.vault)
        cfg=json.loads((Path(self.temp.name)/'recovered/automation.json').read_text())
        self.assertEqual(cfg['watchlist'],[])
        self.assertIn('600001',cfg['pdf_unwatched_at'])

    def test_pipeline_archives_after_run_is_finalized(self):
        from stock_research import pipeline,tracking,publish
        cfg={'download_per_run':0,'analysis_per_run':0}
        with patch.object(core,'ROOT',self.root),patch.object(core,'VAULT',self.vault), \
             patch.object(pipeline,'config',return_value=cfg), \
             patch.object(pipeline,'research_benchmark',return_value={'bars':[]}), \
             patch('stock_research.industry_sources.refresh_researched',return_value={}), \
             patch.object(Store,'import_pilot',return_value=0),patch.object(pipeline,'download_queue',return_value=[]), \
             patch.object(publish,'publish',return_value={}),patch.object(publish,'publish_status'), \
             patch.object(tracking,'collect_tracking_prices',return_value={}), \
             patch('stock_research.pdf_retention.cleanup_pdfs',return_value={}):
            result=pipeline.run(skip_scan=True,skip_ai=True)
        self.assertEqual(result['state'],'ok')
        recovered=archive.restore_isolated(Path(self.temp.name)/'recovered',self.vault)
        self.assertTrue(recovered['ok'])
        copied=Store(Path(self.temp.name)/'recovered/db/automation.sqlite')
        try:self.assertEqual(copied.rows('SELECT state FROM runs')[-1]['state'],'ok')
        finally:copied.conn.close()


if __name__=='__main__':unittest.main()
