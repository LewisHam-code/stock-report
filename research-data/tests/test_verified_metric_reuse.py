import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stock_research import core, analysis, financial_evidence as evidence, models


def proof():
    return {'status': 'checked','contract':'financial-facts-v1','version': evidence.VERSION, 'conflicts': [],
            'checks': [{'passed': True}], 'metrics': [
                {'metric': name, 'scope': scope, 'period_end': '2025-12-31', 'value': 100,
                 'standard_value':100,'unit':'yuan','fact_id':'fixture-'+name,'evidence':{'source_sha256':'fixture'},'source_page':8, 'quote': name + '100', 'verified': True}
                for name, scope in evidence.CORE_FIELDS.items()]}


class MetricReuseTests(unittest.TestCase):
    def test_unknown_optional_claim_is_preserved_without_inventing_a_page(self):
        from stock_research.model_inputs import relocate_unlocated_claims
        data = {'management_claims': [{'claim': '未来投产', 'source_page': None, 'quote': '预计投产', 'verification_needed': '核对时间'}], 'uncertainties': [], 'metrics': [{'value': 123}]}
        before = copy.deepcopy(data);result, changes = relocate_unlocated_claims(data)
        self.assertEqual(data, before)
        self.assertEqual(result['management_claims'], [])
        self.assertEqual(result['metrics'], data['metrics'])
        self.assertIn('未来投产', result['uncertainties'][0])
        self.assertIn('预计投产', result['uncertainties'][0])
        self.assertIn('未核实', result['uncertainties'][0])
        self.assertEqual(len(changes), 1)
        data['management_claims'][0]['source_page'] = 'unknown'
        result, changes = relocate_unlocated_claims(data)
        self.assertEqual(result, data)
        self.assertEqual(changes, [])

    def test_complete_current_proof_can_be_reused_without_mutation(self):
        data = proof();before = copy.deepcopy(data)
        self.assertTrue(evidence.reusable_core_proof(data, '2025FY'))
        self.assertEqual(data, before)

    def test_incomplete_conflicting_wrong_period_and_unlocated_proof_fall_back(self):
        changes = [lambda p: p['metrics'].pop(),
                   lambda p: p['metrics'].append(copy.deepcopy(p['metrics'][0])),
                   lambda p: p['metrics'][0].update(verified=False),
                   lambda p: p['metrics'][0].update(period_end='2024-12-31'),
                   lambda p: p['metrics'][0].update(standard_value=None),
                   lambda p: p['metrics'][0].update(source_page=0),
                   lambda p: p['metrics'][0].update(quote=''),
                   lambda p: p.update(conflicts=[{'metric': 'revenue'}]),
                   lambda p: p.update(checks=[{'passed': False}]),
                   lambda p: p.update(version='obsolete')]
        for change in changes:
            with self.subTest(change=change):
                data = proof();change(data)
                self.assertFalse(evidence.reusable_core_proof(data, '2025FY'))

    def test_empty_model_metrics_are_replaced_by_original_pdf_evidence(self):
        narrative = {key: '' for key in ('conclusion', 'evidence', 'valuation', 'risks', 'valuation_context')}
        narrative.update(industry_outlook='unknown', industry_quotes=[], topics=[], catalysts=[], falsification=[], scenarios=[])
        extracted = {'industry': 'fixture', 'company_business': '', 'metrics': [], 'management_claims': [],
                     'sections': [], 'risks': [], 'audit_opinion': '', 'uncertainties': []}
        cfg = {**core.config(), 'reuse_verified_pdf_metrics': True, '_benchmark_fresh_extraction': True}
        doc = {'id': 'fixture', 'sha256': 'fixture', 'company': 'fixture', 'code': '600000', 'market': 'SH',
               'period': '2025FY', 'published_at': core.stamp(), 'source_url': 'https://static.cninfo.com.cn/fixture.pdf'}
        calls = []
        def complete(*args, **kwargs):
            calls.append(args)
            return copy.deepcopy(extracted if args[4] == 'financial_extraction' else narrative), {'provider': 'deepseek', 'model': 'fixture', 'test_mode': False}
        with tempfile.TemporaryDirectory() as directory:
            store = core.Store(Path(directory) / 'test.sqlite');self.addCleanup(store.conn.close)
            with patch.object(evidence, 'primary_metrics', return_value=proof()), \
                 patch.object(analysis, 'excerpt', return_value=('fixture', {'full_text': True})), \
                 patch.object(analysis, 'verify_extraction', side_effect=lambda d,v,c:v), \
                 patch.object(analysis, 'completion', side_effect=complete):
                result = analysis.analyze(store, doc, {'_fixed_industry_documents': {'documents': [], 'errors': []}}, settings=cfg)
        self.assertTrue(result['input_optimization']['reused_pdf_metrics'])
        self.assertEqual(result['financial_analysis']['data_quality']['verified_count'], 13)
        self.assertIn('metrics必须返回空数组', calls[0][0])
        self.assertEqual(len(calls), 2)
        self.assertEqual({m['metric'] for m in result['financial_analysis']['metrics']}, set(evidence.CORE_FIELDS))
        payload = json.loads(calls[1][1])
        self.assertTrue(all(m['verified'] for m in payload['financial']['metrics']))

    def test_initial_thinking_override_cannot_disable_deep_report_reasoning(self):
        cfg = {**core.config(), 'provider': 'deepseek', 'model': 'deepseek-flash', 'single_model': True,
               'initial_report_thinking': 'disabled', 'prefer_off_peak': False}
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}
        for purpose, disabled in [('financial_extraction', True), ('comprehensive_report', True), ('deep_report', False)]:
            with patch.object(models, 'api_key', return_value='fixture-only'), \
                 patch.object(models, 'model_open', return_value=io.StringIO(json.dumps(response))) as net:
                models.completion('', '', schema, purpose=purpose, settings=cfg)
            body = json.loads(net.call_args.args[0].data)
            self.assertEqual(body.get('thinking') == {'type': 'disabled'}, disabled)
            if not disabled:
                self.assertEqual(body['reasoning_effort'], 'low')

    def test_real_response_path_records_narrow_repair_and_keeps_metric_validation(self):
        extraction = {'industry': '', 'company_business': '', 'metrics': [], 'sections': [], 'risks': [],
                      'audit_opinion': '', 'uncertainties': [], 'management_claims': [
                          {'claim': '未来投产', 'source_page': None, 'quote': '预计投产', 'verification_needed': '核对时间'}]}
        cfg = {**core.config(), 'provider': 'deepseek', 'model': 'deepseek-flash', 'single_model': True, 'prefer_off_peak': False}
        def call(value):
            response = {'usage': {'prompt_tokens': 1, 'completion_tokens': 1}, 'choices': [
                {'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]}
            with patch.object(models, 'api_key', return_value='fixture-only'), \
                 patch.object(models, 'model_open', return_value=io.StringIO(json.dumps(response))):
                return models.completion('', '', analysis.EXTRACTION, purpose='financial_extraction', settings=cfg)
        result, meta = call(extraction)
        self.assertEqual(len(meta['local_normalizations']), 1)
        self.assertIn('未核实', result['uncertainties'][0])
        self.assertIsNone(extraction['management_claims'][0]['source_page'])
        extraction['metrics'] = [{'metric': 'revenue', 'value': 'not-a-number'}]
        with self.assertRaises(core.ServiceError):
            call(extraction)

    def test_live_extraction_page_range_uses_same_audited_parser_as_saved_response(self):
        extraction={'industry':'','company_business':'','metrics':[],'management_claims':[],'sections':[{'title':'业务','analysis':'范围7-9原样保留','source_pages':[7,8,9]}], 'risks':[],'audit_opinion':'','uncertainties':[]}
        raw=json.dumps(extraction,ensure_ascii=False).replace('[7, 8, 9]','[7-9]')
        response={'usage':{'prompt_tokens':1,'completion_tokens':1},'choices':[{'finish_reason':'stop','message':{'content':raw}}]}
        cfg={**core.config(),'provider':'deepseek','model':'deepseek-flash','single_model':True,'prefer_off_peak':False}
        with patch.object(models,'api_key',return_value='fixture-only'), patch.object(models,'model_open',return_value=io.StringIO(json.dumps(response))) as net:
            result,meta=models.completion('','',analysis.EXTRACTION,purpose='financial_extraction',settings=cfg)
        self.assertEqual(net.call_count,1)
        self.assertEqual(result['sections'][0]['source_pages'],[7,8,9])
        self.assertEqual(result['sections'][0]['analysis'],'范围7-9原样保留')
        self.assertIn('source_pages范围展开为逐页列表',meta['local_normalizations'])


if __name__ == '__main__':
    unittest.main()
