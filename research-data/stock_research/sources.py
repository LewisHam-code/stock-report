from __future__ import annotations

import hashlib
import gzip
import html
import io
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from market_labels import market_name
from .core import ROOT, ServiceError, TZ, atomic_json, config, finite, now, stamp
from .disclosure_sources import OFFICIAL_HOSTS, official_pdf

UA = "StockResearchLocal/1.0 (public disclosure research)"
CNINFO = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CATEGORIES = "category_ndbg_szsh;category_bndbg_szsh;category_yjdbg_szsh;category_sjdbg_szsh"
TEXT_PARSER = 'pypdf-layout-include-rotated-v2'


def request(url, data=None, headers=None, timeout=None, limit=12_000_000, allowed_hosts=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    def check_destination(target):
        parsed = urllib.parse.urlparse(target)
        if allowed_hosts and (parsed.scheme != 'https' or parsed.hostname not in allowed_hosts or parsed.username or parsed.password or parsed.port not in (None,443)):
            raise ServiceError('untrusted_pdf', '原文跳转到未允许的来源，已停止下载')
    class OfficialRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, response_headers, newurl):
            check_destination(newurl)
            return super().redirect_request(request, fp, code, msg, response_headers, newurl)
    check_destination(url)
    try:
        open_request = urllib.request.build_opener(OfficialRedirect()).open if allowed_hosts else urllib.request.urlopen
        with open_request(req, timeout=timeout or config()["network_timeout_seconds"]) as r:
            if allowed_hosts:
                check_destination(r.geturl())
            body = r.read(limit + 1)
            if len(body) > limit:
                raise ServiceError("response_too_large", "来源响应超过文件大小限制")
            if r.headers.get('Content-Encoding') == 'gzip' or body.startswith(b'\x1f\x8b'):
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                        body = stream.read(limit + 1)
                except (OSError, EOFError):
                    raise ServiceError('source_format', '来源压缩数据不完整') from None
                if len(body) > limit:
                    raise ServiceError('response_too_large', '来源解压后超过文件大小限制')
            return body
    except urllib.error.HTTPError as e:
        raise ServiceError("source_http_" + str(e.code), "公开数据源返回 HTTP " + str(e.code)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ServiceError("source_network", "公开数据源暂时不可达") from None


def get_json(url, params=None, **kwargs):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        return json.loads(request(url, **kwargs))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ServiceError("source_format", "公开数据源返回格式异常") from None


def report_period(title):
    title = re.sub(r"[_\s]+", "", re.sub(r"<[^>]+>", "", html.unescape(title)))
    # A/H issuers may prepend this exact disclosure wrapper to a full interim
    # report. Strip only the wrapper; keep rejecting ordinary notices/digests.
    title = re.sub(r'^境内同步披露公告[-－—:：]', '', title)
    if any(word in title for word in ("摘要", "英文", "审计", "取消", "说明", "公告", "督导", "募集", "债券")):
        return None
    match = re.search(r"(20\d{2})年?(第一季度|一季度|第1季度|半年度|中期|第三季度|三季度|第3季度|年度|年)报告(?:全文)?(?:[（(].*?[)）])?$", title)
    if not match:
        return None
    year, kind = match.groups()
    quarter = "Q1" if kind in ("第一季度", "一季度", "第1季度") else "H1" if kind in ("半年度", "中期") else "Q3" if kind in ("第三季度", "三季度", "第3季度") else "FY"
    return year + quarter


def document_from_record(record, column):
    title = re.sub(r"<[^>]+>", "", html.unescape(str(record.get("announcementTitle", "")))).strip()
    period = report_period(title)
    code = str(record.get("secCode", ""))
    ident = str(record.get("announcementId", ""))
    adjunct = str(record.get("adjunctUrl", ""))
    if not period or not re.fullmatch(r"\d{6}", code) or not re.fullmatch(r"\d{8,13}", ident):
        return None
    if not re.fullmatch(r"finalpage/[0-9/-]+/[0-9]+\.pdf", adjunct, re.I):
        return None
    source_market = str(record.get("pageColumn", "")).upper()
    market = "SH" if source_market.startswith("SH") else "SZ" if source_market.startswith("SZ") else "BJ" if source_market.startswith("BJ") or column == "bj" else None
    if not market or not market_name(market, code):
        return None
    try:
        published = datetime.fromtimestamp(int(record["announcementTime"]) / 1000, TZ)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if published.date() > now().date() or not str(record.get("secName", "")).strip():
        return None
    return dict(id=ident, code=code, company=str(record["secName"]).strip(), market=market, period=period,
                title=title, published_at=published.isoformat(timespec="seconds"), source_url="https://static.cninfo.com.cn/" + adjunct)


def query_announcements(start, end, column="szse", page=1, keyword="", stock=""):
    form = dict(pageNum=str(page), pageSize="30", column="szse", tabName="fulltext",
                plate={"bj":"bj", "szse":"sz", "sse":"sh"}.get(column,""), stock=stock,
                searchkey=keyword, secid="", category="" if keyword else CATEGORIES, trade="", seDate=f"{start}~{end}",
                sortName="time", sortType="desc")
    result = get_json(CNINFO, data=urllib.parse.urlencode(form).encode(), headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": "https://www.cninfo.com.cn/"})
    if isinstance(result, dict) and result.get("announcements") is None and result.get("totalAnnouncement") == 0:
        result["announcements"] = []
    if not isinstance(result, dict) or not isinstance(result.get("announcements"), list):
        raise ServiceError("cninfo_format", "巨潮公告数据格式异常，不能确认覆盖范围")
    return result


def cninfo_identity(store,code):
    key='foundation-cninfo-security-identities-v1';index=store.cached(key) if store else None
    if index is None:
        try:
            payload=get_json('https://www.cninfo.com.cn/new/data/szse_stock.json',limit=5_000_000)
            rows=payload.get('stockList') if isinstance(payload,dict) else None
            if not isinstance(rows,list) or len(rows)<1000:return ''
            index={}
            for row in rows:
                ident=str(row.get('orgId',''));stock=str(row.get('code',''))
                if len(stock)==6 and stock.isdigit() and ident and ',' not in ident:
                    index.setdefault(stock,set()).add(ident)
            index={stock:next(iter(ids)) for stock,ids in index.items() if len(ids)==1}
            if store:store.cache(key,index,hours=24*7)
        except ServiceError:return ''
    return code+','+index[code] if code in index else ''


def collect(store, priority_codes=()):
    from .disclosure_sources import POLICY, add_disclosure, query_disclosures
    cfg, today = config(), now().date()
    watched = set(priority_codes)
    summary = {"new": 0, "watched_new": 0, "pages": 0, "examined": 0, "sources": [], "backfill_tasks": 0}
    for policy in POLICY:
        market = policy['market']
        key = "scan_tasks_official_v1_" + market
        tasks = store.setting(key)
        if tasks is None:
            tasks = [{"day": (today - timedelta(days=i)).isoformat(), "page": 1} for i in range(1, cfg["initial_lookback_days"])]
        # Revisit overlapping recent days to catch corrected and late-indexed disclosures.
        existing = {t["day"] for t in tasks}
        for i in range(1, cfg["scan_lookback_days"]):
            day = (today - timedelta(days=i)).isoformat()
            if day not in existing:
                tasks.append({"day": day, "page": 1})
        # Today is scanned every run; an unfinished day's cursor remains durable.
        current = next((t for t in tasks if t["day"] == today.isoformat()), {"day": today.isoformat(), "page": 1})
        tasks = [t for t in tasks if t["day"] != today.isoformat()]
        tasks.insert(0, current)
        state = {"name": policy['name'] + '定期报告', "market": market, "state": "ok", "new": 0, 'primary':policy['primary']}
        try:
            def accept(data):
                summary["pages"] += 1
                summary["examined"] += len(data['documents'])
                state['provider'] = data['provider']
                if data.get('primary_error'):
                    state.update(state='fallback', primary_error=data['primary_error'], fallback='巨潮资讯')
                for doc in data['documents']:
                    added = add_disclosure(store, doc)
                    summary["new"] += added
                    state["new"] += added
                    if doc["code"] in watched:
                        summary["watched_new"] += added

            # A day's descending pages shift as new filings arrive. Recheck its
            # first page before resuming a deeper cursor, including empty watchlists.
            # This is a broad public scan: no watchlist codes are sent upstream.
            if current["page"] > 1:
                front=query_disclosures(store, market, today.isoformat(), today.isoformat(), 1)
                accept(front)
                if current.get('provider')!=front['provider']:
                    # The fallback first page was already accepted. Resume its
                    # own pagination without fetching either page one twice or
                    # an incompatible page from the previous provider.
                    current['provider']=front['provider']
                    if front['total']>front['page_size']:current['page']=2
                    else:tasks.remove(current)
                time.sleep(0.3)
            for _ in range(cfg["pages_per_source_per_run"]):
                if not tasks:
                    break
                task = tasks[0]
                data = query_disclosures(store, market, task["day"], task["day"], task["page"])
                if task.get('provider') != data['provider'] and task['page'] > 1:
                    task['page'] = 1
                    data = query_disclosures(store, market, task['day'], task['day'], 1)
                task['provider'] = data['provider']
                accept(data)
                total = data['total']
                if task["page"] * data['page_size'] < total:
                    task["page"] += 1
                else:
                    tasks.pop(0)
                store.set(key, tasks)
                time.sleep(0.3)
        except ServiceError as e:
            state.update(state="unavailable", error=e.code)
        store.set(key, tasks)
        summary["backfill_tasks"] += len(tasks)
        summary["sources"].append(state)
    store.set("last_scan", summary)
    return summary


def fetch_document_pdf(doc, store=None):
    """Resolve a blocked exchange PDF to an exact CNINFO copy, with provenance."""
    from .disclosure_sources import canonical_title
    if not official_pdf(doc.get('source_url','')):
        raise ServiceError('untrusted_pdf', '财报来源不在允许列表中')
    source = doc['source_url']
    failure = None
    failure_key = 'pdf-primary-error-' + str(urllib.parse.urlparse(source).hostname)
    try:
        prior_failure = store.cached(failure_key) if store else None
        if prior_failure:
            raise ServiceError(prior_failure['code'], '官网原文下载处于短暂冷却期')
        data = request(source, limit=30_000_000, timeout=40, allowed_hosts=OFFICIAL_HOSTS,
                       headers={'Referer':'https://www.sse.com.cn/' if doc['market']=='SH' else 'https://www.bse.cn/'})
        if not data.startswith(b'%PDF-'):
            raise ServiceError('invalid_pdf', '来源返回访问验证页或非 PDF 内容')
    except ServiceError as error:
        if urllib.parse.urlparse(source).hostname not in {'static.sse.com.cn','www.sse.com.cn','www.bse.cn','bse.cn'}:
            raise
        if error.code not in {'invalid_pdf','source_network','source_http_403','source_http_307','source_http_502','source_http_503','source_http_504'}:
            raise
        failure = error.code
        if store:
            store.cache(failure_key, {'code':error.code}, hours=0.5)
        column = {'SH':'sse','BJ':'bj'}[doc['market']]
        date = doc['published_at'][:10]
        matches = []
        identity=cninfo_identity(store,doc['code'])
        for page in range(1,4):
            result = query_announcements(date,date,column,page,stock=identity,keyword='' if identity else doc['company'])
            for row in result['announcements']:
                candidate = document_from_record(row,column)
                if (candidate and candidate['code']==doc['code'] and candidate['market']==doc['market']
                    and candidate['period']==doc['period'] and candidate['published_at'][:10]==date
                    and canonical_title(candidate['title'],candidate['company'])==canonical_title(doc['title'],doc['company'])):
                    matches.append(candidate)
            if page*30 >= int(result.get('totalAnnouncement',0)):
                break
        urls = {candidate['source_url'] for candidate in matches}
        # A category/stock-filtered search can omit an announcement visible in
        # company search. Retain every original identity/version condition.
        if not urls and identity:
            search_complete=False
            for page in range(1,4):
                result=query_announcements(date,date,column,page,keyword=doc['company'])
                for row in result['announcements']:
                    candidate=document_from_record(row,column)
                    if (candidate and candidate['code']==doc['code'] and candidate['market']==doc['market']
                        and candidate['period']==doc['period'] and candidate['published_at'][:10]==date
                        and canonical_title(candidate['title'],candidate['company'])==canonical_title(doc['title'],doc['company'])):
                        matches.append(candidate)
                if page*30>=int(result.get('totalAnnouncement',0)):
                    search_complete=True
                    break
            if not search_complete:
                raise ServiceError('pdf_fallback_unconfirmed','官方备用搜索超出核对页数，不能确认唯一副本，保留待补采')
            urls={candidate['source_url'] for candidate in matches}
        if len(urls) != 1:
            raise ServiceError('pdf_fallback_unconfirmed', '官网原文暂不可用，巨潮副本未能唯一匹配，保留待补采')
        source = urls.pop()
        data = request(source, limit=30_000_000, timeout=40, allowed_hosts=OFFICIAL_HOSTS)
        if not data.startswith(b'%PDF-'):
            raise ServiceError('invalid_pdf', '备用披露原文不是 PDF')
    digest = hashlib.sha256(data).hexdigest()
    if doc.get('sha256') and digest != doc['sha256']:
        raise ServiceError('pdf_changed', '原文与已解读版本指纹不同，需人工复核')
    if store:
        provenance = {'official_url':doc['source_url'],'download_url':source,
                      'fallback_reason':failure,'sha256':digest,'fetched_at':stamp()}
        store.set('download-source-' + doc['id'], provenance)
        store.set('pdf-source-last-check', provenance)
    return data


def archive_pdf(doc, store=None):
    from .securities import price_enabled
    if not price_enabled(doc['code']):raise ServiceError('security_delisted','该股票已确认退市，停止下载财报')
    relative = doc.get("pdf") or f"filings/{doc['market']}/{doc['code']}/{doc['period']}/{doc['id']}.pdf"
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise ServiceError("invalid_document_path", "原文索引路径不合法")
    if path.exists():
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if doc.get("sha256") and sha != doc["sha256"]:
            raise ServiceError("pdf_changed", "同一公告原文校验发生变化，等待人工复核")
    else:
        data = fetch_document_pdf(doc, store)
        sha = hashlib.sha256(data).hexdigest()
        if not data.startswith(b"%PDF-"):
            raise ServiceError("invalid_pdf", "财报原文不是 PDF")
        if doc.get("sha256") and sha != doc["sha256"]:
            raise ServiceError("pdf_changed", "官方原文与已解读版本不一致，需人工复核")
        path.parent.mkdir(parents=True, exist_ok=True)
        # An interrupted write must not leave a truncated archive that later
        # looks like a successfully downloaded original. Never replace originals.
        fd, temporary = tempfile.mkstemp(prefix=".stock-download-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                    raise ServiceError("pdf_changed", "原文并发归档出现校验差异，等待复核") from None
        finally:
            os.unlink(temporary)
    return relative, sha, path


def extract_pdf(doc):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    relative, sha, path = archive_pdf(doc)
    output = ROOT / "text" / f"{doc['id']}-{sha[:12]}.json"
    if output.is_file():
        try:
            saved = json.loads(output.read_text())
            pages = saved.get('pages')
            if (saved.get('sha256') == sha and saved.get('text_parser') == TEXT_PARSER
                    and isinstance(pages,list) and pages
                    and all(isinstance(p,dict) and p.get('page') == i+1 and isinstance(p.get('text'),str) for i,p in enumerate(pages))
                    and sum(len(p['text'].strip()) for p in pages) >= 300):
                return relative, sha, str(output.relative_to(ROOT)), len(pages)
        except (OSError,ValueError,AttributeError):
            pass  # An incomplete or obsolete text record must be parsed again.
    pages = []
    physical_page = None
    try:
        reader = PdfReader(path)
        for i, page in enumerate(reader.pages):
            physical_page = i + 1
            # Valid PDFs may contain genuinely empty pages with no /Contents.
            # pypdf's layout extractor indexes that key without checking it.
            # Preserve the physical page number; never renumber later evidence.
            if page.get_contents() is None:
                pages.append({'page':physical_page,'text':'','empty_content':True})
                continue
            text = page.extract_text(extraction_mode="layout",layout_mode_strip_rotated=False) or ""
            pages.append({"page":physical_page,"text":text})
    except (PdfReadError,KeyError,ValueError,TypeError,IndexError,NotImplementedError,OSError) as error:
        # Do not publish a partially extracted document or buy a model read.
        failure=ServiceError('pdf_text_parse_failed','PDF文字结构无法完整解析，已保留原文，跳过并等待本地复核')
        failure.details={'page':physical_page,'parser_error':type(error).__name__}
        raise failure from None
    if sum(len(p["text"].strip()) for p in pages) < 300:
        import pdfplumber
        try:
            with pdfplumber.open(path) as pdf:
                alternative = [{"page": i + 1, "text": page.extract_text() or ""} for i, page in enumerate(pdf.pages)]
        except Exception as error:
            failure=ServiceError('pdf_text_parse_failed','PDF备用解析未成功，已保留原文，跳过并等待本地复核')
            failure.details={'parser_error':type(error).__name__}
            raise failure from None
        if sum(len(p["text"]) for p in alternative) > sum(len(p["text"]) for p in pages):
            pages = alternative
    if sum(len(p["text"].strip()) for p in pages) < 300:
        raise ServiceError("ocr_required", "扫描件或文字过少，需 OCR 核验；禁止生成评级")
    atomic_json(output, {"sha256": sha, "pages": pages, "extracted_at": stamp(),
                         'text_parser':TEXT_PARSER,'layout_warning':'旋转文字已保留；复杂行列仍须原表核对'})
    return relative, sha, str(output.relative_to(ROOT)), len(pages)


def model_pages(pages, period=''):
    """Remove repeated margin titles, never accounting units or body rows.

    Original extracted pages remain immutable for quotes and evidence checks.
    """
    kind={'FY':'年度','H1':'半年度','Q1':'第一季度','Q3':'第三季度'}.get(period[4:])
    pattern=re.compile(r'[^0-9]{2,65}'+re.escape(period[:4])+r'年'+str(kind)+r'报告') if kind else None
    counts=Counter()
    for p in pages:
        lines=p['text'].splitlines()
        counts.update({re.sub(r'\s','',line) for line in lines[:3]+lines[-3:]
                       if pattern and pattern.fullmatch(re.sub(r'\s','',line)) and any(w in line for w in ('公司','股份'))})
    repeated={s for s,n in counts.items() if n>=max(3,(len(pages)+1)//2)}
    retained=set();result=[]
    for p in pages:
        lines=p['text'].splitlines();kept=[]
        for i,line in enumerate(lines):
            key=re.sub(r'\s','',line);margin=i<3 or i>=len(lines)-3
            if margin and key in repeated:
                if key in retained:continue
                retained.add(key)
            if margin and key in (f"{p['page']}/{len(pages)}",f"{p['page']}／{len(pages)}"):continue
            kept.append(line)
        text=re.sub(r'\n{3,}','\n\n',re.sub(r'[ \t]{2,}','\t','\n'.join(kept)))
        result.append({**p,'text':text})
    return result


def substantive_coverage_complete(coverage):
    """All substantive pages, with only the explicitly reviewed admin filter."""
    if coverage.get('full_text') is True:
        return True
    if coverage.get('all_source_pages_selected') is not True:
        return False
    from .report_content import VERSION
    allowed={'目录及备查文件目录','释义表引导语','通用缩写及重复报告期释义','重复身份或行政联系方式'}
    omissions=coverage.get('omissions') or []
    return (coverage.get('filter_version')==VERSION and bool(omissions) and
            all(o.get('reason') in allowed for o in omissions))


def excerpt(doc, max_chars=None, omit_administrative=False):
    data = json.loads((ROOT / doc["text_file"]).read_text())
    pages = model_pages(data['pages'],doc.get('period',''))
    limit = config()["max_document_chars"] if max_chars is None else max_chars
    def rendered_size(page):return len(f"[PDF第{page['page']}页]\n{page['text']}")
    size = sum(rendered_size(p) for p in pages)+max(0,len(pages)-1)*2
    if size <= limit:
        selected = pages
    else:
        words = ("主要会计", "财务指标", "资产负债表", "利润表", "现金流量表", "经营情况", "风险", "收入", "审计意见", "应收账款", "存货", "关联交易", "行业", "订单", "供不应求", "产能", "价格")
        ranked = sorted(pages, key=lambda p: (p["page"] <= 8, sum(p["text"].count(w) for w in words)), reverse=True)
        selected, used = [], 0
        for page in ranked:
            added=rendered_size(page)+(2 if selected else 0)
            if used + added <= limit:
                selected.append(page)
                used += added
        selected.sort(key=lambda p: p["page"])
    coverage=dict(total_pages=len(pages),read_pages=[p['page'] for p in selected],full_text=len(selected)==len(pages))
    if omit_administrative:
        from .report_content import filter_report_pages,VERSION
        filtered,omissions=filter_report_pages(pages)
        chosen=set(coverage['read_pages']);by_page={p['page']:p for p in filtered}
        selected=[by_page[p['page']] for p in selected if by_page[p['page']]['text'].strip()]
        omissions=[o for o in omissions if o['page'] in chosen]
        coverage.update(all_source_pages_selected=coverage['full_text'],filter_version=VERSION,
                        omissions=omissions,full_text=coverage['full_text'] and not omissions,
                        read_pages=[p['page'] for p in selected],
                        scope_note='仅省略已列明的目录、通用释义及行政信息；不可称作逐字全文输入')
    return "\n\n".join(f"[PDF第{p['page']}页]\n{p['text']}" for p in selected),coverage


def financial_history(code, market):
    from .securities import price_enabled
    if not price_enabled(code):raise ServiceError('security_delisted','该股票已确认退市，停止财务采集')
    from .foundation_data import financial_for
    base=financial_for(code,market)
    if base:
        try:
            age=(now()-datetime.fromisoformat(base['fetched_at'])).total_seconds()
            if 0<=age<24*3600:return base
        except (ValueError,TypeError):pass
    url = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
    params = dict(reportName="RPT_F10_FINANCE_MAINFINADATA", columns="ALL", filter=f'(SECUCODE="{code}.{market}")',
                  pageNumber=1, pageSize=60, sortTypes=-1, sortColumns="REPORT_DATE")
    raw = get_json(url, params)
    rows = (raw.get("result") or {}).get("data") or []
    history = []
    mapping = {"revenue": "TOTALOPERATEREVE", "net_profit": "PARENTNETPROFIT", "adjusted_profit": "KCFJCXSYJLR", "operating_cashflow": "NETCASH_OPERATE_PK", "total_assets": "TOTAL_ASSETS_PK", "roe": "ROEJQ", "gross_margin": "XSMLL", "net_margin": "XSJLL", "eps": "EPSJB"}
    mapping.update(bps="BPS",capital_reserve_per_share="MGZBGJ",retained_earnings_per_share="MGWFPLR",operating_cashflow_per_share="MGJYXJJE",total_shares="TOTAL_SHARE",a_float_shares="A_FREE_SHARE")
    for row in rows:
        end, disclosed = str(row.get("REPORT_DATE", ""))[:10], str(row.get("NOTICE_DATE", ""))[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end) or end > now().date().isoformat():
            continue
        if disclosed and disclosed > now().date().isoformat():
            continue
        h = {"period_end": end, "disclosed_at": disclosed or None, **{k: finite(row.get(v)) for k, v in mapping.items()}}
        history.append(h)
    if not history:
        raise ServiceError("history_unavailable", "历史财务未获取到可核验记录")
    return {"history": history, "source": "https://data.eastmoney.com/bbsj/" + code + ".html", "fetched_at": stamp(), "basis": "累计口径；元、百分比，ROE为报告原口径；非官方结构化源，需与原文复核"}


def latest_quote(code, market):
    from .market_sources import latest_quote as quote
    return quote(code, market)


def daily_bars(code, market, count=640, minimum=60, adjustment='qfq'):
    from .market_sources import daily_bars as prices
    return prices(code, market, count, minimum, adjustment=adjustment)


def company_snapshot(store, code, market):
    from .industry_sources import fetch as public_industry
    key = "snapshot-v4-" + market + code
    cached = store.cached(key)
    if cached:
        return {**cached,'industry':public_industry(store,code,market)}
    result = {"code": code, "market": market, "fetched_at": stamp(), "errors": []}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {"financial": pool.submit(financial_history, code, market), "quote": pool.submit(latest_quote, code, market), "prices": pool.submit(daily_bars, code, market)}
        for name, future in futures.items():
            try:
                result[name] = future.result()
            except ServiceError as e:
                result["errors"].append({"part": name, "code": e.code})
            except Exception:
                result["errors"].append({"part": name, "code": "source_unexpected"})
    # SQLite connections remain on the owning thread; providers fetch in their
    # own bounded pool and return only public classification metadata.
    result['industry']=public_industry(store,code,market)
    store.cache(key, result, hours=2 if result["errors"] else 6)
    from .securities import note_quote_identity
    note_quote_identity(store,code,result.get('quote') or {})
    store.set('market-source-last-check', {'asof':stamp(),
              'quote_provider':result.get('quote',{}).get('provider'),
              'quote_check':result.get('quote',{}).get('cross_validation',{}).get('status'),
              'bars_provider':result.get('prices',{}).get('provider'),
              'bars_check':result.get('prices',{}).get('cross_validation',{}).get('status'),
              'errors':result['errors']})
    store.execute("INSERT INTO snapshots VALUES(?,?,?) ON CONFLICT(code) DO UPDATE SET asof=excluded.asof,payload=excluded.payload", (code, stamp(), json.dumps(result, ensure_ascii=False)))
    return result
