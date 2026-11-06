"""Printed YOY extensions for already certified summary amounts; no AI calls.

Independent from the primary amount cache: fixing growth must not force a full
PDF rescan or change immutable original report payloads.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import re

from . import core
from .financial_evidence import normalize,number

VERSION='printed-summary-growth-v1'
IMPLEMENTATION=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
METRICS={'revenue','net_profit','adjusted_profit','operating_cashflow','eps'}


def _contains(cell,box):
    return cell and cell[0]-.8<=box[0] and box[2]<=cell[2]+.8 and cell[1]-.8<=box[1] and box[3]<=cell[3]+.8


def certify(metric,tables,sha):
    evidence=metric.get('evidence') or {};value=evidence.get('value') or {};period=evidence.get('period') or {}
    if (not metric.get('verified') or not metric.get('fact_id') or metric.get('metric') not in METRICS
            or metric.get('scope') not in ('cumulative','single_quarter') or evidence.get('statement')!='summary'
            or evidence.get('source_sha256')!=sha or period.get('scope')!=metric.get('scope')):return None
    box=value.get('bbox');page=value.get('page')
    if not box or len(box)!=4 or page!=metric.get('source_page'):return None
    for table in tables:
        if period.get('table_bbox') and any(abs(a-b)>1 for a,b in zip(table['bbox'],period['table_bbox'])):continue
        rows=table['rows']
        for row_index,row in enumerate(rows):
            selected=[c for c in row if _contains(c['bbox'],box) and number(c['text'])==metric.get('value')]
            if len(selected)!=1:continue
            current=selected[0]
            def header(cell):
                pieces=[c for before in rows[:row_index] for c in before if c['bbox']
                        and min(c['bbox'][2],cell['bbox'][2])-max(c['bbox'][0],cell['bbox'][0])>.6*(cell['bbox'][2]-cell['bbox'][0])]
                return normalize(''.join(c['text'] or '' for c in pieces)),pieces
            prior=[];growth=[]
            for cell in row:
                if not cell['bbox'] or cell['bbox'][0]<current['bbox'][2]-.8:continue
                label,anchors=header(cell)
                if '上年' in label and any(w in label for w in ('增减','同比','增长')) and '%' in normalize(cell['text']):growth.append((cell,anchors))
                elif '上年' in label and not any(w in label for w in ('增减','同比','增长')):prior.append((cell,anchors))
            if len(prior)!=1 or len(growth)!=1:continue
            previous=number(prior[0][0]['text']);printed=number(growth[0][0]['text']);amount=metric['value']
            if previous is None or previous<=0 or printed is None:continue
            text=normalize(growth[0][0]['text']).rstrip('%');digits=len(text.partition('.')[2])
            calculated=(amount/previous-1)*100
            if abs(calculated-printed)>.5*10**(-digits)+.000001:continue
            return {'yoy_pct':printed,'yoy_verified':True,'yoy_verification_method':'printed_summary_column_and_prior_arithmetic',
                    'yoy_evidence':{'source_sha256':sha,'page':page,'scope':metric['scope'],'current_fact_id':metric['fact_id'],
                                    'prior':prior[0][0],'prior_headers':prior[0][1],'growth':growth[0][0],'growth_headers':growth[0][1],
                                    'calculated_pct':calculated,'printed_rounding_tolerance':.5*10**(-digits)},
                    'yoy_fact_id':core.digest([sha,metric['fact_id'],prior[0],growth[0]])[:24]}
    return None


def enrich(doc,extraction):
    data=deepcopy(extraction);metrics=data.get('metrics') or []
    targets=[m for m in metrics if m.get('yoy_pct') is None and m.get('verified') and m.get('metric') in METRICS
             and (m.get('evidence') or {}).get('statement')=='summary']
    if not targets:return data
    path=(core.ROOT/str(doc.get('pdf') or '')).resolve();sha=doc.get('sha256')
    if not path.is_relative_to(core.ROOT.resolve()) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:
        data['summary_growth_check']={'state':'source_unavailable','verified':0};return data
    if not re.fullmatch(r'\d{6}',str(doc.get('code',''))) or doc.get('market') not in ('SH','SZ','BJ'):return data
    fingerprint=core.digest([sha,IMPLEMENTATION,[m.get('fact_id') for m in targets]])
    saved=core.VAULT/'30_公司'/doc['market']/doc['code']/'财报事实'/('同比-'+fingerprint+'.json')
    try:
        import json
        changes=json.loads(saved.read_text())['changes'] if saved.is_file() else None
        if changes is None:
            import pdfplumber
            changes={};pages={}
            with pdfplumber.open(path) as pdf:
                for m in targets:
                    n=m.get('source_page')
                    if not isinstance(n,int) or not 1<=n<=len(pdf.pages):continue
                    if n not in pages:
                        pages[n]=[{'bbox':list(t.bbox),'rows':[[{'bbox':list(c) if c else None,'text':text or ''} for c,text in zip(row.cells,values)]
                                   for row,values in zip(t.rows,t.extract())]} for t in pdf.pages[n-1].find_tables()]
                    proof=certify(m,pages[n],sha)
                    if proof:changes[m['fact_id']]=proof
            core.atomic_json(saved,{'version':VERSION,'checked_at':core.stamp(),'source_sha256':sha,'changes':changes})
        for m in targets:m.update(changes.get(m['fact_id'],{}))
        data['summary_growth_check']={'version':VERSION,'state':'checked','verified':len(changes),'candidates':len(targets),'checked_at':core.stamp()}
    except (OSError,ValueError,KeyError):
        data['summary_growth_check']={'version':VERSION,'state':'local_proof_unavailable','verified':0}
    return data
