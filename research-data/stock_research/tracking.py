"""Point-in-time report follow-up. This never calls a model or changes a rating."""
from __future__ import annotations

import json
import re
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, time

from .core import ROOT, SITE, ServiceError, Store, atomic_json, dumps, finite, now, stamp

HORIZONS = (3, 7, 15, 30, 60, 90)
BASELINES = ROOT / "db" / "tracking-baselines.json"
FORMAL_RATINGS = {"buy", "overweight", "neutral", "underweight", "sell"}


def _formal(baseline):
    score = finite(baseline.get('score'))
    return (baseline.get('provenance') == 'on_time' and baseline.get('rating') in FORMAL_RATINGS
            and baseline.get('score_status') == 'calculated' and score is not None and 0 <= score <= 100
            and baseline.get('test_mode') is not True)


def _date(value):
    try:
        return datetime.fromisoformat(value).date()
    except (TypeError, ValueError):
        return None


def _change(last, first):
    a, b = finite(last), finite(first)
    return round((a / b - 1) * 100, 2) if a is not None and b is not None and b > 0 else None


def _after_publication(quote_time, generated_at):
    try:
        return datetime.fromisoformat(quote_time) > datetime.fromisoformat(generated_at)
    except (TypeError, ValueError):
        return False


def _bars(prices):
    return sorted((b for b in (prices or {}).get("bars", []) if b.get("date") and finite(b.get("close")) and finite(b.get("open"))), key=lambda b: b["date"])


def horizon_result(baseline, prices, benchmark, horizon, today=None):
    """Entry is the first session after publication; exit is session N close."""
    today = today or now().date()
    published = _date(baseline.get("generated_at"))
    rows = [b for b in _bars(prices) if published and b["date"] > published.isoformat() and b["date"] <= today.isoformat()]
    benchmark_rows = [b for b in _bars(benchmark) if published and b["date"] > published.isoformat() and b["date"] <= today.isoformat()]
    if len(benchmark_rows) >= horizon:
        expected_start, expected_end = benchmark_rows[0]["date"], benchmark_rows[horizon - 1]["date"]
        stock_by_date = {b["date"]: b for b in rows}
        if expected_start not in stock_by_date or expected_end not in stock_by_date:
            return {"state": "data_gap", "reason": "观察期起点或终点缺少个股行情，可能停牌"}
        entry, end = stock_by_date[expected_start], stock_by_date[expected_end]
    else:
        if len(rows) < horizon:
            return {"state": "pending", "sessions": len(rows)}
        entry, end = rows[0], rows[horizon - 1]
    if (prices or {}).get('cross_validation', {}).get('status') == 'conflict':
        return {'state': 'data_gap', 'reason': '两个行情源前复权价格存在冲突，需复核'}
    if (prices or {}).get("adjustment") != "qfq":
        return {"state": "data_gap", "reason": "缺少个股前复权价格"}
    market = {b["date"]: b for b in _bars(benchmark)}
    stock_return = _change(end["close"], entry["open"])
    index_return = (_change(market[end["date"]]["close"], market[entry["date"]]["open"])
                    if entry["date"] in market and end["date"] in market else None)
    if stock_return is None:
        return {"state": "data_gap", "reason": "收益率无法计算"}
    excess = round(stock_return - index_return, 2) if index_return is not None else None
    rating = baseline.get("rating")
    formal = _formal(baseline)
    # Absolute direction is the primary diagnostic; relative performance stays separate.
    aligned = (stock_return > 0 if rating in ("buy", "overweight") else stock_return < 0 if rating in ("underweight", "sell") else abs(stock_return) <= 3)
    relative_aligned = ((excess > 0 if rating in ("buy", "overweight") else excess < 0 if rating in ("underweight", "sell") else abs(excess) <= 3)
                        if excess is not None else None)
    return {"state": "evaluated", "entry_date": entry["date"], "end_date": end["date"],
            "entry_open": entry["open"], "end_close": end["close"],
            "index_entry_open": market[entry["date"]]["open"] if entry["date"] in market else None,
            "index_end_close": market[end["date"]]["close"] if end["date"] in market else None,
            "price_source": (prices or {}).get("source"), "benchmark_source": (benchmark or {}).get("source"),
            "return_pct": stock_return, "benchmark_pct": index_return, "excess_pct": excess,
            "aligned": aligned if formal else None, "relative_aligned": relative_aligned if formal else None,
            "benchmark_gap": index_return is None,
            "evaluable": formal}


def _score_band(score):
    score = finite(score)
    if score is None:
        return "未知"
    if score < 25:
        return "0–24"
    if score < 45:
        return "25–44"
    if score < 65:
        return "45–64"
    if score < 80:
        return "65–79"
    return "80–100"


def _group_stats(items, horizon, key):
    groups = {}
    for item in items:
        outcome = item["horizons"][str(horizon)]
        if outcome.get('state') != 'evaluated' or not outcome.get("evaluable"):
            continue
        group = key(item)
        groups.setdefault(group, []).append(outcome)
    return [{"group": name, "count": len(values), "aligned": sum(v["aligned"] is True for v in values),
             "alignment_pct": round(100 * sum(v["aligned"] is True for v in values) / len(values), 1),
             "average_excess_pct": round(sum(v["excess_pct"] for v in values if v["excess_pct"] is not None) /
                                         sum(v["excess_pct"] is not None for v in values), 2)
             if any(v["excess_pct"] is not None for v in values) else None}
            for name, values in sorted(groups.items())]


def _pattern(item, horizon):
    outcome = item["horizons"][str(horizon)]
    if not outcome.get("evaluable") or outcome.get("aligned"):
        return None
    if item["rating"] in ("buy", "overweight"):
        return "大盘同跌" if outcome["benchmark_pct"] is not None and outcome["benchmark_pct"] < 0 else "个股走弱"
    if item["rating"] in ("sell", "underweight"):
        return "个股逆势走强" if outcome["benchmark_pct"] is not None and outcome["benchmark_pct"] <= 0 else "个股走强"
    return "中性观察带外"


def capture_baselines(reports, existing=None, captured_at=None, repair_original_metadata=False, publication_quotes=None):
    existing = dict(existing or {})
    captured_at = captured_at or stamp()
    for report in reports:
        if report.get('formal_tracking_eligible') is False or report.get('report_kind')=='historical_foundation':continue
        original=(report.get('decision') or {}).get('scoring') or report.get('scoring') or {}
        score_fields={'score':finite(report.get('score',original.get('score'))),
                      'score_status':report.get('score_status') or original.get('status'),
                      'score_coverage':report.get('score_coverage',original.get('coverage_pct')),
                      'scoring_version':original.get('version')}
        metadata = {key: report.get(key) for key in ('industry', 'provider', 'model', 'test_mode')}
        metadata['strategy_version'] = report.get('strategy_version')
        original_quote=report.get('quote_at_analysis') or (report.get('decision') or {}).get('quote') or {}
        metadata.update(price_collected_at=original_quote.get('fetched_at'),price_source=original_quote.get('source'),
                        price_cross_validation=original_quote.get('cross_validation'))
        from .tracking_quotes import baseline_fields, valid
        publication_fields = baseline_fields(report, (publication_quotes or {}).get(report['id'], {}))
        if valid(original_quote):
            publication_fields = {**metadata, 'price': original_quote['price'], 'price_asof': original_quote['asof'],
                                  'price_basis': 'original_report'}
        if report["id"] in existing:
            # publish_tracking receives immutable database payloads before any
            # current-rule scoring. Only repair absent metadata from that
            # original nested decision; never alter rating or capture timing.
            nested=(report.get('decision') or {}).get('scoring') or {}
            if repair_original_metadata:
                fields={**metadata, **({'score':finite(nested.get('score')),'score_status':nested.get('status'),
                        'score_coverage':nested.get('coverage_pct'),'scoring_version':nested.get('version')} if nested else {})}
                base=dict(existing[report['id']])
                added={key:value for key,value in fields.items() if base.get(key) is None and value is not None}
                if added:
                    audit = {'metadata_source':'original_report','metadata_repaired_at':captured_at}
                    if any(key in added for key in ('score','score_status','score_coverage','scoring_version')):
                        audit.update(score_metadata_source='original_report_decision',score_metadata_repaired_at=captured_at)
                    existing[report['id']]={**base,**added,**audit}
            base = existing[report['id']]
            if finite(base.get('price')) is None and publication_fields:
                existing[report['id']] = {**base, **publication_fields, 'price_repaired_at': captured_at}
            continue
        quote = report.get("quote_at_analysis") or (report.get("decision") or {}).get("quote") or {}
        generated = report.get("generated_at")
        try:
            delay = datetime.fromisoformat(captured_at) - datetime.fromisoformat(generated)
            provenance = "on_time" if timedelta(0) <= delay <= timedelta(minutes=30) else "backfilled"
        except (TypeError, ValueError):
            provenance = "backfilled"
        existing[report["id"]] = {"report_id": report["id"], "code": report["code"], "company": report["company"],
                                  "period": report["period"], "generated_at": generated, "captured_at": captured_at,
                                  "price": finite(quote.get("price")), "price_asof": quote.get("asof"),
                                  "rating": report.get("rating"), **score_fields, **metadata, "provenance": provenance}
        if not valid(quote) and publication_fields:
            existing[report['id']].update(publication_fields)
    return existing


def _pending_result(baseline, benchmark, horizon, today, index_dates=None):
    published = _date(baseline.get("generated_at"))
    dates = index_dates if index_dates is not None else [bar["date"] for bar in _bars(benchmark)]
    sessions = bisect_right(dates, today.isoformat()) - bisect_right(dates, published.isoformat()) if published else 0
    if sessions >= horizon:
        return {"state": "data_gap", "reason": "观察期已满，个股收盘行情待补齐"}
    return {"state": "pending", "sessions": sessions}


def build_tracking(reports, snapshots, benchmark, baselines, asof=None, outcomes=None, publication_quotes=None):
    asof = asof or stamp()
    today = _date(asof) or now().date()
    index_dates = [bar["date"] for bar in _bars(benchmark)]
    items = []
    for report in reports:
        base = baselines[report["id"]]
        snap = snapshots.get(report["code"]) or {}
        quote = snap.get("quote") or {}
        from .tracking_quotes import valid
        saved_quote = ((publication_quotes or {}).get(report['id']) or {}).get('quote') or {}
        publication_current = False
        if valid(saved_quote) and (not valid(quote) or datetime.fromisoformat(saved_quote['asof']) > datetime.fromisoformat(quote['asof'])
                or (saved_quote['asof'] == quote['asof'] and str(saved_quote.get('fetched_at') or '') > str(quote.get('fetched_at') or ''))):
            quote = saved_quote
            publication_current = True
        initial=False
        if (finite(quote.get('price')) is None or finite(quote.get('price'))<=0 or not _date(quote.get('asof'))
                or str(quote.get('asof') or '')<str(base.get('price_asof') or '')):
            quote={'price':base.get('price'),'asof':base.get('price_asof'),'fetched_at':base.get('price_collected_at'),
                   'source':base.get('price_source'),'cross_validation':base.get('price_cross_validation') or {}}
            initial=True
        price_date = _date(quote.get("asof"))
        current_price = finite(quote.get("price"))
        if current_price is not None and current_price<=0:current_price=None
        results = {str(n): (outcomes.get((report["id"], n)) or _pending_result(base, benchmark, n, today, index_dates))
                   if outcomes is not None else horizon_result(base, snap.get("prices"), benchmark, n, today)
                   for n in HORIZONS}
        # Saved outcomes may predate repaired metadata. Recheck eligibility from
        # the frozen baseline; never trust a persisted evaluable flag alone.
        results = {key: {**value, 'evaluable': False, 'aligned': None, 'relative_aligned': None}
                   if not _formal(base) or value.get('state') != 'evaluated' else value for key, value in results.items()}
        from .securities import price_enabled
        from .tracking_market import chart_series
        if not price_enabled(report['code']):
            results={key:value if value.get('state')=='evaluated' else
                     {'state':'data_gap','reason':'已确认退市；保留样本，终止价格或观察期数据不足，不能计为预测成功'}
                     for key,value in results.items()}
        items.append({**base, "current_price": current_price, "current_asof": quote.get("asof") if current_price is not None else None,
                      "current_source": quote.get("source") if current_price is not None else None,
                      "current_checked_at":quote.get('fetched_at') if publication_current else snap.get('quote_checked_at') or quote.get('fetched_at'),
                      "current_cross_validation":quote.get('cross_validation',{}),
                      "current_is_initial":initial,
                      "current_after_interpretation":_after_publication(quote.get('asof'),base.get('generated_at')),
                      "price_trend":chart_series(snap.get('chart_prices'),str(base.get('generated_at') or '')[:10],today.isoformat()),
                      # Keep the visible price and its arithmetic change consistent;
                      # source verification remains explicit in current_cross_validation.
                      "current_change_pct": _change(current_price, base.get("price")),
                      "price_is_stale": (today - price_date).days > 1 if current_price is not None and price_date else None,
                      "horizons": results})
    items.sort(key=lambda r: r.get("generated_at") or "", reverse=True)
    summary = {}
    for n in HORIZONS:
        outcomes = [r["horizons"][str(n)] for r in items]
        eligible = [o for o in outcomes if o['state']=='evaluated' and o.get("evaluable")]
        summary[str(n)] = {"reports": len(items), "mature": sum(o["state"] == "evaluated" for o in outcomes),
                           "evaluable": len(eligible), "aligned": sum(o["aligned"] is True for o in eligible),
                           "alignment_pct": round(100 * sum(o["aligned"] is True for o in eligible) / len(eligible), 1) if eligible else None,
                           "relative_alignment_pct": round(100 * sum(o["relative_aligned"] is True for o in eligible) /
                                                           sum(o["relative_aligned"] is not None for o in eligible), 1)
                           if any(o["relative_aligned"] is not None for o in eligible) else None,
                           "pending": sum(o["state"] == "pending" for o in outcomes),
                           "data_gap": sum(o["state"] == "data_gap" for o in outcomes),
                           "benchmark_gap": sum(o.get("benchmark_gap") is True for o in outcomes)}
        summary[str(n)]['companies'] = len({item['code'] for item in items if item['horizons'][str(n)].get('evaluable')})
        exploratory = [item for item in items if not _formal(item) and item['horizons'][str(n)]['state']=='evaluated']
        summary[str(n)]['exploratory'] = {
            'mature': len(exploratory), 'companies': len({item['code'] for item in exploratory}),
            'provisional': sum(item.get('score_status')!='calculated' for item in exploratory),
            'backfilled': sum(item.get('provenance')!='on_time' for item in exploratory),
            'test_mode': sum(item.get('test_mode') is True for item in exploratory),
            'basis': '仅统计观察期完成数量；排除项可重叠，不计算方向吻合率，不参与正式评级检验'}
        patterns = {}
        for item in items:
            pattern = _pattern(item, n)
            if pattern:
                patterns[pattern] = patterns.get(pattern, 0) + 1
        summary[str(n)]["by_rating"] = _group_stats(items, n, lambda item: item["rating"])
        summary[str(n)]["by_score"] = _group_stats(items, n, lambda item: _score_band(item.get("score")))
        summary[str(n)]['by_industry'] = _group_stats(items, n, lambda item: item.get('industry') or '原始行业未知')
        summary[str(n)]['by_model'] = _group_stats(items, n, lambda item: '/'.join(str(item.get(key) or '未知') for key in ('provider','model')))
        summary[str(n)]['by_version'] = _group_stats(items, n, lambda item: item.get('scoring_version') or '原始版本未知')
        summary[str(n)]["patterns"] = [{"name": k, "count": v} for k, v in sorted(patterns.items(), key=lambda pair: (-pair[1], pair[0]))]
    return {"generated_at": asof, "horizons": list(HORIZONS), "summary": summary, "reports": items,
            "method": {"basis": "报告生成后下一交易日开盘至第N个交易日收盘，个股前复权收益与沪深300价格指数同期表现对照",
                       "alignment": "方向吻合：买入/增持看个股正收益，减持/卖出看负收益，中性暂用±3个百分点观察带。相对大盘吻合率另行计算；这些是诊断口径，非已验证的收益预测",
                       "limits": "解读时与最近行情的价格变化使用原始报价，遇分红送转可能失真；观察期结果用前复权日线。未走满、缺数据、补录评级均不计入吻合率。"}}


def publish_tracking(reports, snapshots, benchmark, store=None, vault=None):
    if store:
        from .tracking_notes import restore
        restore(store, BASELINES, vault)
    try:
        old = json.loads(BASELINES.read_text()) if BASELINES.exists() else {}
        if not isinstance(old, dict):
            old = {}
    except (OSError, ValueError):
        old = {}
    from .tracking_quotes import saved
    publication_quotes = {report['id']: saved(report, vault) for report in reports
                          if report.get('market') in ('SH', 'SZ', 'BJ')}
    baselines = capture_baselines(reports, old,repair_original_metadata=True,publication_quotes=publication_quotes)
    if baselines != old:
        atomic_json(BASELINES, baselines)
    outcomes = {(r["report_id"], r["horizon"]): json.loads(r["payload"])
                for r in store.rows("SELECT report_id,horizon,payload FROM tracking_outcomes")} if store else None
    from .tracking_market import overlay
    payload = build_tracking(reports, overlay(snapshots,vault), benchmark, baselines, outcomes=outcomes,publication_quotes=publication_quotes)
    atomic_json(SITE / "tracking.json", payload)
    if store:
        from .tracking_notes import sync
        sync(store, baselines, vault)
    return {"reports": len(payload["reports"]), "evaluable_3d": payload["summary"]["3"]["evaluable"]}


def _quote_cache_hours(at=None):
    """A closing quote must expire by the next weekday's market open."""
    at = at or now()
    opening = at.replace(hour=9, minute=30, second=0, microsecond=0)
    if at.weekday() < 5 and opening <= at < at.replace(hour=16, minute=30, second=0, microsecond=0):
        return 1
    if at >= opening or at.weekday() >= 5:
        opening += timedelta(days=1)
    while opening.weekday() >= 5:
        opening += timedelta(days=1)
    return min(36, max(1 / 60, (opening - at).total_seconds() / 3600))


def _attempt(store, key, action, at=None):
    """Bound failures per source and day; a later trading day may retry."""
    day = (at or now()).date().isoformat()
    previous = store.setting("tracking_failures_" + key, {})
    failures = previous.get("count", 0) if isinstance(previous, dict) and previous.get("day") == day else 0
    if failures >= 3:
        return None
    try:
        value = action()
    except ServiceError:
        store.set("tracking_failures_" + key, {"day": day, "count": failures + 1})
        return None
    if failures:
        store.set("tracking_failures_" + key, {"day": day, "count": 0})
    return value


def collect_tracking_prices(store, watched=(), at=None, vault=None):
    """One post-close pass: fetch only due horizons and watched closes. No model calls."""
    from .sources import daily_bars, latest_quote
    from .tracking_notes import restore

    restore(store, BASELINES, vault)

    at = at or now()
    today = at.date().isoformat()
    result = {"outcomes": 0, "stock_series": 0, "watch_closes": 0, "benchmark_requests": 0}
    from . import core,job_schedule
    adaptive=job_schedule.enabled(core.config())
    if (job_schedule.trading_day(at.date()) is not True if adaptive else at.weekday() >= 5) or at.time() < time(16, 30):
        return result
    rows = store.rows("SELECT r.id,r.payload,d.market,d.code FROM reports r JOIN documents d ON d.id=r.document_id")
    watched_markets = {item["code"]: item.get("market") for item in watched if isinstance(item, dict) and item.get("market") in ("SH", "SZ", "BJ")}
    if not rows and not watched_markets:
        return result
    index_key = "tracking-index-em-ths-v1"
    cached_index = store.cached(index_key) or {}
    benchmark = cached_index.get("prices") if cached_index.get("day") == today else None
    try:
        baselines = json.loads(BASELINES.read_text()) if BASELINES.exists() else {}
    except (OSError, ValueError):
        baselines = {}
    completed = {(r["report_id"], r["horizon"]) for r in store.rows("SELECT report_id,horizon FROM tracking_outcomes")}
    from .securities import price_enabled
    outstanding = any(price_enabled(row['code']) and row["id"] in baselines and any((row["id"], n) not in completed for n in HORIZONS) for row in rows)
    if benchmark is None and outstanding:
        benchmark = _attempt(store, index_key, lambda: daily_bars("000300", "SH", count=640, minimum=1), at)
        if benchmark is not None:
            result["benchmark_requests"] = 1
            store.cache(index_key, {"day": today, "prices": benchmark}, hours=36)
            store.cache("benchmark-hs300-v2", benchmark, hours=_quote_cache_hours(at))
    if benchmark:
        index_dates = [bar["date"] for bar in _bars(benchmark)]
        due = {}
        for row in rows:
            report = json.loads(row["payload"])
            baseline = baselines.get(report["id"])
            if not baseline:
                continue
            for horizon in HORIZONS:
                if (report["id"], horizon) in completed:
                    continue
                state = _pending_result(baseline, benchmark, horizon, at.date(), index_dates)
                if state["state"] != "pending":
                    due.setdefault(report["code"], {"market": row["market"], "jobs": []})["jobs"].append((report["id"], baseline, horizon))
        for code, group in due.items():
            from .securities import price_enabled
            if not price_enabled(code):continue
            series_key = "tracking-series-em-ths-v1-" + code
            saved_series = store.cached(series_key) or {}
            # A fresh analysis may have fetched the same series already today.
            cached = store.cached("snapshot-v4-" + group["market"] + code)
            fetched = datetime.fromisoformat(cached["fetched_at"]) if cached and cached.get("fetched_at") else None
            prices = (cached or {}).get("prices") if fetched and fetched.date() == at.date() and fetched.time() >= time(16, 30) else None
            if not prices and saved_series.get("day") == today:
                prices = saved_series.get("prices")
            if not prices:
                count = 160 if all((_date(base.get("generated_at")) or at.date()) >= at.date() - timedelta(days=230)
                                   for _, base, _ in group["jobs"]) else 640
                prices = _attempt(store, series_key,
                                  lambda c=code, m=group["market"], n=count: daily_bars(c, m, count=n, minimum=1), at)
                if prices is not None:
                    result["stock_series"] += 1
                    store.cache(series_key, {"day": today, "prices": prices}, hours=36)
            if not prices:
                continue
            for report_id, baseline, horizon in group["jobs"]:
                outcome = horizon_result(baseline, prices, benchmark, horizon, at.date())
                dates = [b["date"] for b in _bars(benchmark) if b["date"] > str(baseline.get("generated_at", ""))[:10]]
                end_date = _date(dates[horizon - 1]) if len(dates) >= horizon else None
                # A missing bar can be a delayed source update. Confirm it after
                # four calendar days; a later explicit repair can revisit gaps.
                confirmed_gap = outcome["state"] == "data_gap" and end_date and (at.date() - end_date).days >= 4
                if outcome["state"] == "evaluated" or confirmed_gap:
                    store.execute("INSERT OR IGNORE INTO tracking_outcomes VALUES(?,?,?,?)",
                                  (report_id, horizon, dumps(outcome), stamp()))
                    result["outcomes"] += 1
    # Watched companies alone get a daily raw close. One row per code/date.
    for code, market in watched_markets.items():
        from .securities import price_enabled
        if not price_enabled(code):continue
        if store.rows("SELECT 1 FROM tracking_watch_closes WHERE code=? AND trade_date=?", (code, today)):
            continue
        seen_key = "tracking-close-seen-" + code
        if (store.cached(seen_key) or {}).get("day") == today:
            continue
        if adaptive:
            from .tracking_market import read,record_path
            quote=read(record_path(market,code,vault)).get('quote')
        else:
            quote = _attempt(store, "tracking-close-" + code,
                             lambda c=code, m=market: latest_quote(c, m), at)
        quote_asof = datetime.fromisoformat(quote["asof"]) if quote and quote.get("asof") else None
        price = finite((quote or {}).get("price"))
        if quote_asof and quote_asof.date() == at.date() and quote_asof.time() >= time(15) and price is not None and price > 0:
            store.execute("INSERT OR IGNORE INTO tracking_watch_closes VALUES(?,?,?,?,?)",
                          (code, today, price, quote["source"], stamp()))
            result["watch_closes"] += 1
            store.cache("tracking-live-v3-" + code, quote, hours=_quote_cache_hours(at))
        elif quote is not None:
            store.cache(seen_key, {"day": today, "stale": True}, hours=36)
    return result


def current_quotes(codes, store=None):
    """Refresh only requested report stocks; reuse recent quotes on repeated views."""
    from .sources import latest_quote

    if not isinstance(codes, list) or not 1 <= len(codes) <= 30 or any(not isinstance(c, str) or not re.fullmatch(r"\d{6}", c) for c in codes):
        raise ServiceError("invalid_tracking_codes", "请提供最多30个有效股票代码")
    owned = store is None
    store = store or Store()
    try:
        unique = list(dict.fromkeys(codes))
        marks = ",".join("?" for _ in unique)
        markets = {r["code"]: r["market"] for r in store.rows(
            "SELECT d.code,d.market FROM documents d JOIN reports r ON r.document_id=d.id WHERE d.code IN (" + marks + ") GROUP BY d.code", unique)}
        available = {code: store.cached("tracking-live-v3-" + code) for code in unique if code in markets}
        from .securities import price_enabled
        missing = [code for code in unique if code in markets and price_enabled(code) and available.get(code) is None
                   and store.cached("tracking-live-error-v3-" + code) is None]
        from . import core,job_schedule
        if job_schedule.enabled(core.config()):
            from .tracking_market import read,record_path
            for code in unique:
                if code in markets and not available.get(code):available[code]=read(record_path(markets[code],code)).get('quote')
            missing=[]  # The independent market scheduler owns source refreshes.
        if missing:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {code: pool.submit(latest_quote, code, markets[code]) for code in missing}
                for code, future in futures.items():
                    try:
                        quote = future.result()
                        from .securities import note_quote_identity
                        note_quote_identity(store,code,quote)
                        available[code] = quote
                        # During trading the quote is live; after close it is stable.
                        store.cache("tracking-live-v3-" + code, quote, hours=_quote_cache_hours())
                    except ServiceError:
                        available[code] = None
                        store.cache("tracking-live-error-v3-" + code, {"unavailable": True}, hours=0.25)
        return {code: quote for code, quote in available.items() if quote}
    finally:
        if owned:
            store.conn.close()
