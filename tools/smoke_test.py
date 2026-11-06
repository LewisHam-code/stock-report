#!/usr/bin/env python3
"""Clean-copy smoke test. No model calls, external requests or installed jobs."""
from pathlib import Path
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from check_release import ROOT, release_files

def main():
    checks=0
    with tempfile.TemporaryDirectory(prefix='stock-source-check-') as directory:
        project=Path(directory)/'project';project.mkdir()
        for p in release_files():
            out=project/p.relative_to(ROOT);out.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,out)
        env={'PATH':os.environ.get('PATH',''),'PYTHONUTF8':'1','LANG':'en_US.UTF-8'}
        for demo in (True,False):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            command=[sys.executable,str(project/'tools/serve.py'),'--port',str(port)]
            if demo:command.append('--demo')
            process=subprocess.Popen(command,env=env,cwd=project,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            base=f'http://127.0.0.1:{port}'
            def get(path,headers=None,body=None):
                request=urllib.request.Request(base+path,data=body,headers=headers or {})
                try:
                    with urllib.request.urlopen(request,timeout=3) as r:return r.status,r.read()
                except urllib.error.HTTPError as r:return r.code,r.read()
            try:
                for _ in range(50):
                    try:
                        status,body=get('/api/health')
                        if status==200:break
                    except (OSError,urllib.error.URLError):pass
                    if process.poll() is not None:raise AssertionError('server exited before ready')
                    time.sleep(.1)
                else:raise AssertionError('server did not start')
                checks+=1
                for path in ('/','/reports.html','/tracking.html','/filings.html','/settings.html',
                             '/site.css','/runtime-settings.js','/data/reports.json','/data/tracking.json',
                             '/api/settings','/api/runtime','/api/watchlist','/api/sources'):
                    status,body=get(path);assert status==200,(demo,path,status);checks+=1
                for path in ('/.env.local','/research-data/automation.json','/../research-data/automation.example.json',
                             '/%2e%2e/research-data/automation.example.json','/data/%2e%2e/%2e%2e/.env.local'):
                    assert get(path)[0]==404,path;checks+=1
                assert get('/api/settings',{'Origin':'https://example.com'})[0]==403;checks+=1
                html=get('/settings.html')[1].decode()
                assert all(key not in html for key in ('readingCostAlerts','currentBatchPanel','stopCurrentBatch'));checks+=1
                if demo:
                    assert get('/api/run',{'Content-Type':'application/json','X-Stock-Research':'1'},b'{}')[0]==405;checks+=1
                    assert not (project/'research-data/automation.json').exists();checks+=1
                    assert not (project/'research-data/db').exists();checks+=1
                    reports=json.loads(get('/data/reports.json')[1])['reports'];assert len(reports)==3
                    for r in reports:
                        assert r['demo'] is True
                        assert get('/'+r['detail_url'].removeprefix('./'))[0]==200;checks+=1
                else:
                    cfg=json.loads((project/'research-data/automation.json').read_text())
                    assert not cfg['automatic_enabled'] and not cfg['model_profiles'] and not cfg['watchlist'];checks+=1
                    assert json.loads(get('/data/reports.json')[1])['reports']==[];checks+=1
                    with sqlite3.connect(project/'research-data/db/automation.sqlite') as conn:
                        assert conn.execute('SELECT COUNT(*) FROM reports').fetchone()[0]==0
                        assert conn.execute('SELECT COUNT(*) FROM api_calls').fetchone()[0]==0
                    checks+=2
            finally:
                process.terminate()
                try:process.communicate(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.communicate()
    print(json.dumps({'ok':True,'checks':checks,'paid_calls':0,'native_jobs_installed':0}))

if __name__=='__main__':main()
