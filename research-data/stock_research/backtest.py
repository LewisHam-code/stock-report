from __future__ import annotations

import math
from datetime import datetime

from .core import dumps, now, stamp
from .strategy import mean


def simulate(bars, strategy="pullback", events=None, initial=100000, board="main"):
    """Daily close signals execute next day; no current-day bar enters signals.

    Point-in-time fundamental events must use knowledge timestamps, never the
    document period end. This is an exploratory single-security replay.
    """
    if len(bars) < 100:
        return {"state": "insufficient", "trades": [], "reason": "至少需要100个交易日"}
    cash, shares, entry, stop, first, stage, held = initial, 0, 0.0, 0.0, 0.0, 0, -1
    trades, curve = [], []
    commission, sell_tax, slip = 0.0003, 0.0005, 0.001
    limit = 0.30 if board == "beijing" else 0.20 if board in ("star", "chinext") else 0.10
    lot_min = 200 if board == "star" else 100
    events = sorted(events or [], key=lambda e: e["known_at"])

    def order(side, i, qty, reason, raw):
        nonlocal cash, shares, entry, held
        b = bars[i]
        prior = bars[i - 1]["close"]
        locked = abs(b["high"] - b["low"]) < 0.001
        if locked and ((side == "buy" and b["open"] >= prior * (1 + limit - 0.003)) or (side == "sell" and b["open"] <= prior * (1 - limit + 0.003))):
            return False
        if side == "sell" and held >= i:
            return False
        price = raw * (1 + slip if side == "buy" else 1 - slip)
        qty = min(qty, shares) if side == "sell" else qty
        amount = qty * price
        fee = max(5, amount * commission) + (amount * sell_tax if side == "sell" else 0)
        if qty <= 0 or (side == "buy" and (qty < lot_min or amount + fee > cash)):
            return False
        if side == "buy":
            entry = (entry * shares + amount) / (shares + qty)
            shares += qty
            cash -= amount + fee
            held = i
        else:
            shares -= qty
            cash += amount - fee
        trades.append({"date": b["date"], "side": side, "shares": qty, "price": round(price, 4), "fee": round(fee, 2), "reason": reason})
        return True

    for i in range(65, len(bars)):
        b, p = bars[i], bars[i - 1]
        past = bars[:i]
        closes = [x["close"] for x in past]
        ma20, ma60 = mean(closes[-20:]), mean(closes[-60:])
        trend = p["close"] > ma20 > ma60
        high = max(x["high"] for x in past[-60:])
        pullback = -18 <= (p["close"] / high - 1) * 100 <= -2
        rebound = p["close"] > max(x["high"] for x in past[-6:-1])
        signal = trend and pullback and rebound
        if strategy == "fundamental_trend":
            known = [e for e in events if e["known_at"][:10] < b["date"]]
            signal = signal and bool(known) and known[-1].get("eligible", False)
        if shares and strategy != "averaging_baseline":
            stop = max(stop, min(x["low"] for x in past[-10:]))
            if b["open"] <= stop:
                order("sell", i, shares, "跳空跌破止损，按开盘并计滑点", b["open"])
            elif b["low"] <= stop:
                order("sell", i, shares, "盘中止损并计滑点", stop)
        if shares and strategy == "averaging_baseline":
            if p["close"] >= entry * 2 or i - held >= 120:
                order("sell", i, shares, "假设基线上涨100%或持有120交易日退出", b["open"])
            elif stage < 3 and p["close"] <= first * (0.85 if stage == 1 else 0.75):
                qty = math.floor(min(cash * 0.98, initial * 0.3) / (b["open"] * (1 + slip)) / 100) * 100
                if order("buy", i, qty, "下跌15%/25%补仓假设基线", b["open"]):
                    stage += 1
        if not shares and signal:
            equity = cash
            stop = min(x["low"] for x in past[-10:])
            risk = b["open"] - stop
            if risk <= 0:
                continue
            value = equity * 0.4 if strategy == "averaging_baseline" else min(equity * 0.15, equity * 0.005 / risk * b["open"])
            qty = math.floor(value / (b["open"] * (1 + slip)) / 100) * 100
            if order("buy", i, qty, "前日收盘确认，次日开盘执行", b["open"]):
                first, stage = b["open"], 1
        curve.append({"date": b["date"], "equity": cash + shares * b["close"]})
    final = cash + shares * bars[-1]["close"]
    peak, drawdown = initial, 0
    for row in curve:
        peak = max(peak, row["equity"])
        drawdown = min(drawdown, row["equity"] / peak - 1)
    years = max((datetime.fromisoformat(bars[-1]["date"]) - datetime.fromisoformat(bars[65]["date"])).days / 365.25, 1 / 365.25)
    return {"state": "exploratory", "strategy": strategy, "start": bars[65]["date"], "end": bars[-1]["date"], "initial_equity": initial,
            "final_equity": round(final, 2), "return_pct": (final / initial - 1) * 100, "annualized_pct": ((final / initial) ** (1 / years) - 1) * 100,
            "max_drawdown_pct": drawdown * 100, "buy_hold_return_pct": (bars[-1]["close"] / bars[65]["open"] - 1) * 100,
            "trades": trades, "open_shares": shares, "curve": curve[::max(1, len(curve) // 100)],
            "assumptions": {"commission": commission, "minimum_commission": 5, "sell_tax": sell_tax, "slippage_per_side": slip, "t_plus_one": True, "signal_execution": "次日开盘", "single_stock_position_cap": "趋势策略15%；补仓对照基线40/30/30假设"},
            "limitations": ["当前归档公司样本，存在幸存者与选择偏差，不能代表全市场", "日线不能重建盘中排队；仅保守阻止一字涨跌停成交", "前复权价格不是逐日历史复权因子，企业行动与真实成交金额可能有偏差", "手续费、税费、滑点是可调整研究假设，不是券商实际收费", "基本面事件最早使用报告生成与公告披露两者中较晚时间，避免回填评级产生前视偏差", "完整40/30/30信号加仓和多股票组合尚需独立组合回测，单股票结果不证明策略有效"]}


def run_backtests(store):
    import json
    from .core import atomic_json, SITE
    results = []
    for row in store.rows("SELECT code,payload FROM snapshots"):
        snapshot = json.loads(row["payload"])
        prices = snapshot.get("prices") or {}
        bars = prices.get("bars", [])
        if len(bars) < 100:
            continue
        code = row["code"]
        board = "star" if code.startswith("688") else "chinext" if code.startswith("30") else "beijing" if snapshot.get("market") == "BJ" else "main"
        reports = [json.loads(r["payload"]) for r in store.rows("SELECT payload FROM reports WHERE json_extract(payload,'$.code')=?", (code,))]
        events = [{"known_at": max(r["generated_at"], r["disclosed_at"]), "eligible": r["rating"] == "buy" and not r["test_mode"]} for r in reports]
        for strategy in ("averaging_baseline", "pullback", "fundamental_trend"):
            result = simulate(bars, strategy, events=events, board=board)
            result["code"] = code
            result["adjustment"] = prices.get("adjustment")
            results.append(result)
    payload = {"generated_at": stamp(), "state": "available" if results else "waiting_for_price_history", "scope": "探索性单股票历史重放，不是经过验证的全市场业绩", "results": results}
    store.execute("INSERT OR REPLACE INTO backtests VALUES(?,?,?)", (now().date().isoformat(), stamp(), dumps(payload)))
    atomic_json(SITE / "backtests.json", payload)
    return {"state": payload["state"], "result_count": len(results)}
