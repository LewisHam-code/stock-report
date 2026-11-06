"""One financial-fact contract for extraction, scoring, publication and repair.

Candidates never become facts just because a model/parser set verified=True.
Only physically certified PDF values enter the trusted set. No AI calls.
"""
import copy
import json
import re
import hashlib
from pathlib import Path
from collections import defaultdict

from . import core
from .financial_evidence import VERSION, CORE_FIELDS, FACTORS, values_agree

CONTRACT='financial-facts-v1'
IMPLEMENTATION=hashlib.sha256(b''.join(Path(__file__).with_name(name).read_bytes() for name in ('financial_facts.py','financial_evidence.py','statement_evidence.py','pdf_fact_sources.py'))).hexdigest()
PROFILE_EXCLUSIONS={
    'general':{},
    'bank':{'gross_margin':'银行使用净息差等专用口径','inventory':'银行不以制造业存货作为通用核心指标',
            'receivables':'银行应核对贷款及信用风险口径','capex':'银行不采用制造业资本开支作为通用核心门槛'},
    'insurance':{'gross_margin':'保险使用保险服务结果等专用口径','inventory':'保险不采用制造业存货作为通用核心指标',
                 'receivables':'保险采用保险合同及保费应收专用口径','capex':'保险不采用制造业资本开支作为通用核心门槛'},
    'securities':{'gross_margin':'证券公司使用金融业务专用盈利口径','inventory':'证券公司金融资产不能作为制造业存货',
                  'receivables':'证券公司采用融资融券及结算专用口径','capex':'证券公司不采用制造业资本开支作为通用核心门槛'}}


def applicable_fields(profile='general'):
    return {k:v for k,v in CORE_FIELDS.items() if k not in PROFILE_EXCLUSIONS.get(profile,{})}


def artifact(doc):
    if (doc.get('market') not in ('SH','SZ','BJ') or not re.fullmatch(r'\d{6}',str(doc.get('code','')))
            or not re.fullmatch(r'[a-zA-Z0-9-]{1,100}',str(doc.get('id','')))
            or not re.fullmatch(r'[0-9a-f]{64}',str(doc.get('sha256','')))):return None
    return core.VAULT/'30_公司'/doc['market']/doc['code']/'财报事实'/(doc['period']+'-'+doc['id']+'-'+doc['sha256'][:16]+'-'+VERSION+'-'+IMPLEMENTATION[:12]+'.json')


def document_facts(store,doc,allow_refetch=False):
    identity={k:doc.get(k) for k in ('id','code','market','period','sha256')}
    source=(core.ROOT/str(doc.get('pdf') or '')).resolve()
    if doc.get('pdf') and source.is_relative_to(core.ROOT.resolve()) and source.is_file():
        if hashlib.sha256(source.read_bytes()).hexdigest()!=doc.get('sha256'):
            return {'metrics':[],'status':'source_identity_mismatch','contract':CONTRACT,'version':VERSION}
    key=VERSION+'-'+IMPLEMENTATION+'-'+core.digest(identity)
    cached=store.cached(key) if store else None
    if cached and cached.get('contract')==CONTRACT and cached.get('implementation_sha256')==IMPLEMENTATION:return cached
    target=artifact(doc)
    from .financial_evidence import _text_record
    _,record=_text_record(doc)
    saved=record.get('pdf_proof') if record else None
    if (isinstance(saved,dict) and saved.get('contract')==CONTRACT and saved.get('implementation_sha256')==IMPLEMENTATION
            and saved.get('identity')==identity and saved.get('version')==VERSION):
        return saved
    if target and target.is_file():
        value=json.loads(target.read_text())
        if value.get('version')==VERSION and value.get('identity')==identity and value.get('contract')==CONTRACT and value.get('implementation_sha256')==IMPLEMENTATION:
            if store:store.cache(key,value,hours=24*365)
            return value
    from .financial_evidence import _scan_pdf
    try:value=_scan_pdf(doc,allow_refetch)
    except (OSError,ValueError):value={'metrics':[],'status':'pdf_table_check_failed'}
    value.update(contract=CONTRACT,identity=identity,version=VERSION,implementation_sha256=IMPLEMENTATION)
    if value.get('status')=='checked':
        if target:core.atomic_json(target,value)
        if store:store.cache(key,value,hours=24*365)
    return value


def resolve_candidates(doc,certified,unresolved,issues,profile='general'):
    """Resolve like-for-like, certified rows. Do not vote on parser guesses."""
    grouped=defaultdict(list)
    for c in certified:
        if c.get('evidence') and c.get('verified'):grouped[(c['metric'],c['scope'])].append(copy.deepcopy(c))
    # A company's summary may report operating revenue, excluding a separate
    # interest/insurance addition in total operating income. Preserve both.
    key=('revenue','cumulative')
    summary=[c for c in grouped.get(key,[]) if c['evidence']['statement']=='summary']
    if summary and '营业总收入' not in summary[0]['evidence']['label']['text']:
        statement=[{**c,'metric':'revenue','revenue_basis':'operating_revenue'} for c in grouped.get(('operating_revenue','cumulative'),[])]
        if statement:grouped[key]=summary+statement
    metrics=[];conflicts=[]
    for key,rows in grouped.items():
        rows.sort(key=lambda c:(c['evidence']['statement']!='summary',c['source_page']))
        fact=rows[0];disagree=[r for r in rows[1:] if not values_agree(fact,r)]
        fact['supporting_evidence']=[r['evidence'] for r in rows[1:]]
        fact['fact_id']=core.digest([doc.get('sha256'),doc.get('period'),*key,fact['value'],fact['unit'],fact['evidence']])[:24]
        if disagree:
            conflicts.append({'metric':key[0],'scope':key[1],'kind':'source_conflict',
                              'reason':'相同期间、单位及合并口径的原文数值存在差异',
                              'pages':sorted({r['source_page'] for r in rows})})
            fact.update(verified=False,standard_value=None,verification_status='conflict',verification_reason='已定位的原文存在同口径差异，尚待复核')
        metrics.append(fact)
    by={(m['metric'],m['scope']):m for m in metrics}
    def instant(name):return by.get((name,'instant'),{}).get('standard_value')
    a,l,e=instant('total_assets'),instant('total_liabilities'),instant('total_equity');checks=[]
    if all(v is not None for v in (a,l,e)):
        tolerance=max(.05,sum(by.get((name,'instant'),{}).get('rounding_tolerance',0) for name in ('total_assets','total_liabilities','total_equity')))
        checks.append({'name':'资产=负债+所有者权益','passed':abs(a-l-e)<=tolerance,'difference':round(a-l-e,4),'tolerance':tolerance})
    rev=by.get(('operating_revenue','cumulative')) or by.get(('revenue','cumulative'))
    cost=by.get(('operating_cost','cumulative'))
    if rev and cost and rev.get('verified') and cost.get('verified') and rev['standard_value']>0:
        value=(rev['standard_value']-cost['standard_value'])/rev['standard_value']*100
        metrics.append(dict(metric='gross_margin',value=value,standard_value=value,scope='cumulative',unit='percentage',
                            period_end=rev['period_end'],verified=True,source_page=rev['source_page'],
                            quote=rev['quote']+'；'+cost['quote'],verification_status='verified',
                            verification_method='derived_from_verified',verification_reason='由已核实的同口径收入、成本计算',
                            formula='(合并营业收入−合并营业成本)/合并营业收入×100',
                            inputs=[rev,cost],fact_id=core.digest([rev['fact_id'],cost['fact_id'],'gross_margin'])[:24]))
    return {'metrics':metrics,'candidates':unresolved,'status':'checked','statement_issues':issues,'profile':profile,
            'conflicts':conflicts,'checks':checks,'contract':CONTRACT}


def reconcile(doc,extraction,proof):
    data=copy.deepcopy(extraction);original=copy.deepcopy(extraction.get('metrics',[]))
    facts={(m['metric'],m['scope']):copy.deepcopy(m) for m in proof.get('metrics',[])}
    candidates=[]
    for m in original:
        key=(m['metric'],m['scope']);fact=facts.get(key)
        # Text presence alone proves neither the year column nor the entity.
        candidate={**m,'verified':False,'standard_value':None,'verification_status':'candidate_only'}
        candidates.append(candidate)
        if fact:
            prior=core.finite(m.get('original_model_value',m.get('value')))
            unit=m.get('original_model_unit',m.get('unit'))
            standard=prior*FACTORS.get(unit,1) if prior is not None else None
            fact.update(original_model_value=prior,original_model_unit=unit,
                        original_model_quote=m.get('original_model_quote',m.get('quote')),
                        original_verified=m.get('original_verified',m.get('verified',False)))
            fact['repair_kind']='source_conflict' if not fact.get('verified') else (
                'verification_repair' if standard is not None and abs(standard-fact['standard_value'])<=max(.01,abs(fact['standard_value'])*1e-10)
                else 'source_value_correction')
    data['metrics']=[m for m in candidates if (m['metric'],m['scope']) not in facts]+list(facts.values())
    for m in data['metrics']:
        if not m.get('verified'):
            m['standard_value']=None
            m.setdefault('verification_reason','提取候选值尚缺完整的原文行、期间列、单位或合并口径证明')
    profile=proof.get('profile','general');exclusions=PROFILE_EXCLUSIONS.get(profile,{})
    applicable=applicable_fields(profile)
    available={(m['metric'],m['scope']) for m in data['metrics'] if m.get('verified') and core.finite(m.get('standard_value')) is not None}
    missing=[]
    for name,scope in applicable.items():
        if (name,scope) in available:continue
        observed=[i for i in proof.get('statement_issues',[]) if i['metric']==name and i.get('scope',scope)==scope]
        blank=bool(observed and all(i.get('source_status')=='blank' for i in observed))
        not_applicable=any(i.get('source_status')=='not_applicable' for i in observed)
        missing.append({'metric':name,'scope':scope,'status':'source_not_applicable' if not_applicable else 'source_blank' if blank else 'parser_unresolved',
                        'reason':'原文对应报告期明确标注不适用，未填零或用其他期间替代' if not_applicable else '已定位原表空白，不能按零处理' if blank else '完整原文证据尚待定位；不能据此认定未披露或模型提取错误',
                        'source_pages':sorted({i['source_page'] for i in observed})})
    failed=[c for c in proof.get('checks',[]) if not c['passed']]
    critical={'revenue':'cumulative','net_profit':'cumulative','operating_cashflow':'cumulative'}
    critical_missing=[name for name,scope in critical.items() if (name,scope) not in available]
    # If the local table adapter found a YTD value for these summary fields,
    # a failed physical proof is a parser issue to repair before another paid
    # narrative/publication. It is not an undisclosed/missing company value.
    summary_unresolved=[name for name in ('adjusted_profit','roe','eps')
                        if (name,'cumulative') not in available and any(
                            c.get('metric')==name and c.get('scope')=='cumulative'
                            for c in proof.get('candidates',[]))]
    blocked=bool(summary_unresolved) or proof.get('status')!='checked' or bool(proof.get('conflicts')) or bool(failed) or bool(critical_missing)
    data['fact_contract']=CONTRACT
    data['candidate_metrics']=candidates
    data['primary_evidence']={k:v for k,v in proof.items() if k not in ('metrics','candidates')}
    data['data_quality']={'expected_count':len(applicable),'verified_count':sum(k in available for k in applicable.items()),
                          'applicable_fields':applicable,'profile':profile,
                          'not_applicable':[{'metric':k,'reason':v} for k,v in exclusions.items()],
                          'industry_note':'金融企业另需专用行业指标，通用指标齐备不代表行业分析完整' if profile!='general' else '',
                          'missing':missing,'conflicts':proof.get('conflicts',[]),'checks':proof.get('checks',[]),'failed_checks':failed,
                          'parser_status':proof.get('status'),'checked_at':proof.get('checked_at'),
                          'critical_missing':critical_missing,'summary_unresolved':summary_unresolved,'analysis_ready':not blocked,'status':'review' if blocked or missing else 'complete',
                          'issue_kind':'parser_or_source' if blocked else 'evidence_missing' if missing else None,
                          'note':'仅对适用指标的原文数值核验，不代表审计或全文观点均已验证'}
    return data


def certified_metrics(extraction):
    """Individual proven fields for display; this never authorizes analysis."""
    if extraction.get('fact_contract')==CONTRACT:
        return [m for m in extraction.get('metrics',[]) if m.get('verified') and m.get('fact_id') and core.finite(m.get('standard_value')) is not None]
    return [m for m in extraction.get('metrics',[]) if m.get('verified') and core.finite(m.get('standard_value')) is not None]


def trusted_metrics(extraction):
    if extraction.get('fact_contract')==CONTRACT and not (extraction.get('data_quality') or {}).get('analysis_ready'):
        return []
    return certified_metrics(extraction)


def require_ready(extraction):
    if extraction.get('fact_contract')==CONTRACT and not (extraction.get('data_quality') or {}).get('analysis_ready'):
        raise core.ServiceError('financial_proof_pending','原文数值、期间或报表口径尚待本地核验，已停止后续模型解读；提取结果保留')
