"""Forward-only outcome records for frozen research versions. No paid calls."""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import mean

from .core import finite
from .research_profile import completed_bars, moment

HORIZONS = (3,7,15,30,60,90)


def _pct(last,first):
    return round((last/first-1)*100,4) if finite(last) is not None and finite(first) is not None and first>0 else None


def trading_dates(start,asof):
    from .job_schedule import trading_day
    at=moment(asof);day=moment(start)
    if not at or not day:return [],'invalid_time'
    current=day.date()+timedelta(days=1);dates=[]
    while current<=at.date():
        state=trading_day(current)
        if state is None:return dates,'calendar_unverified'
        if state and (current<at.date() or at.hour>15 or at.hour==15 and at.minute>=35):dates.append(current.isoformat())
        current+=timedelta(days=1)
    return dates,None


def observations(frozen,prices,benchmark,*,asof,sector=None):
    """Exact exchange sessions, not 'N available bars'.

    QFQ series are used within one series for next-open return comparisons.
    Frozen raw interpretation prices are deliberately not divided by QFQ bars.
    """
    dates,error=trading_dates(frozen.get('information_asof'),asof)
    stock={b['date']:b for b in completed_bars(prices,asof)}
    market={b['date']:b for b in completed_bars(benchmark,asof)}
    industry={b['date']:b for b in completed_bars(sector or {},asof)}
    result={}
    for horizon in HORIZONS:
        if error:
            result[str(horizon)]={'state':'data_gap','reason':error};continue
        if len(dates)<horizon:
            result[str(horizon)]={'state':'pending','complete_sessions':len(dates)};continue
        window=dates[:horizon]
        if any(d not in stock for d in window):
            result[str(horizon)]={'state':'data_gap','reason':'观察期存在个股缺日或停牌，不能用后续日子替代'};continue
        if prices.get('adjustment')!='qfq' or prices.get('cross_validation',{}).get('status')=='conflict':
            result[str(horizon)]={'state':'data_gap','reason':'复权口径或行情核对不可用'};continue
        entry,end=stock[window[0]],stock[window[-1]]
        if finite(entry.get('open')) is None or entry['open']<=0:
            result[str(horizon)]={'state':'data_gap','reason':'下一交易日开盘未取得'};continue
        gross=_pct(end['close'],entry['open'])
        if gross is None:
            result[str(horizon)]={'state':'data_gap','reason':'收益率无法计算'};continue
        benchmark_return=_pct(market[window[-1]]['close'],market[window[0]].get('open')) if all(d in market for d in window) else None
        sector_return=_pct(industry[window[-1]]['close'],industry[window[0]].get('open')) if all(d in industry for d in window) else None
        points=[(d,_pct(stock[d]['close'],entry['open'])) for d in window]
        best=max(points,key=lambda p:p[1]);worst=min(points,key=lambda p:p[1])
        eligible=points[1:]  # T+1: same-day exit is not an executable strategy.
        peak=entry['open'];drawdown=0
        for d in window:
            peak=max(peak,stock[d]['close']);drawdown=min(drawdown,_pct(stock[d]['close'],peak))
        # Actual tradability requires exchange halt/limit flags and orders.
        # Even normal-looking OHLC data must not be labelled verified fills.
        result[str(horizon)]={'state':'observed','entry_date':window[0],'end_date':window[-1],
            'entry_open':entry['open'],'end_close':end['close'],'gross_return_pct':gross,
            'benchmark_return_pct':benchmark_return,'market_excess_pp':round(gross-benchmark_return,4) if benchmark_return is not None else None,
            'sector_return_pct':sector_return,'sector_excess_pp':round(gross-sector_return,4) if sector_return is not None else None,
            'max_close_gain_pct':best[1],'max_close_gain_date':best[0],'max_close_loss_pct':worst[1],'max_close_loss_date':worst[0],
            'max_drawdown_close_pct':drawdown,'t_plus_one_max_close_gain_pct':max((p[1] for p in eligible),default=None),
            'cost_scenarios':[{'round_trip_bps':bps,'net_return_pct':round(((1+gross/100)*(1-bps/10000)-1)*100,4)} for bps in (0,20,40)],
            'execution_status':'unverified','execution_gaps':['停复牌及涨跌停可成交性','实际成交与滑点','分红除权处理可交易证明'],
            'prospective_feature_eligible':frozen.get('capture_kind')=='at_generation',
            'warning':'收盘路径是事后观察，不是可提前知道的卖点或真实成交收益。'}
    return {'schema':1,'asof':asof,'primary_horizon':15,'horizons':result}


def _ranks(values):
    ordered=sorted(set(values))
    return [(sum(v<x for v in values)+(sum(v==x for v in values)+1)/2) for x in values]


def rank_correlation(pairs):
    if len(pairs)<3:return None
    x,y=map(_ranks,zip(*pairs));mx,my=mean(x),mean(y)
    den=(sum((a-mx)**2 for a in x)*sum((b-my)**2 for b in y))**.5
    return round(sum((a-mx)*(b-my) for a,b in zip(x,y))/den,5) if den else None


def statistics(rows,horizon=15):
    """Descriptive outputs only; no automatic production-weight adjustment."""
    cases=[];seen=set()
    for row in sorted(rows,key=lambda r:r['frozen'].get('information_asof','')):
        frozen=row['frozen'];key=(frozen.get('code'),str(frozen.get('information_asof',''))[:10])
        outcome=row['outcomes']['horizons'].get(str(horizon),{})
        if frozen.get('capture_kind')!='at_generation' or outcome.get('state')!='observed' or key in seen:continue
        score=frozen['profile']['composites']['short']
        if not score.get('ranking_eligible') or finite(score.get('score')) is None:continue
        seen.add(key);cases.append((frozen,outcome,score['score']))
    pairs=[(score,out['market_excess_pp']) for _,out,score in cases if out.get('market_excess_pp') is not None]
    groups=[]
    ranked=sorted(cases,key=lambda r:r[2]);n=len(ranked)
    if n>=10:
        for i in range(5):
            group=ranked[n*i//5:n*(i+1)//5]
            returns=[o['market_excess_pp'] for _,o,_ in group if o.get('market_excess_pp') is not None]
            groups.append({'quintile':i+1,'n':len(group),'mean_market_excess_pp':mean(returns) if returns else None})
    return {'horizon':horizon,'n':n,'unique_companies':len({f['code'] for f,_,_ in cases}),
            'spearman_market_excess':rank_correlation(pairs),'quintiles':groups,'production_weight_change':False,
            'inference_status':'descriptive_only','execution_verified_n':sum(o.get('execution_status')=='verified' for _,o,_ in cases),
            'missing_for_validation':['跨披露季和行情成熟样本','行业/同日聚类区间估计','训练测试标签隔离与滚动向前检验','与简单成长/动量基准及逐维消融对照'],
            'note':'后补评分、缺项分不进入前瞻排名；描述统计不代表收益已被验证。'}
