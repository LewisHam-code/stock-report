"""Reuse saved publication quotes without moving later prices into the past."""
from datetime import datetime, timedelta
import json

from . import core


def _at(value):
    try:
        at = datetime.fromisoformat(value)
        return at.astimezone(core.TZ) if at.tzinfo else None
    except (ValueError, TypeError):
        return None


def valid(quote):
    price = core.finite((quote or {}).get('price'))
    return price is not None and price > 0 and _at(quote.get('asof')) is not None


def saved(report, vault=None):
    """Read only; publication owns collection and its bounded retry policy."""
    from .publication_market import path
    target = path(report)
    if vault is not None:
        target = vault / target.relative_to(core.VAULT)
    try:
        record = json.loads(target.read_text()) if target.is_file() else {}
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict):
        return {}
    if (record.get('state') != 'available' or record.get('report_id') != report['id']
            or record.get('code') != report['code'] or record.get('market') != report.get('market')
            or record.get('report_generated_at') != report.get('generated_at')):
        return {}
    quote = record.get('quote') or {}
    if not isinstance(quote, dict):
        return {}
    captured = _at(record.get('captured_at'))
    fetched = _at(quote.get('fetched_at'))
    # captured_at is the request start; fetched_at can be a few seconds later.
    if (not valid(quote) or not captured or not fetched or fetched > captured + timedelta(seconds=120)
            or _at(quote['asof']) > max(captured, fetched) or max(captured, fetched) > core.now()):
        return {}
    return record


def baseline_fields(report, record):
    """Only contemporaneous captures or the same closed session may fill gaps.

    A delayed closing quote is explicitly reconstructed, never represented as
    an input the model actually saw. Unknown exchange calendars fail closed.
    """
    if not record:
        return {}
    quote = record['quote']
    generated, captured = _at(report.get('generated_at')), _at(record.get('captured_at'))
    fetched, asof = _at(quote.get('fetched_at')), _at(quote.get('asof'))
    if not all((generated, captured, fetched, asof)) or captured < generated:
        return {}
    completed = max(captured, fetched)
    if record.get('kind') == 'publication' and completed - generated <= timedelta(seconds=120):
        basis = 'publication_capture'
    else:
        from .job_schedule import trading_day
        # Require an actual completed session, and no subsequent market open
        # between its quote, report generation and the later successful fetch.
        if trading_day(asof.date()) is not True or not 15 <= asof.hour < 16:
            return {}
        opening = asof.replace(hour=9, minute=15, second=0, microsecond=0) + timedelta(days=1)
        for _ in range(14):
            state = trading_day(opening.date())
            if state is None:
                return {}
            if state:
                break
            opening += timedelta(days=1)
        else:
            return {}
        closed = asof.replace(hour=16, minute=0, second=0, microsecond=0)
        if not closed <= generated <= fetched <= completed < opening:
            return {}
        basis = 'closed_session_reconstruction'
    return {'price': quote['price'], 'price_asof': quote['asof'],
            'price_collected_at': quote['fetched_at'], 'price_source': quote.get('source'),
            'price_cross_validation': quote.get('cross_validation'), 'price_basis': basis,
            'price_capture_kind': record.get('kind'), 'price_snapshot_report_id': record['report_id']}
