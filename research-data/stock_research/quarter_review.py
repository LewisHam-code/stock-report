"""Source-backed quarter changes for narrative input and visible review.

This is a factual supplement, not a score or a hindsight price explanation.
Only the certified quarter bridge may supply numbers; missing stays missing.
"""
from . import core

VERSION = 'quarter-conclusion-review-v1'
NAMES = {'net_profit': '归母净利润', 'adjusted_profit': '扣非净利润',
         'revenue': '营业收入', 'operating_cashflow': '经营现金流'}


def _valid(value):
    return (isinstance(value, dict) and value.get('verified') is True and value.get('fact_id')
            and value.get('scope') == 'single_quarter' and not value.get('restatement_warning')
            and core.finite(value.get('standard_value')) is not None)


def _change(current, previous, name):
    a, b = current['standard_value'], previous['standard_value']
    pct = (a - b) / abs(b) * 100 if b else None
    if a >= 0 > b:
        text = '转为净流入' if name == 'operating_cashflow' else '扭亏为盈'
    elif a < 0 <= b:
        text = '转为净流出' if name == 'operating_cashflow' else '由盈转亏'
    elif a < 0 and b < 0:
        text = ('净流出收窄' if name == 'operating_cashflow' else '减亏') if a > b else ('净流出扩大' if name == 'operating_cashflow' else '亏损扩大') if a < b else '持平'
        if pct: text += f'{abs(pct):.2f}%'
    elif pct is None:
        text = '由零增加' if a > 0 else '持平'
    else:
        text = '增长' + f'{pct:.2f}%' if pct > 0 else '下降' + f'{abs(pct):.2f}%' if pct < 0 else '持平'
    return pct, text


def build(bridge, *, asof=None):
    from .financial_periods import quarter, period_for
    info = quarter(bridge.get('period'))
    if not info:
        return {'version': VERSION, 'summary': '', 'changes': [], 'missing': []}
    period_label = ('一季度', '二季度', '三季度', '四季度')[info[1] - 1]
    year, q = info
    expected = (period_for(year, q), period_for(year if q > 1 else year - 1, q - 1 if q > 1 else 4))
    changes, highlights, missing = [], [], []
    for name in NAMES:
        row = next((r for r in bridge.get('rows', []) if r.get('metric') == name), {})
        a, b = row.get('current'), row.get('previous')
        if (not _valid(a) or not _valid(b) or a.get('unit') != 'yuan' or b.get('unit') != 'yuan'
                or a.get('basis') != b.get('basis') or (a.get('period'), b.get('period')) != expected):
            missing.append(name)
            continue
        pct, text = _change(a, b, name)
        change = {'metric': name, 'current': a, 'previous': b, 'qoq_pct': pct, 'text': text}
        # Materiality is for conclusion emphasis only, never scoring/ratings.
        material = (pct is not None and abs(pct) >= 10) or (a['standard_value'] < 0 <= b['standard_value']) or (b['standard_value'] < 0 <= a['standard_value'])
        if material:
            change['material'] = True
            highlights.append(NAMES[name] + '环比' + text)
        changes.append(change)
    margin = bridge.get('gross_margin') or {}
    a, b = margin.get('current'), margin.get('previous')
    if _valid(a) and _valid(b) and a.get('unit') == b.get('unit') == 'percentage' and (a.get('period'), b.get('period')) == expected:
        delta = a['standard_value'] - b['standard_value']
        changes.append({'metric': 'gross_margin', 'current': a, 'previous': b, 'delta_pp': delta,
                        'text': '收缩' if delta < 0 else '提升' if delta > 0 else '持平', 'material': abs(delta) >= 2})
        if abs(delta) >= 2:
            highlights.append(f"毛利率由{b['standard_value']:.2f}%{'降' if delta < 0 else '升'}至{a['standard_value']:.2f}%（{'下降' if delta < 0 else '提升'}{abs(delta):.2f}个百分点）")
    else:
        missing.append('gross_margin')
    return {'version': VERSION, 'asof': asof or core.stamp(), 'period': bridge.get('quarter', f'{year}Q{q}'),
            'previous_quarter': bridge.get('previous_quarter'), 'changes': changes, 'missing': missing,
            'summary': period_label + '：' + '；'.join(highlights) + '。' if highlights else '',
            'note': '按已核对原文计算单季变化；环比受季节性与一次性项目影响，不据此断言低于市场预期或解释股价涨跌。'}


def model_view(review):
    """Bounded model input; full source facts remain in the saved report."""
    sources = {}
    def refs(value):
        if value.get('inputs'):
            return list(dict.fromkeys(ref for item in value['inputs'] for ref in refs(item)))
        identity = value['fact_id']
        sources[identity] = {k: value.get(k) for k in ('period', 'metric', 'standard_value', 'source_page', 'source_url', 'document_id')}
        return [identity]
    changes = []
    for row in review.get('changes', []):
        changes.append({**{k: row[k] for k in ('metric', 'qoq_pct', 'delta_pp', 'text', 'material') if k in row},
                        'current': row['current']['standard_value'], 'previous': row['previous']['standard_value'],
                        'source_facts': list(dict.fromkeys(refs(row['current']) + refs(row['previous'])))})
    return {**{k: review.get(k) for k in ('version', 'period', 'previous_quarter', 'summary', 'missing', 'note')},
            'changes': changes, 'source_facts': sources}
