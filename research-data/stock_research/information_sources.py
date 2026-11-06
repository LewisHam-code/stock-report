"""Bounded company news/announcement metadata, separate from verified facts."""
from __future__ import annotations

import hashlib
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlparse, urlunparse

from .core import ServiceError, atomic_text, dumps, stamp, VAULT
from .disclosure_sources import POLICY, json_payload


def _text(value):
    return re.sub(r'<[^>]+>', '', html.unescape(str(value or ''))).strip()


def _link(value, domain):
    try:
        parsed = urlparse(str(value))
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.port not in (None,80,443):
            return None
        if parsed.hostname != domain and not (parsed.hostname or '').endswith('.' + domain):
            return None
        return urlunparse(parsed._replace(scheme='https', fragment=''))
    except ValueError:
        return None


def _event(code, provider, category, title, url, published_at=None, publisher=None, summary=''):
    return {'id': hashlib.sha256(url.encode()).hexdigest()[:24], 'code': code, 'provider': provider,
            'category': category, 'title': _text(title), 'source_url': url, 'published_at': published_at,
            'publisher': _text(publisher) or provider, 'summary': _text(summary)[:600], 'captured_at': stamp(),
            'verification_status': 'source_metadata_only'}


def eastmoney_announcements(code):
    from .sources import get_json
    data = get_json('https://np-anotice-stock.eastmoney.com/api/security/ann',
                    {'sr':-1, 'page_size':10, 'page_index':1, 'ann_type':'A', 'stock_list':code},
                    headers={'Referer':'https://data.eastmoney.com/'})
    section = data.get('data') if isinstance(data, dict) else None
    rows = section.get('list') if isinstance(section, dict) else None
    if not isinstance(rows, list):
        raise ServiceError('information_format', '东方财富公告列表格式异常')
    events = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('codes'),list):
            continue
        if code not in {str(item.get('stock_code')) for item in row['codes'] if isinstance(item,dict)}:
            continue
        ident = str(row.get('art_code',''))
        if re.fullmatch(r'AN\d+', ident):
            events.append(_event(code, 'eastmoney', 'announcement', row.get('title'),
                                 f'https://data.eastmoney.com/notices/detail/{code}/{ident}.html', row.get('notice_date')))
    return events


def eastmoney_news(code, company):
    from .sources import request
    param = {'uid':'', 'keyword':company if company and company != '名称待核实' else code, 'type':['cmsArticleWebOld'],
             'client':'web', 'clientType':'web', 'clientVersion':'curr',
             'param':{'cmsArticleWebOld':{'searchScope':'default','sort':'latest','pageIndex':1,'pageSize':10,'preTag':'','postTag':''}}}
    data = json_payload(request('https://search-api-web.eastmoney.com/search/jsonp?' + urlencode({'cb':'stock_info','param':json.dumps(param,ensure_ascii=False)}),
                                headers={'Referer':'https://so.eastmoney.com/'}))
    section = data.get('result') if isinstance(data, dict) else None
    rows = section.get('cmsArticleWebOld') if isinstance(section, dict) else None
    if not isinstance(rows, list):
        raise ServiceError('information_format', '东方财富新闻列表格式异常')
    events = []
    for row in rows:
        if not isinstance(row,dict):
            continue
        text = _text(str(row.get('title','')) + ' ' + str(row.get('content','')))
        # A bare six-digit search may match a share count in another company.
        if company and company != '名称待核实' and company not in text:
            continue
        link = _link(row.get('url'), 'eastmoney.com')
        if link:
            event = _event(code, 'eastmoney', 'news', row.get('title'), link, row.get('date'),row.get('mediaName'),row.get('content'))
            event['relevance'] = 'company_name' if company and company != '名称待核实' else 'code_query_unverified'
            events.append(event)
    return events


def ths_news(code):
    from .sources import request
    raw = request('https://stockpage.10jqka.com.cn/' + code + '/', limit=2_000_000).decode('utf-8', errors='replace')
    chunks = []
    # Decode only JSON data carried by Next.js; never execute page scripts.
    for match in re.finditer(r'self\.__next_f\.push\((\[.*?\])\)</script>', raw, re.S):
        try:
            data = json.loads(match.group(1))
            if len(data)>1 and isinstance(data[1],str):
                chunks.append(data[1])
        except ValueError:
            continue
    text = ''.join(chunks)
    match = re.search(r'"initialUpperState"\s*:', text)
    if not match:
        raise ServiceError('information_format', '同花顺页面未提供可识别的个股资讯列表')
    try:
        data, _ = json.JSONDecoder().raw_decode(text[match.end():].lstrip())
        rows = data['items']
        if not isinstance(rows,list):
            raise TypeError()
    except (ValueError, KeyError, TypeError):
        raise ServiceError('information_format', '同花顺个股资讯结构发生变化') from None
    events=[]
    for row in rows[:10]:
        if not isinstance(row,dict):
            continue
        link = _link(row.get('jumpUrl'), '10jqka.com.cn')
        if not link:
            continue
        # Relative labels such as “昨天” are not exact publication timestamps.
        event = _event(code,'ths','announcement' if row.get('type')=='announcement' else 'news',row.get('title'),link,
                       publisher=row.get('source'),summary=row.get('summary'))
        event['source_time_label']=row.get('timeLabel')
        events.append(event)
    return events


def company_information(store, code, market, company, vault=None):
    key='company-information-v1-' + market + code
    cached=store.cached(key)
    if cached is not None:
        return cached
    events,errors=[],[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs=[('eastmoney_announcements',pool.submit(eastmoney_announcements,code)),
              ('eastmoney_news',pool.submit(eastmoney_news,code,company)),('ths_news',pool.submit(ths_news,code))]
        for provider,future in jobs:
            try:events.extend(future.result())
            except ServiceError as error:errors.append({'provider':provider,'code':error.code})
    events=list({event['id']:event for event in events}.values())
    payload={'code':code,'company':company,'asof':stamp(),'items':events,'errors':errors,
             'basis':'第三方资讯元数据；公告事实须回到所属交易所原文核验，尚未用于自动调整评级'}
    from .company_events import append_events
    append_events(code, market, events, vault)
    store.cache(key,payload,hours=1 if errors else 6)
    # One stable note per company keeps original links portable. No private
    # annotations are overwritten and no third-party article bodies are copied.
    folder=(vault or VAULT)/'30_公司'/market/code
    note=folder/'资讯来源-自动生成.md'
    old=note.read_text() if note.exists() else ''
    marker='<!-- STOCK_INFORMATION_V1 -->'
    if not old or marker in old:
        personal=old.split('\n## 我的备注\n',1)[1] if '\n## 我的备注\n' in old else ''
        lines=[marker,'# 个股资讯来源','',payload['basis'],'','| 来源 | 类型 | 标题 | 发布时间 |','|---|---|---|---|']
        for event in events:
            title=event['title'].replace('|','∣').replace('[','［').replace(']','］')
            lines.append(f"| {event['provider']} | {event['category']} | [{title}]({event['source_url']}) | {event['published_at'] or '待核实'} |")
        lines += ['', '<!-- STOCK_INFORMATION_DATA\n' + dumps(payload).replace('-->','--\\u003e') + '\nSTOCK_INFORMATION_DATA -->', '', '## 我的备注', personal.rstrip(), '']
        atomic_text(note,'\n'.join(lines))
    return payload


def source_policy(store):
    return {'disclosures':POLICY,'market_data':{'primary':'东方财富','secondary':'同花顺',
            'cross_validation':'同交易时点报价与同交易日前复权日线比较；差异保留复核，不平均填补',
            'news':'东方财富公告与新闻、同花顺个股资讯按需获取，6小时缓存；尚未用于自动评级'},
            'last_scan':store.setting('last_scan',{}).get('sources',[]),
            'last_market_check':store.setting('market-source-last-check',{}),
            'last_pdf_check':store.setting('pdf-source-last-check',{}), 'asof':stamp()}
