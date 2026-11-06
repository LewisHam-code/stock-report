"""Provider identity, units, disclosure fallback and provenance regressions."""
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch, Mock

from stock_research import sources, disclosure_sources as disclosures, market_sources as market, information_sources as info
from stock_research.core import ServiceError, Store


class DisclosureSourceTests(unittest.TestCase):
    def setUp(self):
        p=patch('stock_research.securities.price_enabled',return_value=True);p.start();self.addCleanup(p.stop)
    def test_cninfo_market_is_filtered_by_plate(self):
        from urllib.parse import parse_qs
        for column, plate in (('bj','bj'),('szse','sz'),('sse','sh')):
            with patch.object(sources,'get_json',return_value={'announcements':[],'totalAnnouncement':0}) as get:
                sources.query_announcements('2026-10-16','2026-10-17',column)
                form=parse_qs(get.call_args.kwargs['data'].decode())
                self.assertEqual(form['plate'],[plate])

    def test_bse_current_page_shape_and_market_are_checked(self):
        row={'companyCd':'920394','companyName':'示例','disclosureTitle':'[定期报告]示例:2026年三季度报告',
             'publishDate':'2026-10-16','destFilePath':'/disclosure/2026/2026-10-16/report.pdf','xxfcbj':'2'}
        payload=[{'listInfo':{'content':[row],'totalElements':1,'size':20}}]
        with patch.object(sources,'request',return_value=json.dumps(payload).encode()) as get:
            result=disclosures.query_exchange('BJ','2026-10-16','2026-10-17',1)
            self.assertEqual(len(result['documents']),1)
            self.assertEqual(result['page_size'],20)
            self.assertIn('companyAnnouncement.do',get.call_args.args[0])
        row['xxfcbj']='0'
        with patch.object(sources,'request',return_value=json.dumps(payload).encode()):
            with self.assertRaises(ServiceError):disclosures.query_exchange('BJ','2026-10-16','2026-10-17',1)

    def test_allowlist_requires_https_and_rejects_credentials_ports_and_lookalikes(self):
        for host in disclosures.OFFICIAL_HOSTS:
            self.assertTrue(disclosures.official_pdf('https://'+host+'/disclosure/report.PDF'))
        for value in ('http://static.sse.com.cn/file.pdf','https://static.sse.com.cn.evil.test/file.pdf',
                      'https://user@www.bse.cn/file.pdf','https://www.bse.cn:8080/file.pdf','https://www.bse.cn/file.html'):
            self.assertFalse(disclosures.official_pdf(value))

    def test_exchange_identity_and_cninfo_duplicates_keep_original_records(self):
        record={'SECURITY_CODE':'600001','SECURITY_NAME':'示例','TITLE':'示例股份有限公司2026年第三季度报告',
                'SSEDATE':'2026-10-17','URL':'/disclosure/listedinfo/announcement/c/new/2026-10-17/report.pdf'}
        doc=disclosures.exchange_document(record,'SH')
        self.assertTrue(doc['id'].startswith('sse-'))
        self.assertEqual(doc['source_url'],'https://static.sse.com.cn'+record['URL'])
        with tempfile.TemporaryDirectory() as directory:
            store=Store(pathlib.Path(directory)/'db.sqlite');self.addCleanup(store.conn.close)
            old={**doc,'id':'1225000001','title':'2026年第三季度报告','source_url':'https://static.cninfo.com.cn/finalpage/2026-10-17/1225000001.PDF'}
            store.add_document(old)
            self.assertEqual(disclosures.add_disclosure(store,doc),0)
            self.assertEqual(store.rows('SELECT COUNT(*) n FROM documents')[0]['n'],1)
            self.assertEqual(store.rows('SELECT source_url FROM documents')[0]['source_url'],old['source_url'])
            self.assertEqual(store.setting('official-disclosure-1225000001')['source_url'],doc['source_url'])
            newer={**doc,'id':'sse-'+'a'*24,'title':doc['title']+'（更正后）'}
            self.assertEqual(disclosures.add_disclosure(store,newer),1)

    def test_official_failure_is_an_explicit_bounded_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(pathlib.Path(directory)/'db.sqlite');self.addCleanup(store.conn.close)
            with patch.object(disclosures,'query_exchange',side_effect=ServiceError('source_http_307','verification')) as primary, \
                 patch.object(disclosures,'query_cninfo',return_value={'documents':[],'total':0,'page_size':30}):
                for _ in range(2):
                    result=disclosures.query_disclosures(store,'BJ','2026-10-17','2026-10-17',1)
                    self.assertEqual(result['provider'],'cninfo')
                    self.assertEqual(result['primary_error'],'source_http_307')
            self.assertEqual(primary.call_count,1)

    def test_source_switch_restarts_cursor_instead_of_skipping_fallback_page_one(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(pathlib.Path(directory)/'db.sqlite');self.addCleanup(store.conn.close)
            day=sources.now().date().isoformat()
            store.set('scan_tasks_official_v1_SH',[{'day':day,'page':3,'provider':'sh'}])
            calls=[]
            def query(store,which,start,end,page):
                calls.append((which,page))
                return {'documents':[],'total':0,'page_size':30,'provider':'cninfo','primary_error':'source_network'}
            with patch.object(sources,'config',return_value={'initial_lookback_days':1,'scan_lookback_days':1,'pages_per_source_per_run':1}), \
                 patch.object(disclosures,'query_disclosures',side_effect=query),patch.object(sources.time,'sleep'):
                result=sources.collect(store)
            self.assertEqual(calls[0],('SH',1))
            self.assertNotIn(('SH',3),calls)
            self.assertEqual(calls.count(('SH',1)),1)
            self.assertEqual(result['sources'][0]['state'],'fallback')

    def test_pdf_redirect_is_checked_before_following(self):
        # Exercise the real redirect policy with a fake opener, without I/O.
        with patch.object(sources.urllib.request,'build_opener') as opener:
            def build(handler):
                with self.assertRaises(ServiceError):
                    handler.redirect_request(Mock(),None,302,'Found',{},'https://evil.test/file.pdf')
                return Mock(open=Mock(side_effect=ServiceError('test_stop','done')))
            opener.side_effect=build
            with self.assertRaises(ServiceError):
                sources.request('https://static.sse.com.cn/file.pdf',allowed_hosts=disclosures.OFFICIAL_HOSTS)

    def test_exchange_challenge_only_falls_back_to_exact_disclosure(self):
        doc={'id':'sse-'+'c'*24,'code':'600001','company':'示例','market':'SH','period':'2026Q3',
             'title':'示例2026年第三季度报告','published_at':'2026-10-16T00:00:00+08:00',
             'source_url':'https://static.sse.com.cn/file.pdf'}
        row={'announcementId':'1225000001','secCode':'600001','secName':'示例','pageColumn':'SHZB',
             'announcementTitle':'2026年第三季度报告','adjunctUrl':'finalpage/2026-10-16/1225000001.PDF',
             'announcementTime':1792080000000}
        with tempfile.TemporaryDirectory() as directory:
            store=Store(pathlib.Path(directory)/'db.sqlite');self.addCleanup(store.conn.close)
            with patch.object(sources,'request',side_effect=[b'<html>verification</html>',b'%PDF-1.4 matched']), \
                 patch.object(sources,'cninfo_identity',return_value='600001,fixture'), \
                 patch.object(sources,'query_announcements',return_value={'announcements':[row],'totalAnnouncement':1}) as query:
                self.assertTrue(sources.fetch_document_pdf(doc,store).startswith(b'%PDF-'))
                self.assertIn('cninfo',store.setting('download-source-'+doc['id'])['download_url'])
                self.assertEqual(query.call_args.kwargs,{'stock':'600001,fixture','keyword':''})
            row['secCode']='600002'
            with patch.object(sources,'request',return_value=b'<html>verification</html>'), \
                 patch.object(sources,'cninfo_identity',return_value='600001,fixture'), \
                 patch.object(sources,'query_announcements',return_value={'announcements':[row],'totalAnnouncement':1}):
                with self.assertRaises(ServiceError) as caught:sources.fetch_document_pdf(doc,store)
                self.assertEqual(caught.exception.code,'pdf_fallback_unconfirmed')

    def test_uncategorized_company_search_still_requires_exact_unique_copy(self):
        doc={'id':'sse-'+'d'*24,'code':'600001','company':'示例','market':'SH','period':'2026Q3',
             'title':'示例2026年第三季度报告','published_at':'2026-10-16T00:00:00+08:00',
             'source_url':'https://static.sse.com.cn/file.pdf'}
        row={'announcementId':'1225000002','secCode':'600001','secName':'示例','pageColumn':'SHZB',
             'announcementTitle':'2026年第三季度报告','adjunctUrl':'finalpage/2026-10-16/1225000002.PDF',
             'announcementTime':1792080000000}
        def search(*args,**kwargs):
            return {'announcements':[],'totalAnnouncement':0} if kwargs.get('stock') else {'announcements':[row],'totalAnnouncement':1}
        with patch.object(sources,'request',side_effect=[b'<html>verification</html>',b'%PDF-1.4 exact']), \
             patch.object(sources,'cninfo_identity',return_value='600001,fixture'),patch.object(sources,'query_announcements',side_effect=search) as query:
            self.assertTrue(sources.fetch_document_pdf(doc).startswith(b'%PDF-'))
            self.assertEqual(query.call_count,2);self.assertEqual(query.call_args.kwargs,{'keyword':'示例'})
        for change in ({'secCode':'600002'},{'announcementTitle':'2026年半年度报告'},{'announcementTitle':'2026年第三季度报告（更正后）'}):
            bad={**row,**change}
            with patch.object(sources,'request',return_value=b'<html>verification</html>'), \
                 patch.object(sources,'cninfo_identity',return_value='600001,fixture'), \
                 patch.object(sources,'query_announcements',side_effect=[{'announcements':[],'totalAnnouncement':0},{'announcements':[bad],'totalAnnouncement':1}]):
                with self.assertRaises(ServiceError) as caught:sources.fetch_document_pdf(doc)
            self.assertEqual(caught.exception.code,'pdf_fallback_unconfirmed')

        # A bounded preview cannot establish that the matching copy is unique.
        with patch.object(sources,'request',return_value=b'<html>verification</html>'), \
             patch.object(sources,'cninfo_identity',return_value='600001,fixture'), \
             patch.object(sources,'query_announcements',side_effect=[{'announcements':[],'totalAnnouncement':0}]+[{'announcements':[row],'totalAnnouncement':91}]*3):
            with self.assertRaises(ServiceError) as caught:sources.fetch_document_pdf(doc)
            self.assertEqual(caught.exception.code,'pdf_fallback_unconfirmed')

    def test_gzip_response_is_decoded_with_a_size_bound(self):
        import gzip
        from io import BytesIO
        payload=b'%PDF-1.4 example'
        response=BytesIO(gzip.compress(payload));response.headers={'Content-Encoding':'gzip'}
        with patch.object(sources.urllib.request,'urlopen',return_value=response):
            self.assertEqual(sources.request('https://static.sse.com.cn/file.pdf'),payload)
        response=BytesIO(gzip.compress(b'x'*1000));response.headers={'Content-Encoding':'gzip'}
        with patch.object(sources.urllib.request,'urlopen',return_value=response):
            with self.assertRaises(ServiceError):sources.request('https://static.sse.com.cn/file.pdf',limit=100)


class MarketSourceTests(unittest.TestCase):
    def setUp(self):
        market._FAILURES.clear()
        p=patch('stock_research.securities.price_enabled',return_value=True);p.start();self.addCleanup(p.stop)

    def test_index_identity_and_outage_cooldown(self):
        row={'items':{'5':'1B0300','10':'4170.61','updateTime':'2026-10-16 15:00'}}
        with patch.object(sources,'request',return_value=json.dumps(row).encode()) as get:
            self.assertEqual(market.ths_quote('000300','SH')['code'],'000300')
            self.assertIn('zs_1B0300',get.call_args.args[0])
        action=Mock(side_effect=ServiceError('source_network','unavailable'))
        for _ in range(2):
            with self.assertRaises(ServiceError):market.provider_call('test',action)
        self.assertEqual(action.call_count,1)

    def test_prior_year_missing_does_not_discard_new_listing_bars(self):
        with patch.object(sources,'request',side_effect=[b'callback({"data":"20261016,10,12,9,11,10000"})',ServiceError('source_http_404','none')]):
            self.assertEqual(len(market.ths_bars('600001','SH',100)['bars']),1)

    def test_eastmoney_units_and_pe_bases_are_not_interchanged(self):
        payload={'data':{'f57':'600001','f58':'示例','f43':12.34,'f60':12,'f86':1792135800,
                         'f116':12340000000,'f162':5.2,'f163':6.4,'f164':6.3,'f167':.43}}
        with patch.object(sources,'get_json',return_value=payload) as get:
            quote=market.eastmoney_quote('600001','SH')
        self.assertEqual(quote['price'],12.34)
        self.assertEqual(quote['market_cap'],12340000000)
        self.assertEqual((quote['pe_dynamic_source'],quote['pe_static_source'],quote['pe_ttm']),(5.2,6.4,6.3))
        self.assertEqual(get.call_args.args[1]['fltt'],2)

    def test_ths_uses_trading_time_not_the_web_clock(self):
        payload={'items':{'5':'600001','10':'12.34','name':'示例','3153':'6.3','592920':'.43',
                          'updateTime':'2026-10-16 15:30','time':'2026-10-18 22:00:00 北京时间'}}
        with patch.object(sources,'request',return_value=('callback('+json.dumps(payload)+')').encode()):
            quote=market.ths_quote('600001','SH')
        self.assertTrue(quote['asof'].startswith('2026-10-16'))
        self.assertEqual(quote['pe_ttm'],6.3)

    def test_cross_check_compares_the_same_time_and_does_not_average_conflicts(self):
        first={'price':10,'pe_ttm':12,'pb':2,'market_cap':100000,'asof':'2026-10-16T15:00:00+08:00','provider':'eastmoney','source':'https://example.com/a'}
        second={**first,'provider':'ths','price':11,'source':'https://example.com/b'}
        self.assertEqual(market.compare_quotes(first,second)['status'],'conflict')
        self.assertTrue(market.compare_quotes(first,{**second,'price':10.02})['fields']['price']['matches'])
        self.assertFalse(market.compare_quotes(first,{**second,'price':10.03})['fields']['price']['matches'])
        with patch.object(market,'eastmoney_quote',return_value=first),patch.object(market,'ths_quote',return_value=second),patch.object(market,'tencent_quote',side_effect=ServiceError('source_network','unavailable')):
            quote=market.latest_quote('600001','SH')
        self.assertEqual(quote['price'],10)
        self.assertEqual(quote['cross_validation']['status'],'conflict')
        self.assertEqual(market.compare_quotes(first,{**second,'asof':'2026-10-15T15:00:00+08:00'})['status'],'not_comparable')

    def test_third_source_only_runs_for_price_conflicts_and_can_select_either_source(self):
        first={'price':10,'pe_ttm':12,'asof':'2026-10-16T10:00:20+08:00','provider':'eastmoney','source':'https://example.com/a'}
        second={**first,'price':11,'provider':'ths','source':'https://example.com/b'}
        for third_price,winner in ((10,'eastmoney'),(11,'ths'),(12,'eastmoney')):
            third={**first,'price':third_price,'provider':'tencent','source':'https://gu.qq.com/sh600001/gp'}
            with patch.object(market,'eastmoney_quote',return_value=first),patch.object(market,'ths_quote',return_value=second),patch.object(market,'tencent_quote',return_value=third) as fetch:
                result=market.latest_quote('600001','SH')
            fetch.assert_called_once_with('600001','SH')
            self.assertEqual(result['provider'],winner)
            self.assertEqual(result['pe_ttm'],12)
            self.assertEqual(result['cross_validation']['price_resolution']['status'],'unresolved' if third_price==12 else 'resolved')
            self.assertEqual(set(result['source_quotes']),{'eastmoney','ths','tencent'})
            self.assertFalse(result['cross_validation']['fields']['price']['matches'])
        # Price agreement with another field conflicting needs no third quote.
        for other in ({**second,'price':10,'pe_ttm':50},{**second,'asof':'2026-10-15T10:00:20+08:00'}):
            with patch.object(market,'eastmoney_quote',return_value=first),patch.object(market,'ths_quote',return_value=other),patch.object(market,'tencent_quote') as fetch:
                market.latest_quote('600001','SH')
            fetch.assert_not_called()

    def test_third_source_requires_unique_price_agreement_and_comparable_time(self):
        first={'price':10,'asof':'2026-10-16T10:00:20+08:00','provider':'eastmoney','source':'https://example.com/a'}
        second={**first,'price':11,'provider':'ths'}
        third={**first,'price':11,'provider':'tencent'}
        for asof in ('2026-10-15T10:00:20+08:00','2026-10-16T10:05:00+08:00','2026-10-16T10:00:23+08:00'):
            winner,resolution=market.resolve_price_conflict(first,second,{**third,'asof':asof})
            self.assertEqual(winner,first)
            self.assertEqual(resolution['status'],'unresolved')
        _,resolution=market.resolve_price_conflict(first,{**second,'price':10.02},{**third,'price':10.01})
        self.assertEqual(resolution['status'],'unresolved')

    def test_minute_precision_and_old_ticks_are_not_price_errors(self):
        first={'price':10,'asof':'2026-10-16T10:00:20+08:00','provider':'eastmoney','source':'https://example.com/a'}
        second={**first,'price':11,'provider':'ths','asof':'2026-10-16T10:00:00+08:00','timestamp_precision':'minute'}
        result=market.compare_quotes(first,second)
        self.assertEqual(result['status'],'not_comparable')
        self.assertIsNone(result['fields']['price']['matches'])
        third={**second,'provider':'tencent','timestamp_precision':'second'}
        _,resolution=market.resolve_price_conflict(first,second,third)
        self.assertEqual(resolution['status'],'unresolved')
        self.assertFalse(resolution['comparisons']['ths']['comparable'])
        second={**first,'price':11,'provider':'ths','asof':'2026-10-16T10:00:17+08:00','timestamp_precision':'second'}
        self.assertEqual(market.compare_quotes(first,second)['status'],'not_comparable')

    def test_conflict_resamples_all_three_once_and_uses_the_new_round(self):
        first={'price':10,'asof':'2026-10-16T10:00:20+08:00','provider':'eastmoney','source':'https://example.com/a'}
        second={**first,'price':11,'provider':'ths','source':'https://example.com/b'}
        refreshed={**first,'price':12,'asof':'2026-10-16T10:00:25+08:00'}
        third={**refreshed,'provider':'tencent','source':'https://gu.qq.com/sh600001/gp'}
        with patch.object(market,'eastmoney_quote',side_effect=[first,refreshed]) as em,patch.object(market,'ths_quote',side_effect=[second,{**second,'asof':refreshed['asof']}]) as ths,patch.object(market,'tencent_quote',return_value=third) as tx:
            result=market.latest_quote('600001','SH')
        self.assertEqual((em.call_count,ths.call_count,tx.call_count),(2,2,1))
        self.assertEqual(result['price'],12)
        self.assertEqual(result['cross_validation']['price_resolution']['status'],'resolved')
        self.assertTrue(result['cross_validation']['price_resolution']['resampled_concurrently'])
        self.assertEqual(result['source_quotes']['eastmoney']['price'],12)

    def test_freshest_second_level_quote_is_preferred_within_same_session(self):
        first={'price':10,'asof':'2026-10-16T10:00:20+08:00','provider':'eastmoney','source':'https://example.com/a'}
        second={**first,'price':10,'provider':'ths','asof':'2026-10-16T10:01:00+08:00','timestamp_precision':'minute'}
        with patch.object(market,'eastmoney_quote',return_value=first),patch.object(market,'ths_quote',return_value=second),patch.object(market,'tencent_quote') as tx:
            result=market.latest_quote('600001','SH')
        self.assertEqual(result['provider'],'ths')
        self.assertEqual(result['cross_validation']['status'],'not_comparable')
        tx.assert_not_called()

    def test_tencent_parser_checks_identity_price_time_and_north_exchange(self):
        fields=['']*33
        fields[1:5]=['同惠电子','920509','22.76','23.58']
        fields[30:33]=['20261016133209','-0.82','-3.48']
        raw=('v_bj920509="'+'~'.join(fields)+'";').encode('gb18030')
        with patch.object(sources,'request',return_value=raw) as fetch:
            quote=market.tencent_quote('920509','BJ')
        self.assertEqual((quote['price'],quote['previous_close']),(22.76,23.58))
        self.assertEqual(quote['asof'],'2026-10-16T13:32:09+08:00')
        self.assertEqual(fetch.call_args.kwargs['timeout'],6)
        for payload in (raw.replace(b'920509',b'920508'),raw.replace(b'20261016133209',b'badtime'),raw.replace(b'22.76',b'0')):
            with patch.object(sources,'request',return_value=payload):
                with self.assertRaises(ServiceError):market.tencent_quote('920509','BJ')

    def test_primary_failure_uses_ths_with_explicit_provenance(self):
        quote={'price':10,'asof':'2026-10-16T15:00:00+08:00','provider':'ths','source':'https://stockpage.10jqka.com.cn/600001/'}
        with patch.object(market,'eastmoney_quote',side_effect=ServiceError('source_network','failed')),patch.object(market,'ths_quote',return_value=quote):
            result=market.latest_quote('600001','SH')
        self.assertEqual(result['provider'],'ths')
        self.assertEqual(result['cross_validation']['status'],'unavailable')
        self.assertEqual(result['source_errors'][0]['provider'],'eastmoney')

    def test_daily_bar_order_and_volume_units_agree(self):
        em={'data':{'code':'600001','klines':['2026-10-16,10,11,12,9,100,100000']}}
        ths='callback('+json.dumps({'data':'20261016,10,12,9,11,10000,100000'})+')'
        with patch.object(sources,'get_json',return_value=em),patch.object(sources,'request',return_value=ths.encode()):
            first=market.eastmoney_bars('600001','SH',1)
            second=market.ths_bars('600001','SH',1)
        self.assertEqual(first['bars'],second['bars'])
        self.assertEqual(market.compare_bars(first,second)['status'],'matched')
        second['bars'][0]['close']=10
        self.assertEqual(market.compare_bars(first,second)['status'],'conflict')

    def test_malformed_jsonp_and_wrong_code_do_not_become_valid_quotes(self):
        with self.assertRaises(ServiceError):disclosures.json_payload('<script>cookie=1</script>')
        with patch.object(sources,'get_json',return_value={'data':{'f57':'600002','f43':12}}):
            with self.assertRaises(ServiceError):market.eastmoney_quote('600001','SH')

    def test_chart_raw_bars_use_unadjusted_provider_modes(self):
        em={'data':{'code':'600001','klines':['2026-10-16,10,11,12,9,100']}}
        ths=b'callback({"data":"20261016,10,12,9,11,10000"})'
        with patch.object(sources,'get_json',return_value=em) as get:
            first=market.eastmoney_bars('600001','SH',1,'none')
        self.assertEqual(get.call_args.args[1]['fqt'],0)
        with patch.object(sources,'request',return_value=ths) as get:
            second=market.ths_bars('600001','SH',1,'none')
        self.assertIn('/00/',get.call_args.args[0]);self.assertEqual(first['adjustment'],'none')
        self.assertEqual(market.compare_bars(first,second)['status'],'matched')
        self.assertEqual(market.compare_bars(first,{**second,'adjustment':'qfq'})['status'],'not_comparable')


class InformationSourceTests(unittest.TestCase):
    def test_ths_hydration_is_parsed_as_data_and_relative_dates_remain_unknown(self):
        chunk='0:{"initialUpperState":'+json.dumps({'items':[{'title':'公司公告','jumpUrl':'https://news.10jqka.com.cn/20261017/c1.shtml','timeLabel':'昨天','source':'同花顺'}]},ensure_ascii=False)+'}'
        page='<script>self.__next_f.push('+json.dumps([1,chunk],ensure_ascii=False)+')</script>'
        with patch.object(sources,'request',return_value=page.encode()):events=info.ths_news('600001')
        self.assertEqual(len(events),1)
        self.assertIsNone(events[0]['published_at'])
        self.assertEqual(events[0]['verification_status'],'source_metadata_only')

    def test_company_news_avoids_share_count_false_matches(self):
        payload={'result':{'cmsArticleWebOld':[{'title':'其他公司回购600001股','url':'https://finance.eastmoney.com/a/1.html'},
                                              {'title':'示例公司发布公告','url':'http://finance.eastmoney.com/a/2.html','date':'2026-10-16'}]}}
        with patch.object(sources,'request',return_value=json.dumps(payload).encode()):events=info.eastmoney_news('600001','示例公司')
        self.assertEqual(len(events),1)
        self.assertTrue(events[0]['source_url'].startswith('https://'))


if __name__=='__main__':unittest.main()
