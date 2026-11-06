import copy,json,unittest
from pathlib import Path

from stock_research.financial_growth import certify
from stock_research.model_inputs import financial_view,financial_table_view


class PrintedGrowthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data=json.loads((Path(__file__).parent/'fixtures/gaishi_summary_growth.json').read_text())

    def metric(self,name,scope):return next(m for m in self.data['metrics'] if m['metric']==name and m['scope']==scope)

    def test_ytd_and_quarter_growth_bind_to_their_own_physical_tables(self):
        for name,scope,expected in [('revenue','cumulative',21.08),('revenue','single_quarter',6.12),('adjusted_profit','cumulative',-21.66),('adjusted_profit','single_quarter',-31.74)]:
            proof=certify(self.metric(name,scope),self.data['tables'],self.data['sha256'])
            self.assertEqual(proof['yoy_pct'],expected);self.assertTrue(proof['yoy_verified'])

    def test_unverified_amount_wrong_scope_and_different_pdf_are_rejected(self):
        m=self.metric('revenue','single_quarter')
        self.assertIsNone(certify({**m,'verified':False},self.data['tables'],self.data['sha256']))
        self.assertIsNone(certify({**m,'scope':'cumulative'},self.data['tables'],self.data['sha256']))
        self.assertIsNone(certify(m,self.data['tables'],'0'*64))

    def test_bad_printed_arithmetic_or_nonpositive_prior_not_promoted(self):
        m=self.metric('revenue','single_quarter')
        tables=copy.deepcopy(self.data['tables']);tables[2]['rows'][2][7]['text']='99.00%'
        self.assertIsNone(certify(m,tables,self.data['sha256']))
        tables=copy.deepcopy(self.data['tables']);tables[2]['rows'][2][4]['text']='-179,561,140.16'
        self.assertIsNone(certify(m,tables,self.data['sha256']))

    def test_wrong_comparison_header_and_other_table_cannot_substitute(self):
        m=self.metric('revenue','single_quarter');tables=copy.deepcopy(self.data['tables'])
        tables[2]['rows'][0][8]['text']='本报告期比上季度';tables[2]['rows'][0][5]['text']='本年上季度'
        self.assertIsNone(certify(m,tables,self.data['sha256']))
        self.assertIsNone(certify(m,tables[:2],self.data['sha256']))

    def test_proof_coordinates_do_not_inflate_either_model_input_mode(self):
        m=self.metric('revenue','single_quarter');proof=certify(m,self.data['tables'],self.data['sha256'])
        from stock_research.financial_facts import CONTRACT
        data={'fact_contract':CONTRACT,'metrics':[{**m,**proof}],'summary_growth_check':{'state':'checked'}}
        regular=financial_view(data)
        self.assertEqual(regular['metrics'][0]['yoy_pct'],6.12)
        for view in (regular,financial_table_view(data)):
            self.assertNotIn('yoy_evidence',json.dumps(view));self.assertNotIn('summary_growth_check',view)


if __name__=='__main__':unittest.main()
