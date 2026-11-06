from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .core import ROOT, ServiceError


def ensure_service():
    url = "http://127.0.0.1:" + os.environ.get('STOCK_PORT', '8876') + "/api/health"
    def healthy():
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return json.load(response).get("service") == "stock-local-settings"
        except (urllib.error.URLError, TimeoutError, ValueError):
            return False
    if healthy():
        return True
    from .deployment import SERVICE, job_loaded, launch
    if job_loaded(SERVICE):
        launch(["kickstart", f"gui/{__import__('os').getuid()}/{SERVICE}"])
    else:
        subprocess.Popen([sys.executable, str(ROOT / "run.py"), "serve"], cwd=ROOT.parent,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(20):
        if healthy():
            return True
        time.sleep(0.25)
    raise ServiceError("settings_service_unavailable", "本机设置服务无法启动，可能有程序占用了端口")
