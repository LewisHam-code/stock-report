import copy
import json
import io
import pathlib
import tempfile
import urllib.error
import unittest
from datetime import timedelta
from unittest.mock import patch

from stock_research import analysis, core, sources
from stock_research.analysis import EXTRACTION, validate, verify_extraction
from stock_research.backtest import simulate
from stock_research.core import ServiceError, Store, now, pct_change
from stock_research.sources import document_from_record, query_announcements, report_period
from stock_research.strategy import evaluate, position_plan, single_quarters
from stock_research import models


def record(code="600000", market="SH"):
    return {"secCode": code, "secName": "测试公司", "announcementId": "1225000000", "announcementTitle": "2026年第三季度报告", "announcementTime": int(now().timestamp() * 1000), "adjunctUrl": "finalpage/2026-10-17/1225000000.PDF", "pageColumn": market}


def bars():
    values = [10 + i * 0.04 for i in range(40)] + [11.6 + i * 0.06 for i in range(16)] + [12.6, 12.8, 13.0, 13.1, 11.9, 11.8, 11.85, 11.9, 12.1, 12.3, 12.5, 11.6]
    values += [11.5 + (i % 4) * 0.01 for i in range(70)]
    start = now().date() - timedelta(days=len(values))
    result = [dict(date=(start + timedelta(days=i)).isoformat(), open=v, close=v, high=v + 0.03, low=v - 0.03, volume=1000) for i, v in enumerate(values)]
    result[66].update(open=12.4, high=12.6, low=11.7)
    return result


class FinanceTests(unittest.TestCase):
    def test_single_quarters_use_cumulative_subtraction(self):
        rows = [{"period_end": "2025-03-31", "revenue": 120}, {"period_end": "2025-06-30", "revenue": 300}, {"period_end": "2026-03-31", "revenue": 150}, {"period_end": "2026-06-30", "revenue": 450}]
        q = single_quarters(rows, "revenue")[-1]
        self.assertEqual(q["value"], 300)
        self.assertAlmostEqual(q["yoy_pct"], 66.6666666667)

    def test_negative_base_is_not_growth(self):
        self.assertIsNone(pct_change(10, -10))
        self.assertIsNone(pct_change(10, 0))

    def test_missing_preceding_cumulative_is_not_a_quarter(self):
        self.assertIsNone(single_quarters([{"period_end": "2026-06-30", "revenue": 300}], "revenue")[0]["value"])

    def test_risk_position_respects_budget_and_lot(self):
        with patch('stock_research.strategy.config', return_value={**core.config(), 'risk_per_trade':.005,'maximum_position_fraction':.2}):
            plan = position_plan(13, 12.5, "600000", 100000)
        self.assertEqual(plan["shares"], 1000)
        self.assertLessEqual(plan["planned_loss"], 500)
        self.assertEqual(plan["shares"] % 100, 0)

    def test_invalid_stop_never_creates_position(self):
        self.assertEqual(position_plan(10, 11, "600000")["shares"], 0)

    def test_missing_evidence_cannot_rate_buy(self):
        doc = {"period": "2026Q3", "company": "测试公司", "code": "600000"}
        result = evaluate(doc, {"metrics": [], "coverage": {"full_text": True}}, {}, {})
        self.assertEqual(result["rating"], "unrated")
        self.assertTrue(result["missing"])


class SourceTests(unittest.TestCase):
    def test_all_period_types(self):
        for title, expected in [("2025年年度报告", "2025FY"), ("2025年度报告（更正后）", "2025FY"), ("2026年半年度报告", "2026H1"), ("2026年第一季度报告", "2026Q1"), ("2026年第三季度报告", "2026Q3")]:
            self.assertEqual(report_period(title), expected)

    def test_summary_english_and_correction_notice_excluded(self):
        for title in ("2025年年度报告摘要", "关于2025年年度报告的更正公告", "2025年年度报告（英文）"):
            self.assertIsNone(report_period(title))

    def test_b_shares_and_neeq_excluded(self):
        self.assertIsNone(document_from_record(record("900001", "SH"), "szse"))
        self.assertIsNone(document_from_record(record("200001", "SZ"), "szse"))
        self.assertIsNone(document_from_record(record("430001", "NEEQ"), "neeq"))

    def test_beijing_requires_source_market(self):
        self.assertEqual(document_from_record(record("920001", "BJ"), "bj")["market"], "BJ")
        self.assertIsNone(document_from_record(record("430001", "UNKNOWN"), "szse"))

    def test_malicious_pdf_path_rejected(self):
        r = record()
        r["adjunctUrl"] = "../../.env.local"
        self.assertIsNone(document_from_record(r, "szse"))

    def test_empty_disclosure_day_is_not_source_failure(self):
        with patch.object(sources, "get_json", return_value={"announcements": None, "totalAnnouncement": 0}):
            self.assertEqual(query_announcements("2026-10-01", "2026-10-01")["announcements"], [])

    def test_missing_records_with_nonzero_count_is_failure(self):
        with patch.object(sources, "get_json", return_value={"announcements": None, "totalAnnouncement": 50}):
            with self.assertRaises(ServiceError):
                query_announcements("2026-10-01", "2026-10-01")


class IntegrityTests(unittest.TestCase):
    def test_document_insert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(pathlib.Path(directory) / "test.sqlite")
            try:
                doc = document_from_record(record(), "szse")
                self.assertEqual(store.add_document(doc), 1)
                self.assertEqual(store.add_document(doc), 0)
            finally:
                store.conn.close()

    def test_numeric_value_and_quote_must_both_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "test.json").write_text(json.dumps({"pages": [{"page": 1, "text": "营业收入 1,000.00 元"}]}))
            data = {"industry": "测试", "company_business": "测试", "metrics": [{"metric": "revenue", "value": 1000, "unit": "yuan", "scope": "cumulative", "period_end": "2026-09-30", "source_page": 1, "quote": "营业收入 1,000.00 元"}], "management_claims": [], "sections": [], "risks": [], "audit_opinion": "未披露", "uncertainties": []}
            with patch.object(analysis, "ROOT", root):
                correct = verify_extraction({"text_file": "test.json", "period": "2026Q3"}, copy.deepcopy(data), {})
                self.assertTrue(correct["metrics"][0]["text_matched"])
                self.assertFalse(correct["metrics"][0]["verified"])
                self.assertIsNone(correct["metrics"][0]["standard_value"])
                wrong = copy.deepcopy(data)
                wrong["metrics"][0]["value"] = 1000000
                self.assertFalse(verify_extraction({"text_file": "test.json", "period": "2026Q3"}, wrong, {})["metrics"][0]["verified"])

    def test_json_mode_invalid_field_type_rejected(self):
        with self.assertRaises(ServiceError):
            validate({"industry": 12}, EXTRACTION)

    def test_secret_is_not_in_public_settings(self):
        original = core.config()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "automation.json").write_text(json.dumps(original))
            with patch.object(core, "ROOT", root), patch.object(core, "PROJECT", root), patch.dict("os.environ", {}, clear=True):
                fake = "sk-unit-test-value-never-real"
                settings = core.save_settings({"provider": "deepseek", "model": "deepseek-chat", "test_mode": True, "api_key": fake})
                self.assertNotIn(fake, json.dumps(settings))
                self.assertTrue(settings["configured"]["deepseek"])
                self.assertEqual((root / ".env.local").stat().st_mode & 0o777, 0o600)
                core.save_settings({"provider": "openai", "model": "gpt-5-mini", "test_mode": True, "api_key": ""})
                self.assertIn(fake, (root / ".env.local").read_text())


class ExecutionTests(unittest.TestCase):
    def test_t_plus_one_and_next_day_execution(self):
        data = bars()
        result = simulate(data)
        self.assertGreaterEqual(len(result["trades"]), 2)
        self.assertEqual(result["trades"][0]["date"], data[66]["date"])
        self.assertEqual(result["trades"][1]["date"], data[67]["date"])
        self.assertEqual(result["trades"][0]["side"], "buy")
        self.assertEqual(result["trades"][1]["side"], "sell")

    def test_future_bars_do_not_change_past_trades(self):
        data = bars()
        first = simulate(data[:110])["trades"]
        full = [t for t in simulate(data)["trades"] if t["date"] <= data[109]["date"]]
        self.assertEqual(first, full)

    def test_future_fundamental_rating_not_backdated(self):
        events = [{"known_at": (now() + timedelta(days=1)).isoformat(), "eligible": True}]
        self.assertEqual(simulate(bars(), "fundamental_trend", events=events)["trades"], [])

    def test_locked_limit_up_cannot_buy(self):
        data = bars()
        data[66].update(open=14, close=14, high=14, low=14)
        result = simulate(data)
        self.assertFalse(any(t["date"] == data[66]["date"] and t["side"] == "buy" for t in result["trades"]))


class ModelFailureTests(unittest.TestCase):
    def test_gpt61_uses_explicit_low_reasoning_and_records_actual_response_model(self):
        cfg={**core.config(),'provider':'openai','model':'gpt-6.1-sol','single_model':True,'max_output_tokens':10000,'api_protocol':'openai','base_url':'https://api.openai.com/v1'}
        response=io.BytesIO(b'{"status":"completed","model":"gpt-6.1-sol","output":[{"content":[{"type":"output_text","text":"OK"}]}],"usage":{"input_tokens":10,"output_tokens":2}}')
        with patch.object(models,'api_key',return_value='sk-test-placeholder-only'),patch.object(models,'model_open',return_value=response) as call:
            text,meta=models.completion('test','test',settings=cfg,purpose='financial_extraction')
        payload=json.loads(call.call_args.args[0].data)
        self.assertEqual(payload['reasoning'],{'effort':'low'})
        self.assertEqual(payload['max_output_tokens'],10000)
        self.assertEqual(meta['response_model'],'gpt-6.1-sol')
        self.assertEqual(text,'OK')

    def test_v4_extraction_does_not_exhaust_budget_on_default_thinking(self):
        cfg={**core.config(),'provider':'deepseek','model':'deepseek-v4-pro','single_model':True,'max_output_tokens':10000,'prefer_off_peak':False}
        response=io.BytesIO(b'{"choices":[{"finish_reason":"stop","message":{"content":"OK"}}]}')
        with patch.object(models,'api_key',return_value='sk-test-placeholder-only'),patch.object(models,'model_open',return_value=response) as call:
            models.completion('test','test',settings=cfg,purpose='financial_extraction')
        payload=json.loads(call.call_args.args[0].data)
        self.assertEqual(payload['max_tokens'],10000)
        self.assertEqual(payload['thinking'],{'type':'disabled'})

    def test_deepseek_quota_error_opens_circuit(self):
        cfg = {**core.config(), "provider": "deepseek", "model": "deepseek-chat", "single_model": True}
        error = urllib.error.HTTPError('https://api.deepseek.com/chat/completions',402,'Payment Required',{},io.BytesIO(b'{"error":{"code":"insufficient_balance"}}'))
        with tempfile.TemporaryDirectory() as directory:
            store = Store(pathlib.Path(directory) / "test.sqlite")
            try:
                with patch.object(models, "api_key", return_value="sk-test-placeholder-only"), patch.object(models, "model_open", side_effect=error):
                    with self.assertRaises(ServiceError) as caught:
                        models.completion("test", "test", store=store, settings=cfg)
                self.assertEqual(caught.exception.code,"quota_exhausted")
                self.assertEqual(store.setting("ai_circuit_deepseek")["code"],"quota_exhausted")
                self.assertEqual(store.rows("SELECT state FROM api_calls")[0]["state"],"quota_exhausted")
            finally:
                store.conn.close()

    def test_truncated_response_is_not_success(self):
        cfg = {**core.config(), "provider": "deepseek", "model": "deepseek-chat", "prefer_off_peak":False}
        cfg.pop('model_profiles', None)  # Explicit legacy-route fixture, independent of local accounts.
        response = io.BytesIO(b'{"choices":[{"finish_reason":"length","message":{"content":"partial"}}]}')
        with patch.object(models, "api_key", return_value="sk-test-placeholder-only"), patch.object(models, "model_open", return_value=response):
            with self.assertRaises(ServiceError) as caught:
                models.completion("test", "test", settings=cfg)
        self.assertEqual(caught.exception.code,"model_output_incomplete")

    def test_missing_key_does_not_make_network_request(self):
        with patch.object(models, "api_key", side_effect=ServiceError("missing_key","missing")), patch.object(models, "model_open") as network:
            with self.assertRaises(ServiceError):
                models.completion("test", "test")
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
