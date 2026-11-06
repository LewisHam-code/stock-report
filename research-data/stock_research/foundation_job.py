"""One-off bootstrap: isolated historical reviews, bounded Flash validation."""
import gzip
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path
from . import core, foundation_budget as budget

ACTIVE={'pilot','bulk','waiting_idle','data','focused','focused_waiting_idle'}

def active():
    value=budget.load()
    return value.get('workspace')==core.digest(str(core.ROOT.resolve())) and value.get('phase') in ACTIVE

def initialize(target=50):
    if budget.path().exists():return budget.load()
    cfg=core.config()
    master=json.loads((core.VAULT/'00_基础资料'/'证券名录.json').read_text())
    from market_labels import market_name
    groups={}
    for row in master['securities']:
        if row['status']!='listed':continue
        groups.setdefault(market_name(row['market'],row['code']),[]).append(row)
    for rows in groups.values():rows.sort(key=lambda r:hashlib.sha256(r['code'].encode()).hexdigest())
    candidates=[]
    for index in range(20):
        for board,rows in sorted(groups.items()):
            row=rows[index];year=core.now().year-1-index%3
            candidates.append({k:row.get(k) for k in ('code','company','market','risk_flag','aliases')}|
                              {'board':board,'year':year,'sample_index':len(candidates)})
    value={'schema':1,'id':'foundation-'+core.now().strftime('%Y%m%d'),'phase':'pilot','created_at':core.stamp(),
           'workspace':core.digest(str(core.ROOT.resolve())),
           'budget_cny':1000,'pilot_target':max(50,min(100,target)),'calls':{},'results':{},'candidates':candidates,
           'normal_strategy':{k:cfg.get(k) for k in ('daily_api_calls','analysis_per_run','download_per_run','prefer_off_peak','interval_minutes')},
           'policy':'本次补建豁免日常次数和每轮数量；试读可立即执行，验证通过后的批量只在闲时；不自动调用Pro'}
    core.atomic_json(budget.path(),value)
    return value

def settings():
    value=budget.load();cfg=core.config()
    profile=next((p for p in core.model_profiles(cfg) if p['provider']=='deepseek' and p['model']=='deepseek-flash'),None)
    if profile is None:raise core.ServiceError('foundation_flash_missing','Flash模型配置尚未就绪')
    return {**cfg,**profile,'single_model':True,'profile_id':profile['id'],'foundation_task':value['id'],
            'prefer_off_peak':value['phase']!='pilot','reading_mode':'deep'}

def find_annual(store,security):
    from .securities import price_enabled
    if not price_enabled(security['code']):raise core.ServiceError('security_delisted','该股票已确认退市，停止查找和下载年报')
    from .disclosure_sources import query_exchange
    from .sources import query_announcements,document_from_record
    year=security['year'];start=f'{year+1}-01-01';end=min(f'{year+1}-12-31',core.now().date().isoformat())
    period=f'{year}FY';market=security['market'];code=security['code']
    from .securities import lookup
    listed=str((lookup(code) or {}).get('listed_at') or '').replace('-','')[:4]
    if listed.isdigit() and int(listed)>year and int(listed)<core.now().year:
        year=int(listed);period=f'{year}FY';start=f'{year+1}-01-01';end=min(f'{year+1}-12-31',core.now().date().isoformat())
    if market in ('SH','BJ'):
        try:
            response=query_exchange(market,start,end,1,code)
            choices=[d for d in response['documents'] if d['code']==code and d['period']==period]
            if choices:return max(choices,key=lambda d:(d['published_at'],d['id']))
        except core.ServiceError:pass
    column={'SH':'sse','SZ':'szse','BJ':'bj'}[market]
    # Company names change and may contain full-width/spaced exchange labels.
    # CNINFO's own security identity avoids name-search truncation and aliases.
    identity=cninfo_identity(store,code)
    for page in (1,2,3):
        response=query_announcements(start,end,column,page,stock=identity,keyword='' if identity else security['company'])
        choices=[d for row in response['announcements'] if (d:=document_from_record(row,column))
                 and d['code']==code and d['market']==market and d['period']==period]
        if choices:return max(choices,key=lambda d:(d['published_at'],d['id']))
        if page*30>=response.get('totalAnnouncement',0):break
    raise core.ServiceError('foundation_annual_not_found','未找到代码与期间一致的官方年度报告')


def cninfo_identity(store,code):
    from .sources import cninfo_identity as resolve
    return resolve(store,code)

def review(security,full_text=False,settings_override=None):
    from .sources import archive_pdf,extract_pdf
    from .foundation_data import financial_for
    from .analysis import analyze
    store=core.Store()
    try:
        from .securities import price_enabled
        code=security.get('code') or security.get('document',{}).get('code')
        if code and not price_enabled(code):raise core.ServiceError('security_delisted','该股票已确认退市，停止下载和解读')
        doc=security.get('document') or find_annual(store,security)
        if security.get('fixed_year') and doc.get('period')!=str(security['year'])+'FY':
            raise core.ServiceError('annual_period_mismatch','报告期间不符，未调用模型')
        from .financial_evidence import _text_record
        _,record=_text_record(doc) if doc.get('sha256') else (None,None)
        if record is None:
            pdf,sha,_=archive_pdf(doc,store);doc.update(pdf=pdf,sha256=sha)
            pdf,sha,text,pages=extract_pdf(doc);doc.update(pdf=pdf,sha256=sha,text_file=text,page_count=pages)
        else:text=doc['text_file'];pages=len(record['pages'])
        cfg={**settings(),**(settings_override or {})}
        if cfg.get('_document_prepared_observer'):
            cfg['_document_prepared_observer'](doc)
        if full_text:
            from .sources import excerpt
            cfg['max_document_chars']=600000
            selected,coverage=excerpt(doc,max_chars=cfg['max_document_chars'])
            if not coverage['full_text'] or len(selected.encode())>900000:
                raise core.ServiceError('foundation_full_text_too_large','原文超过本轮完整阅读保护，保留给分章复核；尚未调用模型')
        financial=financial_for(doc['code'],doc['market']) or {'history':[]}
        # Historical reviews are fact-building exercises; current prices and
        # today's rule ratings never become purported historical predictions.
        snapshot={'financial':financial,'errors':[{'part':'valuation','code':'historical_fact_review_only'}]}
        report=analyze(store,doc,snapshot,settings=cfg)
        report['review_context']={'kind':'historical_foundation','not_a_historical_prediction':True,'formal_tracking_eligible':False,
                                 **({'one_off_task':cfg['one_off_annual_task']} if cfg.get('one_off_annual_task') else {})}
        output=core.VAULT/'30_公司'/doc['market']/doc['code']/'历史年报解读'/f'{doc["period"]}-{doc["id"]}-{report["version"]}.json'
        core.atomic_json(output,report)
        raw=json.loads((core.ROOT/text).read_text())
        core.atomic_text(output.with_name(output.stem+'-原文文字.json.gz.hex'),gzip.compress(core.dumps(raw).encode()).hex())
        metrics=report['financial_analysis']['metrics'];quality=report['financial_analysis'].get('data_quality') or {}
        conflicts=[m['metric'] for m in metrics if m.get('verification_status')=='conflict']
        verified={m['metric'] for m in metrics if m.get('verified')}
        from .sources import substantive_coverage_complete
        result={'state':'reviewed','code':doc['code'],'market':doc['market'],'company':doc['company'],'period':doc['period'],
                'board':security['board'],'pages':pages,'verified_metrics':len(verified),'core_verified':quality.get('verified_count'),
                'conflicts':conflicts,'full_text':report['financial_analysis'].get('coverage',{}).get('full_text'),
                'substantive_full_coverage':substantive_coverage_complete(report['financial_analysis'].get('coverage',{})),
                'model':report['model'],'source':doc['source_url'],'finished_at':core.stamp(),
                'quality_status':quality.get('status','pending'),'parser_status':quality.get('parser_status'),'document_id':doc['id'],'report_id':report['id'],
                'report_path':str(output.relative_to(core.VAULT)),'document':doc,
                'reading_mode':cfg.get('reading_mode'),'failed_checks':quality.get('failed_checks',[]),
                'critical_corrections':sum(m.get('repair_kind')=='source_value_correction' and m.get('original_model_value') is not None for m in metrics)}
        if security.get('validation_stratum'):result['validation_stratum']=security['validation_stratum']
        textnote=('# 历史年报研究底稿\n\n'+doc['company']+' · '+doc['period']+'\n\n'+
                  '这是补建时形成的历史事实解读，不是当年发布的评级，不进入正式方向吻合率。\n\n'+
                  report.get('evidence','')+'\n\n## 风险与待核实\n\n'+report.get('risks','')+'\n\n原文：'+doc['source_url']+'\n')
        core.atomic_text(output.with_suffix('.md'),textnote)
        if cfg.get('one_off_annual_task'):
            from .publish import note_text
            core.atomic_text(output.with_suffix('.md'),'历史事实补建底稿；不代表当年评级或预测，不进入正式方向吻合率。\n\n'+note_text(report))
        return result
    finally:store.conn.close()

def summarize():
    with budget.edit() as job:
        values=list(job.get('results',{}).values());reviews=[r for r in values if r['state']=='reviewed']
        job['progress']={'reviewed':len(reviews),'attempted':len(values),'target':job.get('pilot_target',50),
                         'with_conflicts':sum(bool(r['conflicts']) for r in reviews),'with_incomplete_coverage':sum(not r.get('substantive_full_coverage',r['full_text']) for r in reviews),
                         'with_nonverbatim_coverage':sum(not r['full_text'] for r in reviews),
                         'boards':sorted({r['board'] for r in reviews}),'periods':sorted({r['period'] for r in reviews})}
        if len(reviews)>=job.get('pilot_target',50) and job.get('phase')=='pilot':
            # Coverage and conflicts must be addressed before broad paid work.
            passed=all(not r['conflicts'] and r['quality_status']=='complete' and r.get('substantive_full_coverage',r['full_text']) for r in reviews)
            job['validation']={'state':'passed' if passed else 'needs_review','basis':'数值原文核对、缺项和实质正文覆盖；仅可省略已记录的目录/行政信息，不代表逐字全文或预测能力',
                               'checked_at':core.stamp()}
            # Annual cost is measured before committing the whole universe.
            reported=len(reviews)
            idle_cost=sum((c.get('cost',c['maximum'])/c.get('multiplier',1)) if c.get('state')=='usage_returned'
                          else c['maximum'] for c in budget.all_calls(job))
            projected=idle_cost/reported*5574*3 if reported else None
            job['validation']['projected_idle_total_cny']=projected
            affordable=projected is not None and projected<=job['budget_cny']
            job['phase']='waiting_idle' if passed and affordable else 'needs_review'
        if job.get('scope_decision')=='focused':
            values=list(job.get('focused_results',{}).values())
            job['focused_progress']={'queued':len(job.get('focused_queue',[])),'prepared':len(job.get('focused_documents',{})),
                                     'processed':len(values),'accepted':sum(r.get('acceptance')=='accepted' for r in values),
                                     'needs_review':sum(r.get('acceptance')=='needs_review' or r['state']=='failed' for r in values)}
        job['updated_at']=core.stamp()
        result={k:job.get(k) for k in ('id','phase','progress','validation','estimated_spend_cny','budget_cny','cost_basis','focused_scope','focused_progress')}
    core.atomic_json(budget.folder()/'进度.json',result)
    core.atomic_text(budget.folder()/'进度-自动生成.md','# 基础补建进度\n\n'+core.dumps(result)+'\n')
    return result

def run_pilot(max_documents=None):
    job=initialize()
    if job['phase']!='pilot':return summarize()
    done=sum(r['state']=='reviewed' for r in job['results'].values())
    remaining=min(job['pilot_target']-done,max_documents or job['pilot_target'])
    candidates=[c for c in job['candidates'] if str(c['sample_index']) not in job['results']]
    failures=0;halted=False
    with ThreadPoolExecutor(max_workers=2) as pool:
        active={pool.submit(review,c):c for c in candidates[:min(2,remaining)]};following=iter(candidates[min(2,remaining):])
        while active:
            future=next(as_completed(active));candidate=active.pop(future)
            try:result=future.result();remaining-=1;failures=0
            except core.ServiceError as error:
                result={'state':'failed','code':candidate['code'],'error':error.code,'finished_at':core.stamp()};failures+=1
            except Exception:
                result={'state':'failed','code':candidate['code'],'error':'foundation_unexpected','finished_at':core.stamp()};failures+=1
            with budget.edit() as value:value['results'][str(candidate['sample_index'])]=result
            progress=summarize();print(core.dumps(progress),flush=True)
            if failures>=3:
                with budget.edit() as value:value['phase']='source_or_model_blocked'
                halted=True
            # Drain work already in progress so paid results are not discarded
            # when another worker trips the consecutive-error breaker.
            if not halted and remaining>len(active):
                candidate=next(following,None)
                if candidate:active[pool.submit(review,candidate)]=candidate
    return summarize()


def run_bulk(max_seconds=1800):
    """Only reached after pilot quality/cost gates, and only in off-peak."""
    from .model_timing import window
    if budget.load().get('scope_decision')=='focused':
        raise core.ServiceError('foundation_scope_changed','本轮已改为关注及候选范围，不再运行全市场批量解读')
    cfg=settings();timing=window({**cfg,'prefer_off_peak':True})
    if timing['deferred']:
        return {**summarize(),'waiting_for':timing['resume_at']}
    job=budget.load()
    if job.get('validation',{}).get('state')!='passed':return summarize()
    with budget.edit() as value:value['phase']='bulk'
    # Discovery uses exchange-first cursors and deduplicates exact document IDs.
    from .disclosure_sources import query_disclosures
    start=time.monotonic();store=core.Store()
    try:
        for year in range(core.now().year-3,core.now().year):
            for market in ('SH','SZ','BJ'):
                key=f'{market}-{year}';job=budget.load();cursor=job.get('index_cursors',{}).get(key,{'page':1})
                if cursor.get('complete'):continue
                while time.monotonic()-start<max_seconds:
                    page=cursor['page'];data=query_disclosures(store,market,f'{year+1}-01-01',f'{year+1}-12-31',page)
                    if page>1 and cursor.get('provider')!=data['provider']:
                        cursor={'page':1};continue
                    with budget.edit() as value:
                        index=value.setdefault('annual_documents',{})
                        for doc in data['documents']:
                            if doc['period']==f'{year}FY':index[doc['id']]=doc
                        cursor={'page':page+1,'provider':data['provider'],'complete':page*data['page_size']>=data['total']}
                        value.setdefault('index_cursors',{})[key]=cursor
                    if cursor['complete']:break
                    time.sleep(.2)
                if time.monotonic()-start>=max_seconds:return summarize()
        master=json.loads((core.VAULT/'00_基础资料'/'证券名录.json').read_text())
        listed={r['market']+r['code'] for r in master['securities'] if r['status']=='listed'}
        failures=0
        for ident,doc in budget.load().get('annual_documents',{}).items():
            if doc['market']+doc['code'] not in listed:continue
            job=budget.load()
            if ident in job.get('bulk_results',{}):continue
            if time.monotonic()-start>=max_seconds or window(settings())['deferred']:return summarize()
            try:result=review({**doc,'year':int(doc['period'][:4]),'board':doc['market'],'document':doc});failures=0
            except core.ServiceError as error:
                failures+=1;result={'state':'failed','code':doc['code'],'error':error.code}
            with budget.edit() as value:value.setdefault('bulk_results',{})[ident]=result
            if failures>=3:
                with budget.edit() as value:value['phase']='source_or_model_blocked'
                return summarize()
        with budget.edit() as value:value['phase']='complete';value['completed_at']=core.stamp()
        return summarize()
    finally:store.conn.close()


def tick():
    phase=budget.load().get('phase')
    if phase=='pilot':return run_pilot()
    if phase in ('focused_waiting_idle','focused'):return run_focused()
    if phase in ('waiting_idle','bulk'):return run_bulk()
    return summarize()


def plan_focused():
    """Use the existing watchlist and published qualified candidates only."""
    from market_labels import market_name
    cfg=core.config();watch={r['market']+r['code']:dict(r) for r in cfg.get('watchlist',[]) if isinstance(r,dict)}
    candidates={};path=core.SITE/'reports.json'
    payload=json.loads(path.read_text()) if path.exists() else []
    reports=payload.get('reports',[]) if isinstance(payload,dict) else payload
    latest={}
    for r in sorted(reports,key=lambda v:(v.get('date',''),v.get('generated_at','')),reverse=True):latest.setdefault(r['code'],r)
    for r in latest.values():
        recent=(core.now().date()-timedelta(days=90)).isoformat()<=r.get('date','')<=core.now().date().isoformat()
        if (recent and r.get('rating') in ('buy','overweight') and r.get('evidence_status')=='sufficient'
            and r.get('score_status')=='calculated' and (r.get('score_coverage') or 0)>=85 and (r.get('score') or 0)>=65
            and not r.get('test_mode')):
            relative=r.get('detail_url','');detail_path=(core.SITE.parent/relative.removeprefix('./')).resolve()
            if not relative or not detail_path.is_relative_to(core.SITE.resolve()) or not detail_path.is_file():continue
            detail=json.loads(detail_path.read_text());growth=next((d for d in detail.get('scoring',{}).get('dimensions',[]) if d['name']=='成长'),{})
            trend=detail.get('decision',{}).get('growth',{});flags=detail.get('decision',{}).get('red_flags')
            if ((growth.get('covered_weight') or 0)<20 or (growth.get('points') or 0)<15 or (trend.get('revenue_yoy_pct') or 0)<=0
                or (trend.get('adjusted_profit_yoy_pct') or 0)<=0 or flags!=[]):continue
            from .securities import lookup
            security=lookup(r['code'])
            if security and security.get('status')=='listed':candidates[security['market']+security['code']]=security
    universe={**candidates,**watch};queue=[]
    for key,row in sorted(universe.items(),key=lambda p:(p[0] not in watch,p[0])):
        for year in range(core.now().year-1,core.now().year-4,-1):
            queue.append({k:row[k] for k in ('code','company','market')}|
                         {'id':key+'-'+str(year),'year':year,'board':market_name(row['market'],row['code']),
                          'priority':'watchlist' if key in watch else 'qualified_candidate'})
    with budget.edit() as task:
        task.update(scope_decision='focused',scope_decided_at=core.stamp(),phase='focused_waiting_idle',focused_queue=queue)
        task['focused_scope']={'watchlist_companies':len(watch),'candidate_companies':len(candidates.keys()-watch.keys()),
                              'companies':len(universe),'annuals':len(queue),'require_full_text':True,
                              'basis':'关注优先；候选沿用现有正式评级门槛，不将未评级股票擅自列为推荐；预算是上限，不要求花满'}
        task.setdefault('focused_results',{})
    return {'scope':budget.load()['focused_scope'],'queued':len(queue)}


def prepare_focused():
    """Download/parse the small scoped queue without buying model tokens."""
    from .sources import archive_pdf,extract_pdf
    store=core.Store();prepared=0
    try:
        for security in budget.load().get('focused_queue',[]):
            if security['id'] in budget.load().get('focused_documents',{}):continue
            try:
                doc=find_annual(store,security)
                pdf,sha,_=archive_pdf(doc,store);doc.update(pdf=pdf,sha256=sha)
                pdf,sha,text,pages=extract_pdf(doc);doc.update(pdf=pdf,sha256=sha,text_file=text,page_count=pages)
                with budget.edit() as task:
                    task.setdefault('focused_documents',{})[security['id']]=doc
                    if security['id'] in task.get('focused_prepare_errors',{}):task['focused_prepare_errors'][security['id']]['resolved_at']=core.stamp()
                prepared+=1
            except core.ServiceError as error:
                with budget.edit() as task:task.setdefault('focused_prepare_errors',{})[security['id']]={'code':error.code,'asof':core.stamp()}
        return {'prepared':prepared,'model_calls':0}
    finally:store.conn.close()


def run_focused(max_seconds=1800):
    from .model_timing import window
    timing=window({**settings(),'prefer_off_peak':True})
    if timing['deferred']:return {**summarize(),'waiting_for':timing['resume_at']}
    with budget.edit() as task:task['phase']='focused'
    started=time.monotonic();failures=0
    for row in budget.load().get('focused_queue',[]):
        task=budget.load()
        if row['id'] in task.get('focused_results',{}):continue
        if time.monotonic()-started>=max_seconds or window(settings())['deferred']:return summarize()
        try:
            doc=task.get('focused_documents',{}).get(row['id'])
            result=review({**row,**({'document':doc} if doc else {})},full_text=True);failures=0
            result['acceptance']='accepted' if result.get('substantive_full_coverage',result['full_text']) and result['quality_status']=='complete' and not result['conflicts'] else 'needs_review'
        except core.ServiceError as error:
            if error.code in ('model_off_peak_wait','foundation_budget_limit'):
                with budget.edit() as task:task['phase']='focused_waiting_idle' if error.code=='model_off_peak_wait' else 'needs_review';task['focused_blocker']=error.code
                return summarize()
            result={'state':'failed','code':row['code'],'error':error.code,'finished_at':core.stamp()};failures+=1
        with budget.edit() as task:task.setdefault('focused_results',{})[row['id']]=result
        if failures>=3:
            with budget.edit() as task:task['phase']='source_or_model_blocked'
            return summarize()
    with budget.edit() as task:
        results=task.get('focused_results',{}).values()
        task['phase']='complete' if all(r.get('acceptance')=='accepted' for r in results) else 'needs_review'
        task['focused_finished_at']=core.stamp()
    return summarize()
