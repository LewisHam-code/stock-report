"""Latest-quarter Pro queue; bounded pipelines and grouped publication."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor,ProcessPoolExecutor,wait,FIRST_COMPLETED
from multiprocessing import get_context
import fcntl
import json
import time
import traceback
import uuid
from pathlib import Path
from . import core,annual_costs,foundation_budget

HALTS={'quota_exhausted':'balance_stopped','balance_unavailable':'balance_stopped','foundation_budget_limit':'budget_stopped',
       'annual_cost_hold':'cost_approval_wait','annual_cost_policy_pending':'cost_approval_wait','invalid_key':'blocked',
       'unsafe_model_redirect':'blocked','model_off_peak_wait':'waiting_idle','current_batch_disabled':'disabled'}
LOCAL_ISSUES={'pdf_text_parse_failed','pdf_parse_failed','invalid_pdf','pdf_changed','ocr_required','financial_proof_pending',
              'deep_reading_requires_partition','correction_unresolved','security_delisted','annual_st_excluded'}
TERMINAL={'done','needs_review','failed','superseded','cost_skipped'}

def folder():return core.VAULT/'00_基础资料'/'新财季精读任务'
def load():
    path=folder()/'控制.json'
    return json.loads(path.read_text()) if path.is_file() else {}

@contextmanager
def edit():
    folder().mkdir(parents=True,exist_ok=True)
    with (folder()/'任务.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX);value=load()
        try:yield value
        except BaseException:raise
        else:value['updated_at']=core.stamp();core.atomic_json(folder()/'控制.json',value)
        finally:fcntl.flock(lock,fcntl.LOCK_UN)

def active(value=None):
    value=load() if value is None else value
    return bool(value.get('enabled') and value.get('phase') in ('ready','running','waiting_idle')
                and value.get('workspace')==core.digest(str(core.ROOT.resolve())))

def continuous():
    value=load();return active(value) and value.get('phase') in ('ready','running') and value.get('continuous_idle') is True

def managed():
    value=load()
    return value.get('policy_enabled') is True and value.get('workspace')==core.digest(str(core.ROOT.resolve()))

def current_period(at=None):
    at=at or core.now();quarter=(at.month-1)//3
    return str(at.year-1)+'FY' if quarter==0 else str(at.year)+('Q1','H1','Q3')[quarter-1]

def queue_scope():
    if not managed():return '',()
    value=load()
    return ' AND d.period=? AND d.published_at>=? ',(current_period(),value['start_inclusive'])

def eligible(row):
    from .annual_batch import exclusion
    return row.get('period')==current_period() and row.get('published_at','')[:10]>='2026-09-01' and not exclusion(row)

def initialize(store,rows):
    """Activate only the explicitly requested current-quarter scope."""
    if load():raise core.ServiceError('current_batch_exists','已有财季任务，保留其状态，不自动重置')
    selected=[r for r in rows if eligible(r)]
    done=[r for r in selected if interpreted(store,r)]
    done_ids={r['id'] for r in done};queue=[r for r in selected if r['id'] not in done_ids]
    ident='current-'+current_period()+'-'+uuid.uuid4().hex[:10]
    cost_policy={'hard_limits':{'deepseek-flash':1,'deepseek-v4-pro':5},'increase_ratio':1.1,
                 'consecutive_reports':3,'min_samples':5,'neighbor_count':10,'sample_window':20}
    core.atomic_json(folder()/'队列.json',queue)
    core.atomic_json(folder()/'已解读待核对.json',done)
    with edit() as task:
        task.update(id=ident,period=current_period(),start_inclusive='2026-09-01',policy_enabled=True,
                    workspace=core.digest(str(core.ROOT.resolve())),
                    enabled=bool(queue),phase='ready' if queue else 'complete',continuous_idle=True,workers=2,
                    total=len(queue),already_read=len(done),eligible=len(selected),cursor=0,states={},cost_policy=cost_policy,
                    created_at=core.stamp())
    reports=[interpreted(store,row)[0] for row in done]
    core.atomic_json(annual_costs.root()/annual_costs.seed_name(ident),annual_costs.seed_from_reports(reports,kind=current_period()[4:]))
    return status()

def enqueue_discovered(store):
    if not managed():return
    with (folder()/'入队.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:return _enqueue_discovered(store)
        finally:fcntl.flock(lock,fcntl.LOCK_UN)

def _enqueue_discovered(store):
    if not managed():return
    value=load();rows=json.loads((folder()/'队列.json').read_text())
    discovered=store.rows('SELECT * FROM documents WHERE period=? AND published_at>=? ORDER BY published_at,id',
                          (current_period(),value['start_inclusive']))
    grouped={}
    for row in discovered:grouped.setdefault((row['market'],row['code'],row['period']),[]).append(row)
    added=[]
    for versions in grouped.values():
        rank=lambda r:(r['published_at'][:10],any(w in r['title'] for w in ('更正','修订','更新')),r['published_at'],r['id'])
        row=max(versions,key=rank)
        if not eligible(row) or interpreted(store,row):continue
        if any(same_version(row,r) for r in rows+added):continue
        # A newer effective version supersedes a queued, not-yet-started one.
        older=[r for r in rows if (r['market'],r['code'],r['period'])==(row['market'],row['code'],row['period'])]
        if older and max(r['published_at'] for r in older)>row['published_at']:continue
        row['superseded_versions']=[r for r in versions+older if not same_version(row,r)]
        added.append(row)
    if added:
        with edit() as task:
            states=task.setdefault('states',{})
            for row in added:
                for old in row['superseded_versions']:
                    if states.get(old['id']) not in ('done','needs_review','failed') and old['id'] not in task.get('inflight',{}):
                        states[old['id']]='superseded'
            rows+=added;core.atomic_json(folder()/'队列.json',rows)
            task['total']=len(rows);task['cursor']=sum(v in TERMINAL for v in states.values())
            if task['phase']=='complete':task.update(enabled=True,phase='ready')
    return len(added)

def authorized(cfg):
    value=load()
    if not active(value) or value['id']!=cfg.get('report_batch_task'):
        raise core.ServiceError('current_batch_disabled','新财季任务已停用，已收到的结果保留')
    return value

def settings():
    value=load();cfg=core.config()
    p=next((p for p in core.model_profiles(cfg) if p['provider']=='deepseek' and p['model']=='deepseek-v4-pro' and not p.get('custom_base_url')),None)
    if p is None:raise core.ServiceError('current_pro_missing','Pro官方配置未找到')
    return {**cfg,**p,'profile_id':p['id'],'single_model':True,'test_mode':False,'prefer_off_peak':cfg.get('prefer_off_peak',True),'reading_mode':'deep','_live_execution_policy':True,
            'foundation_task':foundation_budget.load()['id'],'report_batch_task':value['id'],'_report_cost_policy':value['cost_policy'],
            'max_document_chars':600000,'_request_timeout_seconds':180}

def admit(cfg):
    value=authorized(cfg)
    from .model_timing import require_idle
    require_idle(cfg,'deep_report');annual_costs.admit(cfg)
    from .annual_batch import balance_available,exclusion
    if cfg.get('_security_code'):
        reason=exclusion({'code':cfg['_security_code']})
        if reason:raise core.ServiceError(reason,'跳过退市和ST、*ST股票')
    from datetime import datetime
    try:fresh=(core.now()-datetime.fromisoformat(value.get('balance_checked_at',''))).total_seconds()<180
    except ValueError:fresh=False
    if fresh and value.get('balance_available') is True:return
    available=balance_available(cfg)
    with edit() as task:task.update(balance_checked_at=core.stamp(),balance_available=available)
    if not available:raise core.ServiceError('quota_exhausted','余额不足，停止新财季任务')

def same_version(a,b):
    from .disclosure_sources import canonical_title
    return (a.get('market'),a.get('code'),a.get('period'))==(b.get('market'),b.get('code'),b.get('period')) and (
        bool(a.get('id') and a.get('id')==b.get('id')) or bool(a.get('source_url') and a.get('source_url')==b.get('source_url')) or
        a.get('published_at','')[:10]==b.get('published_at','')[:10] and canonical_title(a.get('title',''),a.get('company',''))==canonical_title(b.get('title',''),b.get('company','')))

def interpreted(store,row):
    for record in store.rows('SELECT r.payload,d.* FROM reports r JOIN documents d ON d.id=r.document_id WHERE d.market=? AND d.code=? AND d.period=? ORDER BY r.created_at DESC',
                             (row['market'],row['code'],row['period'])):
        report=json.loads(record.pop('payload'))
        if not report.get('test_mode') and same_version(row,record):return report,record
    return None

def receipt(row):return folder()/'结果'/(row['id']+'.json')

def prepare(store,row):
    from .report_corrections import resolve
    # Reuse an official cached original across exchange/CNINFO identities.
    candidates=store.rows('SELECT * FROM documents WHERE market=? AND code=? AND period=?',(row['market'],row['code'],row['period']))
    existing=next((d for d in candidates if same_version(row,d) and d.get('pdf')),None)
    if existing and not any(w in row['title'] for w in ('更正','修订','更新')):
        from .report_corrections import prepare as original
        return original(existing)
    return resolve(store,row)

def _process(row):
    store=core.Store()
    try:
        cfg=settings();cfg['_security_code']=row['code']
        admit(cfg)
        old=interpreted(store,row)
        if old:return {'state':'already_read','report_id':old[0]['id'],'code':row['code']}
        saved=receipt(row)
        if saved.is_file():return {**json.loads(saved.read_text()),'recovered_receipt':True}
        doc=prepare(store,row)
        store.add_document(doc)
        store.execute('UPDATE documents SET pdf=?,sha256=?,text_file=?,page_count=?,state=\'parsed\' WHERE id=?',
                      (doc['pdf'],doc['sha256'],doc['text_file'],doc['page_count'],doc['id']))
        with edit() as task:task.setdefault('downloaded_ids',[]);task['downloaded_ids']=list(set(task['downloaded_ids']+[row['id']]))
        from .sources import company_snapshot
        from .pipeline import research_benchmark
        from .valuation_inputs import verified_cashflow
        snapshot=company_snapshot(store,doc['code'],doc['market']);snapshot.update(verified_cashflow(store,doc['code']));snapshot['benchmark']=research_benchmark(store)
        # Only demonstration reports exist for these identities: never reuse a
        # demo extraction as a completed paid Pro reading.
        cfg['_benchmark_fresh_extraction']=any(json.loads(r['payload']).get('test_mode') for r in store.rows('SELECT payload FROM reports WHERE document_id=?',(doc['id'],)))
        def response(purpose,data):core.atomic_json(folder()/'模型原始响应'/row['id']/(purpose+'-'+uuid.uuid4().hex+'.json'),data)
        cfg['_model_response_observer']=response
        from .response_recovery import extraction as recover_extraction
        cfg['_extraction_recovery']=lambda document:recover_extraction(document,folder()/'模型原始响应'/row['id'])
        from .analysis import analyze
        report=analyze(store,doc,snapshot,settings=cfg)
        original=core.VAULT/'30_公司'/doc['market']/doc['code']/'研究原始'/(report['id']+'.json')
        core.atomic_json(original,report)
        # Durable before returning to the coordinator, so restarts do not buy
        # another full read after a response has already been received.
        result={'state':'model_returned','code':doc['code'],'document':doc,'report_path':str(original.relative_to(core.VAULT)),'report_id':report['id']}
        core.atomic_json(saved,result)
        from .publication_market import capture
        try:capture(report,backfill=False)
        except core.ServiceError:pass  # Receipt remains recoverable; source gaps do not buy a second read.
        return result
    finally:store.conn.close()

def _commit(result,publish_now=True):
    if result['state']!='model_returned':return
    report=json.loads((core.VAULT/result['report_path']).read_text());doc=result['document'];store=core.Store()
    try:
        store.add_document(doc)
        store.execute('UPDATE documents SET pdf=?,sha256=?,text_file=?,page_count=?,state=\'complete\',error_code=NULL,next_retry=NULL WHERE id=?',
                      (doc['pdf'],doc['sha256'],doc['text_file'],doc['page_count'],doc['id']))
        store.execute('INSERT OR IGNORE INTO reports VALUES(?,?,?,?,?)',(report['id'],doc['id'],report['version'],core.dumps(report),core.stamp()))
        with edit() as task:task['archive_pending']=True
        if publish_now:_publish_ready([report['id']],store=store)
    finally:store.conn.close()


def _publish_ready(report_ids,store=None):
    """One refresh for completed, already durable reports; safe to replay."""
    from .publish import publish
    owned=store is None;store=store or core.Store();started=time.monotonic()
    try:
        published=publish(store,{'new':0,'parsed':0,'analyzed':len(report_ids)},archive=False,incremental_ids=set(report_ids),preserve_existing=True)
        with edit() as task:task['last_publication']={**published.get('publication',{}),'at':core.stamp(),'report_count':len(report_ids)}
        return round(time.monotonic()-started,3)
    finally:
        if owned:store.conn.close()


def _flush_archive(force=False):
    """Bound full-backup frequency; every raw result is already durable."""
    if not load().get('archive_pending'):return True
    interval=core.config().get('quarter_archive_interval_seconds',0)
    if interval and not force:
        from datetime import datetime
        try:
            age=(core.now()-datetime.fromisoformat(load().get('last_archive',{}).get('at',''))).total_seconds()
            if 0<=age<interval:return True
        except (TypeError,ValueError):pass
    store=core.Store();started=time.monotonic()
    try:
        from .vault_archive import sync
        sync(store,cfg=core.config())
        with edit() as task:
            task.update(archive_pending=False,last_archive={'at':core.stamp(),'elapsed_seconds':round(time.monotonic()-started,3)})
        return True
    except Exception:
        with edit() as task:task.update(enabled=False,phase='blocked',blocker='current_archive_failed')
        return False
    finally:store.conn.close()


def _isolated_process(row):
    """Spawn-safe receipt/error protocol; no SQLite connection crosses a PID."""
    started=time.monotonic()
    try:return {'ok':True,'result':{**_process(row),'pipeline_seconds':round(time.monotonic()-started,3)}}
    except core.ServiceError as error:
        return {'ok':False,'error_code':error.code,'timing':getattr(error,'timing',{})}
    except Exception as error:
        frames=[{'module':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(error.__traceback__)]
        core.atomic_json(folder()/'问题'/(row['id']+'-工作进程异常.json'),{'error_type':type(error).__name__,'at':core.stamp(),
            'frames':frames[-10:]})
        if isinstance(error,(TypeError,ValueError,IndexError,KeyError)) and any(f['module'] in ('statement_evidence.py','pdf_fact_sources.py') for f in frames):
            return {'ok':False,'error_code':'financial_proof_pending','timing':{}}
        return {'ok':False,'unexpected':True}


def _worker_result(future,isolated):
    value=future.result()
    if not isolated:return value
    if value.get('ok'):return value['result']
    if value.get('unexpected'):raise RuntimeError('财报工作进程异常，原始资料已保留')
    error=core.ServiceError(value['error_code'],'财报处理暂未完成，原始资料已保留')
    error.timing=value.get('timing',{})
    raise error

def status():
    value=load()
    if not value:return {}
    states=Counter(value.get('states',{}).values())
    throughput={'started_at':value.get('throughput_started_at'),'completed':value.get('throughput_completed',0),'target_per_12h':[500,600]}
    if throughput['started_at']:
        from datetime import datetime
        try:
            seconds=max(0,(core.now()-datetime.fromisoformat(throughput['started_at'])).total_seconds())
            throughput['elapsed_minutes']=round(seconds/60,1)
            if seconds>=1800:
                throughput['completed_per_hour']=round(throughput['completed']*3600/seconds,1)
                throughput['projected_12h']=int(throughput['completed']*43200/seconds)
        except (TypeError,ValueError):pass
    return {k:value.get(k) for k in ('id','period','phase','enabled','cursor','total','eligible','already_read','repair_summary','workers','inflight','blocker','resume_at','updated_at','archive_pending','last_archive','last_publication')}|{
        'throughput':throughput,
        'total':value.get('total',0)-len(value.get('excluded_ids',[])),
        'cursor':value.get('cursor',0)-len(value.get('excluded_ids',[])),
        'completed':states.get('done',0),'needs_review':states.get('needs_review',0),'failed':states.get('failed',0),'cost_skipped':states.get('cost_skipped',0),
        'downloaded':len(value.get('downloaded_ids',[])),
        'cost_cny':foundation_budget.load().get('phase_costs',{}).get(value['id'],0)}

def stop():
    with edit() as task:task.update(enabled=False,continuous_idle=False,phase='disabled',blocker='user_stopped')
    return status()

def repair_existing(store):
    """Original-file recheck and missing-quote supplements, never model calls."""
    from .financial_recheck import recheck,apply
    from .publication_market import capture
    rows=json.loads((folder()/'已解读待核对.json').read_text())
    records=[]
    for row in rows:
        pair=interpreted(store,row)
        if not pair:continue
        report,doc=pair
        before=sum(m.get('verified') is True for m in report.get('financial_analysis',{}).get('metrics',[]))
        try:
            recheck(report,doc,store,force=True)
            view=apply(report)
            market=capture(view,backfill=True)
            quality=view['financial_analysis']['data_quality']
            records.append({'report_id':report['id'],'code':doc['code'],'before_verified':before,
                            'after_verified':sum(m.get('verified') is True for m in view['financial_analysis'].get('metrics',[])),
                            'quality':quality['status'],'market_state':market['state'],'state':'checked'})
        except core.ServiceError as error:
            records.append({'report_id':report['id'],'code':doc['code'],'state':'needs_review','error':error.code})
        core.atomic_json(folder()/'已有报告复核记录.json',{'checked_at':core.stamp(),'model_calls':0,'reports':records})
    summary={'checked':sum(r['state']=='checked' for r in records),'needs_review':sum(r['state']=='needs_review' for r in records),
             'financial_pending':sum(r.get('quality')!='complete' for r in records),
             'filled_metrics':sum(max(0,r.get('after_verified',0)-r.get('before_verified',0)) for r in records),'model_calls':0}
    with edit() as task:task['repair_summary']=summary
    return summary

from collections import Counter

def tick(max_seconds=300):
    if not active():return status()
    if not _flush_archive():return status()
    cfg=settings()
    try:admit(cfg)
    except core.ServiceError as error:
        with edit() as task:task.update(phase=HALTS.get(error.code,'blocked'),blocker=error.code,enabled=error.code=='model_off_peak_wait',resume_at=getattr(error,'timing',{}).get('resume_at'))
        if error.code=='model_off_peak_wait':_flush_archive(force=True)
        return status()
    rows=json.loads((folder()/'队列.json').read_text())
    with edit() as task:task.update(phase='running',blocker=None,resume_at=None,inflight={})
    started=time.monotonic();pending={};stop=False;deferred={};last_published=started
    batch_size=max(1,min(10,int(cfg.get('quarter_publish_batch_size',1))))
    def flush_ready():
        nonlocal last_published,stop
        if not deferred:return
        try:
            seconds=_publish_ready([result['report_id'] for result in deferred.values()])
        except Exception as error:
            core.atomic_json(folder()/'问题'/('批次发布-'+uuid.uuid4().hex+'.json'),{'at':core.stamp(),'error_type':type(error).__name__,'document_ids':list(deferred)})
            with edit() as task:task.update(phase='blocked',enabled=False,blocker='current_publication_failed')
            # Durable DB rows and raw receipts remain; replay refreshes them.
            stop=True;deferred.clear();return
        with edit() as task:
            events=task.setdefault('recent_completions',[])
            for ident,result in deferred.items():
                task.setdefault('states',{})[ident]='done';task.setdefault('inflight',{}).pop(ident,None)
                events.append({'at':core.stamp(),'document_id':ident,'pipeline_seconds':result.get('pipeline_seconds'),'publication_batch_seconds':seconds,'publication_batch_size':len(deferred),'reused':result['state']!='model_returned'})
                if result['state']=='model_returned' and not result.get('recovered_receipt'):
                    task['throughput_completed']=task.get('throughput_completed',0)+1
            task['recent_completions']=events[-200:];task['cursor']=sum(v in TERMINAL for v in task['states'].values())
        deferred.clear();last_published=time.monotonic()
    isolated=cfg.get('quarter_worker_processes') is True
    executor=ProcessPoolExecutor if isolated else ThreadPoolExecutor
    options={'mp_context':get_context('spawn')} if isolated else {}
    with executor(max_workers=load().get('workers',2),**options) as pool:
        while True:
            task=load()
            queued=[r for r in rows if task.get('states',{}).get(r['id']) not in TERMINAL and r['id'] not in pending.values() and r['id'] not in deferred]
            while not stop and active() and time.monotonic()-started<max_seconds and queued and len(pending)<load().get('workers',2):
                row=queued.pop(0)
                with edit() as task:task.setdefault('inflight',{})[row['id']]={'code':row['code'],'company':row['company'],'started_at':core.stamp()}
                pending[pool.submit(_isolated_process if isolated else _process,row)]=row['id']
            if not pending:
                flush_ready();break
            finished,_=wait(pending,timeout=15,return_when=FIRST_COMPLETED)
            for future in finished:
                ident=pending.pop(future);row=next(r for r in rows if r['id']==ident)
                try:
                    result=_worker_result(future,isolated)
                    if batch_size>1 and result.get('report_id'):
                        _commit(result,publish_now=False);deferred[ident]=result;state=None
                        with edit() as task:task.setdefault('inflight',{}).setdefault(ident,{})['stage']='awaiting_publication'
                    else:_commit(result);state='done'
                except core.ServiceError as error:
                    if error.code in HALTS:
                        # A sibling can notice the coordinator's stop while
                        # draining. Preserve the original fault or user stop.
                        if error.code!='current_batch_disabled':
                            with edit() as task:task.update(phase=HALTS[error.code],enabled=error.code=='model_off_peak_wait',blocker=error.code,resume_at=getattr(error,'timing',{}).get('resume_at'))
                        stop=True;state=None
                    elif error.code=='rate_limited':
                        with edit() as task:task.update(workers=1,phase='blocked',enabled=False,blocker='rate_limited')
                        stop=True;state=None
                    elif error.code in ('report_cost_limit','report_cost_unestimated'):
                        state='cost_skipped'
                        core.atomic_json(folder()/'问题'/(ident+'.json'),{'error':error.code,'code':row['code'],'state':state,'at':core.stamp()})
                    else:
                        state='needs_review' if error.code in LOCAL_ISSUES else 'failed'
                        core.atomic_json(folder()/'问题'/(ident+'.json'),{'error':error.code,'code':row['code'],'state':state,'at':core.stamp()})
                except Exception as error:
                    state=None;stop=True
                    core.atomic_json(folder()/'问题'/(ident+'-程序异常.json'),{'error_type':type(error).__name__,'at':core.stamp(),
                        'frames':[{'module':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(error.__traceback__)[-10:]]})
                    with edit() as task:task.update(phase='blocked',enabled=False,blocker='current_unexpected')
                with edit() as task:
                    if ident not in deferred:task.setdefault('inflight',{}).pop(ident,None)
                    if state:task.setdefault('states',{})[ident]=state;task['cursor']=sum(v in TERMINAL for v in task['states'].values())
            if deferred and (len(deferred)>=batch_size or not pending or time.monotonic()-last_published>=45):flush_ready()
            if stop and not pending:break
    task=load()
    if not _flush_archive(force=task.get('cursor',0)>=task.get('total',0) or task.get('phase')=='waiting_idle'):return status()
    with edit() as task:
        if active(task):task.update(phase='complete' if task['cursor']>=task['total'] else 'ready',enabled=task['cursor']<task['total'])
    return status()
