"""Bounded PDF retention after reports and durable source evidence are published."""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .core import ROOT, SITE, finite, now
from .financial_evidence import CORE_FIELDS, enrich_extraction, persist_primary_proof
from .watchlist import watched_codes
from .disclosure_sources import official_pdf

NORMAL_DAYS = 30
RECOMMENDED_DAYS = 90
WATCHED_DAYS = 730
UNWATCHED_DAYS = 30
PER_RUN_LIMIT = 20


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _recommended(report):
    scoring = report.get("scoring") or (report.get("decision") or {}).get("scoring") or {}
    growth = next((d for d in scoring.get("dimensions", []) if d.get("name") == "成长"), {})
    trend = (report.get("decision") or {}).get("growth") or {}
    flags = (report.get("decision") or {}).get("red_flags") or []
    return (report.get("rating") in ("buy", "overweight") and report.get("evidence_status") == "sufficient"
            and scoring.get("status") == "calculated" and (finite(scoring.get("coverage_pct")) or 0) >= 85
            and (finite(scoring.get("score")) or 0) >= 65 and (finite(growth.get("covered_weight")) or 0) >= 20
            and (finite(growth.get("points")) or 0) >= 15 and (finite(trend.get("revenue_yoy_pct")) or 0) > 0
            and (finite(trend.get("adjusted_profit_yoy_pct")) or 0) > 0 and not flags)


def _published_report(report_id):
    path = (SITE / "report-details" / (str(report_id) + ".json")).resolve()
    if not path.is_relative_to((SITE / "report-details").resolve()) or not path.is_file():
        return None
    try:
        report = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) and report.get("id") == report_id else None


def _archive_path(relative):
    if not relative:
        return None
    path = ROOT / relative
    return path if not path.is_symlink() and path.resolve().is_relative_to((ROOT / "filings").resolve()) and path.suffix.lower() == ".pdf" else None


def _matches(path, expected):
    if not expected:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as original:
        for block in iter(lambda: original.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest() == expected


def cleanup_pdfs(store, cfg=None, today=None):
    """Remove only eligible local copies; source URL, hash, page text and proof remain."""
    cfg = cfg or {}
    today = today or now().date()
    watched = set(watched_codes(cfg))
    unwatched = cfg.get("pdf_unwatched_at", {})
    if not isinstance(unwatched, dict):
        unwatched = {}
    result = {"removed": 0, "freed_bytes": 0, "skipped": 0}
    rows = store.rows("""SELECT d.*,r.id AS report_id,r.created_at AS report_created
        FROM documents d JOIN reports r ON r.id=(SELECT id FROM reports WHERE document_id=d.id
            ORDER BY created_at DESC,rowid DESC LIMIT 1)
        WHERE d.state='complete' AND d.pdf IS NOT NULL
        ORDER BY r.created_at ASC""")
    for doc in rows:
        if result["removed"] >= PER_RUN_LIMIT:
            break
        report = _published_report(doc["report_id"])
        finished = _day(doc["report_created"])
        disclosed = _day(doc["published_at"])
        path = _archive_path(doc["pdf"])
        if not report or not finished or not disclosed or not path or not official_pdf(doc['source_url']):
            result["skipped"] += 1
            continue
        if (report.get("financial_analysis") or {}).get("data_quality", {}).get("status") != "complete":
            result["skipped"] += 1
            continue
        if doc["code"] in watched:
            deadline = max(finished + timedelta(days=NORMAL_DAYS), disclosed + timedelta(days=WATCHED_DAYS))
        else:
            days = RECOMMENDED_DAYS if _recommended(report) else NORMAL_DAYS
            deadline = finished + timedelta(days=days)
            removed_at = _day(unwatched.get(doc["code"]))
            if removed_at:
                deadline = max(deadline, removed_at + timedelta(days=UNWATCHED_DAYS))
        if today < deadline:
            continue
        try:
            proof = persist_primary_proof(store, doc)
            if not proof or proof.get("conflicts") or any(not check.get("passed") for check in proof.get("checks", [])):
                result["skipped"] += 1
                continue
            verified = {(metric.get("metric"), metric.get("scope")) for metric in proof.get("metrics", [])
                        if metric.get("verified") and finite(metric.get("standard_value")) is not None}
            if any((name, scope) not in verified for name, scope in CORE_FIELDS.items()):
                result["skipped"] += 1
                continue
            current = enrich_extraction(store, doc, report["financial_analysis"])
            if current["data_quality"]["status"] != "complete":
                result["skipped"] += 1
                continue
            if not path.exists():
                store.execute("UPDATE documents SET pdf=NULL WHERE id=? AND pdf=?", (doc["id"], doc["pdf"]))
                continue
            if not path.is_file() or not _matches(path, doc["sha256"]):
                result["skipped"] += 1
                continue
            size = path.stat().st_size
            path.unlink()
            store.execute("UPDATE documents SET pdf=NULL WHERE id=? AND pdf=?", (doc["id"], doc["pdf"]))
            result["removed"] += 1
            result["freed_bytes"] += size
        except (OSError, ValueError, TypeError):
            result["skipped"] += 1
    return result
