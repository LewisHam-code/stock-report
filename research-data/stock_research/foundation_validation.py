"""Reconcile pilot outputs, original-model omissions, repairs and cost."""
from collections import Counter
import json
from . import core, foundation_budget as budget


def review_records(task):
    for result in task.get('results',{}).values():
        if result['state']!='reviewed':continue
        folder=core.VAULT/'30_公司'/result['market']/result['code']/'历史年报解读'
        records=[json.loads(p.read_text()) for p in folder.glob(result['period']+'-'+result['document_id']+'*.json')]
        records=[r for r in records if 'financial_analysis' in r and (not result.get('report_id') or r.get('id')==result['report_id'])]
        yield result,max(records,key=lambda r:r.get('generated_at','')) if records else None


def recheck_originals(document_ids=None):
    """Recheck all pilot PDFs locally; retain generated reports unchanged."""
    from concurrent.futures import ThreadPoolExecutor
    from .financial_evidence import VERSION,enrich_extraction
    def check(pair):
        result,item=pair
        if item is None:return {'status':'missing_output','document_id':result['document_id']}
        folder=core.VAULT/'30_公司'/result['market']/result['code']/'历史年报解读'/'原文复核'
        output=folder/(result['document_id']+'-'+item['version']+'-'+VERSION+'.json')
        if output.exists():return json.loads(output.read_text())
        text=next((core.ROOT/'text').glob(result['document_id']+'-*.json'),None)
        pdf=next((core.ROOT/'filings').glob('**/'+result['document_id']+'.pdf'),None)
        doc={'id':result['document_id'],'period':result['period'],'code':result['code'],'market':result['market'],
             'sha256':item['_generation_evidence']['source_sha256'],
             'text_file':str(text.relative_to(core.ROOT)) if text else None,
             'pdf':str(pdf.relative_to(core.ROOT)) if pdf else None}
        store=core.Store()
        try:
            checked=enrich_extraction(store,doc,item['financial_analysis'],allow_refetch=False)
            assessment={'status':'checked','document_id':doc['id'],'report_id':item['id'],'parser_version':VERSION,
                        'original_quality':item['financial_analysis'].get('data_quality'),
                        'quality':checked['data_quality'],'metrics':checked['metrics'],'asof':core.stamp(),
                        'basis':'仅重新核对本地原文，零模型调用；原始解读与文字结论保留，未重新生成'}
            core.atomic_json(output,assessment)
            return assessment
        finally:store.conn.close()
    pairs=list(review_records(budget.load()))
    selected=[p for p in pairs if document_ids is None or p[0]['document_id'] in document_ids]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(check,selected))
    values=[]
    for result,item in pairs:
        if item is None:continue
        folder=core.VAULT/'30_公司'/result['market']/result['code']/'历史年报解读'/'原文复核'
        records=[json.loads(p.read_text()) for p in folder.glob(result['document_id']+'-'+item['version']+'-*.json')]
        if records:values.append(max(records,key=lambda r:r['asof']))
    checked=[v for v in values if v['status']=='checked']
    summary={'asof':core.stamp(),'parser_version':VERSION,'reviewed':len(values),'checked':len(checked),
             'parser_versions':dict(Counter(v['parser_version'] for v in checked)),
             'core_complete':sum(v['quality']['status']=='complete' for v in checked),
             'core_verified':sum(v['quality']['verified_count'] for v in checked),
             'core_expected':sum(v['quality']['expected_count'] for v in checked),
             'source_conflicts':sum(len(v['quality']['conflicts']) for v in checked),
             'missing':dict(Counter(m['metric']+':'+m['status'] for v in checked for m in v['quality']['missing'])),
             'model_calls':0,'basis':'程序原文核对后的指标覆盖；不代表模型原始正确率、叙述无误或全文已读'}
    core.atomic_json(budget.folder()/'原文复核汇总.json',summary)
    return summary


def report():
    task=budget.load();results=[v for v in task.get('results',{}).values() if v['state']=='reviewed']
    counts=Counter();missing=Counter();total_pages=read_pages=0
    for result,item in review_records(task):
        if item is None:counts['missing_review_output']+=1;continue
        analysis=item['financial_analysis'];quality=analysis.get('data_quality') or {}
        counts['core_complete']+=quality.get('status')=='complete'
        counts['core_verified']+=quality.get('verified_count',0)
        counts['core_expected']+=quality.get('expected_count',0)
        counts['source_conflicts']+=len(quality.get('conflicts',[]))
        coverage=analysis.get('coverage') or {};total_pages+=coverage.get('total_pages',0);read_pages+=len(coverage.get('read_pages',[]))
        for entry in quality.get('missing',[]):missing[entry['metric']+':'+entry['status']]+=1
        for metric in analysis['metrics']:
            if not metric.get('verified') or metric.get('verification_method') not in ('pdf_table_column','pdf_statement_column','derived_from_verified'):continue
            counts['proof_metrics']+=1
            if 'original_model_value' not in metric:counts['model_omitted_then_program_added']+=1
            elif metric['original_model_value'] is None:counts['model_blank_then_program_filled']+=1
            elif metric.get('repair_kind')=='source_value_correction':counts['model_value_or_unit_corrected']+=1
            elif not metric.get('original_verified'):counts['model_citation_or_period_repaired']+=1
            else:counts['model_value_and_citation_retained']+=1
    calls=[c for c in budget.all_calls(task) if c.get('phase','pilot')=='pilot'];settled=[c for c in calls if c['state']!='reserved']
    estimated=sum(c.get('cost',c['maximum']) for c in settled)
    idle=sum(c.get('cost',c['maximum'])/c.get('multiplier',1) if c['state']=='usage_returned' else c['maximum'] for c in settled)
    count=len(results)
    value={'asof':core.stamp(),'reviewed':count,'attempted':len(task.get('results',{})),
           'failures':dict(Counter(r.get('error','unknown') for r in task.get('results',{}).values() if r['state']=='failed')),
           'model_calls':len(settled),'usage_returned_calls':sum(c['state']=='usage_returned' for c in settled),
           'input_tokens':sum(c.get('input_tokens',0) for c in settled),
           'output_tokens':sum(c.get('output_tokens',0) for c in settled),
           'cache_hit_tokens':sum(c.get('cache_hit_tokens',0) for c in settled),
           'boards':dict(Counter(r['board'] for r in results)),'periods':dict(Counter(r['period'] for r in results)),
           'counts':dict(counts),'missing':dict(missing),'total_pages':total_pages,'model_read_pages':read_pages,
           'estimated_spend_cny':estimated,'estimated_idle_equivalent_cny':idle,
           'projected_three_year_annual_cny':idle/count*5574*3 if count else None,
           'pricing_basis':'按供应商返回用量和请求时段估算，非账单；外推包含样本失败开销，不保证长短年报分布相同',
           'quality_basis':'修复后的核心指标覆盖，不是模型原始准确率、全文无遗漏证明或预测有效性证明'}
    core.atomic_json(budget.folder()/'年报验证汇总.json',value)
    text=('# 年报试读验证汇总\n\n'+f'已完成{count}份；估算费用{estimated:.2f}元；同用量闲时约{idle:.2f}元。\n\n'+
          f'原文核对后核心指标齐备：{counts["core_complete"]}/{count}份；指标{counts["core_verified"]}/{counts["core_expected"]}项。\n\n'+
          f'模型读取页数{read_pages}/{total_pages}；完整性需结合逐份覆盖记录。\n\n'+
          '原始提取、程序补齐和数值修复分别统计，不能把最终完整率写成模型准确率。\n\n'+core.dumps(value)+'\n')
    core.atomic_text(budget.folder()/'年报验证汇总-自动生成.md',text)
    return value
