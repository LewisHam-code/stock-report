"""Display-only quarter bridge from certified original filings. No model calls.

Never reverse a rounded growth rate into a historical amount. Never subtract
EPS, ROE or a balance-sheet stock to manufacture a quarterly flow.
"""
from copy import deepcopy
from decimal import Decimal
import json
import re

from . import core

VERSION = 'financial-period-comparison-v2'
FLOWS = ('revenue', 'net_profit', 'adjusted_profit', 'operating_cashflow')
ENDS = {'Q1': '03-31', 'H1': '06-30', 'Q3': '09-30', 'FY': '12-31'}


def quarter(period):
    if not re.fullmatch(r'20\d{2}(Q1|H1|Q3|FY)', str(period)):
        return None
    return int(period[:4]), {'Q1': 1, 'H1': 2, 'Q3': 3, 'FY': 4}[period[4:]]


def period_for(year, q):
    return str(year) + ('Q1', 'H1', 'Q3', 'FY')[q - 1]


def required_periods(period):
    current = quarter(period)
    if not current:
        return []
    year, q = current
    previous = (year if q > 1 else year - 1, q - 1 if q > 1 else 4)
    py, pq = previous
    targets = [(year, q), previous, (year - 1, q), (py - 1, pq), (py if pq > 1 else py - 1, pq - 1 if pq > 1 else 4)]
    result = []
    for y, n in targets:
        for p in ([period_for(y, n)] + ([period_for(y, n - 1)] if n > 1 else [])):
            if p not in result:
                result.append(p)
    return result


def _usable(m, period):
    evidence = m.get('evidence') or {}
    return (m.get('verified') is True and m.get('fact_id') and core.finite(m.get('standard_value')) is not None
            and m.get('period_end') == period[:4] + '-' + ENDS[period[4:]]
            and evidence.get('entity') in ('consolidated', 'listed_consolidated_summary')
            and evidence.get('source_sha256') and (m.get('unit') in ('yuan', 'thousand_yuan', 'ten_thousand_yuan', 'million_yuan', 'hundred_million_yuan')
                                                  or m.get('metric') == 'roe' and m.get('unit') == 'percentage'))


def _basis(m):
    if m.get('revenue_basis'):
        return m['revenue_basis']
    label = (m.get('evidence') or {}).get('label', {}).get('text', '')
    return 'total_revenue' if m.get('metric') == 'revenue' and '营业总收入' in label else 'operating_revenue' if m.get('metric') == 'revenue' else m.get('metric')


def _amount(m, period):
    value = {k: deepcopy(m.get(k)) for k in ('metric', 'standard_value', 'scope', 'period_end', 'fact_id', 'source_page', 'yoy_pct', 'source_url', 'document_id', 'restatement_warning', 'verification_method')}
    value.update(verified=True, unit='percentage' if m.get('unit') == 'percentage' else 'yuan', basis=_basis(m), source_sha256=m['evidence']['source_sha256'],
                 period=period, method='原文单季' if m['scope'] == 'single_quarter' else '原文累计')
    return value


def build(period, filings):
    """filings: exact fiscal period -> certified metrics; pure and testable."""
    current = quarter(period)
    if not current:
        return {'version': VERSION, 'rows': []}
    year, q = current
    main_revenue = next((m for m in filings.get(period, []) if m.get('metric') == 'revenue' and m.get('scope') == 'cumulative' and _usable(m, period)), {})
    revenue_basis = _basis(main_revenue)

    def find(p, name, scope):
        candidates = [m for m in filings.get(p, []) if m.get('metric') == name and m.get('scope') == scope and _usable(m, p)]
        if name == 'revenue':
            # Income statements can certify operating revenue separately from
            # total operating income. Use that explicit row, never assume equal.
            candidates += [{**m, 'metric': 'revenue', 'revenue_basis': 'operating_revenue'} for m in filings.get(p, [])
                           if m.get('metric') == 'operating_revenue' and m.get('scope') == scope and _usable(m, p)]
            candidates.sort(key=lambda m: _basis(m) != revenue_basis)
        return candidates[0] if candidates else None

    def single(y, n, name):
        p = period_for(y, n)
        direct = find(p, name, 'single_quarter')
        if direct:
            return _amount(direct, p)
        cumulative = find(p, name, 'cumulative')
        if not cumulative:
            return None
        if n == 1:
            return {**_amount(cumulative, p), 'scope': 'single_quarter', 'method': '第一季度累计即单季'}
        prev = period_for(y, n - 1)
        previous = find(prev, name, 'cumulative')
        if not previous or _basis(cumulative) != _basis(previous):
            return None
        # Cross-filing restatements need reconciliation rather than subtraction.
        if cumulative.get('restatement_warning') or previous.get('restatement_warning'):
            return None
        inputs = [_amount(cumulative, p), _amount(previous, prev)]
        value = float(Decimal(str(inputs[0]['standard_value'])) - Decimal(str(inputs[1]['standard_value'])))
        return {'metric': name, 'scope': 'single_quarter', 'period': p, 'period_end': cumulative['period_end'],
                'verified': True, 'unit': 'yuan', 'basis': _basis(cumulative), 'standard_value': value,
                'method': '全年减前三季度' if n == 4 else '本期累计减上期累计', 'derived': True,
                'formula': p + '累计 − ' + prev + '累计', 'inputs': inputs,
                'fact_id': core.digest([VERSION, *[x['fact_id'] for x in inputs]])[:24]}

    rows = []
    for name in FLOWS:
        latest, previous, prior = single(year, q, name), single(year if q > 1 else year - 1, q - 1 if q > 1 else 4, name), single(year - 1, q, name)
        cumulative = find(period, name, 'cumulative')
        prior_cumulative = find(str(year - 1) + period[4:], name, 'cumulative')
        if latest and previous and latest['basis'] != previous['basis']:
            previous = None
        if latest and prior and latest['basis'] != prior['basis']:
            prior = None
        if latest and latest.get('restatement_warning'):
            previous = prior = None
        if previous and previous.get('restatement_warning'):
            previous = None
        if prior and prior.get('restatement_warning'):
            prior = None
        if cumulative and prior_cumulative and _basis(cumulative) != _basis(prior_cumulative):
            prior_cumulative = None
        if (cumulative and cumulative.get('restatement_warning')) or (prior_cumulative and prior_cumulative.get('restatement_warning')):
            prior_cumulative = None
        rows.append({'metric': name, 'current': latest, 'previous': previous, 'prior_year': prior,
                     'cumulative': _amount(cumulative, period) if cumulative else None,
                     'prior_cumulative': _amount(prior_cumulative, str(year - 1) + period[4:]) if prior_cumulative else None})
    py, pq = (year if q > 1 else year - 1, q - 1 if q > 1 else 4)
    previous_period = period_for(py, pq)
    previous_rows = []
    for row in rows:
        name = row['metric']
        before = single(py if pq > 1 else py - 1, pq - 1 if pq > 1 else 4, name)
        prior = single(py - 1, pq, name)
        current_value = row['previous']
        if current_value and before and current_value['basis'] != before['basis']:
            before = None
        if current_value and prior and current_value['basis'] != prior['basis']:
            prior = None
        if before and before.get('restatement_warning'):
            before = None
        if prior and prior.get('restatement_warning'):
            prior = None
        previous_rows.append({'metric': name, 'current': current_value, 'previous': before, 'prior_year': prior})
    previous_metrics = []
    for name, scope in [('inventory', 'instant'), ('roe', 'cumulative' if pq == 1 else 'single_quarter')]:
        metric = find(previous_period, name, scope)
        if metric:
            previous_metrics.append(_amount(metric, previous_period))
    annual_period = str(year - 1) + 'FY' if q == 1 else period
    annual_metrics = []
    annual_rows = []
    for name in (*FLOWS, 'inventory', 'roe'):
        metric = find(annual_period, name, 'instant' if name == 'inventory' else 'cumulative')
        if metric:
            annual_metrics.append(_amount(metric, annual_period))
        prior = find(str(int(annual_period[:4]) - 1) + annual_period[4:], name, 'instant' if name == 'inventory' else 'cumulative')
        if metric and prior and (_basis(metric) != _basis(prior) or metric.get('restatement_warning') or prior.get('restatement_warning')):
            prior = None
        annual_rows.append({'metric': name, 'prior_cumulative': _amount(prior, str(int(annual_period[:4]) - 1) + annual_period[4:]) if prior else None})
    def margin(y, n):
        revenue, cost = single(y, n, 'revenue'), single(y, n, 'operating_cost')
        if (not revenue or not cost or revenue['basis'] != 'operating_revenue'
                or revenue['standard_value'] <= 0 or cost['standard_value'] < 0
                or revenue.get('restatement_warning') or cost.get('restatement_warning')):
            return None
        value = (Decimal(str(revenue['standard_value'])) - Decimal(str(cost['standard_value']))) / Decimal(str(revenue['standard_value'])) * 100
        return {'metric': 'gross_margin', 'standard_value': float(value), 'unit': 'percentage',
                'scope': 'single_quarter', 'period': period_for(y, n), 'period_end': revenue['period_end'],
                'verified': True, 'derived': True, 'inputs': [revenue, cost],
                'formula': '(单季合并营业收入 − 单季合并营业成本) / 单季合并营业收入 × 100',
                'fact_id': core.digest([VERSION, revenue['fact_id'], cost['fact_id'], 'quarter_margin'])[:24]}
    return {'version': VERSION, 'period': period, 'quarter': f'{year}Q{q}',
            'gross_margin': {'current': margin(year, q), 'previous': margin(py, pq)},
            'previous_rows': previous_rows, 'previous_metrics': previous_metrics,
            'annual_period': annual_period, 'annual_metrics': annual_metrics, 'annual_rows': annual_rows,
            'previous_quarter': f'{year if q > 1 else year - 1}Q{q - 1 if q > 1 else 4}',
            'prior_year_quarter': f'{year - 1}Q{q}', 'rows': rows,
            'note': '仅同口径、已核对的原文金额参与比较；单季差额为计算值，不等于单独审计。'}


def enrich(store, doc, extraction, *, asof=None):
    """Read local certified sources only; publishing must never fetch the market."""
    from .financial_evidence import primary_metrics
    from .research_inputs import supporting_index
    cutoff = asof or core.stamp()
    docs = store.rows('SELECT * FROM documents WHERE code=? AND market=? AND published_at<=? ORDER BY published_at DESC,id DESC', (doc['code'], doc['market'], cutoff))
    try:
        docs += json.loads(supporting_index(doc['code'], doc['market']).read_text()).get('documents', [])
    except (OSError, ValueError):
        pass
    docs.sort(key=lambda d: (d.get('published_at', ''), d.get('id', '')), reverse=True)
    periods = set(required_periods(doc['period']))
    def decorate(source, metrics):
        rows = [deepcopy(m) for m in metrics if (m.get('evidence') or {}).get('source_sha256') == source.get('sha256')]
        correction = any(w in source.get('title', '') for w in ('更正', '修订', '重述'))
        for m in rows:
            m.update(source_url=source.get('source_url'), document_id=source.get('id'))
            if correction:
                m['restatement_warning'] = True
        return rows
    filings = {doc['period']: decorate(doc, extraction.get('metrics', []))}
    for source in docs:
        p = source.get('period')
        if (p not in periods or p in filings or source.get('code') != doc['code'] or source.get('market') != doc['market']
                or source.get('published_at', '') > cutoff):
            continue
        proof = primary_metrics(store, source, allow_refetch=False)
        if proof.get('status') != 'checked' or any(not c['passed'] for c in proof.get('checks', [])):
            continue
        filings[p] = decorate(source, proof.get('metrics', []))
    return build(doc['period'], filings)
