"""Model-free quote capture immediately before a report enters the library.

Original model inputs and judgments remain immutable. Captures live beside
each company's notes, with a separate trading timestamp and capture time.
"""
from contextlib import contextmanager
from datetime import datetime,timedelta
import fcntl
import json
import re

from . import core


def folder(report):
    if report.get('market') not in ('SH','SZ','BJ') or not re.fullmatch(r'\d{6}',str(report.get('code',''))):
        raise core.ServiceError('invalid_market_identity','报告股票代码或市场无效')
    return core.VAULT/'30_公司'/report['market']/report['code']/'行情'


def path(report):
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,160}',str(report.get('id',''))):
        raise core.ServiceError('invalid_report_identity','报告编号无效')
    return folder(report)/'报告入库快照'/(report['id']+'.json')


@contextmanager
def _lock(report):
    directory=folder(report);directory.mkdir(parents=True,exist_ok=True)
    with (directory/'采集.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)


def _expiry(at):
    opening=at.replace(hour=9,minute=15,second=0,microsecond=0)
    if at.weekday()<5 and opening<=at<at.replace(hour=16,minute=30,second=0,microsecond=0):
        return at+timedelta(seconds=60)
    if at>=opening or at.weekday()>=5:opening+=timedelta(days=1)
    while opening.weekday()>=5:opening+=timedelta(days=1)
    return opening


def capture(report,backfill=None):
    from .market_sources import latest_quote
    from .market_metrics import build_market_snapshot
    from .foundation_data import financial_for
    target=path(report)
    with _lock(report):
        prior=json.loads(target.read_text()) if target.is_file() else None
        at=core.now()
        if prior and (prior['state']=='available' or prior.get('attempts',1)>=3 or prior.get('retry_after','')>at.isoformat()):return prior
        if backfill is None:
            try:backfill=(at-datetime.fromisoformat(report['generated_at'])).total_seconds()>120
            except (ValueError,TypeError,KeyError):backfill=True
        # Once marked as a later supplement it can never be relabelled as
        # the original quote merely because a later retry succeeds.
        kind='backfill' if backfill else (prior['kind'] if prior else 'publication')
        record={'schema':1,'report_id':report['id'],'code':report['code'],'market':report['market'],
                'kind':kind,'captured_at':core.stamp(),'report_generated_at':report.get('generated_at'),
                'attempts':(prior or {}).get('attempts',0)+1}
        try:
            from .securities import price_enabled
            if not price_enabled(report['code']):raise core.ServiceError('security_delisted','已退市，停止采集行情')
            cached_path=folder(report)/'最新报价缓存.json'
            cached=json.loads(cached_path.read_text()) if cached_path.is_file() else {}
            quote=cached['quote'] if cached.get('expires_at','')>at.isoformat() else latest_quote(report['code'],report['market'])
            if not cached or quote is not cached.get('quote'):
                core.atomic_json(cached_path,{'quote':quote,'expires_at':_expiry(at).isoformat()})
            financial=financial_for(report['code'],report['market']) or {}
            market=build_market_snapshot(quote,report.get('financial_analysis') or {},financial.get('history',[]),
                                         record['captured_at'],financial.get('source'))
            market.update(captured_at=record['captured_at'],capture_kind=kind,
                          basis_note='报告完成后采集的市场数据；不反向改写原模型判断、评分或预测基准')
            record.update(state='available',quote=quote,market_at_publication=market)
        except core.ServiceError as error:
            record.update(state='unavailable',error=error.code,retry_after=(at+timedelta(minutes=15)).isoformat())
        core.atomic_json(target.with_name(target.stem+'-attempt'+str(record['attempts'])+'.json'),record)
        core.atomic_json(target,record)
        return record


def attach(report,collect=False,backfill=None):
    """Return a presentation copy; never edit the saved model report."""
    target=path(report)
    record=capture(report,backfill) if collect else json.loads(target.read_text()) if target.is_file() else None
    if not record:return report
    result={**report,'market_publication_context':{k:record.get(k) for k in
            ('kind','captured_at','state','error','report_generated_at')}}
    if record['state']=='available':
        from .market_metrics import refresh_market_financials
        result['market_at_publication']=refresh_market_financials(
            record['market_at_publication'],record.get('quote'),report.get('financial_analysis') or {},
            known_at=report.get('generated_at'))
    return result
