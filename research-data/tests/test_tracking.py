import unittest
import json
import pathlib
import tempfile
from datetime import date, datetime
from unittest.mock import Mock, patch

from stock_research.core import Store, TZ
from stock_research.tracking import build_tracking, capture_baselines, collect_tracking_prices, current_quotes, horizon_result


class TrackingTests(unittest.TestCase):
    def setUp(self):
        from stock_research import core
        cfg={**core.config()};cfg.pop('schedule_policy',None)
        policy=patch.object(core,'config',return_value=cfg);policy.start();self.addCleanup(policy.stop)
        # Synthetic 600001 is independent of the user's real, delisted record.
        catalog=patch('stock_research.securities.lookup',return_value=None);catalog.start();self.addCleanup(catalog.stop)
        self.base = {"report_id": "r1", "code": "600001", "company": "示例", "period": "2026Q3",
                     "generated_at": "2026-10-12T20:00:00+08:00", "price": 10, "price_asof": "2026-10-12T15:00:00+08:00",
                     "rating": "buy", "score": 83, "score_status": "calculated", "provenance": "on_time"}
        self.stock = {"adjustment": "qfq", "bars": [
            {"date": "2026-10-12", "open": 10, "close": 10},
            {"date": "2026-10-13", "open": 10, "close": 10.5},
            {"date": "2026-10-14", "open": 10.5, "close": 11},
            {"date": "2026-10-15", "open": 11, "close": 12}]}
        self.index = {"adjustment": "none", "bars": [
            {"date": "2026-10-13", "open": 100, "close": 100},
            {"date": "2026-10-14", "open": 100, "close": 101},
            {"date": "2026-10-15", "open": 101, "close": 102}]}

    def test_horizon_uses_next_session_open_and_waits_for_maturity(self):
        self.assertEqual(horizon_result(self.base, self.stock, self.index, 3, date(2026, 10, 14))["state"], "pending")
        result = horizon_result(self.base, self.stock, self.index, 3, date(2026, 10, 15))
        self.assertEqual((result["entry_date"], result["end_date"]), ("2026-10-13", "2026-10-15"))
        self.assertEqual(result["return_pct"], 20)
        self.assertEqual(result["benchmark_pct"], 2)
        self.assertTrue(result["aligned"])

    def test_nested_original_score_is_frozen_and_missing_metadata_repairs_are_explicit(self):
        report={'id':'r1','code':'600001','company':'示例','period':'2026Q3','rating':'buy',
                'generated_at':'2026-10-12T20:00:00+08:00','decision':{'scoring':{'score':83,'status':'calculated','coverage_pct':100,'version':'v2'}}}
        at='2026-10-12T20:05:00+08:00'
        base=capture_baselines([report],captured_at=at)
        self.assertEqual(base['r1']['score'],83);self.assertEqual(base['r1']['score_status'],'calculated')
        missing={'r1':{**base['r1'],'score':None,'score_status':None}}
        repaired=capture_baselines([report],missing,captured_at=at,repair_original_metadata=True)
        self.assertEqual(repaired['r1']['score'],83)
        self.assertEqual(repaired['r1']['provenance'],base['r1']['provenance'])
        report['decision']['scoring']['score']=10
        self.assertEqual(capture_baselines([report],repaired,repair_original_metadata=True)['r1']['score'],83)

    def test_missing_benchmark_and_backfilled_rating_do_not_pass_as_valid(self):
        no_benchmark = horizon_result(self.base, self.stock, {"bars": []}, 3, date(2026, 10, 15))
        self.assertEqual(no_benchmark["state"], "evaluated")
        self.assertIsNone(no_benchmark["relative_aligned"])
        self.base["provenance"] = "backfilled"
        result = horizon_result(self.base, self.stock, self.index, 3, date(2026, 10, 15))
        self.assertIsNone(result["aligned"])
        self.assertFalse(result["evaluable"])

    def test_suspension_does_not_extend_a_three_day_horizon(self):
        halted = {"adjustment": "qfq", "bars": [self.stock["bars"][1], self.stock["bars"][2],
                                               {"date": "2026-10-16", "open": 12, "close": 12}]}
        result = horizon_result(self.base, halted, self.index, 3, date(2026, 10, 15))
        self.assertEqual(result["state"], "data_gap")

    def test_baseline_is_frozen_but_latest_close_is_visible_before_next_session(self):
        report = {"id": "r1", "code": "600001", "company": "示例", "period": "2026Q3",
                  "generated_at": "2026-10-12T20:00:00+08:00", "rating": "buy", "score": 83,
                  "score_status": "calculated", "score_coverage": 100,
                  "quote_at_analysis": {"price": 10, "asof": "2026-10-12T15:00:00+08:00"}}
        old = capture_baselines([report], captured_at="2026-10-12T20:05:00+08:00")
        report["rating"] = "sell"
        report["score"] = 12
        kept = capture_baselines([report], old, captured_at="2026-10-15T20:00:00+08:00")
        self.assertEqual(kept["r1"]["rating"], "buy")
        snap = {"600001": {"quote": {"price": 10.5, "asof": "2026-10-12T15:00:00+08:00"}, "prices": self.stock}}
        item = build_tracking([report], snap, self.index, kept, "2026-10-15T20:00:00+08:00")["reports"][0]
        self.assertEqual(item["current_price"],10.5)
        self.assertFalse(item['current_after_interpretation'])
        self.assertEqual(item['horizons']['3']['state'],'evaluated')

    def test_initial_latest_price_falls_back_to_frozen_interpretation_price(self):
        item=build_tracking([{'id':'r1','code':'600001'}],{}, {},{'r1':self.base},'2026-10-12T22:00:00+08:00')['reports'][0]
        self.assertEqual(item['current_price'],10);self.assertEqual(item['current_change_pct'],0)
        self.assertTrue(item['current_is_initial']);self.assertFalse(item['current_after_interpretation'])
        self.assertEqual(item['horizons']['3']['state'],'pending')

    def test_older_snapshot_cannot_replace_the_initial_price(self):
        snap={'600001':{'quote':{'price':9,'asof':'2026-10-09T15:00:00+08:00'}}}
        item=build_tracking([{'id':'r1','code':'600001'}],snap,{}, {'r1':self.base},'2026-10-12T22:00:00+08:00')['reports'][0]
        self.assertEqual(item['current_price'],10);self.assertEqual(item['price'],10)

    def test_conflicting_quote_retains_verification_and_displayed_arithmetic_change(self):
        cross={'status':'conflict','fields':{'price':{'primary':27.02,'secondary':26.87,'matches':False}}}
        snap={'600001':{'quote':{'price':27.02,'asof':'2026-10-21T13:00:27+08:00','cross_validation':cross}}}
        base={**self.base,'price':27.37,'generated_at':'2026-10-21T07:18:28+08:00'}
        item=build_tracking([{'id':'r1','code':'600001'}],snap,{}, {'r1':base},'2026-10-21T13:05:00+08:00')['reports'][0]
        self.assertEqual(item['current_price'],27.02)
        self.assertEqual(item['current_change_pct'],-1.28)
        self.assertEqual(item['current_cross_validation'],cross)

    def test_breakdown_counts_only_original_formal_judgments(self):
        published = [{"id": "r1", "code": "600001"}, {"id": "r2", "code": "600002"}]
        other = {**self.base, "report_id": "r2", "code": "600002", "provenance": "backfilled"}
        snaps = {code: {"quote": {"price": 12, "asof": "2026-10-15T15:00:00+08:00"}, "prices": self.stock}
                 for code in ("600001", "600002")}
        result = build_tracking(published, snaps, self.index, {"r1": self.base, "r2": other}, "2026-10-15T20:00:00+08:00")
        group = result["summary"]["3"]
        self.assertEqual((group["mature"], group["evaluable"], group["aligned"]), (2, 1, 1))
        self.assertEqual(group["by_rating"][0]["count"], 1)

    def test_sparse_collection_deduplicates_due_reports_and_watched_close(self):
        from stock_research import sources, tracking
        at = datetime(2026, 10, 22, 17, tzinfo=TZ)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            store = Store(root / "test.sqlite")
            self.addCleanup(store.conn.close)
            for number in (1, 2):
                document = "122500000" + str(number)
                store.add_document(dict(id=document, code="600001", company="示例", market="SH", period="2026Q3",
                                        title="季度报告", published_at="2026-10-18", source_url="https://example.com/report.pdf"))
                report = {"id": "r" + str(number), "code": "600001", "generated_at": "2026-10-18T20:00:00+08:00"}
                store.execute("INSERT INTO reports VALUES(?,?,?,?,?)", (report["id"], document, "v1", json.dumps(report), report["generated_at"]))
            baselines = {"r1": {**self.base, "generated_at": "2026-10-18T20:00:00+08:00"},
                         "r2": {**self.base, "report_id": "r2", "generated_at": "2026-10-18T20:00:00+08:00"}}
            path = root / "baselines.json"
            path.write_text(json.dumps(baselines))
            index = {"bars": [{"date": d, "open": 100, "close": 101} for d in ("2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22")],
                     "source": "https://example.com/index", "adjustment": "none"}
            prices = {"bars": [{"date": d, "open": 10, "close": 11} for d in ("2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22")],
                      "source": "https://example.com/stock", "adjustment": "qfq"}
            calls = []
            def bars(code, market, count=640, minimum=60):
                calls.append(code)
                return index if code == "000300" else prices
            quote = {"price": 11, "asof": "2026-10-22T15:00:00+08:00", "source": "https://example.com/quote"}
            with patch.object(tracking, "BASELINES", path), patch.object(sources, "daily_bars", side_effect=bars), \
                 patch.object(sources, "latest_quote", return_value=quote) as quotes:
                first = collect_tracking_prices(store, [{"code": "600001", "market": "SH"}], at, root/'vault')
                second = collect_tracking_prices(store, [{"code": "600001", "market": "SH"}], at, root/'vault')
            self.assertEqual(first, {"outcomes": 2, "stock_series": 1, "watch_closes": 1, "benchmark_requests": 1})
            self.assertEqual(second, {"outcomes": 0, "stock_series": 0, "watch_closes": 0, "benchmark_requests": 0})
            self.assertEqual(calls, ["000300", "600001"])
            self.assertEqual(quotes.call_count, 1)
            self.assertEqual(len(store.rows("SELECT * FROM tracking_outcomes")), 2)
            self.assertEqual(len(store.rows("SELECT * FROM tracking_watch_closes")), 1)

    def test_on_demand_quotes_are_cached_and_are_not_model_calls(self):
        from stock_research import sources
        with tempfile.TemporaryDirectory() as directory:
            store = Store(pathlib.Path(directory) / "test.sqlite")
            self.addCleanup(store.conn.close)
            store.add_document(dict(id="1225000001", code="600001", company="示例", market="SH", period="2026Q3",
                                    title="季度报告", published_at="2026-10-18", source_url="https://example.com/report.pdf"))
            store.execute("INSERT INTO reports VALUES(?,?,?,?,?)", ("r1", "1225000001", "v1", "{}", "2026-10-18"))
            quote = {"price": 11, "asof": "2026-10-18T15:00:00+08:00", "source": "https://example.com/quote"}
            with patch.object(sources, "latest_quote", return_value=quote) as source:
                self.assertEqual(current_quotes(["600001"], store)["600001"], quote)
                self.assertEqual(current_quotes(["600001"], store)["600001"], quote)
            self.assertEqual(source.call_count, 1)

    def test_company_note_keeps_tracking_facts_and_can_rebuild_index(self):
        from stock_research import tracking_notes
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            original = Store(root / 'original.sqlite')
            self.addCleanup(original.conn.close)
            original.add_document(dict(id="1225000001", code="600001", company="示例", market="SH", period="2026Q3",
                                       title="季度报告", published_at="2026-10-18", source_url="https://example.com/report.pdf"))
            original.execute("INSERT INTO tracking_watch_closes VALUES(?,?,?,?,?)",
                             ("600001", "2026-10-22", 11.0, "https://example.com/quote", "2026-10-22T17:00:00+08:00"))
            original.execute("INSERT INTO tracking_outcomes VALUES(?,?,?,?)",
                             ("r1", 3, json.dumps({"state": "evaluated", "return_pct": 10.0}), "2026-10-22T17:00:00+08:00"))
            vault = root / 'vault'
            with patch.object(tracking_notes, 'config', return_value={'watchlist': []}):
                tracking_notes.sync(original, {'r1': self.base}, vault)
                note = vault / '30_公司/SH/600001/解读追踪-自动生成.md'
                self.assertIn('2026-10-22 | 11.0', note.read_text())
                note.write_text(note.read_text().replace('## 我的备注\n', '## 我的备注\n保留人工观察。\n'))
                tracking_notes.sync(original, {'r1': self.base}, vault)
                self.assertIn('保留人工观察。', note.read_text())
                rebuilt = Store(root / 'rebuilt.sqlite')
                self.addCleanup(rebuilt.conn.close)
                baseline_path = root / 'rebuilt-baselines.json'
                tracking_notes.restore(rebuilt, baseline_path, vault)
            self.assertEqual(len(rebuilt.rows('SELECT * FROM tracking_watch_closes')), 1)
            self.assertEqual(len(rebuilt.rows('SELECT * FROM tracking_outcomes')), 1)
            self.assertEqual(json.loads(baseline_path.read_text())['r1']['code'], '600001')

    def test_local_tracking_route_requests_only_named_codes(self):
        from stock_research import service, tracking
        handler = object.__new__(service.Handler)
        handler.path = '/api/tracking/prices?code=600001&code=600002'
        handler.send_json = Mock()
        with patch.object(tracking, 'current_quotes', return_value={'600001': {'price': 11}}) as quotes:
            handler.handle_get()
        quotes.assert_called_once_with(['600001', '600002'])
        self.assertTrue(handler.send_json.call_args.args[0]['ok'])

    def test_after_close_quote_expires_before_the_next_session(self):
        from stock_research import core, sources, tracking
        with tempfile.TemporaryDirectory() as directory:
            store = Store(pathlib.Path(directory) / 'test.sqlite')
            self.addCleanup(store.conn.close)
            store.add_document(dict(id='1225000001', code='600001', company='示例', market='SH', period='2026Q3',
                                    title='季度报告', published_at='2026-10-18', source_url='https://example.com/report.pdf'))
            store.execute('INSERT INTO reports VALUES(?,?,?,?,?)', ('r1', '1225000001', 'v1', '{}', '2026-10-18'))
            for hour, day, price in ((18, 19, 10), (10, 20, 11)):
                at = datetime(2026, 10, day, hour, tzinfo=TZ)
                quote = {'price': price, 'asof': at.isoformat()}
                with patch.object(core, 'now', return_value=at), patch.object(tracking, 'now', return_value=at), \
                     patch.object(sources, 'latest_quote', return_value=quote) as fetch:
                    self.assertEqual(current_quotes(['600001'], store)['600001']['price'], price)
                    self.assertEqual(current_quotes(['600001'], store)['600001']['price'], price)
                    self.assertEqual(fetch.call_count, 1)

    def test_same_day_quote_after_publication_can_be_displayed(self):
        base = {**self.base, 'generated_at': '2026-10-12T10:00:00+08:00'}
        snap = {'600001': {'quote': {'price': 11, 'asof': '2026-10-12T15:00:00+08:00'}}}
        result = build_tracking([{'id': 'r1', 'code': '600001'}], snap, {}, {'r1': base},
                                '2026-10-12T17:00:00+08:00', outcomes={})
        self.assertEqual(result['reports'][0]['current_price'], 11)
        self.assertEqual(result['reports'][0]['current_change_pct'], 10)

    def test_incomplete_series_is_fetched_once_per_day_not_once_per_run(self):
        from stock_research import core, sources, tracking
        at = datetime(2026, 10, 16, 17, tzinfo=TZ)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            store = Store(root/'test.sqlite')
            self.addCleanup(store.conn.close)
            store.add_document(dict(id='1225000001', code='600001', company='示例', market='SH', period='2026Q3',
                                    title='季度报告', published_at='2026-10-12', source_url='https://example.com/report.pdf'))
            store.execute('INSERT INTO reports VALUES(?,?,?,?,?)',
                          ('r1', '1225000001', 'v1', json.dumps({'id': 'r1', 'code': '600001'}), '2026-10-12'))
            path = root/'baselines.json'
            path.write_text(json.dumps({'r1': self.base}))
            incomplete = {**self.stock, 'bars': self.stock['bars'][:-1]}
            with patch.object(tracking, 'BASELINES', path), patch.object(core, 'now', return_value=at), \
                 patch.object(sources, 'daily_bars', side_effect=[self.index, incomplete]) as fetch:
                first = collect_tracking_prices(store, (), at, root/'vault')
                second = collect_tracking_prices(store, (), at, root/'vault')
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(first['stock_series'], 1)
            self.assertEqual(second['stock_series'], 0)
            self.assertEqual(store.rows('SELECT * FROM tracking_outcomes'), [])

    def test_failure_limit_resets_each_day_without_growing_settings_keys(self):
        from stock_research import tracking
        from stock_research.core import ServiceError
        with tempfile.TemporaryDirectory() as directory:
            store = Store(pathlib.Path(directory)/'test.sqlite')
            self.addCleanup(store.conn.close)
            failing = Mock(side_effect=ServiceError('source_network', 'unavailable'))
            for day in (19, 20):
                for _ in range(5):
                    tracking._attempt(store, 'one-source', failing, datetime(2026, 10, day, 17, tzinfo=TZ))
            self.assertEqual(failing.call_count, 6)
            self.assertEqual(len(store.rows('SELECT * FROM settings')), 1)

    def test_short_listing_history_is_usable_for_tracking(self):
        from stock_research import sources
        from stock_research.core import ServiceError
        raw = {'data': {'code':'600001','klines':['2026-10-12,10,11,12,9,100']}}
        with patch.object(sources, 'get_json', return_value=raw), \
             patch('stock_research.market_sources.ths_bars',side_effect=ServiceError('source_network','unavailable')):
            self.assertEqual(len(sources.daily_bars('600001', 'SH', minimum=1)['bars']), 1)
            with self.assertRaises(ServiceError):
                sources.daily_bars('600001', 'SH')


if __name__ == "__main__":
    unittest.main()
