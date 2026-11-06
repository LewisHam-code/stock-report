"""Incremental, portable research archive. No PDFs, secrets or background jobs.

Immutable gzip JSON objects are committed by a small manifest last. Existing
objects are shared by hash; a failed sync cannot damage the previous manifest.
Restoration is intentionally restricted to a new, isolated directory.
"""
from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re

from . import core

SCHEMA = 1
TABLES = ('documents','reports','snapshots','tracking_outcomes','tracking_watch_closes',
          'cache','runs','api_calls','settings','backtests')
CONFIG_FIELDS = ('watchlist','pdf_unwatched_at','strategy_version','timezone','provider','model','test_mode',
                 'routing_mode','web_research','max_document_chars','max_output_tokens','daily_api_calls',
                 'reading_mode','omit_administrative_content','reuse_verified_pdf_metrics','initial_report_thinking',
                 'financial_reading_cost_policy',
                 'quarter_worker_processes',
                 'download_per_run','analysis_per_run','market_refresh_per_run','scan_lookback_days',
                 'initial_lookback_days','pages_per_source_per_run','network_timeout_seconds',
                 'risk_per_trade','maximum_position_fraction','portfolio_open_risk_limit','account_equity_example')
FORBIDDEN = {'api_key','authorization','cookie','set-cookie','password','access_token','refresh_token',
             'client_secret','private_key','ssh_key'}
ARCHIVE = Path('00_基础资料/系统归档')


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':')).encode('utf-8')


def _guard(value):
    if isinstance(value,dict):
        if any(str(key).casefold() in FORBIDDEN for key in value):
            raise core.ServiceError('archive_secret_field','归档遇到凭证字段，已停止；原数据保留')
        for item in value.values():_guard(item)
    elif isinstance(value,list):
        for item in value:_guard(item)
    elif isinstance(value,str) and re.search(r'(?:\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}|\bAIza[A-Za-z0-9_-]{30,})',value):
        raise core.ServiceError('archive_secret_value','归档遇到疑似密钥，已停止；原数据保留')


def _safe_path(vault, relative):
    relative = Path(relative)
    target = vault / relative
    if relative.is_absolute() or '..' in relative.parts or not target.resolve().is_relative_to(vault.resolve()):
        raise core.ServiceError('archive_path_invalid','归档路径异常，停止读取')
    return target


def _blob(vault, folder, raw):
    digest = _hash(raw)
    relative = folder / '对象' / (digest+'.json.gz')
    path = _safe_path(vault,relative)
    if path.exists():
        if _hash(gzip.decompress(path.read_bytes())) != digest:
            raise core.ServiceError('archive_hash_mismatch','归档数据指纹不符，停止同步并保留原文件')
        return {'path':relative.as_posix(),'sha256':digest,'bytes':len(raw)},False
    path.parent.mkdir(parents=True,exist_ok=True)
    # The blob is immutable. Readers only use it after the manifest is committed.
    encoded = gzip.compress(raw,compresslevel=6,mtime=0)
    with path.open('xb') as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    return {'path':relative.as_posix(),'sha256':digest,'bytes':len(raw)},True


def _read_blob(vault, reference):
    path = _safe_path(vault,reference['path'])
    raw = gzip.decompress(path.read_bytes())
    if _hash(raw) != reference['sha256'] or len(raw) != reference['bytes']:
        raise core.ServiceError('archive_hash_mismatch','归档数据指纹不符，停止恢复')
    return raw


def safe_config(cfg):
    result = {key:cfg[key] for key in CONFIG_FIELDS if key in cfg}
    result['model_profiles'] = [{key:profile[key] for key in ('id','provider','model','enabled','test_mode','stages','api_protocol') if key in profile}
                                for profile in cfg.get('model_profiles',[])]
    _guard(result)
    return result


def sync_watchlist(cfg, vault=None):
    """Small synchronous write on add/remove; no full database scan."""
    vault = Path(vault or core.VAULT)
    value = {key:cfg.get(key,[] if key=='watchlist' else {}) for key in ('watchlist','pdf_unwatched_at')}
    _guard(value)
    data = {'schema':SCHEMA,'kind':'stock_watchlist','data':value,'sha256':_hash(_json(value))}
    path = vault/'00_基础资料/我的关注.json'
    if path.exists():
        old = json.loads(path.read_text())
        if old.get('kind')!='stock_watchlist':
            raise core.ServiceError('archive_file_conflict','关注归档位置已有其他文件，未覆盖')
        if old==data:return False
    core.atomic_json(path,data)
    return True


def _ordered(rows):
    return sorted(rows,key=_json)


def sync(store, vault=None, root=None, cfg=None):
    if vault is None and root is None:
        database=Path(store.conn.execute('PRAGMA database_list').fetchone()[2]).resolve()
        if database != (core.ROOT/'db/automation.sqlite').resolve():
            raise core.ServiceError('archive_target_required','隔离数据库必须显式指定归档目录')
    vault,root = Path(vault or core.VAULT),Path(root or core.ROOT)
    cfg = cfg if cfg is not None else core.config()
    archive = vault/ARCHIVE
    archive.mkdir(parents=True,exist_ok=True)
    with (archive/'同步.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        # One consistent read view even if the local service updates a quote.
        store.conn.execute('BEGIN')
        try:
            tables={name:_ordered(store.rows('SELECT * FROM '+name)) for name in TABLES}
        finally:
            store.conn.rollback()
        documents=tables['documents']
        markets={row['code']:row['market'] for row in documents}
        markets.update({row['code']:row['market'] for row in cfg.get('watchlist',[]) if isinstance(row,dict) and row.get('market') in ('SH','SZ','BJ')})
        doc_codes={row['id']:row['code'] for row in documents}
        report_codes={row['id']:doc_codes.get(row['document_id']) for row in tables['reports']}
        token_codes={**doc_codes,**report_codes,**{row['sha256']:row['code'] for row in documents if row.get('sha256')}}
        created,total_bytes=0,0
        def put(raw,code=None):
            nonlocal created,total_bytes
            folder=Path('30_公司')/markets[code]/code/'数据' if code in markets else ARCHIVE
            reference,new=_blob(vault,folder,raw)
            created+=int(new);total_bytes+=len(raw)
            return reference
        manifest={'schema':SCHEMA,'tables':{},'texts':[],
                  'config':safe_config(cfg),'limits':[
                      'PDF、密钥、私密接口地址和系统启动项不归档；换机后需独立配置密钥和启动项。',
                      '历史未保存的精确模型输入不能重建；当时的原始报告与今天的快照分别保存，不能互相替代。']}
        for name,rows in tables.items():
            records=[]
            for row in rows:
                code=row.get('code') or doc_codes.get(row.get('document_id')) or report_codes.get(row.get('report_id'))
                if code is None and name in ('cache','settings'):
                    key=row['key']
                    code=next((owner for token,owner in token_codes.items() if token in key),None)
                    if code is None:
                        match=re.search(r'(?:SH|SZ|BJ|-)(\d{6})(?:-|$)',key)
                        code=match[1] if match and match[1] in markets else None
                    if code is None and name=='cache':
                        value=json.loads(row['payload'])
                        code=value.get('code') if isinstance(value,dict) and value.get('code') in markets else None
                record={}
                for key,value in row.items():
                    if key in ('payload','value','summary') and isinstance(value,str):
                        try:_guard(json.loads(value))
                        except json.JSONDecodeError:_guard(value)
                        record[key]={'$blob':put(value.encode('utf-8'),code)}
                    else:
                        _guard(value)
                        record[key]=value
                records.append(record)
            manifest['tables'][name]={'records':records,'count':len(rows),'sha256':_hash(_json(rows))}
        # Preserve all extracted originals, including pilot/orphan text records.
        referenced={Path(row['text_file']).name:row['code'] for row in documents if row.get('text_file')}
        for path in sorted((root/'text').glob('*.json')):
            if path.is_symlink():
                raise core.ServiceError('archive_text_symlink','原文文字文件为符号链接，停止归档')
            raw=path.read_bytes();_guard(json.loads(raw))
            code=referenced.get(path.name)
            if code is None:
                ident=path.name.split('-',1)[0];code=doc_codes.get(ident)
            manifest['texts'].append({'name':path.name,'blob':put(raw,code)})
        baseline=root/'db/tracking-baselines.json'
        if baseline.exists():
            raw=baseline.read_bytes();_guard(json.loads(raw))
            manifest['baselines']=put(raw)
        manifest['historical_input_gaps']=sum(not json.loads(row['payload']).get('_generation_evidence') for row in tables['reports'])
        manifest['content_sha256']=_hash(_json(manifest))
        pointer=archive/'归档清单.json'
        old=json.loads(pointer.read_text()) if pointer.exists() else None
        if old and (old.get('schema')!=SCHEMA or 'content_sha256' not in old):
            raise core.ServiceError('archive_file_conflict','归档清单位置已有其他文件，未覆盖')
        changed=old!=manifest
        if changed:
            # Keep every committed manifest; identical payloads share blobs.
            _blob(vault,ARCHIVE,_json(manifest))
            core.atomic_json(pointer,manifest)
        sync_watchlist(cfg,vault)
        note=archive/'说明.md'
        marker='<!-- STOCK_VAULT_ARCHIVE_V1 -->'
        content=(marker+'\n# 研究数据归档\n\n'
                 '逐股数据对象保存在各公司“数据/对象”内，公共对象与归档清单保存在本目录。对象采用gzip压缩JSON和SHA-256指纹，重复内容只保存一次；清单最后提交，可校验并恢复研究数据库。\n\n'
                 '包括全部原始结构化报告、财报记录、财务行情快照、提取文字、核对缓存、追踪记录、运行与调用用量记录、关注清单和研究参数。现有公司笔记、证券名录、变更事件仍在原位置。\n\n'
                 'PDF、密钥、私密接口地址和系统启动项不包含在内。精确生成输入只从新机制启用后的报告开始保存；旧报告仅保留实际留存的材料，不用当前数据冒充历史输入。恢复验证使用隔离目录，不覆盖正在运行的系统。\n')
        if not note.exists() or (marker in note.read_text() and note.read_text()!=content):core.atomic_text(note,content)
        return {'schema':SCHEMA,'changed':changed,'new_objects':created,'logical_bytes':total_bytes,
                'tables':{name:len(rows) for name,rows in tables.items()},'text_files':len(manifest['texts']),
                'historical_input_gaps':manifest['historical_input_gaps'],'manifest_sha256':manifest['content_sha256']}


def restore_isolated(target, vault=None):
    """Validate and rebuild into an empty directory; never open the live DB."""
    vault,target=Path(vault or core.VAULT).resolve(),Path(target).resolve()
    if target.is_relative_to(vault) or target==core.ROOT.resolve() or (target.exists() and any(target.iterdir())):
        raise core.ServiceError('restore_target_not_empty','恢复只允许使用独立的空目录')
    manifest=json.loads((vault/ARCHIVE/'归档清单.json').read_text())
    expected=manifest.get('content_sha256')
    if manifest.get('schema')!=SCHEMA or _hash(_json({key:value for key,value in manifest.items() if key!='content_sha256'}))!=expected:
        raise core.ServiceError('archive_manifest_invalid','归档清单版本或指纹不符')
    if set(manifest['tables'])!=set(TABLES):
        raise core.ServiceError('archive_schema_invalid','归档数据表不完整')
    tables={}
    for name in TABLES:
        section=manifest['tables'][name]
        rows=[]
        for record in section['records']:
            rows.append({key:_read_blob(vault,value['$blob']).decode('utf-8')
                         if isinstance(value,dict) and set(value)=={'$blob'} else value for key,value in record.items()})
        if len(rows)!=section['count'] or _hash(_json(rows))!=section['sha256']:
            raise core.ServiceError('archive_table_mismatch','归档数据表内容不匹配')
        tables[name]=rows
    texts=[]
    for item in manifest['texts']:
        if Path(item['name']).name!=item['name'] or not item['name'].endswith('.json'):
            raise core.ServiceError('archive_path_invalid','原文文字文件名不合法')
        texts.append((item['name'],_read_blob(vault,item['blob'])))
    baselines=_read_blob(vault,manifest['baselines']) if manifest.get('baselines') else None
    watch=json.loads((vault/'00_基础资料/我的关注.json').read_text())
    if watch.get('kind')!='stock_watchlist' or _hash(_json(watch['data']))!=watch.get('sha256'):
        raise core.ServiceError('archive_watchlist_invalid','关注清单归档指纹不符')
    target.mkdir(parents=True,exist_ok=True)
    restored=core.Store(target/'db/automation.sqlite')
    try:
        restored.conn.execute('BEGIN')
        for name,rows in tables.items():
            allowed={row['name'] for row in restored.rows('PRAGMA table_info('+name+')')}
            for row in rows:
                if set(row)!=allowed:raise core.ServiceError('archive_schema_invalid','归档字段与当前版本不兼容')
                columns=list(row)
                restored.conn.execute('INSERT INTO '+name+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(row[key] for key in columns))
        restored.conn.commit()
        verified={name:_hash(_json(_ordered(restored.rows('SELECT * FROM '+name))))==manifest['tables'][name]['sha256'] for name in TABLES}
        integrity=restored.conn.execute('PRAGMA quick_check').fetchone()[0]
        foreign_keys=restored.rows('PRAGMA foreign_key_check')
    finally:
        restored.conn.close()
    for name,raw in texts:
        path=target/'text'/name;path.parent.mkdir(exist_ok=True);path.write_bytes(raw)
    if baselines is not None:(target/'db/tracking-baselines.json').write_bytes(baselines)
    cfg={**manifest['config'],**watch['data'],'automatic_enabled':False,'schedule':{}}
    cfg['model_profiles']=[{**profile,'enabled':False} for profile in cfg.get('model_profiles',[])]
    core.atomic_json(target/'automation.json',cfg)
    ok=all(verified.values()) and integrity=='ok' and not foreign_keys
    return {'ok':ok,'tables_verified':verified,'row_counts':{name:len(rows) for name,rows in tables.items()},
            'texts_verified':len(texts),'baseline_verified':baselines is not None,'watchlist_verified':True,
            'integrity':integrity,'foreign_key_violations':len(foreign_keys),'automatic_run_disabled':True,
            'historical_input_gaps':manifest.get('historical_input_gaps',0)}
