"""Per-document spending admission and durable skips; no global cost pause."""
from contextlib import contextmanager
import fcntl
import uuid
from . import core

KIND='financial-per-report-cap-v1'
SKIPS={'report_cost_limit','report_cost_unestimated'}

def enabled(cfg):return (cfg.get('financial_reading_cost_policy') or {}).get('mode')=='per_report_cap'
def folder():return core.VAULT/'00_基础资料'/'费用变更保护'/'单份费用上限'
def read(path,default):
    import json
    return json.loads(path.read_text()) if path.is_file() else default

@contextmanager
def locked():
    folder().mkdir(parents=True,exist_ok=True)
    with (folder()/'费用.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(lock,fcntl.LOCK_UN)

def ledger_path(doc):return folder()/'记录'/(core.digest(doc)+'.json')
def legacy_spending(cfg,doc):
    task=cfg.get('report_batch_task') or cfg.get('one_off_annual_task')
    if not task:return 0
    from .annual_costs import home,ledger_path as old_path
    calls=[]
    for model in ('deepseek-flash','deepseek-v4-pro'):
        calls.extend(read(old_path(home(task),doc,model),{}).get('calls',{}).values())
    # Unknown old usage retains its reserved maximum, never a fabricated zero.
    return sum(c.get('actual_cost_cny') if c.get('actual_cost_cny') is not None else c.get('request_upper_cny',5) for c in calls)

def _alert(context,code,limit,estimate,paid,model):
    alerts=read(folder()/'跳过提醒.json',{})
    key=core.digest(context['id']);old=alerts.get(key,{})
    alerts[key]={'document_id':context['id'],'company':context.get('company',''),'code':context.get('code',''),
        'period':context.get('period',''),'reason':code,'limit_cny':limit,'estimated_total_cny':estimate,
        'already_used_or_reserved_cny':paid,'model':model,'at':core.stamp(),'occurrences':old.get('occurrences',0)+1}
    core.atomic_json(folder()/'跳过提醒.json',alerts)

def before_request(cfg,purpose,body):
    from .report_cost_guard import cost,cap,stage
    from .model_timing import price_multiplier
    context=cfg['_financial_cost_context'];doc=context['id'];model=cfg['model']
    limit=core.finite(cfg['financial_reading_cost_policy'].get('limit_cny'))
    if limit is None or limit<=0:raise core.ServiceError('report_cost_policy_invalid','单份费用上限配置无效')
    upper=cost(model,len(core.dumps(body).encode())+1024,cap(body)) if cfg.get('provider')=='deepseek' and not cfg.get('custom_base_url') else None
    multiplier=price_multiplier(cfg)
    if upper is not None:upper*=multiplier
    with locked():
        ledger=read(ledger_path(doc),{'document_id':doc,'calls':{}})
        paid=legacy_spending(cfg,doc)+sum(c.get('cost_cny',c['upper_cny']) for c in ledger['calls'].values())
        estimate=paid+upper if upper is not None else None
        code='report_cost_unestimated' if estimate is None else 'report_cost_limit' if estimate>limit+1e-12 else None
        if code:
            _alert(context,code,limit,estimate,paid,model)
            raise core.ServiceError(code,'无法核实模型单价，已跳过本份并继续队列' if estimate is None else f'整份报告累计预估超过{limit:.2f}元，已跳过本份并继续队列')
        ident=uuid.uuid4().hex
        ledger['calls'][ident]={'state':'reserved','upper_cny':upper,'model':model,'purpose':purpose,'at':core.stamp()}
        core.atomic_json(ledger_path(doc),ledger)
    return {'kind':KIND,'request_id':ident,'document_id':doc,'model':model,'tariff_multiplier':multiplier,
        'stage':stage(purpose),'hard_limit_cny':limit,'request_upper_cny':upper,'estimated_report_cost_cny':estimate}

def record_usage(cfg,purpose,body,usage,forecast):
    from .report_cost_guard import cost
    inputs=usage.get('prompt_tokens',usage.get('input_tokens'));outputs=usage.get('completion_tokens',usage.get('output_tokens'))
    valid=all(isinstance(v,int) and not isinstance(v,bool) and v>=0 for v in (inputs,outputs))
    with locked():
        path=ledger_path(forecast['document_id']);ledger=read(path,{'calls':{}})
        item=ledger['calls'].get(forecast['request_id'])
        if not item or item['state']!='reserved':return
        if valid:
            hit=usage.get('prompt_cache_hit_tokens',0)
            hit=hit if isinstance(hit,int) and not isinstance(hit,bool) and 0<=hit<=inputs else 0
            item.update(state='settled',cost_cny=cost(forecast['model'],inputs,outputs,hit)*forecast['tariff_multiplier'],input_tokens=inputs,output_tokens=outputs)
        else:item.update(state='usage_unknown',cost_cny=item['upper_cny'])
        item['finished_at']=core.stamp();core.atomic_json(path,ledger)
        paid=legacy_spending(cfg,forecast['document_id'])+sum(c.get('cost_cny',c['upper_cny']) for c in ledger['calls'].values())
        if paid>forecast['hard_limit_cny']+1e-12:
            _alert(cfg['_financial_cost_context'],'report_cost_limit',forecast['hard_limit_cny'],paid,paid,forecast['model'])

def cancel_request(cfg,forecast):
    if forecast and forecast.get('kind')==KIND:
        record_usage(cfg,'',{}, {'prompt_tokens':0,'completion_tokens':0},forecast)

def status(cfg):
    policy=cfg.get('financial_reading_cost_policy') or {}
    rows=sorted(read(folder()/'跳过提醒.json',{}).values(),key=lambda r:r['at'],reverse=True)
    return {'enabled':enabled(cfg),'limit_cny':policy.get('limit_cny'),
        'relative_increase_limit':False if enabled(cfg) else True,'total_budget_limit':False if enabled(cfg) else True,
        'skipped_count':len(rows),'skipped':rows[:20]}
