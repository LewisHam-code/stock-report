"""Column-aware checks against original PDF summary tables, without AI calls."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from io import BytesIO
from urllib.parse import urlparse

from .core import ROOT, atomic_json, finite, stamp

VERSION = "pdf-financial-evidence-v15"
FACTORS = {"yuan": 1, "thousand_yuan": 1000, "ten_thousand_yuan": 10000, "million_yuan": 1000000, "hundred_million_yuan": 100000000, "percentage": 1, "ratio": 1}
MONEY_UNITS = {"元": "yuan", "千元": "thousand_yuan", "万元": "ten_thousand_yuan", "百万元": "million_yuan", "亿元": "hundred_million_yuan"}
CORE_FIELDS = {'revenue':'cumulative','net_profit':'cumulative','adjusted_profit':'cumulative','operating_cashflow':'cumulative',
               'capex':'cumulative','roe':'cumulative','gross_margin':'cumulative','eps':'cumulative','total_assets':'instant',
               'total_liabilities':'instant','cash':'instant','receivables':'instant','inventory':'instant'}


def normalize(value):
    return re.sub(r"\s+", "", unicodedata.normalize('NFKC',str(value if value is not None else ''))).replace("−", "-").replace('﹣','-')


def number(value):
    raw=unicodedata.normalize('NFKC',str(value if value is not None else ''))
    if re.search(r'\d\s+\d',raw):return None
    text = normalize(raw)
    if text.startswith('(') and text.endswith(')'):
        text='-'+text[1:-1]
    # Commas must form complete thousands groups. A sliced/concatenated PDF
    # cell such as 03,342,043.542,0 is not a number to repair by deletion.
    if not re.fullmatch(r"-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?%?", text):
        return None
    return finite(text.replace(',','').rstrip('%'))


def rounding_tolerance(printed,unit):
    text=normalize(printed).replace(',','').rstrip('%').strip('()')
    if number(printed) is None:return 0
    places=len(text.rsplit('.',1)[1]) if '.' in text else 0
    return .5*10**(-places)*FACTORS.get(unit,1)


def values_agree(left,right):
    tolerance=max(.01,left.get('rounding_tolerance',0)+right.get('rounding_tolerance',.005)+.00001)
    return abs(left['standard_value']-right['standard_value'])<=tolerance


def metric_name(label):
    label = normalize(label)
    if '净利润率' in label:return None
    if '每股' in label and any(w in label for w in ('扣除非经常性损益','扣非')):return None
    if '每股' in label and not label.startswith(('基本每股收益','基本/稀释每股收益')):
        return None
    if "加权平均净资产收益率" in label or ("净资产收益率" in label and "加权平均" in label):
        return None if any(w in label for w in ('扣除非经常性损益','扣非')) else "roe"
    if label.startswith(("营业收入", "营业总收入")):
        return "revenue"
    if "扣除非经常性损益" in label and "净利润" in label:
        return "adjusted_profit"
    if "归属于" in label and "净利润" in label and "股东" in label:
        return "net_profit"
    if "经营活动产生的现金流量净额" in label:
        return "operating_cashflow"
    if label.startswith(('基本每股收益','基本/稀释每股收益')):
        return "eps"
    if label.startswith(("总资产","资产总计","资产总额")):
        return "total_assets"
    return None


def dated_summary_scopes(value,period):
    """Printed full date intervals, used by dual-listed quarterly reports."""
    text=normalize(value);year=period[:4]
    years=re.findall(r'20\d{2}',text)
    if not years or any(y!=year for y in years):return set()
    end=year+'-'+{'FY':'12-31','H1':'06-30','Q1':'03-31','Q3':'09-30'}[period[4:]]
    result=set()
    match=re.search(r'由?(20\d{2})年(\d{1,2})月(\d{1,2})日至(20\d{2})年(\d{1,2})月(\d{1,2})日期间',text)
    if match:
        start=f'{match[1]}-{int(match[2]):02}-{int(match[3]):02}'
        stop=f'{match[4]}-{int(match[5]):02}-{int(match[6]):02}'
        if stop==end:
            if start==year+'-01-01':result.add('cumulative')
            elif period.endswith('Q3') and start==year+'-07-01':result.add('single_quarter')
    point=re.search(r'于(20\d{2})年(\d{1,2})月(\d{1,2})日',text)
    if point and f'{point[1]}-{int(point[2]):02}-{int(point[3]):02}'==end:result.add('instant')
    return result


def header_columns(row, period):
    columns = {}
    for index, value in enumerate(row):
        text = normalize(value)
        if not text or any(s in text for s in ("比上", "增减", "变动", "同期")):
            continue
        dated=dated_summary_scopes(text,period)
        if dated:
            columns[index]='cumulative' if 'cumulative' in dated else 'single_quarter' if 'single_quarter' in dated else 'instant'
            continue
        if "年初至报告期末" in text:
            columns[index] = "cumulative"
        elif text in ("本报告期末", "报告期末", "期末余额", "期末数"):
            columns[index] = "instant"
        elif text==period[:4]+'年末':
            columns[index]='instant'
        elif text in ("本报告期", "本期", "本年度"):
            columns[index] = "single_quarter" if period.endswith("Q3") else "cumulative"
        elif period.endswith("FY") and re.fullmatch(period[:4]+r"年?(?:度)?(?:\(调整后\))?",text):
            columns[index] = "cumulative"
    return columns


def aligned_summary_tables(page, period):
    """Recover broken PDF cell grids using the printed period columns.

    Some vector PDFs omit border cells or split one printed column into three
    phantom columns. Only a summary with both explicit quarter and YTD headers
    is eligible; numeric body cell geometry must independently prove the bands.
    """
    recovered = []
    for table in page.find_tables():
        raw = table.extract()
        words = page.crop(table.bbox).extract_words()
        def read_band(left, top, right, bottom):
            selected = [w for w in words if left <= (w['x0']+w['x1'])/2 < right
                        and top <= (w['top']+w['bottom'])/2 < bottom]
            selected.sort(key=lambda w: (round(w['top']/3), w['x0']))
            return ''.join(w['text'] for w in selected)
        # Missing border cells can omit an entire label or growth column.
        filled = copy.deepcopy(raw)
        for row, data in zip(table.rows, filled):
            cells = [c for c in row.cells if c]
            if not cells:
                continue
            top, bottom = min(c[1] for c in cells), max(c[3] for c in cells)
            if data[0] is None:
                data[0] = read_band(table.bbox[0], top, min(c[0] for c in cells), bottom)
            if data[-1] is None:
                data[-1] = read_band(max(c[2] for c in cells), top, table.bbox[2], bottom)
        recovered.append(filled)
        header = next(((i, header_columns(row, period)) for i, row in enumerate(raw)
                       if set(header_columns(row, period).values()) >= {"single_quarter", "cumulative"}), None)
        if not header:
            continue
        index, scopes = header
        bands = {}
        for column, scope in scopes.items():
            box = table.rows[index].cells[column]
            if not box:
                continue
            center = (box[0] + box[2]) / 2
            candidates = [cell for row, data in zip(table.rows, raw) for cell, text in zip(row.cells, data)
                          if cell and cell[0] < center < cell[2] and number(text) is not None
                          and cell[2] - cell[0] < (table.bbox[2] - table.bbox[0]) / 3]
            if candidates:
                bands[scope] = max(candidates, key=lambda b: b[2] - b[0])
        if not all(s in bands for s in ("single_quarter", "cumulative")):
            continue
        q, y = bands["single_quarter"], bands["cumulative"]
        xs = [table.bbox[0], q[0], q[2], y[0], y[2], table.bbox[2]]
        if any(b-a < 8 for a,b in zip(xs,xs[1:])):
            continue
        intervals = sorted({(cell[1], cell[3]) for row, data in zip(table.rows, raw)
                            for cell, text in zip(row.cells, data) if cell and number(text) is not None
                            and any(a < (cell[0]+cell[2])/2 < b for a,b in ((q[0],q[2]),(y[0],y[2])))})
        merged = []
        for top, bottom in intervals:
            if merged and top < merged[-1][1] - .1:
                merged[-1] = (merged[-1][0], max(bottom, merged[-1][1]))
            else:
                merged.append((top,bottom))
        ys = sorted({table.bbox[1], table.bbox[3]} | {v for pair in merged for v in pair})
        rows = []
        for top, bottom in zip(ys, ys[1:]):
            if bottom-top > .1:
                rows.append([read_band(left,top,right,bottom) for left,right in zip(xs,xs[1:])])
        recovered.append(rows)
    return recovered


def table_metrics(tables_by_page, period, units_by_page=None):
    result, previous_columns, previous_width, previous_headers = {}, {}, None, []
    end = period[:4] + "-" + {"FY": "12-31", "H1": "06-30", "Q1": "03-31", "Q3": "09-30"}[period[4:]]
    for page, tables in tables_by_page:
        page_start_columns,page_start_width,page_start_headers=dict(previous_columns),previous_width,list(previous_headers)
        for raw_table in tables:
            quarter_headers=''.join(normalize(v) for row in raw_table[:4] for v in row)
            if period.endswith('FY') and sum(q in quarter_headers for q in ('第一季度','第二季度','第三季度','第四季度'))>=2:
                # A quarterly breakdown may continue on the next page with no
                # repeated header. Never inherit annual columns into that grid.
                previous_columns,previous_width,previous_headers={},None,[]
                page_start_columns,page_start_width,page_start_headers={},None,[]
                continue
            # Some PDFs expose wrapped labels/header lines as separate sparse
            # rows. Merge text-only continuation cells, preserving column indices.
            table = []
            for raw_row in raw_table:
                row = list(raw_row)
                has_number = any(number(v) is not None for v in row)
                detected = header_columns(row, period)
                if (table and not has_number and not detected and len(row) == len(table[-1])
                        and not any(normalize(v) and number(table[-1][i]) is not None for i,v in enumerate(row))):
                    prior = table[-1]
                    for i, value in enumerate(row):
                        if value:
                            prior[i] = str(prior[i] or "") + str(value)
                else:
                    table.append(row)
            # Alternative extraction grids for one physical page must not
            # overwrite the previous page's continuation header for each other.
            continuation_start=bool(table and (metric_name(table[0][0]) or normalize(table[0][0]).startswith('稀释每股收益')))
            if continuation_start and len(table[0])==page_start_width:
                columns,headers=dict(page_start_columns),list(page_start_headers)
            else:
                columns = dict(previous_columns) if continuation_start and len(table[0]) == previous_width else {}
                headers = list(previous_headers) if columns else []
            for row in table:
                detected = header_columns(row, period)
                if detected:
                    columns = detected
                    previous_columns, previous_width = dict(columns), len(row)
                    headers = list(row)
                    previous_headers = list(row)
                    continue
                label = normalize("".join(str(v or "") for v in row[:min(columns)])) if columns else ""
                name = metric_name(label)
                if not name or not columns:
                    continue
                unit = "percentage" if name == "roe" else "yuan" if name == "eps" else None
                if unit is None:
                    match = re.search(r"\((亿元|百万元|万元|千元|元)\)", label)
                    unit = MONEY_UNITS.get(match.group(1)) if match else (units_by_page or {}).get(page)
                if not unit:
                    continue  # A monetary unit must be proved, never guessed.
                for index, scope in columns.items():
                    if index >= len(row) or (name == "total_assets") != (scope == "instant"):
                        continue
                    value = number(row[index])
                    if value is None:
                        continue
                    key = (name, scope)
                    if key in result:
                        continue  # First original summary table wins over later explanations.
                    result[key] = dict(metric=name, value=value, unit=unit, scope=scope, period_end=end,
                                       rounding_tolerance=rounding_tolerance(row[index],unit),
                                       source_page=page, quote=" | ".join(str(v or "").replace("\n", "") for v in row),
                                       verified=True, standard_value=value*FACTORS[unit], verification_method="pdf_table_column",
                                       source_column=index+1, column_scope=scope, verification_status="verified", verification_reason="指标行、报告期列、数值及单位已核对")
                    if name != "roe" and index+1 < len(row):
                        boundary = min((i for i in columns if i > index), default=len(row))
                        growth = [i for i in range(index+1,min(boundary,len(headers))) if any(w in normalize(headers[i]) for w in ("同比","增减","变动","增长"))]
                        yoy = number(row[growth[0]]) if growth else None
                        other_period = any(normalize(v) and not any(w in normalize(v) for w in ("同比","增减","变动","增长")) for v in headers[index+1:boundary])
                        if growth and yoy is None and not other_period:
                            yoy = next((number(v) for v in row[index+1:boundary] if normalize(v)),None)
                        # In these recognized summary tables a growth column follows
                        # each period column, except for balance-sheet end values.
                        if scope != "instant" and yoy is not None:
                            result[key]["yoy_pct"] = yoy
    return list(result.values())


def annual_continuation(previous,current,period,page_number):
    """Recover EPS/ROE from a cross-page annual table using proven cell bands."""
    if not period.endswith('FY'):return []
    bands=[]
    for table in previous.find_tables():
        rows=table.extract()
        for index,row in enumerate(rows):
            if not any('主要财务指标' in normalize(v) for v in row):continue
            for column,scope in header_columns(row,period).items():
                box=table.rows[index].cells[column]
                if scope=='cumulative' and box and box[1]>previous.height*.75:
                    bands.append((column,box))
    if len(bands)!=1:return []
    column,box=bands[0];words=current.extract_words();result=[]
    for word in words:
        label=normalize(word['text']);name=metric_name(label)
        if name not in ('roe','eps') or '扣除' in label or '稀释' in label:continue
        # Wrapped deducted ROE labels must not be mistaken for ordinary ROE.
        neighbors=[w for w in words if abs(w['top']-word['top'])<4 and w['x0']<box[0]]
        if any('扣除' in w['text'] for w in neighbors):continue
        values=[w for w in words if abs(w['top']-word['top'])<4 and box[0]<(w['x0']+w['x1'])/2<box[2] and number(w['text']) is not None]
        if len(values)!=1:continue
        value=number(values[0]['text']);unit='percentage' if name=='roe' else 'yuan'
        result.append(dict(metric=name,value=value,unit=unit,scope='cumulative',period_end=period[:4]+'-12-31',
            source_page=page_number,quote=word['text']+' | '+values[0]['text'],verified=True,standard_value=value,
            verification_method='pdf_table_column',source_column=column+1,column_scope='cumulative',verification_status='verified',
            corroboration_page=page_number-1,verification_reason='前页年度列单元格边界与本页数值坐标匹配，未按首个数字猜测'))
    return result


def annual_summary_unit_continues(previous_text,current_text,period):
    if not period.endswith('FY'):return False
    previous_text=normalize(previous_text)[-1200:];current_text=normalize(current_text)[:1500]
    return ('主要会计数据' in previous_text and '单位' in previous_text
            and '主要会计数据' in current_text and period[:4]+'年' in current_text)


def _text_record(doc):
    relative = doc.get("text_file")
    if not relative:
        return None, None
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, None
    if data.get("sha256") != doc.get("sha256") or not isinstance(data.get("pages"), list) or not data["pages"]:
        return None, None
    return path, data


def summary_page_tables(page,period,continuation=None):
    """Read one original grid and carry its column geometry across a page."""
    from .statement_evidence import reconstructed_table
    canonical=[];next_continuation=None
    for table in page.find_tables():
        inherit=(continuation if period.endswith('FY') and table.bbox[1]<page.height*.2 else None)
        rows,centers=reconstructed_table(page,table,period,inherit['centers'] if inherit else None)
        if (inherit and len(rows[0])==len(inherit['header']) and centers
                and len(centers)==len(inherit['centers'])
                and all(abs(a-b)<3 for a,b in zip(centers,inherit['centers']))
                and not any(header_columns(r,period) for r in rows[:2])):
            # The same physical annual grid crosses a page boundary.
            # Complete the previous label, preserving its amount/page,
            # then carry the independently printed header to this page.
            tail=normalize(rows[0][0])
            if tail.startswith(('的净利润','流量净额','率','股)')) and not any(number(v) is not None for v in rows[0][1:]):
                prior_row=inherit['rows'][-1]
                prior_row[0]=str(prior_row[0] or '')+str(rows.pop(0)[0] or '')
            rows.insert(0,list(inherit['header']))
        canonical.append(rows)
        headers=[r for r in rows if header_columns(r,period)]
        if (headers and centers and table.bbox[3]>page.height*.8
                and any(metric_name(r[0]) or '归属于上市公司股东' in normalize(r[0]) for r in rows)):
            next_continuation={'centers':centers,'header':headers[-1],'rows':rows}
    return canonical,next_continuation


def primary_metrics(store, doc, allow_refetch=False):
    from .financial_facts import document_facts
    return document_facts(store,doc,allow_refetch=allow_refetch)


def _scan_pdf(doc,allow_refetch=False):
    """Private candidate adapter; returned facts require physical certification."""
    record_path,record=_text_record(doc)
    path=(ROOT/str(doc.get('pdf') or '')).resolve()
    source=path if doc.get('pdf') and path.is_relative_to(ROOT.resolve()) and path.is_file() else None
    if source is None and allow_refetch:
        from .disclosure_sources import official_pdf
        if not official_pdf(doc.get('source_url','')):return {'metrics':[],'status':'original_unavailable'}
        from .sources import fetch_document_pdf
        raw=fetch_document_pdf(doc,None)
        source=BytesIO(raw)
    if source is None:return {'metrics':[],'status':'original_unavailable'}
    actual=hashlib.sha256(source.getvalue() if isinstance(source,BytesIO) else source.read_bytes()).hexdigest()
    if actual!=doc.get('sha256'):return {'metrics':[],'status':'source_identity_mismatch'}
    import pdfplumber
    from .statement_evidence import statement_metrics,_heading
    from .pdf_fact_sources import SourceIndex
    tables,units,canonical_tables=[],{},[]
    prior_text='';inherited_units=set();continuation=None
    with pdfplumber.open(source) as pdf:
        page_count=len(pdf.pages)
        text_pages=record['pages'] if record else [{'page':i+1,'text':p.extract_text() or ''} for i,p in enumerate(pdf.pages)]
        indexer=SourceIndex(pdf,doc,text_pages)
        base=12 if doc['period'].endswith(('FY','H1')) else 4
        summary_indices=set(range(min(base,page_count)))
        from .pdf_fact_sources import summary_heading
        for i,r in enumerate(text_pages[:50]):
            if any(summary_heading(normalize(line)) for line in r['text'].splitlines()):
                summary_indices.update(range(i,min(i+3,page_count)))
        for index in sorted(summary_indices):
            page=pdf.pages[index]
            text=normalize(page.extract_text() or '')
            unit=re.search(r'单位[:：](?:人民币)?(亿元|百万元|万元|千元|元)(?!/)',text)
            if unit:units[index+1]=MONEY_UNITS[unit[1]]
            elif index in units and annual_summary_unit_continues(prior_text,text,doc['period']):
                units[index+1]=units[index];inherited_units.add(index+1)
            canonical,continuation=summary_page_tables(page,doc['period'],continuation)
            canonical_tables.append((index+1,canonical))
            tables.append((index+1,aligned_summary_tables(page,doc['period'])+page.extract_tables()))
            prior_text=text
        # Text indexing avoids geometrically parsing hundreds of notes pages.
        # Include original statements, continuations and heading transitions.
        selected=[]
        for i,text in enumerate(text_pages,1):
            state=indexer.start_states.get(i,(None,False))[0]
            end_state=indexer.end_states.get(i,(None,False))[0]
            if state in ('balance','income','cashflow') or end_state in ('balance','income','cashflow') or any(_heading(line) for line in text.get('text','').splitlines()):
                selected.append((i,pdf.pages[i-1]))
        statement,issues,checks=statement_metrics(selected,doc['period'])
        summary=table_metrics(canonical_tables,doc['period'],units)
        known={(m['metric'],m['scope']) for m in summary}
        summary.extend(m for m in table_metrics(tables,doc['period'],units) if (m['metric'],m['scope']) not in known)
        continued=[]
        if doc['period'].endswith('FY'):
            for i in range(1,min(12,page_count)):
                continued.extend(annual_continuation(pdf.pages[i-1],pdf.pages[i],doc['period'],i+1))
        known={(m['metric'],m['scope']) for m in summary}
        summary.extend(m for m in continued if (m['metric'],m['scope']) not in known)
        issues.extend(indexer.not_applicable([i+1 for i in summary_indices]))
        facts=[];unresolved=[]
        for candidate in summary+statement:
            if candidate.get('verification_method')=='derived_from_verified':continue
            certified,error=indexer.certify(candidate)
            if certified:
                certified.pop('conflict_pages',None)
                certified['rounding_tolerance']=rounding_tolerance(certified['source_cell'],certified['unit'])
                facts.append(certified)
            else:
                unresolved.append({**candidate,'verified':False,'standard_value':None,
                                   'verification_status':'parser_unresolved','verification_reason':error})
        proved_keys={(m['metric'],m['scope']) for m in facts}
        discovered=indexer.discover([i+1 for i in summary_indices]+[n for n,_ in selected],exclude=proved_keys)
        facts.extend(m for m in discovered if (m['metric'],m['scope']) not in proved_keys)
        profile=indexer.industry_profile(selected)
    from .financial_facts import resolve_candidates
    result=resolve_candidates(doc,facts,unresolved,issues,profile)
    result.update(version=VERSION,checked_at=stamp(),pages_scanned=page_count)
    return result


def persist_primary_proof(store, doc):
    """Keep verified table evidence with page text before an original is removed."""
    path, record = _text_record(doc)
    if path is None:
        return None
    proof = primary_metrics(store, doc)
    if proof.get("status") != "checked":
        return None
    if record.get("pdf_proof") != proof:
        record["pdf_proof"] = proof
        atomic_json(path, record)
    return proof


def reusable_core_proof(proof, period):
    """Skip model retyping only when every core field has sound PDF evidence."""
    from .financial_facts import CONTRACT,PROFILE_EXCLUSIONS
    if not isinstance(proof, dict) or proof.get('contract')!=CONTRACT or proof.get('version') != VERSION or proof.get('status') != 'checked' or proof.get('conflicts'):
        return False
    if any(check.get('passed') is not True for check in proof.get('checks', [])):
        return False
    suffix = {'Q1': '03-31', 'H1': '06-30', 'Q3': '09-30', 'FY': '12-31'}.get(period[4:])
    if suffix is None:
        return False
    expected_end = period[:4] + '-' + suffix
    for name, scope in CORE_FIELDS.items():
        if name in PROFILE_EXCLUSIONS.get(proof.get('profile','general'),{}):continue
        rows = [m for m in proof.get('metrics', []) if m.get('metric') == name and m.get('scope') == scope]
        if len(rows) != 1:
            return False
        metric = rows[0]
        if (not metric.get('fact_id') or not (metric.get('evidence') or metric.get('verification_method')=='derived_from_verified' and all(m.get('fact_id') and m.get('evidence') for m in metric.get('inputs',[]))) or metric.get('verified') is not True or metric.get('period_end') != expected_end or
            finite(metric.get('standard_value')) is None or metric.get('unit') not in FACTORS or
            not isinstance(metric.get('source_page'), int) or metric['source_page'] <= 0 or not metric.get('quote')):
            return False
    return True


def enrich_extraction(store, doc, extraction, allow_refetch=False):
    from .financial_facts import reconcile
    try:
        proof=primary_metrics(store,doc,allow_refetch=allow_refetch)
    except Exception:
        proof={'metrics':[],'status':'pdf_table_check_failed'}
    return reconcile(doc,extraction,proof)
