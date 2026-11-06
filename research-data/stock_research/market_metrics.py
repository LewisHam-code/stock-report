"""A report's original price snapshot; derived ratios disclose their basis."""
from .core import finite, stamp


def build_market_snapshot(quote, extraction, history=None, known_at=None, financial_source=None):
    out = dict(quote or {})
    out["basis_note"] = "原解读采用的行情快照；非实时价格。动态PE为已披露累计利润简单年化，不是盈利预测。"
    from .financial_facts import certified_metrics
    metrics = {m["metric"]:m for m in certified_metrics(extraction or {}) if m.get("scope") in ("cumulative", "instant")}
    price, cap, pb = [finite(out.get(k)) for k in ("price", "market_cap", "pb")]
    profit = metrics.get("net_profit") or {}
    end = profit.get("period_end", "")
    p = finite(profit.get("standard_value"))
    multiplier = {"03-31":4,"06-30":2,"09-30":4/3,"12-31":1}.get(end[5:])
    out["pe_dynamic"] = cap/(p*multiplier) if cap and p is not None and p > 0 and multiplier else None
    out["dynamic_pe_period"] = end or None
    out['pe_dynamic_calculated']=out['pe_dynamic']
    out['dynamic_pe_calculation_period']=out['dynamic_pe_period']
    out['dynamic_pe_basis']='report_annualized'
    if finite(out.get('pe_dynamic_source')) is not None:
        source=finite(out['pe_dynamic_source'])
        out.update(pe_dynamic=source if source>0 else None,dynamic_pe_period=None,dynamic_pe_basis='provider')
    valid_history = [r for r in (history or []) if not known_at or (r.get("period_end", "9999") <= known_at[:10] and (not r.get("disclosed_at") or r["disclosed_at"] <= known_at[:10]))]
    auxiliary = next((r for r in valid_history if r.get("period_end")==end),{})
    annual = sorted([r for r in valid_history if r["period_end"].endswith("12-31")],key=lambda r:r["period_end"],reverse=True)
    annual_profit = finite(annual[0].get("net_profit")) if annual else None
    out["pe_static"] = cap/annual_profit if cap and annual_profit and annual_profit > 0 else None
    out["static_pe_period"] = annual[0]["period_end"] if annual else None
    out['pe_static_calculated']=out['pe_static'];out['static_pe_basis']='historical_financial'
    if finite(out.get('pe_static_source')) is not None:
        source=finite(out['pe_static_source'])
        out.update(pe_static=source if source>0 else None,static_pe_period=None,static_pe_basis='provider')
    out["total_shares_estimate"] = cap/price if cap and price and price > 0 else None
    out["bps_estimate"] = price/pb if price and pb and pb > 0 else None
    for name in ("eps", "roe", "gross_margin"):
        value = metrics.get(name) or {}
        out[name] = value.get("standard_value")
        out[name+"_period"] = value.get("period_end")
    cash = finite((metrics.get("operating_cashflow") or {}).get("standard_value"))
    out["cashflow_per_share_estimate"] = cash/out["total_shares_estimate"] if cash is not None and out["total_shares_estimate"] else None
    out["financial_period"] = end or (metrics.get("roe") or {}).get("period_end")
    for field in ("bps","capital_reserve_per_share","retained_earnings_per_share","operating_cashflow_per_share","total_shares","a_float_shares"):
        out[field] = finite(auxiliary.get(field))
    if out.get("gross_margin") is None:
        out["gross_margin"] = finite(auxiliary.get("gross_margin"))
    out["auxiliary_basis"] = "东方财富结构化财务辅助源；按报告期匹配，未逐项核对原文，不自动作为已验证评分证据"
    out["financial_source"] = financial_source
    return out


def refresh_market_financials(existing, quote, extraction, history=None, known_at=None, financial_source=None):
    """Refresh repaired financial denominators without replacing historical prices."""
    # Some immutable older reports already contain a saved enriched snapshot.
    # Preserve those market fields and auxiliary facts; only recompute the
    # financial values for which we have a newly checked report or history.
    original = existing or quote or {}
    computed = build_market_snapshot(original, extraction, history, known_at, financial_source)
    if not existing:
        out = computed
    else:
        out = dict(existing)
        for key in ('pe_dynamic','dynamic_pe_period','dynamic_pe_basis','static_pe_basis','pe_dynamic_calculated',
                    'dynamic_pe_calculation_period','pe_static_calculated','eps','eps_period','roe','roe_period',
                    'gross_margin','gross_margin_period','cashflow_per_share_estimate','financial_period'):
            out[key] = computed.get(key)
        if computed.get('pe_static') is not None:
            out.update(pe_static=computed['pe_static'],static_pe_period=computed['static_pe_period'])
        for key in ('bps','capital_reserve_per_share','retained_earnings_per_share','operating_cashflow_per_share','total_shares','a_float_shares'):
            if computed.get(key) is not None:
                out[key] = computed[key]
    out['financial_checked_at'] = (extraction or {}).get('data_quality',{}).get('checked_at') or stamp()
    out['financial_refresh_note'] = '按已核对财报更新财务指标和计算分母；原行情价格、时点及来源保持原记录'
    return out
