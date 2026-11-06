import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from stock_research import core, models, scorecard, strategy
from stock_research.core import ServiceError, now
from stock_research.ratings import classify


class RoutingTests(unittest.TestCase):
    def settings(self):
        return {"model_profiles": [
            {"id": "off", "provider": "openai", "model": "off", "enabled": False, "test_mode": False},
            {"id": "a", "provider": "deepseek", "model": "first", "enabled": True, "test_mode": True},
            {"id": "b", "provider": "deepseek", "model": "second", "enabled": True, "test_mode": False}]}

    def test_success_stops_and_skips_unchecked_models(self):
        with patch.object(models, "_completion_once", return_value=("ok", {})) as call:
            result, meta = models.completion("", "", settings=self.settings())
        self.assertEqual(result, "ok")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[-1]["profile_id"], "a")
        self.assertEqual(meta["fallbacks"], [])

    def test_failure_uses_next_enabled_model(self):
        with patch.object(models, "_completion_once", side_effect=[ServiceError("quota_exhausted", "test"), ("ok", {})]) as call:
            _, meta = models.completion("", "", settings=self.settings())
        self.assertEqual([c.args[-1]["profile_id"] for c in call.call_args_list], ["a", "b"])
        self.assertEqual(meta["fallbacks"][0]["code"], "quota_exhausted")

    def test_redirect_stops_without_falling_back_to_another_provider(self):
        with patch.object(models,'_completion_once',side_effect=ServiceError('unsafe_model_redirect','check address')) as call:
            with self.assertRaises(ServiceError) as caught:models.completion('','',settings=self.settings())
        self.assertEqual(caught.exception.code,'unsafe_model_redirect')
        self.assertEqual(call.call_count,1)

    def test_no_selection_and_daily_limit_do_not_bypass_budget(self):
        cfg = self.settings()
        for p in cfg["model_profiles"]:
            p["enabled"] = False
        with patch.object(models, "_completion_once") as call:
            with self.assertRaises(ServiceError):
                models.completion("", "", settings=cfg)
            call.assert_not_called()
        with patch.object(models, "_completion_once", side_effect=ServiceError("daily_api_limit", "test")) as call:
            with self.assertRaises(ServiceError):
                models.completion("", "", settings=self.settings())
            self.assertEqual(call.call_count, 1)

    def test_save_null_id_order_and_isolated_credentials(self):
        cfg = core.config()
        cfg.pop("model_profiles", None)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "automation.json").write_text(json.dumps(cfg))
            with patch.object(core, "ROOT", root), patch.object(core, "PROJECT", root), patch.dict("os.environ", {}, clear=True):
                core.save_settings({"id": None, "provider": "deepseek", "model": "first", "api_key": "sk-fake-only-first-key", "enabled": True})
                saved = core.save_settings({"id": None, "provider": "deepseek", "model": "second", "api_key": "sk-fake-only-second-key", "enabled": False})
                first, second = saved["profiles"][-2:]
                self.assertEqual(core.api_key("deepseek", first["id"]), "sk-fake-only-first-key")
                self.assertEqual(core.api_key("deepseek", second["id"]), "sk-fake-only-second-key")
                order = [{"id": p["id"], "enabled": p["id"] == second["id"]} for p in reversed(saved["profiles"])]
                result = core.save_model_order({"profiles": order})
                self.assertEqual(result["model"], "second")
                self.assertEqual(result["profiles"][0]["id"], second["id"])
                self.assertNotIn("sk-fake", json.dumps(result))
                with self.assertRaises(ServiceError):
                    core.save_model_order({"profiles": [order[0], order[0]]})

    def test_remove_updates_fallback_without_enabling_unchecked_models(self):
        cfg = core.config()
        cfg["model_profiles"] = [{**p, "key_env": "DEEPSEEK_API_KEY" if p["provider"] == "deepseek" else "OPENAI_API_KEY"} for p in self.settings()["model_profiles"]]
        cfg.pop("removed_model_profiles", None)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "automation.json").write_text(json.dumps(cfg))
            (root / ".env.local").write_text("DEEPSEEK_API_KEY=sk-unit-test-placeholder-only\n")
            with patch.object(core, "ROOT", root), patch.object(core, "PROJECT", root), patch.dict("os.environ", {}, clear=True):
                result = core.remove_model_profile({"id": "a"})
                self.assertEqual([p["id"] for p in result["profiles"]], ["off", "b"])
                self.assertEqual(result["model"], "second")
                self.assertEqual(result["enabled_count"], 1)
                result = core.remove_model_profile({"id": "b"})
                self.assertEqual(result["enabled_count"], 0)
                self.assertFalse(result["profiles"][0]["enabled"])
                result = core.remove_model_profile({"id": "off"})
                self.assertEqual(result["profiles"], [])
                self.assertEqual(core.model_profiles(), [])
                with self.assertRaises(ServiceError):
                    core.remove_model_profile({"id": "missing"})
                self.assertTrue((root / ".env.local").exists())
                self.assertNotIn("removed_model_profiles", result)
                self.assertNotIn("sk-unit-test", json.dumps(result))
                saved = core.save_settings({"provider": "deepseek", "model": "second", "api_key": ""})
                self.assertTrue(saved["profiles"][0]["configured"])


class ScoreTests(unittest.TestCase):
    def setUp(self):
        catalog=patch('stock_research.securities.lookup',return_value=None);catalog.start();self.addCleanup(catalog.stop)
    def test_five_grades_and_evidence_gate(self):
        for score,rating in ((90,'buy'),(70,'overweight'),(50,'neutral'),(30,'underweight'),(10,'sell')):
            self.assertEqual(classify({'score':score,'status':'calculated'},[],[],{'buy_signal':True})['rating'],rating)
        self.assertEqual(classify({'score':95,'status':'calculated'},[],[],{})['rating'],'overweight')
        self.assertEqual(classify({'score':95,'status':'provisional'},[],[],{'buy_signal':True})['rating'],'unrated')
        self.assertEqual(classify({'score':95,'status':'calculated'},['缺少估值'],[],{'buy_signal':True})['evidence_status'],'insufficient')
    def test_missing_inputs_are_not_reweighted(self):
        sc = scorecard.score_report({"period": "2026Q3"}, {})
        self.assertEqual(sum(d["maximum"] for d in sc["dimensions"]), 100)
        self.assertEqual(sc["score"], 0)
        self.assertEqual(sc["coverage_pct"], 0)
        self.assertEqual(sc["possible_range"], [0, 100])
        self.assertEqual(sc["status"], "provisional")

    def test_small_peer_samples_and_negative_pe_are_missing(self):
        self.assertIsNone(scorecard.lower_percentile(10, [20] * 19))
        self.assertIsNone(scorecard.lower_percentile(-1, [20] * 20))
        self.assertEqual(scorecard.lower_percentile(10, [20] * 20), 100)
        self.assertEqual(scorecard.lower_percentile(10, [10] * 20), 50)

    def test_unqualified_audit_is_not_penalized(self):
        for opinion in ("标准无保留意见", "无保留意见", "未经审计"):
            sc = scorecard.score_report({"financial_analysis": {"audit_opinion": opinion}}, {})
            self.assertEqual(sc["penalty"], 0)
        for opinion in ("保留意见", "无法表示意见", "无保留意见，持续经营重大不确定性"):
            sc = scorecard.score_report({"financial_analysis": {"audit_opinion": opinion}}, {})
            self.assertEqual(sc["risk_cap"], 39)

    def test_st_risk_and_nonpositive_profit_cap(self):
        report = {"company": "*ST测试", "financial_analysis": {"metrics": [{"metric": "net_profit", "standard_value": -100, "verified": True, "scope": "cumulative"}]}}
        sc = scorecard.score_report(report, {})
        self.assertEqual(sc["penalty"], 30)
        self.assertLessEqual(sc["score"], 39)
        self.assertLessEqual(sc["possible_range"][1], 39)

    def test_full_evidence_can_reach_one_hundred(self):
        year = now().year - 1
        history = [{"period_end": f"{year-i}-12-31", "revenue": 10000 / 1.6**i, "roe": 25, "eps": 2 / 1.2**i,
                    "total_assets": 500, "net_profit": 100, "operating_cashflow": 200} for i in range(6)]
        for y, amounts in ((year, [100, 200, 300]), (year+1, [120, 260, 420])):
            for suffix, value in zip(("03-31", "06-30", "09-30"), amounts):
                history.append({"period_end": f"{y}-{suffix}", "revenue": value, "adjusted_profit": value})
        report = {"period": f"{year+1}Q3", "industry_outlook": "positive", "financial_analysis": {"metrics": [
            {"metric": k, "standard_value": v, "verified": True, "scope": "cumulative"}
            for k,v in (("net_profit", 100), ("operating_cashflow", 130), ("total_assets", 500), ("total_liabilities", 100))]},
            "industry_evidence": [{"url": u, "published_at": now().date().isoformat(), "quote": "verified quote"} for u in ("https://a.example", "https://b.example")]}
        snapshot = {"financial": {"history": history}, "quote": {"pe_ttm": 5, "market_cap": 1000}, "verified_ttm_cashflow": 200, "verified_ttm_capex": 50,
                    "prices": {"bars": [{"close": 10 * 1.006**i * (1.01 if i%2 else 1)} for i in range(280)]}}
        peers = [{"code":str(100000+i),"pe_ttm": 20, "same_period": True, "industry_verified": True} for i in range(20)]
        tech = {"available": True, "stale": False, "relative_strength_60d_pp": 30, "trend": True, "pullback": True, "breakout_confirmation": True,"buy_signal":True}
        with patch.object(scorecard, "technical_signals", return_value=tech):
            sc = scorecard.score_report(report, snapshot, peers)
        self.assertEqual(sc["coverage_pct"], 100)
        self.assertEqual(sc["score"], 100)
        self.assertEqual(sc["status"], "calculated")
        self.assertEqual(sc['observed_reference']['score'],100)
        # The actual evaluation route must consume the peer input without
        # requiring government industry prose to mention PE or valuations.
        snapshot['valuation_peers']=peers
        snapshot['quote'].update(price=10,asof=now().isoformat())
        report.update(company='公开样例',code='600001')
        report['financial_analysis']['coverage']={'full_text':True}
        report['financial_analysis']['metrics'] += [{'metric':k,'standard_value':420,'verified':True,'scope':'cumulative'} for k in ('revenue','adjusted_profit')]
        with patch.object(scorecard,'technical_signals',return_value=tech),patch.object(strategy,'technical_signals',return_value=tech):
            result=strategy.evaluate(report,report['financial_analysis'],snapshot,{'verified_sources':report['industry_evidence'],'outlook':'positive','valuation_context':''})
        self.assertEqual(result['rating'],'buy')
        self.assertEqual(result['rating_blockers'],[])

    def test_duplicate_peers_cannot_fill_a_twenty_company_requirement(self):
        peer={'code':'100001','pe_ttm':20,'industry_verified':True,'same_period':True}
        sc=scorecard.score_report({'code':'600001','period':'2026Q3'},{'quote':{'pe_ttm':5},'valuation_peers':[peer]*20})
        factor=next(i for i in sc['items'] if i['name']=='同行盈利收益率比较')
        self.assertFalse(factor['available'])

    def test_partial_score_is_a_bound_and_diagnostic_never_changes_rating(self):
        sc=scorecard.score_report({'period':'2026Q3'}, {})
        self.assertEqual(sc['score_kind'],'lower_bound')
        self.assertEqual(sc['uncovered_weight'],100)
        self.assertIsNone(sc['observed_reference']['score'])
        self.assertTrue(sc['provisional_reasons'])
        self.assertEqual(classify(sc,[],[],{'buy_signal':True})['rating'],'unrated')
        self.assertEqual(classify({'score':99,'status':'calculated','provisional_reasons':['关键证据缺失']},[],[],{'buy_signal':True})['rating'],'unrated')


if __name__ == "__main__":
    unittest.main()
