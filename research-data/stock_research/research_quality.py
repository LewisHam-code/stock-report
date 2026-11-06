"""Explain scoring gaps without filling missing factors or calling a model."""
from __future__ import annotations

from collections import Counter

from .core import SITE, VAULT, atomic_json, atomic_text, stamp


def remediation(item):
    name = item.get('name', '')
    actions = {
        '同行盈利收益率比较': ('待采集与核验', '补齐至少20家独立同业、同一时点且行业已核实的正PE样本；亏损公司不计算PE分位'),
        '自由现金流收益率': ('待原文核验', '补齐TTM经营现金流、购建长期资产现金支出和同期市值；不以投资现金流替代资本开支'),
        '有外部证据的景气方向': ('待核实行业证据', '补齐至少两条90日内有明确发布日期和原文引文的行业资料，再形成景气判断'),
        '60日相对强度': ('待补齐日线', '核对个股与沪深300的同日期日线和新鲜度'),
        '风险调整中期动量': ('待核对历史长度', '核对是否有273个交易日日线；上市时间不足时保留缺项'),
        '强势回调买点': ('待核对日线', '核对交易历史长度及行情新鲜度'),
    }
    return actions.get(name, ('待核对财务口径', '核对原文、结构化历史财务的期间、单位和可比基期；未披露与解析失败不能混为一类'))


def build(reports):
    latest = {}
    for report in sorted(reports, key=lambda r: (r.get('generated_at', ''), r['id']), reverse=True):
        latest.setdefault(report.get('document_id', report['id']), report)
    factors, rows = {}, []
    for report in latest.values():
        score = report.get('scoring') or {}
        gaps = []
        for item in score.get('items', []):
            if item.get('available'):
                continue
            state, action = remediation(item)
            gap = {'factor': item['name'], 'dimension': item['group'], 'weight': item['weight'],
                   'state': state, 'reason': item.get('missing_reason') or '尚无可用输入', 'action': action}
            if item['name']=='自由现金流收益率':
                gap['evidence_status'] = report.get('ttm_cashflow_evidence')
            gaps.append(gap)
            factor = factors.setdefault(item['name'], {**gap, 'reports': 0})
            factor['reports'] += 1
        rows.append({'report_id': report['id'], 'code': report['code'], 'company': report['company'],
                     'coverage_pct': score.get('coverage_pct'), 'score_status': score.get('status', 'unknown'),
                     'source_errors': report.get('data_gaps', []), 'gaps': gaps})
    return {'schema': 1, 'generated_at': stamp(), 'report_count': len(rows),
            'provisional_count': sum(row['score_status'] == 'provisional' for row in rows),
            'dimensions': dict(Counter(gap['dimension'] for row in rows for gap in row['gaps'])),
            'factors': sorted(factors.values(), key=lambda x: (-x['reports'], x['dimension'], x['factor'])),
            'reports': rows,
            'method': '全库统计，不随页面筛选改变；每份财报只统计最新一版解读。状态表示待办类型，不表示已经发起补采。缺失原因沿用实际评分输入，不填零或行业均值。'}


def publish_quality(reports, site=None, vault=None):
    payload = build(reports)
    atomic_json((site or SITE) / 'research-quality.json', payload)
    note = (vault or VAULT) / '00_导航' / '研究数据缺口-自动生成.md'
    marker = '<!-- RESEARCH_QUALITY_V1 -->'
    lines = [marker, '# 研究数据缺口', '', payload['method'], '',
             f"{payload['report_count']}份最新解读，{payload['provisional_count']}份暂定评分。", '',
             '| 因子 | 涉及报告 | 待办 | 补齐条件 |', '|---|---:|---|---|']
    for factor in payload['factors']:
        lines.append(f"| {factor['factor']} | {factor['reports']} | {factor['state']} | {factor['action']} |")
    content = '\n'.join(lines) + '\n'
    if not note.exists() or (marker in note.read_text() and note.read_text() != content):
        atomic_text(note, content)
    return {'reports': payload['report_count'], 'provisional': payload['provisional_count'], 'factors': len(payload['factors'])}
