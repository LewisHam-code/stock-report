#!/usr/bin/env python3
"""Conservative source checks. Prints file/rule only, never matched values."""
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
TOP_FILES={'.gitignore','.env.example','README.md','LICENSE','SECURITY.md','CONTRIBUTING.md','CHANGELOG.md','THIRD_PARTY_NOTICES.md'}
BASE_FILES={'run.py','market_labels.py','schema.sql','requirements.txt','requirements.lock','automation.example.json'}

def release_files(root=ROOT):
    for p in sorted(root.rglob('*')):
        if not p.is_file():continue
        r=p.relative_to(root);parts=r.parts
        if '__pycache__' in parts or any(x.startswith('.') for x in parts[:-1]):continue
        allow=(len(parts)==1 and r.name in TOP_FILES)
        allow |= parts[0] in ('docs','examples') and p.suffix in ('.md','.json','.jpg','.png','.svg')
        allow |= parts[0]=='tools' and len(parts)==2 and p.suffix=='.py'
        allow |= parts[0]=='stock-site' and len(parts)==2 and p.suffix in ('.html','.js','.css')
        allow |= parts[0]=='research-data' and ((len(parts)==2 and p.name in BASE_FILES) or
                    (len(parts)==3 and parts[1]=='stock_research' and p.suffix=='.py') or
                    (len(parts)>=3 and parts[1]=='tests' and p.suffix in ('.py','.cjs','.json')))
        if allow:yield p

def check(root=ROOT):
    findings=[];files=list(release_files(root))
    patterns={
        'home_path':re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/'),
        'windows_home':re.compile(r'[A-Za-z]:\\Users\\[^\\\s]+'),
        'private_key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
        'provider_token':re.compile(r'\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{30,})'),
    }
    for p in files:
        r=p.relative_to(root).as_posix()
        if p.is_symlink():findings.append((r,'symlink'));continue
        try:text=p.read_text()
        except UnicodeError:continue
        for name,pattern in patterns.items():
            for match in pattern.finditer(text):
                value=match.group()
                if name=='provider_token' and re.search(r'fake|fixture|placeholder|unit-test|sk-test-',value,re.I):continue
                findings.append((r,name));break
    example=json.loads((root/'research-data/automation.example.json').read_text())
    if example.get('automatic_enabled') or example.get('model_profiles') or example.get('watchlist'):
        findings.append(('research-data/automation.example.json','nonempty_private_defaults'))
    html=(root/'stock-site/settings.html').read_text()
    js=(root/'stock-site/runtime-settings.js').read_text()
    for name in ('readingCostPolicy','readingCostAlerts','currentBatchPanel','stopCurrentBatch'):
        if name in html or name in js:findings.append(('stock-site/settings.html','removed_panel_hook'))
    for image in ('overview.jpg','report.jpg','tracking.jpg','settings.jpg'):
        if not (root/'docs/images'/image).is_file():findings.append(('docs/images/'+image,'missing_screenshot'))
    return files,sorted(set(findings))

def main():
    files,findings=check()
    print(json.dumps({'checked_files':len(files),'ok':not findings,'findings':findings},ensure_ascii=False))
    raise SystemExit(bool(findings))

if __name__=='__main__':main()
