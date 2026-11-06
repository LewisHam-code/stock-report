import copy,hashlib,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from stock_research import core,publish


class LegacyProfileProofTests(unittest.TestCase):
    def test_old_body_must_match_exact_sha_before_live_profile_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(publish,'VAULT',Path(tmp)/'vault'),patch.object(publish,'SITE',Path(tmp)/'site'):
            at='2026-10-28T06:50:46+08:00'
            original={'id':'old-r','document_id':'old-d','code':'600001','market':'SH','period':'2026Q3','version':'v1','generated_at':'2026-10-27T22:00:00+08:00'}
            doc={'id':'old-d','sha256':'source','period':'2026Q3'}
            report={**original,'scoring':{'score':20,'asof':at},'score':20,'decision':{},'financial_analysis':{'metrics':[{'value':123}]},'assessment_asof':at,
                    'research_profile':{'original_profile':{'score':10},'repaired_legacy_score':{'score':20,'asof':at},'current':{'score':30,'asof':at},'operating_catalysts':{'asof':at}}}
            entry={'source_hash':hashlib.sha256(core.dumps(original).encode()).hexdigest(),'document_signature':publish._document_signature(doc),'detail_hash':hashlib.sha256(core.dumps(report).encode()).hexdigest()}
            saved=copy.deepcopy(report['research_profile'])
            for key in ('current','operating_catalysts','repaired_legacy_score'):saved[key]['asof']='2026-10-27T18:00:00+08:00'
            directory=publish.VAULT/'30_公司/SH/600001/六维研究/old-r'
            core.atomic_json(directory/'更新-checkpoint.json',saved)
            live=copy.deepcopy(report);live['research_profile']['current']={'score':45,'asof':'2026-10-28T15:00:00+08:00'}
            target=publish.SITE/'report-details/old-r.json';core.atomic_json(target,live)
            self.assertIsNotNone(publish._reuse_published(original,entry,doc))
            for field in ('score','financial_analysis','research_profile'):
                tampered=copy.deepcopy(live)
                if field=='score':tampered[field]=99
                elif field=='financial_analysis':tampered[field]['metrics'][0]['value']=999
                else:tampered[field]['original_profile']['score']=99
                core.atomic_json(target,tampered)
                self.assertIsNone(publish._reuse_published(original,entry,doc),field)
            core.atomic_json(target,live)
            self.assertIsNone(publish._reuse_published(original,{**entry,'detail_hash':'no_matching_history'},doc))
