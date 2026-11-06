#!/usr/bin/env python3
"""Package only the reviewed source allowlist, never a working directory snapshot."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile
from check_release import ROOT, check

def main():
    parser=argparse.ArgumentParser(description='生成不含个人运行数据的源码包')
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    if args.output.exists():parser.error('输出文件已存在，未覆盖')
    files,findings=check()
    if findings:parser.exit(2,'源码检查未通过；先运行 tools/check_release.py 查看规则名。\n')
    manifest={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(args.output,'x',compression=zipfile.ZIP_DEFLATED) as z:
        for p in files:
            # Fresh ZIP entries omit local timestamps, usernames and filesystem metadata.
            info=zipfile.ZipInfo('stock-report/'+p.relative_to(ROOT).as_posix())
            info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o644<<16
            z.writestr(info,p.read_bytes())
        z.writestr('stock-report/SOURCE_MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'files':len(files),'sha256':hashlib.sha256(args.output.read_bytes()).hexdigest(),'ok':True}))

if __name__=='__main__':main()
