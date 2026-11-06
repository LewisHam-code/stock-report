"""Local watchlist and queue priority; stock interests never leave this host."""
from __future__ import annotations

import re

from market_labels import market_name
from .core import ROOT, SETTINGS_LOCK, ServiceError, Store, atomic_json, config

MAX_WATCHED = 50


def watched_codes(cfg=None):
    cfg = cfg if cfg is not None else config()
    items = cfg.get("watchlist", [])
    if not isinstance(items, list):
        return []
    return [item["code"] for item in items if isinstance(item, dict) and re.fullmatch(r"\d{6}", str(item.get("code", "")))]


def list_watched(store=None):
    owned = store is None
    store = store or Store()
    try:
        items = config().get("watchlist", [])
        if not isinstance(items, list):
            items = []
        result = []
        for item in items:
            code = item.get("code", "")
            from .securities import lookup
            current = lookup(code) or {}
            rows = store.rows("""SELECT d.company,d.market,d.period,d.published_at,d.state,
                (SELECT r.id FROM reports r WHERE r.document_id=d.id ORDER BY r.created_at DESC LIMIT 1) AS report_id
                FROM documents d WHERE d.code=? ORDER BY d.published_at DESC,d.id DESC LIMIT 1""", (code,))
            latest = rows[0] if rows else None
            market = latest["market"] if latest else item.get("market", "")
            result.append({"code": code, "company": current.get('company') or (latest["company"] if latest else item.get("company", "名称待核实")),
                           "market": market, "market_name": market_name(market, code) if market else None,
                           "security_status":current.get('status'),"risk_flag":current.get('risk_flag'),
                           "added_at": item.get("added_at", ""), "latest_filing": latest})
        return result
    finally:
        if owned:
            store.conn.close()


def _resolve(store, term):
    text = str(term).strip()
    if not 2 <= len(text) <= 32 or any(ord(c) < 32 for c in text):
        raise ServiceError("invalid_watch_target", "请输入6位股票代码或完整公司名称")
    from .securities import lookup,resolve,search
    local=search(text,exact=True)
    if local:
        return resolve(store,text)
    if re.fullmatch(r"\d{6}", text):
        rows = store.rows("SELECT code,company,market FROM documents WHERE code=? ORDER BY published_at DESC,id DESC LIMIT 1", (text,))
        if rows:
            row = rows[0]
            if market_name(row["market"], text):
                return row
        return resolve(store,text)
    if re.fullmatch(r"\d+", text):
        raise ServiceError("invalid_watch_target", "股票代码应为6位数字")
    rows = store.rows("""SELECT d.code,d.company,d.market FROM documents d
        JOIN (SELECT code,MAX(published_at) AS latest FROM documents GROUP BY code) recent
        ON d.code=recent.code AND d.published_at=recent.latest WHERE d.company=?
        GROUP BY d.code ORDER BY d.code LIMIT 3""", (text,))
    if not rows:
        return resolve(store,text)
    if len(rows) != 1:
        raise ServiceError("watch_name_ambiguous", "该名称匹配多个股票代码，请使用6位股票代码")
    current=lookup(rows[0]['code'])
    if current and current['status']=='delisted':raise ServiceError('security_delisted','该股票已被官方确认退市')
    return current or rows[0]


def change_watchlist(action, value, store=None):
    if action not in ("add", "remove"):
        raise ServiceError("invalid_watch_action", "关注操作不合法")
    owned = store is None
    store = store or Store()
    try:
        resolved = _resolve(store,value) if action=='add' else None
        with SETTINGS_LOCK:
            cfg = config()
            items = cfg.get("watchlist", [])
            if not isinstance(items, list):
                items = []
            unwatched = cfg.get("pdf_unwatched_at")
            if not isinstance(unwatched, dict):
                unwatched = {}
                cfg["pdf_unwatched_at"] = unwatched
            if action == "add":
                code = resolved["code"]
                if any(item.get("code") == code for item in items):
                    raise ServiceError("already_watched", "这只股票已经在关注列表中")
                if len(items) >= MAX_WATCHED:
                    raise ServiceError("watchlist_limit", "最多关注50只股票")
                from .core import stamp
                items.append({"code": code, "company": resolved["company"], "market": resolved["market"], "added_at": stamp()})
                unwatched.pop(code, None)
            else:
                code = str(value).strip()
                if not re.fullmatch(r"\d{6}", code) or not any(item.get("code") == code for item in items):
                    raise ServiceError("watch_not_found", "关注列表中没有这只股票")
                items = [item for item in items if item.get("code") != code]
                from .core import stamp
                unwatched[code] = stamp()
            cfg["watchlist"] = items
            atomic_json(ROOT / "automation.json", cfg)
            from .vault_archive import sync_watchlist
            sync_watchlist(cfg)
        return list_watched(store)
    finally:
        if owned:
            store.conn.close()
