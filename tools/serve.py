#!/usr/bin/env python3
"""Start one foreground, loopback-only site/API; no startup jobs are installed."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description='股票财报自动采集解读系统 · 本地运行')
    parser.add_argument('--port', type=int, default=8876)
    parser.add_argument('--demo', action='store_true', help='仅显示虚构示例；禁止写入和模型调用')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:parser.error('端口须在1024至65535之间')
    os.environ['STOCK_PORT'] = str(args.port)
    os.environ['STOCK_FOREGROUND'] = '1'
    sys.path.insert(0, str(PROJECT / 'research-data'))
    from stock_research.service import Handler, ResearchServer
    from stock_research import core

    class DemoHandler(Handler):
        def handle_get(self):
            route = urlparse(self.path).path
            fixtures = PROJECT / 'examples' / 'demo-site'
            mapping = {'/api/watchlist':'watchlist', '/api/runtime':'runtime',
                       '/api/settings':'settings', '/api/sources':'sources',
                       '/api/historical-reports':'historical-reports',
                       '/api/securities/display':'securities', '/api/securities/search':'securities'}
            if route == '/api/health':return self.send_json({'ok':True,'service':'stock-demo','demo':True})
            if route in mapping:
                return self.send_json(json.loads((fixtures/'api'/(mapping[route]+'.json')).read_text()))
            if route.startswith('/api/'):return self.send_json({'error':'演示模式没有此接口'},404)
            return self.send_site_file(fixtures)

        def do_POST(self):
            return self.send_json({'error':'只读演示：请使用不带 --demo 的命令配置自己的数据'},405)

    class DemoServer(ResearchServer):
        def service_actions(self):pass  # No background jobs, database or external requests.

    if not args.demo:
        target = core.ROOT / 'automation.json'
        if not target.exists():
            core.atomic_json(target, core.config())
        store = core.Store()
        store.conn.close()
        empty_feeds = {'reports':{'reports':[]}, 'filings':{'filings':[]},
            'historical-reports':{'reports':[],'revision':0},
            'tracking':{'reports':[],'horizons':[3,7,15,30,60,90],
                'method':{'basis':'尚无追踪记录','alignment':'以实际留存时间为准','limits':'首次安装尚无研究数据。'}},
            'tracking-market':{'stocks':{}},'digests':{'daily':{'conclusion':'尚无解读。'},'weekly':{'conclusion':'尚无解读。'}},
            'research-quality':{'report_count':0,'provisional_count':0,'method':'尚无解读。','factors':[]},
            'status':{'document_count':0,'report_count':0,'queue':[],'schedule':{'active':False},
                'scan':{'sources':[]},'settings':core.public_settings(),'empty_install':True}}
        for name, value in empty_feeds.items():
            path = core.SITE / (name+'.json')
            if not path.exists():core.atomic_json(path, {'generated_at':core.stamp(),**value})
    server_type = DemoServer if args.demo else ResearchServer
    try:
        server = server_type(('127.0.0.1',args.port), DemoHandler if args.demo else Handler)
        server.demo = args.demo
    except OSError:
        parser.exit(2, '端口不可用；请指定其他 --port，现有服务未改动。\n')
    print(f'股票财报自动采集解读系统： http://127.0.0.1:{args.port}/'+(' （虚构示例 / 只读）' if args.demo else ' （本地模式）'),flush=True)
    try:server.serve_forever(poll_interval=1)
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__ == '__main__':main()
