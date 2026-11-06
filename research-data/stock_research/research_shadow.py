"""Private immutable checkpoints and a deterministic ~20-company pilot.

Existing publication and market lanes call this module. No new daemon, paid
API, or database migration. All raw reports and original scores are preserved.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import fcntl
import json
import re

from . import core
from .research_profile import VERSION, CATALOG, DIMENSIONS, WEIGHTS, profile
from .operating_catalysts import discover
from .research_validation import observations,statistics


def home(vault=None):return (vault or core.VAULT)/'00_基础资料'/'开发技术资料'/'20261021-评分与研究体系升级'
def pilot_path(vault=None):return home(vault)/'试点名单-板块均衡-v2.json'


def _read(path):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return {}


def _identity(report):
    return (report.get('market') in ('SH','SZ','BJ') and re.fullmatch(r'\d{6}',str(report.get('code','')))
            and re.fullmatch(r'[a-zA-Z0-9-]{1,150}',str(report.get('id',''))))


def folder(report,vault=None):
    if not _identity(report):raise ValueError('invalid_research_identity')
    return (vault or core.VAULT)/'30_公司'/report['market']/report['code']/'六维研究'/report['id']


def _immutable(path,value):
    """Serialize writers and publish atomically; never replace a saved record."""
    path.parent.mkdir(parents=True,exist_ok=True)
    with (path.parent/'写入.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if path.exists():
            saved=_read(path)
            if not saved:raise ValueError('immutable_checkpoint_corrupt')
            return saved
        core.atomic_json(path,value)
    return value


def pages_for(doc):
    if not doc or not doc.get('text_file'):return []
    path=(core.ROOT/doc['text_file']).resolve()
    if not path.is_relative_to(core.ROOT.resolve()):return []
    value=_read(path)
    return value.get('pages',[])


def capture_generation(report,snapshot,doc,benchmark=None,store=None):
    at=report['generated_at'];data=profile(report,{**snapshot,'benchmark':benchmark or snapshot.get('benchmark',{})},asof=at)
    catalyst=discover(report,pages_for(doc),asof=at)
    frozen={'schema':1,'version':VERSION,'report_id':report['id'],'code':report['code'],
            'information_asof':at,'recorded_at':core.stamp(),'capture_kind':'at_generation',
            'disclosed_at':report.get('disclosed_at'),'source_sha256':doc.get('sha256'),
            'original_score':deepcopy(report.get('scoring')),'baseline_quote':deepcopy(report.get('quote_at_analysis')),
            'profile':data,'operating_catalysts':catalyst}
    # Isolated evaluation/test databases must never write into the real vault.
    from pathlib import Path
    db=store.conn.execute('PRAGMA database_list').fetchone()[2] if store else ''
    live=bool(db and Path(db).resolve()==(core.ROOT/'db/automation.sqlite').resolve())
    saved=_immutable(folder(report)/('原时点-'+VERSION+'.json'),frozen) if live else frozen
    # This exact object also travels in the original report database payload.
    return saved


def enrich_generation_inputs(store,snapshot,doc):
    """Public cached data before the information cutoff, isolated DBs opt out.

    The model-facing view deliberately does not include these new fields.
    They are used by deterministic research after the existing paid outputs.
    """
    from pathlib import Path
    import sqlite3
    if not isinstance(getattr(store,'conn',None),sqlite3.Connection):return snapshot
    db=store.conn.execute('PRAGMA database_list').fetchone()[2]
    if not db or Path(db).resolve()!=(core.ROOT/'db/automation.sqlite').resolve():return snapshot
    from .research_sectors import enrich
    try:
        from .research_inputs import prepare_company,_price
        from .tracking_market import overlay
        from .valuation_inputs import verified_cashflow
        company={k:doc[k] for k in ('code','market','company')}
        prepare_company(store,company,doc['period'],comparisons=True)
        _price(company,core.stamp())
        prepared=overlay({doc['code']:snapshot}).get(doc['code'],snapshot)
        prepared={**prepared,**verified_cashflow(store,doc['code'])}
        values,errors=enrich(store,{doc['code']:prepared},prepared.get('benchmark') or store.cached('benchmark-hs300-v2') or {})
        result=dict(values.get(doc['code'],prepared))
        if errors:result['research_source_gaps']=errors
        return result
    except core.ServiceError:return snapshot


def summary_fields(study):
    current=study.get('current') or {}
    return {'research_scores':{d['id']:{'score':d['score'],'coverage_pct':d['coverage_pct'],'complete':d['complete']} for d in current.get('dimensions',[])},
            'research_composites':current.get('composites',{}),'research_version':current.get('version')}


def select_pilot(reports,vault=None):
    target=pilot_path(vault)
    saved=_read(target)
    if saved:return saved
    latest={}
    for report in sorted(reports,key=lambda r:r.get('generated_at',''),reverse=True):
        if _identity(report) and not report.get('test_mode') and report.get('report_kind')!='historical_foundation':latest.setdefault(report['code'],report)
    codes=[c for c in ('002832','603466','603236') if c in latest]
    groups=defaultdict(lambda:defaultdict(list))
    for code,r in latest.items():
        if code in codes:continue
        board='科创板' if code.startswith('688') else '创业板' if code.startswith(('300','301')) else r['market']
        groups[board][r.get('industry','未知')].append(code)
    # Round-robin strata; independent of returns, prices and outcome scores.
    used_industries=defaultdict(set)
    while len(codes)<min(20,len(latest)):
        for board in sorted(groups):
            industries=[i for i in sorted(groups[board]) if groups[board][i]]
            unseen=[i for i in industries if i not in used_industries[board]]
            if industries and len(codes)<20:
                industry=(unseen or industries)[0]
                code=sorted(groups[board][industry])[0];codes.append(code);groups[board][industry].remove(code)
                used_industries[board].add(industry)
    rows=[{'code':c,'company':latest[c]['company'],'market':latest[c]['market'],'industry':latest[c].get('industry'),
           'report_id_at_selection':latest[c]['id'],'reason':'既有问题案例' if c in ('002832','603466','603236') else '板块/行业分层轮选'} for c in codes]
    return _immutable(target,{'version':VERSION,'selected_at':core.stamp(),'companies':rows,'selection_rule':'3个既有案例+确定性的板块/行业分层轮选；不使用后续收益挑选'})


def cache_industry_sources(raw_reports,vault=None):
    """Deduplicate only public source texts, not model requests or chat data."""
    index={}
    for report in raw_reports:
        documents=(report.get('_generation_evidence') or {}).get('industry_documents',{}).get('documents',[])
        for doc in documents:
            if not isinstance(doc,dict) or not doc.get('text') or not doc.get('url','').startswith('https://'):continue
            safe={k:doc[k] for k in ('url','title','published_at','published_at_basis','text','fetched_at') if k in doc}
            sha=core.digest(safe['text']);_immutable(home(vault)/'行业共享原文'/(sha+'.json'),{**safe,'source_sha256':sha})
            index[sha]={k:v for k,v in safe.items() if k!='text'}
    core.atomic_json(home(vault)/'行业共享原文/索引.json',{'sources':index,'note':'公开源文去重缓存；已有材料不自动视为已核实行业景气或公司竞争优势。'})
    return len(index)


def attach(report,snapshot,doc,original,pilot_codes,vault=None):
    at=core.stamp();base=folder(report,vault)
    reviewed=_read(home(vault)/'人工核实评价'/str(report['code']+'.json'))
    current=profile(report,snapshot,asof=at,assessments=reviewed.get('factors'))
    catalysts=discover(report,pages_for(doc),asof=at,observations=reviewed.get('operating_observations',[]))
    frozen=original.get('research_at_generation') or _read(base/('原时点-'+VERSION+'.json'))
    if frozen and frozen.get('version')!=VERSION:frozen=None
    if not frozen:
        # Reconstruct only from the actual saved generation snapshot. Do not
        # substitute today's prices and claim the new score existed then.
        historical=(original.get('_generation_evidence') or {}).get('snapshot') or {}
        frozen={'schema':1,'version':VERSION,'report_id':report['id'],'code':report['code'],
                'information_asof':original.get('generated_at'),'recorded_at':at,'capture_kind':'reconstructed',
                'source_sha256':(doc or {}).get('sha256'),'baseline_quote':original.get('quote_at_analysis'),
                'original_score':original.get('scoring'),'profile':profile(report,historical,asof=original.get('generated_at') or at),
                'note':'新规则上线后利用保留材料重建；使用了后续事实核验，不属于当时已发布的六维预测。'}
        if report['code'] in pilot_codes:frozen=_immutable(base/('后补重建-'+VERSION+'.json'),frozen)
    value={'version':VERSION,'mode':'shadow','capture_kind':frozen.get('capture_kind'),
           'original_score':frozen.get('original_score'),'original_profile':frozen.get('profile'),
           'repaired_legacy_score':report.get('scoring'),'current':current,'operating_catalysts':catalysts,
           'pilot_member':report['code'] in pilot_codes,'interpretation':'旧分保留；六维试运行不参与默认评级。后补分不进入前瞻准确率。'}
    checkpoint_member=report['code'] in pilot_codes or frozen.get('capture_kind')=='at_generation'
    if checkpoint_member:
        from .research_profile import completed_bars
        def series_input(value,n=60):
            value=value or {}
            return {**{k:value[k] for k in ('source','fetched_at','adjustment','cross_validation') if k in value},'bars':completed_bars(value,at)[-n:]}
        inputs={'quote':{k:(snapshot.get('quote') or {}).get(k) for k in ('price','pe_ttm','market_cap','asof','fetched_at','source','cross_validation','price_resolution')},
                'prices':series_input(snapshot.get('prices')),'benchmark':series_input(snapshot.get('benchmark')),
                'sector_context':{k:v for k,v in (snapshot.get('sector_context') or {}).items() if k not in ('prices','members')},
                'sector_prices':series_input(snapshot.get('sector_prices'),21),
                'ttm':{k:snapshot.get(k) for k in ('verified_ttm_cashflow','verified_ttm_capex','ttm_cashflow_evidence')},
                'research_prices_conflict':snapshot.get('research_prices_conflict'),
                'fact_ids':[m.get('fact_id') for m in (report.get('financial_analysis') or {}).get('metrics',[]) if m.get('verified')],
                'yoy_fact_ids':[m['yoy_fact_id'] for m in (report.get('financial_analysis') or {}).get('metrics',[]) if m.get('yoy_verified') and m.get('yoy_fact_id')]}
        input_hash=core.digest(inputs)
        _immutable(home(vault)/'输入快照'/(input_hash+'.json'),inputs)
        value['current_inputs_fingerprint']=input_hash
        # Content-address changes, not a new copy on every UI refresh.
        stable=deepcopy(value)
        def strip_clocks(item):
            if isinstance(item,dict):return {k:strip_clocks(v) for k,v in item.items() if k not in ('asof','checked_at','recorded_at')}
            if isinstance(item,list):return [strip_clocks(v) for v in item]
            return item
        fingerprint=core.digest(strip_clocks(stable))[:24]
        _immutable(base/('更新-'+VERSION+'-'+fingerprint+'.json'),value)
        core.atomic_json(base/'最新索引.json',{'fingerprint':fingerprint,'updated_at':at,'version':VERSION})
        outcome=observations(frozen,snapshot.get('prices') or {},snapshot.get('benchmark') or {},asof=at,sector=snapshot.get('sector_prices'))
        core.atomic_json(base/'观察结果.json',outcome)
    return value


def audit(reports,pilot,vault=None,site=None):
    chosen=[];seen=set()
    for report in reports:
        if report.get('research_profile',{}).get('pilot_member') and report['code'] not in seen:
            chosen.append(report);seen.add(report['code'])
    factors={}
    for spec in CATALOG:
        states=Counter(f['state'] for r in chosen for d in r['research_profile']['current']['dimensions'] for f in d['factors'] if f['id']==spec[0])
        factors[spec[0]]={'name':spec[2],'states':dict(states),'known_pct':round(states['known']/len(chosen)*100,1) if chosen else 0}
    value={'version':VERSION,'checked_at':core.stamp(),'companies':len(chosen),'selection':pilot,
           'factors':factors,'model_calls_added':0,'model_input_added':0,'model_cost_increase_pct':0,
           'status':'availability_pilot','production_scoring_switched':False,
           'blocking_factors':[v['name'] for v in factors.values() if v['known_pct']<70]}
    core.atomic_json(home(vault)/'试点可得性审计.json',value)
    text=['# 试点可得性审计','',f"核对时间：{value['checked_at']}；公司{len(chosen)}家。此表衡量数据可得性，不是模型正确率。",'',
          '|因子|已知比例|状态计数|','|---|---:|---|']
    text += [f"|{v['name']}|{v['known_pct']}%|{json.dumps(v['states'],ensure_ascii=False)}|" for v in factors.values()]
    text+=['','## 不能直接转生产的原因','',*['- '+x for x in value['blocking_factors']], '',
           '当前纯本地旁路没有新增模型请求、输入或模型费用；读取和存储开销另计。基础项未达到稳定取数条件时保留缺失，不填50分、不伪装完整。']
    core.atomic_text(home(vault)/'06-试点可得性审计.md','\n'.join(text)+'\n')
    core.atomic_json((site or core.SITE)/'research-method.json',{'version':VERSION,'mode':'shadow','dimensions':DIMENSIONS,'weights':WEIGHTS,
        'factor_catalog':[{'id':s[0],'dimension':s[1],'name':s[2],'weight':s[3],'unit':s[4],'formula':s[5],'ttl_class':s[6]} for s in CATALOG],
        'primary_horizon':15,'availability':{k:v for k,v in value.items() if k!='selection'},'longitudinal_validation':'awaiting_mature_prospective_samples'})
    rows=[]
    for r in reports:
        if not (r.get('research_profile',{}).get('pilot_member') or r.get('research_profile',{}).get('capture_kind')=='at_generation'):continue
        base=folder(r,vault);frozen=_read(base/('原时点-'+VERSION+'.json')) or _read(base/('后补重建-'+VERSION+'.json'))
        outcome=_read(base/'观察结果.json')
        if frozen and outcome:rows.append({'frozen':frozen,'outcomes':outcome})
    core.atomic_json(home(vault)/'前瞻统计.json',{'asof':core.stamp(),'results':[statistics(rows,n) for n in (3,7,15,30,60,90)]})
    return value


def refresh_after_market(store,vault=None):
    """Refresh pilot plus newly frozen reports in the existing market lane."""
    from pathlib import Path
    if vault is None:
        db=store.conn.execute('PRAGMA database_list').fetchone()[2]
        if not db or Path(db).resolve()!=(core.ROOT/'db/automation.sqlite').resolve():return {'state':'isolated_database'}
    pilot=_read(pilot_path(vault))
    if not pilot:return {'state':'not_initialized'}
    pilot_codes={c['code'] for c in pilot.get('companies',[])}
    companies={c['code']:c for c in pilot.get('companies',[])}
    for row in store.rows('SELECT payload FROM reports ORDER BY created_at DESC'):
        report=json.loads(row['payload']);frozen=report.get('research_at_generation') or {}
        if _identity(report) and frozen.get('capture_kind')=='at_generation' and frozen.get('version')==VERSION:
            companies.setdefault(report['code'],{k:report[k] for k in ('code','market','company')})
    from .research_inputs import refresh_prices
    from .tracking_market import overlay
    from .valuation_inputs import verified_cashflow
    price_result=refresh_prices({'companies':list(companies.values())},vault=vault)
    codes=set(companies)
    snapshots=overlay({r['code']:json.loads(r['payload']) for r in store.rows('SELECT code,payload FROM snapshots')},vault)
    benchmark=store.cached('benchmark-hs300-v2') or {}
    from .research_profile import completed_bars,moment
    at=core.stamp();clock=moment(at)
    completed=completed_bars(benchmark,at)
    if clock.strftime('%H:%M')>='15:35' and (not completed or completed[-1]['date']!=at[:10]):
        from .sources import daily_bars
        attempt_key='research-benchmark-close-'+at[:10]
        attempt=store.setting(attempt_key,{})
        if attempt.get('failures',0)<3:
            try:
                benchmark=daily_bars('000300','SH',count=640,minimum=60)
                store.cache('benchmark-hs300-v2',benchmark,hours=36)
            except core.ServiceError as error:
                store.set(attempt_key,{'failures':attempt.get('failures',0)+1,'error':error.code})
    from .research_sectors import enrich as sector_enrich
    scoped={code:s for code,s in snapshots.items() if code in codes}
    enriched,sector_errors=sector_enrich(store,scoped,benchmark)
    snapshots.update(enriched)
    for code,snapshot in enriched.items():
        if snapshot.get('sector_context'):
            core.atomic_json((vault or core.VAULT)/'30_公司'/snapshot.get('market',companies[code]['market'])/code/'行情/六维行业量价.json',snapshot['sector_context'])
    originals={};seen_pilot=set()
    for row in store.rows('SELECT r.payload,d.id,d.code FROM reports r JOIN documents d ON d.id=r.document_id ORDER BY r.created_at DESC'):
        original=json.loads(row['payload']);frozen=original.get('research_at_generation') or {}
        prospective=frozen.get('capture_kind')=='at_generation' and frozen.get('version')==VERSION
        if prospective or row['code'] in pilot_codes and row['code'] not in seen_pilot:
            originals[original['id']]=original
            if row['code'] in pilot_codes:seen_pilot.add(row['code'])
    chosen=[]
    for original in originals.values():
        code=original['code']
        target=core.SITE/'report-details'/(original['id']+'.json')
        display=_read(target)
        if not display:continue
        row=store.rows('SELECT * FROM documents WHERE id=?',(original['document_id'],))
        doc=row[0] if row else None
        snapshot={**snapshots.get(code,{}),'benchmark':benchmark,**verified_cashflow(store,code)}
        saved=store.cached('tracking-series-em-ths-v1-'+code) or {}
        if saved.get('prices'):
            latest=lambda prices:max((b.get('date','') for b in prices.get('bars',[])),default='')
            if latest(saved['prices'])>latest(snapshot.get('prices') or {}):snapshot['prices']=saved['prices']
        display['research_profile']=attach(display,snapshot,doc,original,pilot_codes,vault)
        core.atomic_json(target,display);chosen.append(display)
    index=core.SITE/'reports.json';listing=_read(index)
    changed={r['id']:r for r in chosen}
    if listing:
        for item in listing.get('reports',[]):
            if item.get('id') in changed:item.update(summary_fields(changed[item['id']]['research_profile']))
        listing['generated_at']=core.stamp();core.atomic_json(index,listing)
    audit(chosen,pilot,vault=vault)
    return {'state':'updated','companies':len(chosen),'daily_prices':dict(Counter(r['state'] for r in price_result)),'sector_errors':sector_errors,'model_calls':0}
