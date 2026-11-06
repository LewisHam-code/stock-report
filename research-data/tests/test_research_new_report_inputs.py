import json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch

from stock_research import research_shadow as shadow


class NewReportInputTests(unittest.TestCase):
    def test_market_refresh_enriches_new_company_without_relabelling_it_pilot(self):
        pilot={'companies':[{'code':'600001','market':'SH','company':'试点'}]}
        reports=[{'id':'old','code':'600001','market':'SH','company':'试点','document_id':'d1'},
                 {'id':'new','code':'920826','market':'BJ','company':'新公司','document_id':'d2',
                  'research_at_generation':{'capture_kind':'at_generation','version':shadow.VERSION}}]
        store=Mock();store.cached.return_value={'bars':[{'date':'2026-10-21','close':10}],'fetched_at':'2026-10-21T16:00:00+08:00'}
        def rows(sql,args=()):
            if sql.startswith('SELECT payload FROM reports'):return [{'payload':json.dumps(r)} for r in reports]
            if sql.startswith('SELECT code,payload FROM snapshots'):return [{'code':r['code'],'payload':json.dumps({'code':r['code'],'market':r['market']})} for r in reports]
            if 'JOIN documents' in sql:return [{'payload':json.dumps(r),'id':r['document_id'],'code':r['code']} for r in reports]
            if sql.startswith('SELECT * FROM documents'):return [{'id':args[0]}]
            return []
        store.rows.side_effect=rows
        with tempfile.TemporaryDirectory() as temporary:
            vault=Path(temporary)/'vault';site=Path(temporary)/'site';(site/'report-details').mkdir(parents=True)
            shadow.pilot_path(vault).parent.mkdir(parents=True);shadow.pilot_path(vault).write_text(json.dumps(pilot))
            for r in reports:(site/'report-details'/(r['id']+'.json')).write_text(json.dumps(r))
            with patch('stock_research.core.SITE',site),patch('stock_research.core.stamp',return_value='2026-10-21T16:00:00+08:00'),\
                 patch('stock_research.research_inputs.refresh_prices',return_value=[]) as prices,\
                 patch('stock_research.research_sectors.enrich',side_effect=lambda s,items,b:(items,{})) as sectors,\
                 patch('stock_research.valuation_inputs.verified_cashflow',return_value={}),\
                 patch.object(shadow,'attach',return_value={}) as attach,patch.object(shadow,'audit'):
                shadow.refresh_after_market(store,vault=vault)
                self.assertEqual({r['code'] for r in prices.call_args.args[0]['companies']},{'600001','920826'})
                self.assertIn('920826',sectors.call_args.args[1])
                for call in attach.call_args_list:self.assertEqual(call.args[4],{'600001'})


if __name__=='__main__':unittest.main()
