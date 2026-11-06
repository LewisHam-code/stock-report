import unittest
from unittest.mock import Mock,patch
import json
from stock_research import industry_sources as industry
from stock_research.core import ServiceError


class IndustryTests(unittest.TestCase):
    def test_eastmoney_preserves_two_distinct_classifications_and_identity(self):
        data={'jbzl':[{'SECUCODE':'600519.SH','SECURITY_CODE':'600519','EM2016':'食品饮料-饮料-白酒','INDUSTRYCSRC1':'制造业-酒、饮料和精制茶制造业'}]}
        source=industry.eastmoney_parse(data,'600519','SH')
        self.assertEqual(source['classifications'][0]['path'],['食品饮料','饮料','白酒'])
        self.assertNotEqual(source['classifications'][0]['taxonomy'],source['classifications'][1]['taxonomy'])
        with self.assertRaises(ServiceError):industry.eastmoney_parse(data,'600000','SH')

    def test_gbk_page_extracts_industry_without_confusing_script_text(self):
        page='<meta charset=gbk><h1>贵州茅台 600519</h1><table><td>所属申万行业：</td><td>食品饮料 — 白酒Ⅱ</td></table><script>所属申万行业：错误分类</script>'
        source=industry.ths_parse(page.encode('gbk'),'600519')
        self.assertEqual(source['classifications'],[{'taxonomy':'shenwan_display','path':['食品饮料','白酒Ⅱ']}])
        with self.assertRaises(ServiceError):industry.ths_parse(page.encode('gbk'),'600000')

    def test_different_taxonomies_never_become_verified_peer_inputs(self):
        rows=[{'provider':'eastmoney','classifications':[{'taxonomy':'eastmoney_2016','path':['食品饮料','饮料','白酒']}]},
              {'provider':'ths','classifications':[{'taxonomy':'shenwan_display','path':['食品饮料','白酒Ⅱ']}]}]
        result=industry.compare(rows)
        self.assertEqual(result['status'],'cross_taxonomy_consistent')
        self.assertFalse(result['verified_for_peers'])

    def test_matching_or_conflicting_common_taxonomy_is_explicit(self):
        first={'provider':'eastmoney','classifications':[{'taxonomy':'common','path':['一级','二级']}]}
        second={'provider':'ths','classifications':[{'taxonomy':'common','path':['一级','二级']}]}
        self.assertTrue(industry.compare([first,second])['verified_for_peers'])
        second['classifications'][0]['path']=['其他']
        self.assertEqual(industry.compare([first,second])['status'],'conflict')
        self.assertFalse(industry.compare([first])['verified_for_peers'])

    def test_refresh_prioritizes_watchlist_bounds_requests_and_preserves_snapshot_time(self):
        store=Mock()
        def rows(sql,args=()):
            if 'JOIN reports' in sql:return [{'code':'600000','market':'SH'}]
            return [{'asof':'2026-10-16','payload':json.dumps({'quote':{'price':10}})}]
        store.rows.side_effect=rows
        store.cached.return_value=None
        data={'observed_at':'2026-10-19','sources':[{'provider':'eastmoney'},{'provider':'ths'}],'errors':[]}
        with patch.object(industry,'fetch',return_value=data) as fetch:
            result=industry.refresh_researched(store,[{'code':'600519','market':'SH'}],limit=1)
            fetch.assert_called_once_with(store,'600519','SH',None)
        self.assertEqual(result,{'updated':1,'with_two_sources':1,'errors':0})
        args=store.execute.call_args.args[1]
        self.assertEqual(args[:2],('600519','2026-10-16'))
        self.assertEqual(json.loads(args[2])['quote']['price'],10)
        store.reserve_api_call.assert_not_called()
        store.reset_mock();store.cached.return_value=data
        with patch.object(industry,'fetch') as fetch:
            self.assertEqual(industry.refresh_researched(store,limit=1)['updated'],0)
            fetch.assert_not_called()
