"""Bounded public inputs for the approved pilot, outside the model queue.

Only the prior annual and comparable interim PDFs needed for TTM are fetched.
These supporting documents never enter historical model-reading admission.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime
import json
import re

from . import core


def home(vault=None):return (vault or core.VAULT)/'00_基础资料'/'六维输入补齐'
def supporting_index(code,market,vault=None):return home(vault)/'必要原文'/market/code/'索引.json'


def _read(path):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return {}


def required_periods(period):
    if not re.fullmatch(r'20\d{2}(?:FY|Q1|H1|Q3)',period):return []
    return [period] if period.endswith('FY') else [period,str(int(period[:4])-1)+'FY',str(int(period[:4])-1)+period[4:]]


def _find(store,company,period,asof):
    from . import sources,disclosure_sources
    code,market=company['code'],company['market'];year=int(period[:4])
    if period.endswith('FY'):start,end=f'{year+1}-01-01',f'{year+1}-06-30'
    else:
        month={'Q1':4,'H1':7,'Q3':10}[period[4:]]
        start,end=f'{year}-{month:02}-01',f'{year}-{month+1:02}-30' if month+1 in (4,6,9,11) else f'{year}-{month+1:02}-31'
    end=min(end,asof[:10]);docs=[]
    if market in ('SH','BJ'):
        try:docs=disclosure_sources.query_exchange(market,start,end,1,code=code)['documents']
        except core.ServiceError:pass
    candidates=[d for d in docs if d.get('code')==code and d.get('period')==period]
    if not candidates:
        identity=sources.cninfo_identity(store,code)
        if not identity:raise core.ServiceError('supporting_identity_missing','未取得官方公司身份，不作全市场历史扫描')
        # Different exchanges use 三季度/第三季度/第3季度. A company/date
        # bounded generic quarterly label avoids mistaking spelling for absence.
        keyword={'FY':'年度报告','Q1':'季度报告','H1':'半年度报告','Q3':'季度报告'}[period[4:]]
        column={'SH':'sse','SZ':'szse','BJ':'bj'}[market]
        data=sources.query_announcements(start,end,column,1,keyword=keyword,stock=identity)
        candidates=[d for row in data['announcements'] if (d:=sources.document_from_record(row,column)) and d['code']==code and d['market']==market and d['period']==period]
    candidates=[d for d in candidates if disclosure_sources.official_pdf(d['source_url']) and d['published_at']<=asof]
    if not candidates:raise core.ServiceError('supporting_official_missing','定向官方查询未取得所需完整财报')
    return max(candidates,key=lambda d:(d['published_at'],'更正' in d['title'],d['id']))


def prepare_company(store,company,period,*,asof=None,vault=None,comparisons=False):
    """Bounded official sources for TTM and optional quarter bridges; no AI calls."""
    from .sources import archive_pdf,extract_pdf
    from .financial_evidence import primary_metrics
    at=asof or core.stamp();code,market=company['code'],company['market']
    if not re.fullmatch(r'\d{6}',code) or market not in ('SH','SZ','BJ'):raise ValueError('invalid_supporting_identity')
    index=supporting_index(code,market,vault);saved=_read(index)
    documents={d['period']:d for d in saved.get('documents',[])}
    local=store.rows('SELECT * FROM documents WHERE code=? AND published_at<=? ORDER BY published_at DESC',(code,at))
    errors=saved.get('errors',{});failures=saved.get('failures',{})
    new=0
    targets=required_periods(period)
    if comparisons:
        from .financial_periods import required_periods as comparison_periods
        targets=list(dict.fromkeys(targets+comparison_periods(period)))
    for target in targets:
        existing=next((d for d in local if d['period']==target and d.get('pdf') and d.get('text_file')),None)
        if existing:continue
        if target in documents and documents[target].get('text_file'):continue
        if failures.get(target,0)>=3:continue
        try:
            doc=_find(store,company,target,at)
            relative,sha,_=archive_pdf(doc,store)
            doc.update(pdf=relative,sha256=sha)
            pdf,sha,text,pages=extract_pdf(doc)
            doc.update(pdf=pdf,sha256=sha,text_file=text,page_count=pages,supporting_only=True,downloaded_at=core.stamp())
            proof=primary_metrics(store,doc,allow_refetch=False)
            doc['proof_status']=proof.get('status');documents[target]=doc
            errors.pop(target,None);failures[target]=0;new+=1
        except core.ServiceError as e:
            failures[target]=failures.get(target,0)+1;errors[target]=e.code
        except (ValueError,OSError,TypeError,IndexError):
            failures[target]=failures.get(target,0)+1;errors[target]='supporting_local_parse_failed'
        core.atomic_json(index,{'code':code,'market':market,'documents':list(documents.values()),'errors':errors,'failures':failures,'updated_at':core.stamp(),'model_calls':0})
    result={'code':code,'market':market,'documents':list(documents.values()),'errors':errors,'failures':failures,'updated_at':core.stamp(),'model_calls':0}
    core.atomic_json(index,result)
    return {'code':code,'downloaded':new,'errors':errors,'supporting_documents':len(documents)}


def _price(company,asof,vault=None):
    from .sources import daily_bars
    from .tracking_market import record_path
    path=record_path(company['market'],company['code'],vault).with_name('六维技术日线.json')
    prior=_read(path);day=asof[:10]
    mode='close' if datetime.fromisoformat(asof).strftime('%H:%M')>='15:35' else 'intraday'
    if prior.get('completed_day')==day and (mode=='intraday' or prior.get('completed_mode')=='close'):return {'code':company['code'],'state':'cached'}
    if prior.get('consecutive_failures',0)>=3:return {'code':company['code'],'state':'circuit_open'}
    try:
        value=daily_bars(company['code'],company['market'],count=640,minimum=60,adjustment='qfq')
        if value.get('cross_validation',{}).get('status')=='conflict':
            prior['candidate_prices']=value
            raise core.ServiceError('research_prices_conflict','日线来源冲突')
        prior={'prices':value,'completed_day':day,'completed_mode':mode,'fetched_at':core.stamp(),'consecutive_failures':0}
        core.atomic_json(path,prior)
        return {'code':company['code'],'state':'updated','bars':len(value['bars'])}
    except core.ServiceError as e:
        prior.update(last_error=e.code,attempted_at=core.stamp(),consecutive_failures=prior.get('consecutive_failures',0)+1)
        core.atomic_json(path,prior)
        return {'code':company['code'],'state':'unavailable','error':e.code}


def refresh_prices(pilot,*,asof=None,vault=None):
    """Reuse existing market pass; at most once/day per pilot company."""
    at=asof or core.stamp();result=[]
    from .securities import price_enabled
    companies=[c for c in pilot.get('companies',[]) if price_enabled(c['code'])]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(_price,c,at,vault) for c in companies]
        for future in as_completed(futures):
            try:result.append(future.result())
            except Exception:result.append({'state':'unavailable','error':'research_prices_unexpected'})
    core.atomic_json(home(vault)/'日线采集.json',{'at':at,'companies':result,'model_calls':0})
    return result
