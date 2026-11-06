from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path
from urllib.parse import urlparse
from collections import Counter
from datetime import timedelta

from market_labels import market_name
from .core import ROOT, SITE, VAULT, atomic_json, atomic_text, config, dumps, now, public_settings, stamp
from .scorecard import FORMULA, SOURCES, VERSION
from .ratings import LABELS

STATES = {"pending": "待归档", "parsed": "待模型解读", "complete": "已解读", "review": "需人工复核"}


def update_note_properties(path, properties):
    """Update generated index fields while preserving the user's note body."""
    text = path.read_text()
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return
    front, body = text[4:].split("\n---\n", 1)
    lines = front.splitlines()
    for key, value in properties.items():
        line = key + ": " + json.dumps(value, ensure_ascii=False)
        index = next((i for i, old in enumerate(lines) if old.startswith(key + ":")), None)
        if index is None:
            lines.append(line)
        else:
            lines[index] = line
    updated = "---\n" + "\n".join(lines) + "\n---\n" + body
    if updated != text:
        atomic_text(path, updated)


def note_text(report):
    front = dict(type="research", market=market_name(report["market"], report["code"]), code=report["code"], company=report["company"], industry_l1=report["industry"], fiscal_period=report["period"], rating=LABELS[report["rating"]], test_mode=report["test_mode"], written_at=report["date"], source_url=report["source"], model=report["model"])
    if report.get('document_provenance'):
        front['download_url'] = report['document_provenance'].get('download_url')
        front['source_fallback'] = bool(report['document_provenance'].get('fallback_reason'))
    text = "---\n" + "\n".join(k + ": " + json.dumps(v, ensure_ascii=False) for k, v in front.items()) + "\n---\n"
    scoring = report.get("scoring")
    score_line = f"买入评分：{scoring['score']}/100（{scoring['status_label']}；覆盖率{scoring['coverage_pct']}%）。" if scoring else "买入评分：待计算。"
    text += f"# {report['company']}（{report['code']}）{report['period']} 综合研究\n\n评级：{LABELS[report['rating']]}；{score_line}\n\n{report['conclusion']}\n\n"
    for title, body in (("支持证据", report["evidence"]), ("估值与近期交易", report["valuation"]), ("风险与反证", report["risks"])):
        text += f"## {title}\n\n{body}\n\n"
    for section in report["financial_analysis"]["sections"]:
        text += f"## {section['title']}\n\n{section['analysis']}\n\n来源页码：{', '.join(map(str,section['source_pages']))}。\n\n"
    text += "## 财务指标与原文证据\n\n"
    for metric in report["financial_analysis"]["metrics"]:
        text += f"- {metric['metric']}：{metric['value']} {metric['unit']}；{'引文与数值已匹配' if metric['verified'] else '未通过原文核对，不用于评分'}；PDF第{metric['source_page']}页。原文：{metric['quote']}\n"
    text += "\n## 评分与仓位约束\n\n" + dumps(report["decision"]) + "\n\n## 行业来源\n\n"
    for source in report["industry_evidence"]:
        text += f"- [{source['title']}]({source['url']})（{source['published_at']}）：{source['quote']}\n"
    text += "\n## 失效条件\n\n" + "\n".join("- " + s for s in report["falsification"])
    return text + f"\n\n[财报原文]({report['source']})\n\n生成时间：{report['generated_at']}；{report['provider']} / {report['model']}。\n"


def scoring_note(report):
    score = report["scoring"]
    front = dict(type="scorecard", market=market_name(report["market"], report["code"]), code=report["code"], company=report["company"], industry_l1=report["industry"],
                 rating=LABELS[report["rating"]], buy_score=score["score"], coverage=score["coverage_pct"], score_status=score["status_label"], score_version=score["version"], updated_at=score["asof"][:10])
    text = "---\n" + "\n".join(k+": "+json.dumps(v,ensure_ascii=False) for k,v in front.items()) + "\n---\n"
    text += f"# {report['company']} 买入评分\n\n评级：{LABELS[report['rating']]}；买入评分：{score['score']}/100；{score['status_label']}；数据覆盖率{score['coverage_pct']}%。\n\n{score['formula']}\n\n"
    text += "| 维度 | 得分 | 满分 | 已覆盖权重 |\n|---|---:|---:|---:|\n"
    text += "\n".join(f"| {d['name']} | {d['points']} | {d['maximum']} | {d['covered_weight']} |" for d in score["dimensions"])
    text += "\n\n## 缺失数据\n\n" + "\n".join("- "+s for s in score["missing"])
    text += "\n\n## 风险扣分\n\n" + "\n".join(f"- {p['reason']}：{p['points']}分" for p in score["penalties"])
    text += "\n\n## 指标公式与原始值\n\n"
    for item in score["items"]:
        text += f"- {item['name']}：{item['points']}/{item['weight']}分；值={item['value']}；{item['formula']}；来源：{item['source']}。\n"
    text += "\n## 方法来源\n\n" + "\n".join(f"- [{s['name']}]({s['url']})：{s['use']}" for s in SOURCES)
    return text + "\n\n" + score["interpretation"] + "\n"


def _publication_revision():
    """Invalidate rendered reuse when executable research rules/config change."""
    from .vault_archive import safe_config
    digest=hashlib.sha256(dumps(safe_config(config())).encode())
    for path in sorted(Path(__file__).parent.glob('*.py')):
        digest.update(path.name.encode());digest.update(path.read_bytes())
    return digest.hexdigest()


def _publication_cache(revision, preserve_existing=False):
    path=VAULT/'00_基础资料'/'增量发布缓存.json'
    try:value=json.loads(path.read_text())
    except (OSError,ValueError):return {}
    if value.get('kind')!='rendered-report-cache-v1':return {}
    if value.get('revision')!=revision and not preserve_existing:return {}
    return {ident:{**entry,'revision':entry.get('revision',value.get('revision'))}
            for ident,entry in value.get('reports',{}).items() if isinstance(entry,dict)
            and (preserve_existing or entry.get('revision',value.get('revision'))==revision)}


def _document_signature(document):
    return hashlib.sha256(dumps({k:(document or {}).get(k) for k in ('id','sha256','text_file','period','source_url')}).encode()).hexdigest()


def _detail_core_hash(report):
    # Market refresh owns only these live shadow fields. Original facts,
    # frozen profiles, production scores and all other content stay protected.
    value=dict(report)
    if isinstance(value.get('research_profile'),dict):
        value['research_profile']={k:v for k,v in value['research_profile'].items()
                                   if k not in ('current','operating_catalysts','current_inputs_fingerprint')}
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def _legacy_profile_matches(report,entry):
    """Recover a pre-core-hash cache only by reproducing its exact old SHA.

    The market lane saved immutable shadow checkpoints with clocks deduped.
    Reapply the report's recorded assessment clock; never accept a changed
    financial value, original judgment or frozen profile on this path.
    """
    from datetime import datetime,timedelta
    try:clock=datetime.fromisoformat(report['assessment_asof'])
    except (KeyError,ValueError,TypeError):return False
    directory=VAULT/'30_公司'/report['market']/report['code']/'六维研究'/report['id']
    candidates=sorted(directory.glob('更新-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
    for path in candidates:
        try:saved=json.loads(path.read_text())
        except (OSError,ValueError):continue
        old_at=(saved.get('current') or {}).get('asof')
        for seconds in (0,1,-1,2,-2):
            at=(clock+timedelta(seconds=seconds)).isoformat(timespec='seconds')
            def restore(value):
                if isinstance(value,dict):
                    return {k:at if k in ('asof','checked_at','recorded_at') and v==old_at else restore(v) for k,v in value.items()}
                if isinstance(value,list):return [restore(v) for v in value]
                return value
            profile={**saved,'repaired_legacy_score':report['scoring']}
            for key in ('current','operating_catalysts'):
                if key in profile:profile[key]=restore(profile[key])
            historical={**report,'research_profile':profile}
            if _detail_core_hash(historical)!=_detail_core_hash(report):continue
            if hashlib.sha256(dumps(historical).encode()).hexdigest()==entry.get('detail_hash'):return True
    return False


def _reuse_published(original,entry,document):
    if not isinstance(entry,dict) or entry.get('source_hash')!=hashlib.sha256(dumps(original).encode()).hexdigest():return None
    if entry.get('document_signature')!=_document_signature(document):return None
    path=SITE/'report-details'/(original['id']+'.json')
    try:
        raw=path.read_bytes()
        report=json.loads(raw)
        if any(report.get(k)!=original.get(k) for k in ('id','document_id','code','market','period','version','generated_at')):return None
        if not all(k in report for k in ('scoring','decision','research_profile','financial_analysis')):return None
        if hashlib.sha256(raw).hexdigest()!=entry.get('detail_hash'):
            if entry.get('detail_core_hash'):
                if _detail_core_hash(report)!=entry['detail_core_hash']:return None
            elif not _legacy_profile_matches(report,entry):return None
    except (OSError,ValueError):return None
    return report


def publish(store, summary=None, archive=True, incremental_ids=None, preserve_existing=False, refresh_limit=0):
    started=time.monotonic();revision=_publication_revision()
    reuse_entries=_publication_cache(revision,preserve_existing) if incremental_ids is not None else {}
    from .securities import lookup as security_lookup
    def searchable_names(code,original):
        current=security_lookup(code) or {}
        return ' '.join(str(value) for value in [original,current.get('company'),current.get('full_name'),*current.get('aliases',[])] if value)
    reports = [json.loads(r["payload"]) for r in store.rows("SELECT payload FROM reports ORDER BY created_at DESC,rowid DESC")]
    source_hashes={r['id']:hashlib.sha256(dumps(r).encode()).hexdigest() for r in reports}
    documents = store.rows("SELECT * FROM documents ORDER BY published_at DESC,id DESC")
    documents_by_id = {doc["id"]: doc for doc in documents}
    if incremental_ids is not None:
        incremental_ids=set(incremental_ids)
        # Ordinary housekeeping may refresh a bounded number of older renderings.
        # Newly completed reports always run all current checks below.
        if preserve_existing and refresh_limit:
            stale=[r['id'] for r in reports if reuse_entries.get(r['id'],{}).get('revision')!=revision]
            incremental_ids.update(stale[:max(0,int(refresh_limit))])
    reused={}
    for original in reports:
        if incremental_ids is not None and original['id'] not in incremental_ids:
            rendered=_reuse_published(original,reuse_entries.get(original['id']),documents_by_id.get(original['document_id']))
            if rendered is not None:reused[original['id']]=rendered
    # Tracking freezes the original judgment, before current-rule recalculation.
    tracking_reports = [dict(report) for report in reports]
    snapshots = {r["code"]: json.loads(r["payload"]) for r in store.rows("SELECT code,payload FROM snapshots")}
    from .tracking_market import overlay
    snapshots = overlay(snapshots,VAULT)
    from .research_shadow import select_pilot, cache_industry_sources
    pilot = select_pilot(tracking_reports,vault=VAULT)
    pilot_codes = {row['code'] for row in pilot['companies']}
    originals_by_id = {r['id']: r for r in tracking_reports}
    cache_industry_sources(tracking_reports,vault=VAULT)
    from .valuation_inputs import verified_cashflow
    for code in {report['code'] for report in reports if report['id'] not in reused}:
        snapshots.setdefault(code, {}).update(verified_cashflow(store, code))
    benchmark = store.cached("benchmark-hs300-v2")
    if benchmark is None:
        saved = store.rows("SELECT payload FROM cache WHERE key='benchmark-hs300-v2'")
        benchmark = json.loads(saved[0]["payload"]) if saved else {"bars": []}
    for index,report in enumerate(reports):
        if report['id'] in reused:
            reports[index]=reused[report['id']]
            continue
        document = documents_by_id.get(report["document_id"])
        if document:
            from .financial_recheck import recheck,apply
            recheck(report,document,store)
            report.update(apply(report))
        # Full generation inputs stay in the private vault archive, not the
        # web detail response. The database payload remains unchanged.
        report.pop('_generation_evidence',None)
        from .strategy import evaluate
        from .financial_evidence import enrich_extraction
        snapshot = {**snapshots.get(report["code"], {}), "benchmark": benchmark}
        old_decision = report.get("decision") or {}
        report.setdefault("quote_at_analysis", old_decision.get("quote") or {})
        document = documents_by_id.get(report["document_id"])
        if document:
            report["financial_analysis"] = enrich_extraction(store, document, report.get("financial_analysis") or {})
            from .financial_growth import enrich as enrich_growth
            report['financial_analysis']=enrich_growth(document,report['financial_analysis'])
            from .financial_periods import enrich as compare_periods
            report['financial_comparisons']=compare_periods(store,document,report['financial_analysis'],asof=report.get('generated_at'))
            from .quarter_review import build as review_quarter
            report['quarter_review']=review_quarter(report['financial_comparisons'])
        from .market_metrics import refresh_market_financials
        report["market_at_analysis"] = refresh_market_financials(report.get("market_at_analysis"),report["quote_at_analysis"], report["financial_analysis"],
                                                               (snapshot.get("financial") or {}).get("history", []), report.get("generated_at"),(snapshot.get("financial") or {}).get("source"))
        report['industry_evidence']=[{**source,**({'verification_warning':'网站首页没有可核实的文章发布日期，不作为近期行业证据'}
            if urlparse(source.get('url','')).path.lower() in ('','/','/index.html','/index.htm') else {})}
            for source in report.get('industry_evidence',[])]
        # Score, quote, evidence gates and trading signal use one current snapshot.
        # The original generated narrative and immutable database payload remain.
        decision = evaluate(report, report.get("financial_analysis") or {}, snapshot,
                            {"verified_sources": [source for source in report.get('industry_evidence',[]) if not source.get('verification_warning')], "outlook": report.get("industry_outlook", "unknown"), "valuation_context": report.get("valuation_context", "")})
        scoring = decision["scoring"]
        report["legacy_rating"] = report["rating"]
        report["rating"] = decision["rating"]
        report["evidence_status"] = decision["evidence_status"]
        report["scoring"] = scoring
        report["score"] = scoring["score"]
        report["score_coverage"] = scoring["coverage_pct"]
        report["score_status"] = scoring["status"]
        if "legacy_rule_score" not in old_decision:
            report["legacy_rule_score"] = old_decision.get("score")
        report["decision"] = decision
        report["assessment_asof"] = scoring["asof"]
        report['ttm_cashflow_evidence'] = snapshot.get('ttm_cashflow_evidence')
        if snapshot.get('industry'):
            report['industry_sources']=snapshot['industry']
        from .research_consistency import apply as check_consistency
        report.update(check_consistency(report))
        from .research_shadow import attach as attach_research
        from .tracking_market import overlay
        current_snapshot=overlay({report['code']:snapshot}).get(report['code'],snapshot)
        report['research_profile']=attach_research(report,current_snapshot,document,originals_by_id[report['id']],pilot_codes,vault=VAULT)
        # Private generation inputs remain in the immutable original report.
        report.pop('research_at_generation',None)
        store.cache("scorecard-"+report["id"]+"-"+VERSION,scoring,hours=24*365)
    lookup = {}
    company_lookup = {}
    for report in reports:
        lookup.setdefault(report["document_id"], report)
        company_lookup.setdefault(report["code"], report)
    filings = []
    for doc in documents:
        name = market_name(doc["market"], doc["code"])
        if not name:
            continue
        report = lookup.get(doc["id"])
        provenance = store.setting('download-source-' + doc['id'], {})
        official_alias = store.setting('official-disclosure-' + doc['id'], {})
        filings.append({"announcement_id": doc["id"], "code": doc["code"], "company": doc["company"], "market": doc["market"], "market_name": name,
                        "industry": report["industry"] if report else "未核实", "fiscal_period": doc["period"], "title": doc["title"], "disclosed_at": doc["published_at"],
                        "company_search":searchable_names(doc['code'],doc['company']),
                        "source_url": doc["source_url"], "download_url":provenance.get('download_url'),
                        "source_fallback":bool(provenance.get('fallback_reason')),
                        "official_url":official_alias.get('source_url') or doc['source_url'],
                        "archived": bool(doc["pdf"] and (ROOT / doc["pdf"]).is_file()), "status": STATES.get(doc["state"], "待处理"), "error_code": doc["error_code"], "report_id": report["id"] if report else None})
        folder = VAULT / "30_公司" / doc["market"] / doc["code"]
        company_report = company_lookup.get(doc["code"])
        company_note = folder / "公司.md"
        if not company_note.exists():
            front = dict(type="company", market=name, exchange=doc["market"], code=doc["code"], company=doc["company"], industry_l1=company_report["industry"] if company_report else "未核实")
            atomic_text(company_note, "---\n" + "\n".join(k + ": " + json.dumps(v, ensure_ascii=False) for k,v in front.items()) + f"\n---\n# {doc['company']}（{doc['code']}）\n\n在财报索引与研究报告索引中按代码查询。\n")
        elif company_report:
            update_note_properties(company_note, {"market": name, "industry_l1": company_report["industry"]})
        filing_note = folder / "财报" / f"{doc['period']}-{doc['id']}.md"
        if not filing_note.exists():
            front = dict(type="filing", market=name, exchange=doc["market"], code=doc["code"], company=doc["company"], industry_l1=report["industry"] if report else "未核实", fiscal_period=doc["period"], disclosed_at=doc["published_at"][:10], status=STATES[doc["state"]], source_url=doc["source_url"])
            atomic_text(filing_note, "---\n" + "\n".join(k + ": " + json.dumps(v, ensure_ascii=False) for k,v in front.items()) + f"\n---\n# {doc['title']}\n\n[官方财报原文]({doc['source_url']})\n")
        else:
            update_note_properties(filing_note, {"market": name, "status": STATES[doc["state"]], "industry_l1": report["industry"] if report else "未核实"})
        if provenance or official_alias:
            update_note_properties(filing_note, {'official_url':official_alias.get('source_url') or doc['source_url'],
                                   'download_url':provenance.get('download_url') or doc['source_url'],
                                   'source_fallback':bool(provenance.get('fallback_reason'))})
    # Store original exports before replacing them for the first automated run.
    for name in ("filings.json", "reports.json"):
        original = SITE / name
        backup = ROOT / "backups" / ("before-automation-" + name)
        if original.exists() and not backup.exists():
            atomic_text(backup, original.read_text())
    atomic_json(SITE / "filings.json", {"generated_at": stamp(), "filings": filings})
    summaries = []
    cache_entries={}
    for report in reports:
        folder = VAULT / "30_公司" / report["market"] / report["code"]
        note = folder / "研究" / f"{report['period']}-{report['document_id']}-{report['version']}.md"
        if not note.exists():
            atomic_text(note, note_text(report))
        if report['id'] not in reused:atomic_text(folder / "评分" / (report["id"] + "-" + VERSION + ".md"), scoring_note(report))
        from .publication_market import attach
        display_report=report if report['id'] in reused else attach(report,collect=True)
        from .filing_performance import attach as attach_performance
        if report['id'] not in reused:
            display_report=attach_performance(display_report,documents_by_id.get(report['document_id']),snapshots.get(report['code'],{}),benchmark)
            atomic_json(SITE / "report-details" / (report["id"] + ".json"), display_report)
        detail_path=SITE/'report-details'/(report['id']+'.json')
        cache_entries[report['id']]={'source_hash':source_hashes[report['id']],
            'document_signature':_document_signature(documents_by_id.get(report['document_id'])),
            'detail_hash':hashlib.sha256(detail_path.read_bytes()).hexdigest(),
            'detail_core_hash':_detail_core_hash(display_report),
            'revision':reuse_entries[report['id']]['revision'] if report['id'] in reused else revision}
        item = {k: report[k] for k in ("id", "code", "company", "industry", "date", "period", "rating", "topics", "conclusion", "evidence", "valuation", "risks", "source", "test_mode", "provider", "model", "generated_at")}
        item['company_search']=searchable_names(report['code'],report['company'])
        item['publication_refresh_pending']=cache_entries[report['id']]['revision']!=revision
        item["detail_url"] = "./data/report-details/" + report["id"] + ".json"
        item.update(score=report["score"],score_coverage=report["score_coverage"],score_status=report["score_status"],scoring_version=VERSION,
                    score_kind=report["scoring"]["score_kind"],score_range=report["scoring"]["possible_range"],rating_blockers=report["decision"]["rating_blockers"])
        item["evidence_status"] = report["evidence_status"]
        from .research_shadow import summary_fields
        item.update(summary_fields(report.get('research_profile',{})))
        summaries.append(item)
    latest_ids = {r["id"] for r in lookup.values()}
    summaries = [r for r in summaries if r["id"] in latest_ids]
    atomic_json(SITE / "reports.json", {"generated_at": stamp(), "reports": summaries, "version_count": len(reports)})
    from .research_quality import publish_quality
    publish_quality(reports, SITE, VAULT)
    from .research_shadow import audit as audit_research
    audit_research(reports,pilot,vault=VAULT,site=SITE)
    from .tracking import publish_tracking
    tracking = publish_tracking(tracking_reports, snapshots, benchmark, store, VAULT)
    atomic_json(SITE / "scoring-method.json", {"version":VERSION,"formula":FORMULA,"dimensions":[{"name":name,"maximum":weight} for name,weight in (("质量",30),("成长",25),("估值",20),("动量",15),("行业",10))],"references":SOURCES,"status":"本项目研究公式；借鉴公开框架，权重和阈值未经A股样本外验证"})
    today = now().date()
    monday = today - timedelta(days=today.weekday())
    digests = {}
    for kind, since, folder in (("daily", today, "50_日报"), ("weekly", monday, "60_周报")):
        chosen = [r for r in reports if r["id"] in latest_ids and since.isoformat() <= r["date"] <= today.isoformat()]
        counts = dict(Counter(LABELS[r["rating"]] for r in chosen))
        title = ("每日研究简报" if kind == "daily" else "每周研究回顾")
        conclusion = f"本期完成{len(chosen)}份解读，覆盖{len({r['code'] for r in chosen})}家公司。"
        if not chosen:
            conclusion += "当前尚无已完成解读，不能据此判断市场机会。"
        digests[kind] = {"title": title, "start": since.isoformat(), "end": today.isoformat(), "conclusion": conclusion, "ratings": counts,
                         "items": [{"id": r["id"], "code": r["code"], "company": r["company"], "rating": r["rating"], "conclusion": r["conclusion"], "test_mode": r["test_mode"]} for r in chosen], "topics": dict(Counter(t for r in chosen for t in r["topics"]))}
        content = f"# {title} · {today.isoformat()}\n\n{conclusion}\n\n"
        content += "\n\n".join(f"## {r['company']}（{r['code']}）\n\n{r['conclusion']}" for r in chosen)
        # One dated generated digest; deterministic refreshes do not create duplicates.
        atomic_text(VAULT / folder / (str(today if kind == "daily" else monday) + ".md"), content + "\n\n更新时间：" + stamp() + "\n")
    atomic_json(SITE / "digests.json", {"generated_at": stamp(), **digests})
    publish_status(store, summary, len(summaries), len(reports))
    base = VAULT / "00_导航" / "研究报告索引.base"
    if not base.exists():
        atomic_text(base, 'filters:\n  and:\n    - file.inFolder("股票/30_公司")\n    - type == "research"\nproperties:\n  note.written_at:\n    displayName: 解读日期\n  note.market:\n    displayName: 市场\n  note.code:\n    displayName: 股票代码\n  note.company:\n    displayName: 公司名称\n  note.industry_l1:\n    displayName: 行业\n  note.fiscal_period:\n    displayName: 报告期\n  note.rating:\n    displayName: 研究评级\nviews:\n  - type: table\n    name: 自动解读报告\n    order:\n      - written_at\n      - market\n      - code\n      - company\n      - industry_l1\n      - fiscal_period\n      - rating\n')
    archive_started=time.monotonic()
    if archive:
        from .vault_archive import sync
        sync(store, VAULT, ROOT, config())
    atomic_json(VAULT/'00_基础资料'/'增量发布缓存.json',{'kind':'rendered-report-cache-v1','revision':revision,'reports':cache_entries,'updated_at':stamp()})
    return {"documents": len(filings), "reports": len(summaries), "versions": len(reports), "tracking": tracking,
        'publication':{'rendered':len(reports)-len(reused),'reused':len(reused),
                       'refresh_pending':sum(entry['revision']!=revision for entry in cache_entries.values()),
            'elapsed_seconds':round(time.monotonic()-started,3),'archive_seconds':round(time.monotonic()-archive_started,3)}}


def publish_status(store, summary=None, report_count=None, version_count=None):
    queue = store.rows("SELECT state,COUNT(*) AS count FROM documents GROUP BY state")
    latest = store.rows("SELECT started_at,finished_at,state,summary FROM runs ORDER BY id DESC LIMIT 1")
    settings = public_settings()
    counts = store.rows("SELECT COUNT(*) documents,COUNT(DISTINCT code) companies FROM documents")[0]
    if report_count is None:
        report_count = store.rows("SELECT COUNT(DISTINCT document_id) n FROM reports")[0]["n"]
    if version_count is None:
        version_count = store.rows("SELECT COUNT(*) n FROM reports")[0]["n"]
    if summary is None and latest:
        summary = json.loads(latest[0]["summary"] or "{}")
    status = {"generated_at": stamp(), "queue": queue, "report_count": report_count, "report_version_count": version_count, "company_count": counts["companies"],
              "document_count": counts["documents"], "settings": settings, "run": latest[0] if latest else None, "summary": summary or {},
              "scan": store.setting("last_scan", {}), "schedule": store.setting("schedule", {"active": False}),
              "data_policy": "公开原文可追溯；五档研究评级与证据状态分开；缺失数据与覆盖范围明确披露"}
    atomic_json(SITE / "status.json", status)
