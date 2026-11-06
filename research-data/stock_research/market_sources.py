"""Eastmoney and THS quotes; Tencent verifies price conflicts on demand."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import re
from threading import Lock
from time import monotonic
from .core import ServiceError, TZ, finite, now, stamp
from .disclosure_sources import json_payload

VERSION = 'market-em-ths-v1'
_FAILURES = {}
_FAILURE_LOCK = Lock()


def provider_call(name, function, *args):
    """Bound a provider outage across symbols during one worker process."""
    with _FAILURE_LOCK:
        failure = _FAILURES.get(name)
    if failure and monotonic() < failure[0]:
        raise ServiceError(failure[1], '来源暂时不可用，冷却后再试')
    try:
        return function(*args)
    except ServiceError as error:
        if error.code in ('source_network', 'source_http_403', 'source_http_429', 'source_http_502', 'source_http_503', 'source_http_504'):
            with _FAILURE_LOCK:
                _FAILURES[name] = (monotonic() + 300, error.code)
        raise


def _ths_identity(code, market):
    _identity(code, market)
    # The provider's own index directory identifies SSE CSI300 as 1B0300.
    return ('zs_1B0300', '1B0300') if market == 'SH' and code == '000300' else ('hs_' + code, code)


def _ths_source(code, market):
    return ('https://q.10jqka.com.cn/zs/detail/code/1B0300/' if market == 'SH' and code == '000300'
            else 'https://stockpage.10jqka.com.cn/' + code + '/')


def _identity(code, market):
    if len(code) != 6 or not code.isascii() or not code.isdigit() or market not in ('SH', 'SZ', 'BJ'):
        raise ServiceError('invalid_market_identity', '股票代码或市场不合法')
    return ('1.' if market == 'SH' else '0.') + code


def _timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).strip()).replace(tzinfo=TZ)
    except ValueError:
        raise ServiceError('quote_timestamp_missing', '行情缺少有效交易时间') from None
    if result > now():
        raise ServiceError('future_quote', '行情交易时间晚于系统时间')
    return result.isoformat(timespec='seconds')


def eastmoney_quote(code, market):
    from .sources import get_json
    data = get_json('https://push2.eastmoney.com/api/qt/stock/get',
                    {'secid': _identity(code, market), 'fltt': 2,
                     'fields': 'f43,f57,f58,f59,f60,f86,f116,f162,f163,f164,f167,f168,f170'},
                    headers={'Referer': 'https://quote.eastmoney.com/'})
    row = data.get('data') if isinstance(data, dict) else None
    if not isinstance(row, dict) or row.get('f57') != code or not finite(row.get('f43')) or finite(row['f43']) <= 0:
        raise ServiceError('quote_unavailable', '东方财富未返回匹配的有效股价')
    try:
        asof = datetime.fromtimestamp(float(row['f86']), TZ)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ServiceError('quote_timestamp_missing', '东方财富缺少交易时间') from None
    # fltt=2 supplies already-scaled prices/ratios. f162/f163/f164 are
    # dynamic/static/TTM PE respectively, as mapped by the provider's UI.
    return dict(code=code, company=row.get('f58'), price=finite(row['f43']), previous_close=finite(row.get('f60')),
                change_pct=finite(row.get('f170')), pe_ttm=finite(row.get('f164')),
                pe_dynamic_source=finite(row.get('f162')), pe_static_source=finite(row.get('f163')),
                pb=finite(row.get('f167')), market_cap=finite(row.get('f116')), turnover_pct=finite(row.get('f168')),
                asof=_timestamp(asof.isoformat()), provider='eastmoney', source='https://quote.eastmoney.com/' + ('sh' if market == 'SH' else 'bj' if market == 'BJ' else 'sz') + code + '.html',
                quote_schema=VERSION, timestamp_precision='second', fetched_at=stamp())


def ths_quote(code, market):
    from .sources import request
    symbol, provider_code = _ths_identity(code, market)
    payload = json_payload(request('https://d.10jqka.com.cn/v6/realhead/' + symbol + '/last.js',
                                   headers={'Referer': 'https://stockpage.10jqka.com.cn/' + code + '/'}))
    row = payload.get('items') if isinstance(payload, dict) else None
    if not isinstance(row, dict) or str(row.get('5')) != provider_code or not finite(row.get('10')) or finite(row['10']) <= 0:
        raise ServiceError('quote_unavailable', '同花顺未返回匹配的有效股价')
    # `time` is the web response clock, including weekends. Only updateTime
    # represents the quote's trading timestamp.
    return dict(code=code, company=row.get('name'), price=finite(row['10']), previous_close=finite(row.get('6')),
                change_pct=finite(row.get('199112')), pe_ttm=finite(row.get('3153')),
                pe_dynamic_source=finite(row.get('2034120')), pe_static_source=finite(row.get('134152')),
                pb=finite(row.get('592920')), market_cap=finite(row.get('3541450')), turnover_pct=finite(row.get('1968584')),
                asof=_timestamp(row.get('updateTime')), provider='ths', source=_ths_source(code, market),
                quote_schema=VERSION, timestamp_precision='second' if re.search(r'\d{2}:\d{2}:\d{2}',str(row.get('updateTime'))) else 'minute', fetched_at=stamp())


def price_time_comparison(primary, secondary):
    """Web snapshots can be close in time, never guaranteed simultaneous trades."""
    a,b=datetime.fromisoformat(primary['asof']),datetime.fromisoformat(secondary['asof'])
    delta=abs((a-b).total_seconds())
    result={'seconds_apart':delta,'comparable':False,'mode':'not_comparable'}
    if a.date()!=b.date():return {**result,'reason':'交易日期不同'}
    at=now()
    # Only use the closing-batch exception once that session has settled.
    settled=a.date()<at.date() or (a.date()==at.date() and (at.hour,at.minute)>=(15,35))
    if settled and a.hour>=15 and b.hour>=15 and delta<=3600:
        return {**result,'comparable':True,'mode':'closing_batch'}
    # Older saved THS quotes did not record precision. A :00 timestamp from
    # that source cannot safely be assumed to carry second-level precision.
    def precision(quote,clock):
        return quote.get('timestamp_precision') or ('minute' if quote.get('provider')=='ths' and clock.second==0 else 'second')
    if precision(primary,a)!='second' or precision(secondary,b)!='second':
        return {**result,'reason':'行情时间仅精确到分钟，不能作秒级价格裁决'}
    if delta>2:return {**result,'reason':'盘中行情时点相差超过2秒'}
    return {**result,'comparable':True,'mode':'same_second' if delta==0 else 'near_time'}


def compare_quotes(primary, secondary):
    result = {'provider': secondary['provider'], 'source': secondary['source'], 'asof': secondary['asof'], 'fields': {}}
    a, b = datetime.fromisoformat(primary['asof']), datetime.fromisoformat(secondary['asof'])
    # Providers stamp the closing batch at different times (15:00 / 15:30).
    close_batch = a.hour >= 15 and b.hour >= 15 and abs((a-b).total_seconds()) <= 60*60
    if a.date() != b.date() or (abs((a-b).total_seconds()) > 15*60 and not close_batch):
        return {**result, 'status': 'not_comparable', 'reason': '交易日期或时间相差过大'}
    timing=price_time_comparison(primary,secondary)
    result['price_timing']=timing
    for field, tolerance, absolute in (('price', 0, .02), ('previous_close',.005,.02),
            ('pe_ttm', .03, .03),('pe_dynamic_source',.03,.03),('pe_static_source',.03,.03),
            ('pb', .03, .01), ('market_cap', .01, 1),('turnover_pct',.03,.03),('change_pct',.03,.05)):
        x, y = finite(primary.get(field)), finite(secondary.get(field))
        if x is not None and y is not None:
            match=abs(x-y)<=max(abs(x)*tolerance,absolute)+1e-9
            result['fields'][field]={'primary':x,'secondary':y,'matches':None if field=='price' and not timing['comparable'] else match}
    result['status'] = 'not_comparable' if not timing['comparable'] else 'matched' if result['fields'] and all(v['matches'] for v in result['fields'].values()) else 'conflict'
    if not timing['comparable']:result['reason']=timing.get('reason')
    return result


def tencent_quote(code, market):
    """A bounded third quote, requested only for a two-provider price conflict."""
    from .sources import request
    _identity(code, market)
    symbol=market.lower()+code
    raw=request('https://qt.gtimg.cn/q='+symbol,headers={'Referer':'https://gu.qq.com/'},timeout=6,limit=32000)
    try:
        match=re.fullmatch(r'\s*v_'+symbol+r'="([^"\r\n]*)";\s*',raw.decode('gb18030'))
        fields=match.group(1).split('~') if match else []
        if len(fields)<33 or fields[2]!=code or not re.fullmatch(r'\d{14}',fields[30]):raise ValueError()
        price,previous=finite(fields[3]),finite(fields[4])
        if price is None or price<=0 or previous is None or previous<=0:raise ValueError()
        at=datetime.strptime(fields[30],'%Y%m%d%H%M%S').replace(tzinfo=TZ)
    except (UnicodeError,ValueError,IndexError):
        raise ServiceError('quote_unavailable','腾讯未返回匹配的有效行情或交易时间') from None
    return dict(code=code,company=fields[1],price=price,previous_close=previous,change_pct=finite(fields[32]),
                asof=_timestamp(at.isoformat()),provider='tencent',source='https://gu.qq.com/'+symbol+'/gp',
                quote_schema=VERSION,timestamp_precision='second',fetched_at=stamp())


def resolve_price_conflict(selected, other, third):
    """Keep full original quotes; only one close-in-time match can resolve a price."""
    comparisons={}
    for quote in (selected,other):
        timing=price_time_comparison(quote,third)
        comparable=timing['comparable']
        # Two price ticks allow a small snapshot skew without the broad ratio
        # tolerance used for PE and market-cap fields in the ordinary check.
        matches=comparable and abs(quote['price']-third['price'])<=.020000001
        comparisons[quote['provider']]={'price':quote['price'],'asof':quote['asof'],
                                        'comparable':comparable,'matches':matches,'timing':timing}
    winners=[quote for quote in (selected,other) if comparisons[quote['provider']]['matches']]
    resolved=len(winners)==1
    return (winners[0] if resolved else selected),dict(status='resolved' if resolved else 'unresolved',
            provider='tencent',price=third['price'],asof=third['asof'],source=third['source'],
            comparisons=comparisons,selected_provider=(winners[0] if resolved else selected)['provider'])


def _quote_round(code,market,third=False):
    providers=[('eastmoney',eastmoney_quote),('ths',ths_quote)]
    if third:providers.append(('tencent',tencent_quote))
    quotes,errors=[],[]
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        jobs=[(name,pool.submit(provider_call,name+'_quote',action,code,market)) for name,action in providers]
        for name,future in jobs:
            try:quotes.append(future.result())
            except ServiceError as error:errors.append({'provider':name,'code':error.code})
    return quotes,errors


def latest_quote(code, market):
    from .securities import price_enabled
    if not price_enabled(code):raise ServiceError('security_delisted','该股票已确认退市，停止行情采集')
    quotes,errors=_quote_round(code,market)
    if not quotes:
        raise ServiceError('quote_unavailable', '东方财富和同花顺行情均暂不可用')
    # Show the freshest valid source quote, with Eastmoney winning exact ties.
    selected=max(quotes,key=lambda quote:quote['asof'])
    selected = dict(selected)
    other = next((q for q in quotes if q['provider'] != selected['provider']), None)
    check=compare_quotes(selected,other) if other else {'status':'unavailable','errors':list(errors)}
    # A different PE/PB or a missing provider does not trigger the third source.
    a=datetime.fromisoformat(selected['asof'])
    b=datetime.fromisoformat(other['asof']) if other else a
    suspected=other and abs(selected['price']-other['price'])>.020000001 and (
        price_time_comparison(selected,other)['comparable'] or
        (a.date()==b.date() and abs((a-b).total_seconds())<=60))
    if suspected:
        # One bounded retry only: refresh all three concurrently instead of
        # comparing a newly arrived third quote against the previous round.
        original=check
        refreshed,retry_errors=_quote_round(code,market,third=True)
        errors.extend(retry_errors)
        original_quotes={quote['provider']:quote for quote in quotes}
        by_provider={**original_quotes,**{quote['provider']:quote for quote in refreshed}}
        quotes=list(by_provider.values())
        primary=[quote for quote in quotes if quote['provider']!='tencent']
        selected=dict(max(primary,key=lambda quote:quote['asof']))
        other=next(quote for quote in primary if quote['provider']!=selected['provider'])
        check=compare_quotes(selected,other)
        third=by_provider.get('tencent')
        if third:
            winner,resolution=resolve_price_conflict(selected,other,third)
            selected=dict(winner)
            remaining=next(quote for quote in primary if quote['provider']!=winner['provider'])
            check=compare_quotes(selected,remaining)
            check['price_resolution']={**resolution,'original_comparison':original,'resampled_concurrently':True}
        else:
            check['price_resolution']={'status':'unavailable','provider':'tencent','resampled_concurrently':True,
                                      'error':next((error['code'] for error in retry_errors if error['provider']=='tencent'),'quote_unavailable')}
    selected['cross_validation'] = check
    selected['source_errors'] = errors
    selected['source_quotes']={quote['provider']:quote for quote in quotes}
    return selected


def _bar(day, opening, close, high, low, volume):
    try:
        day = datetime.strptime(str(day).replace('-', ''), '%Y%m%d').date().isoformat()
    except ValueError:
        return None
    values = [finite(v) for v in (opening, close, high, low)]
    if day > now().date().isoformat() or any(v is None or v <= 0 for v in values):
        return None
    if max(values[:2]) > values[2]+.011 or min(values[:2]) < values[3]-.011:
        return None
    return dict(date=day, open=values[0], close=values[1], high=values[2], low=values[3], volume=finite(volume))


def eastmoney_bars(code, market, count, adjustment='qfq'):
    from .sources import get_json
    data = get_json('https://push2his.eastmoney.com/api/qt/stock/kline/get',
                    {'secid': _identity(code, market), 'klt': 101, 'fqt': 0 if adjustment=='none' else 1, 'lmt': count,
                     'end': now().strftime('%Y%m%d'), 'fields1': 'f1,f2,f3,f4,f5,f6',
                     'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'},
                    headers={'Referer': 'https://quote.eastmoney.com/'})
    row = data.get('data') if isinstance(data, dict) else None
    if not isinstance(row, dict) or row.get('code') != code or not isinstance(row.get('klines'), list):
        raise ServiceError('history_prices_unavailable', '东方财富未返回匹配的历史日线')
    bars = []
    for line in row['klines']:
        fields = line.split(',') if isinstance(line, str) else []
        if len(fields) >= 6 and (bar := _bar(*fields[:6])):
            if len(fields)>6:_checked_amount(bar,fields[6],100)
            bars.append(bar)
    return {'bars': sorted({b['date']: b for b in bars}.values(), key=lambda b: b['date'])[-count:],
            'adjustment': adjustment, 'provider': 'eastmoney', 'source': 'https://quote.eastmoney.com/' + ('sh' if market == 'SH' else 'bj' if market == 'BJ' else 'sz') + code + '.html',
            'fetched_at': stamp(), 'volume_unit': '手', 'schema': VERSION}


def ths_bars(code, market, count, adjustment='qfq'):
    from .sources import request
    symbol, _ = _ths_identity(code, market)
    bars = []
    # THS 01 is forward-adjusted daily data; 11 is weekly and must not be
    # mixed into daily horizons. Year files avoid downloading full history.
    for year in range(now().year, now().year - min(6, count//180 + 2), -1):
        try:
            mode='00' if adjustment=='none' else '01'
            payload = json_payload(request(f'https://d.10jqka.com.cn/v6/line/{symbol}/{mode}/{year}.js',
                                           headers={'Referer': _ths_source(code, market)}))
        except ServiceError as error:
            if bars and error.code == 'source_http_404':
                break  # A newly listed stock need not have a prior-year file.
            raise
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), str):
            raise ServiceError('history_prices_unavailable', '同花顺日线格式异常')
        for line in payload['data'].split(';'):
            fields = line.split(',')
            if len(fields) >= 6 and (bar := _bar(fields[0], fields[1], fields[4], fields[2], fields[3], fields[5])):
                if len(fields)>6:_checked_amount(bar,fields[6],1)
                bar['volume'] = bar['volume']/100 if bar['volume'] is not None else None
                bars.append(bar)
        if len(bars) >= count:
            break
    return {'bars': sorted({b['date']: b for b in bars}.values(), key=lambda b: b['date'])[-count:],
            'adjustment': adjustment, 'provider': 'ths', 'source': _ths_source(code, market),
            'fetched_at': stamp(), 'volume_unit': '手（原始股数÷100）', 'schema': VERSION}


def _checked_amount(bar,raw,shares_per_volume_unit):
    """Retain turnover only if yuan/share arithmetic fits the OHLC range.

    In old adjusted bars the true turnover VWAP may lie outside the adjusted
    price range. Those values remain unavailable; never infer a new unit.
    """
    amount,volume=finite(raw),finite(bar.get('volume'))
    if amount is None or amount<0 or volume is None or volume<0:return
    if volume==0:
        if amount==0:bar.update(amount=0,amount_unit='yuan',amount_check='zero_volume')
        return
    vwap=amount/(volume*shares_per_volume_unit)
    if bar['low']-.02<=vwap<=bar['high']+.02:
        bar.update(amount=amount,amount_unit='yuan',amount_check='price_volume_consistency')


def compare_bars(primary, secondary):
    if primary.get('adjustment')!=secondary.get('adjustment'):
        return {'status':'not_comparable','reason':'复权口径不一致'}
    other = {b['date']: b for b in secondary['bars']}
    matched, conflicts = 0, []
    for bar in primary['bars'][-60:]:
        candidate = other.get(bar['date'])
        if candidate:
            if all(abs(bar[k]-candidate[k]) <= max(.02, abs(bar[k])*.001) for k in ('open','close','high','low')):
                matched += 1
            else:
                conflicts.append(bar['date'])
    return {'provider': secondary['provider'], 'source': secondary['source'], 'matched_sessions': matched,
            'conflict_dates': conflicts, 'status': 'conflict' if conflicts else 'matched' if matched else 'not_comparable',
            'basis': '相同交易日、相同复权口径的开高低收；成交量不参与价格一致性判断'}


def daily_bars(code, market, count=640, minimum=60, adjustment='qfq'):
    if adjustment not in ('qfq','none'):raise ServiceError('invalid_adjustment','不支持的复权口径')
    extra=() if adjustment=='qfq' else (adjustment,)
    from .securities import price_enabled
    if not price_enabled(code):raise ServiceError('security_delisted','该股票已确认退市，停止历史行情采集')
    errors = []
    selected = None
    try:
        selected = provider_call('eastmoney_bars', eastmoney_bars, code, market, count, *extra)
        if len(selected['bars']) < minimum:
            raise ServiceError('history_prices_unavailable', '东方财富日线条数不足')
    except ServiceError as error:
        errors.append({'provider': 'eastmoney', 'code': error.code})
        selected = None
    if selected is None:
        try:
            selected = provider_call('ths_bars', ths_bars, code, market, count, *extra)
        except ServiceError as error:
            errors.append({'provider': 'ths', 'code': error.code})
    elif not (code == '000300' and market == 'SH'):
        try:
            selected['cross_validation'] = compare_bars(selected, provider_call('ths_bars', ths_bars, code, market, min(count, 90), *extra))
        except ServiceError as error:
            errors.append({'provider': 'ths', 'code': error.code})
    if not selected or len(selected['bars']) < minimum:
        raise ServiceError('history_prices_unavailable', f'新行情源日线不足{minimum}条')
    selected.setdefault('cross_validation', {'status': 'unavailable', 'errors': errors})
    selected['source_errors'] = errors
    return selected
