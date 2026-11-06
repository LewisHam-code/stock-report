"""Small model-facing views; complete originals remain in the research archive."""
from copy import deepcopy


def financial_table_view(extracted):
    """Lossless verified values in a compact table; full proof stays local.

    Does not summarize business/audit prose or promote candidate numbers.
    Markdown is only a presentation of the physically verified fact contract.
    """
    from .financial_facts import CONTRACT
    if extracted.get('fact_contract')!=CONTRACT:return financial_view(extracted)
    data=financial_view(extracted)
    def cell(value):
        return str(value if value is not None else '').replace('\\','\\\\').replace('|','\\|').replace('\r',' ').replace('\n','<br>')
    verified=[];pending=[];derived=[]
    for m in extracted.get('metrics',[]):
        if not (m.get('verified') and m.get('fact_id')):
            pending.append({k:m[k] for k in ('metric','scope','period_end','verified','verification_status','verification_reason','source_page') if k in m})
            continue
        verified.append([m.get(k) for k in ('metric','scope','period_end','value','unit','source_page','quote')])
        if m.get('formula'):
            derived.append({'metric':m['metric'],'formula':m['formula'],
                            'inputs':[{k:v for k,v in i.items() if k in ('metric','scope','source_page')} for i in m.get('inputs',[])]})
    if verified:
        lines=['|metric|scope|period_end|原值|原单位|PDF页|原文引文|','|---|---|---|---:|---|---:|---|']
        lines.extend('|'+ '|'.join(cell(v) for v in row)+'|' for row in verified)
        data['verified_metrics_markdown']='\n'.join(lines)
        data['metric_table_basis']='已核实原文；value为原单位值；累计=cumulative，时点=instant，单季=single_quarter；derived_metrics按列明公式计算'
    data['metrics']=pending
    if derived:data['derived_metrics']=derived
    quality=data.get('data_quality') or {}
    # Success bookkeeping is not research evidence. Preserve missing fields,
    # conflicts, applicability and failed accounting checks without duplication.
    for key in ('checked_at','note','issue_kind'):
        quality.pop(key,None)
    if quality.get('parser_status')=='checked':quality.pop('parser_status')
    if quality.get('checks'):
        quality['checks']=[{k:v for k,v in check.items() if k in ('name','passed') or not check.get('passed')} for check in quality['checks']]
    if quality.get('applicable_fields'):
        quality.pop('applicable_fields')  # expected_count plus missing/not_applicable are sufficient
    coverage=data.get('coverage') or {}
    if coverage.get('omissions'):
        from collections import Counter
        counts=Counter(o.get('reason','未指定') for o in coverage['omissions'])
        coverage['omitted_blocks_by_reason']=dict(counts)
        coverage.pop('omissions')
    return data


def financial_view(extracted):
    data=deepcopy(extracted)
    for key in ('candidate_metrics','primary_evidence','fact_contract','summary_growth_check'):data.pop(key,None)
    def metric_view(metric):
        value={k:v for k,v in metric.items() if not k.startswith('original_model_') and k not in ('original_verified','repair_kind','source_layout','evidence','supporting_evidence','fact_id','prior_published_value','source_cell','yoy_evidence','yoy_fact_id','yoy_verified','yoy_verification_method')}
        # These two stock success messages repeat on every metric. Preserve all
        # failure/conflict explanations and any substantive custom explanation.
        if value.get('verified') and value.get('verification_reason') in ('页码、原文引文及数值已匹配','原文表格列已核实'):
            value.pop('verification_reason')
        if isinstance(value.get('inputs'),list):value['inputs']=[metric_view(m) for m in value['inputs']]
        if not metric.get('verified'):
            return {k:value[k] for k in ('metric','scope','period_end','verified','source_page','verification_status','verification_reason') if k in value}
        return value
    if isinstance(data.get('metrics'),list):data['metrics']=[metric_view(m) for m in data['metrics']]
    coverage=data.get('coverage') or {}
    pages=coverage.get('read_pages')
    if isinstance(pages,list) and pages and all(isinstance(n,int) for n in pages):
        ranges=[]
        for page in sorted(set(pages)):
            if ranges and page==ranges[-1][1]+1:ranges[-1][1]=page
            else:ranges.append([page,page])
        coverage.pop('read_pages')
        coverage['read_page_ranges']=ranges
        coverage['read_page_count']=len(set(pages))
    return data


def extraction_cache_key(doc,instructions,schema,prompt):
    from .core import digest
    # Rating/strategy changes do not change PDF facts. Reading instructions,
    # schema, source identity and exact prompt do; model routing remains separate.
    return 'extraction-v5-'+digest([doc['sha256'],instructions,schema,prompt])


def compatible_cached_request(request,instructions,schema,prompt):
    """Allow old caches only when their recorded actual inputs match exactly."""
    from .core import dumps
    if not isinstance(request,dict):return False
    messages=request.get('messages')
    if not isinstance(messages,list) or len(messages)!=2:return False
    actual=[(m.get('role'),m.get('content')) for m in messages]
    return actual==[('system',instructions+'\n必须输出一个合法JSON对象，严格符合此JSON Schema：'+dumps(schema)),('user',prompt)]


def relocate_unlocated_claims(data):
    """Keep explicit unknown pages as uncertainties, never invent page numbers.

    Only a narrow, otherwise valid optional claim can be relocated. All raw
    provider responses remain in the receipt; unrelated schema errors fail.
    """
    if not isinstance(data, dict) or not isinstance(data.get('management_claims'), list):
        return data, []
    if not isinstance(data.get('uncertainties'), list) or not all(isinstance(v, str) for v in data['uncertainties']):
        return data, []
    result = deepcopy(data);kept = [];changes = []
    expected = {'claim', 'source_page', 'quote', 'verification_needed'}
    for index, claim in enumerate(result['management_claims']):
        if (isinstance(claim, dict) and set(claim) == expected and claim['source_page'] is None and
            all(isinstance(claim[key], str) for key in expected - {'source_page'})):
            result['uncertainties'].append('管理层主张页码未知，未核实：' + claim['claim'] +
                                           '；引文待定位：' + claim['quote'] + '；待核实：' + claim['verification_needed'])
            changes.append('management_claims[' + str(index) + ']→uncertainties（未知页码）')
        else:
            kept.append(claim)
    result['management_claims'] = kept
    # Optional metrics without a source page are candidates, not evidence.
    # Keep their complete content as an uncertainty instead of fabricating a
    # page or rejecting every other usable part of the saved response.
    if isinstance(result.get('metrics'),list):
        from .analysis import METRIC,validate
        from .core import ServiceError,dumps
        kept=[]
        for index,metric in enumerate(result['metrics']):
            # A null value with unknown source metadata is an explicit missing
            # item, not a malformed numeric claim. Preserve it as uncertainty;
            # never invent scope, unit, page, date or quotation to make it fit.
            metadata=('unit','scope','period_end','source_page','quote')
            if isinstance(metric,dict) and metric.get('value','absent') is None and any(metric.get(k,'absent') is None for k in metadata):
                nullable=deepcopy(METRIC)
                for field in metadata:
                    original_type=nullable['properties'][field]['type']
                    types=original_type if isinstance(original_type,list) else [original_type]
                    nullable['properties'][field]['type']=list(dict.fromkeys(types+['null']))
                    if 'enum' in nullable['properties'][field]:nullable['properties'][field]['enum'].append(None)
                try:validate(metric,nullable)
                except ServiceError:pass
                else:
                    result['uncertainties'].append('指标值及原文定位资料不足，保留缺项，不参与评分：'+dumps(metric))
                    changes.append('metrics['+str(index)+']→uncertainties（空值元数据未知）')
                    continue
            if isinstance(metric,dict) and metric.get('source_page','absent') is None:
                try:validate({**metric,'source_page':1},METRIC)
                except ServiceError:pass
                else:
                    result['uncertainties'].append('指标页码未知，仅为未核实候选，不参与评分：'+dumps(metric))
                    changes.append('metrics['+str(index)+']→uncertainties（未知页码）')
                    continue
            kept.append(metric)
        result['metrics']=kept
    return result, changes
