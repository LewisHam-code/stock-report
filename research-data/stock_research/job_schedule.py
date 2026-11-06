"""Independent, China-time policies for disclosures, models and market prices."""
from datetime import datetime
from . import core

POLICY='adaptive-v1'
CALENDAR_SOURCE='https://www.sse.com.cn/disclosure/dealinstruc/closed/'
# Exchange closures, verified against the SSE annual calendar, 2026-10-20.
CLOSURES={2026:(('01-01','01-03'),('02-15','02-23'),('04-04','04-06'),('05-01','05-05'),
                ('06-19','06-21'),('09-25','09-27'),('10-01','10-07'))}

def enabled(cfg):return cfg.get('schedule_policy')==POLICY

def trading_day(day):
    if day.weekday()>=5:return False
    if day.year not in CLOSURES:return None
    return not any(a<=day.strftime('%m-%d')<=b for a,b in CLOSURES[day.year])

def disclosure_slot(at=None):
    at=(at or core.now()).astimezone(core.TZ);minute=at.hour*60+at.minute
    return {'slot':at.date().isoformat()+':15:'+str(minute//15),
            'mode':'continuous','interval_seconds':900}

def disclosure_due(state,at=None):
    at=(at or core.now()).astimezone(core.TZ);slot=disclosure_slot(at)
    last=state.get('last_started_at')
    try:elapsed=(at-datetime.fromisoformat(last)).total_seconds()
    except (TypeError,ValueError,AttributeError):elapsed=None
    # Fixed quarter-hour boundaries and durable receipts survive restarts.
    if state.get('slot')==slot['slot']:return False
    if elapsed is not None and elapsed<0:return False
    return True

def market_slot(at=None):
    at=(at or core.now()).astimezone(core.TZ)
    if trading_day(at.date()) is not True:return None
    minute=at.hour*60+at.minute;day=at.date().isoformat()
    if 570<=minute<690 or 780<=minute<900:
        base=570 if minute<=690 else 780
        return {'slot':day+':live:'+str(base+(minute-base)//30*30),'mode':'intraday','interval_seconds':1800}
    if minute>=935:return {'slot':day+':close','mode':'close','interval_seconds':1800}
    return None

def market_due(state,at=None):
    slot=market_slot(at)
    return bool(slot and slot['slot']!=state.get('market_slot'))

def public_policy():
    return {'timezone':'Asia/Shanghai','disclosure_minutes':15,'disclosure_hours':'全天',
            'price_minutes':30,'morning':'09:30—11:30','afternoon':'13:00—15:00',
            'closing_capture':'15:35','observation_days':14,'calendar_source':CALENDAR_SOURCE}
