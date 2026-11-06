from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
SITE = PROJECT / "stock-site" / "data"
VAULT = PROJECT / "storage" / "archive"
TZ = ZoneInfo("Asia/Shanghai")
SETTINGS_LOCK = threading.RLock()
MODEL_STAGES = ("extraction", "deep_analysis")
DEFAULT_MODEL_STAGES = ("extraction", "deep_analysis")
KEY_VARIABLES = {"openai":"OPENAI_API_KEY", "deepseek":"DEEPSEEK_API_KEY", "anthropic":"ANTHROPIC_API_KEY", "gemini":"GEMINI_API_KEY"}


def now():
    return datetime.now(TZ)


def stamp():
    return now().isoformat(timespec="seconds")


def config():
    path = ROOT / "automation.json"
    if not path.exists():
        path = ROOT / "automation.example.json"
    return json.loads(path.read_text())


def read_secret(variable):
    value = os.environ.get(variable, "")
    if not value:
        path = PROJECT / ".env.local"
        if path.is_file() and not path.is_symlink():
            for line in path.read_text().splitlines():
                if line.startswith(variable + "="):
                    value = line.split("=", 1)[1].strip().strip("\"'")
    return value


def normalized_model_stages(stages):
    return list(dict.fromkeys('deep_analysis' if stage=='analysis' else stage for stage in stages))


def model_profiles(cfg=None):
    cfg = cfg or config()
    if "model_profiles" in cfg:
        from .model_endpoints import endpoint_settings
        return [{**p, **endpoint_settings(p["provider"], p.get("api_protocol"), p.get("base_url")), "stages": normalized_model_stages(p.get("stages", list(DEFAULT_MODEL_STAGES)))} for p in cfg["model_profiles"]]
    provider = cfg.get("provider", "deepseek")
    profiles = [{"id": "legacy-" + provider, "provider": provider, "model": cfg["model"], "enabled": True,
                 "test_mode": cfg.get("test_mode", False), "key_env": KEY_VARIABLES.get(provider, "OPENAI_API_KEY")}]
    other = "openai" if provider == "deepseek" else "deepseek"
    variable = "OPENAI_API_KEY" if other == "openai" else "DEEPSEEK_API_KEY"
    if len(read_secret(variable)) >= 16:
        profiles.append({"id": "legacy-" + other, "provider": other, "model": "gpt-5-mini" if other == "openai" else "deepseek-chat",
                         "enabled": False, "test_mode": True, "key_env": variable})
    from .model_endpoints import endpoint_settings
    return [{**p, **endpoint_settings(p["provider"]), "stages": list(DEFAULT_MODEL_STAGES)} for p in profiles]


def api_key(provider=None, profile_id=None):
    # Never source a shell file or print secret-bearing environment variables.
    cfg = config()
    provider = provider or cfg.get("provider", "openai")
    variable = KEY_VARIABLES.get(provider)
    if not variable:
        raise ServiceError("invalid_provider", "不支持的模型服务商")
    if profile_id:
        profile = next((p for p in model_profiles(cfg) if p["id"] == profile_id and p["provider"] == provider), None)
        if not profile:
            raise ServiceError("unknown_profile", "已保存的模型配置不存在，请刷新列表")
        variable = profile.get("key_env", variable)
    else:
        candidates = [p for p in model_profiles(cfg) if p["provider"] == provider]
        candidate = next((p for p in candidates if p["enabled"]), candidates[0] if candidates else None)
        if candidate:
            variable = candidate.get("key_env", variable)
    if not re.fullmatch(r"(?:OPENAI_API_KEY|DEEPSEEK_API_KEY|ANTHROPIC_API_KEY|GEMINI_API_KEY|STOCK_MODEL_[A-F0-9]+_API_KEY)", variable):
        raise ServiceError("unsafe_secret_reference", "模型密钥配置不合法")
    value = read_secret(variable)
    if len(value) < 16:
        raise ServiceError("missing_key", "AI 密钥未配置")
    return value


def public_settings():
    cfg = config()
    providers = {}
    for provider in KEY_VARIABLES:
        providers[provider] = False
    profiles = []
    for p in model_profiles(cfg):
        try:
            api_key(p["provider"], p["id"])
            configured = True
        except ServiceError:
            configured = False
        providers[p["provider"]] = providers[p["provider"]] or configured
        profiles.append({k: p[k] for k in ("id", "provider", "model", "enabled", "test_mode", "stages", "api_protocol", "base_url", "custom_base_url")})
        profiles[-1]["base_url_confirmed"] = bool(p.get("base_url_confirmed"))
        profiles[-1]["configured"] = configured
    from .model_timing import research_window
    return {"provider": cfg.get("provider", "deepseek"), "model": cfg["model"], "test_mode": cfg.get("test_mode", False),
            "reading_mode":"deep",
            "prefer_off_peak": cfg.get('prefer_off_peak', True), "model_time_policy": research_window(cfg, profiles),
            "configured": providers, "profiles": profiles, "routing_mode": "fallback", "enabled_count": sum(p["enabled"] for p in profiles),
            "stage_routes": {stage: [{k:p[k] for k in ("id", "provider", "model", "configured")} for p in profiles if p["enabled"] and stage in p["stages"]] for stage in MODEL_STAGES},
            "daily_api_calls": cfg["daily_api_calls"], "analysis_per_run": cfg["analysis_per_run"]}


def save_settings(data):
    provider = data.get("provider")
    model = str(data.get("model", "")).strip()
    if provider not in KEY_VARIABLES or not re.fullmatch(r"[a-zA-Z0-9._:-]{1,100}", model):
        raise ServiceError("invalid_settings", "请选择服务商并填写有效的模型名称")
    with SETTINGS_LOCK:
        cfg = config()
        profiles = model_profiles(cfg)
        ident = str(data.get("id") or "")
        from .model_endpoints import endpoint_settings
        prior = next((p for p in profiles if p["id"] == ident), {})
        if prior and prior["provider"] != provider:
            raise ServiceError("profile_provider_mismatch", "切换服务商时请新增模型配置，避免复用其他服务商密钥")
        endpoint = endpoint_settings(provider, data.get("api_protocol", prior.get("api_protocol")), data.get("base_url", prior.get("base_url")))
        existing = next((p for p in profiles if p["id"] == ident), None) if ident else next((p for p in profiles if p["provider"] == provider and p["model"] == model and p["api_protocol"] == endpoint["api_protocol"] and p["base_url"] == endpoint["base_url"]), None)
        confirmed = data.get("base_url_confirmed") is True or bool(existing and existing.get("base_url_confirmed") and existing["base_url"] == endpoint["base_url"])
        if endpoint["custom_base_url"] and not confirmed:
            raise ServiceError("custom_destination_confirmation", "自定义地址需确认将本项密钥与公开财报发送至该地址")
        stages = data.get("stages", existing.get("stages", list(DEFAULT_MODEL_STAGES)) if existing else list(DEFAULT_MODEL_STAGES))
        if not isinstance(stages, list) or not stages or any(not isinstance(s, str) or s not in (*MODEL_STAGES,'analysis') for s in stages) or len(stages) != len(set(stages)):
            raise ServiceError("invalid_model_stages", "请选择财务提取或精读用途")
        stages=normalized_model_stages(stages)
        if ident and not existing:
            raise ServiceError("unknown_profile", "已保存的模型不存在，请刷新列表")
        if existing and existing["provider"] != provider:
            raise ServiceError("profile_provider_mismatch", "切换服务商时请新增模型配置，避免复用其他服务商密钥")
        if not existing and len(profiles) >= 20:
            raise ServiceError("profile_limit", "最多保存20项模型配置")
        ident = existing["id"] if existing else "m-" + uuid.uuid4().hex
        key = str(data.get("api_key", "")).strip()
        variable = existing.get("key_env") if existing else None
        if key:
            if len(key) < 16 or len(key) > 512 or not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
                raise ServiceError("invalid_key_format", "密钥格式不正确，未保存")
            # Isolate updates to one profile, including profiles sharing a legacy key.
            variable = "STOCK_MODEL_" + uuid.uuid4().hex.upper() + "_API_KEY"
            target = PROJECT / ".env.local"
            if target.is_symlink():
                raise ServiceError("unsafe_secret_target", "私密配置不能使用符号链接")
            lines = target.read_text().splitlines() if target.exists() else []
            lines.append(variable + "=" + key)
            atomic_text(target, "\n".join(lines) + "\n")
            target.chmod(0o600)
        elif not variable:
            saved_refs = profiles + cfg.get("removed_model_profiles", [])
            shared = next((p.get("key_env") for p in saved_refs if p["provider"] == provider and p["model"] == model and len(read_secret(p.get("key_env", ""))) >= 16), None)
            shared = shared or next((p.get("key_env") for p in saved_refs if p["provider"] == provider and len(read_secret(p.get("key_env", ""))) >= 16), None)
            variable = shared or KEY_VARIABLES[provider]
        profile = {"id": ident, "provider": provider, "model": model, "enabled": bool(data.get("enabled", existing["enabled"] if existing else True)),
                   "test_mode": bool(data.get("test_mode", False)), "key_env": variable, "stages": stages, **endpoint, "base_url_confirmed": confirmed}
        if existing:
            profiles[profiles.index(existing)] = profile
        else:
            if len(profiles) >= 20:
                raise ServiceError("profile_limit", "最多保存20项模型配置")
            profiles.append(profile)
        _save_profiles(cfg, profiles)
        return public_settings()


def _save_profiles(cfg, profiles):
    first = next((p for p in profiles if p["enabled"]), None)
    cfg["model_profiles"] = profiles
    cfg["routing_mode"] = "fallback"
    if first:
        cfg.update(provider=first["provider"], model=first["model"], test_mode=first["test_mode"])
    atomic_json(ROOT / "automation.json", cfg)


def save_model_order(data):
    submitted = data.get("profiles")
    if not isinstance(submitted, list) or not all(isinstance(p, dict) and isinstance(p.get("enabled"), bool) for p in submitted):
        raise ServiceError("invalid_model_order", "模型勾选与排序格式不正确")
    with SETTINGS_LOCK:
        cfg = config()
        existing = {p["id"]: p for p in model_profiles(cfg)}
        ids = [p.get("id") for p in submitted]
        if len(ids) != len(set(ids)) or set(ids) != set(existing):
            raise ServiceError("stale_model_order", "模型列表已变化，请刷新后重新勾选排序")
        profiles = [{**existing[p["id"]], "enabled": p["enabled"]} for p in submitted]
        _save_profiles(cfg, profiles)
        return public_settings()


def remove_model_profile(data):
    ident = data.get("id")
    if not isinstance(ident, str) or not ident:
        raise ServiceError("invalid_profile", "请选择要删除的模型配置")
    with SETTINGS_LOCK:
        cfg = config()
        profiles = model_profiles(cfg)
        selected = next((p for p in profiles if p["id"] == ident), None)
        if not selected:
            raise ServiceError("unknown_profile", "该模型配置已不存在，请刷新列表")
        # Keep references locally for reversible re-adding; never return them or secrets.
        cfg.setdefault("removed_model_profiles", []).append({**selected, "enabled": False, "removed_at": stamp()})
        _save_profiles(cfg, [p for p in profiles if p["id"] != ident])
        return public_settings()


class ServiceError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def atomic_json(path, value):
    atomic_text(path, dumps(value))


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".stock-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def finite(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def pct_change(current, previous):
    a, b = finite(current), finite(previous)
    # A negative or zero base is a turnaround, not a comparable growth rate.
    return (a / b - 1) * 100 if a is not None and b is not None and b > 0 else None


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, code TEXT NOT NULL, company TEXT NOT NULL,
 market TEXT NOT NULL, period TEXT NOT NULL, title TEXT NOT NULL,
 published_at TEXT NOT NULL, source_url TEXT NOT NULL,
 pdf TEXT, sha256 TEXT, text_file TEXT, page_count INTEGER,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_retry TEXT, error_code TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_document_queue ON documents(state,next_retry,published_at DESC);
CREATE INDEX IF NOT EXISTS idx_company_period ON documents(code,period,published_at);
CREATE TABLE IF NOT EXISTS reports (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 version TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(document_id,version));
CREATE TABLE IF NOT EXISTS snapshots (
 code TEXT PRIMARY KEY, asof TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tracking_outcomes (
 report_id TEXT NOT NULL, horizon INTEGER NOT NULL, payload TEXT NOT NULL,
 observed_at TEXT NOT NULL, PRIMARY KEY(report_id,horizon));
CREATE TABLE IF NOT EXISTS tracking_watch_closes (
 code TEXT NOT NULL, trade_date TEXT NOT NULL, close REAL NOT NULL,
 source TEXT NOT NULL, recorded_at TEXT NOT NULL, PRIMARY KEY(code,trade_date));
CREATE TABLE IF NOT EXISTS cache (
 key TEXT PRIMARY KEY, expires_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
 state TEXT NOT NULL, summary TEXT);
CREATE TABLE IF NOT EXISTS api_calls (
 id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, purpose TEXT NOT NULL,
 state TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS backtests (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
"""


class Store:
    def __init__(self, path=None):
        # Separate new database: the pilot database is imported read-only.
        path = Path(path or ROOT / "db" / "automation.sqlite")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        path.chmod(0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def execute(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    def rows(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args)]

    def setting(self, key, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dumps(value)))

    def reserve_api_call(self, purpose, limit):
        # A connection check and a worker can run in separate processes. Reserve
        # the budget in one SQLite write transaction before contacting a model.
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            used = self.api_calls_today()
            if limit > 0 and used >= limit:
                raise ServiceError("daily_api_limit", "已达到本地每日模型调用上限，次日续跑")
            call_id = self.conn.execute("INSERT INTO api_calls(created_at,purpose,state) VALUES(?,?,'started')", (stamp(), purpose)).lastrowid
            self.conn.commit()
            return call_id
        except Exception:
            self.conn.rollback()
            raise

    def api_calls_today(self):
        start = now().date()
        return self.conn.execute("SELECT COUNT(*) FROM api_calls WHERE created_at>=? AND created_at<? AND purpose NOT LIKE 'foundation:%' AND purpose NOT LIKE 'benchmark:%'",
                                 (start.isoformat(), (start + timedelta(days=1)).isoformat())).fetchone()[0]

    def cached(self, key):
        r = self.conn.execute("SELECT payload FROM cache WHERE key=? AND expires_at>?", (key, stamp())).fetchone()
        return json.loads(r[0]) if r else None

    def cache(self, key, value, hours=6):
        expiry = (now() + timedelta(hours=hours)).isoformat(timespec="seconds")
        self.execute("INSERT INTO cache VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET expires_at=excluded.expires_at,payload=excluded.payload", (key, expiry, dumps(value)))

    def add_document(self, doc):
        keys = ("id", "code", "company", "market", "period", "title", "published_at", "source_url", "pdf", "sha256")
        if not re.fullmatch(r"\d{6}", doc["code"]) or not re.fullmatch(r"(?:\d{8,13}|(?:sse|bse)-[a-f0-9]{24})", doc["id"]):
            raise ValueError("Invalid document identity")
        return self.execute(
            "INSERT OR IGNORE INTO documents(" + ",".join(keys) + ",created_at) VALUES(" + ",".join("?" for _ in range(11)) + ")",
            tuple(doc.get(k) for k in keys) + (stamp(),),
        ).rowcount

    def import_pilot(self):
        source = ROOT / "db" / "market.sqlite"
        if not source.exists():
            return 0
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as old:
            old.row_factory = sqlite3.Row
            count = 0
            for r in old.execute("SELECT f.*, c.name,c.market FROM filings f JOIN companies c ON f.code=c.code"):
                count += self.add_document(dict(id=r["announcement_id"], code=r["code"], company=r["name"], market=r["market"], period=r["fiscal_period"], title=r["title"], published_at=r["disclosed_at"], source_url=r["source_url"], pdf=r["local_pdf"], sha256=r["pdf_sha256"]))
        return count


@contextmanager
def run_lock():
    import fcntl
    lock = ROOT / "db" / "pipeline.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ServiceError("already_running", "已有采集任务正在运行，本次跳过")
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
