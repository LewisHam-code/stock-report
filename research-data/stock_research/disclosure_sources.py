"""Exchange-first disclosure discovery, with explicit CNINFO fallback."""
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime
from urllib.parse import urlencode, urljoin, urlparse

from market_labels import market_name
from .core import ServiceError, TZ, now

OFFICIAL_HOSTS = frozenset({'static.cninfo.com.cn', 'static.sse.com.cn', 'www.sse.com.cn',
                           'disc.static.szse.cn', 'www.bse.cn', 'bse.cn'})
POLICY = [
    {'market': 'SH', 'name': '上交所', 'primary': 'https://www.sse.com.cn/disclosure/listedinfo/announcement/', 'fallback': '巨潮资讯'},
    {'market': 'SZ', 'name': '深交所／巨潮资讯', 'primary': 'https://www.cninfo.com.cn/', 'fallback': None},
    {'market': 'BJ', 'name': '北交所', 'primary': 'https://www.bse.cn/disclosure/announcement.html', 'fallback': '巨潮资讯'},
]


def official_pdf(url):
    try:
        parsed = urlparse(url)
        return (parsed.scheme == 'https' and parsed.hostname in OFFICIAL_HOSTS and parsed.port in (None, 443)
                and not parsed.username and not parsed.password and parsed.path.lower().endswith('.pdf'))
    except (TypeError, ValueError):
        return False


def json_payload(raw):
    try:
        text = raw.decode('utf-8-sig') if isinstance(raw, bytes) else raw
    except UnicodeDecodeError:
        raise ServiceError('source_format', '来源字符编码异常') from None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        match = re.fullmatch(r'\s*[\w.]+\s*\((.*)\)\s*;?\s*', text or '', re.S)
        if match:
            try:
                return json.loads(match.group(1))
            except ValueError:
                pass
    raise ServiceError('source_format', '来源没有返回可识别的数据，可能需要访问验证')


def exchange_document(record, market):
    from .sources import report_period
    if market == 'SH':
        code, company, title, date, link = [record.get(k) for k in ('SECURITY_CODE', 'SECURITY_NAME', 'TITLE', 'SSEDATE', 'URL')]
        link = urljoin('https://static.sse.com.cn/', str(link or ''))
        prefix = 'sse'
    else:
        code = record.get('companyCd') or record.get('secCode')
        company = record.get('companyAbbr') or record.get('companyName') or record.get('secName')
        title = record.get('disclosureTitle') or record.get('title')
        date = record.get('publishDate') or record.get('publishTime')
        link = urljoin('https://www.bse.cn/', str(record.get('destFilePath') or record.get('filePath') or ''))
        prefix = 'bse'
    title = re.sub(r'<[^>]+>', '', html.unescape(str(title or ''))).strip()
    code, company = str(code or ''), str(company or '').strip()
    period = report_period(title)
    if not period or not company or not market_name(market, code) or not official_pdf(link):
        return None
    try:
        published = datetime.fromisoformat(str(date)[:10]).replace(tzinfo=TZ)
    except (TypeError, ValueError):
        return None
    if published.date() > now().date():
        return None
    ident = prefix + '-' + hashlib.sha256(link.encode()).hexdigest()[:24]
    return dict(id=ident, code=code, company=company, market=market, period=period, title=title,
                published_at=published.isoformat(timespec='seconds'), source_url=link)


def query_exchange(market, start, end, page, code=''):
    from .sources import get_json, request
    if market == 'SH':
        params = {'isPagination': 'true', 'pageHelp.pageSize': 30, 'pageHelp.pageNo': page,
                  'pageHelp.beginPage': page, 'pageHelp.cacheSize': 1, 'pageHelp.endPage': page,
                  'productId': code, 'securityType': '0101,120100,020100,020200,120200',
                  'reportType2': 'DQBG', 'reportType': 'ALL', 'beginDate': start, 'endDate': end}
        data = get_json('https://query.sse.com.cn/security/stock/queryCompanyBulletin.do', params,
                        headers={'Referer': POLICY[0]['primary']})
        if not isinstance(data, dict) or not isinstance(data.get('pageHelp'), dict):
            raise ServiceError('disclosure_format', '上交所公告响应格式异常')
        page_info = data['pageHelp']
        records = data.get('result')
        if records is None:
            records = page_info.get('data')
        total = page_info.get('total')
    else:
        # Use the current official announcement page's own query and field list.
        params = [('page', page - 1), ('companyCd',code), ('isNewThree',1), ('startTime',start),
                  ('endTime',end), ('keyword',''), ('xxfcbj[]','2'), ('disclosureType[]','9503')]
        params += [('disclosureSubtype[]', key) for key in ('9504-8001','9504-2106','9504-2104')]
        params += [('needFields[]', key) for key in ('companyCd','companyName','disclosureTitle',
                                                    'destFilePath','publishDate','xxfcbj','fileExt')]
        body = urlencode(params).encode()
        data = json_payload(request('https://www.bse.cn/disclosureInfoController/companyAnnouncement.do', data=body,
                                    headers={'Content-Type': 'application/x-www-form-urlencoded', 'Referer': POLICY[2]['primary']}))
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, dict):
            raise ServiceError('bse_format', '北交所公告响应格式异常')
        info = data.get('listInfo')
        if not isinstance(info, dict):
            raise ServiceError('bse_format', '北交所公告响应缺少分页信息')
        records, total = info.get('content'), info.get('totalElements')
        if not isinstance(info.get('size'),int) or info['size'] <= 0:
            raise ServiceError('bse_format', '北交所公告响应缺少有效分页大小')
        if isinstance(records, list) and any(not isinstance(row, dict) or str(row.get('xxfcbj')) != '2' for row in records):
            raise ServiceError('bse_market_mismatch', '北交所来源返回了其他市场记录')
    if not isinstance(records, list) or not isinstance(total, (int, float)) or total < 0:
        raise ServiceError('disclosure_format', '官方公告响应缺少列表或总数，不能确认覆盖范围')
    return {'documents': [doc for record in records if isinstance(record, dict)
                          and (doc := exchange_document(record, market))],
            'total': int(total), 'page_size': int(info.get('size',20)) if market == 'BJ' else 30}


def query_cninfo(market, start, end, page):
    from .sources import document_from_record, query_announcements
    column = {'BJ':'bj', 'SH':'sse', 'SZ':'szse'}[market]
    data = query_announcements(start, end, column, page)
    return {'documents': [doc for record in data['announcements']
                          if (doc := document_from_record(record, column)) and doc['market'] == market],
            'total': int(data.get('totalAnnouncement', 0)), 'page_size': 30}


def query_disclosures(store, market, start, end, page):
    """Source-specific cursors prevent page shifts when the fallback changes."""
    if market == 'SZ':
        return {**query_cninfo(market, start, end, page), 'provider': 'cninfo'}
    key = 'disclosure-primary-error-' + market
    if store.cached(key) is None:
        try:
            return {**query_exchange(market, start, end, page), 'provider': market.lower()}
        except ServiceError as error:
            store.cache(key, {'code': error.code}, hours=0.5)
    result = query_cninfo(market, start, end, page)
    return {**result, 'provider': 'cninfo', 'primary_error': (store.cached(key) or {}).get('code', 'source_unavailable')}


def canonical_title(title, company):
    text = re.sub(r'\s+|<[^>]+>', '', title)
    # Prefix company names vary between exchanges and CNINFO. Preserve the
    # report name and all correction/version suffixes after the first year.
    match = re.search(r'20\d{2}年?', text)
    return text[match.start():] if match else text


def add_disclosure(store, doc):
    """Avoid importing the same announcement twice from different hosts."""
    from .securities import price_enabled
    if not price_enabled(doc['code']):return 0
    candidates = store.rows('SELECT id,title,company FROM documents WHERE code=? AND period=? AND substr(published_at,1,10)=?',
                            (doc['code'], doc['period'], doc['published_at'][:10]))
    for old in candidates:
        if canonical_title(old['title'], old['company']) == canonical_title(doc['title'], doc['company']):
            if doc['id'].startswith(('sse-', 'bse-')):
                store.set('official-disclosure-' + old['id'], {'source_url': doc['source_url'], 'source_id': doc['id']})
            return 0
    return store.add_document(doc)
