from __future__ import annotations

import json
from datetime import timedelta

from .core import ServiceError, Store, api_key, config, dumps, model_profiles, now, run_lock, stamp
from .sources import archive_pdf, collect, company_snapshot, daily_bars, extract_pdf
from .watchlist import watched_codes


def _watch_order(codes):
    return ("CASE WHEN d.code IN (" + ",".join("?" for _ in codes) + ") THEN 0 ELSE 1 END," if codes else ""), tuple(codes)


def download_queue(store, limit, codes=()):
    order, args = _watch_order(codes)
    from .current_batch import queue_scope
    scope,scope_args=queue_scope()
    return store.rows("""SELECT d.* FROM documents d WHERE d.state='pending'
        AND (d.next_retry IS NULL OR d.next_retry<=?) """+scope+" ORDER BY " + order +
        "d.published_at DESC,d.id DESC LIMIT ?", (stamp(), *scope_args, *args, limit))


def analysis_queue(store, limit, codes=()):
    # First research precedes repeat research; refresh oldest assessments first.
    order, args = _watch_order(codes)
    from .current_batch import queue_scope
    scope,scope_args=queue_scope()
    return store.rows("""SELECT d.* FROM documents d WHERE d.state='parsed'
        AND (d.next_retry IS NULL OR d.next_retry<=?) """+scope+" ORDER BY " + order +
        "EXISTS(SELECT 1 FROM reports r WHERE r.document_id=d.id),"
        "COALESCE((SELECT MAX(r.created_at) FROM reports r WHERE r.document_id=d.id),''),"
        "d.published_at DESC,d.id DESC LIMIT ?", (stamp(), *scope_args, *args, limit))


def retire_delisted(store):
    from .securities import load
    codes=[r['code'] for r in load()[0]['securities'] if r.get('status')=='delisted']
    for start in range(0,len(codes),200):
        batch=codes[start:start+200];marks=','.join('?' for _ in batch)
        store.execute("UPDATE documents SET state='review',error_code='security_delisted',next_retry=NULL WHERE state IN ('pending','parsed') AND code IN ("+marks+")",batch)


def research_benchmark(store):
    """Shared market input, independent of model availability and daily quota."""
    cached = store.cached('benchmark-hs300-v2')
    if cached and len(cached.get('bars',[])) >= 61:
        return cached
    if store.cached('benchmark-research-retry-v1'):
        return {'bars':[]}
    try:
        result = daily_bars('000300','SH',count=640,minimum=61)
    except ServiceError as error:
        store.cache('benchmark-research-retry-v1',{'failed':True,'code':error.code},hours=2)
        return {'bars':[]}
    at = now()
    refresh_at = at.replace(hour=16,minute=30,second=0,microsecond=0)
    if at >= refresh_at:
        refresh_at += timedelta(days=1)
    while refresh_at.weekday() >= 5:
        refresh_at += timedelta(days=1)
    store.cache('benchmark-hs300-v2',result,hours=max(1/60,(refresh_at-at).total_seconds()/3600))
    return result


def run(skip_scan=False, skip_ai=False, max_analysis=None):
    try:
        with run_lock():
            return _run(skip_scan, skip_ai, max_analysis)
    except ServiceError as e:
        return {"ok": False, "code": e.code, "message": str(e)}


def _run(skip_scan, skip_ai, max_analysis):
    from .analysis import analyze
    from .pdf_retention import cleanup_pdfs
    from .publish import publish
    store, cfg = Store(), config()
    priority_codes = watched_codes(cfg)
    # Owning the process lock proves earlier unfinished rows no longer have a worker.
    store.execute("UPDATE runs SET state='interrupted',finished_at=?,summary=? WHERE state='running'", (stamp(), dumps({"blockers": ["previous_run_interrupted"]})))
    run_id = store.execute("INSERT INTO runs(started_at,state) VALUES(?,'running')", (stamp(),)).lastrowid
    summary = {"imported": 0, "new": 0, "watched_new": 0, "parsed": 0, "watched_parsed": 0,
               "analyzed": 0, "watched_analyzed": 0, "errors": [], "blockers": []}
    try:
        summary["imported"] = store.import_pilot()
        # Unchanged originals do not need another paid narrative each day.
        # Price/rule scores still refresh on publication without model tokens.
        refresh_days = cfg.get("narrative_refresh_days", 0)
        if not refresh_days:
            store.execute("""UPDATE documents SET state='complete',next_retry=NULL
                WHERE state='parsed' AND error_code IS NULL AND EXISTS
                (SELECT 1 FROM reports r WHERE r.document_id=documents.id)""")
        else:
            store.execute("""UPDATE documents SET state='parsed',next_retry=NULL WHERE state='complete'
            AND id IN (SELECT document_id FROM reports GROUP BY document_id HAVING MAX(json_extract(payload,'$.date'))<=?)
            AND NOT EXISTS(SELECT 1 FROM documents newer WHERE newer.code=documents.code
                AND (newer.published_at>documents.published_at OR
                    (newer.published_at=documents.published_at AND newer.id>documents.id)))""", ((now().date()-timedelta(days=refresh_days)).isoformat(),))
        from . import job_schedule
        adaptive=job_schedule.enabled(cfg)
        if not skip_scan and adaptive:
            from .disclosure_intake import run as intake
            scan=intake()
            summary['new']=scan.get('new',0)
        if not skip_scan and not adaptive:
            from .securities import refresh
            summary['securities'] = refresh(force=store.setting('security_catalog_refresh_requested',False))
            if summary['securities']['state']!='busy':store.set('security_catalog_refresh_requested',False)
            scan = collect(store, priority_codes)
            summary["new"] = scan["new"]
            summary["watched_new"] = scan["watched_new"]
            if any(s["state"] == "unavailable" for s in scan["sources"]):
                summary["blockers"].append("部分公告源不可用，覆盖不完整")
        retire_delisted(store)
        from . import current_batch
        current_batch.enqueue_discovered(store)
        pending = [] if current_batch.managed() or adaptive else download_queue(store, cfg["download_per_run"] or -1, priority_codes)
        for doc in pending:
            try:
                # Preserve a valid original even when subsequent text extraction fails.
                archived, digest, _ = archive_pdf(doc, store)
                store.execute("UPDATE documents SET pdf=?,sha256=? WHERE id=?", (archived, digest, doc["id"]))
                doc.update(pdf=archived, sha256=digest)
                pdf, sha, text, pages = extract_pdf(doc)
                store.execute("UPDATE documents SET pdf=?,sha256=?,text_file=?,page_count=?,state='parsed',error_code=NULL,next_retry=NULL WHERE id=?", (pdf, sha, text, pages, doc["id"]))
                summary["parsed"] += 1
                if doc["code"] in priority_codes:
                    summary["watched_parsed"] += 1
            except ServiceError as e:
                attempts = doc["attempts"] + 1
                store.execute("UPDATE documents SET attempts=?,state=?,next_retry=?,error_code=? WHERE id=?", (attempts, "review" if attempts >= 3 or e.code in ("ocr_required", "pdf_changed") else "pending", (now() + timedelta(hours=6)).isoformat(timespec="seconds"), e.code, doc["id"]))
                summary["errors"].append({"document": doc["id"], "stage": "pdf", "code": e.code})
                if sum(item.get('stage')=='pdf' and item['code']==e.code for item in summary['errors'])>=3:
                    summary['blockers'].append('pdf_source_circuit_breaker')
                    break
            except Exception:
                store.execute("UPDATE documents SET state='review',error_code='pdf_parse_failed' WHERE id=?", (doc["id"],))
                summary["errors"].append({"document": doc["id"], "stage": "pdf", "code": "pdf_parse_failed"})
        from .industry_sources import refresh_researched
        summary['industry_metadata']=refresh_researched(store,cfg.get('watchlist',[]))
        benchmark = None
        if store.rows('SELECT 1 FROM reports LIMIT 1'):
            benchmark = research_benchmark(store)
        # Full financial snapshots are fetched for new interpretations only.
        # Follow-up prices use sparse, model-free collection below.
        from .foundation_job import active as foundation_active, tick as foundation_tick
        building=foundation_active()
        can_analyze = not skip_ai and not building and not current_batch.managed()
        if can_analyze:
            selected = [p for p in model_profiles(cfg) if p["enabled"]]
            available = False
            for profile in selected:
                try:
                    api_key(profile["provider"], profile["id"])
                    available = True
                    break
                except ServiceError:
                    continue
            if not available:
                can_analyze = False
                summary["blockers"].append("missing_key" if selected else "no_enabled_models")
            else:
                from .model_timing import research_window, WAIT
                timing = research_window(cfg, selected)
                if timing['deferred']:
                    can_analyze = False
                    summary['analysis_wait'] = timing
                    summary['blockers'].append(WAIT)
        limit = (cfg["analysis_per_run"] or -1) if max_analysis is None else max(0, min(20, max_analysis))
        if can_analyze:
            queue = analysis_queue(store, limit, priority_codes)
            for doc in queue:
                try:
                    # Check before fetching financial/industry data for a job
                    # that cannot make its next model request today. The model
                    # reservation remains the authoritative concurrent check.
                    if cfg["daily_api_calls"] > 0 and store.api_calls_today() >= cfg["daily_api_calls"]:
                        raise ServiceError("daily_api_limit", "已达到本地每日模型调用上限，次日续跑")
                    snapshot = company_snapshot(store, doc["code"], doc["market"])
                    from .valuation_inputs import verified_cashflow
                    snapshot.update(verified_cashflow(store, doc['code']))
                    if benchmark is None:
                        benchmark = research_benchmark(store)
                    snapshot["benchmark"] = benchmark
                    report = analyze(store, doc, snapshot)
                    store.execute("INSERT OR IGNORE INTO reports VALUES(?,?,?,?,?)", (report["id"], doc["id"], report["version"], dumps(report), stamp()))
                    store.execute("UPDATE documents SET state='complete',error_code=NULL,next_retry=NULL WHERE id=?", (doc["id"],))
                    summary["analyzed"] += 1
                    if doc["code"] in priority_codes:
                        summary["watched_analyzed"] += 1
                except ServiceError as e:
                    if e.code in ('deep_reading_requires_partition','financial_proof_pending','report_cost_approval_required','report_cost_baseline_missing','report_cost_limit','report_cost_unestimated'):
                        store.execute("UPDATE documents SET state='review',error_code=?,next_retry=NULL WHERE id=?",(e.code,doc['id']))
                        summary['errors'].append({'document':doc['id'],'stage':'analysis','code':e.code})
                        continue
                    if e.code == 'model_off_peak_wait':
                        summary['analysis_wait'] = e.timing
                        summary['blockers'].append(e.code)
                        break  # This is a price deferral, not a document failure.
                    summary["errors"].append({"document": doc["id"], "stage": "analysis", "code": e.code})
                    store.execute("UPDATE documents SET error_code=?,next_retry=? WHERE id=?", (e.code, (now() + timedelta(hours=1)).isoformat(timespec="seconds"), doc["id"]))
                    if e.code in ("quota_exhausted", "invalid_key", "missing_key", "rate_limited", "daily_api_limit", "model_access_denied", "no_enabled_models", "unsafe_model_redirect"):
                        summary["blockers"].append(e.code)
                        break
                    # Stop a run after three same-class failures; keep jobs durable.
                    if sum(e2["code"] == e.code for e2 in summary["errors"]) >= 3:
                        summary["blockers"].append("analysis_circuit_breaker")
                        break
        from .tracking import collect_tracking_prices
        summary["tracking_prices"] = collect_tracking_prices(store, cfg.get("watchlist", []))
        if building and not skip_ai:
            summary['foundation']=foundation_tick()
        summary["pdf_retention"] = cleanup_pdfs(store, cfg)
        state = "waiting" if summary["blockers"] else "partial" if summary["errors"] else "ok"
        # During a managed quarter, a code update must not turn ordinary
        # housekeeping into hours of all-history PDF revalidation.
        publish_options={'incremental_ids':set(),'preserve_existing':True,'refresh_limit':1} if current_batch.managed() else {}
        summary["published"] = publish(store, summary, archive=False, **publish_options)
        store.execute("UPDATE runs SET finished_at=?,state=?,summary=? WHERE id=?", (stamp(), state, dumps(summary), run_id))
        # The first export is made before declaring success; finalize public status
        # only after all report, digest and note writes have succeeded.
        from .publish import publish_status
        publish_status(store, summary)
        from .vault_archive import sync
        sync(store,cfg=cfg)
        # Publish and archive normal work first: a long one-off batch must
        # not delay the ordinary site's data refresh for up to fifty minutes.
        if not skip_ai:
            from .annual_batch import active as annual_active, tick as annual_tick
            if annual_active():summary['annual_batch']=annual_tick()
            if current_batch.active():summary['current_batch']=current_batch.tick()
        return {"ok": state == "ok", "state": state, **summary}
    except Exception:
        # Avoid exposing traces, paths, SQL or input data in public status exports.
        store.execute("UPDATE runs SET finished_at=?,state='failed',summary=? WHERE id=?", (stamp(), dumps({"error": "pipeline_unexpected", "counts": {k: summary[k] for k in ("new", "parsed", "analyzed")}}), run_id))
        try:
            from .publish import publish_status
            publish_status(store, {"blockers": ["pipeline_unexpected"]})
        except Exception:
            pass  # Preserve the sanitized primary failure even if the disk is full.
        raise ServiceError("pipeline_unexpected", "研究流程发生错误，原始资料已保留，状态已记录") from None
    finally:
        store.conn.close()
