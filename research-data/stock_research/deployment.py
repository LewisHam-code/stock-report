"""User-owned launchd jobs controlled by the local web app; no shell or root."""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .core import PROJECT, ROOT, SETTINGS_LOCK, ServiceError, Store, atomic_json, atomic_text, config, now, run_lock, stamp

SERVICE = "local.stock-research.opensource.service"
WORKER = "local.stock-research.opensource.worker"
INTERVALS = (5, 15, 30, 60, 120, 360, 720, 1440)
AGENTS = Path.home() / "Library" / "LaunchAgents"
LOGS = ROOT / "logs"


def launch(arguments, required=True):
    if sys.platform != 'darwin':
        if required:raise ServiceError('native_schedule_unavailable','此命令需要 macOS；请使用前台启动方式')
        return subprocess.CompletedProcess(arguments, 1, b'', b'')
    result = subprocess.run(["/bin/launchctl", *arguments], capture_output=True, timeout=15)
    if required and result.returncode:
        raise ServiceError("system_schedule_failed", "系统任务设置未成功，请检查设置页运行状态")
    return result


def job_loaded(label):
    if os.environ.get('STOCK_FOREGROUND') == '1':return False
    return launch(["print", f"gui/{os.getuid()}/{label}"], required=False).returncode == 0


def job_spec(label, minutes=60):
    python = PROJECT / ".venv" / "bin" / "python3"
    if not python.is_file():
        raise ServiceError("independent_python_missing", "项目独立运行环境未安装")
    spec = {"Label": label, "ProgramArguments": [str(python), str(ROOT / "run.py"), "serve" if label == SERVICE else "scheduled"],
            "WorkingDirectory": str(PROJECT), "RunAtLoad": True, "ProcessType": "Background", "Umask": 0o077,
            "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "STOCK_MANAGED_SERVICE": "1"},
            "StandardOutPath": str(LOGS / ("service.log" if label == SERVICE else "worker.log")),
            "StandardErrorPath": str(LOGS / ("service-error.log" if label == SERVICE else "worker-error.log"))}
    if label == SERVICE:
        spec.update(KeepAlive=True, ThrottleInterval=30)
    else:
        spec["StartInterval"] = minutes * 60
        # The settings transaction holds the research lock; start after it commits.
        spec["RunAtLoad"] = False
    return spec


def write_job(label, spec):
    AGENTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    LOGS.chmod(0o700)
    path = AGENTS / (label + ".plist")
    if path.is_symlink():
        raise ServiceError("unsafe_startup_target", "启动项不能使用符号链接")
    if path.exists():
        previous = plistlib.loads(path.read_bytes())
        if previous.get("Label") != label or previous.get("ProgramArguments", [])[:2] != spec["ProgramArguments"][:2]:
            raise ServiceError("startup_conflict", "同名启动项属于其他配置，未覆盖")
        backup = ROOT / "backups" / (label + "-before-update.plist")
        if not backup.exists():
            atomic_text(backup, path.read_text())
    atomic_text(path, plistlib.dumps(spec).decode())
    path.chmod(0o600)
    return path


def install_service():
    path = write_job(SERVICE, job_spec(SERVICE))
    if not job_loaded(SERVICE):
        launch(["enable", f"gui/{os.getuid()}/{SERVICE}"])
        launch(["bootstrap", f"gui/{os.getuid()}", str(path)])
    return {"ok": True, "service_installed": job_loaded(SERVICE)}


def schedule_config(cfg=None):
    cfg = cfg or config()
    return {"enabled": cfg.get("automatic_enabled", False), "interval_minutes": cfg.get("interval_minutes", 60),
            "daily_api_calls": cfg["daily_api_calls"], "analysis_per_run": cfg["analysis_per_run"], "download_per_run": cfg["download_per_run"],
            "prefer_off_peak": cfg.get("prefer_off_peak", True),
            "quarter_workers": cfg.get("quarter_workers",2),
            "financial_reading_limit_cny":(cfg.get('financial_reading_cost_policy') or {}).get('limit_cny',.5),
            "narrative_refresh_days": cfg.get("narrative_refresh_days", 0),
            "schedule_policy": cfg.get("schedule_policy")}


def save_schedule(data):
    if not isinstance(data.get("enabled"), bool):
        raise ServiceError("invalid_schedule", "请选择是否开启自动运行")
    for name, valid in (("interval_minutes", lambda v: v in INTERVALS), ("daily_api_calls", lambda v: 0 <= v <= 10000),
                        ("analysis_per_run", lambda v: 0 <= v <= 20), ("download_per_run", lambda v: 0 <= v <= 100)):
        value = data.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not valid(value):
            raise ServiceError("invalid_schedule", "运行频率或处理限额不在允许范围内")
    with SETTINGS_LOCK:
        old = config()
        updated = {**old, **{k: data[k] for k in ("interval_minutes", "daily_api_calls", "analysis_per_run", "download_per_run")}, "automatic_enabled": data["enabled"]}
        refresh_days = data.get("narrative_refresh_days", old.get("narrative_refresh_days", 0))
        if isinstance(refresh_days, bool) or not isinstance(refresh_days, int) or refresh_days not in (0, 7, 30):
            raise ServiceError("invalid_schedule", "请选择仅新财报解读，或每7天、30天重新解读")
        updated["narrative_refresh_days"] = refresh_days
        preference = data.get('prefer_off_peak', old.get('prefer_off_peak', True))
        if not isinstance(preference, bool):
            raise ServiceError('invalid_schedule','请选择模型解读时段')
        updated['prefer_off_peak'] = preference
        workers=data.get('quarter_workers',old.get('quarter_workers',2))
        if isinstance(workers,bool) or not isinstance(workers,int) or not 1<=workers<=20:
            raise ServiceError('invalid_schedule','精读并发数量须为1至20份')
        limit=data.get('financial_reading_limit_cny',(old.get('financial_reading_cost_policy') or {}).get('limit_cny',.5))
        if isinstance(limit,bool) or not isinstance(limit,(int,float)) or not 0<limit<=5:
            raise ServiceError('invalid_schedule','单份累计预估上限须大于0且不超过5元')
        updated['quarter_workers']=workers
        updated['financial_reading_cost_policy']={**(old.get('financial_reading_cost_policy') or {}),'mode':'per_report_cap','limit_cny':limit}
        if os.environ.get('STOCK_FOREGROUND') == '1':
            atomic_json(ROOT / 'automation.json', updated)
            return runtime_status()
        def sync_execution():
            from . import current_batch
            if current_batch.managed():
                with current_batch.edit() as task:task['workers']=workers
        # Execution preferences must be saveable during a paid report. They
        # do not change launchd registration or interrupt in-flight requests.
        if (updated['automatic_enabled']==old.get('automatic_enabled',False)
            and updated['interval_minutes']==old.get('interval_minutes',60)
            and (not updated['automatic_enabled'] or job_loaded(WORKER))):
            atomic_json(ROOT/'automation.json',updated);sync_execution()
            return runtime_status()
        install_service()
        path = AGENTS / (WORKER + ".plist")
        loaded = job_loaded(WORKER)
        # Hold the same lock as research while changing jobs; never terminate a live report.
        try:
            with run_lock():
                if loaded:
                    launch(["bootout", f"gui/{os.getuid()}/{WORKER}"])
                atomic_json(ROOT / "automation.json", updated)
                try:
                    if updated["automatic_enabled"]:
                        write_job(WORKER, job_spec(WORKER, updated["interval_minutes"]))
                        launch(["enable", f"gui/{os.getuid()}/{WORKER}"])
                        launch(["bootstrap", f"gui/{os.getuid()}", str(path)])
                    elif path.exists():
                        launch(["disable", f"gui/{os.getuid()}/{WORKER}"])
                except (ServiceError, OSError, subprocess.TimeoutExpired):
                    atomic_json(ROOT / "automation.json", old)
                    launch(["bootout", f"gui/{os.getuid()}/{WORKER}"], required=False)
                    if loaded and path.exists():
                        write_job(WORKER, job_spec(WORKER, old.get("interval_minutes", 60)))
                        launch(["enable", f"gui/{os.getuid()}/{WORKER}"], required=False)
                        launch(["bootstrap", f"gui/{os.getuid()}", str(path)], required=False)
                    elif path.exists():
                        launch(["disable", f"gui/{os.getuid()}/{WORKER}"], required=False)
                    raise ServiceError("schedule_apply_failed", "定时任务变更失败，已保留原配置") from None
        except ServiceError as e:
            if e.code == "already_running":
                raise ServiceError("research_running", "当前正在处理研究任务，请完成后再修改定时设置，现有任务未中断") from None
            raise
    sync_execution()
    state = runtime_status()
    record_schedule(state)
    # Saving a budget or interval must not silently start another paid round.
    # Only newly enabling/re-registering the schedule requests an immediate run.
    if data["enabled"] and (not old.get("automatic_enabled", False) or not loaded):
        try:
            launch(["kickstart", f"gui/{os.getuid()}/{WORKER}"])
        except (ServiceError, OSError, subprocess.TimeoutExpired):
            state["warning"] = "定时设置已保存，但立即启动未成功；系统将按已登记的间隔重试。"
    return state


def runtime_status():
    cfg = config()
    store = Store()
    try:
        rows = store.rows("SELECT started_at,finished_at,state,summary FROM runs ORDER BY id DESC LIMIT 1")
        today_used = store.rows("SELECT COUNT(*) n FROM api_calls WHERE created_at>=? AND purpose NOT LIKE 'foundation:%' AND purpose NOT LIKE 'benchmark:%'", (now().date().isoformat(),))[0]["n"]
        tokens = store.rows("SELECT COALESCE(SUM(input_tokens),0) input,COALESCE(SUM(output_tokens),0) output FROM api_calls WHERE created_at>=?", (now().date().isoformat(),))[0]
        counts = store.rows("SELECT state,COUNT(*) n FROM documents GROUP BY state")
        latest = rows[0] if rows else None
        if latest:
            summary = json.loads(latest.pop("summary") or "{}")
            latest["counts"] = {k: summary.get(k, 0) for k in ("new", "parsed", "analyzed")}
            latest["blockers"] = summary.get("blockers", [])
            latest["error_codes"] = sorted({e.get("code", "unknown") for e in summary.get("errors", [])})
        from .model_timing import research_window
        from .current_batch import status as current_status
        from . import job_schedule,disclosure_intake
        from .reading_costs import status as cost_status
        intake=disclosure_intake.state() if job_schedule.enabled(cfg) else {}
        return {"settings": schedule_config(cfg), "foreground":os.environ.get('STOCK_FOREGROUND')=='1', "model_time_policy": research_window(cfg), "service_managed": job_loaded(SERVICE), "schedule_registered": job_loaded(WORKER),
                "independent_python": "codex-runtimes" not in sys.base_prefix and (PROJECT / ".venv" / "bin" / "python3").is_file(),
                "queue": counts, "latest_run": latest, "api_calls_today": today_used, "tokens_today": tokens,
                "current_batch":current_status(), "reading_cost_policy":cost_status(cfg), "job_policy":job_schedule.public_policy() if job_schedule.enabled(cfg) else None,
                "disclosure_intake":{k:intake.get(k) for k in ('state','last_started_at','last_completed_at','duration_seconds','pages','examined','new','error')}, "asof": stamp()}
    finally:
        store.conn.close()


def record_schedule(state):
    store = Store()
    try:
        store.set("schedule", {"active": bool(state["settings"]["enabled"] and state["schedule_registered"]), "engine": "launchd",
                              "interval_minutes": state["settings"]["interval_minutes"], "service_managed": state["service_managed"], "checked_at": stamp()})
    finally:
        store.conn.close()
