"""Compute TTM cash inputs only from already available, checked PDF tables."""
from __future__ import annotations

import re

from .core import finite, now


def verified_cashflow(store, code, asof=None):
    from .financial_evidence import primary_metrics
    asof = str(asof or now().date().isoformat())[:10]
    documents = store.rows('SELECT * FROM documents WHERE code=? ORDER BY published_at DESC,id DESC',(code,))
    # Exact supporting PDFs are deliberately outside the model-reading queue.
    from . import core
    from .research_inputs import supporting_index
    import json
    for market in ('SH','SZ','BJ'):
        path=supporting_index(code,market)
        if path.is_file():
            try:
                saved=json.loads(path.read_text())
                documents.extend(d for d in saved.get('documents',[]) if d.get('code')==code and d.get('market')==market and d.get('supporting_only') is True)
            except (ValueError,OSError):pass
    documents.sort(key=lambda d:d.get('published_at',''),reverse=True)
    by_period = {}
    ends = {'FY':'12-31','H1':'06-30','Q1':'03-31','Q3':'09-30'}
    for doc in documents:
        match = re.fullmatch(r'(20\d{2})(FY|H1|Q1|Q3)',doc['period'])
        if not match or doc['published_at'][:10] > asof:
            continue
        end = match[1]+'-'+ends[match[2]]
        if end <= asof:
            by_period.setdefault(doc['period'],doc)
    ordered = sorted(by_period, key=lambda p:p[:4]+'-'+ends[p[4:]], reverse=True)
    if not ordered:
        return {'ttm_cashflow_evidence':{'status':'missing','required_periods':[],'reason':'尚无已披露可用财报'}}
    latest = ordered[0]
    required = [latest] if latest.endswith('FY') else [latest,str(int(latest[:4])-1)+'FY',str(int(latest[:4])-1)+latest[4:]]
    missing = [period for period in required if period not in by_period]
    result = {'ttm_cashflow_evidence':{'status':'missing','period':latest,'required_periods':required,
                                      'missing_periods':missing,'source_documents':[]}}
    if missing:
        return result
    values = {'operating_cashflow':[], 'capex':[]}
    for period in required:
        doc = by_period[period]
        proof = primary_metrics(store, doc)
        relevant_conflicts=[c for c in proof.get('conflicts',[]) if c.get('scope') in (None,'cumulative') and (not c.get('metric') or c.get('metric') in values)]
        if proof.get('status')!='checked' or relevant_conflicts or any(not check['passed'] for check in proof.get('checks',[])):
            result['ttm_cashflow_evidence']['reason']='所需累计现金流原文存在冲突、会计检查未通过或解析尚未完成'
            return result
        from .financial_facts import CONTRACT
        def certified_method(metric):
            if metric.get('verification_method') in ('pdf_statement_column','pdf_table_column'):return True
            # Discovery's internal method name remains "candidate", even after
            # SourceIndex independently certifies its physical anchors. The
            # contract/fact identity/anchors, not this legacy name, prove it.
            evidence=metric.get('evidence') or {}
            return (metric.get('verification_method')=='pdf_cell_candidate' and proof.get('contract')==CONTRACT
                    and metric.get('fact_id') and all(evidence.get(k) for k in ('source_sha256','value','label','period','unit','entity_evidence')))
        metrics = {metric['metric']:metric for metric in proof.get('metrics',[])
                   if metric.get('verified') and metric.get('scope')=='cumulative'
                   and certified_method(metric)}
        if any(finite(metrics.get(name,{}).get('standard_value')) is None for name in values):
            missing_metrics=[name for name in values if finite(metrics.get(name,{}).get('standard_value')) is None]
            result['ttm_cashflow_evidence'].update(reason=period+'缺少已核实的'+','.join(missing_metrics)+'；原表空白不能按零处理',
                missing_metrics=[{'period':period,'metric':name,'source_issues':[i for i in proof.get('statement_issues',[]) if i.get('metric')==name]} for name in missing_metrics])
            return result
        for name in values:
            values[name].append(metrics[name]['standard_value'])
        result['ttm_cashflow_evidence']['source_documents'].append({
            'document_id':doc['id'],'period':period,'sha256':doc['sha256'],'source_url':doc['source_url'],'published_at':doc['published_at'],
            'pages':{name:metrics[name]['source_page'] for name in values}})
    totals = {name:rows[0] if len(rows)==1 else rows[0]+rows[1]-rows[2] for name,rows in values.items()}
    if totals['capex'] < 0:
        return result  # Negative cash paid needs review, never silently reverse the sign.
    result.update(verified_ttm_cashflow=totals['operating_cashflow'],verified_ttm_capex=totals['capex'])
    result['ttm_cashflow_evidence'].update(status='verified',formula='年度值' if len(required)==1 else '本期累计+上年全年−上年同期累计')
    return result
