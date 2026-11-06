import unittest
from unittest.mock import patch
from stock_research import research_sectors as sectors,research_profile

class IndustryMarketTests(unittest.TestCase):
    def test_accepts_current_object_rows_and_rejects_missing_total(self):
        rows,n=sectors._rows({'data':{'total':1,'diff':{'0':{'f12':'BK0420'}}}})
        self.assertEqual(n,1);self.assertEqual(rows,[{'f12':'BK0420'}])
        from stock_research.core import ServiceError
        with self.assertRaises(ServiceError):sectors._rows({'data':{'diff':[]}})

    def test_exact_same_provider_classification_only_not_concept_guess(self):
        source={'sources':[{'provider':'eastmoney','classifications':[{'taxonomy':'eastmoney_2016','path':['电子','半导体']}]}]}
        self.assertEqual(sectors.industry_id(source,{'半导体':'BK0001'}),('BK0001','半导体'))
        self.assertEqual(sectors.industry_id(source,{'半导体设备':'BK0002'}),(None,None))
        source['sources'][0]['provider']='ths'
        self.assertEqual(sectors.industry_id(source,{'半导体':'BK0001'}),(None,None))

    def test_shared_sector_fetched_once_and_membership_checked(self):
        stock={'industry':{'sources':[{'provider':'eastmoney','classifications':[{'taxonomy':'eastmoney_2016','path':['半导体']}]}]}}
        source={'members':['600001'],'prices':{'bars':[]},'return_20d_pct':1}
        with patch.object(sectors,'catalog',return_value={'半导体':'BK0001'}),patch.object(sectors,'sector',return_value=source) as fetch:
            enriched,errors=sectors.enrich(None,{'600001':stock,'600002':stock},{})
        self.assertEqual(fetch.call_count,1)
        self.assertTrue(enriched['600001']['sector_context']['membership_verified'])
        self.assertFalse(enriched['600002']['sector_context']['membership_verified'])
        self.assertEqual(errors,{})

    def test_old_intraday_bar_does_not_become_final_when_clock_moves(self):
        value={'fetched_at':'2026-10-21T10:00:00+08:00','bars':[{'date':'2026-10-21','close':10}]}
        self.assertEqual(research_profile.completed_bars(value,'2026-10-21T16:00:00+08:00'),[])

    def test_turnover_unit_check_does_not_guess_wrong_scale(self):
        from stock_research.market_sources import _checked_amount
        bar={'volume':100,'low':9,'high':11}
        _checked_amount(bar,100000,100)
        self.assertEqual(bar['amount'],100000)
        bad={'volume':100,'low':9,'high':11}
        _checked_amount(bad,10,100)
        self.assertNotIn('amount',bad)

    def test_unavailable_sector_preserves_snapshots_without_fake_neutral(self):
        from stock_research.core import ServiceError
        stocks={'600001':{}}
        with patch.object(sectors,'catalog',side_effect=ServiceError('source_network','test')):
            result,errors=sectors.enrich(None,stocks,{})
        self.assertEqual(result,stocks);self.assertEqual(errors,{'catalog':'source_network'})

if __name__=='__main__':unittest.main()
