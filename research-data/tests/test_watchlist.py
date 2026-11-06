import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_research import core, pipeline, sources, watchlist, securities
from stock_research.core import ServiceError, Store


class WatchlistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = self.root / "automation.json"
        self.settings.write_text(json.dumps({"download_per_run": 1, "analysis_per_run": 1}))
        self.core_patch = patch.object(core, "ROOT", self.root)
        self.watch_patch = patch.object(watchlist, "ROOT", self.root)
        self.core_patch.start()
        self.vault_patch=patch.object(core,'VAULT',self.root/'vault');self.vault_patch.start()
        self.watch_patch.start()
        self.catalog_patch=patch.object(securities,'master_path',return_value=self.root/'master.json');self.catalog_patch.start()
        self.refresh_patch=patch.object(securities,'refresh',return_value={'state':'cached'});self.refresh_patch.start()
        self.fallback_patch=patch.object(securities,'cninfo_rows',return_value=[]);self.fallback_patch.start()
        core.atomic_json(self.root/'master.json',{'schema':1,'securities':[
            securities.security('600519','贵州茅台','SH','https://www.sse.com.cn/')]})
        self.store = Store(self.root / "test.sqlite")

    def tearDown(self):
        self.store.conn.close()
        self.catalog_patch.stop();self.refresh_patch.stop();self.fallback_patch.stop()
        self.watch_patch.stop()
        self.core_patch.stop()
        self.vault_patch.stop()
        self.temp.cleanup()

    def document(self, code, company, ident, published_at):
        self.store.add_document(dict(id=ident, code=code, company=company, market="SH", period="2026Q3",
                                     title="2026年第三季度报告", published_at=published_at,
                                     source_url="https://static.cninfo.com.cn/finalpage/2026-10-18/" + ident + ".PDF"))

    def test_add_by_exact_local_name_and_priority_both_queues(self):
        self.document("688475", "萤石网络", "1225600001", "2026-10-16T00:00:00+08:00")
        self.document("688508", "芯朋微", "1225600002", "2026-10-17T00:00:00+08:00")
        result = watchlist.change_watchlist("add", "萤石网络", self.store)
        self.assertEqual(result[0]["code"], "688475")
        self.assertEqual(watchlist.watched_codes(), ["688475"])
        self.assertEqual(pipeline.download_queue(self.store, 1, watchlist.watched_codes())[0]["code"], "688475")
        self.store.execute("UPDATE documents SET state='parsed'")
        self.assertEqual(pipeline.analysis_queue(self.store, 1, watchlist.watched_codes())[0]["code"], "688475")
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)

    def test_catalog_code_is_verified_without_outbound_lookup(self):
        with patch.object(watchlist, "Store", side_effect=AssertionError("unexpected store creation")):
            items = watchlist.change_watchlist("add", "600519", self.store)
        self.assertEqual(items[0]["company"], "贵州茅台")
        self.assertEqual(items[0]["market"], "SH")
        self.document("600519", "贵州茅台", "1225600003", "2026-10-18T00:00:00+08:00")
        self.assertEqual(watchlist.list_watched(self.store)[0]["company"], "贵州茅台")

    def test_unknown_name_or_unsupported_code_is_not_silently_added(self):
        for value in ("不存在的公司", "200001", "430001", "68847"):
            with self.assertRaises(ServiceError):
                watchlist.change_watchlist("add", value, self.store)
        self.assertEqual(watchlist.watched_codes(), [])

    def test_duplicate_and_remove_preserves_documents(self):
        self.document("688475", "萤石网络", "1225600001", "2026-10-16T00:00:00+08:00")
        watchlist.change_watchlist("add", "688475", self.store)
        with self.assertRaises(ServiceError):
            watchlist.change_watchlist("add", "萤石网络", self.store)
        self.assertEqual(watchlist.change_watchlist("remove", "688475", self.store), [])
        self.assertEqual(self.store.rows("SELECT COUNT(*) AS n FROM documents")[0]["n"], 1)
        self.assertIn("688475", json.loads(self.settings.read_text())["pdf_unwatched_at"])
        watchlist.change_watchlist("add", "688475", self.store)
        self.assertNotIn("688475", json.loads(self.settings.read_text())["pdf_unwatched_at"])

    def test_watched_scan_rechecks_fresh_page_before_resuming_cursor(self):
        today = sources.now().date().isoformat()
        self.store.set("scan_tasks_official_v1_SH", [{"day": today, "page": 2, 'provider':'sh'}])
        self.store.set("scan_tasks_official_v1_BJ", [])
        calls = []

        def query(store, column, start, end, page):
            calls.append((column, page))
            records = [document({'id':'1225600099','code':'600000'},column)] if (column,page)==('SH',1) else []
            return {'documents':records,'total':60 if column=='SH' else 0,'page_size':30,'provider':'sh' if column=='SH' else 'cninfo'}

        def document(record, column):
            return dict(id=record["id"], code=record["code"], company="样例公司", market="SH",
                        period="2026Q3", title="2026年第三季度报告", published_at=today,
                        source_url="https://static.cninfo.com.cn/finalpage/2026-10-18/1225600099.PDF")

        cfg = {"initial_lookback_days": 1, "scan_lookback_days": 1, "pages_per_source_per_run": 1}
        with patch.object(sources, "config", return_value=cfg), patch('stock_research.disclosure_sources.query_disclosures',side_effect=query), \
             patch.object(sources.time, "sleep"):
            result = sources.collect(self.store, ["600000"])
        self.assertEqual(calls[:2], [("SH", 1), ("SH", 2)])
        self.assertEqual(result["watched_new"], 1)
        self.assertEqual(pipeline.download_queue(self.store, 1, ["600000"])[0]["code"], "600000")


if __name__ == "__main__":
    unittest.main()
