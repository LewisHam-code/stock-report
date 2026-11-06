"""Portable, per-company Obsidian records for price follow-up."""
from __future__ import annotations

import json
import re

from .core import VAULT, atomic_json, atomic_text, config, dumps, finite

MARKER_START = "<!-- STOCK_TRACKING_V1\n"
MARKER_END = "\nSTOCK_TRACKING_V1 -->"
NAME = "解读追踪-自动生成.md"


def _safe(value):
    return str(value if value is not None else "—").replace("|", "∣").replace("\n", " ")


def restore(store, baseline_path, vault=None):
    """When the SQLite index is rebuilt, restore facts from vault notes once."""
    if store.setting("tracking_notes_imported_v1", False):
        return
    vault = vault or VAULT
    recovered = {}
    for note in vault.glob("30_公司/*/*/" + NAME):
        try:
            raw = note.read_text().split(MARKER_START, 1)[1].split(MARKER_END, 1)[0]
            data = json.loads(raw)
            code = note.parent.name
            if data.get("code") != code or not re.fullmatch(r"\d{6}", code):
                continue
            local_baselines = {b["report_id"]: b for b in data.get("baselines", [])
                               if b.get("code") == code and b.get("report_id")}
            recovered.update(local_baselines)
            for row in data.get("closes", []):
                if row.get("code") == code and finite(row.get("close")) is not None:
                    store.execute("INSERT OR IGNORE INTO tracking_watch_closes VALUES(?,?,?,?,?)",
                                  (code, row["trade_date"], row["close"], row["source"], row["recorded_at"]))
            for row in data.get("outcomes", []):
                if row.get("report_id") in local_baselines and row.get("horizon") in (3, 7, 15, 30, 60, 90):
                    store.execute("INSERT OR IGNORE INTO tracking_outcomes VALUES(?,?,?,?)",
                                  (row["report_id"], row["horizon"], dumps(row["payload"]), row["observed_at"]))
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            continue
    if recovered:
        try:
            old = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
        except (OSError, ValueError):
            old = {}
        for report_id, baseline in recovered.items():
            old.setdefault(report_id, baseline)
        atomic_json(baseline_path, old)
    store.set("tracking_notes_imported_v1", True)


def sync(store, baselines, vault=None):
    """Write one small note per stock; unchanged notes incur no disk write."""
    vault = vault or VAULT
    markets = {row["code"]: row["market"] for row in store.rows("SELECT code,market FROM documents GROUP BY code")}
    watched = {}
    for item in config().get("watchlist", []):
        if isinstance(item, dict) and item.get("market") in ("SH", "SZ", "BJ"):
            markets.setdefault(item.get("code"), item["market"])
            watched[item.get("code")] = item
    by_code = {}
    for baseline in baselines.values():
        by_code.setdefault(baseline["code"], {"baselines": [], "closes": [], "outcomes": []})["baselines"].append(baseline)
    for row in store.rows("SELECT code,trade_date,close,source,recorded_at FROM tracking_watch_closes ORDER BY code,trade_date"):
        by_code.setdefault(row["code"], {"baselines": [], "closes": [], "outcomes": []})["closes"].append(row)
    report_codes = {baseline["report_id"]: baseline["code"] for baseline in baselines.values()}
    for row in store.rows("SELECT report_id,horizon,payload,observed_at FROM tracking_outcomes ORDER BY report_id,horizon"):
        code = report_codes.get(row["report_id"])
        if code:
            by_code[code]["outcomes"].append({"report_id": row["report_id"], "horizon": row["horizon"],
                                                 "payload": json.loads(row["payload"]), "observed_at": row["observed_at"]})
    for code, facts in by_code.items():
        market = markets.get(code)
        if not market:
            continue
        note = vault / "30_公司" / market / code / NAME
        existing = note.read_text() if note.exists() else ""
        if existing and MARKER_START not in existing:
            continue  # Never overwrite an unrelated user note.
        personal = existing.split("\n## 我的备注\n", 1)[1] if "\n## 我的备注\n" in existing else ""
        latest = max(facts["baselines"], key=lambda b: b.get("generated_at") or "") if facts["baselines"] else {}
        company = latest.get("company") or watched.get(code, {}).get("company") or code
        last_close = facts["closes"][-1] if facts["closes"] else {}
        lines = ["---", 'type: "tracking"', f'code: "{code}"', f'exchange: "{market}"',
                 "company: " + json.dumps(company, ensure_ascii=False),
                 "watched: " + ("true" if code in watched else "false"),
                 "latest_rating: " + json.dumps(latest.get("rating"), ensure_ascii=False),
                 "latest_score: " + dumps(latest.get("score")),
                 "last_close: " + dumps(last_close.get("close")),
                 "last_close_date: " + json.dumps(last_close.get("trade_date"), ensure_ascii=False),
                 "---",
                 f"# {_safe(company)} · 解读追踪", "", "系统记录发布时判断、关注期间收盘价和观察期结果。个人内容请写在末尾备注区。", "",
                 "## 报告发布基准", "", "| 报告ID | 解读时间 | 评级 | 评分 | 解读时价格 | 价格时间 |", "|---|---|---|---:|---:|---|"]
        for b in facts["baselines"]:
            lines.append("| " + " | ".join(_safe(b.get(k)) for k in ("report_id", "generated_at", "rating", "score", "price", "price_asof")) + " |")
        lines += ["", "## 关注期间每日收盘价", "", "| 交易日 | 收盘价 | 行情来源 |", "|---|---:|---|"]
        for row in facts["closes"]:
            lines.append("| " + " | ".join(_safe(row.get(k)) for k in ("trade_date", "close", "source")) + " |")
        lines += ["", "## 到期观察结果", "", "| 报告ID | 交易日数 | 状态 | 入场日期 | 结束日期 | 个股收益% | 沪深300收益% |", "|---|---:|---|---|---|---:|---:|"]
        for row in facts["outcomes"]:
            outcome = row["payload"]
            lines.append("| " + " | ".join(_safe(value) for value in
                (row["report_id"], row["horizon"], outcome.get("state"), outcome.get("entry_date"), outcome.get("end_date"),
                 outcome.get("return_pct"), outcome.get("benchmark_pct"))) + " |")
        machine_data = dumps({"version": 1, "code": code, **facts}).replace("-->", "--\\u003e")
        lines += ["", MARKER_START + machine_data + MARKER_END,
                  "", "## 我的备注", personal.rstrip(), ""]
        content = "\n".join(lines)
        if content != existing:
            atomic_text(note, content)
