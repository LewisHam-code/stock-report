"""Contract regressions: wrong values/periods/entities never enter trusted facts."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from stock_research import core,financial_facts as facts,financial_evidence as evidence
from stock_research.pdf_fact_sources import SourceIndex
from test_statement_evidence import OriginalPage

class CertificateTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.fixture=json.loads((Path(__file__).parent/'fixtures/annual_audit_layouts.json').read_text())[0]
 def index(self):
  f=self.fixture;empty={'width':595,'height':842,'lines':[],'tables':[]};pages=[OriginalPage(empty) for _ in range(145)];records=[]
  for p in f['summary']+f['pages']:
   pages[p['number']-1]=OriginalPage(p)
   records.append({'page':p['number'],'text':'\n'.join(l['text'] for l in p['lines'])})
  records.sort(key=lambda p:p['page'])
  return SourceIndex(SimpleNamespace(pages=pages),{'period':'2025FY','sha256':'source'},records)
 def candidate(self):
  return {'metric':'net_profit','scope':'cumulative','unit':'yuan','period_end':'2025-12-31',
          'source_page':140,'value':2103342043.54,'quote':'候选引文'}
 def test_correct_value_has_independent_coordinates_for_value_label_period_unit_entity(self):
  checked,error=self.index().certify(self.candidate())
  self.assertIsNone(error);self.assertTrue(checked['verified'])
  for key in ('value','label','period','unit','entity_evidence'):
   self.assertTrue(checked['evidence'][key]['bbox']);self.assertTrue(checked['evidence'][key]['text'])
 def test_truncated_or_previous_year_values_are_rejected_even_with_correct_label_and_flag(self):
  for value in (3342043.542,2005148708.38):
   checked,_=self.index().certify({**self.candidate(),'value':value,'verified':True})
   self.assertIsNone(checked,value)
 def test_wrong_unit_period_or_metric_is_not_repaired_by_guessing(self):
  for change in ({'unit':'ten_thousand_yuan'},{'period_end':'2024-12-31'},{'metric':'adjusted_profit'}):
   checked,_=self.index().certify({**self.candidate(),**change})
   self.assertIsNone(checked,change)
 def test_pure_quote_presence_never_proves_consolidated_fact(self):
  from stock_research import analysis
  # The public verification function records a text match only; the separate
  # physical certificate is mandatory before any trusted fact can be emitted.
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);(root/'text.json').write_text(json.dumps({'pages':[{'page':1,'text':'营业收入 100 元'}]}))
   metric={'metric':'revenue','scope':'cumulative','unit':'yuan','period_end':'2025-12-31','source_page':1,'value':100,'quote':'营业收入 100 元'}
   raw={'industry':'','company_business':'','metrics':[metric],'management_claims':[],'sections':[],'risks':[],'audit_opinion':'','uncertainties':[]}
   with patch.object(analysis,'ROOT',root):result=analysis.verify_extraction({'text_file':'text.json','period':'2025FY'},raw,{})
   self.assertTrue(result['metrics'][0]['text_matched']);self.assertFalse(result['metrics'][0]['verified'])
 def test_split_header_and_body_require_identical_physical_column_edges(self):
  p=json.loads((Path(__file__).parent/'fixtures/split_balance_header.json').read_text())
  empty={'width':595,'height':842,'lines':[],'tables':[]}
  pages=[OriginalPage(empty) for _ in range(92)]+[OriginalPage(p)]
  source=SourceIndex(SimpleNamespace(pages=pages),{'period':'2025FY','sha256':'source'},[{'page':93,'text':'合并资产负债表'}])
  candidate={'metric':'cash','scope':'instant','unit':'yuan','period_end':'2025-12-31','source_page':93,'value':271334661.91}
  checked,error=source.certify(candidate)
  self.assertIsNone(error);self.assertEqual(checked['standard_value'],271334661.91)
  self.assertIsNone(source.certify({**candidate,'value':563606433.44})[0])

class FactContractTests(unittest.TestCase):
 def proof(self,profile='general'):
  metrics=[dict(metric=k,scope=v,unit='percentage' if k in ('roe','gross_margin') else 'yuan',value=100,standard_value=100,
                period_end='2025-12-31',verified=True,fact_id='fact-'+k,evidence={'source_sha256':'source'}) for k,v in evidence.CORE_FIELDS.items()]
  return {'metrics':metrics,'contract':facts.CONTRACT,'profile':profile,'status':'checked','conflicts':[],'checks':[]}
 def test_model_flags_cannot_replace_missing_source_proof_and_original_is_immutable(self):
  original={'metrics':[{'metric':'revenue','scope':'cumulative','value':999,'standard_value':999,'verified':True,'unit':'yuan'}]};before=copy.deepcopy(original)
  result=facts.reconcile({},original,{'metrics':[],'status':'checked'})
  self.assertEqual(original,before);self.assertEqual(facts.trusted_metrics(result),[])
  self.assertFalse(result['metrics'][0]['verified']);self.assertIsNone(result['metrics'][0]['standard_value'])
  with self.assertRaises(core.ServiceError):facts.require_ready(result)
 def test_conflicts_or_failed_checks_stop_model_analysis_and_rating_inputs(self):
  for change in ({'conflicts':[{'metric':'revenue'}]},{'checks':[{'name':'勾稽','passed':False}]},{'status':'original_unavailable'}):
   result=facts.reconcile({}, {'metrics':[]},{**self.proof(),**change})
   self.assertFalse(result['data_quality']['analysis_ready']);self.assertFalse(facts.trusted_metrics(result))
   with self.assertRaises(core.ServiceError):facts.require_ready(result)
 def test_known_but_unproven_summary_value_blocks_another_paid_interpretation(self):
  proof=self.proof();proof['metrics']=[m for m in proof['metrics'] if m['metric']!='eps']
  proof['candidates']=[{'metric':'eps','scope':'cumulative','value':.35,'source_page':2}]
  result=facts.reconcile({}, {'metrics':[]},proof)
  self.assertEqual(result['data_quality']['summary_unresolved'],['eps'])
  self.assertFalse(result['data_quality']['analysis_ready'])
  with self.assertRaises(core.ServiceError):facts.require_ready(result)
  self.assertTrue(facts.certified_metrics(result))
 def test_bank_applicability_is_explicit_and_not_filled_as_zero(self):
  proof=self.proof('bank');proof['metrics']=[m for m in proof['metrics'] if m['metric'] not in facts.PROFILE_EXCLUSIONS['bank']]
  result=facts.reconcile({}, {'metrics':[]},proof);q=result['data_quality']
  self.assertEqual(q['expected_count'],9);self.assertEqual(q['verified_count'],9)
  self.assertEqual(len(q['not_applicable']),4);self.assertIn('专用行业指标',q['industry_note'])
  self.assertFalse(any(m['metric']=='inventory' for m in result['metrics']))
 def test_same_named_operating_revenue_is_used_instead_of_total_income(self):
  def row(name,value,label,kind):
   return dict(metric=name,scope='cumulative',unit='yuan',value=value,standard_value=value,verified=True,source_page=7,
               quote=label,evidence={'statement':kind,'label':{'text':label}})
  candidates=[row('revenue',100,'营业收入','summary'),row('revenue',101,'营业总收入','income'),row('operating_revenue',100,'营业收入','income')]
  proof=facts.resolve_candidates({'sha256':'source','period':'2025FY'},candidates,[],[])
  self.assertFalse(proof['conflicts']);self.assertEqual(next(m for m in proof['metrics'] if m['metric']=='revenue')['standard_value'],100)
 def test_changed_pdf_cannot_use_old_cache_claim_even_if_metadata_is_unchanged(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);(root/'sample.pdf').write_bytes(b'changed PDF')
   with patch.object(core,'ROOT',root):
    result=facts.document_facts(None,{'pdf':'sample.pdf','sha256':'0'*64,'id':'id','period':'2025FY'})
   self.assertEqual(result['status'],'source_identity_mismatch');self.assertFalse(result['metrics'])
