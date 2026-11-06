import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from stock_research import core,analysis,model_inputs,response_recovery as recovery


class ResponseRecoveryTests(unittest.TestCase):
    def data(self):
        return {'industry':'示例行业','company_business':'公开业务','metrics':[],
                'management_claims':[],'sections':[],'risks':[],'audit_opinion':'未披露','uncertainties':[]}

    def test_only_page_ranges_expand_and_control_characters_remain_text(self):
        text='{"sections":[{"source_pages":[1, 7-9],"analysis":"页码7-9不应改写"}]}'
        value,changes=recovery.parse(text)
        self.assertEqual(value['sections'][0]['source_pages'],[1,7,8,9])
        self.assertEqual(value['sections'][0]['analysis'],'页码7-9不应改写')
        self.assertTrue(changes)
        value,changes=recovery.parse('{"note":"第一行\n第二行"}')
        self.assertEqual(value['note'],'第一行\n第二行')
        for text in ('{"source_pages":[9-7]}','{"value":7-9}','{"value":123,}'):
            with self.assertRaises(ValueError):recovery.parse(text)

    def test_unlocated_values_are_preserved_as_unverified_not_given_fake_pages(self):
        value=self.data();metric={'metric':'gross_margin','value':30.62,'unit':'percentage','scope':'cumulative',
                                'period_end':'2026-09-30','source_page':None,'quote':'待原文定位'}
        value['metrics']=[metric];before=copy.deepcopy(value)
        normalized,changes=model_inputs.relocate_unlocated_claims(value)
        self.assertEqual(value,before);self.assertEqual(normalized['metrics'],[])
        self.assertIn('30.62',normalized['uncertainties'][0]);self.assertIn('不参与评分',normalized['uncertainties'][0])
        analysis.validate(normalized,analysis.EXTRACTION);self.assertEqual(len(changes),1)
        value['metrics'][0]['unit']='unrecognized'
        normalized,_=model_inputs.relocate_unlocated_claims(value)
        with self.assertRaises(core.ServiceError):analysis.validate(normalized,analysis.EXTRACTION)

    def test_saved_result_requires_unchanged_source_and_complete_pro_response(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);directory=root/'vault'/'responses';directory.mkdir(parents=True)
            pdf=root/'source.pdf';pdf.write_bytes(b'fixture-source');os.utime(pdf,(1,1))
            doc={'pdf':'source.pdf','sha256':hashlib.sha256(pdf.read_bytes()).hexdigest()}
            response=directory/'financial_extraction-fixture.json'
            raw={'model':'deepseek-v4-pro','choices':[{'finish_reason':'stop','message':{'content':json.dumps(self.data())}}],'usage':{'prompt_tokens':10,'completion_tokens':20}}
            response.write_text(json.dumps(raw));original=response.read_bytes()
            with patch.object(core,'ROOT',root),patch.object(core,'VAULT',root/'vault'),patch('stock_research.models.model_open') as network:
                result=recovery.extraction(doc,directory)
                self.assertEqual(result[0],self.data());self.assertEqual(result[1]['usage'],raw['usage'])
                self.assertEqual(result[1]['saved_response_recovery']['new_extraction_model_calls'],0)
                self.assertEqual(response.read_bytes(),original);network.assert_not_called()
                self.assertIsNone(recovery.extraction({**doc,'sha256':'changed'},directory))
                raw['choices'][0]['finish_reason']='length';response.write_text(json.dumps(raw))
                self.assertIsNone(recovery.extraction(doc,directory))

    def test_null_metric_with_unknown_metadata_is_missing_not_a_reason_to_rebuy(self):
        value=self.data()
        metric={'metric':'gross_margin','value':None,'unit':None,'scope':None,'period_end':None,'source_page':None,'quote':None}
        value['metrics']=[metric];before=copy.deepcopy(value)
        normalized,changes=model_inputs.relocate_unlocated_claims(value)
        self.assertEqual(value,before);self.assertEqual(normalized['metrics'],[])
        self.assertIn('不参与评分',normalized['uncertainties'][0]);self.assertIn('null',normalized['uncertainties'][0])
        analysis.validate(normalized,analysis.EXTRACTION);self.assertEqual(len(changes),1)
        for update in ({'value':12}, {'unit':'invented_unit'}, {'note':'extra unsupported field'}):
            value['metrics']=[{**metric,**update}]
            rejected,_=model_inputs.relocate_unlocated_claims(value)
            with self.assertRaises(core.ServiceError):analysis.validate(rejected,analysis.EXTRACTION)

    def test_partial_missing_metric_keeps_known_strings_and_shared_schema_unchanged(self):
        schema_before=copy.deepcopy(analysis.METRIC)
        for missing in ({'scope':None}, {'source_page':None,'quote':None}, {'unit':None}):
            value=self.data();value['metrics']=[{'metric':'gross_margin','value':None,'unit':'percentage',
                'scope':'cumulative','period_end':'2026-09-30','source_page':2,'quote':'未单列毛利率',**missing}]
            normalized,changes=model_inputs.relocate_unlocated_claims(value)
            self.assertEqual(normalized['metrics'],[]);self.assertEqual(len(changes),1)
            self.assertIn('2026-09-30',normalized['uncertainties'][0])
            analysis.validate(normalized,analysis.EXTRACTION)
        self.assertEqual(analysis.METRIC,schema_before)


if __name__=='__main__':unittest.main()
