"""Explicit one-off task budget, separate from the normal daily call quota."""
import fcntl
import json
import uuid
from contextlib import contextmanager
from . import core

def folder():return core.VAULT/'00_基础资料'/'基础补建任务'
def path():return folder()/'任务.json'

@contextmanager
def edit():
    folder().mkdir(parents=True,exist_ok=True)
    with (folder()/'预算.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        value=json.loads(path().read_text()) if path().exists() else {}
        try:yield value
        finally:
            core.atomic_json(path(),value)
            fcntl.flock(lock,fcntl.LOCK_UN)

def load():return json.loads(path().read_text()) if path().exists() else {}

def committed(value):
    return (value.get('archived_cost_cny',0)+sum(r.get('cost',r['maximum']) for r in value.get('calls',{}).values())
            +sum(value.get('external_costs',{}).values()))

def settled_cost(value):
    return (value.get('archived_cost_cny',0)+sum(r.get('cost',r['maximum']) for r in value.get('calls',{}).values() if r['state']!='reserved')
            +sum(value.get('external_costs',{}).values()))

def all_calls(value):
    import gzip
    for record in value.get('call_archives',[]):
        yield from json.loads(gzip.decompress(bytes.fromhex((folder()/'调用账本'/record['file']).read_text()))).values()
    yield from value.get('calls',{}).values()

def _compact(value):
    # Keep the frequently updated control file bounded. Complete call records
    # remain in immutable Obsidian ledger chunks, including unknown usage.
    finished={k:v for k,v in value.get('calls',{}).items() if v['state']!='reserved'}
    if len(finished)<200:return
    import gzip
    name='调用账本-'+uuid.uuid4().hex+'.json.gz.hex'
    core.atomic_text(folder()/'调用账本'/name,gzip.compress(core.dumps(finished).encode()).hex())
    value['archived_cost_cny']=value.get('archived_cost_cny',0)+sum(v['cost'] for v in finished.values())
    value.setdefault('call_archives',[]).append({'file':name,'count':len(finished)})
    value['calls']={k:v for k,v in value['calls'].items() if k not in finished}

def reserve(cfg,purpose,body):
    task=cfg.get('foundation_task')
    if not task:return None
    one_off=cfg.get('report_batch_task') or cfg.get('one_off_annual_task')
    if cfg.get('report_batch_task'):
        from .current_batch import authorized
        authorized(cfg)
    elif one_off:
        from .annual_batch import authorized
        authorized(cfg)
    pro=bool(one_off and (cfg.get('_foundation_exception_review') or cfg.get('report_batch_task')) and cfg.get('model')=='deepseek-v4-pro')
    if cfg.get('provider')!='deepseek' or (cfg.get('model')!='deepseek-flash' and not pro):
        raise core.ServiceError('foundation_model_not_allowed','补建预算仅授权Flash，不自动调用其他模型')
    # UTF-8 byte length is a conservative token ceiling for the sent text;
    # include serialization overhead and the full output cap, at peak prices.
    maximum=(len(core.dumps(body).encode())*(9 if pro else 2)+body.get('max_tokens',10000)*(27 if pro else 8))/1_000_000
    with edit() as value:
        if value.get('id')!=task or (not one_off and value.get('phase') not in ('pilot','bulk','focused')):
            raise core.ServiceError('foundation_inactive','补建任务未启用，不能绕过日常额度')
        spent=committed(value)
        from .reading_costs import enabled
        per_report=enabled(cfg) and bool(cfg.get('_financial_cost_context'))
        if not per_report and spent+maximum>value.get('budget_cny',0):
            raise core.ServiceError('foundation_budget_limit','已接近补建预算，保留队列等待核对')
        ident=uuid.uuid4().hex
        from .model_timing import price_multiplier
        multiplier=price_multiplier(cfg)
        value.setdefault('calls',{})[ident]={'purpose':purpose,'phase':one_off or value['phase'],'model':cfg.get('model'),'maximum':maximum,'started_at':core.stamp(),
                                            'document_id':(cfg.get('_financial_cost_context') or {}).get('id'),'run_id':cfg.get('_cost_run_id'),
                                            'multiplier':multiplier,'state':'reserved'}
        value['reserved_cny']=sum(r['maximum'] for r in value['calls'].values() if r['state']=='reserved')
    return ident

def settle(ident,usage):
    if not ident:return
    with edit() as task:
        item=task['calls'][ident]
        if item['state']!='reserved':return
        inputs=usage.get('input_tokens',usage.get('prompt_tokens'))
        outputs=usage.get('output_tokens',usage.get('completion_tokens'))
        if isinstance(inputs,int) and isinstance(outputs,int) and inputs>=0 and outputs>=0:
            hit=usage.get('prompt_cache_hit_tokens',0)
            hit=hit if isinstance(hit,int) and 0<=hit<=inputs else 0
            rates=(.15,4.5,13.5) if item.get('model')=='deepseek-v4-pro' else (.02,1,4)
            item.update(state='usage_returned',input_tokens=inputs,output_tokens=outputs,cache_hit_tokens=hit,
                        cost=(hit*rates[0]+(inputs-hit)*rates[1]+outputs*rates[2])*item['multiplier']/1_000_000)
        else:item.update(state='usage_unknown',cost=item['maximum'])
        item['finished_at']=core.stamp()
        phase=item['phase']
        task.setdefault('phase_costs',{})[phase]=task.get('phase_costs',{}).get(phase,0)+item['cost']
        _compact(task)
        task['estimated_spend_cny']=settled_cost(task)
        task['reserved_cny']=sum(r['maximum'] for r in task['calls'].values() if r['state']=='reserved')
        task['cost_basis']='按返回用量与请求时段估算；未知用量按预留上限计，实际以服务商账单为准'
