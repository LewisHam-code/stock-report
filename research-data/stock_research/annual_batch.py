"""Removable, one-off annual-report queue, driven by the existing worker.

No new timer, service, schema migration, or changes to ordinary run limits.
The control file is separate from historical research and the daily workflow.
"""
from contextlib import contextmanager
import fcntl
import json
import time
import urllib.error
import urllib.request
import uuid

from . import core, foundation_budget as budget

ACTIVE = {'ready', 'running', 'waiting_idle'}
STOP_ERRORS = {'quota_exhausted':'balance_stopped', 'balance_unavailable':'balance_check_failed',
               'annual_cost_hold':'cost_approval_wait','annual_cost_policy_pending':'cost_approval_wait',
               'foundation_budget_limit':'budget_stopped', 'invalid_key':'blocked',
               'unsafe_model_redirect':'blocked','report_cost_approval_required':'cost_approval_wait','report_cost_baseline_missing':'cost_approval_wait', 'annual_batch_disabled':'disabled'}


def folder():
    return budget.folder()/'最近一年年报一次性任务'


def load():
    path=folder()/'控制.json'
    return json.loads(path.read_text()) if path.is_file() else {}


@contextmanager
def edit():
    folder().mkdir(parents=True,exist_ok=True)
    with (folder()/'任务.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        value=load()
        try:
            yield value
        except BaseException:
            raise
        else:
            value['updated_at']=core.stamp()
            core.atomic_json(folder()/'控制.json',value)
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)


def active(value=None):
    value=load() if value is None else value
    return bool(value.get('enabled') and value.get('phase') in ACTIVE
                and value.get('workspace')==core.digest(str(core.ROOT.resolve())))


def authorized(cfg):
    value=load()
    if not active(value) or value.get('id')!=cfg.get('one_off_annual_task'):
        raise core.ServiceError('annual_batch_disabled','一次性任务已停用，原队列和底稿保留')
    if cfg.get('_foundation_exception_review') and not value.get('pro_exception_review'):
        raise core.ServiceError('foundation_model_not_allowed','本任务未授权异常复核模型')
    return value


def exclusion(row):
    from .securities import lookup,risk_flag
    current=lookup(row['code']) or row
    if current.get('status')=='delisted':return 'security_delisted'
    flag=current.get('risk_flag') or risk_flag(current.get('company',''))
    if flag in ('ST','*ST','退市整理'):return 'annual_st_excluded'
    return None


def settings(pro=False):
    value=load();cfg=core.config()
    wanted='deepseek-v4-pro' if pro else 'deepseek-flash'
    profile=next((p for p in core.model_profiles(cfg) if p['provider']=='deepseek' and p['model']==wanted
                  and not p.get('custom_base_url')),None)
    if profile is None:
        raise core.ServiceError('annual_model_missing','一次性任务所需的官方模型配置不存在')
    return {**cfg,**profile,'profile_id':profile['id'],'single_model':True,
            'foundation_task':budget.load()['id'],'one_off_annual_task':value['id'],
            '_foundation_exception_review':pro,'_benchmark_fresh_extraction':pro,
            'compact_financial_tables':bool(value.get('validated_input_method')=='verified-markdown-v1'),
            **({'_annual_cost_policy':value['annual_cost_policy']} if value.get('annual_cost_policy') else {}),
            'prefer_off_peak':True,'reading_mode':'deep','max_document_chars':600000,
            '_request_timeout_seconds':180}


def balance_available(cfg):
    """Free account availability query. Never persist keys or HTTP bodies."""
    from .models import model_open
    request=urllib.request.Request('https://api.deepseek.com/user/balance',
        headers={'Authorization':'Bearer '+core.api_key('deepseek',cfg['profile_id'])})
    try:
        with model_open(request,timeout=20) as response:
            value=json.load(response)
        if not isinstance(value,dict) or not isinstance(value.get('is_available'),bool):
            raise ValueError('invalid availability')
        return value['is_available']
    except core.ServiceError:
        raise
    except urllib.error.HTTPError as error:
        if error.code==402:return False
        raise core.ServiceError('balance_unavailable','账户余额查询暂不可用，已暂停付费任务') from None
    except (ValueError,TypeError,OSError):
        raise core.ServiceError('balance_unavailable','账户余额查询暂不可用，已暂停付费任务') from None


def admit(cfg):
    value=authorized(cfg)
    if cfg.get('_annual_cost_policy'):
        from .annual_costs import admit as cost_admit
        cost_admit(cfg)
    if cfg.get('_security_code'):
        reason=exclusion({'code':cfg['_security_code']})
        if reason:raise core.ServiceError(reason,'本次任务跳过已退市或ST、*ST股票')
    # Price admission also occurs immediately before each transport call.
    from .model_timing import require_idle
    require_idle(cfg,'deep_report')
    checked=value.get('balance_checked_at')
    from datetime import datetime
    fresh=False
    if checked:
        try:fresh=(core.now()-datetime.fromisoformat(checked)).total_seconds()<180
        except (ValueError,TypeError):pass
    if fresh and value.get('balance_available') is True and value.get('balance_profile')==cfg['profile_id']:return
    available=balance_available(cfg)
    with edit() as task:
        task.update(balance_checked_at=core.stamp(),balance_available=available,balance_profile=cfg['profile_id'])
        if not available:task.update(phase='balance_stopped',enabled=False,blocker='quota_exhausted')
    if not available:
        raise core.ServiceError('quota_exhausted','API余额不足，一次性任务已停止，已完成底稿保留')


def initialize(year=None):
    """Explicit CLI action only; the periodic worker never creates this task."""
    if load():return status()
    year=year or core.now().year-1
    master=json.loads((core.VAULT/'00_基础资料'/'证券名录.json').read_text())
    from market_labels import market_name
    watch={r['market']+r['code'] for r in core.config().get('watchlist',[])}
    queue=[];seen=set();new_listings=0
    for row in master['securities']:
        key=row['market']+row['code'];board=market_name(row['market'],row['code'])
        if row.get('status')!='listed' or not board or key in seen:continue
        if exclusion(row):continue
        seen.add(key)
        if str(row.get('listed_at') or '')[:4].isdigit() and int(row['listed_at'][:4])>year:
            # Listing after year end does not prove that no annual report
            # exists. Query its official source; pay only for an exact year.
            new_listings+=1
        queue.append({k:row[k] for k in ('code','market','company')}|
                     {'id':key+'-'+str(year),'board':board,'year':year,'fixed_year':True,
                      'priority':'watchlist' if key in watch else 'market'})
    queue.sort(key=lambda r:(r['priority']!='watchlist',r['code'],r['market']))
    core.atomic_json(folder()/'队列.json',queue)
    with edit() as task:
        task.update(id='annual-'+str(year)+'-'+uuid.uuid4().hex[:10],year=year,enabled=True,phase='ready',
                    workspace=core.digest(str(core.ROOT.resolve())),created_at=core.stamp(),cursor=0,
                    total=len(queue),excluded_new_listings=0,new_listings_to_check=new_listings,reviewed=0,accepted=0,needs_review=0,
                    unavailable=0,failed=0,downloaded=0,pro_reviewed=0,consecutive_errors=0,skipped_risk=0,
                    pro_exception_review=True,reference_balance_cny=314.97,
                    models={'precision':'deepseek-flash','exception_review':'deepseek-v4-pro'},
                    policy='一次性最近完整年度精读；高峰等待；余额不足或总预算到限停止；完成后自动停用；不修改日常策略')
    core.atomic_text(folder()/'任务说明.md',
        '# 最近一年年报一次性补建\n\n任务完成后自动停用，日常采集与新财报解读继续沿用原设置。\n\n'
        '停用方式：设置页点击“停用一次性补建”。已发出的响应会保存，后续模型请求停止；原文、底稿、用量账本保留。\n\n'
        '状态和队列在本目录，研究底稿在各公司“历史年报解读”。正文省略行政重复内容，保留来源页码；超长或缺证报告转待复核。\n'
        'Pro仅复核原文冲突、报表勾稽失败或多个核心指标被原文纠正的报告，不作为Flash失败后的自动替补。\n')
    return status()


def stop():
    if not load():return status()
    with edit() as task:
        task.update(enabled=False,phase='disabled',stopped_at=core.stamp())
    return status()


def archive():
    """Remove the temporary UI entry without deleting research or schedules."""
    if not load():return status()
    with edit() as task:
        task.update(enabled=False,phase='archived',archived=True,archived_at=core.stamp())
    return status()


def status():
    value=load()
    result={k:value.get(k) for k in ('id','year','enabled','phase','archived','total','cursor','reviewed','accepted',
        'needs_review','unavailable','failed','downloaded','pro_reviewed','current','blocker','resume_at',
        'balance_checked_at','updated_at','excluded_new_listings','skipped_risk','quality_recheck','continuous_idle')}
    if value.get('id'):result['estimated_spend_cny']=budget.load().get('phase_costs',{}).get(value['id'],0)
    if value.get('annual_cost_policy'):
        from .annual_costs import status as cost_status
        result['cost_control']={**cost_status(value['id']),'policy':value['annual_cost_policy']}
    if value.get('enabled') and value.get('phase')=='waiting_idle':
        from .model_timing import window
        # A stored deferral describes the previous worker check. Re-evaluate
        # the clock for display without starting paid work from a status read.
        result['time_policy']=window({'provider':'deepseek','model':'deepseek-flash','prefer_off_peak':True})
    return result


def _hold(error):
    code=error.code
    phase='waiting_idle' if code=='model_off_peak_wait' else STOP_ERRORS.get(code,'blocked')
    with edit() as task:
        task.update(phase=phase,blocker=code,enabled=phase=='waiting_idle')
        if code=='model_off_peak_wait':task['resume_at']=error.timing.get('resume_at')
    return status()


def _receipt(row,stage='flash'):
    return folder()/'结果'/f'{row["id"]}-{stage}.json'


def _read_receipt(row,stage='flash'):
    path=_receipt(row,stage)
    result=json.loads(path.read_text()) if path.is_file() else None
    if result and result.get('report_path'):
        report_path=(core.VAULT/result['report_path']).resolve()
        if report_path.is_relative_to(core.VAULT.resolve()) and report_path.is_file():
            from .financial_recheck import apply
            report=apply(json.loads(report_path.read_text()))
            if report.get('financial_recheck'):
                q=report['financial_analysis']['data_quality']
                result.update(quality_status=q['status'],conflicts=q['conflicts'],failed_checks=q['failed_checks'],parser_status=q['parser_status'],
                              critical_corrections=0,acceptance=report['financial_recheck']['review_state'])
                result.update(review_routing(result))
    return result


def sync_report_library():
    task=load()
    if not task:return
    from .historical_reports import sync_receipts
    fingerprint=[task.get(k) for k in ('reviewed','pro_reviewed','cursor','phase')]
    sync_receipts(folder()/'结果',task['id'],fingerprint)


def review_routing(result):
    """Do not buy a second model read to diagnose a local proof failure."""
    local=(['原文数值冲突，先核对解析与报表口径'] if result.get('conflicts') else [])+(
        ['报表勾稽未通过，先核对原表与单位'] if result.get('failed_checks') else [])
    if result.get('parser_status') and result['parser_status']!='checked':local.append('本地原表校验未完成')
    pro=['多个核心指标的模型提取值被已核实原文纠正'] if result.get('critical_corrections',0)>=3 and not local else []
    return {'local_review_reasons':local,'exception_reasons':pro}


def _review(row,pro=False,doc=None):
    from .foundation_job import review
    stage='pro' if pro else 'flash';cfg=settings(pro)
    cfg['_security_code']=row['code']
    def response(purpose,value):
        path=folder()/'模型原始响应'/row['id']/f'{stage}-{purpose}-{uuid.uuid4().hex}.json'
        core.atomic_json(path,value)
    cfg['_model_response_observer']=response
    def prepared(document):
        path=folder()/'已备原文'/f'{row["id"]}.json'
        if not path.is_file():
            core.atomic_json(path,document)
            with edit() as task:task['downloaded']+=1
    cfg['_document_prepared_observer']=prepared
    with edit() as task:task['current']={'code':row['code'],'market':row['market'],'stage':stage,'started_at':core.stamp()}
    result=review({**row,**({'document':doc} if doc else {})},full_text=True,settings_override=cfg)
    result.update(review_routing(result))
    result['acceptance']='accepted' if result.get('substantive_full_coverage') and result['quality_status']=='complete' and not result['conflicts'] else 'needs_review'
    core.atomic_json(_receipt(row,stage),result)
    from .historical_reports import publish_receipt
    receipt_path=_receipt(row,stage);stat=receipt_path.stat()
    publish_receipt(result,'pro_review' if pro else 'flash_deep',
                    cfg['one_off_annual_task']+'/'+receipt_path.name,[stat.st_mtime_ns,stat.st_size])
    return result


def tick(max_seconds=3000,max_documents=None):
    """A bounded slice under pipeline.run_lock; safe when this file is absent."""
    if not active():return status()
    cfg=settings()
    try:admit(cfg)
    except core.ServiceError as error:return _hold(error)
    with edit() as task:task.update(phase='running',blocker=None,resume_at=None)
    queue=json.loads((folder()/'队列.json').read_text());started=time.monotonic();processed=0
    while active() and load()['cursor']<len(queue):
        if time.monotonic()-started>=max_seconds or (max_documents is not None and processed>=max_documents):break
        task=load();row=queue[task['cursor']]
        try:
            reason=exclusion(row)
            if reason:raise core.ServiceError(reason,'本次任务跳过已退市或ST、*ST股票')
            admit(cfg)
            result=_read_receipt(row)
            if result is None:
                prepared=folder()/'已备原文'/f'{row["id"]}.json'
                saved=json.loads(prepared.read_text()) if prepared.is_file() else budget.load().get('focused_documents',{}).get(row['id'])
                result=_review(row,doc=saved)
            with edit() as task:
                if task.get('counted_cursor',-1)<task['cursor']:
                    task['reviewed']+=1
                    task['accepted' if result['acceptance']=='accepted' else 'needs_review']+=1
                    task['counted_cursor']=task['cursor']
            # A received Flash result is durable before optional Pro work.
            if result.get('exception_reasons'):
                if _read_receipt(row,'pro') is None:_review(row,pro=True,doc=result.get('document'))
            with edit() as task:
                task.update(cursor=task['cursor']+1,consecutive_errors=0,current=None)
                if _read_receipt(row,'pro') is not None:task['pro_reviewed']+=1
        except core.ServiceError as error:
            if error.code in STOP_ERRORS or error.code=='model_off_peak_wait':return _hold(error)
            if error.code in ('security_delisted','annual_st_excluded'):
                state='skipped_risk'
            elif error.code=='foundation_annual_not_found':
                state='unavailable'
            elif error.code in ('foundation_full_text_too_large','deep_reading_requires_partition','ocr_required','financial_proof_pending','pdf_text_parse_failed','pdf_parse_failed','invalid_pdf','pdf_changed'):
                state='needs_review'
            else:state='failed'
            details=getattr(error,'details',{})
            safe_details={k:details[k] for k in ('page','parser_error') if k in details}
            core.atomic_json(folder()/'问题'/f'{row["id"]}.json',{'code':row['code'],'state':state,'error':error.code,'details':safe_details,'at':core.stamp()})
            with edit() as task:
                task['cursor']+=1;task[state]=task.get(state,0)+1;task['current']=None
                task['consecutive_errors']=task.get('consecutive_errors',0)+1 if state=='failed' and task.get('last_error')==error.code else (1 if state=='failed' else 0)
                task['last_error']=error.code
                if task['consecutive_errors']>=3:task.update(phase='blocked',enabled=False,blocker=error.code)
            if not active():return status()
        except Exception as error:
            # An unexpected local error must not turn into hourly paid retries.
            import traceback
            from pathlib import Path
            frames=[{'module':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
                    for frame in traceback.extract_tb(error.__traceback__)[-12:]]
            core.atomic_json(folder()/'问题'/f'{row["id"]}-程序异常.json',{
                'code':row['code'],'state':'blocked','error':'annual_unexpected',
                'error_type':type(error).__name__,'frames':frames,'at':core.stamp()})
            with edit() as task:task.update(phase='blocked',enabled=False,blocker='annual_unexpected')
            return status()
        processed+=1
    with edit() as task:
        if task['cursor']>=task['total']:task.update(phase='complete',enabled=False,finished_at=core.stamp(),current=None)
        elif active(task):task['phase']='ready'
    return status()
