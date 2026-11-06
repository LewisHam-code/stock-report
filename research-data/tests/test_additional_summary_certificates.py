import json,unittest
from pathlib import Path
from types import SimpleNamespace
from stock_research import financial_facts as facts
from stock_research.pdf_fact_sources import SourceIndex
from test_statement_evidence import OriginalPage

class AdditionalSummaryTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.fixtures=json.loads((Path(__file__).parent/'fixtures/additional_summary_certificates.json').read_text())
 def index(self,company):
  f=next(x for x in self.fixtures if x['company']==company);blank={'width':595,'height':842,'lines':[],'tables':[]}
  pages=[OriginalPage(blank) for _ in range(max(p['number'] for p in f['pages']))];records=[]
  for p in f['pages']:
   pages[p['number']-1]=OriginalPage(p);records.append({'page':p['number'],'text':'\n'.join(l['text'] for l in p['lines'])})
  return SourceIndex(SimpleNamespace(pages=pages),f,records)
 def candidate(self,name,value,page,period='2025FY',scope='cumulative'):
  return dict(metric=name,value=value,source_page=page,period_end=period[:4]+('-12-31' if period.endswith('FY') else '-09-30'),scope=scope,unit='percentage' if name=='roe' else 'yuan')
 def test_borderless_dated_columns_and_centered_wrapped_labels(self):
  source=self.index('中国移动')
  for name,value,page,unit,standard in [('adjusted_profit',984,2,'hundred_million_yuan',98400000000),('eps',4.9,3,'yuan',4.9),('roe',7.5,3,'percentage',7.5)]:
   c={**self.candidate(name,value,page,'2026Q3'),'unit':unit};m,error=source.certify(c)
   self.assertIsNone(error,(name,error));self.assertEqual(m['standard_value'],standard)
  self.assertIsNone(source.certify(self.candidate('eps',1.26,3,'2026Q3'))[0])
  self.assertIsNone(source.certify(self.candidate('eps',5.34,3,'2026Q3'))[0])
 def test_net_profit_margin_cannot_be_certified_as_profit_money(self):
  c={**self.candidate('net_profit',13.6,2,'2026Q3'),'unit':'hundred_million_yuan'}
  self.assertIsNone(self.index('中国移动').certify(c)[0])
 def test_glued_current_and_prior_amounts_are_split_only_on_printed_columns(self):
  source=self.index('陕国投Ａ');m,error=source.certify(self.candidate('adjusted_profit',1429527642.06,9))
  self.assertIsNone(error);self.assertEqual(m['standard_value'],1429527642.06)
  self.assertIsNone(source.certify(self.candidate('adjusted_profit',1245365225.57,9))[0])
  m,error=self.index('藏格矿业').certify(self.candidate('adjusted_profit',4031206848.14,11))
  self.assertIsNone(error)
 def test_units_can_continue_after_an_already_complete_metric_label(self):
  for company,name,value,page in [('红棉股份','eps',.0436,7),('供销大集','adjusted_profit',-913179998.77,7)]:
   m,error=self.index(company).certify(self.candidate(name,value,page))
   self.assertIsNone(error,(company,error));self.assertEqual(m['evidence']['unit']['page'],8)
 def test_wrapped_nonrecurring_profit_names_are_not_mistaken_for_net_profit(self):
  for company,value in [('古井贡酒',3489463594.30),('京粮控股',-279148762.48)]:
   m,error=self.index(company).certify(self.candidate('adjusted_profit',value,7))
   self.assertIsNone(error,(company,error));self.assertEqual(m['standard_value'],value)
 def test_unit_split_across_pages_keeps_explicit_currency_and_share_evidence(self):
  m,error=self.index('海南海药').certify(self.candidate('eps',-.3314,7))
  self.assertIsNone(error);self.assertEqual(m['standard_value'],-.3314)
  self.assertEqual(m['evidence']['unit']['kind'],'cross_page_unit')
  self.assertEqual([f['page'] for f in m['evidence']['unit']['fragments']],[7,8])
 def test_reverse_weighted_roe_alias_and_eps_with_table_yuan_declaration(self):
  source=self.index('万科A')
  for name,value in [('roe',-55.42),('eps',-7.4453)]:
   m,error=source.certify(self.candidate(name,value,11))
   self.assertIsNone(error,(name,error));self.assertEqual(m['standard_value'],value)
  self.assertIsNone(source.certify(self.candidate('roe',-75.75,11))[0])
 def test_explicit_not_applicable_is_not_a_missing_value_or_zero(self):
  source=self.index('深康佳Ａ');issues=source.not_applicable([7,8])
  roe=[i for i in issues if i['metric']=='roe' and i['scope']=='cumulative']
  self.assertEqual(len(roe),1);self.assertEqual(roe[0]['source_cell'],'不适用')
  data=facts.reconcile({}, {'metrics':[]},{'metrics':[],'status':'checked','statement_issues':issues})
  gap=next(i for i in data['data_quality']['missing'] if i['metric']=='roe')
  self.assertEqual(gap['status'],'source_not_applicable');self.assertFalse(data['metrics'])
 def test_wrapped_adjusted_roe_cannot_lose_its_qualifier_in_baseline_fallback(self):
  for company,ordinary,adjusted in [('同惠电子',5.87,5.70),('民士达',2.41,2.05)]:
   source=self.index(company)
   candidate=self.candidate('roe',ordinary,3,'2026Q3','single_quarter')
   m,error=source.certify(candidate)
   self.assertIsNone(error)
   self.assertIsNone(m['evidence']['label']['continued_page'])
   self.assertNotIn('合并资产负债表',m['evidence']['label']['text'])
   self.assertIsNone(source.certify({**candidate,'value':adjusted})[0])
 def test_annual_quarter_breakdown_never_inherits_a_prior_annual_header(self):
  source=self.index('金徽股份')
  for name,annual,quarter in [('net_profit',542265974.26,159296554.37),('adjusted_profit',520202397.59,144115571.62)]:
   self.assertIsNotNone(source.certify(self.candidate(name,annual,8))[0])
   self.assertIsNone(source.certify(self.candidate(name,quarter,9))[0])
  m,error=source.certify(self.candidate('roe',16.45,9))
  self.assertIsNone(error);self.assertNotIn('扣除非经常性损益',m['evidence']['label']['text'])
  self.assertIsNone(source.certify(self.candidate('roe',15.88,9))[0])
 def test_per_share_section_unit_overrides_the_general_million_yuan_header(self):
  m,error=self.index('平安银行').certify(self.candidate('eps',2.07,18))
  self.assertIsNone(error);self.assertEqual(m['standard_value'],2.07)
  self.assertIn('每股比率',m['evidence']['unit']['text'])

if __name__=='__main__':unittest.main()
