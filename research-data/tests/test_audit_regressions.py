"""Regression checks for budget, queue, publication and operational failures."""
import contextlib
import io
import json
import pathlib
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from stock_research import core, deployment, models, pipeline, publish, service, tracking
from stock_research.core import ServiceError, Store, now
from stock_research.ratings import classify


class BudgetTests(unittest.TestCase):
    def test_exhausted_budget_does_not_refetch_financial_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root/'budget.sqlite'
            store = Store(path)
            store.add_document(dict(id='1225000001', code='600000', company='样例公司', market='SH', period='2026Q3',
                                    title='季度报告', published_at=now().isoformat(), source_url='https://static.cninfo.com.cn/example.pdf'))
            store.execute("UPDATE documents SET state='parsed'")
            store.reserve_api_call('analysis', 1)
            store.conn.close()
            cfg={'download_per_run':1, 'analysis_per_run':1, 'daily_api_calls':1}
            with patch.object(core, 'ROOT', root), patch.object(pipeline, 'Store', side_effect=lambda: Store(path)), \
                 patch.object(pipeline, 'config', return_value=cfg), patch.object(Store, 'import_pilot', return_value=0), \
                 patch.object(pipeline, 'model_profiles', return_value=[{'enabled':True,'provider':'deepseek','id':'example'}]), \
                 patch.object(pipeline, 'api_key', return_value='placeholder'), \
                 patch.object(pipeline, 'company_snapshot') as snapshots, patch('stock_research.analysis.analyze') as analyze, \
                 patch.object(tracking, 'collect_tracking_prices', return_value={}), \
                 patch('stock_research.pdf_retention.cleanup_pdfs', return_value={}), \
                 patch.object(publish, 'publish', return_value={}), patch.object(publish, 'publish_status'), \
                 patch('stock_research.vault_archive.sync',return_value={}):
                result=pipeline.run(skip_scan=True)
            self.assertEqual(result['state'],'waiting')
            self.assertIn('daily_api_limit',result['blockers'])
            snapshots.assert_not_called()
            analyze.assert_not_called()

    def test_concurrent_checks_cannot_overrun_daily_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'budget.sqlite'
            Store(path).conn.close()
            barrier = threading.Barrier(2)
            def reserve(_):
                store = Store(path)
                try:
                    barrier.wait(timeout=5)
                    try:
                        store.reserve_api_call('connection_test', 1)
                        return 'reserved'
                    except ServiceError as error:
                        return error.code
                finally:
                    store.conn.close()
            with ThreadPoolExecutor(2) as pool:
                self.assertCountEqual(list(pool.map(reserve, range(2))), ['reserved', 'daily_api_limit'])

    def test_connection_check_obeys_the_same_budget_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'budget.sqlite'
            store = Store(path)
            store.reserve_api_call('analysis', 1)
            store.conn.close()
            cfg = dict(provider='deepseek', model='deepseek-v4-pro', single_model=True,
                       daily_api_calls=1, max_output_tokens=256)
            with patch.object(models, 'Store', side_effect=lambda: Store(path)), patch.object(models, 'config', return_value=cfg), \
                 patch.object(models, 'api_key', return_value='placeholder-credential'), patch.object(models, 'model_open') as network:
                with self.assertRaises(ServiceError) as caught:
                    models.connection_test()
                self.assertEqual(caught.exception.code, 'daily_api_limit')
                network.assert_not_called()


class QueueAndRatingTests(unittest.TestCase):
    def test_risk_constraint_never_upgrades_a_sell(self):
        for score in (0, 10, 24.9):
            result = classify({'score': score, 'status': 'calculated'}, [], ['现金流匹配度偏低'], {})
            self.assertEqual(result['rating'], 'sell')
        self.assertEqual(classify({'score': 95, 'status': 'calculated'}, [], ['现金流匹配度偏低'], {'buy_signal': True})['rating'], 'underweight')

    def test_unread_filings_precede_refreshes_then_oldest_refresh_first(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(pathlib.Path(directory) / 'queue.sqlite')
            self.addCleanup(store.conn.close)
            for n, date in ((1, '2026-10-01'), (2, '2026-10-17'), (3, '2026-10-16')):
                ident = '122500000' + str(n)
                store.add_document(dict(id=ident, code='600000', company='样例公司', market='SH', period='2026Q3',
                                        title='季度报告', published_at=date, source_url='https://static.cninfo.com.cn/example.pdf'))
                store.execute("UPDATE documents SET state='parsed' WHERE id=?", (ident,))
                if n != 1:
                    store.execute('INSERT INTO reports VALUES(?,?,?,?,?)', (str(n), ident, str(n), '{}', '2026-10-15' if n == 2 else '2026-10-14'))
            self.assertEqual([d['id'][-1] for d in pipeline.analysis_queue(store, 3)], ['1', '3', '2'])


class PublicationTests(unittest.TestCase):
    def test_note_metadata_update_preserves_user_body(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'filing.md'
            body = '# 财报\n\n我的手工批注：保留。\n'
            path.write_text('---\nstatus: "待解读"\ncustom: true\n---\n' + body)
            publish.update_note_properties(path, {'status': '已解读', 'industry_l1': '制造业'})
            self.assertEqual(path.read_text().split('\n---\n', 1)[1], body)
            self.assertIn('custom: true', path.read_text())
            self.assertIn('status: "已解读"', path.read_text())

    def test_latest_index_keeps_all_detail_versions_and_syncs_note_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with contextlib.ExitStack() as stack:
                for name, value in (('ROOT', root), ('SITE', root/'site'), ('VAULT', root/'vault')):
                    stack.enter_context(patch.object(publish, name, value))
                stack.enter_context(patch.object(tracking, 'BASELINES', root/'tracking-baselines.json'))
                stack.enter_context(patch.object(tracking, 'SITE', root/'site'))
                stack.enter_context(patch.object(publish, 'public_settings', return_value={}))
                stack.enter_context(patch('stock_research.publication_market.capture',return_value=None))
                store = Store(root/'db.sqlite')
                self.addCleanup(store.conn.close)
                ident = '1225000001'
                store.add_document(dict(id=ident, code='600000', company='样例公司', market='SH', period='2026Q3',
                                        title='季度报告', published_at=now().isoformat(), source_url='https://static.cninfo.com.cn/example.pdf'))
                store.execute("UPDATE documents SET state='complete' WHERE id=?", (ident,))
                for version in ('v1', 'v2'):
                    report = dict(id='sample-'+version, document_id=ident, version=version, code='600000', company='样例公司', market='SH',
                                  industry='制造业', period='2026Q3', date=now().date().isoformat(), generated_at=now().isoformat(),
                                  rating='buy', score=83, score_status='calculated', score_coverage=100,
                                  evidence_status='sufficient', test_mode=False, source='https://static.cninfo.com.cn/example.pdf',
                                  provider='deepseek', model='sample', conclusion='营收增长，现金流需要复核。', evidence='支持线索', valuation='原时点评估',
                                  risks='风险事项', topics=[], falsification=[], financial_analysis={'metrics': [], 'sections': [], 'industry': '制造业'}, industry_evidence=[])
                    report['_generation_evidence']={'requests':{'narrative':{'input':'原时点输入'}}}
                    store.execute('INSERT INTO reports VALUES(?,?,?,?,?)', (report['id'], ident, version, json.dumps(report), now().isoformat()))
                folder = root/'vault/30_公司/SH/600000'
                filing = folder/'财报'/f'2026Q3-{ident}.md'
                filing.parent.mkdir(parents=True)
                filing.write_text('---\nstatus: "待解读"\n---\n保留个人批注。\n')
                result = publish.publish(store)
                index = json.loads((root/'site/reports.json').read_text())
                self.assertEqual(result['reports'], 1)
                self.assertEqual(result['versions'], 2)
                self.assertEqual([r['id'] for r in index['reports']], ['sample-v2'])
                self.assertEqual(len(list((root/'site/report-details').glob('*.json'))), 2)
                self.assertNotIn('_generation_evidence',json.loads((root/'site/report-details/sample-v2.json').read_text()))
                self.assertEqual(len(list((folder/'研究').glob('*.md'))), 2)
                self.assertIn('status: "已解读"', filing.read_text())
                self.assertTrue(filing.read_text().endswith('保留个人批注。\n'))
                detail = json.loads((root/'site/report-details/sample-v2.json').read_text())
                self.assertEqual(detail['rating'], 'unrated')
                self.assertFalse(detail['decision']['technical']['available'])
                self.assertEqual(detail['score'], detail['decision']['score'])
                # Incremental publication recomputes the requested version,
                # retaining every existing version and its original judgment.
                previous=(root/'site/report-details/sample-v1.json').read_bytes()
                with patch('stock_research.financial_recheck.recheck',wraps=__import__('stock_research.financial_recheck',fromlist=['recheck']).recheck) as recheck:
                    incremental=publish.publish(store,archive=False,incremental_ids={'sample-v2'})
                self.assertEqual(incremental['publication']['rendered'],1)
                self.assertEqual(incremental['publication']['reused'],1)
                self.assertEqual(recheck.call_count,1)
                self.assertEqual((root/'site/report-details/sample-v1.json').read_bytes(),previous)
                current=json.loads((root/'site/report-details/sample-v2.json').read_text())
                self.assertEqual(current['financial_analysis'],detail['financial_analysis'])
                self.assertEqual(current['score'],detail['score'])
                self.assertEqual(current['rating'],detail['rating'])
                self.assertNotIn('_generation_evidence',current)
                live=json.loads((root/'site/report-details/sample-v1.json').read_text())
                live['research_profile']['current']={'asof':'2026-10-28','market_refresh':True}
                core.atomic_json(root/'site/report-details/sample-v1.json',live)
                live_update=publish.publish(store,archive=False,incremental_ids={'sample-v2'})
                self.assertEqual(live_update['publication']['reused'],1)
                self.assertEqual(json.loads((root/'site/report-details/sample-v1.json').read_text())['research_profile']['current']['market_refresh'],True)
                live['score']=99
                core.atomic_json(root/'site/report-details/sample-v1.json',live)
                tampered=publish.publish(store,archive=False,incremental_ids={'sample-v2'},preserve_existing=True)
                self.assertEqual(tampered['publication']['reused'],0)
                self.assertNotEqual(json.loads((root/'site/report-details/sample-v1.json').read_text())['score'],99)
                # A changed source document, edited detail or revision cannot
                # silently adopt the earlier cached rendering.
                store.execute('UPDATE documents SET sha256=? WHERE id=?',('new-source',ident))
                changed=publish.publish(store,archive=False,incremental_ids={'sample-v2'})
                self.assertEqual(changed['publication']['reused'],0)
                (root/'site/report-details/sample-v1.json').write_text('{}')
                repaired=publish.publish(store,archive=False,incremental_ids={'sample-v2'})
                self.assertEqual(repaired['publication']['reused'],0)
                self.assertEqual(json.loads((root/'site/report-details/sample-v1.json').read_text())['company'],'样例公司')
                original=json.loads(store.rows('SELECT payload FROM reports WHERE id=?',('sample-v1',))[0]['payload'])
                original['conclusion']='修改后的原始正文必须重新发布'
                store.execute('UPDATE reports SET payload=? WHERE id=?',(json.dumps(original),'sample-v1'))
                updated=publish.publish(store,archive=False,incremental_ids={'sample-v2'})
                self.assertEqual(updated['publication']['reused'],0)
                self.assertEqual(json.loads((root/'site/report-details/sample-v1.json').read_text())['conclusion'],original['conclusion'])
                full=publish.publish(store,archive=False)
                self.assertEqual(full['publication']['rendered'],2)
                self.assertEqual(full['publication']['reused'],0)
                with patch.object(publish,'_publication_revision',return_value='new-code'):
                    rebuilt=publish.publish(store,archive=False,incremental_ids={'sample-v2'})
                self.assertEqual(rebuilt['publication']['reused'],0)
                # Queue publication keeps unchanged old artifacts at their own
                # revision, while certifying the newly requested report fully.
                old_bytes=(root/'site/report-details/sample-v1.json').read_bytes()
                with patch.object(publish,'_publication_revision',return_value='queue-priority-code'):
                    with patch('stock_research.financial_recheck.recheck',wraps=__import__('stock_research.financial_recheck',fromlist=['recheck']).recheck) as recheck:
                        priority=publish.publish(store,archive=False,incremental_ids={'sample-v2'},preserve_existing=True)
                    self.assertEqual(recheck.call_count,1)
                    self.assertEqual(priority['publication']['rendered'],1)
                    self.assertEqual(priority['publication']['refresh_pending'],1)
                    self.assertEqual((root/'site/report-details/sample-v1.json').read_bytes(),old_bytes)
                    index=json.loads((root/'site/reports.json').read_text())
                    self.assertFalse(index['reports'][0]['publication_refresh_pending'])
                    self.assertEqual(set(publish._publication_cache('queue-priority-code')),{'sample-v2'})
                    with patch('stock_research.financial_recheck.recheck',wraps=__import__('stock_research.financial_recheck',fromlist=['recheck']).recheck) as recheck:
                        bounded=publish.publish(store,archive=False,incremental_ids=set(),preserve_existing=True,refresh_limit=1)
                    self.assertEqual(recheck.call_count,1)
                    self.assertEqual(bounded['publication']['rendered'],1)
                    self.assertEqual(bounded['publication']['refresh_pending'],0)
                    self.assertEqual(set(publish._publication_cache('queue-priority-code')),{'sample-v1','sample-v2'})
                self.assertEqual(json.loads(store.rows('SELECT payload FROM reports LIMIT 1')[0]['payload'])['rating'], 'buy')
                baseline = json.loads((root/'tracking-baselines.json').read_text())['sample-v2']
                self.assertEqual((baseline['rating'], baseline['score']), ('buy', 83))

    def test_publish_failure_records_failure_and_preserves_primary_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with patch.object(core, 'ROOT', root), patch.object(pipeline, 'Store', side_effect=lambda: Store(root/'db.sqlite')), \
                 patch.object(pipeline, 'config', return_value={'download_per_run': 1, 'market_refresh_per_run': 0, 'analysis_per_run': 1}), \
                 patch.object(Store, 'import_pilot', return_value=0), patch('stock_research.backtest.run_backtests', return_value={}), \
                 patch.object(tracking, 'collect_tracking_prices', return_value={}), \
                 patch.object(publish, 'publish', side_effect=OSError('sensitive internal error')), \
                 patch.object(publish, 'publish_status', side_effect=OSError('secondary error')):
                result = pipeline.run(skip_scan=True, skip_ai=True)
                self.assertEqual(result['code'], 'pipeline_unexpected')
                self.assertNotIn('sensitive', str(result))
                store = Store(root/'db.sqlite')
                self.assertEqual(store.rows('SELECT state FROM runs')[0]['state'], 'failed')
                store.conn.close()


class StatusTests(unittest.TestCase):
    def test_status_failure_is_sanitized(self):
        handler = object.__new__(service.Handler)
        handler.trusted = lambda: True
        handler.handle_get = Mock(side_effect=subprocess.TimeoutExpired('private-command', 15))
        handler.send_json = Mock()
        handler.do_GET()
        data, status = handler.send_json.call_args.args
        self.assertEqual(status, 503)
        self.assertNotIn('private-command', str(data))

    def test_cli_reports_unsuccessful_run_as_nonzero(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('stock_cli_audit', pathlib.Path(__file__).parents[1]/'run.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch('sys.argv', ['stock', 'run']), patch('stock_research.runtime.ensure_service'), \
             patch.object(pipeline, 'run', return_value={'ok': False, 'state': 'waiting'}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(), 2)


if __name__ == '__main__':
    unittest.main()
