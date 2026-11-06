import json,tempfile,unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from stock_research import core,tracking_market as market,sources

class TrackingMarketTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=core.Store(self.root/'fixture.sqlite');self.addCleanup(self.store.conn.close)
        for name,path in [('VAULT',self.root/'vault'),('SITE',self.root/'site')]:
            p=patch.object(core,name,path);p.start();self.addCleanup(p.stop)
        p=patch('stock_research.securities.price_enabled',return_value=True);p.start();self.addCleanup(p.stop)
        cfg={**core.config()};cfg.pop('schedule_policy',None)
        p=patch.object(core,'config',return_value=cfg);p.start();self.addCleanup(p.stop)
        self.at=datetime(2026,10,20,22,tzinfo=core.TZ)
        for n in (1,2):
            doc=dict(id='122500000'+str(n),code='600001',market='SH',period='2026Q3',company='示例',title='第三季度报告',published_at='2026-10-19',source_url='https://example.com/public.pdf')
            self.store.add_document(doc)
            payload={'id':'r'+str(n),'code':'600001','market':'SH','generated_at':'2026-10-20T21:00:00+08:00','rating':'buy','score':80,'quote_at_analysis':{'price':10}}
            self.store.execute('INSERT INTO reports VALUES(?,?,?,?,?)',(payload['id'],doc['id'],'v',core.dumps(payload),payload['generated_at']))
        self.quote={'price':11,'asof':'2026-10-20T15:00:00+08:00','fetched_at':self.at.isoformat(),'source':'https://quote.eastmoney.com/sh600001.html','cross_validation':{'status':'matched','fields':{'price':{'matches':True}}}}
        self.prices={'bars':[{'date':'2026-10-19','close':10},{'date':'2026-10-20','close':11}], 'adjustment':'none','cross_validation':{'status':'matched'}}
    def test_adaptive_refresh_honors_session_slots_and_final_capture_once(self):
        cfg={**core.config(),'schedule_policy':'adaptive-v1'}
        with patch.object(core,'config',return_value=cfg),patch.object(sources,'latest_quote',return_value=self.quote) as quote,patch.object(sources,'daily_bars',return_value=self.prices):
            for hour,minute in ((9,30),(9,59),(10,0),(12,0),(15,35),(23,0)):
                clock=self.at.replace(hour=hour,minute=minute)
                with patch.object(core,'now',return_value=clock):market.refresh(self.store,at=clock)
        self.assertEqual(quote.call_count,3)
        self.assertEqual(market.read(market.home()/'控制.json')['market_slot'],'2026-10-20:close')
    def test_hourly_pass_deduplicates_stocks_and_never_calls_models(self):
        original=self.store.rows('SELECT payload FROM reports')
        with patch.object(sources,'latest_quote',return_value=self.quote) as quote,patch.object(sources,'daily_bars',return_value=self.prices) as bars,patch('stock_research.models.completion') as model,patch.object(core,'now',return_value=self.at):
            first=market.refresh(self.store,at=self.at)
            second=market.refresh(self.store,at=self.at+timedelta(minutes=59))
        self.assertEqual(first['stocks'],1);self.assertEqual(second['last_started_at'],first['last_started_at'])
        quote.assert_called_once();bars.assert_called_once();model.assert_not_called()
        self.assertEqual(bars.call_args.kwargs['adjustment'],'none')
        self.assertEqual(self.store.rows('SELECT payload FROM reports'),original)
        record=market.read(market.record_path('SH','600001'))
        self.assertEqual(record['quote']['price'],11)
        self.assertEqual(market.read(core.SITE/'tracking-market.json')['stocks']['600001']['trend']['points'],[{'date':'2026-10-20','close':11}])
    def test_closed_market_is_rechecked_after_one_hour_and_trade_time_stays_frozen(self):
        with patch.object(sources,'latest_quote',return_value=self.quote) as quote,patch.object(sources,'daily_bars',return_value=self.prices),patch.object(core,'now',return_value=self.at):market.refresh(self.store,at=self.at)
        later=self.at+timedelta(hours=1)
        with patch.object(sources,'latest_quote',return_value=self.quote) as quote,patch.object(sources,'daily_bars',return_value=self.prices),patch.object(core,'now',return_value=later):market.refresh(self.store,at=later)
        quote.assert_called_once();record=market.read(market.record_path('SH','600001'))
        self.assertEqual(record['quote']['asof'],'2026-10-20T15:00:00+08:00');self.assertEqual(record['quote_checked_at'],later.isoformat())
    def test_quote_completion_delay_cannot_stretch_the_refresh_to_two_hours(self):
        core.atomic_json(market.home()/'控制.json',{'last_started_at':self.at.isoformat()})
        core.atomic_json(market.record_path('SH','600001'),{'quote':self.quote,'quote_checked_at':(self.at+timedelta(seconds=5)).isoformat()})
        later=self.at+timedelta(hours=1)
        with patch.object(sources,'latest_quote',return_value=self.quote) as quote,patch.object(sources,'daily_bars',return_value=self.prices),patch.object(core,'now',return_value=later):market.refresh(self.store,at=later)
        quote.assert_called_once()
    def test_older_hourly_record_cannot_replace_a_newer_analysis_snapshot(self):
        core.atomic_json(market.record_path('SH','600001'),{'quote':{**self.quote,'asof':'2026-10-20T14:00:00+08:00'}})
        snapshot={'600001':{'market':'SH','quote':{**self.quote,'price':12,'asof':'2026-10-20T15:00:00+08:00'}}}
        self.assertEqual(market.overlay(snapshot)['600001']['quote']['price'],12)
    def test_source_failure_preserves_last_price_and_stops_after_three_failures(self):
        core.atomic_json(market.record_path('SH','600001'),{'quote':self.quote,'quote_checked_at':'2026-10-20T08:00:00+08:00'})
        with patch.object(sources,'latest_quote',side_effect=core.ServiceError('quote_unavailable','fixture')) as quote,patch.object(sources,'daily_bars',return_value=self.prices):
            for n in range(4):
                at=self.at.replace(hour=18)+timedelta(hours=n)
                with patch.object(core,'now',return_value=at):market.refresh(self.store,at=at)
        self.assertEqual(quote.call_count,3)
        record=market.read(market.record_path('SH','600001'));self.assertEqual(record['quote']['price'],11)
        self.assertEqual(record['quote_checked_at'],'2026-10-20T08:00:00+08:00')
        self.assertEqual(record['errors']['quote'],'daily_failure_limit')
    def test_delisted_stocks_make_no_source_calls(self):
        with patch('stock_research.securities.price_enabled',return_value=False),patch.object(sources,'latest_quote') as quote,patch.object(sources,'daily_bars') as bars:
            result=market.refresh(self.store,at=self.at)
        self.assertEqual(result['stocks'],0);quote.assert_not_called();bars.assert_not_called()
    def test_series_filters_future_points_and_keeps_source_conflicts(self):
        value=market.chart_series({**self.prices,'bars':self.prices['bars']+[{'date':'2026-10-21','close':12}],
                                   'cross_validation':{'status':'conflict'}},'2026-10-20','2026-10-20')
        self.assertEqual(value['points'],[{'date':'2026-10-20','close':11}]);self.assertEqual(value['cross_validation']['status'],'conflict')
    def test_raw_closes_accumulate_without_replacing_adjusted_observation_data(self):
        earlier={**self.prices,'bars':[{'date':'2026-10-18','close':9}]}
        previous={'raw_prices':earlier,'prices':{'adjustment':'qfq','bars':[]}}
        item={'code':'600001','market':'SH','start':'2026-10-18'}
        with patch.object(sources,'latest_quote',return_value=self.quote),patch.object(sources,'daily_bars',return_value=self.prices):
            result=market._fetch(item,previous,self.at,set())
        self.assertEqual([b['date'] for b in result['raw_prices']['bars']],['2026-10-18','2026-10-19','2026-10-20'])
        self.assertEqual(result['prices'],previous['prices'])
        core.atomic_json(market.record_path('SH','600001'),result)
        snap={'600001':{'market':'SH','prices':previous['prices']}}
        overlay=market.overlay(snap)['600001']
        self.assertEqual(overlay['prices']['adjustment'],'qfq')
        self.assertEqual(overlay['chart_prices']['adjustment'],'none')
    def test_intraday_bar_is_not_persisted_as_a_final_close(self):
        with patch.object(sources,'latest_quote',return_value=self.quote),patch.object(sources,'daily_bars',return_value=self.prices):
            result=market._fetch({'code':'600001','market':'SH','start':'2026-10-19'},{},self.at.replace(hour=14),set())
        self.assertEqual(result['raw_prices']['bars'],[{'date':'2026-10-19','close':10}])
    def test_existing_local_service_runs_only_one_bounded_hourly_job(self):
        from stock_research.service import ResearchServer
        server=object.__new__(ResearchServer)
        with patch('stock_research.service.time.monotonic',return_value=10000),patch.object(core,'config',return_value={'automatic_enabled':True}),patch('stock_research.service.threading.Thread') as thread:
            server.service_actions();server.service_actions()
        thread.assert_called_once();thread.return_value.start.assert_called_once()

if __name__=='__main__':unittest.main()
