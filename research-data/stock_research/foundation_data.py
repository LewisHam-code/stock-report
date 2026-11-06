"""Resumable, model-free, whole-market statement collection into Obsidian."""
from __future__ import annotations
import gzip
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from . import core

ENDPOINT='https://datacenter-web.eastmoney.com/api/data/v1/get'
TABLES={
 'income':('RPT_DMSK_FN_INCOME','REPORT_DATE',{'revenue':'TOTAL_OPERATE_INCOME','operating_revenue':'OPERATE_INCOME','operating_cost':'OPERATE_COST','net_profit':'PARENT_NETPROFIT','adjusted_profit':'DEDUCT_PARENT_NETPROFIT'}),
 'balance':('RPT_DMSK_FN_BALANCE','REPORT_DATE',{'total_assets':'TOTAL_ASSETS','total_liabilities':'TOTAL_LIABILITIES','total_equity':'TOTAL_EQUITY','cash':'MONETARYFUNDS','receivables':'ACCOUNTS_RECE','inventory':'INVENTORY'}),
 'cashflow':('RPT_DMSK_FN_CASHFLOW','REPORT_DATE',{'operating_cashflow':'NETCASH_OPERATE','capex':'CONSTRUCT_LONG_ASSET'}),
 'indicators':('RPT_LICO_FN_CPD','REPORTDATE',{'eps':'BASIC_EPS','roe':'WEIGHTAVG_ROE','gross_margin':'XSMLL','net_profit':'PARENT_NETPROFIT','revenue':'TOTAL_OPERATE_INCOME'})}

def root():return core.VAULT/'00_基础资料'/'财务底库'

def quarter_ends(through,count=12):
    return sorted(f'{y}-{md}' for y in range(through.year-count//4-2,through.year+1)
                  for md in ('03-31','06-30','09-30','12-31') if f'{y}-{md}'<=through.isoformat())[-count:]

def periods(today=None):
    today=today or core.now().date()
    annual=[f'{y}-12-31' for y in range(today.year-6,today.year)]
    quarters=quarter_ends(today)
    return annual,quarters

def page(table,period,number):
    from .sources import get_json
    report,field,_=TABLES[table]
    data=get_json(ENDPOINT,dict(reportName=report,columns='ALL',filter=f"({field}='{period}')",
                              pageNumber=number,pageSize=500,sortTypes='1',sortColumns='SECURITY_CODE'),timeout=25,limit=12_000_000)
    result=data.get('result') or {}
    if not data.get('success') or not isinstance(result.get('data'),list) or not isinstance(result.get('count'),int):
        raise core.ServiceError('foundation_format','结构化报表分页未通过验证')
    if any(str(r.get(field,''))[:10]!=period for r in result['data']):
        raise core.ServiceError('foundation_period_mismatch','结构化数据期间与请求不匹配')
    return result

def collect_table(table,period):
    folder=root()/'来源批次'/period/table
    marker=folder/'进度.json'
    state=json.loads(marker.read_text()) if marker.exists() else {'next_page':1,'rows':0,'complete':False}
    if state['complete']:return {'table':table,'period':period,'cached':True,'rows':state['rows']}
    start=state['next_page']
    for number in range(start,101):
        value=page(table,period,number);rows=value['data'];count=value['count']
        expected_pages=math.ceil(count/500)
        if count and not rows:raise core.ServiceError('foundation_partial','结构化报表提前返回空页')
        if number>1 and count!=state.get('total',count):
            raise core.ServiceError('foundation_changed','来源总数发生变化，需要单独复核该批次')
        core.atomic_text(folder/f'page-{number:03d}.json.gz.hex',gzip.compress(core.dumps(rows).encode()).hex())
        state.update(next_page=number+1,rows=state['rows']+len(rows),total=count,complete=number>=expected_pages,
                     fetched_at=core.stamp(),source=ENDPOINT,report=TABLES[table][0],period=period)
        if state['complete'] and state['rows']!=count:
            raise core.ServiceError('foundation_partial','结构化报表累计行数不等于来源总数')
        core.atomic_json(marker,state)
        if state['complete']:return {'table':table,'period':period,'rows':state['rows']}
        time.sleep(.15)
    raise core.ServiceError('foundation_page_limit','公开报表超过分页保护上限')

def assemble():
    master=json.loads((core.VAULT/'00_基础资料'/'证券名录.json').read_text())
    companies={r['code']+'.'+r['market']:r for r in master['securities'] if r['status']=='listed'}
    annual,quarters=periods();quarter_pool=quarter_ends(core.now().date(),13);wanted=set(annual+quarter_pool)
    data={key:{} for key in companies};conflicts=0
    for period in sorted(wanted):
        for table in TABLES:
            folder=root()/'来源批次'/period/table;marker=folder/'进度.json'
            if not marker.exists() or not json.loads(marker.read_text())['complete']:
                raise core.ServiceError('foundation_partial','底库来源批次尚未完整，不能发布完成状态')
            for path in sorted(folder.glob('page-*.json.gz.hex')):
                rows=json.loads(gzip.decompress(bytes.fromhex(path.read_text())))
                for row in rows:
                    key=row.get('SECUCODE');security=companies.get(key)
                    if not security or row.get('SECURITY_CODE')!=security['code']:continue
                    notice=str(row.get('NOTICE_DATE') or '')[:10]
                    if not notice or notice>core.now().date().isoformat():continue
                    item=data[key].setdefault(period,{'period_end':period,'sources':{},'conflicts':[]})
                    prior=item['sources'].get(table)
                    if prior and prior['disclosed_at']>notice:continue
                    values={name:core.finite(row.get(field)) for name,field in TABLES[table][2].items()}
                    item['sources'][table]={'report':TABLES[table][0],'disclosed_at':notice,'observed_at':core.stamp(),
                        'source':f'https://data.eastmoney.com/bbsj/{security["code"]}.html','values':values,
                        'industry':row.get('INDUSTRY_NAME') or row.get('BOARD_NAME'),'record_date':str(row.get('UPDATE_DATE') or '')[:10]}
    counts={'companies':len(companies),'with_data':0,'annual6':0,'quarters12':0,'conflicts':0,'rows':0}
    for key,security in companies.items():
        history=[]
        for period,item in sorted(data[key].items()):
            for table,source in item['sources'].items():
                for name,value in source['values'].items():
                    if value is None:continue
                    if name in item and abs(item[name]-value)>max(1,abs(value)*.0001):
                        item['conflicts'].append(name)
                    elif name not in item:item[name]=value
            item['disclosed_at']=max((s['disclosed_at'] for s in item['sources'].values()),default=None)
            item['verified']=False
            if item.get('total_assets') is not None and item.get('total_liabilities') is not None and item.get('total_equity') is not None:
                residual=item['total_assets']-item['total_liabilities']-item['total_equity']
                if abs(residual)>max(10,abs(item['total_assets'])*.00001):item['conflicts'].append('balance_identity')
            item['conflicts']=sorted(set(item['conflicts']));counts['conflicts']+=bool(item['conflicts'])
            history.append(item)
        a=[h for h in history if h['period_end'] in annual]
        q=[h for h in history if h['period_end'] in quarter_pool][-12:]
        required_quarters=quarter_ends(date.fromisoformat(q[-1]['period_end'])) if q else quarters
        counts['with_data']+=bool(history);counts['annual6']+=len(a)==6;counts['quarters12']+=len(q)==12;counts['rows']+=len(history)
        from .strategy import single_quarters
        payload={'schema':1,'code':security['code'],'market':security['market'],'company':security['company'],
            'asof':core.stamp(),'basis':'第三方公开累计财务；非PDF核实值；最新修订版本，不是历史时点可见数据',
            'required_annuals':annual,'required_quarters':required_quarters,'missing_annuals':sorted(set(annual)-{h['period_end'] for h in a}),
            'missing_quarters':sorted(set(required_quarters)-{h['period_end'] for h in q}),
            'quarter_basis':'截至来源已收录最近一期向前12期；预采一季度缓冲，未收录的最新季度不冒充已披露',
            'latest_quarter_available':q[-1]['period_end'] if q else None,'history':history,
            'single_quarters':{m:single_quarters(history,m) for m in ('revenue','net_profit','adjusted_profit','operating_cashflow')}}
        folder=core.VAULT/'30_公司'/security['market']/security['code']
        core.atomic_json(folder/'历史财务-结构化.json',payload)
        note='<!-- FINANCIAL_FOUNDATION_V1 -->\n# 历史财务底库\n\n'+payload['basis']+'。\n\n'+f'公司：{security["company"]}；已收录{len(a)}/6个年度、{len(q)}/12期季度。\n\n详细数字和来源见 [[历史财务-结构化.json]]。\n\n缺项不按零或行业均值补齐；存在差异时回查原文。\n'
        path=folder/'历史财务-自动生成.md'
        if not path.exists() or path.read_text().startswith('<!-- FINANCIAL_FOUNDATION_V1 -->'):core.atomic_text(path,note)
    state={'schema':1,'state':'complete','asof':core.stamp(),'annual_periods':annual,'quarter_periods':quarters,
           'quarter_collection_periods':quarter_pool,'quarter_basis':'每家公司按来源已收录的最近12期统计，非12期所有字段齐备',**counts,'model_calls':0}
    core.atomic_json(root()/'状态.json',state)
    core.atomic_text(root()/'底库状态-自动生成.md','# 全市场结构化财务底库\n\n'+core.dumps(state)+'\n\n完整性为来源覆盖检查，不等于审计或原文核实。\n')
    return state

def run(workers=3):
    annual,_=periods();quarters=quarter_ends(core.now().date(),13)
    jobs=[(table,p) for p in sorted(set(annual+quarters)) for table in TABLES]
    done=errors=0
    with ThreadPoolExecutor(max_workers=min(3,max(1,workers))) as pool:
        active={pool.submit(collect_table,*job):job for job in jobs[:3]};remaining=iter(jobs[3:])
        while active:
            future=next(as_completed(active));job=active.pop(future)
            try:future.result();done+=1;errors=0
            except core.ServiceError as error:
                errors+=1
                core.atomic_json(root()/'状态.json',{'state':'partial','completed_batches':done,'total_batches':len(jobs),'error':error.code,'batch':job,'asof':core.stamp(),'model_calls':0})
            print(core.dumps({'completed_batches':done,'total_batches':len(jobs),'consecutive_errors':errors}),flush=True)
            if errors>=3:
                for f in active:f.cancel()
                raise core.ServiceError('foundation_source_circuit','结构化来源连续三次失败，已保存进度')
            following=next(remaining,None)
            if following:active[pool.submit(collect_table,*following)]=following
    return assemble()

def financial_for(code,market):
    path=core.VAULT/'30_公司'/market/code/'历史财务-结构化.json'
    if not path.exists():return None
    value=json.loads(path.read_text())
    if value.get('code')!=code or value.get('market')!=market:return None
    # Full lineage stays in the Obsidian source file. Repeating it inside every
    # model prompt is unnecessary; numerical conflicts cannot become scores.
    history=[]
    for row in value['history']:
        item={k:v for k,v in row.items() if k!='sources'}
        for field in row.get('conflicts',[]):
            if field=='balance_identity':
                for metric in ('total_assets','total_liabilities','total_equity'):item[metric]=None
            elif field in item:item[field]=None
        history.append(item)
    return {'history':history,'source':f'https://data.eastmoney.com/bbsj/{code}.html','fetched_at':value['asof'],'basis':value['basis']}
