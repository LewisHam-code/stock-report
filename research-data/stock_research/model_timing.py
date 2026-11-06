"""Price-aware admission for research calls; no scheduler, sleep or network."""
from datetime import date, timedelta

from . import core
from .model_endpoints import endpoint_settings

WAIT = 'model_off_peak_wait'
PRICING_SOURCE = 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
HOLIDAY_SOURCE = 'https://www.beijing.gov.cn/cs/gncs/zcwj/202603/t20260327_4568275.html'
# Published national holiday arrangements; adjusted weekend workdays remain
# off-peak under DeepSeek's explicit Monday-Friday pricing definition.
HOLIDAYS = {2026: (('01-01','01-03'), ('02-15','02-23'), ('04-04','04-06'),
                   ('05-01','05-05'), ('06-19','06-21'), ('09-25','09-27'), ('10-01','10-07'))}
PURPOSES = {'financial_extraction', 'comprehensive_report', 'analysis', 'deep_report'}


def applicable(cfg):
    model = cfg.get('model', '')
    if cfg.get('provider') != 'deepseek' or not (
        model in ('deepseek-v4-pro', 'deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp')
        or model.startswith('deepseek-v4-pro-')
    ):
        return False
    return not endpoint_settings('deepseek', cfg.get('api_protocol'), cfg.get('base_url'))['custom_base_url']


def _holiday(day):
    return any(date.fromisoformat(f'{day.year}-{start}') <= day <= date.fromisoformat(f'{day.year}-{end}')
               for start, end in HOLIDAYS.get(day.year, ()))


def window(cfg, at=None, purpose='comprehensive_report'):
    at = (at or core.now()).astimezone(core.TZ)
    enabled = cfg.get('prefer_off_peak', True) and applicable(cfg) and purpose in PURPOSES
    result = {'enabled':bool(enabled), 'deferred':False, 'timezone':'Asia/Shanghai',
              'pricing_source':PRICING_SOURCE, 'pricing_checked_at':'2026-10-27', 'holiday_source':HOLIDAY_SOURCE,
              'holiday_calendar_known':at.year in HOLIDAYS, 'guard_minutes':5}
    if not enabled or at.weekday() >= 5 or _holiday(at.date()):
        return result
    # Stop admitting requests five minutes before peak; do not interrupt an
    # already-running response. Admission is rechecked at the transport layer.
    for start, end in ((9,12), (14,18)):
        peak = at.replace(hour=start, minute=0, second=0, microsecond=0)
        resume = at.replace(hour=end, minute=0, second=0, microsecond=0)
        if peak-timedelta(minutes=5) <= at < resume:
            result.update(deferred=True, resume_at=resume.isoformat(timespec='seconds'),
                          reason='等待模型闲时，采集继续；最早可于北京时间 '+resume.strftime('%H:%M')+' 续跑')
            break
    return result


def require_idle(cfg, purpose):
    status = window(cfg, purpose=purpose)
    if status['deferred']:
        error = core.ServiceError(WAIT, status['reason'])
        error.timing = status
        raise error


def research_window(cfg, profiles=None):
    """Preflight the primary narrative route; each actual stage also checks."""
    profiles = core.model_profiles(cfg) if profiles is None else profiles
    primary = next((p for p in profiles if p.get('enabled') and 'deep_analysis' in core.normalized_model_stages(p.get('stages', core.MODEL_STAGES))), None)
    return window({**cfg, **primary}) if primary else {'enabled':False, 'deferred':False}


def price_multiplier(cfg,at=None):
    """Actual tariff time, excluding the five-minute admission safety margin."""
    at=(at or core.now()).astimezone(core.TZ)
    if not applicable(cfg) or at.weekday()>=5 or _holiday(at.date()):return 1
    return 2 if 9<=at.hour<12 or 14<=at.hour<18 else 1
