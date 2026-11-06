"""Size-adjusted annual-report averages, per-report caps and three strikes.

All records stay in the vault. No model/token-count API, timer or service.
Page count is a secondary size signal; billed input tokens drive the estimate.
"""
from contextlib import contextmanager
import fcntl
import json
import re
import uuid
from math import log
from statistics import mean
from . import core

KIND='annual-size-average-v1'
REPORT_KIND='report-size-average-v2'

def policy(cfg):return cfg.get('_report_cost_policy') or cfg['_annual_cost_policy']
def task_id(cfg):return cfg.get('report_batch_task') or cfg['one_off_annual_task']
def period_kind(cfg):return (cfg.get('_financial_cost_context',{}).get('period') or '2025FY')[4:]
def seed_name(task):return '财季规模平均基线.json' if str(task).startswith('current-') else '年报规模平均基线.json'


def root():return core.VAULT/'00_基础资料'/'费用变更保护'
def home(task):
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,100}',str(task)):raise ValueError('invalid task identity')
    return root()/('财季费用' if str(task).startswith('current-') else '年报费用')/task


@contextmanager
def locked(task):
    base=home(task);base.mkdir(parents=True,exist_ok=True)
    with (base/'费用.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:yield base
        finally:fcntl.flock(lock,fcntl.LOCK_UN)


def read(path,default):return json.loads(path.read_text()) if path.is_file() else default
def ledger_path(base,doc,model):return base/'单份'/(core.digest([doc,model])+'.json')
def stream(model,mode):return model+':'+mode


def document_spending(base,doc):
    calls=[]
    for model in ('deepseek-flash','deepseek-v4-pro'):
        calls.extend(read(ledger_path(base,doc,model),{}).get('calls',{}).values())
    return sum(c.get('actual_cost_cny') or 0 for c in calls),any(c.get('usage_unknown') for c in calls)


def admit(cfg):
    from .reading_costs import enabled
    if enabled(cfg):return
    state=read(home(task_id(cfg))/'状态.json',{})
    if state.get('hold'):raise core.ServiceError('annual_cost_hold','年报费用保护已暂停，请核对异常记录')


def trimmed_average(values):
    values=sorted(values);trim=len(values)//10
    return mean(values[trim:len(values)-trim] if trim else values)


def reference(cfg,name,input_tokens,pages):
    seed=read(root()/seed_name(task_id(cfg)),{'rows':[]})
    state=read(home(task_id(cfg))/'状态.json',{})
    rows=seed['rows']+state.get('samples',[])
    mode=cfg.get('reading_mode','deep');model=cfg['model']
    rows=[r for r in rows if r['model']==model and r['stage']==name and r.get('reading_mode')==mode
          and r.get('period_kind')==period_kind(cfg) and r.get('input_tokens',0)>0]
    # Prefer recent observations of each document, never overweight reruns.
    unique={}
    for r in rows:unique[(r['document_id'],r['model'],r['stage'])]=r
    rows=list(unique.values())
    comparable=[r for r in rows if .5<=r['input_tokens']/max(1,input_tokens)<=2]
    minimum=policy(cfg).get('min_samples',5)
    if len(comparable)<minimum:
        return {'ready':False,'sample_count':len(comparable),'expected_output_tokens':None,
                'reason':'相近规模完整精读样本不足，先在硬上限内积累，不把篇幅差异判作涨价'}
    def distance(r):
        size=abs(log(r['input_tokens']/max(1,input_tokens)))
        if pages and r.get('source_pages'):size+=.2*abs(log(r['source_pages']/pages))
        return size
    rows=sorted(comparable,key=distance)[:policy(cfg).get('neighbor_count',10)]
    return {'ready':True,'sample_count':len(rows),'expected_output_tokens':trimmed_average([r['output_tokens'] for r in rows]),
            'mean_input_tokens':trimmed_average([r['input_tokens'] for r in rows]),
            'mean_source_pages':mean([r['source_pages'] for r in rows if r.get('source_pages')]) if any(r.get('source_pages') for r in rows) else None,
            'reference_documents':[r['document_id'] for r in rows]}


def before_request(cfg,purpose,body):
    from .report_cost_guard import cost,input_size,cap,stage
    limits=policy(cfg);model=cfg['model'];name=stage(purpose)
    limit=core.finite(limits.get('hard_limits',{}).get(model))
    if limit is None or limit<=0:raise core.ServiceError('annual_cost_policy_pending','单份费用硬上限尚未确认，未发出请求')
    task=task_id(cfg);context=cfg['_financial_cost_context'];doc=context['id']
    # UTF-8 bytes plus wrapper allowance provide a conservative text-token
    # ceiling; the complete requested output cap is reserved before transport.
    from .model_timing import price_multiplier
    multiplier=price_multiplier(cfg)
    upper=cost(model,len(core.dumps(body).encode())+1024,cap(body))
    if upper is None:raise core.ServiceError('annual_cost_policy_pending','该模型单价尚未核实，未发出请求')
    upper*=multiplier
    with locked(task) as base:
        state=read(base/'状态.json',{})
        if state.get('hold'):raise core.ServiceError('annual_cost_hold','年报费用保护已暂停，请核对异常记录')
        ledger=read(ledger_path(base,doc,model),{'document_id':doc,'model':model,'calls':{},'runs':[]})
        paid,unknown=document_spending(base,doc)
        if unknown:
            raise core.ServiceError('annual_cost_hold','本份报告存在未知用量，未继续付费')
        if paid+upper>limit+1e-12:
            state['hold']={'reason':'hard_limit','document_id':doc,'model':model,'already_paid_cny':paid,
                           'next_request_upper_cny':upper,'hard_limit_cny':limit,'at':core.stamp()}
            core.atomic_json(base/'状态.json',state)
            raise core.ServiceError('annual_cost_hold','本份报告的请求费用预留将超过硬上限，已暂停')
    token_guess=max(1,input_size(body)/3)
    prior=cfg.get('_cost_prior_body',body)
    return {'kind':REPORT_KIND if cfg.get('report_batch_task') else KIND,'task':task,'request_id':uuid.uuid4().hex,'run_id':cfg['_cost_run_id'],
            'document_id':doc,'model':model,'stage':name,'reading_mode':cfg.get('reading_mode','deep'),'period_kind':period_kind(cfg),
            'source_pages':context.get('page_count'),'source_text_chars':context.get('source_text_chars'),
            'source_pdf_bytes':context.get('source_pdf_bytes'),'input_bytes':input_size(body),'prior_input_bytes':input_size(prior),
            'hard_limit_cny':limit,'request_upper_cny':upper,'tariff_multiplier':multiplier,'extraction_cache_hit':bool(cfg.get('_extraction_cache_hit')),
            'preflight_reference':reference(cfg,name,token_guess,context.get('page_count'))}


def record_usage(cfg,purpose,body,usage,forecast):
    from .report_cost_guard import cost
    inputs=usage.get('prompt_tokens',usage.get('input_tokens'));outputs=usage.get('completion_tokens',usage.get('output_tokens'))
    valid=all(isinstance(v,int) and not isinstance(v,bool) and v>=0 for v in (inputs,outputs))
    hit=usage.get('prompt_cache_hit_tokens',0)
    hit=hit if isinstance(hit,int) and not isinstance(hit,bool) and valid and 0<=hit<=inputs else 0
    event={**forecast,'recorded_at':core.stamp(),'valid_response':False,'usage_unknown':not valid}
    if valid:
        ref=reference(cfg,forecast['stage'],inputs,forecast.get('source_pages'))
        # Normalize to the old representation of this same source. A longer
        # annual report raises input cost normally; duplicate input does not.
        normal_input=inputs*min(1,forecast['prior_input_bytes']/max(1,forecast['input_bytes']))
        expected=(cost(forecast['model'],normal_input,ref['expected_output_tokens'],hit*normal_input/max(1,inputs)) if ref['ready'] else None)
        event.update(input_tokens=inputs,output_tokens=outputs,cache_hit_tokens=hit,reference=ref,
                     actual_cost_cny=cost(forecast['model'],inputs,outputs,hit)*forecast.get('tariff_multiplier',1),
                     same_rate_cost_cny=cost(forecast['model'],inputs,outputs,hit),expected_cost_cny=expected,
                     input_expanded=forecast['input_bytes']>forecast['prior_input_bytes']*1.1)
    else:event.update(actual_cost_cny=None,expected_cost_cny=None)
    with locked(forecast['task']) as base:
        path=ledger_path(base,forecast['document_id'],forecast['model'])
        ledger=read(path,{'document_id':forecast['document_id'],'model':forecast['model'],'calls':{},'runs':[]})
        if forecast['request_id'] in ledger['calls']:return
        ledger['calls'][forecast['request_id']]=event;core.atomic_json(path,ledger)
        state=read(base/'状态.json',{})
        spent,_=document_spending(base,forecast['document_id'])
        if not valid or spent>forecast['hard_limit_cny']+1e-12:
            state['hold']={'reason':'usage_unknown' if not valid else 'hard_limit','document_id':forecast['document_id'],
                           'model':forecast['model'],'actual_cost_cny':spent,'hard_limit_cny':forecast['hard_limit_cny'],'at':core.stamp()}
            core.atomic_json(base/'状态.json',state)


def mark_valid(forecast):
    if not forecast or forecast.get('kind') not in (KIND,REPORT_KIND):return
    with locked(forecast['task']) as base:
        path=ledger_path(base,forecast['document_id'],forecast['model']);ledger=read(path,{})
        event=ledger.get('calls',{}).get(forecast['request_id'])
        if event:event['valid_response']=True;core.atomic_json(path,ledger)


def transport_finished(cfg,purpose,body,usage,forecast):
    if not forecast or forecast.get('kind') not in (KIND,REPORT_KIND):return
    with locked(forecast['task']) as base:
        ledger=read(ledger_path(base,forecast['document_id'],forecast['model']),{})
        missing=forecast['request_id'] not in ledger.get('calls',{})
    if missing:record_usage(cfg,purpose,body,usage,forecast)


def seed_from_reports(reports,kind='FY'):
    """Pure extraction of complete deep-reading samples; originals untouched."""
    from .report_cost_guard import input_size,cost
    samples={};report_rows={}
    for report in sorted(reports,key=lambda r:r.get('generated_at','')):
        if report.get('period','')[4:]!=kind:continue
        coverage=(report.get('financial_analysis') or {}).get('coverage') or {}
        if not (coverage.get('full_text') or coverage.get('all_source_pages_selected')):continue
        requests=(report.get('_generation_evidence') or {}).get('requests') or {}
        extraction=requests.get('extraction') or {}
        system=extraction.get('system') or extraction.get('instructions') or '\n'.join(m.get('content','') for m in extraction.get('messages',[]) if m.get('role')=='system')
        if '本轮为全文精读' not in system:continue
        report_cost=0;paid_models=set()
        for name in ('extraction','narrative'):
            meta=(report.get('model_steps') or {}).get(name) or {};usage=meta.get('usage') or {}
            i=usage.get('prompt_tokens',usage.get('input_tokens'));o=usage.get('completion_tokens',usage.get('output_tokens'))
            if not isinstance(i,int) or not isinstance(o,int) or i<=0 or o<0:continue
            model=meta.get('model') or report.get('model');body=requests.get(name)
            if not body or model not in ('deepseek-flash','deepseek-v4-pro'):continue
            row={'document_id':report['document_id'],'model':model,'stage':name,'reading_mode':'deep','period_kind':kind,
                 'source_pages':coverage.get('total_pages'),'input_tokens':i,'output_tokens':o,'input_bytes':input_size(body),
                 'report_id':report['id'],'generated_at':report.get('generated_at')}
            samples[(report['document_id'],model,name)]=row
            if not (name=='extraction' and report.get('input_optimization',{}).get('extraction_cache_hit')):
                report_cost+=cost(model,i,o,usage.get('prompt_cache_hit_tokens',0));paid_models.add(model)
        if len(paid_models)==1:
            model=next(iter(paid_models));report_rows[(report['document_id'],model)]={'document_id':report['document_id'],'model':model,
                'source_pages':coverage.get('total_pages'),'cost_cny':report_cost}
    summaries={}
    for model in ('deepseek-flash','deepseek-v4-pro'):
        items=[r for r in report_rows.values() if r['model']==model]
        if items:summaries[model]={'reports':len(items),'mean_cost_cny':mean(r['cost_cny'] for r in items),
            'mean_pages':mean(r['source_pages'] for r in items if r.get('source_pages')) if any(r.get('source_pages') for r in items) else None}
    return {'schema':1,'created_at':core.stamp(),'rows':list(samples.values()),'report_summary':summaries,
            'basis':'仅完整精读'+('年报' if kind=='FY' else kind+'财报')+'；同份同模型同阶段只保留最新样本；用实际返回输入/输出token建立规模相近的截尾平均值，旧选读样本不混入'}


def finish_report(cfg,report):
    from .reading_costs import enabled
    if enabled(cfg):return
    if not (cfg.get('_annual_cost_policy') or cfg.get('_report_cost_policy')):return
    limits=policy(cfg);model=cfg['model'];doc=report['document_id'];task=task_id(cfg)
    key=stream(model,cfg.get('reading_mode','deep'))
    with locked(task) as base:
        path=ledger_path(base,doc,model);ledger=read(path,{'calls':{},'runs':[]})
        if ledger.get('finished') and ledger.get('finished_call_count')==len(ledger['calls']):return
        calls=list(ledger['calls'].values())
        if not calls:return
        state=read(base/'状态.json',{'streams':{},'samples':[]})
        streams=state.setdefault('streams',{});summary=streams.setdefault(key,{'consecutive_anomalies':0,'recent_reports':[]})
        actual=sum(c.get('actual_cost_cny') or 0 for c in calls)
        normalized=sum(c.get('same_rate_cost_cny',c.get('actual_cost_cny')) or 0 for c in calls)
        unique={}
        for c in calls:unique.setdefault(c['stage'],c)
        ready=all(c.get('expected_cost_cny') is not None and c.get('valid_response') for c in unique.values())
        expected=sum(c.get('expected_cost_cny') or 0 for c in unique.values()) if ready else None
        reasons=[]
        if any(c.get('input_expanded') for c in calls):reasons.append('同份财报输入膨胀')
        if len(calls)>len(unique):reasons.append('重复付费请求')
        if any(c.get('reference',{}).get('ready') and c['output_tokens']>c['reference']['expected_output_tokens']*limits.get('increase_ratio',1.1) for c in calls):
            reasons.append('相近规模报告的输出异常变长')
        anomaly=bool(ready and expected and normalized>expected*limits.get('increase_ratio',1.1) and reasons)
        result={'report_id':report['id'],'document_id':doc,'model':model,'source_pages':calls[-1].get('source_pages'),
                'actual_cost_cny':actual,'expected_cost_cny':expected,'anomaly':anomaly,'reasons':reasons if anomaly else [],
                'baseline_ready':ready,'same_rate_cost_cny':normalized,'change_pct':(normalized/expected-1)*100 if expected else None,'at':core.stamp()}
        recent=summary['recent_reports']
        existing=next((i for i,r in enumerate(recent) if r['document_id']==doc),None)
        if existing is None:recent.append(result)
        else:recent[existing]=result
        summary['recent_reports']=recent[-limits.get('sample_window',20):]
        streak=0
        for r in reversed(summary['recent_reports']):
            if not r['anomaly']:break
            streak+=1
        summary['consecutive_anomalies']=streak
        summary['average_cost_cny']=mean(r['actual_cost_cny'] for r in summary['recent_reports'])
        summary['average_source_pages']=mean(r['source_pages'] for r in summary['recent_reports'] if r.get('source_pages')) if any(r.get('source_pages') for r in summary['recent_reports']) else None
        if summary['consecutive_anomalies']>=limits.get('consecutive_reports',3):
            state['hold']={'reason':'three_consecutive_anomalies','stream':key,'last_report':result,
                           'consecutive_anomalies':summary['consecutive_anomalies'],'at':core.stamp()}
        # Learn only successful, non-anomalous reports; preserve anomaly history
        # without allowing it to push up the normal rolling average.
        if not anomaly:
            samples=state.setdefault('samples',[])
            samples.extend({k:c[k] for k in ('document_id','model','stage','reading_mode','period_kind','input_tokens','output_tokens','source_pages')}
                           for c in unique.values() if c.get('valid_response') and not c.get('usage_unknown'))
            state['samples']=samples[-120:]
        ledger.update(finished=True,finished_call_count=len(calls),result=result);core.atomic_json(path,ledger);core.atomic_json(base/'状态.json',state)


def status(task):
    state=read(home(task)/'状态.json',{})
    seed=read(root()/seed_name(task),{})
    return {'hold':state.get('hold'),'streams':state.get('streams',{}),'historical_average':seed.get('report_summary',{})}
