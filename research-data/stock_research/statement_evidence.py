"""Read original consolidated statement columns; no AI or statistical filling."""
from __future__ import annotations

import re
from .core import finite


def _text(value):
    from .financial_evidence import normalize
    return normalize(value)


def period_column(text, period):
    """Only current-period headings; prior years and growth columns are excluded."""
    t = _text(text)
    if any(w in t for w in ('上年','上期','期初','比上','同比','增减','变动')):
        return False
    if any(w in t for w in ('期末余额','本期发生额','本年累计','本期金额')):
        return True
    if t.startswith(('本报告期末','报告期末','年初至报告期末','本报告期')):
        return not re.search(r'20\d{2}',t) or period[:4] in t
    year=period[:4]
    if not t.startswith(year):
        return False
    suffix={'Q1':'03-31','H1':'06-30','Q3':'09-30','FY':'12-31'}[period[4:]]
    match=re.search(r'(20\d{2})年(\d{1,2})月(\d{1,2})日',t) or re.search(r'(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d|月)',t)
    if match:
        return f'{match[1]}-{int(match[2]):02}-{int(match[3]):02}'==year+'-'+suffix
    months={'Q1':'3','H1':'6','Q3':'9','FY':'12'}[period[4:]]
    return bool(re.fullmatch(year+r'年?度?',t) and period.endswith('FY') or
                t.startswith(year+{'Q1':'年第一季度','H1':'年上半年','Q3':'年前三季度','FY':'年度'}[period[4:]]) or
                (period.endswith('H1') and t.startswith(year+'年半年度')) or
                re.search(r'1[-—至~～]0?'+months+r'月',t))


def _heading(text):
    t=_text(text)
    t=re.sub(r'^20\d{2}年(?:(?:\d{1,2}月\d{1,2}日)|度)?','',t)
    t=re.sub(r'[（(]续[）)]$','',t)
    t=re.sub(r'^(?:第?[一二三四五六七八九十\d]+[、.．]|\([一二三四五六七八九十\d]+\))','',t)
    for label,kind in (('所有者权益变动表','other'),('股东权益变动表','other'),('资产负债表','balance'),('现金流量表','cashflow'),('利润表','income')):
        if t.endswith(label) and (t.startswith('合并') or t.startswith('母公司')):
            return ('parent' if t.startswith('母公司') else kind)
    return None


def page_lines(page):
    return page.extract_text_lines(return_chars=False)


def numeric_runs(chars):
    """Keep complete printed tokens; never split an amount at a guessed column."""
    groups=[];current=[]
    for c in sorted(chars,key=lambda c:c['x0']):
        numeric=bool(re.fullmatch(r'[0-9０-９.,，．%％()（）−﹣+\-]+',c['text']))
        gap=c['x0']-current[-1]['x1'] if current else 0
        if not numeric or (current and gap>max(1.5,(current[-1]['x1']-current[-1]['x0'])*.55)):
            if current:groups.append(current);current=[]
        if numeric:current.append(c)
    if current:groups.append(current)
    return groups


def line_table(page,start,stop,period,prior=None):
    """An independent printed-date/character-column fallback for borderless PDFs."""
    layout=prior
    lines=page.extract_text_lines(return_chars=True)
    for line in lines:
        if not start<=line['top']<stop:continue
        chars=sorted(line['chars'],key=lambda c:c['x0'])
        text=''.join(c['text'] for c in chars)
        years=list(re.finditer(r'20\d{2}',text))
        if len(years)!=2:continue
        groups=[chars[m.start():years[i+1].start() if i+1<len(years) else len(chars)] for i,m in enumerate(years)]
        labels=[''.join(c['text'] for c in group).strip() for group in groups]
        if not period_column(labels[0],period) or years[0][0]!=period[:4] or years[1][0]==period[:4]:continue
        centers=[(min(c['x0'] for c in group)+max(c['x1'] for c in group))/2 for group in groups]
        gap=centers[1]-centers[0]
        if gap<35:continue
        layout={'centers':centers,'labels':labels,'top':line['bottom'],'header_page':None}
        break
    if not layout:return None,None
    a,b=layout['centers'];gap=b-a
    left,split,right=max(0,a-gap/2),(a+b)/2,min(page.width,b+gap/2+5)
    rows=[['项目']+layout['labels']]
    top=layout['top'] if layout is not prior else start
    for line in lines:
        if not max(start,top)<=line['top']<stop:continue
        chars=sorted(line['chars'],key=lambda c:c['x0'])
        # Header text can be right-aligned while amounts extend far to its
        # left. Assign WHOLE numeric runs by their center, retaining every
        # digit. A partial comma group is never accepted as a new amount.
        from .financial_evidence import number
        cells=[[],[]];used=set()
        for run in numeric_runs(chars):
            token=''.join(c['text'] for c in run)
            if number(token) is None and token not in ('-','—'):continue
            center=(run[0]['x0']+run[-1]['x1'])/2
            column=0 if left<=center<split else 1 if split<=center<right else None
            if column is not None:
                cells[column].append(token);used.update(id(c) for c in run)
        label=''.join(c['text'] for c in chars if id(c) not in used and c['x0']<left)
        rows.append([label]+[' | '.join(v) for v in cells])
    return {'rows':rows,'centers':layout['centers'],'top':max(start,top),'header_page':layout.get('header_page')},layout


def reconstructed_table(page, table, period, prior_centers=None):
    """Collapse phantom PDF cells using period-header and numeric-cell geometry.

    Character centers, rather than joined words, keep adjacent numeric columns
    apart. Full-height numeric cells define row bands so wrapped labels stay on
    their own row. A continuation may reuse only an already established layout.
    """
    from .financial_evidence import number
    raw=table.extract()
    header_cells=[]
    for row,data in zip(table.rows,raw):
        for box,text in zip(row.cells,data):
            t=_text(text)
            if box and (period_column(t,period) or any(w in t for w in ('上年同期','上期发生额','期初余额','上年度末','上年期末')) or re.match(r'^20\d{2}(?:年|度|[-/.])',t)):
                header_cells.append(((box[0]+box[2])/2,t))
        if any(period_column(v,period) for v in data):
            # This physical header row supplies current and comparison columns.
            break
    centers=[p[0] for p in header_cells] or list(prior_centers or [])
    if not centers:
        return raw,[]
    bands=[]
    for center in sorted(set(centers)):
        cells=[box for row,data in zip(table.rows,raw) for box,text in zip(row.cells,data)
               if box and number(text) is not None and box[0]<center<box[2]
               and box[2]-box[0]<(table.bbox[2]-table.bbox[0])*.55]
        if cells:
            box=max(cells,key=lambda b:b[2]-b[0])
            band=(box[0],box[2])
            if band not in bands:
                bands.append(band)
    bands.sort()
    if not bands or any(a[1]>b[0]+1 for a,b in zip(bands,bands[1:])):
        return raw,[p[0] for p in header_cells]
    # Text-strategy table borders may clip the first Chinese character of
    # labels. The printed row's complete left strip is the label evidence.
    label=(0,bands[0][0])
    if label[1]-label[0]<15:
        return raw,[]
    intervals=sorted({(b[1],b[3]) for row,data in zip(table.rows,raw) for b,v in zip(row.cells,data)
                      if b and number(v) is not None and any(a<(b[0]+b[2])/2<c for a,c in bands)})
    merged=[]
    for top,bottom in intervals:
        if merged and top<merged[-1][1]-.1:
            merged[-1]=(merged[-1][0],max(bottom,merged[-1][1]))
        else:
            merged.append((top,bottom))
    ys=sorted({table.bbox[1],table.bbox[3]}|{v for pair in merged for v in pair})
    chars=page.crop((0,table.bbox[1],page.width,table.bbox[3])).chars
    def read(left,top,right,bottom):
        chosen=[c for c in chars if left<=(c['x0']+c['x1'])/2<right and top<=(c['top']+c['bottom'])/2<bottom]
        # Chinese glyphs and digits may have slightly different font tops on
        # one printed baseline; fixed rounding bins reorder the year digits.
        lines=[]
        for c in sorted(chosen,key=lambda c:(c['top'],c['x0'])):
            if not lines or abs(c['top']-lines[-1][0]['top'])>4:
                lines.append([c])
            else:lines[-1].append(c)
        return '\n'.join(''.join(c['text'] for c in sorted(line,key=lambda c:c['x0'])) for line in lines)
    rows=[[read(left,top,right,bottom) for left,right in [label]+bands] for top,bottom in zip(ys,ys[1:]) if bottom-top>.1]
    return rows,[(a+b)/2 for a,b in bands]


def statement_metric(label,kind):
    t=_text(label)
    t=re.sub(r'^(?:[一二三四五六七八九十\d]+[、.．]|\([一二三四五六七八九十\d]+\)|其中[:：])+','',t)
    t=t.replace('、','').replace(',','')
    t=re.sub(r'^(?:减|加)[:：]','',t)
    if kind=='balance':
        for prefix,name in (('货币资金','cash'),('应收账款','receivables'),('存货','inventory'),('资产总计','total_assets'),('负债合计','total_liabilities'),('所有者权益合计','total_equity'),('所有者权益(或股东权益)合计','total_equity')):
            if t.startswith(prefix):return name
    elif kind=='income':
        if t.startswith('营业总收入'):return 'revenue'
        if t.startswith('营业收入'):return 'operating_revenue'
        if t.startswith('营业成本'):return 'operating_cost'
        if t.startswith(('归属于母公司所有者的净利润','归属于母公司股东的净利润')):return 'net_profit'
    elif kind=='cashflow':
        if t.startswith('经营活动产生的现金流量净额'):return 'operating_cashflow'
        # This cash-flow line has a unique acquisition prefix even when its
        # remaining label continues on the next page after the amount.
        if t.startswith('购建固定资产'):return 'capex'
    return None


def statement_metrics(pages,period):
    """Returns proved values plus unresolved candidates and arithmetic checks."""
    from .financial_evidence import number, FACTORS, MONEY_UNITS
    result,issues={},[]
    state=None;column=None;current_center=None;centers=[];unit=None;unit_page=None;header_page=None;line_layout=None
    end=period[:4]+'-'+{'Q1':'03-31','H1':'06-30','Q3':'09-30','FY':'12-31'}[period[4:]]
    for page_no,page in pages:
        lines=page_lines(page)
        headings=[(line['top'],_heading(line['text'])) for line in lines if _heading(line['text'])]
        if state not in ('balance','income','cashflow') and not any(k in ('balance','income','cashflow') for y,k in headings):
            if headings:state=headings[-1][1]
            continue
        tables=page.find_tables();extras=[]
        # Borderless SSE tables otherwise fragment into single numeric columns.
        # Text geometry is an independent fallback, constrained below to the
        # consolidated statement region and an explicit current-period header.
        boundaries=[(0,state)]+headings+[(page.height,None)]
        for (start,kind),(stop,_) in zip(boundaries,boundaries[1:]):
            if kind in ('balance','income','cashflow') and stop-start>20:
                extra,line_layout=line_table(page,start,stop,period,line_layout if start==0 else None)
                if line_layout and line_layout.get('header_page') is None:line_layout['header_page']=page_no
                if extra:
                    extra['header_page']=line_layout['header_page'];extras.append(extra)
            else:line_layout=None
        events=sorted([(top,'heading',kind) for top,kind in headings]+[(t.bbox[1],'table',t) for t in tables]+[(e['top'],'rows',e) for e in extras],key=lambda e:(e[0],e[1]!='heading'))
        for top,event,value in events:
            if event=='heading':
                state=value;column=None;current_center=None;centers=[];unit=None;unit_page=None;header_page=None
                continue
            if state not in ('balance','income','cashflow'):
                continue
            # Select the nearest printed unit above this table, after the most
            # recent statement heading. Never borrow a unit from another table.
            lower=max((y for y,k in headings if y<=top),default=-1)
            unit_lines=[line for line in lines if lower<=line['top']<=top and re.search(r'单位[:：](?:人民币)?(?:亿元|百万元|万元|千元|元)',_text(line['text']))]
            if unit_lines:
                m=re.search(r'单位[:：](?:人民币)?(亿元|百万元|万元|千元|元)',_text(unit_lines[-1]['text']))
                unit=MONEY_UNITS[m[1]];unit_page=page_no
            rows,new_centers=(value['rows'],value['centers']) if event=='rows' else reconstructed_table(page,value,period,centers)
            if new_centers:
                centers=new_centers
                # A continuation may expose a different number of phantom
                # cells than the preceding table. Carry the physical period
                # column, never its old array index into this new grid.
                if current_center is not None and rows and len(rows[0])==len(centers)+1:
                    nearest=min(range(len(centers)),key=lambda i:abs(centers[i]-current_center))
                    column=nearest+1 if abs(centers[nearest]-current_center)<max(8,(max(centers)-min(centers))*.15) else None
            stitched=[]
            for row in rows:
                label=_text(row[0]) if row else ''
                continuation=bool(re.match(r'^(?:的现金|的净利润|益后的净利润|损益后的净利润|流量净额|期资产|资产支付|长期资产|付的现金|支付的现金|号填列|股\))',label))
                numeric_continuation=bool(stitched and not label and any(number(v) is not None for v in row[1:])
                    and not any(number(v) is not None for v in stitched[-1][1:])
                    and re.match(r'^(?:购建固定资产|[1-9一二三四五六七八九十.、()]*归属于|经营活动产生|货币资金|应收账款|存货|营业|负债|资产|所有者)',_text(stitched[-1][0])))
                if stitched and (continuation or numeric_continuation) and not any(period_column(v,period) for v in row):
                    old=stitched[-1]
                    if len(old)==len(row) and not any(number(v) is not None and number(old[i]) is not None for i,v in enumerate(row[1:],1)):
                        stitched[-1]=[str(a or '')+str(b or '') for a,b in zip(old,row)]
                        continue
                stitched.append(row)
            for row in stitched:
                cols=[i for i,t in enumerate(row) if period_column(t,period)]
                # Printed footer years and a monetary amount equal to a year
                # are not new headers. Bare years need a comparison-year cell.
                if statement_metric(row[0] if row else '',state):cols=[]
                if cols and all(re.fullmatch(r'20\d{2}',_text(row[i])) for i in cols):
                    if not any(re.fullmatch(r'20\d{2}',_text(t)) and _text(t)!=period[:4] for t in row):cols=[]
                if cols:
                    # More than one current period descriptor is ambiguous.
                    column=cols[0] if len(cols)==1 else None;header_page=(value.get('header_page') or page_no) if event=='rows' else page_no
                    if column is not None and len(row)==len(centers)+1 and 0<column<=len(centers):current_center=centers[column-1]
                    continue
                if column is None or column>=len(row) or not unit:
                    continue
                name=statement_metric(''.join(str(v or '') for v in row[:column]),state)
                if not name:continue
                v=number(row[column]);scope='instant' if state=='balance' else 'cumulative'
                if v is None:
                    cell=_text(row[column])
                    issues.append({'metric':name,'source_page':page_no,'source_status':'blank' if not cell else 'marker' if cell in ('-','—','不适用') else 'unreadable',
                                   'source_cell':cell,'quote':' | '.join('' if value is None else str(value) for value in row),'reason':'原表对应期间列为空或尚不能可靠识别，未按零处理'})
                    continue
                key=(name,scope)
                candidate=dict(metric=name,value=v,standard_value=v*FACTORS[unit],unit=unit,scope=scope,period_end=end,
                               source_layout='line_geometry' if event=='rows' else 'pdf_cells',
                               source_page=page_no,source_column=column+1,header_source_page=header_page,unit_source_page=unit_page,
                               quote=' | '.join(str(v or '').replace('\n','') for v in row),verified=True,
                               verification_method='pdf_statement_column',statement=state,verification_status='verified',
                               verification_reason='合并报表、指标行、当前期间列及打印单位已核对')
                if key in result:
                    old=result[key]
                    if abs(old['standard_value']-candidate['standard_value'])>max(.05,abs(old['standard_value'])*1e-8):
                        issues.append({'metric':name,'source_page':page_no,'reason':'同一合并期间出现冲突值，需复核'})
                        old.setdefault('conflict_pages',[old['source_page']]).append(page_no)
                    elif (candidate['source_layout']=='pdf_cells' and old.get('source_layout')=='line_geometry'
                          and not old.get('conflict_pages')):
                        # A text-only fallback can clip a final digit. Prefer
                        # the actual PDF cells when both locate the same value.
                        candidate['replaced_line_geometry_value']=old['value']
                        result[key]=candidate
                    continue
                result[key]=candidate
    for metric in result.values():
        if metric.get('conflict_pages'):
            metric.update(verified=False,standard_value=None,verification_status='conflict',verification_reason='同一合并期间出现冲突值，需复核')
    checks=[]
    def val(name):return (result.get((name,'instant')) or {}).get('standard_value')
    a,l,e=val('total_assets'),val('total_liabilities'),val('total_equity')
    if all(v is not None for v in (a,l,e)):
        checks.append({'name':'资产=负债+所有者权益','passed':abs(a-l-e)<=max(.05,abs(a)*1e-8),'difference':round(a-l-e,4)})
    operating=result.get(('operating_revenue','cumulative'))
    if operating and ('revenue','cumulative') not in result:
        result[('revenue','cumulative')]={**operating,'metric':'revenue'}
    rev=operating or result.get(('revenue','cumulative'));cost=result.get(('operating_cost','cumulative'))
    if rev and cost and rev.get('verified') and cost.get('verified') and rev['standard_value']>0:
        v=(rev['standard_value']-cost['standard_value'])/rev['standard_value']*100
        result[('gross_margin','cumulative')]=dict(metric='gross_margin',value=v,standard_value=v,unit='percentage',scope='cumulative',period_end=end,
              source_page=rev['source_page'],quote=rev['quote']+'；'+cost['quote'],verified=True,verification_method='derived_from_verified',
              formula='(合并营业收入−合并营业成本)/合并营业收入×100',inputs=[rev,cost],verification_status='verified',verification_reason='依据已核对的合并营业收入、营业成本计算')
    return list(result.values()),issues,checks
