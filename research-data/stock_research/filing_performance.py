"""Announcement-date observations, separate from prospective rating tracking."""
from . import core,tracking

def observation(report,document,snapshot,benchmark):
    disclosed=(document or {}).get('published_at')
    if not disclosed or report.get('test_mode') or report.get('report_kind')=='historical_foundation':return None
    outcomes={}
    for horizon in tracking.HORIZONS:
        result=tracking.horizon_result({'generated_at':disclosed},snapshot.get('prices'),benchmark,horizon)
        # Retrospective observations must never enter prediction hit rates.
        outcomes[str(horizon)]={k:v for k,v in result.items() if k not in ('evaluable','aligned','relative_aligned')}
    return {'schema':1,'disclosed_at':disclosed,'observed_at':core.stamp(),'horizons':outcomes,
            'basis':'公告日后的首个交易日开盘至第N个交易日收盘，个股采用前复权行情；只观察发布后表现，不计入解读预测准确率。'}

def attach(report,document,snapshot,benchmark):
    value=observation(report,document,snapshot,benchmark)
    if value is None:return report
    path=core.VAULT/'30_公司'/report['market']/report['code']/'公告后表现'/(report['id']+'.json')
    core.atomic_json(path,value)
    return {**report,'filing_performance':value}
