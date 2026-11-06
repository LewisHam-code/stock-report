import copy
import json
import pathlib
import tempfile
import unittest
from unittest.mock import Mock, patch

from stock_research import analysis, core, models, sources
from stock_research.core import ServiceError, Store, now


class TokenEconomyTests(unittest.TestCase):
    def config(self):
        return {**core.config(), 'reading_mode':'initial', '_benchmark_allow_coarse':True, 'model_profiles': [
            {'id':'cheap','provider':'deepseek','model':'small','enabled':True,'test_mode':False,'stages':['extraction']},
            {'id':'strong','provider':'deepseek','model':'large','enabled':True,'test_mode':False,'stages':['analysis']}]}

    def test_each_stage_calls_only_its_enabled_fallback_route(self):
        for purpose, expected in (('financial_extraction','cheap'),('comprehensive_report','strong')):
            with patch.object(models, '_completion_once', return_value=('ok',{})) as call:
                models.completion('','',purpose=purpose,settings=self.config())
                self.assertEqual(call.call_args.args[-1]['profile_id'],expected)

    def test_missing_stage_does_not_silently_use_other_stage(self):
        cfg=self.config();cfg['model_profiles'][1]['enabled']=False
        with patch.object(models,'_completion_once') as call:
            with self.assertRaises(ServiceError):models.completion('','',purpose='comprehensive_report',settings=cfg)
            call.assert_not_called()

    def test_legacy_profiles_keep_both_stages(self):
        cfg=self.config()
        for p in cfg['model_profiles']:p.pop('stages')
        self.assertEqual(core.model_profiles(cfg)[0]['stages'],['extraction','deep_analysis'])

    def test_nonfinite_model_values_are_rejected(self):
        for value in (float('nan'),float('inf'),float('-inf')):
            with self.assertRaises(ServiceError):analysis.validate(value,{'type':'number'})

    def test_saved_profile_stages_are_validated_and_provider_cannot_change(self):
        cfg=self.config()
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            (root/'automation.json').write_text(json.dumps(cfg))
            with patch.object(core,'ROOT',root),patch.object(core,'PROJECT',root),patch.object(core,'read_secret',return_value=''):
                for stages in ([],['download'],['analysis','analysis']):
                    with self.assertRaises(ServiceError):core.save_settings({'id':'cheap','provider':'deepseek','model':'small','stages':stages})
                with self.assertRaises(ServiceError) as caught:core.save_settings({'id':'cheap','provider':'openai','model':'small'})
                self.assertEqual(caught.exception.code,'profile_provider_mismatch')
                result=core.save_settings({'id':'cheap','provider':'deepseek','model':'small','stages':['analysis']})
                self.assertEqual(result['profiles'][0]['stages'],['deep_analysis'])

    def test_compact_payload_preserves_evidence_and_annual_history(self):
        history=[{'period_end':f'{year}-{month:02d}-{day:02d}','revenue':year*month} for year in range(2010,2026) for month,day in ((3,31),(6,30),(9,30),(12,31))]
        quote={'price':20,'asof':now().isoformat()}
        extracted={'metrics':[{'quote':'营业收入 100 元','verified':True}],'sections':[{'analysis':'全部必要的分析'}]}
        initial={'scoring':{'score':80,'references':['x'*5000],'items':['y'*5000]},'technical':{'buy_signal':True},'growth':{'single_quarters':[1,2,3]}}
        payload=analysis.narrative_input({'company':'样例公司','period':'2025FY'},extracted,{'financial':{'history':history},'quote':quote},initial,{'documents':[]})
        self.assertEqual(payload['financial'],extracted)
        self.assertEqual(payload['history_and_quote']['quote'],quote)
        kept=payload['history_and_quote']['financial']['history']
        self.assertEqual(len([r for r in kept if r['period_end'].endswith('12-31')]),6)
        self.assertLessEqual(len(kept),18)
        self.assertNotIn('references',payload['preliminary_rules']['scoring'])
        self.assertEqual(payload['preliminary_rules']['growth'],initial['growth'])

    def test_layout_compression_keeps_numbers_and_page_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            raw='营业收入           1,000.00 元\n\n\n经营现金流          -200.00 元'
            (root/'text.json').write_text(json.dumps({'pages':[{'page':3,'text':raw}]}))
            with patch.object(sources,'ROOT',root),patch.object(sources,'config',return_value={'max_document_chars':1000}):
                text,coverage=sources.excerpt({'text_file':'text.json'})
            self.assertIn('[PDF第3页]',text)
            self.assertIn('1,000.00',text)
            self.assertIn('-200.00',text)
            self.assertTrue(coverage['full_text'])
            self.assertLess(len(text),len(raw))

    def test_document_budget_includes_real_page_markers_and_separators(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            (root/'text.json').write_text(json.dumps({'pages':[{'page':i,'text':'营业收入100元\n资产200元\n'*4} for i in range(1,10)]}))
            with patch.object(sources,'ROOT',root):
                text,coverage=sources.excerpt({'text_file':'text.json'},max_chars=180)
            self.assertLessEqual(len(text),180)
            self.assertTrue(coverage['read_pages'])
            self.assertFalse(coverage['full_text'])

    def test_narrative_model_reuses_extraction_but_changed_pdf_text_invalidates_it(self):
        extracted={'industry':'制造业','metrics':[],'sections':[],'risks':[],'coverage':{'full_text':True}}
        narrative={k:'' for k in ('conclusion','evidence','valuation','risks','valuation_context')}
        narrative.update(industry_outlook='unknown',industry_quotes=[],topics=[],catalysts=[],falsification=[],scenarios=[])
        cfg=self.config()
        doc={'id':'1225000001','sha256':'abc','company':'样例公司','code':'600000','market':'SH','period':'2026Q3','published_at':now().isoformat(),'source_url':'https://static.cninfo.com.cn/example.pdf'}
        stages=[]
        def complete(*args,**kwargs):
            purpose=args[4];stages.append(purpose)
            return copy.deepcopy(extracted if purpose=='financial_extraction' else narrative), {'provider':'deepseek','model':cfg['model_profiles'][0 if purpose=='financial_extraction' else 1]['model'],'profile_id':'recorded','test_mode':False,'_request_body':{'instructions':args[0],'input':args[1]}}
        with tempfile.TemporaryDirectory() as directory:
            store=Store(pathlib.Path(directory)/'cache.sqlite')
            self.addCleanup(store.conn.close)
            with patch.object(analysis,'config',return_value=cfg),patch.object(analysis,'excerpt',return_value=('公开文本',{'full_text':True})) as excerpt, \
                 patch.object(analysis,'verify_extraction',side_effect=lambda d,v,c:v),patch.object(analysis,'industry_documents',return_value={'documents':[],'errors':[]}), \
                 patch.object(analysis,'completion',side_effect=complete),patch('stock_research.financial_facts.require_ready'):
                first=analysis.analyze(store,doc,{})
                cfg['model_profiles'][1]['model']='another-large'
                second=analysis.analyze(store,doc,{})
                self.assertEqual(stages.count('financial_extraction'),1)
                cfg['strategy_version']='new-rating-rules-only'
                analysis.analyze(store,doc,{})
                self.assertEqual(stages.count('financial_extraction'),1)
                cfg['reading_mode']='deep'
                analysis.analyze(store,doc,{})
                self.assertEqual(stages.count('financial_extraction'),2)
                cfg['reading_mode']='initial'
                excerpt.return_value=('补齐旋转文字后的公开文本',{'full_text':True})
                third=analysis.analyze(store,doc,{})
            self.assertEqual(stages.count('financial_extraction'),3)
            self.assertEqual(stages.count('comprehensive_report'),4)
            self.assertEqual(stages.count('deep_report'),1)
            self.assertFalse(first['input_optimization']['extraction_cache_hit'])
            self.assertTrue(second['input_optimization']['extraction_cache_hit'])
            self.assertFalse(third['input_optimization']['extraction_cache_hit'])
            self.assertEqual(first['_generation_evidence']['missing_requests'],[])
            self.assertEqual(first['_generation_evidence']['requests']['extraction'],second['_generation_evidence']['requests']['extraction'])
            self.assertNotIn('_request_body',first['model_steps']['narrative'])

    def test_model_view_keeps_fact_evidence_and_conflicts_without_mutating_archive(self):
        from stock_research.model_inputs import financial_view
        raw={'coverage':{'read_pages':[1,2,3,7,8],'total_pages':9,'full_text':False},
             'metrics':[{'metric':'revenue','value':12,'standard_value':120000,'unit':'ten_thousand_yuan','scope':'cumulative',
                         'period_end':'2025-12-31','source_page':7,'quote':'营业收入12万元','verified':True,
                         'original_model_quote':'营业收入120万元','original_model_value':120,'repair_kind':'source_value_correction',
                         'verification_reason':'页码、原文引文及数值已匹配'},
                        {'metric':'cash','verified':False,'verification_status':'conflict','verification_reason':'合并与摘要冲突',
                         'source_page':8,'quote':'货币资金6元','unit':'yuan','value':6}],
             'data_quality':{'conflicts':[{'metric':'cash'}]},'sections':[{'analysis':'风险依据保留'}]}
        before=copy.deepcopy(raw);view=financial_view(raw)
        self.assertEqual(raw,before)
        self.assertNotIn('original_model_quote',view['metrics'][0])
        self.assertEqual(view['metrics'][0]['value'],12)
        self.assertEqual(view['metrics'][0]['standard_value'],120000)
        self.assertEqual(view['metrics'][0]['quote'],'营业收入12万元')
        self.assertNotIn('value',view['metrics'][1]);self.assertNotIn('quote',view['metrics'][1])
        self.assertEqual(view['metrics'][1]['verification_reason'],raw['metrics'][1]['verification_reason'])
        self.assertEqual(view['data_quality'],raw['data_quality'])
        self.assertEqual(view['sections'],raw['sections'])
        self.assertEqual(view['coverage']['read_page_ranges'],[[1,3],[7,8]])
        self.assertEqual(view['coverage']['read_page_count'],5)

    def test_margin_dedup_keeps_accounting_units_body_rows_and_physical_pages(self):
        title='样例股份有限公司2025年年度报告'
        pages=[{'page':n,'text':f'{title}\n单位：万元 币种：人民币\n营业收入100\n业务情况\n存货为0\n关联交易100\n经营现金流-10\n{n}/4'} for n in range(1,5)]
        before=copy.deepcopy(pages);result=sources.model_pages(pages,'2025FY')
        self.assertEqual(pages,before)
        self.assertEqual(sum(p['text'].count(title) for p in result),1)
        for n,p in enumerate(result,1):
            self.assertEqual(p['page'],n)
            self.assertIn('单位：万元',p['text'])
            self.assertIn('营业收入100',p['text'])
            self.assertIn('经营现金流-10',p['text'])
            self.assertNotIn(f'{n}/4',p['text'])

    def test_existing_current_pdf_text_is_reused_but_obsolete_parser_is_not(self):
        reader=Mock();reader.pages=[Mock(extract_text=Mock(return_value='重新解析的正文'*60))]
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);(root/'text').mkdir()
            target=root/'text'/'fixture-abcdef.json'
            saved={'sha256':'abcdef','text_parser':sources.TEXT_PARSER,'pages':[{'page':1,'text':'已保存文字'*80}],'pdf_proof':{'status':'checked'}}
            target.write_text(json.dumps(saved));doc={'id':'fixture'}
            with patch.object(sources,'ROOT',root),patch.object(sources,'archive_pdf',return_value=('source.pdf','abcdef',root/'source.pdf')),patch('pypdf.PdfReader',return_value=reader) as parse:
                result=sources.extract_pdf(doc)
                parse.assert_not_called()
                self.assertEqual(result,('source.pdf','abcdef','text/fixture-abcdef.json',1))
                self.assertEqual(json.loads(target.read_text()),saved)
                saved['text_parser']='obsolete';target.write_text(json.dumps(saved))
                sources.extract_pdf(doc)
                parse.assert_called_once()

    def test_archived_pdf_is_reused_without_network_or_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            doc={'market':'SH','code':'600000','period':'2026Q3','id':'1225000001','source_url':'https://static.cninfo.com.cn/example.pdf'}
            with patch.object(sources,'ROOT',root),patch.object(sources,'request',return_value=b'%PDF-original-sample') as download:
                relative,sha,path=sources.archive_pdf(doc)
                self.assertEqual(download.call_count,1)
                sources.archive_pdf({**doc,'pdf':relative,'sha256':sha})
                self.assertEqual(download.call_count,1)
                self.assertEqual(path.read_bytes(),b'%PDF-original-sample')
                self.assertEqual(list(path.parent.glob('.stock-download-*')),[])

    def test_empty_content_page_keeps_physical_number_and_is_not_extracted(self):
        blank=Mock();blank.get_contents.return_value=None
        body=Mock();body.get_contents.return_value=object();body.extract_text.return_value='真实正文'*100
        reader=Mock();reader.pages=[blank,body]
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            with patch.object(sources,'ROOT',root),patch.object(sources,'archive_pdf',return_value=('original.pdf','hash',root/'original.pdf')),patch('pypdf.PdfReader',return_value=reader):
                result=sources.extract_pdf({'id':'empty-page'})
            pages=json.loads((root/result[2]).read_text())['pages']
        blank.extract_text.assert_not_called()
        self.assertEqual([p['page'] for p in pages],[1,2])
        self.assertTrue(pages[0]['empty_content']);self.assertIn('真实正文',pages[1]['text'])

    def test_broken_nonempty_page_never_creates_partial_text_record(self):
        page=Mock();page.get_contents.return_value=object();page.extract_text.side_effect=KeyError('fixture')
        reader=Mock();reader.pages=[page]
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            with patch.object(sources,'ROOT',root),patch.object(sources,'archive_pdf',return_value=('original.pdf','hash',root/'original.pdf')),patch('pypdf.PdfReader',return_value=reader):
                with self.assertRaises(ServiceError) as caught:sources.extract_pdf({'id':'broken'})
            self.assertEqual(caught.exception.code,'pdf_text_parse_failed')
            self.assertFalse((root/'text').exists())

    def test_page_markers_do_not_turn_a_scanned_pdf_into_extracted_text(self):
        reader = Mock()
        reader.pages = [Mock(extract_text=Mock(return_value='')) for _ in range(60)]
        fallback = Mock()
        fallback.__enter__ = Mock(return_value=reader)
        fallback.__exit__ = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with patch.object(sources, 'ROOT', root), \
                 patch.object(sources, 'archive_pdf', return_value=('scan.pdf', 'hash', root/'scan.pdf')), \
                 patch('pypdf.PdfReader', return_value=reader), patch('pdfplumber.open', return_value=fallback):
                with self.assertRaises(ServiceError) as caught:
                    sources.extract_pdf({'id': '1225000001'})
            self.assertEqual(caught.exception.code, 'ocr_required')
            self.assertFalse((root/'text').exists())

    def test_interrupted_archive_does_not_leave_a_partial_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            doc={'market':'SH','code':'600000','period':'2026Q3','id':'1225000001','source_url':'https://static.cninfo.com.cn/example.pdf'}
            with patch.object(sources,'ROOT',root),patch.object(sources,'request',return_value=b'%PDF-original-sample'),patch.object(sources.os,'link',side_effect=OSError('write interrupted')):
                with self.assertRaises(OSError):sources.archive_pdf(doc)
            self.assertEqual(list(root.rglob('*.pdf')),[])
            self.assertEqual(list(root.rglob('.stock-download-*')),[])


if __name__=='__main__':unittest.main()
