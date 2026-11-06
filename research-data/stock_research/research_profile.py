"""Pure, point-in-time six-dimensional shadow research contract.

No network or model calls. Unknowns stay unknown, not neutral or renormalized.
Old buy-score and original reports are never mutated by this module.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from statistics import mean, pstdev
import math
import hashlib
from pathlib import Path

from .core import finite, TZ
from .scorecard import scale

VERSION = 'research-six-v1'
IMPLEMENTATION = hashlib.sha256(b''.join(Path(__file__).with_name(name).read_bytes() for name in ('research_profile.py','operating_catalysts.py','research_validation.py'))).hexdigest()
DIMENSIONS = (
    ('quality', '财务质量'), ('growth', '成长与财报变化'),
    ('industry', '行业与竞争优势'), ('valuation', '估值与安全边际'),
    ('attention', '热点与市场关注'), ('timing', '交易时机'),
)
WEIGHTS = {'short': dict(zip([d[0] for d in DIMENSIONS], (15,25,10,10,20,20))),
           'medium': dict(zip([d[0] for d in DIMENSIONS], (25,25,20,20,5,5)))}
# A compact initial base set, not a claim that missing enrichment is known.
CATALOG = (
    ('cash_conversion','quality','现金利润匹配',40,'ratio','财报累计经营现金流/归母净利润','financial'),
    ('leverage','quality','负债约束',30,'percentage','合并总负债/总资产','financial'),
    ('recurring_share','quality','经常性利润占比',30,'percentage','扣非净利润/归母净利润；非正利润单列风险','financial'),
    ('quarter_revenue','growth','单季度营业收入同比',40,'percentage','已核实原文单季同比；负基期不用于增速排序','financial'),
    ('quarter_adjusted','growth','单季度扣非利润同比',40,'percentage','已核实原文单季同比；负基期不用于增速排序','financial'),
    ('growth_acceleration','growth','季度增长变化',20,'percentage_points','单季营收同比−年初至今营收同比（均须同口径核实）','financial'),
    ('industry_demand','industry','行业长期需求',35,'ordinal','独立资料人工核实：下降/稳定/上升','structural'),
    ('industry_cycle','industry','行业供需景气',35,'ordinal','至少两条近期原文匹配的外部资料','industry'),
    ('company_moat','industry','竞争与公司壁垒',30,'ordinal','公司壁垒强/一般/弱；须有竞争和公司地位证据，不按改善方向代替壁垒水平','structural'),
    ('peer_valuation','valuation','可比同行估值',50,'percentile','同日期、同一分类体系、≥20家独立可比公司','market'),
    ('fcf_yield','valuation','已核实TTM自由现金流收益率',50,'percentage','(TTM经营现金流−资本开支)/市值','market'),
    ('sector_strength','attention','行业相对强度',50,'percentage_points','可核实行业指数20日涨幅−基准20日涨幅','market'),
    ('sector_breadth','attention','行业上涨广度',30,'percentage','固定行业成份中当日上涨比例；覆盖至少70%；停牌或无报价计覆盖缺失','market'),
    ('sector_activity','attention','行业成交活跃度',20,'ratio','行业成交额/过去20个完整交易日均值','market'),
    ('trend','timing','个股趋势与位置',40,'ordinal','20日均价相对60日均价、最新完整日价格相对20日均价','market'),
    ('entry_location','timing','距20日均线位置',30,'percentage','回调/过热的位置分档，非自动买点','market'),
    ('liquidity','timing','成交与波动风险',30,'ratio','20日成交额与日收益波动，交易活跃度不等于看涨','market'),
)
ENRICHMENT = ('应收存货同口径周转', '毛利率变化', '一致预期及预告差', '订单交付与利润兑现', '自身历史估值', '主题真实收入关联', '跳空与涨跌停可成交性', '金融行业专用质量模板')
TEMPLATES = {
    'financial': timedelta(days=160), 'structural': timedelta(days=365),
    'industry': timedelta(days=90), 'market': timedelta(days=5),
}


def moment(value):
    try:
        result = datetime.fromisoformat(str(value))
        return result.astimezone(TZ) if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def qualified(source, cutoff, ttl='market'):
    """Known-time and observed-time guards prevent retrospective leakage."""
    at = moment(cutoff)
    known = moment((source or {}).get('known_at') or (source or {}).get('fetched_at'))
    observed = moment((source or {}).get('observed_at') or (source or {}).get('asof') or (source or {}).get('fetched_at'))
    if not at or not known or not observed:
        return 'missing'
    if known > at or observed > at:
        return 'future'
    check = (source or {}).get('cross_validation') or {}
    if check.get('status') == 'conflict' and ((source or {}).get('price_resolution') or {}).get('status') != 'resolved':
        return 'conflict'
    if at - observed > TEMPLATES[ttl]:
        return 'stale'
    return 'known'


def completed_bars(prices, asof):
    at = moment(asof)
    if not at:
        return []
    # Closing bars observed intraday may still be moving; exclude them until
    # the existing final market-collection slot has passed.
    last = at.date().isoformat()
    fetched = moment((prices or {}).get('fetched_at'))
    today_closed = at.time() >= time(15,35) and bool(fetched and (fetched.date()>at.date() or fetched.date()==at.date() and fetched.time()>=time(15,35)))
    return sorted({b['date']:b for b in (prices or {}).get('bars', [])
                   if isinstance(b.get('date'),str) and (b['date'] < last or b['date'] == last and today_closed)
                   and finite(b.get('close')) is not None and b['close'] > 0}.values(), key=lambda b:b['date'])


def _change(a,b):
    return (a/b-1)*100 if finite(a) is not None and finite(b) is not None and b>0 else None


def _external(assessments, ident, asof, ttl):
    row = (assessments or {}).get(ident) or {}
    if not row:
        return None, 'missing', '缺少核实的独立资料及结构化评价'
    status = qualified(row, asof, ttl)
    if status != 'known':
        return None, 'missing' if status=='future' else status, '资料时点不可用或尚未在当时取得'
    mapping={'strong':100,'mixed':50,'weak':0} if ident=='company_moat' else {'up':100,'flat':50,'down':0}
    selected=row.get('moat_level') if ident=='company_moat' else row.get('direction')
    if row.get('verification') != 'reviewed' or not row.get('reviewer') or selected not in mapping:
        return None, 'missing', '尚无可追溯人工证据复核'
    refs = row.get('sources') or []
    if not refs or any(not r.get('quote') or not r.get('url','').startswith('https://') or not r.get('source_sha256') for r in refs):
        return None, 'missing', '来源缺引文、链接或正文哈希'
    return mapping[selected], 'known', ''


def profile(report, snapshot, *, asof, assessments=None):
    from .financial_facts import trusted_metrics
    facts = list(trusted_metrics(report.get('financial_analysis') or {}))
    end = str(report.get('period',''))[:4]+'-'+{'FY':'12-31','Q1':'03-31','H1':'06-30','Q3':'09-30'}.get(str(report.get('period',''))[4:],'invalid')
    # A current report's metrics cannot appear before its disclosure. Evidence
    # repaired later is explicitly tagged reconstructed by the storage layer.
    released = moment(report.get('disclosed_at') or report.get('generated_at'))
    cutoff = moment(asof)
    if not released or not cutoff or released > cutoff:
        facts = []
    factmap = {(m['metric'],m['scope']):m for m in facts if m.get('period_end')==end}
    def fact(name, scope='cumulative'):
        return factmap.get((name,scope),{})
    def value(name, scope='cumulative'):
        return finite(fact(name,scope).get('standard_value'))
    quote = snapshot.get('quote') or {}
    prices = snapshot.get('prices') or {}
    qstate = qualified(quote,asof)
    pstate = qualified(prices,asof)
    if snapshot.get('research_prices_conflict'):pstate='conflict'
    bars = completed_bars(prices,asof) if pstate=='known' else []
    if bars and (cutoff.date()-datetime.fromisoformat(bars[-1]['date']).date()).days>5:
        bars=[];pstate='stale'
    company_financial = (report.get('financial_analysis') or {}).get('data_quality',{}).get('profile') in ('bank','insurance','securities') or any(w in str(report.get('industry','')) for w in ('银行','保险','证券'))
    items = []
    def add(ident, raw=None, score=None, *, state=None, reason='', evidence=None):
        spec = next(c for c in CATALOG if c[0]==ident)
        v = finite(score)
        status = state or ('known' if v is not None else 'missing')
        if status != 'known':v=None
        items.append({'id':ident,'dimension':spec[1],'name':spec[2],'weight':spec[3], 'unit':spec[4],'formula':spec[5],
                      'state':status,'value':raw,'score':round(max(0,min(100,v)),3) if v is not None else None,
                      'reason':reason if status!='known' else '', 'evidence':evidence or [],'asof':asof,'ttl_class':spec[6]})
    def refs(*names):
        return [{'metric':name,'scope':scope,'period_end':fact(name,scope).get('period_end'),'page':fact(name,scope).get('source_page'),
                 'fact_id':fact(name,scope).get('fact_id'),'yoy_fact_id':fact(name,scope).get('yoy_fact_id'),'source':report.get('source')} for name,scope in names if fact(name,scope)]
    profit,cash=value('net_profit'),value('operating_cashflow')
    cash_ratio=cash/profit if cash is not None and profit is not None and profit>0 else None
    no_cash = company_financial
    add('cash_conversion',cash_ratio,scale(cash_ratio,[(-.5,0),(0,0),(.5,35),(1,80),(1.2,100)]) if not no_cash else None,
        state='not_applicable' if no_cash else None,reason='金融行业现金转换不适用' if no_cash else '缺少同口径已核实现金流及正利润基数',evidence=refs(('net_profit','cumulative'),('operating_cashflow','cumulative')))
    assets,liab=value('total_assets','instant'),value('total_liabilities','instant')
    leverage=liab/assets*100 if assets and assets>0 and liab is not None else None
    add('leverage',leverage,scale(leverage,[(20,100),(40,80),(60,50),(80,0)]) if not company_financial else None,
        state='not_applicable' if company_financial else None,reason='金融行业需专用杠杆模型' if company_financial else '缺少已核实合并资产负债',evidence=refs(('total_assets','instant'),('total_liabilities','instant')))
    adj=value('adjusted_profit');ratio=adj/profit*100 if adj is not None and profit is not None and profit>0 else None
    add('recurring_share',ratio,scale(ratio,[(0,0),(50,40),(80,80),(100,100)]),reason='缺少已核实扣非和正利润基数',evidence=refs(('adjusted_profit','cumulative'),('net_profit','cumulative')))
    # Only a physically certified summary column supplies single-quarter YOY;
    # the auxiliary, unverified history cannot silently substitute for it.
    rev=finite(fact('revenue','single_quarter').get('yoy_pct'))
    adj_yoy=finite(fact('adjusted_profit','single_quarter').get('yoy_pct'))
    add('quarter_revenue',rev,scale(rev,[(-20,0),(0,40),(10,60),(25,80),(50,100)]),reason='缺少原文已核实的单季营收同比',evidence=refs(('revenue','single_quarter')))
    add('quarter_adjusted',adj_yoy,scale(adj_yoy,[(-20,0),(0,40),(15,60),(30,80),(60,100),(150,100)]),reason='缺少原文已核实的单季扣非同比',evidence=refs(('adjusted_profit','single_quarter')))
    cumulative_yoy=finite(fact('revenue').get('yoy_pct'))
    accel=rev-cumulative_yoy if rev is not None and cumulative_yoy is not None else None
    add('growth_acceleration',accel,scale(accel,[(-20,0),(0,50),(20,100)]),reason='缺少已核实的单季与累计同口径同比',evidence=refs(('revenue','single_quarter'),('revenue','cumulative')))
    for ident in ('industry_demand','industry_cycle','company_moat'):
        ttl='industry' if ident=='industry_cycle' else 'structural'
        score,state,reason=_external(assessments,ident,asof,ttl)
        row=(assessments or {}).get(ident,{})
        # Cycle direction needs two distinct original articles, not syndication.
        if ident=='industry_cycle' and score is not None and len({r.get('source_sha256') for r in row.get('sources',[])})<2:
            score=None;state='missing';reason='需至少两份近期独立原文'
        add(ident,row.get('moat_level') if ident=='company_moat' else row.get('direction'),score,state=state,reason=reason,evidence=row.get('sources',[]))
    peers=snapshot.get('valuation_peers') or []
    same_date=str(quote.get('asof',''))[:10]
    usable={p.get('code'):p for p in peers if p.get('code')!=report.get('code') and p.get('industry_verified') and p.get('same_period')
            and p.get('price_date')==same_date and p.get('earnings_verified') and qualified(p,asof)=='known' and finite(p.get('pe_ttm')) is not None and p['pe_ttm']>0}
    pe=finite(quote.get('pe_ttm'));rank=None
    if len(usable)>=20 and pe is not None and pe>0 and qstate=='known':
        rank=sum((p['pe_ttm']>pe)+.5*(p['pe_ttm']==pe) for p in usable.values())/len(usable)*100
    add('peer_valuation',{'pe_ttm':pe,'peers':len(usable)},rank,reason='缺少≥20家同日期、同分类且盈利已核实的独立同行',evidence=[{'source':quote.get('source'),'asof':quote.get('asof')}])
    ttm=snapshot.get('ttm_cashflow_evidence') or {}
    cap=finite(quote.get('market_cap'));ocf=finite(snapshot.get('verified_ttm_cashflow'));capex=finite(snapshot.get('verified_ttm_capex'))
    fcf=(ocf-capex)/cap*100 if qstate=='known' and ttm.get('status')=='verified' and all(v is not None for v in (cap,ocf,capex)) and cap>0 and capex>=0 else None
    add('fcf_yield',fcf,scale(fcf,[(-2,0),(0,20),(3,50),(6,80),(10,100)]) if not company_financial else None,
        state='not_applicable' if company_financial else None,reason='金融行业需专用估值模型' if company_financial else '缺少已核实TTM经营现金流、资本开支或同期市值',evidence=ttm.get('source_documents',[]))
    sector=snapshot.get('sector_context') or {};sector_state=qualified(sector,asof)
    valid_sector=sector_state=='known' and sector.get('membership_verified') and sector.get('sample_coverage_pct',0)>=70 and sector.get('member_count',0)>=5
    for ident,key,anchors in (
        ('sector_strength','relative_return_20d_pp',[(-10,0),(0,50),(10,100)]),
        ('sector_breadth','advancing_pct',[(20,0),(50,50),(80,100)]),
        ('sector_activity','turnover_ratio',[(.5,0),(1,50),(2,100)])):
        raw=finite(sector.get(key)) if valid_sector else None
        add(ident,raw,scale(raw,anchors),state=sector_state if sector_state in ('stale','conflict') else None,
            reason='缺少同期核实的行业成份及足够覆盖的量价数据；不能用自选样本冒充全行业',evidence=[{'source':sector.get('source'),'asof':sector.get('asof')}])
    closes=[b['close'] for b in bars];trend=None;location=None;liq=None
    if len(closes)>=60:
        ma20,ma60=mean(closes[-20:]),mean(closes[-60:])
        trend=50*(ma20>ma60)+50*(closes[-1]>ma20)
        location=_change(closes[-1],ma20)
        amounts=[finite(b.get('amount')) for b in bars[-20:]]
        if all(v is not None and v>=0 for v in amounts):
            vol=pstdev([math.log(closes[i]/closes[i-1]) for i in range(len(closes)-19,len(closes))])*100
            liq={'average_amount_yuan':mean(amounts),'daily_volatility_pct':vol}
    add('trend',trend,trend,state=pstate if pstate in ('stale','conflict') else None,reason='需60个完整交易日日线，且时点/价格核对无冲突',evidence=[{'source':prices.get('source'),'last_date':bars[-1]['date'] if bars else None}])
    loc_score=scale(location,[(-20,0),(-5,65),(0,100),(5,80),(15,20),(25,0)])
    add('entry_location',location,loc_score,state=pstate if pstate in ('stale','conflict') else None,reason='缺少完整日线，不能计算偏离均线程度')
    liquidity_score=min(scale(liq['average_amount_yuan'],[(0,0),(2e7,30),(1e8,80),(3e8,100)]),scale(liq['daily_volatility_pct'],[(0,100),(2,85),(5,40),(10,0)])) if liq else None
    add('liquidity',liq,liquidity_score,state=pstate if pstate in ('stale','conflict') else None,reason='缺少至少20个完整交易日的成交额或波动数据')
    dimensions=[]
    for ident,name in DIMENSIONS:
        selected=[x for x in items if x['dimension']==ident]
        if released and cutoff and cutoff-released>TEMPLATES['financial']:
            for factor in selected:
                if factor['ttl_class']=='financial' and factor['state']!='not_applicable':
                    factor.update(state='stale',score=None,reason='财报已超过本版160天有效期，需最新财报或明确延续核实')
        applicable=sum(x['weight'] for x in selected if x['state']!='not_applicable')
        template_gap_weight=(100-applicable) if company_financial and ident in ('quality','valuation') else 0
        applicable+=template_gap_weight  # Missing bank/insurer replacements stay unknown.
        known=sum(x['weight'] for x in selected if x['state']=='known')
        # N/A portions are genuinely excluded. Unknown portions retain their
        # weight; we never stretch the known subtotal to an apparent full score.
        points=sum(x['weight']*x['score']/100 for x in selected if x['state']=='known')
        low=points/applicable*100 if applicable else None
        high=(points+applicable-known)/applicable*100 if applicable else None
        complete=applicable>0 and known==applicable and not (company_financial and ident in ('quality','valuation'))
        dimensions.append({'id':ident,'name':name,'score':round(low,2) if complete else None,
                           'range':[round(low,2),round(high,2)] if applicable else None,
                           'known_contribution':round(low,2) if low is not None else None,
                           'coverage_pct':round(known/applicable*100,2) if applicable else 0,
                           'complete':complete,'state':'complete' if complete else 'incomplete', 'factors':selected,
                           'unimplemented_applicable_weight':template_gap_weight,
                           'template_gap': '金融行业专用模型尚未完成' if company_financial and ident in ('quality','valuation') else None})
    risks=[]
    if profit is not None and profit>0 and cash is not None and cash<0:risks.append({'id':'negative_cash_conversion','label':'正利润与负经营现金流并存，需核查季节性','scored_in':'cash_conversion','extra_penalty':0})
    if profit is not None and profit<=0:risks.append({'id':'nonpositive_profit','label':'归母净利润非正','scored_in':None,'extra_penalty':0})
    opinion=(report.get('financial_analysis') or {}).get('audit_opinion','')
    if any(w in opinion for w in ('否定意见','无法表示意见','持续经营重大不确定')) or ('保留意见' in opinion and '无保留意见' not in opinion):
        risks.append({'id':'audit_risk','label':'审计或持续经营重大风险','blocks_priority':True,'extra_penalty':0})
    if (report.get('financial_analysis') or {}).get('data_quality',{}).get('conflicts'):
        risks.append({'id':'financial_conflict','label':'财务来源口径存在冲突','blocks_priority':True,'extra_penalty':0})
    composites={}
    for horizon,weights in WEIGHTS.items():
        lower=sum((d['range'] or [0,100])[0]*weights[d['id']]/100 for d in dimensions)
        upper=sum((d['range'] or [0,100])[1]*weights[d['id']]/100 for d in dimensions)
        coverage=sum(d['coverage_pct']*weights[d['id']]/100 for d in dimensions)
        complete=all(d['complete'] for d in dimensions)
        composites[horizon]={'score':round(lower,2) if complete else None,'range':[round(lower,2),round(upper,2)],
                              'coverage_pct':round(coverage,2),'complete':complete,
                              'ranking_eligible':complete and not any(r.get('blocks_priority') for r in risks),'weights':weights}
    return {'version':VERSION,'implementation_sha256':IMPLEMENTATION,'mode':'shadow','asof':asof,'dimensions':dimensions,'composites':composites,
            'risk_flags':risks,'market_environment':market_environment(snapshot.get('benchmark') or {},asof),
            'enhancement_gaps':list(ENRICHMENT),'interpretation':'试运行研究规则；未知保留区间，不与完整分混排；覆盖率不是准确率或胜率，风险不因热点高分消失。'}


def market_environment(benchmark,asof):
    state=qualified(benchmark,asof)
    bars=completed_bars(benchmark,asof) if state=='known' else []
    if len(bars)<60:
        return {'state':'missing' if state not in ('stale','conflict') else state,'label':'市场环境待补齐','additive_points':0,
                'missing':['需至少60个同期完整指数日线','全市场广度','市场成交活跃度','涨跌停及风格指数']}
    close=[b['close'] for b in bars];ma20=mean(close[-20:]);ma60=mean(close[-60:])
    strong=close[-1]>ma20>ma60;weak=close[-1]<ma20<ma60
    return {'state':'partial','label':'指数趋势偏强' if strong else '指数趋势偏弱' if weak else '指数趋势中性',
            'benchmark_return_20d_pct':round(_change(close[-1],close[-21]),3),'last_date':bars[-1]['date'],'source':benchmark.get('source'),
            'missing':['全市场广度','市场成交活跃度','涨跌停及风格指数'],'additive_points':0,
            'note':'仅已取得的指数趋势，不能冒充完整大盘环境分；不向个股统一加减分。'}
