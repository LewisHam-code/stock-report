"""Loopback-only settings/control API. No secrets or filesystem are served."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, unquote
from pathlib import Path

from .core import ServiceError, Store, public_settings, remove_model_profile, save_model_order, save_settings, stamp
from .models import connection_test, list_models
from .watchlist import change_watchlist, list_watched

PORT = int(os.environ.get('STOCK_PORT', '8876'))
ORIGINS = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "StockLocal"

    def log_message(self, *args):
        pass  # Request bodies and headers may contain a secret; do not log them.

    def send_site_file(self, root=None):
        from .core import PROJECT
        base = PROJECT / 'stock-site'
        route = unquote(urlparse(self.path).path).lstrip('/') or 'index.html'
        if '\\' in route or any(part.startswith('.') for part in Path(route).parts):
            return self.send_json({'error':'未找到文件'}, 404)
        # Only site assets, never the project root, config, archives or database.
        candidate = base / route
        if root and route.startswith(('data/', 'analysis/')):
            base = Path(root)
            candidate = base / route
        try:
            target = candidate.resolve(strict=True)
            target.relative_to(base.resolve())
            if target.suffix not in ('.html','.css','.js','.json','.png','.svg','.webp') or not target.is_file():
                raise ValueError('unsupported asset')
            body = target.read_bytes()
            if getattr(self.server, 'demo', False) and target.suffix == '.html':
                banner = '<div role="note" style="position:sticky;top:0;z-index:9999;padding:10px 20px;background:#fff3d6;color:#6c4d14;text-align:center;font:14px/1.5 system-ui">只读演示 · 公司、代码对应关系、金额、评分与价格均为虚构样例，非真实财报或投资建议。</div>'
                body = re.sub(rb'(<body\b[^>]*>)',lambda m:m.group(1)+banner.encode(),body,count=1)
        except (OSError, ValueError):
            return self.send_json({'error':'未找到文件'}, 404)
        self.send_response(200)
        self.send_header('Content-Type', (mimetypes.guess_type(str(target))[0] or 'application/octet-stream')+'; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        self.wfile.write(body)

    def trusted(self):
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "")
        return host in (f"localhost:{PORT}", f"127.0.0.1:{PORT}") and (not origin or origin in ORIGINS)

    def send_json(self, data, status=200):
        encoded = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        origin = self.headers.get("Origin", "")
        if origin in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_OPTIONS(self):
        if not self.trusted():
            return self.send_json({"error": "请求来源不受信任"}, 403)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", ""))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Stock-Research")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):
        if not self.trusted():
            return self.send_json({"error": "请求来源不受信任"}, 403)
        try:
            self.handle_get()
        except ServiceError as e:
            self.send_json({"ok": False, "code": e.code, "error": str(e)}, 400)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.send_json({"ok": False, "code": "local_status_unavailable", "error": "本机状态暂不可用，请稍后刷新"}, 503)
        except Exception:
            self.send_json({"ok": False, "code": "local_status_error", "error": "本机状态读取失败"}, 500)

    def handle_get(self):
        parsed = urlparse(self.path)
        if parsed.path in ('/api/securities/search','/api/securities/display'):
            from .securities import search,lookup,load,STATUS
            query=parse_qs(parsed.query)
            catalog=load()[0]
            if parsed.path.endswith('/search'):
                term=query.get('q',[''])[0]
                rows=search(term) if 1 <= len(term) <= 100 else []
            else:
                codes=query.get('code',[])[:100]
                rows=[row for code in codes if __import__('re').fullmatch(r'\d{6}',code) and (row:=lookup(code))]
            return self.send_json({'ok':True,'read_warning':bool(catalog.get('read_error')),
                                   'catalog_updated_at':catalog.get('updated_at'),
                                   'items':[{key:row.get(key) for key in ('code','market','company','full_name','status','risk_flag','verified_at')}
                                                    | {'status_label':STATUS.get(row['status'],'待核实')} for row in rows]})
        if parsed.path == "/api/runtime":
            from .deployment import runtime_status
            return self.send_json(runtime_status())
        if parsed.path == '/api/historical-reports':
            from .annual_batch import sync_report_library
            from .historical_reports import feed
            sync_report_library()
            return self.send_json(feed(parse_qs(parsed.query).get('revision',[None])[0]))
        if parsed.path == '/api/sources':
            from .information_sources import source_policy
            store=Store()
            try:return self.send_json({'ok':True, **source_policy(store)})
            finally:store.conn.close()
        if parsed.path == '/api/company-information':
            from .information_sources import company_information
            code=parse_qs(parsed.query).get('code',[''])[0]
            if not __import__('re').fullmatch(r'\d{6}',code):
                raise ServiceError('invalid_stock_code','请输入有效的6位股票代码')
            store=Store()
            try:
                rows=store.rows('SELECT company,market FROM documents WHERE code=? ORDER BY published_at DESC LIMIT 1',(code,))
                if not rows:raise ServiceError('unknown_stock','本地尚未收录该股票')
                return self.send_json({'ok':True, **company_information(store,code,rows[0]['market'],rows[0]['company'])})
            finally:store.conn.close()
        if parsed.path == "/api/models":
            query = parse_qs(parsed.query)
            try:
                return self.send_json(list_models(query.get("provider", ["deepseek"])[0], query.get("profile_id", [None])[0]))
            except ServiceError as e:
                return self.send_json({"ok": False, "code": e.code, "error": str(e)}, 400)
        if parsed.path == "/api/tracking/prices":
            from .tracking import current_quotes
            return self.send_json({"ok": True, "quotes": current_quotes(parse_qs(parsed.query).get("code", []))})
        if self.path == "/api/settings":
            self.send_json(public_settings())
        elif self.path == "/api/watchlist":
            self.send_json({"ok": True, "items": list_watched()})
        elif self.path == "/api/health":
            self.send_json({"ok": True, "service": "stock-local-settings", "asof": stamp(), "independent_runtime": "codex-runtimes" not in sys.base_prefix,
                            "managed_service": os.environ.get("STOCK_MANAGED_SERVICE") == "1"})
        elif self.path == "/api/status":
            store = Store()
            try:
                self.send_json({"run": store.rows("SELECT started_at,finished_at,state,summary FROM runs ORDER BY id DESC LIMIT 1"), "settings": public_settings()})
            finally:
                store.conn.close()
        else:
            if not parsed.path.startswith('/api/'):
                return self.send_site_file()
            self.send_json({"error": "未找到接口"}, 404)

    def do_POST(self):
        if not self.trusted() or self.headers.get("X-Stock-Research") != "1" or self.headers.get_content_type() != "application/json":
            return self.send_json({"error": "请求来源或格式不合法"}, 403)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 16384:
                return self.send_json({"error": "请求大小不合法"}, 400)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                return self.send_json({"error": "请求格式不合法"}, 400)
            if self.path == "/api/runtime":
                from .deployment import save_schedule
                return self.send_json({"ok": True, "runtime": save_schedule(data)})
            if self.path == '/api/current-batch/stop':
                from .current_batch import stop
                return self.send_json({'ok':True,'current_batch':stop()})
            if self.path == '/api/annual-batch/stop':
                from .annual_batch import stop
                return self.send_json({'ok':True,'annual_batch':stop()})
            if self.path == '/api/annual-batch/archive':
                from .annual_batch import archive
                return self.send_json({'ok':True,'annual_batch':archive()})
            if self.path == "/api/settings":
                settings = save_settings(data)
                store = Store()
                try:
                    selected = next((p for p in settings["profiles"] if p["id"] == data.get("id")),None)
                    selected = selected or next((p for p in settings["profiles"] if p["provider"] == data.get("provider") and p["model"] == data.get("model") and p["api_protocol"] == data.get("api_protocol", "anthropic" if data.get("provider")=="anthropic" else "openai") and (not data.get("base_url") or p["base_url"]==data["base_url"].rstrip('/'))), None)
                    if selected:
                        store.set("ai_circuit_" + selected["id"], {})
                finally:
                    store.conn.close()
                return self.send_json({"ok": True, "message": "已保存至本机私密配置", "settings": settings})
            if self.path == "/api/settings/order":
                return self.send_json({"ok": True, "message": "已保存启用模型和备用顺序", "settings": save_model_order(data)})
            if self.path == "/api/settings/remove":
                return self.send_json({"ok": True, "message": "模型配置已移出调用列表；本机密钥与历史报告保留", "settings": remove_model_profile(data)})
            if self.path == "/api/watchlist":
                return self.send_json({"ok": True, "items": change_watchlist(data.get("action"), data.get("value")),
                                       "message": "关注列表已保存；新发现的财报将在现有队列中优先处理"})
            if self.path == "/api/models":
                key = str(data.get("api_key", "")).strip()
                if key and (len(key) < 16 or len(key) > 512 or any(c.isspace() for c in key)):
                    raise ServiceError("invalid_key_format", "密钥格式不正确")
                return self.send_json(list_models(data.get("provider"), data.get("profile_id"), key or None,
                                                  data.get("base_url"),data.get("api_protocol"),data.get("base_url_confirmed") is True))
            if self.path == "/api/settings/test":
                return self.send_json(connection_test(data.get("profile_id")))
            if self.path == "/api/run":
                from .pipeline import run
                threading.Thread(target=run, kwargs={"skip_scan": False}, daemon=True).start()
                return self.send_json({"ok": True, "message": "已请求运行，将自动跳过重复任务"}, 202)
            return self.send_json({"error": "未找到接口"}, 404)
        except ServiceError as e:
            return self.send_json({"ok": False, "code": e.code, "error": str(e)}, 400)
        except (ValueError, UnicodeDecodeError):
            return self.send_json({"ok": False, "error": "设置内容格式不正确"}, 400)
        except Exception:
            return self.send_json({"ok": False, "error": "本机设置服务发生错误，请查看运行状态"}, 500)


class ResearchServer(ThreadingHTTPServer):
    def service_actions(self):
        if os.environ.get('STOCK_FOREGROUND') == '1':
            from .core import config
            cfg = config()
            if not cfg.get('automatic_enabled', False):return
            at = time.monotonic()
            if at < getattr(self, '_foreground_next', 0):return
            if getattr(self, '_foreground_thread', None) and self._foreground_thread.is_alive():return
            self._foreground_next = at + max(5, cfg.get('interval_minutes', 60))*60
            from .pipeline import run
            self._foreground_thread = threading.Thread(target=run, daemon=True)
            self._foreground_thread.start()
            return
        # Reuse the existing loopback service. No new startup job or resident
        # process; the bounded worker exists only during a due hourly pass.
        at=time.monotonic()
        if at-getattr(self,'_tracking_poll',-60)<30:return
        self._tracking_poll=at
        from . import core,job_schedule
        if job_schedule.enabled(core.config()):
            from .scheduled_jobs import poll
            poll(self,at)
            return
        if getattr(self,'_tracking_thread',None) and self._tracking_thread.is_alive():return
        if at<getattr(self,'_tracking_next',0):return
        from .core import config,now
        from .tracking_market import home,read,fresh,background_refresh
        if not config().get('automatic_enabled',False):return
        if fresh(read(home()/'控制.json').get('last_started_at'),now()):return
        self._tracking_next=at+3600
        self._tracking_thread=threading.Thread(target=background_refresh,name='tracking-hourly',daemon=True)
        self._tracking_thread.start()

def serve():
    server = ResearchServer(("127.0.0.1", PORT), Handler)
    print("股票本机设置服务已就绪，仅监听回环地址", flush=True)
    server.serve_forever(poll_interval=1)
