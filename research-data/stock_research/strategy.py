from __future__ import annotations

import math
import statistics
from datetime import date

from .core import config, finite, now, pct_change


def mean(values):
    return statistics.fmean(values) if values else None


def single_quarters(history, metric):
    """Derive quarters by subtraction of same-year cumulative values."""
    lookup = {r["period_end"]: r for r in history}
    out = []
    previous = {"06-30": "03-31", "09-30": "06-30", "12-31": "09-30"}
    for end, row in sorted(lookup.items()):
        v = finite(row.get(metric))
        if v is None:
            continue
        if end[5:] != "03-31":
            prior = lookup.get(end[:4] + "-" + previous.get(end[5:], ""))
            p = finite(prior.get(metric)) if prior else None
            v = v - p if p is not None else None
        old_end = str(int(end[:4]) - 1) + end[4:]
        prior_row = lookup.get(old_end)
        p = finite(prior_row.get(metric)) if prior_row else None
        if p is not None and end[5:] != "03-31":
            earlier = lookup.get(str(int(end[:4]) - 1) + "-" + previous.get(end[5:], ""))
            earlier_value = finite(earlier.get(metric)) if earlier else None
            p = p - earlier_value if earlier_value is not None else None
        out.append({"period_end": end, "value": v, "yoy_pct": pct_change(v, p)})
    return out


def technical_signals(prices, benchmark=None):
    if (prices or {}).get('cross_validation', {}).get('status') == 'conflict':
        return {'available': False, 'reason': '行情源日线交叉验证存在冲突', 'buy_signal': False}
    bars = (prices or {}).get("bars", [])
    if len(bars) < 65:
        return {"available": False, "reason": "日线不足65个交易日", "buy_signal": False}
    close = [b["close"] for b in bars]
    current, prior = close[-1], close[-2]
    ma20, ma60 = mean(close[-20:]), mean(close[-60:])
    prior_ma60 = mean(close[-65:-5])
    high = max(b["high"] for b in bars[-60:])
    stop = min(b["low"] for b in bars[-10:-1])
    trend = current > ma60 and ma20 > ma60 and ma60 > prior_ma60
    pullback = -18 <= (current / high - 1) * 100 <= -2
    support = current > ma20 * 0.97 and current > prior
    rebound = current > max(b["high"] for b in bars[-6:-1])
    volumes = [finite(b.get("volume")) for b in bars[-21:-1]]
    volume_mean = mean([v for v in volumes if v is not None])
    volume_ratio = finite(bars[-1].get("volume")) / volume_mean if volume_mean and finite(bars[-1].get("volume")) else None
    relative = None
    if benchmark and len(benchmark.get("bars", [])) >= 61:
        benchmark_dates = {b["date"]: b for b in benchmark["bars"]}
        first, last = bars[-61]["date"], bars[-1]["date"]
        if first in benchmark_dates and last in benchmark_dates:
            relative = (current / close[-61] - benchmark_dates[last]["close"] / benchmark_dates[first]["close"]) * 100
    stale = (now().date() - date.fromisoformat(bars[-1]["date"])).days > 5
    return {"available": True, "asof": bars[-1]["date"], "stale": stale, "adjustment": prices.get("adjustment"),
            "ma20": ma20, "ma60": ma60, "trend": trend, "pullback": pullback, "support_rebound": support,
            "breakout_confirmation": rebound, "relative_strength_60d_pp": relative, "volume_ratio": volume_ratio,
            "drawdown_from_60d_high_pct": (current / high - 1) * 100, "stop_ratio": stop / current,
            "buy_signal": bool(trend and pullback and support and rebound and relative is not None and relative > 0 and not stale)}


def position_plan(entry, stop, code, equity=None):
    cfg = config()
    equity = equity or cfg["account_equity_example"]
    if not entry or not stop or stop >= entry or stop <= 0:
        return {"shares": 0, "reason": "没有可用的止损距离"}
    risk_budget = equity * cfg["risk_per_trade"]
    cap = min(risk_budget / (entry - stop), equity * cfg["maximum_position_fraction"] / entry)
    # STAR permits 200+ shares; use conservative 100-share increments everywhere.
    shares = math.floor(cap / 100) * 100
    if code.startswith("688") and shares < 200:
        shares = 0
    return {"example_equity": equity, "risk_budget": risk_budget, "entry_reference": entry, "initial_stop": stop,
            "shares": shares, "position_value": shares * entry, "planned_loss": shares * (entry - stop),
            "stages": [0.4, 0.3, 0.3], "portfolio_risk_limit": cfg["portfolio_open_risk_limit"],
            "conditions": "仅为示例；未读取真实持仓。加仓须复核总风险及新信号；T+1、跳空与跌停可能令实际亏损超过计划。"}


def evaluate(doc, extraction, snapshot, industry):
    from .securities import lookup
    security_state=lookup(doc['code']) or {}
    metrics = {}
    from .financial_facts import trusted_metrics
    for m in trusted_metrics(extraction):
        if m.get("scope") in ("cumulative", "instant"):
            metrics[m["metric"]] = m["standard_value"]
    history = (snapshot.get("financial") or {}).get("history", [])
    quarter_growth = single_quarters(history, "revenue")
    tech = technical_signals(snapshot.get("prices"), snapshot.get("benchmark"))
    quote = snapshot.get("quote") or {}
    current_year = doc["period"][:4]
    last_suffix = {"FY": "12-31", "H1": "06-30", "Q1": "03-31", "Q3": "09-30"}.get(doc["period"][4:])
    current_end = current_year + "-" + (last_suffix or "12-31")
    prior = next((r for r in history if r["period_end"] == str(int(current_year) - 1) + current_end[4:]), {})
    revenue_growth = pct_change(metrics.get("revenue"), prior.get("revenue"))
    profit_growth = pct_change(metrics.get("adjusted_profit"), prior.get("adjusted_profit"))
    score, evidence, missing, red_flags = {}, [], [], []
    quality = extraction.get('data_quality') or {}
    if quote.get('cross_validation', {}).get('status') == 'conflict':
        missing.append('行情及估值报价交叉验证存在冲突，尚待复核')
    if quality.get('conflicts'):
        missing.append('财报原文对应期间数值存在冲突，尚待复核')
    if quality.get('failed_checks'):
        missing.append('财报勾稽校验未通过，尚待复核')
    if quality.get('parser_status') and quality['parser_status'] != 'checked':
        missing.append('财报原表核对尚未成功完成')
    matching = next((r for r in history if r["period_end"] == current_end), {})
    for key in ("revenue", "net_profit"):
        a, b = metrics.get(key), matching.get(key)
        if a is not None and b is not None and abs(a - b) > max(abs(a) * 0.01, 100):
            red_flags.append(key + "原文与结构化源差异超过1%，需复核口径")
    score["成长性（25）"] = (12 if revenue_growth is not None and revenue_growth >= 15 else 6 if revenue_growth is not None and revenue_growth > 0 else 0) + (13 if profit_growth is not None and profit_growth >= 20 else 6 if profit_growth is not None and profit_growth > 0 else 0)
    profit, cash = metrics.get("net_profit"), metrics.get("operating_cashflow")
    cash_ratio = cash / profit if profit is not None and profit > 0 and cash is not None else None
    score["利润质量（25）"] = (15 if cash_ratio is not None and cash_ratio >= 1 else 8 if cash_ratio is not None and cash_ratio >= 0.7 else 0) + (10 if metrics.get("adjusted_profit", -1) > 0 else 0)
    if profit is not None and profit <= 0:
        red_flags.append("归母净利润非正")
    if cash_ratio is not None and cash_ratio < 0.5:
        red_flags.append("经营现金流与归母净利润匹配度低于0.5，需核查季节性及营运资金")
    industry_sources = industry.get("verified_sources", [])
    score["行业与竞争（20）"] = min(20, len(industry_sources) * 5) if industry.get("outlook") == "positive" else min(10, len(industry_sources) * 3)
    pe, pb = finite(quote.get("pe_ttm")), finite(quote.get("pb"))
    # Historical rule score is retained for audit only. Current valuation
    # eligibility is computed from the scorecard inputs below, not keywords
    # in industry articles or the model's prose valuation_context.
    score["估值证据（15）"] = 0
    score["交易信号（15）"] = (6 if tech.get("trend") else 0) + (4 if tech.get("pullback") else 0) + (5 if tech.get("buy_signal") else 0)
    # Growth descriptors with nonpositive bases may be inapplicable. Their
    # missing weights are already accounted for by the coverage gate; they
    # must not become a second blanket prohibition on every research grade.
    for key,label in (("revenue","营业收入"),("net_profit","归母净利润"),("adjusted_profit","扣非净利润"),("operating_cashflow","经营现金流")):
        if metrics.get(key) is None:
            missing.append(label+"尚未通过财报原文核对")
    if not quote.get("price"):
        missing.append("缺少有时间戳的当前股价")
    elif (now() - __import__("datetime").datetime.fromisoformat(quote["asof"])).days > 5:
        missing.append("行情超过5个自然日未更新")
    if not tech.get("available") or tech.get("stale"):
        missing.append("交易日线不足或过期")
    if not extraction.get("coverage", {}).get("full_text"):
        coverage=extraction.get('coverage',{})
        if coverage.get('all_source_pages_selected') and coverage.get('omissions'):
            missing.append('目录、通用释义及行政信息按记录省略，未提供逐字全文')
        else:
            missing.append("长财报采用重点页提取，尚未覆盖全文")
    if any(x in (security_state.get('company') or doc["company"]).upper() for x in ("ST", "退")):
        red_flags.append("风险警示或退市标识")
    if security_state.get('risk_flag'):
        red_flags.append('当前官方证券风险警示：'+security_state['risk_flag'])
    if security_state.get('status') in ('delisted','delisting_announced','pending_confirmation'):
        missing.append('当前证券已退市或上市状态待核实，不形成新的正式评级')
    financial_industry = any(x in extraction.get("industry", "") for x in ("银行", "保险", "证券", "金融"))
    if financial_industry:
        missing.append("金融行业需补充资本充足率、不良率或偿付能力专用模型")
    legacy_score = sum(score.values())
    from .scorecard import score_report
    scoring = score_report({"company": doc["company"], "code": doc["code"], "period": doc["period"], "source": doc.get("source") or doc.get("source_url", "财报原文"), "industry": extraction.get("industry", ""),
                            "financial_analysis": extraction, "industry_evidence": industry_sources, "industry_outlook": industry.get("outlook", "unknown"),
                            "decision": {"red_flags": red_flags}}, snapshot)
    missing.extend(scoring.get("provisional_reasons", []))
    if any("差异超过" in flag for flag in red_flags):
        missing.append("原文与结构化财务口径冲突尚未解决")
    total = scoring["score"]
    score = {d["name"] + "（" + str(d["maximum"]) + "）": d["points"] for d in scoring["dimensions"]}
    from .ratings import classify
    classification = classify(scoring, missing, red_flags, tech)
    rating = classification["rating"]
    if revenue_growth is not None:
        evidence.append(f"累计营收同比{revenue_growth:.2f}%（原文值对比上年同期结构化值）")
    if profit_growth is not None:
        evidence.append(f"累计扣非利润同比{profit_growth:.2f}%")
    if cash_ratio is not None:
        evidence.append(f"经营现金流/归母净利润={cash_ratio:.2f}；须结合季节性解释")
    last_growth = [r["yoy_pct"] for r in quarter_growth[-3:]]
    acceleration = len(last_growth) == 3 and all(x is not None for x in last_growth) and last_growth[0] < last_growth[1] < last_growth[2]
    stop = quote.get("price", 0) * tech["stop_ratio"] if tech.get("stop_ratio") and quote.get("price") else None
    return {"rating": rating, "score": total, "components": score, "scoring": scoring, "legacy_rule_score": legacy_score, "growth": {"revenue_yoy_pct": revenue_growth, "adjusted_profit_yoy_pct": profit_growth, "single_quarters": quarter_growth[-12:], "acceleration": acceleration},
            "cash_conversion": cash_ratio, "evidence": evidence, "missing": missing, "red_flags": red_flags,
            "quote": quote, "technical": tech, "position": position_plan(quote.get("price"), stop, doc["code"]),
            "rating_policy": classification["rating_policy"], "evidence_status": classification["evidence_status"], "rating_blockers": classification["rating_blockers"]}
