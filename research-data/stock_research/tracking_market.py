"""Hourly public quotes and chart series inside the existing local service.

No model calls, extra daemon, database migration or changes to frozen ratings.
"""
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime
import fcntl
import json
from . import core

INTERVAL=3600

def home(vault=None):return (vault or core.VAULT)/'00_基础资料'/'追踪行情更新'
def record_path(market,code,vault=None):return (vault or core.VAULT)/'30_公司'/market/code/'行情'/'解读追踪最新行情.json'
def read(path):
    try:
        value=json.loads(path.read_text()) if path.is_file() else {}
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}

def fresh(value,at):
    try:return 0<=(at-datetime.fromisoformat(value)).total_seconds()<INTERVAL
    except (TypeError,ValueError):return False

def scope(store):
    rows=store.rows("SELECT d.code,d.market,r.payload FROM reports r JOIN documents d ON d.id=r.document_id")
    result={}
    for row in rows:
        report=json.loads(row['payload'])
        if report.get('report_kind')=='historical_foundation' or report.get('formal_tracking_eligible') is False:continue
        start=str(report.get('generated_at') or '')[:10]
        if not start:continue
        code=row['code'];item=result.setdefault(code,{'code':code,'market':row['market'],'start':start})
        item['start']=min(item['start'],start)
    return result

def chart_series(prices,start='',end=None,checked_at=None):
    prices=prices or {};end=end or core.now().date().isoformat()
    return {'points':[{'date':b['date'],'close':b['close']} for b in prices.get('bars',[])
                      if b.get('date','')>=start and b.get('date','')<=end and core.finite(b.get('close')) is not None and b['close']>0],
            'adjustment':prices.get('adjustment'),'source':prices.get('source'),
            'cross_validation':prices.get('cross_validation',{}),'checked_at':checked_at or prices.get('fetched_at')}

def public_record(value,start=''):
    prices=value.get('raw_prices') or {};quote=value.get('quote') or {}
    return {'quote':quote,'quote_checked_at':value.get('quote_checked_at'),
            'last_attempt_at':value.get('last_attempt_at'),'errors':value.get('errors',{}),
            'trend':chart_series(prices,start,checked_at=value.get('series_checked_at'))}

def export(store,vault=None):
    rows=scope(store);stocks={}
    for code,item in rows.items():
        value=read(record_path(item['market'],code,vault))
        if value:stocks[code]=public_record(value,item['start'])
    state=read(home(vault)/'控制.json')
    result={'generated_at':core.stamp(),'interval_seconds':1800 if core.config().get('schedule_policy')=='adaptive-v1' else INTERVAL,'status':{k:state.get(k) for k in
            ('state','last_started_at','last_completed_at','stocks','completed','quote_failures','series_failures')},'stocks':stocks}
    core.atomic_json(core.SITE/'tracking-market.json',result)
    return result

def overlay(snapshots,vault=None):
    result=dict(snapshots)
    for code,item in snapshots.items():
        market=item.get('market')
        # Older snapshots need not contain a market field. Resolve only one
        # known company directory; never scan unrelated private files.
        paths=[record_path(market,code,vault)] if market in ('SH','SZ','BJ') else [record_path(m,code,vault) for m in ('SH','SZ','BJ')]
        value=next((read(path) for path in paths if path.is_file()),{})
        if value:
            result[code]={**item,'tracking_market_errors':value.get('errors',{})}
            if value.get('raw_prices'):result[code]['chart_prices']=value['raw_prices']
            if value.get('quote') and str(value['quote'].get('asof') or '')>=str((item.get('quote') or {}).get('asof') or ''):
                result[code].update(quote=value['quote'],quote_checked_at=value.get('quote_checked_at'))
            last=lambda prices:max((b.get('date','') for b in (prices or {}).get('bars',[])),default='')
            if value.get('prices') and last(value['prices'])>=last(item.get('prices')):result[code]['prices']=value['prices']
        from .research_inputs import _read as read_input
        research_path=next((p.with_name('六维技术日线.json') for p in paths if p.with_name('六维技术日线.json').is_file()),None)
        research_record=read_input(research_path) if research_path else {}
        research_prices=research_record.get('prices')
        if research_record.get('last_error')=='research_prices_conflict':
            result.setdefault(code,dict(item))
            result[code]['research_prices_conflict']=True
        if research_prices:
            result.setdefault(code,dict(item))
            last=lambda prices:max((b.get('date','') for b in (prices or {}).get('bars',[])),default='')
            if last(research_prices)>=last(result[code].get('prices')):result[code]['prices']=research_prices
        sector_path=next((p.with_name('六维行业量价.json') for p in paths if p.with_name('六维行业量价.json').is_file()),None)
        if sector_path:
            sector=read_input(sector_path)
            if sector:
                result.setdefault(code,dict(item));result[code]['sector_context']=sector
                if sector.get('prices'):result[code]['sector_prices']=sector['prices']
    return result

def _fetch(item,previous,at,skip):
    from .sources import latest_quote,daily_bars
    result=dict(previous);result.update(last_attempt_at=at.isoformat(),errors={})
    for part,field,clock,action in (
        ('quote','quote','quote_checked_at',lambda:latest_quote(item['code'],item['market'])),
        ('series','raw_prices','series_checked_at',lambda:daily_bars(item['code'],item['market'],count=640 if item['start']<str(at.year)+'-01-01' else 160,minimum=1,adjustment='none'))):
        # The global hourly gate already deduplicates passes. A second TTL
        # here would skip quotes returned seconds after the previous pass began
        # and accidentally stretch their actual refresh interval to two hours.
        if part in skip:result['errors'][part]='daily_failure_limit';continue
        try:
            value=action()
            if part=='series':
                if value.get('adjustment')!='none':raise core.ServiceError('chart_adjustment_mismatch','走势图须使用未复权价格')
                # Preserve the accumulated actual closes as the source's
                # bounded response window moves forward. Never relabel qfq.
                saved=result.get(field) or {}
                bars={b['date']:b for b in saved.get('bars',[]) if saved.get('adjustment')=='none'}
                bars.update({b['date']:b for b in value.get('bars',[])
                             if b['date']<at.date().isoformat() or (b['date']==at.date().isoformat() and at.hour>=15)})
                value={**value,'bars':sorted(bars.values(),key=lambda b:b['date'])}
            if part=='quote' and result.get('quote',{}).get('asof','')>value.get('asof',''):
                result['errors'][part]='older_quote_rejected';continue
            result[field]=value;result[clock]=core.stamp()
        except core.ServiceError as error:result['errors'][part]=error.code
        except Exception:result['errors'][part]='source_unexpected'
    return result

def refresh(store=None,at=None,vault=None,force=False):
    """One bounded source pass. A separate lock permits ongoing model reads."""
    owned=store is None;store=store or core.Store();at=at or core.now();base=home(vault);base.mkdir(parents=True,exist_ok=True)
    try:
        with (base/'采集.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return {'state':'busy'}
            state=read(base/'控制.json')
            from . import job_schedule
            adaptive=job_schedule.enabled(core.config())
            slot=job_schedule.market_slot(at) if adaptive else None
            if adaptive and not job_schedule.market_due(state,at):return state
            if not adaptive and not force and fresh(state.get('last_started_at'),at):return state
            from .securities import price_enabled
            stocks={code:item for code,item in scope(store).items() if price_enabled(code)}
            state={'state':'running','last_started_at':at.isoformat(),'stocks':len(stocks),'completed':0,'quote_failures':0,'series_failures':0}
            if slot:state['market_slot']=slot['slot']
            core.atomic_json(base/'控制.json',state)
            snapshots={r['code']:json.loads(r['payload']) for r in store.rows('SELECT code,payload FROM snapshots')}
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures={}
                for code,item in stocks.items():
                    previous=read(record_path(item['market'],code,vault))
                    if not previous:
                        snap=snapshots.get(code,{})
                        previous={k:snap[k] for k in ('quote','prices') if snap.get(k)}
                    skips=set()
                    for part in ('quote','series'):
                        failures=store.setting('tracking_hourly_failures_'+part+code,{})
                        if failures.get('day')==at.date().isoformat() and failures.get('count',0)>=3:skips.add(part)
                    futures[pool.submit(_fetch,item,previous,at,skips)]=item
                for future in as_completed(futures):
                    item=futures[future];code=item['code'];value=future.result()
                    core.atomic_json(record_path(item['market'],code,vault),value)
                    for part in ('quote','series'):
                        key='tracking_hourly_failures_'+part+code;prior=store.setting(key,{})
                        count=prior.get('count',0) if prior.get('day')==at.date().isoformat() else 0
                        failed=part in value.get('errors',{})
                        store.set(key,{'day':at.date().isoformat(),'count':min(3,count+1) if failed else 0})
                        if failed:state[part+'_failures']+=1
                    if value.get('quote'):store.cache('tracking-live-v3-'+code,value['quote'],hours=1)
                    state['completed']+=1;core.atomic_json(base/'控制.json',state)
                    if state['completed']%10==0:export(store,vault)
            from .research_shadow import refresh_after_market
            try:state['research_shadow']=refresh_after_market(store,vault=vault)
            except Exception:state['research_shadow']={'state':'failed','error':'research_shadow_refresh_failed'}
            state.update(state='complete',last_completed_at=core.stamp());core.atomic_json(base/'控制.json',state)
            export(store,vault)
            return state
    finally:
        if owned:store.conn.close()

def background_refresh():
    try:return refresh()
    except Exception:
        # Public status never contains credentials, SQL, paths or traces.
        core.atomic_json(home()/'最近错误.json',{'at':core.stamp(),'code':'tracking_refresh_failed'})
        return {'state':'failed'}
