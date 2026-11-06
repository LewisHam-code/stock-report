"""Shared public industry price/breadth inputs, never industry outlook facts.

Exact Eastmoney industry names and verified company membership are required.
No fuzzy industry/theme matching, model calls or fixed 'hot sector' list.
"""
from __future__ import annotations

from datetime import datetime
import math
import re
from statistics import mean
from urllib.parse import urlencode

from . import core
from .research_profile import completed_bars,moment


def _rows(value):
    data=value.get('data') if isinstance(value,dict) else None
    diff=(data or {}).get('diff')
    if isinstance(diff,dict):diff=list(diff.values())
    if not isinstance(data,dict) or not isinstance(diff,list) or not isinstance(data.get('total'),int):
        raise core.ServiceError('sector_format','行业公开数据格式异常')
    return diff,data['total']


def _list(fs,fields,page=1):
    from .sources import get_json
    return _rows(get_json('https://push2.eastmoney.com/api/qt/clist/get',
                        {'pn':page,'pz':100,'fs':fs,'fltt':2,'fields':fields},timeout=10))


def catalog(store):
    key='research-sector-catalog-v1';saved=store.cached(key)
    if saved is not None:return saved
    rows,total=_list('m:90+t:2','f12,f14')
    if total>1000:raise core.ServiceError('sector_catalog_size','行业分类数量异常，停止自动匹配')
    for page in range(2,math.ceil(total/100)+1):rows.extend(_list('m:90+t:2','f12,f14',page)[0])
    result={}
    for r in rows:
        ident,name=r.get('f12'),r.get('f14')
        if isinstance(name,str) and re.fullmatch(r'BK\d{4}',str(ident)):
            result.setdefault(name,[]).append(ident)
    result={name:ids[0] for name,ids in result.items() if len(set(ids))==1}
    store.cache(key,result,hours=24)
    return result


def industry_id(company_industry,boards):
    for source in (company_industry or {}).get('sources',[]):
        if source.get('provider')!='eastmoney':continue
        for classification in source.get('classifications',[]):
            if classification.get('taxonomy')!='eastmoney_2016':continue
            for name in reversed(classification.get('path',[])):
                if name in boards:return boards[name],name
    return None,None


def _index_bars(ident):
    from .sources import get_json
    from .market_sources import _bar
    data=get_json('https://push2his.eastmoney.com/api/qt/stock/kline/get',
        {'secid':'90.'+ident,'klt':101,'fqt':1,'lmt':90,'end':core.now().strftime('%Y%m%d'),
         'fields1':'f1,f2,f3,f4,f5,f6','fields2':'f51,f52,f53,f54,f55,f56,f57'},timeout=10)
    row=data.get('data') if isinstance(data,dict) else None
    if not row or row.get('code')!=ident or not isinstance(row.get('klines'),list):raise core.ServiceError('sector_series','行业指数身份或日线不匹配')
    bars=[]
    for raw in row['klines']:
        fields=raw.split(',') if isinstance(raw,str) else []
        if len(fields)>=7 and (b:=_bar(*fields[:6])):
            amount=core.finite(fields[6])
            if amount is not None and amount>=0:b.update(amount=amount,amount_unit='yuan',amount_basis='provider_turnover_field')
            bars.append(b)
    return {'bars':bars,'adjustment':'qfq','source':'https://quote.eastmoney.com/bk/90.'+ident+'.html','fetched_at':core.stamp()}


def sector(store,ident,asof):
    if not re.fullmatch(r'BK\d{4}',str(ident)):raise ValueError('invalid_sector_id')
    at=moment(asof);slot=asof[:10]+':'+str((at.hour*60+at.minute)//30)
    key='research-sector-live-v1-'+ident+'-'+slot
    saved=store.cached(key)
    if saved is not None:return saved
    rows,total=_list('b:'+ident,'f12,f14,f2,f3,f6')
    if total>1000:raise core.ServiceError('sector_member_size','板块成份过多，暂不作完整广度评价')
    for page in range(2,math.ceil(total/100)+1):rows.extend(_list('b:'+ident,'f12,f14,f2,f3,f6',page)[0])
    members={r['f12']:r for r in rows if re.fullmatch(r'\d{6}',str(r.get('f12'))) and core.finite(r.get('f3')) is not None and core.finite(r.get('f2')) is not None and r['f2']>0}
    day=asof[:10];mode='close' if at.strftime('%H:%M')>='15:35' else 'intraday'
    series_key='research-sector-bars-v1-'+ident+'-'+day+'-'+mode
    prices=store.cached(series_key)
    if prices is None:
        prices=_index_bars(ident);store.cache(series_key,prices,hours=24)
    bars=completed_bars(prices,asof)
    breadth=sum(r['f3']>0 for r in members.values())/len(members)*100 if members else None
    # Turnover is measured only after close; intraday turnover is not compared
    # against full days, which would manufacture a weak activity signal.
    turnover=None
    if mode=='close' and len(bars)>=21 and bars[-1]['date']==day and all(core.finite(b.get('amount')) is not None for b in bars[-21:]):
        base=mean(b['amount'] for b in bars[-21:-1]);turnover=bars[-1]['amount']/base if base>0 else None
    result={'id':ident,'known_at':core.stamp(),'observed_at':core.stamp(),'asof':core.stamp(),
            'source':prices['source'],'prices':prices,'members':sorted(members),'member_count':len(members),'published_member_count':total,
            'sample_coverage_pct':round(min(100,len(members)/total*100),2) if total else 0,
            'advancing_pct':breadth,'breadth_window':'当日涨跌幅，非20日上涨家数',
            'return_20d_pct':(bars[-1]['close']/bars[-21]['close']-1)*100 if len(bars)>=21 else None,
            'turnover_ratio':turnover,'turnover_window':'当日收盘成交额/此前20个完整交易日均额',
            'strength_price_date':bars[-1]['date'] if bars else None,
            'verification':'provider_identity_and_membership','scope':'行业量价；不是主题真实性、行业长期前景或双源验证'}
    store.cache(key,result,hours=2)
    return result


def enrich(store,snapshots,benchmark):
    result={};errors={};boards=None;shared={};at=core.stamp()
    try:boards=catalog(store)
    except core.ServiceError as e:return snapshots,{'catalog':e.code}
    benchmark_rows=completed_bars(benchmark,at);index={b['date']:b for b in benchmark_rows}
    for code,snap in snapshots.items():
        ident,name=industry_id(snap.get('industry'),boards)
        if not ident:errors[code]='industry_catalog_exact_match_missing';result[code]=snap;continue
        if ident not in shared:
            try:shared[ident]=sector(store,ident,at)
            except core.ServiceError as e:shared[ident]={'error':e.code}
        context=shared[ident]
        if context.get('error'):errors[code]=context['error'];result[code]=snap;continue
        context=dict(context);context['name']=name
        context['membership_verified']=code in context['members']
        sector_bars=completed_bars(context['prices'],core.stamp())
        start=sector_bars[-21]['date'] if len(sector_bars)>=21 else None
        end=sector_bars[-1]['date'] if sector_bars else None
        if start in index and end in index and context.get('return_20d_pct') is not None:
            base=(index[end]['close']/index[start]['close']-1)*100
            context['relative_return_20d_pp']=context['return_20d_pct']-base
        result[code]={**snap,'sector_context':context,'sector_prices':context['prices']}
    return result,errors
