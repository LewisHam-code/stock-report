"""Local price-change guard. No token-counting or model API is called.

Forecasts use recorded usage and serialized input sizes at the same rates.
Output length/cache are uncertain: actual overrun blocks subsequent requests.
"""
import json
import uuid
from math import log
from statistics import median
from . import core

LIMIT=1.10
POLICY_VERSION=3
RATES={'deepseek-flash':(.02,1,4),'deepseek-v4-pro':(.15,4.5,13.5)}
PURPOSES={'financial_extraction':'extraction','comprehensive_report':'narrative','deep_report':'narrative'}


def folder():return core.VAULT/'00_基础资料'/'费用变更保护'
def input_size(body):
    return len(core.dumps({k:v for k,v in body.items() if k not in ('model','max_tokens','max_output_tokens')}).encode())
def cap(body):return body.get('max_tokens',body.get('max_output_tokens',0))
def stage(purpose):return PURPOSES.get(purpose)
def cost(model,inputs,outputs,hit=0):
    rates=RATES.get(model)
    if rates is None:return None
    return (max(0,min(inputs,hit))*rates[0]+max(0,inputs-hit)*rates[1]+outputs*rates[2])/1e6


def freeze_baseline(reports):
    target=folder()/'基线.json'
    if target.is_file():return json.loads(target.read_text())
    rows=[]
    for report in reports:
        requests=(report.get('_generation_evidence') or {}).get('requests') or {}
        for name in ('extraction','narrative'):
            body=requests.get(name);meta=(report.get('model_steps') or {}).get(name) or {}
            usage=meta.get('usage') or {};model=meta.get('model') or report.get('model')
            inputs=usage.get('prompt_tokens',usage.get('input_tokens'));outputs=usage.get('completion_tokens',usage.get('output_tokens'))
            if not isinstance(body,dict) or not isinstance(inputs,int) or not isinstance(outputs,int) or model not in RATES:continue
            rows.append({'report_id':report['id'],'document_id':report['document_id'],'period':report['period'],
                         'stage':name,'model':model,'input_bytes':input_size(body),'input_tokens':inputs,'output_tokens':outputs,
                         'cache_hit_tokens':usage.get('prompt_cache_hit_tokens',0),'output_cap':cap(body),
                         'body_fingerprint':core.digest(body),'generated_at':report.get('generated_at')})
    value={'schema':1,'policy':'only_increase_over_10_percent_requires_approval','created_at':core.stamp(),
           'rates_basis':'项目已有闲时单价快照；比较均归一到同模型、同单价，不视为服务商新报价',
           'rows':rows,'baseline_id':core.digest(rows)}
    core.atomic_json(target,value);return value


def estimate(cfg,purpose,body,baseline=None):
    context=cfg.get('_financial_cost_context');name=stage(purpose)
    if not context or not name:return None
    if baseline is None and (folder()/'基线.json').is_file():baseline=json.loads((folder()/'基线.json').read_text())
    if not baseline:raise core.ServiceError('report_cost_baseline_missing','缺少改造前的同模型费用基线，未发出模型请求')
    model=cfg['model'];suffix=context.get('period','')[4:]
    options=[r for r in baseline['rows'] if r['model']==model and r['stage']==name and r['period'][4:]==suffix]
    if not options:raise core.ServiceError('report_cost_baseline_missing','缺少同模型、同报告类型费用基线，需确认后再调用')
    exact=[r for r in options if r['document_id']==context.get('id')]
    options=exact or sorted(options,key=lambda r:abs(log(max(1,input_size(body))/max(1,r['input_bytes']))))[:5]
    if cfg.get('_cost_prior_body'):
        return _same_document_estimate(cfg,body,baseline,options,name)
    forecast=[]
    for old in options:
        inputs=old['input_tokens']*input_size(body)/max(1,old['input_bytes'])
        # Hold output at recorded volume. A larger maximum alone is not a
        # predicted charge; compare the complete report and verify actual usage.
        old_cost=cost(model,old['input_tokens'],old['output_tokens'],old.get('cache_hit_tokens',0))
        same_cache=cost(model,inputs,old['output_tokens'],inputs*old.get('cache_hit_tokens',0)/max(1,old['input_tokens']))
        cold=cost(model,inputs,old['output_tokens'],0)
        forecast.append((old_cost,same_cache,cold,old))
    old_cost=median(r[0] for r in forecast);same_cache=median(r[1] for r in forecast)
    # If the request is identical, it cannot introduce new serialization/input
    # costs. Otherwise account conservatively for loss of prefix caching.
    identical=any(core.digest(body)==r[3]['body_fingerprint'] for r in forecast)
    estimated=same_cache if identical else max(same_cache,median(r[2] for r in forecast))
    references={r[3]['report_id'] for r in forecast}
    report_costs=[sum(cost(model,row['input_tokens'],row['output_tokens'],row.get('cache_hit_tokens',0))
                      for row in baseline['rows'] if row['report_id']==ident and row['model']==model) for ident in references]
    report_baseline=median(report_costs)
    report_estimate=report_baseline+estimated-old_cost
    if cfg.get('_extraction_cache_hit') and name=='narrative':
        extraction_costs=[cost(model,row['input_tokens'],row['output_tokens'],row.get('cache_hit_tokens',0))
                          for row in baseline['rows'] if row['report_id'] in references and row['model']==model and row['stage']=='extraction']
        if extraction_costs:report_estimate-=median(extraction_costs)
    run=cfg.get('_cost_run_id')
    if run:
        for path in (folder()/'用量核对'/run).glob('*.json'):
            prior=json.loads(path.read_text())
            if prior['stage']!=name:
                report_estimate+=prior['actual_same_rates_cny']-prior['old_cost_cny']
    ratio=report_estimate/report_baseline if report_baseline else float('inf')
    return {'schema':1,'policy_version':POLICY_VERSION,'model':model,'stage':name,'document_id':context.get('id'),'period':context.get('period'),
            'baseline_id':baseline['baseline_id'],'reference_reports':[r[3]['report_id'] for r in forecast],
            'old_cost_cny':old_cost,'estimated_cost_cny':estimated,'old_report_cost_cny':report_baseline,'estimated_report_cost_cny':report_estimate,'same_cache_estimate_cny':same_cache,
            'increase_ratio':ratio,'increase_pct':round((ratio-1)*100,3),'input_bytes':input_size(body),
            'body_fingerprint':core.digest(body),'output_cap_increased':cap(body)>max(r[3]['output_cap'] for r in forecast),
            'basis':'按已记录用量、输入大小和同模型闲时单价估算；输出长度及缓存命中不保证，实际仍核对返回用量'}


def _same_document_estimate(cfg,body,baseline,options,name):
    """Compare two representations of this document, not two companies.

    Historical samples only calibrate tokens/byte and output volume. Both
    counterfactuals use the current document's actual input size and cold cache.
    Returned usage replaces forecasts as each paid stage finishes.
    """
    model=cfg['model'];prior=cfg['_cost_prior_body'];context=cfg['_financial_cost_context']
    old_size=input_size(prior);new_size=input_size(body)
    pairs=[]
    for r in options:
        old_tokens=r['input_tokens']*old_size/max(1,r['input_bytes'])
        new_tokens=r['input_tokens']*new_size/max(1,r['input_bytes'])
        pairs.append((cost(model,old_tokens,r['output_tokens']),cost(model,new_tokens,r['output_tokens']),r))
    old_stage=median(p[0] for p in pairs);new_stage=median(p[1] for p in pairs)
    completed={}
    if cfg.get('_cost_run_id'):
        for path in (folder()/'用量核对'/cfg['_cost_run_id']).glob('*.json'):
            r=json.loads(path.read_text())
            if r['stage']!=name:completed[r['stage']]=r
    expected={'extraction','narrative'}
    if cfg.get('_extraction_cache_hit'):expected.discard('extraction')
    remaining=expected-set(completed)-{name}
    old_report=old_stage+sum(r['old_cost_cny'] for r in completed.values())
    new_report=new_stage+sum(r['actual_same_rates_cny'] for r in completed.values())
    references={p[2]['report_id'] for p in pairs}
    for stage_name in remaining:
        estimates=[cost(model,r['input_tokens'],r['output_tokens']) for r in baseline['rows']
                   if r['report_id'] in references and r['model']==model and r['stage']==stage_name]
        if estimates:
            value=median(estimates);old_report+=value;new_report+=value
    ratio=new_report/old_report if old_report else float('inf')
    return {'schema':1,'policy_version':POLICY_VERSION,'comparison_basis':'same_document_prior_input',
            'model':model,'stage':name,'document_id':context.get('id'),'period':context.get('period'),
            'baseline_id':baseline['baseline_id'],'reference_reports':sorted(references),
            'old_cost_cny':old_stage,'estimated_cost_cny':new_stage,'old_report_cost_cny':old_report,'estimated_report_cost_cny':new_report,
            'same_cache_estimate_cny':new_stage,'increase_ratio':ratio,'increase_pct':round((ratio-1)*100,3),
            'input_bytes':new_size,'prior_method_input_bytes':old_size,'body_fingerprint':core.digest(body),
            'prior_method_body_fingerprint':core.digest(prior),'output_cap_increased':cap(body)>cap(prior),
            'expected_paid_stages':sorted(expected),'remaining_stages':sorted(remaining),
            'basis':'同一份财报旧输入与新输入、同模型同价对比；历史样本仅校准输入分词比例和输出量，两侧均按冷缓存；实际用量另核对'}


def _archive_recomputed_legacy_hold(record,report):
    if not (record.get('state')=='requires_approval' and record.get('policy_version',1)<POLICY_VERSION
            and report.get('comparison_basis')=='same_document_prior_input' and report['increase_ratio']<=LIMIT+1e-12
            and all(record.get(k)==report.get(k) for k in ('document_id','model','stage','body_fingerprint','baseline_id'))):return False
    source=folder()/'待确认.json'
    if json.loads(source.read_text())!=record:return False
    target=folder()/'保护记录'/('same-document-recomputed-'+uuid.uuid4().hex+'.json')
    target.parent.mkdir(parents=True,exist_ok=True);source.rename(target)
    core.atomic_json(target.with_suffix('.resolution.json'),{'resolved_at':core.stamp(),'policy_version':POLICY_VERSION,
                     'reason':'旧预测把不同财报篇幅差异计为方法涨价；现按同份财报旧新输入重新核算，涨幅未超过10%',
                     'recomputed_forecast':report})
    return True


def archive_obsolete_cap_hold():
    """Retain an old false hold without releasing genuine/unknown-cost holds.

    An older in-flight worker could write a cap-only hold after the code fix.
    Only a self-consistent, explicitly sub-threshold legacy prediction qualifies.
    """
    hold=folder()/'待确认.json'
    if not hold.is_file():return None
    try:record=json.loads(hold.read_text())
    except (OSError,ValueError):return None
    if (record.get('state')!='requires_approval' or record.get('policy_version',1)!=1
            or record.get('output_cap_increased') is not True
            or any(key in record for key in ('actual_same_rates_cny','input_tokens','output_tokens'))):return None
    old=core.finite(record.get('old_report_cost_cny'));estimated=core.finite(record.get('estimated_report_cost_cny'))
    ratio=core.finite(record.get('increase_ratio'))
    if (old is None or old<=0 or estimated is None or estimated<0 or ratio is None or ratio<0
            or ratio>LIMIT+1e-12 or abs(estimated/old-ratio)>1e-8):return None
    target=folder()/'保护记录'/('legacy-cap-only-'+uuid.uuid4().hex+'.json')
    target.parent.mkdir(parents=True,exist_ok=True)
    # Do not archive a different hold that appeared during classification.
    if json.loads(hold.read_text())!=record:return None
    hold.rename(target)
    core.atomic_json(target.with_suffix('.resolution.json'),{'policy_version':POLICY_VERSION,'resolved_at':core.stamp(),
                     'reason':'旧版仅因输出上限不同暂停；记录中的整份预计费用涨幅未超过10%，原记录归档保留'})
    return target


def require_approved(cfg,purpose,body):
    if not cfg.get('_financial_cost_context') or not stage(purpose):return None
    from . import reading_costs
    if reading_costs.enabled(cfg):return reading_costs.before_request(cfg,purpose,body)
    if cfg.get('_annual_cost_policy') or cfg.get('_report_cost_policy'):
        from .annual_costs import before_request
        return before_request(cfg,purpose,body)
    hold=folder()/'待确认.json'
    archive_obsolete_cap_hold()
    if hold.is_file():
        record=json.loads(hold.read_text())
        recomputed=estimate(cfg,purpose,body) if cfg.get('_cost_prior_body') else None
        if not recomputed or not _archive_recomputed_legacy_hold(record,recomputed):
            raise core.ServiceError('report_cost_approval_required','费用变更保护处于待确认状态，未继续调用模型')
    report=estimate(cfg,purpose,body)
    if report and report['increase_ratio']>LIMIT+1e-12:
        core.atomic_json(hold,{**report,'state':'requires_approval','detected_at':core.stamp()})
        raise core.ServiceError('report_cost_approval_required','预计单份费用上涨可能超过10%，已暂停，需确认后才执行')
    return report


def record_usage(cfg,purpose,body,usage,forecast):
    if not forecast:return
    from . import reading_costs
    if forecast.get('kind')==reading_costs.KIND:return reading_costs.record_usage(cfg,purpose,body,usage,forecast)
    if forecast.get('kind') in ('annual-size-average-v1','report-size-average-v2'):
        from .annual_costs import record_usage as record_annual
        return record_annual(cfg,purpose,body,usage,forecast)
    inputs=usage.get('prompt_tokens',usage.get('input_tokens'));outputs=usage.get('completion_tokens',usage.get('output_tokens'))
    if not all(isinstance(n,int) and not isinstance(n,bool) and n>=0 for n in (inputs,outputs)):
        core.atomic_json(folder()/'待确认.json',{**forecast,'state':'usage_unknown_requires_review','observed_at':core.stamp(),
                         'note':'供应商用量不完整，响应保留，暂停后续付费请求，不能按零费用处理'})
        return
    actual=cost(cfg['model'],inputs,outputs,usage.get('prompt_cache_hit_tokens',0))
    value={**forecast,'actual_same_rates_cny':actual,'input_tokens':inputs,'output_tokens':outputs,'observed_at':core.stamp()}
    ident=core.digest([forecast['document_id'],purpose,forecast['body_fingerprint']])
    run=cfg.get('_cost_run_id') or ident
    directory=folder()/'用量核对'/run
    core.atomic_json(directory/(forecast['stage']+'.json'),value)
    receipts=[json.loads(path.read_text()) for path in directory.glob('*.json')]
    actual_total=sum(r['actual_same_rates_cny'] for r in receipts)
    expected=set(forecast.get('expected_paid_stages',['extraction','narrative']))
    if cfg.get('_extraction_cache_hit'):expected.discard('extraction')
    complete=not(expected-{r['stage'] for r in receipts})
    actual_report=actual_total if complete else max(actual_total,forecast['estimated_report_cost_cny']+actual-forecast['estimated_cost_cny'])
    value.update(actual_report_cost_cny=actual_report,report_complete=complete,
                 actual_report_increase_pct=(actual_report/forecast['old_report_cost_cny']-1)*100)
    core.atomic_json(directory/(forecast['stage']+'.json'),value)
    if actual_report>forecast['old_report_cost_cny']*LIMIT:

        core.atomic_json(folder()/'待确认.json',{**value,'state':'actual_increase_requires_approval',
                         'note':'本次返回结果先保存；后续请求停止，不能通过换模型或重试绕过'})
