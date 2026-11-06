"""Cross-stage audit regressions; fixtures never call paid model services."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_research import analysis,core,models,pipeline,publish,tracking
from stock_research.core import ServiceError,Store


class AuditRegressionTests(unittest.TestCase):
    def test_failed_model_validation_preserves_reported_token_usage(self):
        cfg={'provider':'deepseek','model':'fixture-model','single_model':True,
             'max_output_tokens':256,'daily_api_calls':10}
        schema={'type':'object','properties':{'answer':{'type':'string'}},'required':['answer'],'additionalProperties':False}
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db.sqlite');self.addCleanup(store.conn.close)
            for finish in ('stop','length'):
                response={'choices':[{'finish_reason':finish,'message':{'content':'{}'}}],
                          'usage':{'prompt_tokens':20,'completion_tokens':7}}
                with patch.object(models,'api_key',return_value='test-only-placeholder'), \
                     patch.object(models,'model_open',return_value=io.BytesIO(json.dumps(response).encode())):
                    with self.assertRaises(ServiceError):models.completion('instruction','prompt',schema,store,settings=cfg)
            rows=store.rows('SELECT state,input_tokens,output_tokens FROM api_calls')
            self.assertEqual([r['state'] for r in rows],['model_schema_invalid','model_output_incomplete'])
            self.assertTrue(all(r['input_tokens']==20 and r['output_tokens']==7 for r in rows))

    def test_footer_and_modified_date_do_not_become_publication_date(self):
        today=core.now().date().isoformat()
        old='<meta name="PubDate" content="2020-01-01"><footer>'+today+'</footer>'
        self.assertIsNone(analysis.publication_date(old,'文章内容 '+today)[0])
        self.assertIsNone(analysis.publication_date('<meta property="article:modified_time" content="'+today+'">','更新于 '+today)[0])
        raw='<meta property="article:published_time" content="'+today+'">'
        self.assertEqual(analysis.publication_date(raw,'正文')[0],today)

    def test_three_same_pdf_failures_stop_the_round_without_touching_remaining_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);db=root/'test.sqlite';store=Store(db)
            for number in range(5):
                store.add_document({'id':str(1225000000+number),'code':'600000','company':'示例','market':'SH',
                    'period':'2026Q3','title':'2026年第三季度报告','published_at':core.stamp(),
                    'source_url':'https://static.cninfo.com.cn/finalpage/2026-10-19/1225000000.PDF'})
            store.conn.close()
            with patch.object(core,'ROOT',root),patch.object(pipeline,'Store',side_effect=lambda:Store(db)), \
                 patch.object(pipeline,'config',return_value={'download_per_run':10,'analysis_per_run':0}), \
                 patch.object(Store,'import_pilot',return_value=0), \
                 patch.object(pipeline,'archive_pdf',side_effect=ServiceError('source_network','offline')) as download, \
                 patch.object(tracking,'collect_tracking_prices',return_value={}), \
                 patch('stock_research.pdf_retention.cleanup_pdfs',return_value={}), \
                 patch.object(publish,'publish',return_value={}),patch.object(publish,'publish_status'), \
                 patch('stock_research.vault_archive.sync',return_value={}):
                result=pipeline.run(skip_scan=True,skip_ai=True)
            self.assertEqual(download.call_count,3)
            self.assertIn('pdf_source_circuit_breaker',result['blockers'])
            store=Store(db);self.addCleanup(store.conn.close)
            self.assertEqual(store.rows('SELECT COUNT(*) n FROM documents WHERE attempts=0')[0]['n'],2)
