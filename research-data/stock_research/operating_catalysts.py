"""Six mandatory operating checks, with quotations kept apart from truth.

Discovery is deterministic and local. Keywords create leads, never scores or
buy signals. Promotion requires independently reviewed operating observations.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

VERSION = 'operating-catalysts-v1'
CHECKS = (
    ('product_shortage', '产品供不应求', 'growth', r'供不应求|订单饱满|(?:产能|生产线).{0,8}满负荷', 'supply_demand'),
    ('industry_upturn', '行业高景气度上行', 'industry', r'景气.{0,8}(?:上行|回升|提升|高位)|行业.{0,12}(?:高增长|需求增长|需求上升)', 'industry_cycle'),
    ('market_expansion', '市场拓展', 'growth', r'(?:市场|客户|海外).{0,12}(?:拓展|开拓|突破|超预期)|(?:拓展|开拓).{0,8}(?:市场|客户)', 'market_expansion'),
    ('new_product', '新品上市与放量', 'growth', r'新品|新产品|新一代产品', 'new_product'),
    ('price_uptrend', '产品价格中枢上移', 'industry', r'(?:产品|销售|售价|价格中枢).{0,12}(?:价格上涨|上涨|上行|提升|提高|提价)|(?:产品|销售).{0,8}提价', 'selling_price'),
    ('tight_supply_demand', '供给偏紧、需求旺盛', 'industry', r'(?:供给|供应|供需).{0,8}(?:偏紧|紧张)|需求.{0,6}旺盛', 'supply_demand'),
)
NEGATIVE = re.compile(r'不(?:存在|构成|会出现|会发生)|未(?:出现|发生)|并非|尚未|没有|无法确认|需求(?:不足|疲软)|风险(?:因素|提示)|若|倘若|假如|如果')


def _compact(value):
    return re.sub(r'\s+', '', str(value))


def _before(value, cutoff):
    try:
        a, b = datetime.fromisoformat(value), datetime.fromisoformat(cutoff)
        return a.tzinfo is not None and b.tzinfo is not None and a <= b
    except (TypeError, ValueError):
        return False


def reviewed_observations(rows, signal_id, cutoff):
    """A quote-matched management statement alone cannot pass this gate."""
    result = []
    for row in rows or []:
        if (row.get('signal_id') != signal_id or row.get('review_status') != 'operating_verified'
                or row.get('basis') not in ('official_operating_data', 'independent_operating_data')
                or not row.get('reviewer') or not row.get('source_url', '').startswith('https://')
                or not row.get('quote') or not row.get('source_sha256')
                or not _before(row.get('known_at'), cutoff) or not _before(row.get('reviewed_at'), cutoff)
                or row.get('materiality') not in ('core_revenue', 'core_profit')
                or not row.get('business_scope') or not row.get('falsification')
                or row.get('value') is None or not row.get('unit') or not row.get('period')):
            continue
        result.append(row)
    return result


def discover(report, pages=(), *, asof, observations=()):
    """Run all six checks even when no positive phrase is found.

    Source text is data only. No instructions from a filing are executed.
    Bounded excerpts prevent full-document duplication in public payloads.
    """
    candidates = []
    for page in pages:
        for paragraph in re.split(r'[。；\n]+', page.get('text', '')):
            if any(re.search(pattern, paragraph) for _, _, _, pattern, _ in CHECKS):
                candidates.append({'quote': paragraph.strip()[:500], 'source_page': page.get('page'), 'quote_verified': True})
    # Historical exports may not have page text. Explicitly retain these as
    # unlocated leads, irrespective of an old model's quote_verified flag.
    if not pages:
        candidates = [{**c, 'quote_verified': False} for c in (report.get('financial_analysis') or {}).get('management_claims', [])]
    output = []
    for ident, title, dimension, pattern, group in CHECKS:
        evidence = []
        seen = set()
        excluded = 0
        for candidate in candidates:
            quote = candidate.get('quote', '')
            if not re.search(pattern, _compact(quote)):
                continue
            if NEGATIVE.search(_compact(quote)):
                excluded += 1
                continue
            key = _compact(quote)
            if key in seen:
                continue
            seen.add(key)
            evidence.append({'quote': quote, 'page': candidate.get('source_page'),
                             'url': report.get('source'), 'known_at': report.get('disclosed_at') or report.get('generated_at'),
                             'quote_verified': candidate.get('quote_verified') is True,
                             'operationally_verified': False,
                             'economic_event_id': group+'-'+hashlib.sha256(key.encode()).hexdigest()[:12]})
        verified = reviewed_observations(observations, ident, asof)
        latest = max(verified, key=lambda r: r['known_at']) if verified else None
        periods = {r['period'] for r in verified if latest and r.get('comparison_key') == latest.get('comparison_key') and r.get('comparison_key')}
        repeated = len(periods) >= 2
        baseline = (latest or {}).get('expectation_baseline') or {}
        surprise = bool(latest and baseline.get('value') is not None and baseline.get('source_url')
                        and _before(baseline.get('frozen_at'), latest.get('result_published_at'))
                        and baseline.get('unit') == latest.get('unit') and baseline.get('period') == latest.get('period')
                        and baseline.get('business_scope') == latest.get('business_scope')
                        and latest.get('value') > baseline.get('value'))
        state = 'watch' if latest else 'lead' if evidence else 'not_found'
        if latest and repeated:
            # Persistence requires comparable measurements and their direction,
            # not just a second optimistic article about the same event.
            comparable = sorted([r for r in verified if r.get('comparison_key') == latest.get('comparison_key')], key=lambda r: r['known_at'])
            before = next((r for r in reversed(comparable[:-1]) if r['period'] != latest['period'] and r['unit'] == latest['unit']), None)
            if before and before['value'] != latest['value']:
                state = 'strengthening' if latest['value'] > before['value'] else 'weakening'
        output.append({'id': ident, 'name': title, 'dimension': dimension, 'economic_group': group,
                       'status': state, 'status_label': {'not_found':'本次材料未发现', 'lead':'待验证线索', 'watch':'增长催化·重点关注', 'strengthening':'持续强化', 'weakening':'信号减弱'}[state],
                       'evidence': evidence[:4], 'excluded_risk_or_negative_passages': excluded,
                       'operating_observations': verified, 'sustained': repeated,
                       'exceeds_preexisting_expectation': surprise,
                       'first_known_at': min((r['known_at'] for r in evidence+verified if r.get('known_at')), default=None),
                       'missing': ([] if latest else ['需核实业务规模、实际交付/销量/售价及利润影响', '公司原文只能证明主张存在，不能单独证明经营兑现'])
                                  + ([] if surprise else ['未取得可比事前预期基准，不称超预期'])
                                  + ([] if repeated else ['缺少重复可比观测，不称持续']),
                       'score_contribution': 0})
    return {'version': VERSION, 'checked_at': asof, 'checks': output,
            'policy': '六项逐项检查；关键词只发现线索，不加分；供需两项共享经济事件组，不能重复计分；关注不等于买入。'}
