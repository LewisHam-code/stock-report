"""Public company classification, kept separate from industry outlook evidence."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import json
import re
import unicodedata

from . import core
from .core import ServiceError

TAXONOMIES={'eastmoney_2016':'东方财富行业','csrc_display':'来源标注的证监会行业','shenwan_display':'来源标注的申万行业'}


def _path(value):
    return [part.strip() for part in re.split(r'\s*(?:—+|--+|－|-)\s*',str(value or '')) if part.strip()]


def eastmoney_parse(data,code,market):
    rows=data.get('jbzl') if isinstance(data,dict) else None
    row=rows[0] if isinstance(rows,list) and rows and isinstance(rows[0],dict) else {}
    if row.get('SECUCODE')!=code+'.'+market or row.get('SECURITY_CODE')!=code:
        raise ServiceError('industry_identity_mismatch','东方财富公司行业与请求代码不匹配')
    classes=[{'taxonomy':taxonomy,'path':_path(row[field])} for field,taxonomy in
             (('EM2016','eastmoney_2016'),('INDUSTRYCSRC1','csrc_display')) if row.get(field)]
    if not classes:raise ServiceError('industry_missing','东方财富未提供可识别的行业字段')
    return {'provider':'eastmoney','code':code,'company':row.get('SECURITY_NAME_ABBR'),'classifications':classes,
            'source_url':'https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/Index?code='+market+code+'&type=web'}


class Text(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.skip+=1
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.skip=max(0,self.skip-1)
    def handle_data(self,data):
        if not self.skip and data.strip():self.parts.append(data.strip())


def ths_parse(raw,code):
    encoding=re.search(br'charset\s*=\s*["\x27]?([a-zA-Z0-9_-]+)',raw[:3500])
    charset=encoding[1].decode().lower() if encoding else 'utf-8'
    charset='gb18030' if charset in ('gbk','gb2312') else charset
    try:text=raw.decode(charset)
    except (LookupError,UnicodeError):raise ServiceError('industry_encoding','同花顺行业页面编码无法确认') from None
    parser=Text();parser.feed(text);parts=parser.parts
    header=' '.join(parts[:70])
    if not re.search(r'(?<!\d)'+code+r'(?!\d)',header):
        raise ServiceError('industry_identity_mismatch','同花顺公司行业与请求代码不匹配')
    classes=[]
    for index,part in enumerate(parts):
        if '所属申万行业' in part:
            value=part.split('：',1)[1] if '：' in part else ''
            if not value.strip() and index+1<len(parts):value=parts[index+1]
            path=_path(value)
            if path:classes.append({'taxonomy':'shenwan_display','path':path})
    if not classes:raise ServiceError('industry_missing','同花顺未提供可识别的行业字段')
    return {'provider':'ths','code':code,'classifications':classes,
            'source_url':'https://basic.10jqka.com.cn/'+code+'/company.html'}


def compare(records):
    if len({row['provider'] for row in records})<2:
        return {'status':'single_source' if records else 'unavailable','verified_for_peers':False,
                'reason':'尚未获得两个来源的行业记录'}
    first,second=records[:2]
    a={c['taxonomy']:c['path'] for c in first['classifications']}
    b={c['taxonomy']:c['path'] for c in second['classifications']}
    shared=set(a)&set(b)
    if shared:
        matches=all(a[key]==b[key] for key in shared)
        return {'status':'matched' if matches else 'conflict','verified_for_peers':matches,
                'reason':'相同分类体系的行业路径一致' if matches else '相同分类体系的行业路径存在冲突',
                'comparable_taxonomies':sorted(shared)}
    def leaf(value):return re.sub(r'[IV]+$','',unicodedata.normalize('NFKC',value)).strip()
    consistent=bool({leaf(path[-1]) for path in a.values()} & {leaf(path[-1]) for path in b.values()})
    return {'status':'cross_taxonomy_consistent' if consistent else 'not_comparable','verified_for_peers':False,
            'reason':'细分行业名称可对应，但分类体系不同，尚未形成同口径同行样本' if consistent else '来源使用不同分类体系，保留各自路径，不强行判定冲突或合并'}


def fetch(store,code,market,vault=None):
    from .sources import get_json,request
    if not re.fullmatch(r'\d{6}',code) or market not in ('SH','SZ','BJ'):
        raise ServiceError('invalid_market_identity','股票代码或市场不合法')
    key='company-industry-public-v1-'+market+code
    cached=store.cached(key)
    if cached is not None:return cached
    def em():return eastmoney_parse(get_json('https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax',{'code':market+code},timeout=12),code,market)
    def ths():return ths_parse(request('https://basic.10jqka.com.cn/'+code+'/company.html',timeout=12,limit=1800000),code)
    records,errors=[],[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs=[('eastmoney',pool.submit(em)),('ths',pool.submit(ths))]
        for provider,future in jobs:
            try:records.append(future.result())
            except ServiceError as error:errors.append({'provider':provider,'code':error.code})
    result={'schema':1,'code':code,'market':market,'observed_at':core.stamp(),'sources':records,'errors':errors,
            'cross_validation':compare(records),'basis':'行业归属元数据；不同分类体系分别保留，不能替代行业供需、价格与政策证据，不直接增加行业景气评分'}
    store.cache(key,result,hours=2 if errors else 24*7)
    note=(vault or core.VAULT)/'30_公司'/market/code/'行业资料-自动生成.md'
    marker='<!-- COMPANY_INDUSTRY_V1 -->'
    old=note.read_text() if note.exists() else ''
    if not old or marker in old:
        personal=old.split('\n## 我的备注\n',1)[1] if '\n## 我的备注\n' in old else ''
        lines=[marker,'# 公开行业资料','',result['basis'],'',result['cross_validation']['reason'],'']
        for source in records:
            for classification in source['classifications']:
                lines.append('- '+source['provider']+' / '+TAXONOMIES[classification['taxonomy']]+'：'+' → '.join(classification['path'])+'；来源：'+source['source_url'])
        lines+=['','核验时间：'+result['observed_at'],'','<!-- COMPANY_INDUSTRY_DATA\n'+core.dumps(result)+'\nCOMPANY_INDUSTRY_DATA -->','','## 我的备注',personal.rstrip(),'']
        core.atomic_text(note,'\n'.join(lines))
    return result


def refresh_researched(store,watched=(),limit=3,vault=None):
    """Bounded metadata refresh, independent of paid analysis availability."""
    rows=store.rows('SELECT d.code,d.market FROM documents d JOIN reports r ON r.document_id=d.id GROUP BY d.code,d.market')
    candidates=[*(item for item in watched if isinstance(item,dict)),*rows]
    seen=set();result={'updated':0,'with_two_sources':0,'errors':0}
    for row in candidates:
        code,market=row.get('code'),row.get('market')
        if code in seen or not isinstance(code,str) or not re.fullmatch(r'\d{6}',code) or market not in ('SH','SZ','BJ'):continue
        seen.add(code)
        if store.cached('company-industry-public-v1-'+market+code) is not None:continue
        data=fetch(store,code,market,vault)
        prior=store.rows('SELECT asof,payload FROM snapshots WHERE code=?',(code,))
        snapshot=json.loads(prior[0]['payload']) if prior else {'code':code,'market':market,'errors':[]}
        snapshot['industry']=data
        store.execute('INSERT INTO snapshots VALUES(?,?,?) ON CONFLICT(code) DO UPDATE SET payload=excluded.payload',
                      (code,prior[0]['asof'] if prior else data['observed_at'],core.dumps(snapshot)))
        result['updated']+=1
        result['with_two_sources']+=len(data['sources'])==2
        result['errors']+=bool(data['errors'])
        if result['updated']>=limit or result['errors']>=3:break
    return result
