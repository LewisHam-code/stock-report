"""Transparent research scores; deliberately not a calibrated return forecast."""
from __future__ import annotations

import math
import statistics
from datetime import date

from .core import finite, now, pct_change, stamp
from .strategy import single_quarters, technical_signals

VERSION = "buy-score-2026-10-v2"
FORMULA = "完整买入评分 = clip(质量30 + 成长25 + 估值20 + 动量15 + 行业10 − 风险扣分, 0, 风险上限≤100)；缺项时只展示已证实贡献构成的保守下限与待补齐区间"
SOURCES = [
    {"name": "AAII A+ 多因子框架", "url": "https://www.aaii.com/journal/article/437530-how-the-components-of-a-grades-help-you-analyze-stocks", "use": "借鉴质量、成长、估值与动量分维度；不复刻其专有分位排名和分析师预期因子"},
    {"name": "MSCI Quality", "url": "https://www.msci.com/indexes/index/702787/msci-world-quality-index", "use": "参考ROE、盈利稳定性与财务杠杆"},
    {"name": "Piotroski 原始研究", "url": "https://www.ivey.uwo.ca/media/3775523/value_investing_the_use_of_historical_financial_statement_information.pdf", "use": "参考现金流、应计利润与财务改善思路；没有完整九项年度数据时不宣称计算了原版F-score"},
    {"name": "Schwab 评级方法说明", "url": "https://workplace.schwab.com/story/buy-hold-sell-what-analyst-stock-ratings-mean", "use": "参考多维评价和评级时限，不复制券商模型或收益承诺"},
]


def scale(value, anchors):
    value = finite(value)
    if value is None:
        return None
    if value <= anchors[0][0]:
        return float(anchors[0][1])
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if value <= x1:
            return y0 + (value - x0) / (x1 - x0) * (y1 - y0)
    return float(anchors[-1][1])


def lower_percentile(value, peers):
    peers = [v for v in map(finite, peers) if v is not None and v > 0]
    if finite(value) is None or value <= 0 or len(peers) < 20:
        return None
    # Midrank handles ties; no industry percentile is fabricated for tiny samples.
    higher = sum(p > value for p in peers)
    equal = sum(p == value for p in peers)
    return (higher + equal * 0.5) / len(peers) * 100


def cagr(history, metric, years=3):
    rows = {r["period_end"]: r for r in history if r["period_end"].endswith("12-31")}
    if not rows:
        return None
    end = max(rows)
    start = str(int(end[:4]) - years) + "-12-31"
    a, b = finite(rows[end].get(metric)), finite(rows.get(start, {}).get(metric))
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    if any(str(y) + "-12-31" not in rows for y in range(int(start[:4]), int(end[:4]) + 1)):
        return None
    return ((a / b) ** (1 / years) - 1) * 100


def score_report(report, snapshot, peers=None):
    extraction = report.get("financial_analysis") or {}
    from .financial_facts import trusted_metrics
    values = {m["metric"]: finite(m.get("standard_value")) for m in trusted_metrics(extraction) if m.get("scope") in ("cumulative", "instant")}
    history = (snapshot.get("financial") or {}).get("history", [])
    history = [r for r in history if r.get("period_end", "9999") <= now().date().isoformat() and (not r.get("disclosed_at") or r["disclosed_at"] <= now().date().isoformat())]
    annual = sorted([r for r in history if r["period_end"].endswith("12-31")], key=lambda r: r["period_end"], reverse=True)
    latest, previous = (annual[0] if annual else {}), (annual[1] if len(annual) >= 2 else {})
    quote = snapshot.get("quote") or {}
    prices = snapshot.get("prices") or {}
    tech = technical_signals(prices, snapshot.get("benchmark"))
    items = []
    industry = report.get("industry", "")
    financial = any(word in industry for word in ("银行", "保险", "证券", "金融"))
    history_source = (snapshot.get("financial") or {}).get("source", "结构化历史财务源")
    primary = report.get("source", "财报原文")

    def add(group, name, weight, score, raw, formula, source, reason=""):
        s = finite(score)
        known = s is not None
        s = min(100.0, max(0.0, s)) if known else None
        items.append({"group": group, "name": name, "weight": weight, "factor_score": round(s, 2) if known else None,
                      "points": round(weight * s / 100, 4) if known else 0, "available": known,
                      "value": raw, "formula": formula, "source": source, "missing_reason": reason if not known else ""})

    profit, cash = values.get("net_profit"), values.get("operating_cashflow")
    ratio = cash / profit if profit is not None and profit > 0 and cash is not None else None
    conversion = scale(ratio, [(-0.5, 0), (0, 0), (0.5, 35), (1, 80), (1.2, 100)])
    if profit is not None and profit <= 0 and cash is not None:
        conversion = 0
    add("质量", "现金利润匹配", 10, None if financial else conversion, ratio, "经营现金流/归母净利润；0.5→35，1→80，1.2→100，区间线性插值", primary, "缺少原文核对的现金流、正利润或行业专用口径")
    roe = finite(latest.get("roe"))
    add("质量", "年度ROE", 8, scale(roe, [(0, 0), (5, 25), (10, 50), (15, 75), (20, 100)]), roe, "最近完整年度ROE：0/5/10/15/20%对应0/25/50/75/100", history_source, "缺少完整年度ROE")
    eps = [finite(r.get("eps")) for r in annual[:6]]
    consecutive = len(annual) >= 6 and all(int(annual[i]["period_end"][:4]) - int(annual[i + 1]["period_end"][:4]) == 1 for i in range(5))
    eps_growth = [pct_change(eps[i], eps[i + 1]) for i in range(5)] if consecutive else []
    variability = statistics.pstdev(eps_growth) if len(eps_growth) == 5 and all(g is not None for g in eps_growth) else None
    add("质量", "盈利稳定性", 5, scale(variability, [(0, 100), (10, 90), (25, 60), (50, 20), (80, 0)]), variability, "最近5个完整年度EPS同比增速标准差，百分点；越低越好；需连续6年EPS且各同比正基期", history_source, "缺少连续6年可比EPS或存在非正基期")
    a, b = finite(latest.get("total_assets")), finite(previous.get("total_assets"))
    p, c = finite(latest.get("net_profit")), finite(latest.get("operating_cashflow"))
    accrual = (p - c) / ((a + b) / 2) * 100 if all(x is not None for x in (a, b, p, c)) and (a + b) > 0 else None
    add("质量", "应计利润质量", 4, None if financial else scale(accrual, [(-5, 100), (0, 80), (5, 40), (10, 0)]), accrual, "(年度归母净利润−经营现金流)/平均总资产×100；辅助源口径，非原版F-score", history_source, "缺少年度现金流、利润或两年总资产，或需金融专用模型")
    assets, liabilities = values.get("total_assets"), values.get("total_liabilities")
    leverage = liabilities / assets * 100 if assets and assets > 0 and liabilities is not None else None
    add("质量", "负债约束", 3, None if financial else scale(leverage, [(20, 100), (40, 80), (60, 50), (80, 0)]), leverage, "负债/资产×100；普通非金融企业研究阈值，不适用于银行保险", primary, "缺少原文核对的资产负债或需行业专用模型")

    suffix = {"FY": "12-31", "H1": "06-30", "Q1": "03-31", "Q3": "09-30"}.get(str(report.get("period", ""))[4:])
    end = str(report.get("period", ""))[:4] + "-" + suffix if suffix else ""
    revenue_quarters = single_quarters(history, "revenue")
    profit_quarters = single_quarters(history, "adjusted_profit")
    rev = next((r.get("yoy_pct") for r in revenue_quarters if r["period_end"] == end), None)
    adj = next((r.get("yoy_pct") for r in profit_quarters if r["period_end"] == end), None)
    add("成长", "单季度营收增长", 8, scale(rev, [(-20, 0), (0, 40), (10, 60), (25, 80), (50, 100)]), rev, "同年累计相减后同比；-20/0/10/25/50%对应0/40/60/80/100", history_source, "缺少单季度可比数据或正数基期")
    add("成长", "单季度扣非增长", 8, scale(adj, [(-20, 0), (0, 40), (15, 60), (30, 80), (60, 100)]), adj, "同年累计扣非利润相减后同比；负基期不计算增长率", history_source, "缺少单季度扣非利润或正数基期")
    revenue_cagr = cagr(history, "revenue")
    add("成长", "三年营收复合增长", 5, scale(revenue_cagr, [(-10, 0), (0, 40), (10, 65), (20, 85), (30, 100)]), revenue_cagr, "(最近完整年度营收/三年前营收)^(1/3)−1；需连续4年", history_source, "缺少连续4年年度营收或正值基数")
    trailing = [r["yoy_pct"] for r in revenue_quarters if r["period_end"] <= end][-3:]
    acceleration = (100 if trailing[0] < trailing[1] < trailing[2] and trailing[2] > 0 else 50 if trailing[2] > 0 else 0) if len(trailing) == 3 and all(v is not None for v in trailing) else None
    add("成长", "季度增长加速度", 4, acceleration, trailing, "最近3个单季度同比依次上升且最新为正→100；最新为正但不连续加速→50；否则0", history_source, "缺少最近3个可比单季度同比")

    pe = finite(quote.get("pe_ttm"))
    # Wire the optional validated peer input through the production path and
    # count distinct companies, never duplicated rows or the subject itself.
    peers = peers if peers is not None else snapshot.get("valuation_peers", [])
    peer_by_code = {p["code"]: p for p in peers if isinstance(p.get("code"),str) and len(p["code"]) == 6 and p["code"].isdigit()
                    and p["code"] != report.get("code") and p.get("industry_verified") and p.get("same_period") and finite(p.get("pe_ttm")) is not None}
    peer_values = [p["pe_ttm"] for p in peer_by_code.values()]
    add("估值", "同行盈利收益率比较", 10, lower_percentile(pe, peer_values), pe, "正PE在≥20个同业、同一时点已核实样本中的反向中位分位×100；不使用统一低PE阈值", quote.get("source", ""), "缺少≥20个独立核实同业样本，或盈利为负；不能虚构估值分位")
    ttm_cash, ttm_capex = finite(snapshot.get("verified_ttm_cashflow")), finite(snapshot.get("verified_ttm_capex"))
    market_cap = finite(quote.get("market_cap"))
    fcf_yield = (ttm_cash - ttm_capex) / market_cap * 100 if all(v is not None for v in (ttm_cash, ttm_capex, market_cap)) and market_cap > 0 else None
    add("估值", "自由现金流收益率", 10, None if financial else scale(fcf_yield, [(-2, 0), (0, 20), (3, 50), (6, 80), (10, 100)]), fcf_yield, "(已核实TTM经营现金流−TTM资本开支)/总市值×100；不得以投资现金流替代资本开支", primary, "缺少原文核实的TTM现金流、资本开支或当前市值")

    rs = tech.get("relative_strength_60d_pp") if not tech.get("stale") else None
    add("动量", "60日相对强度", 5, scale(rs, [(-20, 0), (0, 50), (20, 100)]), rs, "股票60交易日涨幅−沪深300同日期涨幅，百分点；−20/0/20对应0/50/100", prices.get("source", ""), "缺少同日期指数日线或行情过期")
    bars = prices.get("bars", [])
    risk_momentum = None
    if len(bars) >= 273 and not tech.get("stale"):
        close = [b["close"] for b in bars]
        logs = [math.log(close[i] / close[i-1]) for i in range(len(close)-252, len(close))]
        volatility = statistics.pstdev(logs) * math.sqrt(252)
        if volatility > 0:
            risk_momentum = ((close[-22] / close[-147] - 1) + (close[-22] / close[-273] - 1)) / 2 / volatility
    add("动量", "风险调整中期动量", 5, scale(risk_momentum, [(-1, 0), (0, 50), (1, 100)]), risk_momentum, "跳过最近21日的6/12月涨幅均值÷252日年化日收益波动率；本项目研究实现", prices.get("source", ""), "需至少273个交易日日线，且行情未过期")
    setup = ((40 if tech.get("trend") else 0) + (30 if tech.get("pullback") else 0) + (30 if tech.get("breakout_confirmation") else 0)) if tech.get("available") and not tech.get("stale") else None
    add("动量", "强势回调买点", 5, setup, {k: tech.get(k) for k in ("trend", "pullback", "breakout_confirmation")}, "趋势成立40、合理回调30、突破确认30；结合既有回调策略", prices.get("source", ""), "交易历史不足或行情过期")

    sources = report.get("industry_evidence", [])
    fresh = []
    for source in sources:
        try:
            age = (now().date() - date.fromisoformat(source.get("published_at", ""))).days
            if 0 <= age <= 90 and source.get("quote"):
                fresh.append(source)
        except (ValueError, TypeError):
            pass
    distinct = len({s["url"] for s in fresh})
    outlook = report.get("industry_outlook", "unknown")
    sector_score = {"positive": 100, "neutral": 50, "negative": 0}.get(outlook) if distinct >= 2 else None
    add("行业", "有外部证据的景气方向", 10, sector_score, {"outlook": outlook, "verified_recent_sources": distinct}, "至少两条90日内原文引文已匹配的行业资料；正向100、中性50、负向0；AI归纳仍有判断不确定性", "; ".join(s["url"] for s in fresh), "缺少足够近期原文证据或景气方向未知；不因管理层乐观用语加分")

    penalties, cap = [], 100
    flags = (report.get("decision") or {}).get("red_flags", [])
    if any("差异超过" in f for f in flags):
        penalties.append({"reason": "原文与结构化财务口径存在未解决冲突", "points": 20})
        cap = min(cap, 59)
    opinion = extraction.get("audit_opinion", "")
    adverse_audit = any(w in opinion for w in ("否定意见", "无法表示意见", "持续经营重大不确定")) or ("保留意见" in opinion and "无保留意见" not in opinion)
    if adverse_audit:
        penalties.append({"reason": "审计或持续经营风险，需要人工核查", "points": 25})
        cap = min(cap, 39)
    if profit is not None and profit <= 0:
        penalties.append({"reason": "归母净利润非正", "points": 15})
        cap = min(cap, 49)
    if profit is not None and profit > 0 and cash is not None and cash < 0:
        penalties.append({"reason": "正利润同时经营现金流为负，需核查季节性", "points": 10})
    if "ST" in str(report.get("company", "")).upper() or "退" in str(report.get("company", "")):
        penalties.append({"reason": "风险警示或退市标识", "points": 30})
        cap = min(cap, 39)
    penalty = min(30, sum(p["points"] for p in penalties))
    earned = sum(p["points"] for p in items)
    coverage = sum(p["weight"] for p in items if p["available"])
    score = min(cap, max(0, earned - penalty))
    upper = min(cap, max(0, earned + 100 - coverage - penalty))
    blockers = []
    if coverage < 85:
        blockers.append(f"评分数据覆盖{coverage:g}%，低于85%门槛")
    missing_valuation=[p['name'] for p in items if not p['available'] and p['group']=='估值']
    if missing_valuation:
        blockers.append('估值因子尚未补齐：'+'、'.join(missing_valuation))
    if sector_score is None:
        blockers.append("缺少至少两条90日内可追溯行业证据及明确景气判断")
    if financial:
        blockers.append("金融行业尚缺专用评分模型")
    provisional = bool(blockers)
    # This diagnostic isolates observed factor performance from completeness.
    # It is never used to produce an investment grade or infer missing factors.
    observed = min(cap,max(0,earned/coverage*100-penalty)) if coverage >= 50 else None
    dimensions = []
    for group, weight in (("质量", 30), ("成长", 25), ("估值", 20), ("动量", 15), ("行业", 10)):
        group_items = [p for p in items if p["group"] == group]
        dimensions.append({"name": group, "maximum": weight, "points": round(sum(p["points"] for p in group_items), 2), "covered_weight": sum(p["weight"] for p in group_items if p["available"])})
    return {"version": VERSION, "score": round(score, 1), "maximum": 100, "status": "provisional" if provisional else "calculated", "status_label": "保守下限" if provisional else "已计算",
            "coverage_pct": round(coverage, 1), "uncovered_weight": round(100-coverage,1), "possible_range": [round(score, 1), round(upper, 1)], "raw_points": round(earned, 2), "penalty": penalty, "risk_cap": cap,
            "score_kind": "lower_bound" if provisional else "complete", "provisional_reasons": blockers,
            "observed_reference": {"score": round(observed,1) if observed is not None else None,
                                   "formula": "已覆盖因子贡献÷已覆盖权重×100，再扣风险分并应用风险上限；覆盖不足50%不计算",
                                   "usage": "仅用于诊断已覆盖因子的表现；不是完整买入评分，不用于评级，不代表缺失估值与行业项也能取得同样分数"},
            "dimensions": dimensions, "items": items, "penalties": penalties, "missing": [p["name"] + "：" + p["missing_reason"] for p in items if not p["available"]],
            "asof": stamp(), "price_asof": quote.get("asof"), "formula": FORMULA, "references": SOURCES,
            "interpretation": "这是本项目的研究规则评分，不是国际机构原版评级、上涨概率或预期收益率。缺项时保守下限不等于完整评分，区间仅表示在已知因子及风险约束不变下的算术范围，不是收益预测或置信区间；缺失权重不转给其他因子。权重、阈值和分档尚未经A股样本外验证。"}
