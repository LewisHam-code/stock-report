"""Original exchange PDF geometry: YTD/quarter, wrapped amounts and page breaks."""
import json,unittest
from pathlib import Path
from types import SimpleNamespace
from stock_research import financial_facts as facts,market_metrics,core
from stock_research.pdf_fact_sources import SourceIndex
from test_statement_evidence import OriginalPage

class QuarterlyCertificateTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.fixtures=json.loads((Path(__file__).parent/'fixtures/quarterly_summary_certificates.json').read_text())
 def index(self,code):
  f=next(x for x in self.fixtures if x['code']==code);blank={'width':595,'height':842,'lines':[],'tables':[]}
  pages=[OriginalPage(blank) for _ in range(max(p['number'] for p in f['pages']))];records=[]
  for p in f['pages']:
   pages[p['number']-1]=OriginalPage(p);records.append({'page':p['number'],'text':'\n'.join(l['text'] for l in p['lines'])})
  return SourceIndex(SimpleNamespace(pages=pages),f,records)
 def candidate(self,name,value,page=2,scope='cumulative',unit=None):
  return dict(metric=name,scope=scope,value=value,source_page=page,period_end='2026-09-30',unit=unit or ('percentage' if name=='roe' else 'yuan'))
 def test_later_ytd_column_uses_only_the_indicator_cell(self):
  source=self.index('300610')
  for name,value in [('adjusted_profit',50690977.08),('eps',.34),('roe',6.0)]:
   proved,error=source.certify(self.candidate(name,value))
   self.assertIsNone(error,(name,error));self.assertEqual(proved['standard_value'],value)
   self.assertNotIn('9,104,273.24',proved['evidence']['label']['text'])
 def test_quarter_values_cannot_be_certified_as_year_to_date(self):
  source=self.index('300610')
  for name,value in [('adjusted_profit',9104273.24),('eps',.08),('roe',1.29)]:
   self.assertIsNone(source.certify(self.candidate(name,value))[0])
   self.assertIsNotNone(source.certify(self.candidate(name,value,scope='single_quarter'))[0])
 def test_bse_heading_and_adjusted_roe_are_distinguished(self):
  source=self.index('920247')
  for name,value in [('adjusted_profit',35255150.75),('eps',.19),('roe',7.54)]:
   self.assertIsNotNone(source.certify(self.candidate(name,value,page=3))[0],name)
  self.assertIsNone(source.certify(self.candidate('roe',7.22,page=3))[0])
  self.assertIsNone(source.certify(self.candidate('eps',.13,page=3))[0])
 def test_summary_continuation_and_explanation_growth_are_not_confused(self):
  source=self.index('603588')
  self.assertIsNotNone(source.certify(self.candidate('roe',11.698))[0])
  self.assertIsNotNone(source.certify(self.candidate('eps',.8))[0])
  self.assertIsNone(source.certify(self.candidate('eps',88.68,page=3,scope='single_quarter'))[0])
 def test_wrapped_amounts_and_basic_eps_label_across_pages(self):
  source=self.index('002911')
  for name,value in [('revenue',19544963509.65),('adjusted_profit',543553682.95),('eps',.35)]:
   proved,error=source.certify(self.candidate(name,value,page=1))
   self.assertIsNone(error,(name,error));self.assertEqual(proved['standard_value'],value)
   if name=='eps':self.assertEqual(proved['evidence']['unit']['page'],2)
  # The tail of the multi-line number is not another certified fact.
  self.assertIsNone(source.certify(self.candidate('revenue',6315.36,page=1,scope='single_quarter'))[0])
 def test_missing_ruling_and_slash_baseline_still_preserve_the_unit_and_row(self):
  self.assertIsNotNone(self.index('300507').certify(self.candidate('eps',.05))[0])
  source=self.index('603132')
  for name,value,page in [('adjusted_profit',636562712.1,1),('eps',.66,2),('roe',19.2,2)]:
   proved,error=source.certify(self.candidate(name,value,page=page))
   self.assertIsNone(error,(name,error));self.assertEqual(proved['standard_value'],value)
  proved,_=source.certify(self.candidate('roe',19.2))
  self.assertEqual(proved['evidence']['unit']['bbox'],proved['evidence']['label']['bbox'])
 def test_printed_thousand_yuan_unit_is_converted_and_never_applied_to_eps(self):
  source=self.index('601068')
  candidate=self.candidate('adjusted_profit',71484,page=1,unit='thousand_yuan')
  proved,error=source.certify(candidate)
  self.assertIsNone(error);self.assertEqual(proved['standard_value'],71484000)
  self.assertEqual(proved['evidence']['unit']['unit'],'thousand_yuan')
  self.assertIsNone(source.certify({**candidate,'unit':'yuan'})[0])
  proved,error=source.certify(self.candidate('eps',.0154))
  self.assertIsNone(error);self.assertEqual(proved['standard_value'],.0154)
 def test_eps_unit_is_proven_even_if_only_the_closing_parenthesis_crosses_a_border(self):
  source=self.index('600066');proved,error=source.certify(self.candidate('eps',1.44,page=1))
  self.assertIsNone(error);self.assertEqual(proved['standard_value'],1.44)
  self.assertIn('元/股',proved['evidence']['unit']['text'])
 def test_wrong_percentage_unit_for_eps_is_rejected(self):
  self.assertIsNone(self.index('300610').certify(self.candidate('eps',.34,unit='percentage'))[0])
 def test_proven_eps_remains_visible_when_an_unrelated_metric_blocks_analysis(self):
  data={'fact_contract':facts.CONTRACT,'data_quality':{'analysis_ready':False},'metrics':[
   {'metric':'eps','scope':'cumulative','verified':True,'fact_id':'certified','standard_value':.35,'period_end':'2026-09-30'}]}
  self.assertEqual(facts.trusted_metrics(data),[])
  self.assertEqual(market_metrics.build_market_snapshot({'price':10},data)['eps'],.35)

if __name__=='__main__':unittest.main()
