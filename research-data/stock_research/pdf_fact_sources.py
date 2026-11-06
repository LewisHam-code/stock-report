"""Independent physical evidence for PDF table candidates; no AI calls."""
import re

from .financial_evidence import FACTORS, MONEY_UNITS, normalize, number, header_columns, metric_name, dated_summary_scopes
from .statement_evidence import numeric_runs, period_column, statement_metric, _heading


def bbox(chars):
    return [round(min(c['x0'] for c in chars),3),round(min(c['top'] for c in chars),3),
            round(max(c['x1'] for c in chars),3),round(max(c['bottom'] for c in chars),3)]


def ordered_chars(chars):
    """Cluster printed baselines; rounding buckets can move a slash to the end."""
    lines=[]
    for c in sorted(chars,key=lambda c:((c['top']+c['bottom'])/2,c['x0'])):
        center=(c['top']+c['bottom'])/2
        if not lines or center-lines[-1][0]>3:lines.append((center,[c]))
        else:lines[-1][1].append(c)
    return [c for _,line in lines for c in sorted(line,key=lambda c:c['x0'])]


def same_number(a,b):
    return a is not None and b is not None and abs(a-b)<=max(.00001,abs(b)*1e-12)


def summary_heading(text):
    return bool(re.search(r'主要(?:会计数据|财务数据|财务指标)|会计数据和财务指标|财务概要|财务摘要',text)
                and not any(w in text for w in ('变动','原因','分季度')))


def summary_end(text):
    return bool(re.search(r'分季度(?:主要)?(?:财务|会计)(?:指标|数据)',text) or re.search(r'非经常性损益项目(?:及|和|与)金额',text)
                or re.search(r'主要(?:会计数据|财务指标).*?(?:变动|原因)',text))


class SourceIndex:
    """One document, lazy page layouts, independently located printed anchors."""
    def __init__(self,pdf,document,text_pages=None):
        self.pdf=pdf;self.document=document;self.pages={};self.start_states={};self.end_states={};self._contexts={};self._labels={}
        state=None;summary=False
        for i,record in enumerate(text_pages or [],1):
            n=record.get('page',i);self.start_states[n]=(state,summary)
            for line in record.get('text','').splitlines():
                t=normalize(line);heading=_heading(re.sub(r'[（(]续[）)]$','',t))
                if heading:state=heading;summary=False
                if summary_heading(t) and n<=50:summary=True
                if summary_end(t):summary=False
            self.end_states[n]=(state,summary)

    def page(self,n):
        if n not in self.pages:
            p=self.pdf.pages[n-1]
            self.pages[n]={'page':p,'lines':p.extract_text_lines(return_chars=True),'tables':p.find_tables()}
            self.pages[n]['table_values']=[(t,t.extract()) for t in self.pages[n]['tables']]
        return self.pages[n]

    def runs(self,n):
        p=self.page(n)
        if 'runs' in p:return p['runs']
        assembled=[];cells=[];seen=set()
        for table in p['tables']:
            for row in table.rows:
                for box in row.cells:
                    if not box or tuple(box) in seen or box[3]-box[1]>120 or box[2]-box[0]>p['page'].width*.55:continue
                    seen.add(tuple(box))
                    chars=[c for line in p['lines'] for c in line['chars']
                           if box[0]-.1<=(c['x0']+c['x1'])/2<box[2]+.1 and box[1]<=(c['top']+c['bottom'])/2<box[3]]
                    chars=ordered_chars(chars)
                    text=''.join(c['text'] for c in chars if c['text'].strip())
                    if chars and number(text) is not None:
                        assembled.append(chars);cells.append(box)
        # A line fragment inside an assembled amount is not another value.
        split_points=set()
        if any(self.context(n,line['top'])=='summary' for line in p['lines']):
            for pn in self.run_pages(n,'summary'):
                for scope in ('cumulative','single_quarter','instant'):
                    for header in self.headers(pn,self.pdf.pages[pn-1].height,scope,'summary'):
                        if header['kind']=='cell':split_points.update((header['bbox'][0],header['bbox'][2]))
        for line in p['lines']:
            for run in numeric_runs(line['chars']):
                if number(''.join(c['text'] for c in run)) is None and split_points:
                    pieces=[];current=[]
                    for c in run:
                        if current and any(current[-1]['x1']-.2<=point<=c['x0']+.2 for point in split_points):
                            pieces.append(current);current=[]
                        current.append(c)
                    if current:pieces.append(current)
                    if len(pieces)>1 and all(number(''.join(c['text'] for c in piece)) is not None for piece in pieces):
                        assembled.extend(pieces);continue
                box=bbox(run)
                if any(c[0]-.6<=box[0] and box[2]<=c[2]+.6 and c[1]<=box[1] and box[3]<=c[3]+.6 for c in cells):continue
                assembled.append(run)
        p['runs']=assembled
        return assembled

    def context(self,n,y):
        if (n,y) in self._contexts:return self._contexts[(n,y)]
        state,summary=self.start_states.get(n,(None,False))
        for line in self.page(n)['lines']:
            if line['top']>y:break
            t=normalize(line['text']);heading=_heading(re.sub(r'[（(]续[）)]$','',t))
            if heading:state=heading;summary=False
            if summary_heading(t) and n<=50:summary=True
            if summary_end(t):summary=False
        value='parent' if state=='parent' else 'summary' if summary else state
        self._contexts[(n,y)]=value
        return value

    def interval_headers(self,n,limit,scope):
        """Borderless columns anchored by printed current/prior date intervals."""
        page=self.page(n);starts=[]
        for line in page['lines']:
            chars=[c for c in sorted(line['chars'],key=lambda c:c['x0']) if c['text'].strip()]
            text=''.join(c['text'] for c in chars)
            for match in re.finditer(r'由20\d{2}年\d{1,2}月\d{1,2}日',text):
                selected=chars[match.start():match.end()]
                starts.append({'chars':selected,'bbox':bbox(selected),'year':match[0][1:5]})
        groups=[]
        for start in sorted(starts,key=lambda x:x['bbox'][1]):
            if not groups or start['bbox'][1]-groups[-1][0]['bbox'][1]>35:groups.append([start])
            else:groups[-1].append(start)
        result=[]
        for group in groups:
            years={g['year'] for g in group}
            if self.document['period'][:4] not in years or len(years)<2:continue
            group.sort(key=lambda g:g['bbox'][0]);top=min(g['bbox'][1] for g in group);bottom=max(g['bbox'][3] for g in group)+60
            anchors=[(g['bbox'][0]+g['bbox'][2])/2 for g in group]
            for line in page['lines']:
                if not top-3<=line['top']<=bottom:continue
                chars=[c for c in sorted(line['chars'],key=lambda c:c['x0']) if c['text'].strip()]
                text=''.join(c['text'] for c in chars)
                for match in re.finditer('变化',text):
                    selected=chars[match.start():match.end()]
                    anchors.append((selected[0]['x0']+selected[-1]['x1'])/2)
            anchors=sorted(set(anchors))
            if len(anchors)<2:continue
            date_centers=[(g['bbox'][0]+g['bbox'][2])/2 for g in group]
            for start in group:
                center=(start['bbox'][0]+start['bbox'][2])/2;i=anchors.index(center)
                left=(anchors[i-1]+center)/2 if i else center-(anchors[i+1]-center)/2
                right=(center+anchors[i+1])/2 if i+1<len(anchors) else center+(center-anchors[i-1])/2
                selected=[]
                for line in page['lines']:
                    if line['top']<start['bbox'][1]-3 or line['top']>bottom:continue
                    chars=[c for c in sorted(line['chars'],key=lambda c:c['x0']) if c['text'].strip()]
                    text=''.join(c['text'] for c in chars)
                    for match in re.finditer(r'(?:由|至|于)20\d{2}年\d{1,2}月\d{1,2}日|期间',text):
                        token=chars[match.start():match.end()]
                        token_center=(token[0]['x0']+token[-1]['x1'])/2
                        if min(date_centers,key=lambda x:abs(x-token_center))==center:selected.extend(token)
                if not selected:continue
                text=''.join(c['text'] for c in ordered_chars(selected));box=bbox(selected)
                if box[3]>limit or scope not in dated_summary_scopes(text,self.document['period']):continue
                result.append({'page':n,'bbox':box,'text':text,'scope':scope,'kind':'printed_interval_band',
                               'column_bounds':[left,right],'comparative_headers':[g['bbox'] for g in group if g['year']!=self.document['period'][:4]]})
        return result

    def headers(self,n,limit,scope,kind):
        result=[];period=self.document['period']
        for table,extracted in self.page(n)['table_values']:
            for row,values in zip(table.rows,extracted):
                for i,(box,text) in enumerate(zip(row.cells,values)):
                    if not box or box[3]>limit:continue
                    explicit=header_columns([text],period).get(0)
                    dated=dated_summary_scopes(text,period) if kind=='summary' else set()
                    accepted=(explicit==scope or scope in dated) if kind=='summary' else period_column(text,period)
                    if not accepted:continue
                    t=normalize(text)
                    if len(re.findall(r'20\d{2}',t))>1 and scope not in dated:continue
                    # An isolated report-year in a company profile is not a
                    # financial column. Require a printed comparative period
                    # on the same horizontal header band, even for split grids.
                    band=''.join(line['text'] for line in self.page(n)['lines']
                                 if line['bottom']>=box[1]-2 and line['top']<=box[3]+2)
                    comparative=(any(y!=period[:4] for y in re.findall(r'20\d{2}',band))
                                 or any(v in band for v in ('上年','上期','期初')))
                    if not comparative:continue
                    if re.fullmatch(r'20\d{2}',t) and not any(re.search(r'20\d{2}',normalize(v)) and period[:4] not in normalize(v) for v in values):continue
                    result.append({'page':n,'bbox':list(box),'text':str(text),'scope':scope,'kind':'cell','center':(box[0]+box[2])/2,'table_bbox':list(table.bbox)})
        if kind=='summary':result.extend(self.interval_headers(n,limit,scope))
        # A borderless/right-aligned statement must have both printed periods.
        if kind!='summary':
            for line in self.page(n)['lines']:
                if line['bottom']>limit:continue
                chars=sorted(line['chars'],key=lambda c:c['x0']);text=''.join(c['text'] for c in chars)
                years=list(re.finditer(r'20\d{2}',text))
                if len(years)!=2:continue
                groups=[chars[m.start():years[j+1].start() if j+1<len(years) else len(chars)] for j,m in enumerate(years)]
                if years[1][0]==period[:4]:continue
                group=groups[0]
                while group and not group[-1]['text'].strip():group=group[:-1]
                label=''.join(c['text'] for c in group)
                if not group or not period_column(label,period):continue
                result.append({'page':n,'bbox':bbox(group),'text':label,'scope':scope,'kind':'right_aligned',
                               'center':(group[0]['x0']+group[-1]['x1'])/2})
        return result

    def label(self,n,run,kind):
        p=self.page(n);box=bbox(run);labels=[]
        key=(n,tuple(box),kind)
        if key in self._labels:return self._labels[key]
        for table in p['tables']:
            if not table.bbox[1]<=box[1]<=table.bbox[3]:continue
            for row in table.rows:
                cells=[b for b in row.cells if b]
                if not cells:continue
                top,bottom=min(b[1] for b in cells),max(b[3] for b in cells)
                if not top<=sum(box[1::2])/2<=bottom:continue
                if bottom-top>120:continue  # merged full-column cells are not row labels
                numeric=[b for b in cells if b[0]-.6<=box[0] and b[2]+.6>=box[2]]
                if not numeric:continue
                left=min(b[0] for b in numeric)
                # The indicator belongs to the first physical cell, not all
                # content left of the candidate. Later YTD columns otherwise
                # inherit quarter amounts, percentages and footnote numbers.
                anchors=[b for b in cells if b[2]<=left+.5 and b[2]-b[0]>=10]
                if anchors:anchors=[min(anchors,key=lambda b:b[0])]
                if kind=='summary' and not anchors:continue
                label_right=min(b[2] for b in anchors) if kind=='summary' else left
                if kind=='summary':
                    # A missing vertical border may merge the indicator and
                    # the quarter amount into one cell. The first complete
                    # numeric token on this printed row supplies its boundary.
                    values=[bbox(r)[0] for r in self.runs(n) if number(''.join(c['text'] for c in r)) is not None
                            and top<=(bbox(r)[1]+bbox(r)[3])/2<bottom and bbox(r)[0]>table.bbox[0]+8]
                    if values:label_right=min(label_right,min(values))
                chars=[c for line in p['lines'] for c in line['chars'] if table.bbox[0]-.5<=c['x0'] and c['x1']<=label_right+.5 and top<=(c['top']+c['bottom'])/2<bottom]
                if chars:
                    text=''.join(c['text'] for c in ordered_chars(chars))
                    if kind=='summary' and any(normalize(text).count(word)>1 for word in ('归属于','每股收益','加权平均净资产收益率')):continue
                    labels.append((text,bbox(chars),numeric))
        for line in p['lines']:
            near=abs(line['top']-box[1])<4
            if kind=='summary' and 0<box[1]-line['top']<=12:
                # A wrapped label is centered around its amount's baseline.
                # An intervening amount proves this is a different row.
                earlier=[r for r in self.runs(n) if line['top']-2<=bbox(r)[1]<box[1]-3
                         and number(''.join(c['text'] for c in r)) is not None]
                near=not earlier
            if near:
                label_right=box[0]
                if kind=='summary':
                    first=[bbox(r)[0] for r in self.runs(n) if abs(bbox(r)[1]-box[1])<4
                           and number(''.join(c['text'] for c in r)) is not None]
                    if first:label_right=min(label_right,min(first))
                chars=[c for c in line['chars'] if c['x1']<=label_right+.1]
                if chars and kind=='summary':
                    base_text=normalize(''.join(c['text'] for c in ordered_chars(chars)))
                    complete_label=bool(metric_name(base_text)) and base_text.count('(')<=base_text.count(')')
                    next_values=[bbox(r)[1] for r in self.runs(n) if bbox(r)[1]>box[3]+3 and number(''.join(c['text'] for c in r)) is not None]
                    until=min(line['bottom']+32,min(next_values)-2 if next_values else line['bottom']+32)
                    for following in p['lines']:
                        if not line['bottom']<=following['top']<=until:continue
                        tail=[c for c in following['chars'] if c['x1']<=label_right+.1]
                        text=normalize(''.join(c['text'] for c in sorted(tail,key=lambda c:c['x0'])))
                        # A complete indicator can be followed by a new row's
                        # wrapped name before that row's centered amount.
                        # Only its printed unit may extend a complete label.
                        if complete_label and not re.match(r'^\((?:%|(?:人民币)?(?:亿元|百万元|万元|千元|元))',text):continue
                        if re.match(r'^(?:\((?:人民币)?(?:元|万元)(?:/股)?\)|净利润|的净利润|东的净利润|利润|扣除非经常性|非经常性损益|经常性损益|常性损益|性损益|损益|益后的净利润|于上市公司股东|于母公司股东|上市公司股东|母公司股东|流量净额|现金流量净额|率|股\))',text):chars.extend(tail)
                if chars:
                    # Some PDFs put an entire numeric column in one tall cell.
                    # Its horizontal borders still prove the column; the
                    # printed baseline independently proves the indicator row.
                    bands=[b for t in p['tables'] for row in t.rows for b in row.cells if b
                           and b[1]<=box[1] and box[3]<=b[3]+.6
                           and b[0]-.6<=box[0] and box[2]<=b[2]+.6 and b[2]-b[0]<p['page'].width*.55]
                    text=''.join(c['text'] for c in ordered_chars(chars))
                    # A shortened baseline must not erase the non-recurring
                    # qualifier printed in the complete physical row cell.
                    adjusted_roe=any('净资产收益率' in normalize(t) and ('扣除非经常性损益' in normalize(t) or '扣非' in normalize(t)) for t,_,_ in labels)
                    if kind=='summary' and adjusted_roe and metric_name(text)=='roe':continue
                    labels.append((text,bbox(chars),bands))
        self._labels[key]=labels
        return labels

    def entity_anchor(self,n,y,kind):
        for pn in self.run_pages(n,kind):
            for line in reversed(self.page(pn)['lines']):
                if pn==n and line['bottom']>y:continue
                text=normalize(line['text']);heading=_heading(re.sub(r'[（(]续[）)]$','',text))
                if kind=='summary':
                    if summary_heading(text):
                        return {'page':pn,'bbox':[line['x0'],line['top'],line['x1'],line['bottom']],'text':line['text']}
                    if heading:return None
                elif heading:
                    if heading!=kind:return None
                    return {'page':pn,'bbox':[line['x0'],line['top'],line['x1'],line['bottom']],'text':line['text']}
        return None

    def metric_matches(self,label,kind,name):
        # A label must not contain values from intervening numeric columns.
        if re.search(r'\d[\d,]*\.\d{2,}',label):return False
        if kind=='summary':return metric_name(label)==name
        detected=statement_metric(label,kind)
        return detected==name or name=='revenue' and detected=='operating_revenue'

    def run_pages(self,n,kind):
        pages=[n]
        while pages[-1]>1 and len(pages)<20:
            prior=pages[-1]-1
            if self.context(prior,self.pdf.pages[prior-1].height)!=kind:break
            pages.append(prior)
        return pages

    def industry_profile(self,selected):
        labels=[]
        for n,_ in selected:
            for line in self.page(n)['lines']:
                if self.context(n,line['top'])!='balance':continue
                values=[''.join(c['text'] for c in r) for r in numeric_runs(line['chars'])]
                if not any(number(v) not in (None,0) and ('.' in v or ',' in v) for v in values):continue
                labels.append(normalize(line['text']))
        text=''.join(labels)
        if re.search('发放贷款(?:和|及)垫款',text) and any(t in text for t in ('吸收存款','客户存款')):return 'bank'
        if '保险合同负债' in text:return 'insurance'
        if '代理买卖证券款' in text and '融出资金' in text:return 'securities'
        return 'general'

    def unit(self,n,run,label,label_box,kind,expected,name):
        if expected in MONEY_UNITS.values() and '%' in normalize(''.join(c['text'] for c in run)):return None
        if name=='roe':
            if expected!='percentage':return None
            if '%' in ''.join(c['text'] for c in run):
                return {'page':n,'bbox':bbox(run),'text':'%','unit':'percentage'}
            if '%' in normalize(label):
                return {'page':n,'bbox':label_box,'text':'%','unit':'percentage'}
            return None
        if name=='eps':
            if expected!='yuan':return None
            if re.search(r'(?<![千百万亿])元/股',normalize(label)):return {'page':n,'bbox':label_box,'text':'元/股','unit':'yuan'}
        inline=re.search(r'\((?:人民币)?(亿元|百万元|万元|千元|元)(?:/股)?(?:\)|$)',normalize(label))
        if inline and MONEY_UNITS[inline[1]]==expected:
            return {'page':n,'bbox':label_box,'text':inline[0],'unit':expected}
        if name=='eps':
            if expected!='yuan' or inline:return None
            for line in reversed(self.page(n)['lines']):
                t=normalize(line['text'])
                if (line['bottom']<=run[0]['top'] and ('每股收益' in t or t.startswith(('每股比率','每股指标','每股数据')))
                        and re.search(r'(?<![千百万亿])元/股',t)):
                    return {'page':n,'bbox':[line['x0'],line['top'],line['x1'],line['bottom']],'text':line['text'],'unit':'yuan'}
            for pn in self.run_pages(n,kind):
                for line in reversed(self.page(pn)['lines']):
                    if pn==n and line['bottom']>run[0]['top']:continue
                    match=re.search(r'单位[:：](?:人民币)?(亿元|百万元|万元|千元|元)(?!/)',normalize(line['text']))
                    if match and match[1]!='元':return None
                    if match:
                        return {'page':pn,'bbox':[line['x0'],line['top'],line['x1'],line['bottom']],
                                'text':line['text'],'unit':'yuan','share_basis':'基本每股收益指标名称明确每股口径'}
            return None
        for pn in self.run_pages(n,kind):
            if pn<1:continue
            lines=self.page(pn)['lines']
            for line in reversed(lines):
                if pn==n and line['bottom']>run[0]['top']:continue
                if self.context(pn,line['top']) not in (kind,None):continue
                match=re.search(r'单位[:：](?:人民币)?(亿元|百万元|万元|千元|元)(?!/)',normalize(line['text']))
                if match:
                    if MONEY_UNITS[match[1]]!=expected:return None
                    return {'page':pn,'bbox':[line['x0'],line['top'],line['x1'],line['bottom']],'text':match[0],'unit':expected}
        return None

    def not_applicable(self,page_numbers):
        result=[];seen=set()
        for n in sorted(set(page_numbers)):
            for table,values in self.page(n)['table_values']:
                for row,texts in zip(table.rows,values):
                    for cell,text in zip(row.cells,texts):
                        if not cell or normalize(text)!='不适用':continue
                        chars=[c for line in self.page(n)['lines'] for c in line['chars']
                               if cell[0]<=(c['x0']+c['x1'])/2<cell[2] and cell[1]<=(c['top']+c['bottom'])/2<cell[3]]
                        if not chars:continue
                        box=bbox(chars)
                        if self.context(n,box[1])!='summary':continue
                        entity=self.entity_anchor(n,box[1],'summary')
                        if not entity:continue
                        for label,label_box,_ in self.label(n,chars,'summary'):
                            name=metric_name(label)
                            if not name:continue
                            for scope in ('cumulative','single_quarter'):
                                headers=[]
                                for pn in self.run_pages(n,'summary'):
                                    headers.extend(self.headers(pn,box[1] if pn==n else self.pdf.pages[pn-1].height,scope,'summary'))
                                for header in headers:
                                    if header['kind']!='cell' or not header['bbox'][0]<=(box[0]+box[2])/2<header['bbox'][2]:continue
                                    key=(name,scope,n)
                                    if key in seen:continue
                                    seen.add(key)
                                    result.append({'metric':name,'scope':scope,'source_page':n,'source_status':'not_applicable','source_cell':'不适用',
                                        'reason':'原文对应期间明确标注不适用，不能填零或用其他期间替代',
                                        'evidence':{'source_sha256':self.document['sha256'],'value':{'page':n,'bbox':box,'text':'不适用'},
                                            'label':{'page':n,'bbox':label_box,'text':label},'period':header,'entity_evidence':entity}})
        return result

    def discover(self,page_numbers,exclude=()):
        result=[];seen=set()
        end=self.document['period'][:4]+'-'+{'FY':'12-31','H1':'06-30','Q1':'03-31','Q3':'09-30'}[self.document['period'][4:]]
        for n in sorted(set(page_numbers)):
            for run in self.runs(n):
                kind=self.context(n,bbox(run)[1])
                if kind not in ('summary','balance','income','cashflow'):continue
                value=number(''.join(c['text'] for c in run))
                if value is None:continue
                for label,box,cells in self.label(n,run,kind):
                    name=metric_name(label) if kind=='summary' else statement_metric(label,kind)
                    if not name and kind=='summary' and n<len(self.pdf.pages):
                        following=self.page(n+1)['tables']
                        if following:
                            row=following[0].extract()[0]
                            if not any(number(v) is not None for v in row):name=metric_name(label+''.join(str(v or '') for v in row))
                    if not name:continue
                    scopes=('instant',) if kind=='balance' or name=='total_assets' else ('cumulative','single_quarter') if kind=='summary' and self.document['period'].endswith('Q3') else ('cumulative',)
                    units=('percentage',) if name=='roe' else ('yuan',) if name=='eps' else tuple(MONEY_UNITS.values())
                    for scope in scopes:
                        if (name,scope) in exclude:continue
                        for unit in units:
                            key=(name,scope,value,unit,n)
                            if key in seen:continue
                            seen.add(key)
                            candidate=dict(metric=name,scope=scope,value=value,unit=unit,source_page=n,period_end=end,
                                           quote=label+' | '+''.join(c['text'] for c in run),verification_method='pdf_cell_candidate')
                            proved,_=self.certify(candidate)
                            if proved:result.append(proved);break
        return result

    def certify(self,candidate):
        n=candidate.get('source_page');value=candidate.get('value');scope=candidate.get('scope');name=candidate.get('metric')
        if not isinstance(n,int) or not 1<=n<=len(self.pdf.pages):return None,'source_page_missing'
        expected_end=self.document['period'][:4]+'-'+{'FY':'12-31','Q1':'03-31','H1':'06-30','Q3':'09-30'}[self.document['period'][4:]]
        if candidate.get('period_end')!=expected_end:return None,'period_mismatch'
        for run in self.runs(n):
            printed=''.join(c['text'] for c in run)
            if not same_number(number(printed),value):continue
            box=bbox(run);kind=self.context(n,box[1])
            if kind not in ('summary','balance','income','cashflow'):continue
            entity_anchor=self.entity_anchor(n,box[1],kind)
            if not entity_anchor:continue
            for label,label_box,cells in self.label(n,run,kind):
                label_page=n;original_label=label;tail_box=None;tail_text=None
                # A row label can finish on the next page, but only the
                # first sparse row of the same grid supplies that suffix.
                prefix=normalize(label).startswith(('归属于','扣除非经常性损益','基本每股','加权平均','净资产收益率','经营活动产生','营业','总资产','资产总'))
                if kind=='summary' and prefix and n<len(self.pdf.pages) and box[3]>self.pdf.pages[n-1].height*.75:
                    nxt=self.page(n+1)['tables']
                    if nxt:
                        first=nxt[0].extract()[0];tail=''.join(str(v or '') for v in first)
                        suffix=re.match(r'^(?:的净利润|净利润|利润|流量净额|现金流量净额|收益|益\(|率|股\)|\((?:人民币)?(?:亿元|百万元|万元|千元|元))',normalize(tail))
                        if suffix and not any(number(v) is not None for v in first) and self.metric_matches(label+tail,kind,name):
                            label+=tail;label_page=n+1;tail_text=tail
                            tail_cells=[c for c in nxt[0].rows[0].cells if c]
                            tail_box=list(min(tail_cells,key=lambda c:c[0])) if tail_cells else None
                if not self.metric_matches(label,kind,name):continue
                headers=[]
                for pn in self.run_pages(n,kind):
                    headers+=self.headers(pn,box[1] if pn==n else self.pdf.pages[pn-1].height,scope,kind)
                for header in headers:
                    if header['kind']=='printed_interval_band':
                        bound=header['column_bounds'][0]<=(box[0]+box[2])/2<header['column_bounds'][1]
                    elif header['kind']=='right_aligned':
                        bound=abs(header['bbox'][2]-box[2])<=2
                    else:
                        same_table=(header['page']!=n or any(list(t.bbox)==header.get('table_bbox') and t.bbox[1]<=box[1]<=t.bbox[3] for t in self.page(n)['tables']))
                        if not same_table and header['page']==n:
                            # Broken ruling often separates the header and
                            # body. Join only a nearby physical fragment
                            # whose numeric column has the very same edges.
                            same_table=any(0<=t.bbox[1]-header['table_bbox'][3]<=80
                                and any(abs(b[0]-header['bbox'][0])<1 and abs(b[2]-header['bbox'][2])<1
                                        for row in t.rows for b in row.cells if b
                                        and b[1]<=box[1] and box[3]<=b[3]+.6)
                                for t in self.page(n)['tables'])
                        bound=same_table and any(b[0]<header['center']<b[2] and b[0]-.6<=box[0] and box[2]<=b[2]+.6
                                  and b[2]-b[0]<self.page(n)['page'].width*.55 for b in cells)
                    if bound and kind=='summary' and header['kind']=='cell':
                        if any(b[2]-b[0]>(header['bbox'][2]-header['bbox'][0])*1.3 for b in cells):
                            bound=header['bbox'][0]<=(box[0]+box[2])/2<header['bbox'][2]
                    if not bound:continue
                    unit=self.unit(n,run,original_label,label_box,kind,candidate.get('unit'),name)
                    if (unit is None and name=='eps' and candidate.get('unit')=='yuan' and label_page!=n
                            and re.search(r'(?<![千百万亿])元/$',normalize(original_label))
                            and normalize(tail_text).startswith('股')):
                        unit={'page':n,'bbox':label_box,'text':'元/股','unit':'yuan','kind':'cross_page_unit',
                              'fragments':[{'page':n,'bbox':label_box,'text':original_label},{'page':label_page,'bbox':tail_box,'text':tail_text}]}
                    if unit is None and label_page!=n:
                        # The continuation is at the top of the next page;
                        # the value's previous-page y coordinate cannot bound
                        # which unit declarations precede that continuation.
                        continuation_run=[{**run[0],'top':tail_box[1]}] if tail_box else run
                        unit=self.unit(label_page,continuation_run,tail_text,tail_box,kind,candidate.get('unit'),name)
                    if unit is None:continue
                    evidence={'source_sha256':self.document.get('sha256'),'value':{'page':n,'bbox':box,'text':printed},
                              'label':{'page':n,'bbox':label_box,'text':label,'continued_page':label_page if label_page!=n else None,'continued_bbox':tail_box},
                              'period':{k:v for k,v in header.items() if k!='center'},'unit':unit,
                              'entity':'listed_consolidated_summary' if kind=='summary' else 'consolidated','entity_evidence':entity_anchor,'statement':kind}
                    result={**candidate,'verified':True,'verification_status':'verified','evidence':evidence,
                            'source_cell':printed,'standard_value':value*FACTORS[candidate['unit']],
                            'verification_reason':'完整数字、指标行、期间列、打印单位及合并口径均有原文坐标证据'}
                    return result,None
        return None,'physical_evidence_unresolved'
