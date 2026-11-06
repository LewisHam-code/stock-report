"""Incremental, model-free projection of historical Obsidian research.

The original reports remain in the vault. This separate feed is consumed by
the report library, never by live recommendations or prediction tracking.
"""
from contextlib import contextmanager
import copy
import fcntl
import json
import re

from . import core


def folder():
    return core.VAULT/'00_基础资料'/'历史解读展示索引'


def _read():
    path=folder()/'索引.json'
    return json.loads(path.read_text()) if path.is_file() else {'items':{},'receipts':{},'sources':{},'revision':0}


@contextmanager
def _lock():
    folder().mkdir(parents=True,exist_ok=True)
    with (folder()/'展示.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(lock,fcntl.LOCK_UN)


def _clean(value):
    if isinstance(value,dict):
        return {k:_clean(v) for k,v in value.items() if not k.startswith('_') and k not in
                {'pdf','text_file','profile_id','path','key_env','request_body','requests'}}
    if isinstance(value,list):return [_clean(v) for v in value]
    return value


def _public(report,receipt,role):
    from .publication_market import attach
    from .financial_recheck import apply as apply_recheck
    report=attach(apply_recheck(report))
    fields=('id','document_id','version','code','company','market','industry','date','period','rating',
            'evidence_status','test_mode','topics','conclusion','score','score_status','score_coverage',
            'scoring','evidence','valuation','risks','source','document_provenance','financial_analysis',
            'decision','industry_evidence','industry_sources','industry_outlook','valuation_context',
            'catalysts','falsification','scenarios','model','provider','quote_at_analysis',
            'financial_recheck','original_conclusion','assessment_asof','market_at_analysis','market_at_publication','market_publication_context','generated_at','disclosed_at','data_gaps')
    detail=_clean({k:copy.deepcopy(report[k]) for k in fields if k in report})
    detail.update(report_kind='historical_foundation',formal_tracking_eligible=False,
                  review_role=role,review_state=(report.get('financial_recheck') or {}).get('review_state',receipt.get('acceptance','needs_review')))
    detail['historical_rating']=detail.get('rating')
    # A fact-building score is not an on-time investment recommendation.
    detail.update(rating='unrated',evidence_status='insufficient')
    detail['review_context']={'kind':'historical_foundation','formal_tracking_eligible':False,
                             'not_a_historical_prediction':True}
    return detail


def _apply(index,report,receipt,role):
    ident=str(report.get('id',''));docid=str(report.get('document_id',''))
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,160}',ident) or not re.fullmatch(r'[a-zA-Z0-9-]{1,100}',docid):
        raise ValueError('invalid historical report identity')
    detail=_public(report,receipt,role)
    # Write the detail before linking it from any index. Full prompts, raw
    # model responses and machine paths are never copied to the web directory.
    core.atomic_json(core.SITE/'report-details'/(ident+'.json'),detail)
    fields=('id','code','company','market','industry','date','period','rating','evidence_status','topics',
            'conclusion','source','test_mode','provider','model','generated_at','score','score_coverage',
            'score_status','report_kind','formal_tracking_eligible','review_role','review_state')
    item={k:detail[k] for k in fields if k in detail}
    item['period_label']=detail.get('period','').replace('FY','年报')
    item['detail_url']='./data/report-details/'+ident+'.json'
    scoring=detail.get('scoring') or {}
    item.update(score_range=scoring.get('possible_range'),score_kind=scoring.get('score_kind'),
                rating_blockers=(detail.get('decision') or {}).get('rating_blockers',[]))
    entry=index.setdefault('items',{}).setdefault(docid,{'versions':{}})
    entry['versions'][ident]={'id':ident,'model':detail.get('model'),'role':role,'detail_url':item['detail_url'],
                              'generated_at':detail.get('generated_at')}
    old=entry.get('preferred')
    # Retain the Flash and Pro files, with one row per underlying filing.
    if old is None or (role=='pro_review',str(item.get('generated_at',''))) >= (
            old.get('review_role')=='pro_review',str(old.get('generated_at',''))):
        entry['preferred']=item
    return docid


def _save(index):
    index['revision']=index.get('revision',0)+1
    index['generated_at']=core.stamp()
    rows=[]
    for entry in index['items'].values():
        versions=sorted(entry['versions'].values(),key=lambda v:(v['role']=='pro_review',v.get('generated_at') or ''))
        item={**entry['preferred'],'review_versions':versions}
        rows.append(item)
    rows.sort(key=lambda row:(row.get('date',''),row.get('generated_at',''),row['id']),reverse=True)
    feed={'generated_at':index['generated_at'],'revision':index['revision'],'reports':rows,
          'version_count':sum(len(entry['versions']) for entry in index['items'].values())}
    core.atomic_json(core.SITE/'historical-reports.json',feed)
    core.atomic_json(folder()/'索引.json',index)


def publish_receipt(receipt,role='flash_deep',receipt_key=None,signature=None):
    path=(core.VAULT/receipt['report_path']).resolve()
    if not path.is_relative_to(core.VAULT.resolve()) or not path.is_file():
        raise ValueError('historical report is outside the vault or missing')
    report=json.loads(path.read_text())
    if report.get('id')!=receipt.get('report_id') or report.get('document_id')!=receipt.get('document_id'):
        raise ValueError('historical receipt does not match its report')
    from .publication_market import capture
    capture(report)
    with _lock():
        index=_read()
        _apply(index,report,receipt,role)
        if receipt_key:index['receipts'][receipt_key]=signature
        _save(index)


def sync_receipts(source_folder,source_id,fingerprint):
    """Catch up a running older worker without restarting a paid request.

    A small source-progress fingerprint skips the receipt scan when unchanged;
    completed reports are read only once, not on every browser refresh.
    """
    with _lock():
        index=_read();site=core.SITE/'historical-reports.json'
        if index['sources'].get(source_id)==fingerprint and site.is_file():return
        changed=False
        for path in source_folder.glob('*.json'):
            key=source_id+'/'+path.name;stat=path.stat();signature=[stat.st_mtime_ns,stat.st_size]
            if index['receipts'].get(key)==signature:continue
            receipt=json.loads(path.read_text())
            if receipt.get('state')!='reviewed':continue
            report_path=(core.VAULT/receipt['report_path']).resolve()
            if not report_path.is_relative_to(core.VAULT.resolve()):raise ValueError('invalid historical path')
            report=json.loads(report_path.read_text())
            if report.get('id')!=receipt.get('report_id') or report.get('document_id')!=receipt.get('document_id'):
                raise ValueError('historical identity mismatch')
            _apply(index,report,receipt,'pro_review' if path.stem.endswith('-pro') else 'flash_deep')
            index['receipts'][key]=signature;changed=True
        index['sources'][source_id]=fingerprint
        if changed or not site.is_file():_save(index)
        else:core.atomic_json(folder()/'索引.json',index)


def feed(revision=None):
    path=core.SITE/'historical-reports.json'
    value=json.loads(path.read_text()) if path.is_file() else {'revision':0,'reports':[],'version_count':0}
    if revision is not None and str(value['revision'])==str(revision):
        return {'revision':value['revision'],'unchanged':True}
    return value
