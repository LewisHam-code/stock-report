#!/usr/bin/env python3
"""Single local entry point for settings, scheduled collection and research."""
import argparse
import json

from stock_research.core import ServiceError, Store, public_settings


def main():
    parser = argparse.ArgumentParser(description="股票财报自动采集解读系统")
    parser.add_argument("command", choices=("run", "scheduled", "serve", "status", "test", "backtest", "install-service", "archive", "verify-archive", "foundation", "annual-init", "annual-tick", "annual-stop"), nargs="?", default="run")
    parser.add_argument("--skip-scan", action="store_true")
    parser.add_argument("--skip-ai", action="store_true")
    parser.add_argument("--max-analysis", type=int)
    args = parser.parse_args()
    try:
        if args.command == "serve":
            from stock_research.service import serve
            serve()
            return
        if args.command == "install-service":
            from stock_research.deployment import install_service
            result = install_service()
        elif args.command in ('annual-init','annual-tick','annual-stop'):
            from stock_research.core import run_lock
            from stock_research import annual_batch
            # Stop is an admission flag; it must be usable during a live run.
            if args.command=='annual-stop':result=annual_batch.stop()
            else:
                with run_lock():result=annual_batch.initialize() if args.command=='annual-init' else annual_batch.tick()
        elif args.command == 'foundation':
            from stock_research.core import run_lock
            from stock_research.foundation_job import tick
            with run_lock():result=tick()
        elif args.command == "scheduled":
            from stock_research.annual_runner import scheduled
            result = scheduled()
        elif args.command == "status":
            store = Store()
            try:
                result = {"settings": public_settings(), "documents": store.rows("SELECT state,COUNT(*) AS count FROM documents GROUP BY state"), "reports": store.rows("SELECT COUNT(*) AS count FROM reports"), "latest_run": store.rows("SELECT started_at,finished_at,state,summary FROM runs ORDER BY id DESC LIMIT 1")}
            finally:
                store.conn.close()
        elif args.command == 'archive':
            from stock_research.core import run_lock
            from stock_research.vault_archive import sync
            with run_lock():
                store=Store()
                try:result={'ok':True,**sync(store)}
                finally:store.conn.close()
        elif args.command == 'verify-archive':
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from stock_research.vault_archive import restore_isolated
            with TemporaryDirectory(prefix='stock-restore-check-') as temporary:
                result=restore_isolated(Path(temporary)/'restored')
        elif args.command == "test":
            from stock_research.models import connection_test
            result = connection_test()
        elif args.command == "backtest":
            from stock_research.backtest import run_backtests
            store = Store()
            try:
                result = run_backtests(store)
            finally:
                store.conn.close()
        else:
            from stock_research.runtime import ensure_service
            ensure_service()
            from stock_research.pipeline import run
            result = run(skip_scan=args.skip_scan, skip_ai=args.skip_ai, max_analysis=args.max_analysis)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except ServiceError as e:
        print(json.dumps({"ok": False, "code": e.code, "message": str(e)}, ensure_ascii=False))
        return 2
    except KeyboardInterrupt:
        return 0
    except OSError:
        print(json.dumps({"ok": False, "code": "local_service_unavailable", "message": "本机服务无法启动或资源暂不可用"}, ensure_ascii=False))
        return 2
    return 2 if isinstance(result, dict) and result.get("ok") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
