"""Bounded, real-provider comparisons. Raw outputs and every paid attempt persist."""
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor,as_completed
from . import core,foundation_budget

LOCK=threading.RLock()
MODELS=('deepseek-flash','deepseek-v4-pro')
RATES={'deepseek-flash':(.02,1,4),'deepseek-v4-pro':(.15,4.5,13.5)}

def home():return core.VAULT/'00_基础资料'/'模型成本对照'
def read(root):return json.loads((root/'实验.json').read_text())
def write(root,data):core.atomic_json(root/'实验.json',data)

def charge(model,usage,multiplier=1,rates=None):
    inputs=usage.get('input_tokens',usage.get('prompt_tokens'))
    outputs=usage.get('output_tokens',usage.get('completion_tokens'))
    if not all(isinstance(n,int) and not isinstance(n,bool) and n>=0 for n in (inputs,outputs)):
        raise core.ServiceError('benchmark_missing_usage','供应商未返回完整用量，保留费用预留，不虚构零费用')
    hit=usage.get('prompt_cache_hit_tokens',0)
    if not isinstance(hit,int) or not 0<=hit<=inputs:hit=0
    cached,miss,out=(rates or RATES)[model]
    return (hit*cached+(inputs-hit)*miss+outputs*out)*multiplier/1e6

def prepare():
    from .foundation_job import find_annual
    from .disclosure_sources import query_exchange
    from .sources import archive_pdf,extract_pdf,query_announcements,document_from_record,cninfo_identity,excerpt
    cfg=core.config();profiles={p['model']:p for p in core.model_profiles(cfg) if p['provider']=='deepseek' and p['model'] in MODELS and not p['custom_base_url']}
    if set(profiles)!=set(MODELS):raise core.ServiceError('benchmark_profile_missing','需保留两个DeepSeek官方模型的现有配置')
    root=home()/('金徽股份-'+core.now().strftime('%Y%m%d-%H%M%S'));root.mkdir(parents=True,exist_ok=False)
    docs={};store=core.Store()
    try:
        saved=foundation_budget.load().get('focused_documents',{}).get('SH603132-2025')
        annual=dict(saved) if saved else find_annual(store,{'code':'603132','company':'金徽股份','market':'SH','year':2025})
        docs['2025FY']=annual
        try:
            listing=query_exchange('SH','2026-07-01',core.now().date().isoformat(),1,'603132')
            choices=[d for d in listing['documents'] if d['code']=='603132' and d['period']=='2026H1']
        except core.ServiceError:choices=[]
        if not choices:
            listing=query_announcements('2026-07-01',core.now().date().isoformat(),'sse',1,stock=cninfo_identity(store,'603132'))
            choices=[d for row in listing['announcements'] if (d:=document_from_record(row,'sse')) and d['code']=='603132' and d['period']=='2026H1']
        if not choices:raise core.ServiceError('benchmark_half_year_missing','尚未找到代码与期间相符的官方半年报')
        docs['2026H1']=max(choices,key=lambda d:(d['published_at'],d['id']))
        for period,doc in docs.items():
            pdf,sha,_=archive_pdf(doc,store);doc.update(pdf=pdf,sha256=sha)
            if not doc.get('text_file') or not (core.ROOT/doc['text_file']).is_file():
                pdf,sha,text,pages=extract_pdf(doc);doc.update(pdf=pdf,sha256=sha,text_file=text,page_count=pages)
            text,coverage=excerpt(doc,max_chars=600000)
            if not coverage['full_text'] or len(text.encode())>900000:
                raise core.ServiceError('benchmark_full_text_too_large','全文超过本轮单次输入保护，尚未购买模型调用')
        from .foundation_data import financial_for
        snapshot={'financial':financial_for('603132','SH') or {'history':[]},
                  'errors':[{'part':'valuation','code':'controlled_cost_comparison_only'}],
                  '_fixed_industry_documents':{'documents':[],'errors':[],'asof':core.stamp()}}
        value={'id':root.name,'created_at':core.stamp(),'state':'prepared','experiment_cap_cny':30,
               'pricing_checked_at':core.now().date().isoformat(),'prices_per_million_cny':RATES,
               'documents':docs,'snapshot':snapshot,'profile_ids':{m:p['id'] for m,p in profiles.items()},'calls':{},'results':{},
               'methods':{'coarse':{'max_chars':140000,'mode':'initial'},'deep':{'max_chars':600000,'mode':'deep'}},
               'policy':'8组；每组两阶段；各模型独立提取；无外部行业检索；提取关闭思考、综合解读low；每次输出上限16384；闲时执行；同一公开原文和财务快照；成本为供应商用量按价核算，不是账户账单'}
        write(root,value)
        return root
    finally:store.conn.close()

class BenchStore:
    def __init__(self,root,group,model,attempt=1):self.base=core.Store();self.root=root;self.group=group;self.model=model;self.attempt=attempt;self.ticket=None
    def __getattr__(self,name):return getattr(self.base,name)
    def reserve_api_call(self,purpose,limit):
        from .model_timing import window
        with LOCK:
            job=read(self.root);calls=job['calls'];spent=sum(c.get('cost',c['maximum']) for c in calls.values())
            if len(calls)>=24 or spent+10>job['experiment_cap_cny']:
                raise core.ServiceError('benchmark_limit','本轮实验达到请求或费用保护，停止继续购买')
            shared=foundation_budget.load()
            existing=foundation_budget.committed(shared)-shared.get('external_costs',{}).get(job['id'],0)
            if existing+spent+10>shared.get('budget_cny',1000):raise core.ServiceError('benchmark_shared_budget','本轮与补建合计已接近总预算')
            ident=uuid.uuid4().hex;self.ticket=ident
            multiplier=2 if window({'provider':'deepseek','model':self.model,'prefer_off_peak':True})['deferred'] else 1
            calls[ident]={'group':self.group,'model':self.model,'purpose':purpose,'attempt':self.attempt,'started_at':core.stamp(),'state':'reserved','maximum':10,'multiplier':multiplier}
            write(self.root,job)
        try:return self.base.reserve_api_call('benchmark:'+job['id']+':'+self.group+':'+purpose,10**9)
        except Exception:
            self.observe(purpose,{'prompt_tokens':0,'completion_tokens':0},None);raise
    def response(self,purpose,data):
        # Provider replies contain only the authorized public-research response,
        # never request headers or credentials. Keep failed JSON for diagnosis.
        core.atomic_json(self.root/('响应-'+self.ticket+'.json'),data)
    def observe(self,purpose,usage,response_model):
        with LOCK:
            job=read(self.root);call=job['calls'][self.ticket]
            call.update(usage=usage,response_model=response_model,finished_at=core.stamp())
            try:call.update(cost=charge(self.model,usage,call['multiplier'],job.get('prices_per_million_cny')),state='usage_returned')
            except core.ServiceError:call.update(cost=call['maximum'],state='usage_unknown')
            write(self.root,job)
    def close(self):
        if self.ticket:
            with LOCK:
                job=read(self.root);call=job['calls'][self.ticket]
                if call['state']=='reserved':call.update(state='usage_unknown',cost=call['maximum'],finished_at=core.stamp());write(self.root,job)
        self.base.conn.close()

def group(root,period,model,method,retry_failed=False):
    from .analysis import analyze
    key=period+'-'+model+'-'+method;job=read(root)
    if key in job['results']:
        if not retry_failed or job['results'][key]['state']!='failed':return job['results'][key]
        with LOCK:
            job=read(root)
            history=job.setdefault('result_attempts',{}).setdefault(key,[])
            if len(history)>=2:raise core.ServiceError('benchmark_retry_limit','同一组合最多三次尝试，停止继续购买')
            history.append(job['results'][key]);write(root,job)
    attempt=len(job.get('result_attempts',{}).get(key,[]))+1
    profiles=core.model_profiles();profile=next(p for p in profiles if p['id']==job['profile_ids'][model])
    store=BenchStore(root,key,model,attempt)
    options=job.get('cell_options',{}).get(model,{}).get(method,{})
    cfg={**core.config(),**profile,'single_model':True,'profile_id':profile['id'],'prefer_off_peak':True,
         'reading_mode':job['methods'][method]['mode'],'max_document_chars':job['methods'][method]['max_chars'],
         'omit_administrative_content':options.get('omit_administrative_content',job.get('omit_administrative_content',False)),
         'initial_report_thinking':job.get('initial_report_thinking_by_model',{}).get(model,'low'),
         'reuse_verified_pdf_metrics':options.get('reuse_verified_pdf_metrics',job.get('reuse_verified_pdf_metrics',False)),
         'max_output_tokens':16384,'_request_timeout_seconds':300,
         '_benchmark_fresh_extraction':True,'_benchmark_allow_coarse':True,'_model_usage_observer':store.observe,'_model_response_observer':store.response}
    try:
        report=analyze(store,dict(job['documents'][period]),job['snapshot'],settings=cfg)
        report['review_context']={'kind':'controlled_model_benchmark','formal_tracking_eligible':False}
        core.atomic_json(root/(key+'.json'),report)
        core.atomic_text(root/(key+'.md'),'# 金徽股份 '+period+' '+model+' '+method+'\n\n'+
                         '本页为受控成本对照，不进入正式解读追踪。\n\n'+report['conclusion']+'\n\n## 证据\n\n'+report['evidence']+'\n\n## 风险\n\n'+report['risks']+'\n\n原文：'+report['source']+'\n')
        quality=report['financial_analysis']['data_quality'];coverage=report['financial_analysis']['coverage']
        result={'state':'complete','attempt':attempt,'period':period,'model':model,'method':method,'report_id':report['id'],
                'read_pages':len(coverage['read_pages']),'total_pages':coverage['total_pages'],'full_text':coverage['full_text'],
                'core_verified':quality['verified_count'],'core_expected':quality['expected_count'],'quality':quality['status']}
    except core.ServiceError as error:result={'state':'failed','attempt':attempt,'period':period,'model':model,'method':method,'error':error.code}
    except Exception as error:result={'state':'failed','period':period,'model':model,'method':method,'error':'benchmark_unexpected','error_type':type(error).__name__}
    finally:store.close()
    with LOCK:
        job=read(root);job['results'][key]=result;write(root,job)
    return result

def summary(root):
    job=read(root);rows=[]
    for key,result in job['results'].items():
        calls=[c for c in job['calls'].values() if c['group']==key];usage=[c.get('usage',{}) for c in calls]
        inputs=sum(u.get('prompt_tokens',u.get('input_tokens',0)) for u in usage)
        outputs=sum(u.get('completion_tokens',u.get('output_tokens',0)) for u in usage)
        hits=sum(u.get('prompt_cache_hit_tokens',0) for u in usage)
        reasons=sum((u.get('completion_tokens_details') or {}).get('reasoning_tokens',0) for u in usage)
        known=all(c['state']=='usage_returned' for c in calls)
        idle=sum(charge(c['model'],c['usage'],rates=job.get('prices_per_million_cny')) for c in calls) if known else None
        successful=[c for c in calls if c.get('attempt',1)==result.get('attempt',1)] if result['state']=='complete' else []
        success_usage=[c.get('usage',{}) for c in successful]
        success_input=sum(u.get('prompt_tokens',u.get('input_tokens',0)) for u in success_usage)
        success_output=sum(u.get('completion_tokens',u.get('output_tokens',0)) for u in success_usage)
        success_cost=sum(c['cost'] for c in successful) if successful and all(c['state']=='usage_returned' for c in successful) else None
        rows.append({**result,'calls':len(calls),'input_tokens':inputs,'cache_hit_tokens':hits,'output_tokens':outputs,'reasoning_tokens':reasons,
                     'successful_input_tokens':success_input,'successful_output_tokens':success_output,
                     'successful_cache_hit_tokens':sum(u.get('prompt_cache_hit_tokens',0) for u in success_usage),
                     'successful_cost_cny':success_cost,
                     'successful_uncached_idle_cny':charge(result['model'],{'prompt_tokens':success_input,'completion_tokens':success_output},rates=job.get('prices_per_million_cny')) if success_cost is not None else None,
                     'failed_attempt_cost_cny':sum(c.get('cost',c['maximum']) for c in calls if c not in successful),
                     'estimated_charged_cny':sum(c.get('cost',c['maximum']) for c in calls),'idle_equivalent_cny':idle,
                     'peak_equivalent_cny':idle*2 if idle is not None else None,
                     'uncached_idle_cny':charge(result['model'],{'prompt_tokens':inputs,'completion_tokens':outputs},rates=job.get('prices_per_million_cny')) if known else None})
    value={'id':job['id'],'asof':core.stamp(),'state':job['state'],'rows':rows,'calls':len(job['calls']),
           'estimated_total_cny':sum(c.get('cost',c['maximum']) for c in job['calls'].values()),
           'pricing_source':'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
           'note':'推理token包含在输出内，不能重复加费；供应商缓存按实际命中计价，另列不命中的可比成本；单份样本不外推全市场；失败尝试也保留成本'}
    core.atomic_json(root/'汇总.json',value)
    lines=['# 金徽股份模型成本对照','',job['policy'],'',
           '|期间|模型|方式|覆盖页数|输入token|缓存命中|输出token|其中推理|闲时费用（元）|高峰折算（元）|状态|',
           '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|']
    for r in rows:
        money=lambda v:'待核对' if v is None else f'{v:.6f}'
        lines.append('|'+ '|'.join(str(v) for v in (r['period'],r['model'],r['method'],str(r.get('read_pages','?'))+'/'+str(r.get('total_pages','?')),r['input_tokens'],r['cache_hit_tokens'],r['output_tokens'],r['reasoning_tokens'],money(r['idle_equivalent_cny']),money(r['peak_equivalent_cny']),r['state']))+'|')
    lines+=['',value['note'],'','## 成功流程与失败费用分开','',
            '|期间|模型|方式|成功输入token|成功输出token|成功流程闲时费用（元）|不命中缓存参考（元）|失败尝试费用（元）|',
            '|---|---|---|---:|---:|---:|---:|---:|']
    for r in rows:
        lines.append('|'+ '|'.join(str(v) for v in (r['period'],r['model'],r['method'],r['successful_input_tokens'],r['successful_output_tokens'],money(r['successful_cost_cny']),money(r['successful_uncached_idle_cny']),money(r['failed_attempt_cost_cny'])))+'|')
    lines+=['','原文：']+[d['source_url'] for d in job['documents'].values()]
    core.atomic_text(root/'汇总-自动生成.md','\n'.join(lines)+'\n')
    return value

def run(root):
    from .model_timing import window
    if window({'provider':'deepseek','model':'deepseek-flash','prefer_off_peak':True})['deferred']:
        return {'state':'waiting_idle','resume_at':window({'provider':'deepseek','model':'deepseek-flash','prefer_off_peak':True})['resume_at']}
    with LOCK:job=read(root);job['state']='running';write(root,job)
    failures=0
    # Different models may run in parallel; each method stays independently billed.
    cells=[(p,m,d) for p in ('2025FY','2026H1') for d in ('coarse','deep') for m in MODELS]
    with ThreadPoolExecutor(max_workers=2) as pool:
        for offset in range(0,len(cells),2):
            futures=[pool.submit(group,root,*cell) for cell in cells[offset:offset+2]]
            for f in as_completed(futures):
                result=f.result();failures=failures+1 if result['state']=='failed' else 0
                print(core.dumps(result),flush=True)
            if failures>=3:break
    with LOCK:
        job=read(root);job['state']='complete' if len(job['results'])==8 and all(v['state']=='complete' for v in job['results'].values()) else 'needs_review';write(root,job)
    value=summary(root)
    with foundation_budget.edit() as task:
        task.setdefault('external_costs',{})[job['id']]=value['estimated_total_cny']
        task['estimated_spend_cny']=foundation_budget.settled_cost(task)
    return value
