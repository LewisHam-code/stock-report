"""Portable security master, daily official refresh and local search indexes."""
from __future__ import annotations

import fcntl
import html
import io
import json
import re
import threading
import time
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from market_labels import market_name
from . import core
from .core import ServiceError, atomic_json, atomic_text, now, stamp
from .disclosure_sources import json_payload

_LOCK = threading.RLock()
_INDEX = {}
MINIMUM = {'SH':1500,'SZ':2000,'BJ':200}
SSE = 'https://www.sse.com.cn/assortment/stock/list/share/'
SZSE = 'https://www.szse.cn/market/product/stock/list/index.html'
BSE = 'https://www.bse.cn/nq/listedcompany.html'
STATUS = {'listed':'在册','unknown':'上市状态待核实','pending_confirmation':'名录缺失，待核实',
          'delisting_announced':'已公告退市，尚未到生效日',
          'suspended':'暂停上市','delisted':'已退市'}


def master_path():
    return core.VAULT / '00_基础资料' / '证券名录.json'


def normalize(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC',str(value or ''))).casefold()


def risk_flag(name):
    value = normalize(name).upper()
    if re.match(r'^(?:S\*)?\*?ST',value) or value.startswith('SST'):
        return '*ST' if '*ST' in value else 'ST'
    return '退市整理' if '退市' in value or value.endswith('退') else ''


def _official_day(value):
    try:
        return datetime.strptime(str(value).replace('-','')[:8],'%Y%m%d').date().isoformat()
    except ValueError:return None


def security(code, name, market, source, full_name='', status='listed', listed_at='', delisted_at=''):
    code,name=str(code).strip(),re.sub('<[^>]+>','',html.unescape(str(name or ''))).strip()
    if not market_name(market,code) or not name or name=='-':
        return None
    listed_at,delisted_at=_official_day(listed_at),_official_day(delisted_at)
    if status=='delisted':
        if delisted_at is None:status='pending_confirmation'
        elif delisted_at>now().date().isoformat():status='delisting_announced'
    return {'code':code,'market':market,'company':name,'full_name':str(full_name or '').strip(),
            'aliases':[],'status':status,'risk_flag':risk_flag(name),'source':source,
            'listed_at':listed_at or None,'delisted_at':delisted_at or None,'verified_at':stamp()}


def xlsx_rows(raw):
    """Read bounded official exports with the standard library, never macros."""
    ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 40_000_000:
                raise ValueError('oversize')
            strings=[]
            if 'xl/sharedStrings.xml' in archive.namelist():
                strings=[''.join(item.itertext()) for item in ET.fromstring(archive.read('xl/sharedStrings.xml'))]
            root=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            rows=[]
            for row in root.findall('.//m:row',ns):
                cells={}
                for cell in row:
                    column=re.sub(r'\d','',cell.get('r',''))
                    value=cell.find('m:v',ns)
                    if cell.get('t')=='inlineStr':
                        text=''.join(cell.find('m:is',ns).itertext())
                    elif cell.get('t')=='s' and value is not None:
                        text=strings[int(value.text)]
                    else:text=value.text if value is not None else ''
                    cells[column]=text
                rows.append(cells)
            if not rows:raise ValueError('empty')
            headers=rows[0]
            return [{normalize(headers.get(key)):value for key,value in row.items()} for row in rows[1:]]
    except (KeyError,ValueError,IndexError,AttributeError,zipfile.BadZipFile,ET.ParseError):
        raise ServiceError('catalog_format','官方证券名录文件格式异常') from None


def sse_rows(kind='listed'):
    from .sources import get_json
    sql='COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L' if kind=='listed' else 'COMMON_SSE_CP_GPJCTPZ_GPLB_ZZGP_L'
    rows=[]
    for page in range(1,11):
        params={'sqlId':sql,'COMPANY_STATUS':'2,4,5,7,8' if kind=='listed' else '3',
                'STOCK_TYPE':'1,2,8','type':'inParams','isPagination':'true','pageHelp.pageSize':2000,
                'pageHelp.pageNo':page,'pageHelp.beginPage':page,'pageHelp.cacheSize':1}
        result=get_json('https://query.sse.com.cn/sseQuery/commonQuery.do',params,headers={'Referer':SSE})
        info=result.get('pageHelp') if isinstance(result,dict) else None
        batch=result.get('result') if isinstance(result,dict) else None
        if (not isinstance(info,dict) or not isinstance(batch,list) or not isinstance(info.get('total'),int)
            or info.get('pageNo')!=page or any(not isinstance(row,dict) for row in batch)):
            raise ServiceError('catalog_format','上交所证券名录分页异常')
        rows.extend(batch)
        if len(rows)>=info['total']:break
        if not batch:raise ServiceError('catalog_partial','上交所证券名录分页不完整')
    else:raise ServiceError('catalog_partial','上交所证券名录超过分页上限')
    result=[]
    for row in rows:
        item=security(row.get('A_STOCK_CODE') or row.get('COMPANY_CODE'),row.get('COMPANY_ABBR'),'SH',SSE,
                      row.get('FULL_NAME'),kind,row.get('LIST_DATE'),row.get('DELIST_DATE') if kind=='delisted' else '')
        if item:result.append(item)
    return result


def szse_rows(kind='listed'):
    from .sources import request
    catalog,tab=('1110','tab1') if kind=='listed' else ('1793_ssgs','tab2')
    url='https://www.szse.cn/api/report/ShowReport?' + urlencode({'SHOWTYPE':'xlsx','CATALOGID':catalog,'TABKEY':tab})
    rows=xlsx_rows(request(url,headers={'Referer':SZSE},limit=8_000_000))
    result=[]
    for row in rows:
        code=row.get('a股代码') if kind=='listed' else row.get('证券代码')
        code=str(code or '').zfill(6)
        item=security(code,row.get('a股简称') if kind=='listed' else row.get('证券简称'),'SZ',url,
                      row.get('公司全称'),kind,row.get('a股上市日期') or row.get('上市日期'),row.get('终止上市日期'))
        if item:result.append(item)
    return result


def bse_rows():
    from .sources import request
    rows=[]
    for page in range(50):
        form={'page':page,'typejb':'T','xxfcbj[]':'2','xxzqdm':'','sortfield':'xxzqdm','sorttype':'asc'}
        data=json_payload(request('https://www.bse.cn/nqxxController/nqxxCnzq.do',data=urlencode(form).encode(),
                                  headers={'Referer':BSE,'Content-Type':'application/x-www-form-urlencoded'}))
        info=data[0] if isinstance(data,list) and len(data)==1 else None
        if (not isinstance(info,dict) or not isinstance(info.get('content'),list) or not isinstance(info.get('totalElements'),int)
            or info.get('number')!=page or any(not isinstance(row,dict) for row in info['content'])):
            raise ServiceError('catalog_format','北交所证券名录分页异常')
        rows.extend(info['content'])
        if len(rows)>=info['totalElements']:break
        if not info['content']:raise ServiceError('catalog_partial','北交所证券名录分页不完整')
        time.sleep(.1)
    else:raise ServiceError('catalog_partial','北交所证券名录超过分页上限')
    return [item for row in rows if str(row.get('xxfcbj'))=='2'
            and (item:=security(row.get('xxzqdm'),row.get('xxzqjc'),'BJ',BSE,listed_at=row.get('fxssrq')))]


def cninfo_rows():
    from .sources import get_json
    result={}
    # This is a public full-market download; no watchlist or search term is sent.
    for feed in ('szse','bj'):
        url=f'https://www.cninfo.com.cn/new/data/{feed}_stock.json'
        data=get_json(url)
        rows=data.get('stockList') if isinstance(data,dict) else None
        if not isinstance(rows,list):raise ServiceError('catalog_format','巨潮证券名录格式异常')
        for row in rows:
            if not isinstance(row,dict) or row.get('category')!='A股':continue
            code=str(row.get('code',''))
            market='BJ' if feed=='bj' else next((m for m in ('SH','SZ') if market_name(m,code)),None)
            if market and (item:=security(code,row.get('zwjc'),market,url,status='unknown')):
                item['pinyin']=row.get('pinyin','')
                result[market+code]=item
    return list(result.values())


def load():
    path=master_path()
    try:
        stat=path.stat();key=(str(path),stat.st_mtime_ns,stat.st_size)
        with _LOCK:
            if _INDEX.get('bad_key')==key:raise ValueError('unchanged invalid catalog')
            if _INDEX.get('key')!=key:
                data=json.loads(path.read_text())
                if data.get('schema')!=1 or not isinstance(data.get('securities'),list):raise ValueError()
                for row in data['securities']:
                    if (not isinstance(row,dict) or not isinstance(row.get('code'),str) or not market_name(row.get('market'),row['code'])
                        or not isinstance(row.get('company'),str) or not row['company'] or row.get('status') not in STATUS
                        or not isinstance(row.get('aliases',[]),list) or any(not isinstance(v,str) for v in row.get('aliases',[]))):
                        raise ValueError('invalid catalog row')
                by_code={row['code']:row for row in data['securities']}
                if len(by_code)!=len(data['securities']):raise ValueError('duplicate catalog identity')
                names={}
                for row in by_code.values():
                    for name in [row['company'],row.get('full_name'),*row.get('aliases',[])]:
                        if name:names.setdefault(normalize(name),set()).add(row['code'])
                _INDEX.update(key=key,data=data,codes=by_code,names=names,bad_key=None)
            return _INDEX['data'],_INDEX['codes'],_INDEX['names']
    except (OSError,ValueError,KeyError,TypeError):
        if 'key' in locals():_INDEX['bad_key']=key
        if _INDEX.get('key',('',))[0]==str(path):
            return {**_INDEX['data'],'read_error':'catalog_unreadable'},_INDEX['codes'],_INDEX['names']
        return {'schema':1,'securities':[],'read_error':'catalog_unavailable'},{},{}


def lookup(code):
    return load()[1].get(str(code))


def search(term, limit=10, exact=False):
    query=normalize(term)
    if len(query)<1 or len(query)>100:return []
    data,codes,names=load()
    matched=set(names.get(query,set()))
    if query in codes:matched.add(query)
    if exact:return [codes[code] for code in sorted(matched)]
    if not matched:
        matched={code for code,row in codes.items() if (code.startswith(query) or query in normalize(row['company'])
                 or query in normalize(row.get('full_name')) or normalize(row.get('pinyin','')).startswith(query))}
    return sorted((codes[code] for code in matched),key=lambda row:(row['status']=='delisted',row['code']))[:limit]


def resolve(store, term):
    matches=search(term,exact=True)
    if not matches:
        refresh()
        matches=search(term,exact=True)
    if not matches:
        # Missing-name fallback downloads public dictionaries, never submits
        # the user's term or watchlist to a remote search endpoint.
        rows=store.cached('securities-name-fallback-v1')
        if rows is None:
            rows=cninfo_rows()
            store.cache('securities-name-fallback-v1',rows,hours=2)
        query=normalize(term)
        matches=[row for row in rows if query in (row['code'],normalize(row['company']),normalize(row.get('full_name')))]
        # A fallback match is identity evidence, not proof of current listing.
    unique={row['code']:row for row in matches}
    if not unique:raise ServiceError('watch_name_not_found','未找到匹配的 A 股证券，请核对名称或代码后重试')
    if len(unique)>1:raise ServiceError('watch_name_ambiguous','匹配到多个证券，请从候选列表选择具体股票')
    row=next(iter(unique.values()))
    if row['status']=='delisted':raise ServiceError('security_delisted','该股票已被官方确认退市，历史资料仍可查阅')
    if not lookup(row['code']):
        # Persist only this verified dictionary identity; don't import the
        # dictionary's legacy code aliases as currently listed securities.
        with (core.ROOT/'securities-refresh.lock').open('a') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                data,_,_=load()
                atomic_json(master_path(),{**data,'securities':data['securities']+[row]})
            except BlockingIOError:pass
    return row


def price_enabled(code):
    return (lookup(code) or {}).get('status')!='delisted'


def note_quote_identity(store, code, quote):
    current=lookup(code)
    name=quote.get('company')
    if current and name and normalize(name)!=normalize(current['company']):
        key='security-name-review-'+code
        if not store.cached(key):
            store.cache(key,{'quote_name':name},hours=24)
            store.set('security_catalog_refresh_requested',True)


def refresh(force=False):
    """One daily attempt, guarded across service and scheduler processes."""
    core.ROOT.mkdir(parents=True,exist_ok=True)
    with (core.ROOT/'securities-refresh.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'state':'busy'}
        old,_,_=load()
        # One session runs from 09:00 to the following 09:00, so an overnight
        # scan cannot suppress the next morning's official-name refresh.
        cycle=(now()-timedelta(hours=9)).date().isoformat()
        same_cycle=old.get('refresh_cycle')==cycle
        attempted=old.get('last_attempt_at')
        try:cooldown=attempted and now()-datetime.fromisoformat(attempted)<timedelta(hours=2)
        except (ValueError,TypeError):cooldown=False
        if not force and same_cycle and (not old.get('errors') or cooldown):
            return {'state':'cached','count':len(old['securities']),'errors':old.get('errors',[])}
        previous={row['market']+row['code']:dict(row) for row in old['securities']}
        merged=dict(previous);errors=[];complete=set();at=stamp()
        for market,fetch in (('SH',sse_rows),('SZ',szse_rows),('BJ',bse_rows)):
            try:
                rows=fetch()
                prior_count=sum(row['market']==market and row['status']=='listed' for row in previous.values())
                if len(rows)<max(MINIMUM[market],prior_count*.8):
                    raise ServiceError('catalog_partial','名录数量异常减少，保留旧数据')
                complete.add(market)
                for row in rows:merged[market+row['code']]=row
                current={row['code'] for row in rows}
                for key,row in previous.items():
                    if row['market']==market and row['status']!='delisted' and row['code'] not in current:
                        merged[key]={**row,'status':'pending_confirmation','status_note':'未出现在本次完整名录，尚无官方退市依据',
                                     'missing_since':row.get('missing_since') or at}
            except ServiceError as error:errors.append({'market':market,'code':error.code})
        # Terminal status requires an explicit official termination list.
        for market,fetch in (('SH',sse_rows),('SZ',szse_rows)):
            try:
                for row in fetch('delisted'):
                    key=market+row['code']
                    if row['status']=='pending_confirmation':
                        errors.append({'market':market,'part':'delisted','code':'delisting_date_unconfirmed'})
                        continue
                    prior=merged.get(key)
                    if (prior and prior['status']=='listed' and _official_day(prior.get('listed_at'))
                        and row.get('delisted_at') and _official_day(prior['listed_at'])>row['delisted_at']):
                        continue  # Historical termination predates this current listing.
                    merged[key]=row
            except ServiceError as error:errors.append({'market':market,'part':'delisted','code':error.code})
        try:
            for row in cninfo_rows():
                key=row['market']+row['code']
                if key not in merged and row['market'] not in complete:merged[key]=row
                elif key in merged and merged[key]['status']!='delisted':merged[key]['pinyin']=row.get('pinyin','')
        except ServiceError as error:errors.append({'part':'name_fallback','code':error.code})
        events=[]
        for key,row in merged.items():
            prior=previous.get(key)
            if prior:
                row['aliases']=sorted(set(prior.get('aliases',[])+row.get('aliases',[])+
                    ([prior['company']] if prior['company']!=row['company'] else [])))
                row['full_name']=row.get('full_name') or prior.get('full_name','')
                changes={field:{'before':prior.get(field),'after':row.get(field)} for field in ('company','risk_flag','status')
                         if prior.get(field)!=row.get(field)}
                if changes:events.append({'code':row['code'],'market':row['market'],'detected_at':at,'changes':changes,'source':row['source']})
        history=(old.get('events',[])+events)[-3000:]
        result={'schema':1,'refresh_cycle':cycle,'last_attempt_at':at,'updated_at':at if complete else old.get('updated_at'),
                'official_markets':sorted(complete),'errors':errors,'securities':sorted(merged.values(),key=lambda r:(r['market'],r['code'])),
                'events':history}
        atomic_json(master_path(),result)
        if events:
            event_path=master_path().with_name('证券变更记录.jsonl')
            with event_path.open('a',encoding='utf-8') as stream:
                for event in events:stream.write(json.dumps(event,ensure_ascii=False)+'\n')
        sync_events(events,result['securities'])
        from .company_events import append_events
        for event in events:
            # Keep the full-universe master log; company ledgers only for
            # existing research folders, avoiding thousands of empty notes.
            if (core.VAULT/'30_公司'/event['market']/event['code']).is_dir():
                append_events(event['code'],event['market'],[event],core.VAULT)
        return {'state':'updated' if complete else 'partial','count':len(merged),'events':len(events),'errors':errors}


def sync_events(events, rows):
    # Only existing company folders get notes; never create thousands of notes.
    by_key={row['market']+row['code']:row for row in rows}
    history_by_code={}
    for event in load()[0].get('events',[]):history_by_code.setdefault(event['code'],[]).append(event)
    folders=[]
    for market in ('SH','SZ','BJ'):
        parent=core.VAULT/'30_公司'/market
        if parent.is_dir():folders.extend((market,p) for p in parent.iterdir() if re.fullmatch(r'\d{6}',p.name) and p.is_dir() and not p.is_symlink())
    for market,folder in folders:
        row=by_key.get(market+folder.name)
        if not row:continue
        note=folder/'证券状态-自动生成.md'
        company_note=folder/'公司.md'
        if company_note.is_file():
            from .publish import update_note_properties
            properties={'company':row['company'],'security_status':row['status'],'risk_flag':row['risk_flag']}
            front=company_note.read_text().split('\n---\n',1)[0].splitlines()
            if any(key+': '+json.dumps(value,ensure_ascii=False) not in front for key,value in properties.items()):
                update_note_properties(company_note,{**properties,'security_checked_at':row['verified_at']})
        marker='<!-- SECURITY_STATE_V1 -->'
        old=note.read_text() if note.exists() else ''
        if old and marker not in old:continue
        personal=old.split('\n## 我的备注\n',1)[1] if '\n## 我的备注\n' in old else ''
        changes=history_by_code.get(row['code'],[])
        history='\n'.join('- '+event['detected_at']+'：'+json.dumps(event['changes'],ensure_ascii=False) for event in changes)
        content=(marker+'\n# 证券当前状态\n\n'+row['company']+' · '+row['code']+'\n\n状态：'+STATUS.get(row['status'],'待核实')+
                 '；风险标记：'+(row['risk_flag'] or '当前简称无 ST 标记')+'。\n\n核验时间：'+row['verified_at']+
                 '\n\n[名录来源]('+row['source']+')\n\n## 已检测变更\n'+(history or '首次建立名录，尚无变更记录。')+
                 '\n\n最新核验时间以 [[00_基础资料/证券名录.json]] 为准；本笔记仅在状态或名称变化时更新。'+
                 '\n\n## 我的备注\n'+personal.rstrip()+'\n')
        signature=lambda text:re.sub(r'核验时间：[^\n]*','核验时间：',text)
        if signature(content)!=signature(old):atomic_text(note,content)
