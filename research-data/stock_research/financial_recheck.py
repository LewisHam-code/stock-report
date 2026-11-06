"""Versioned local corrections. Keep paid outputs immutable and never call AI."""
import copy
import json
import re

from . import core
from .financial_evidence import VERSION, enrich_extraction, primary_metrics
from .financial_facts import IMPLEMENTATION


def path(report):
    if (report.get('market') not in ('SH','SZ','BJ') or not re.fullmatch(r'\d{6}',str(report.get('code','')))
            or not re.fullmatch(r'[a-zA-Z0-9-]{1,160}',str(report.get('id','')))):
        raise ValueError('invalid report identity')
    return core.VAULT/'30_公司'/report['market']/report['code']/'原文复核'/(report['id']+'-'+VERSION+'-'+IMPLEMENTATION[:12]+'.json')


def apply(report):
    target=path(report)
    if not target.is_file():return report
    record=json.loads(target.read_text())
    if record.get('source_digest')!=core.digest(report) or record.get('version')!=VERSION:return report
    result={**report,**copy.deepcopy(record['updates'])}
    return result


def recheck(report,document,store=None,force=False):
    target=path(report)
    if target.is_file() and not force:
        prior=json.loads(target.read_text())
        if prior.get('source_digest')==core.digest(report) and prior.get('version')==VERSION:return prior
    old=report['financial_analysis'];source=copy.deepcopy(old)
    # Withdraw old program proofs before rechecking. A missing new proof must
    # never leave a known obsolete parser's value labelled verified.
    for metric in source.get('metrics',[]):
        if metric.get('verification_method') in ('pdf_table_column','pdf_statement_column','derived_from_verified'):
            metric.update(verified=False,standard_value=None,verification_status='obsolete_parser',
                          verification_reason='旧版程序校验已失效，等待新版原文核对')
    fixed=enrich_extraction(store,document,source)
    for metric in fixed.get('metrics',[]):
        prior=next((m for m in old.get('metrics',[]) if (m['metric'],m['scope'])==(metric['metric'],metric['scope'])),{})
        # These are old published/program values, not necessarily raw model
        # outputs. Never relabel this comparison as a model extraction error.
        metric['prior_published_value']=prior.get('standard_value')
        if 'original_model_value' in prior:
            for key in ('original_model_value','original_model_unit','original_model_quote','original_verified'):
                if key in prior:metric[key]=prior[key]
        else:
            for key in ('original_model_value','original_model_unit','original_model_quote','original_verified'):metric.pop(key,None)
    new={(m['metric'],m['scope']):m for m in fixed['metrics']}
    changes=[]
    for m in old.get('metrics',[]):
        if not m.get('verified') or core.finite(m.get('standard_value')) is None:continue
        value=new.get((m['metric'],m['scope']),{});before=m['standard_value'];after=value.get('standard_value')
        if not value.get('verified') or after is None or abs(before-after)>max(.01,abs(before)*1e-8):
            changes.append({'metric':m['metric'],'scope':m['scope'],'before':before,'after':after,
                            'verified':bool(value.get('verified'))})
    updates={'financial_analysis':fixed,'assessment_asof':core.stamp()}
    generation=report.get('_generation_evidence') or {}
    from .market_metrics import refresh_market_financials
    snapshot=generation.get('snapshot') or {}
    updates['market_at_analysis']=refresh_market_financials(
        report.get('market_at_analysis'),report.get('quote_at_analysis') or (report.get('decision') or {}).get('quote'),fixed,
        (snapshot.get('financial') or {}).get('history',[]),report.get('generated_at'),
        (snapshot.get('financial') or {}).get('source'))
    if isinstance(generation.get('snapshot'),dict):
        from .strategy import evaluate
        industry={'outlook':report.get('industry_outlook'),'valuation_context':report.get('valuation_context'),
                  'verified_sources':report.get('industry_evidence',[])}
        decision=evaluate(document,fixed,generation['snapshot'],industry)
        updates.update(decision=decision,score=decision['scoring']['score'],score_status=decision['scoring']['status'],
                       score_coverage=decision['scoring']['coverage_pct'],scoring=decision['scoring'])
    quality=fixed['data_quality']
    withdrawn=[c for c in changes if c.get('after') is None]
    stale=any(c.get('after') is not None and c.get('verified') for c in changes)
    state='needs_review' if stale or quality['status']!='complete' else 'accepted'
    context={'parser_version':VERSION,'checked_at':core.stamp(),'numeric_changes':changes,'withdrawn_verifications':withdrawn,
             'narrative_status':'requires_review' if stale else 'original_retained','review_state':state,
             'note':'财务指标及规则评分已用原文重新核对；模型原始输出保留，未重新调用模型。'}
    updates.update(financial_recheck=context,review_state=state)
    if stale:
        updates['conclusion']='原文财务指标已更正；旧解读正文涉及修正数据，暂待复核，请先查看更正后的指标与核对记录。'
        updates['original_conclusion']=report.get('conclusion','')
    record={'schema':1,'version':VERSION,'source_digest':core.digest(report),'report_id':report['id'],
            'checked_at':context['checked_at'],'updates':updates}
    core.atomic_json(target,record)
    return record
