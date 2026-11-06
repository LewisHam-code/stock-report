import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from stock_research import core,financial_recheck as repair,historical_reports


class FinancialRecheckTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        p=patch.object(core,'VAULT',self.root/'vault');p.start();self.addCleanup(p.stop)
        self.report={'id':'audit-report','document_id':'123','code':'000027','market':'SZ','period':'2025FY',
                     'conclusion':'原模型结论','financial_analysis':{'metrics':[
                         {'metric':'net_profit','scope':'cumulative','value':3342043.542,'standard_value':3342043.542,
                          'unit':'yuan','verified':True,'verification_method':'pdf_statement_column'}]}}
        self.fixed={'metrics':[{'metric':'net_profit','scope':'cumulative','value':2103342043.54,
                                'standard_value':2103342043.54,'verified':True}],
                    'data_quality':{'status':'complete','verified_count':13}}

    def test_corrections_do_not_overwrite_paid_outputs_and_stale_narrative_is_flagged(self):
        original=copy.deepcopy(self.report)
        with patch.object(repair,'enrich_extraction',return_value=self.fixed),patch('stock_research.models.completion') as paid:
            repair.recheck(self.report,{})
        paid.assert_not_called();self.assertEqual(self.report,original)
        shown=repair.apply(self.report)
        self.assertEqual(shown['financial_analysis']['metrics'][0]['standard_value'],2103342043.54)
        self.assertEqual(shown['financial_recheck']['narrative_status'],'requires_review')
        self.assertEqual(shown['original_conclusion'],'原模型结论')
        self.assertEqual(shown['review_state'],'needs_review')
        self.assertNotIn('original_model_value',shown['financial_analysis']['metrics'][0])

    def test_obsolete_program_proofs_are_withdrawn_when_no_new_proof_exists(self):
        with patch('stock_research.financial_evidence.primary_metrics',return_value={'status':'original_unavailable','metrics':[]}):
            repair.recheck(self.report,{})
        metric=repair.apply(self.report)['financial_analysis']['metrics'][0]
        self.assertFalse(metric['verified']);self.assertIsNone(metric['standard_value'])

    def test_overlay_is_bound_to_exact_source_report(self):
        with patch.object(repair,'enrich_extraction',return_value=self.fixed):repair.recheck(self.report,{})
        changed={**self.report,'conclusion':'另一个原始版本'}
        self.assertEqual(repair.apply(changed),changed)
