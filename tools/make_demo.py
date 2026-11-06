#!/usr/bin/env python3
"""Regenerate clearly fictional UI fixtures; never fetches external data."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'examples/demo-site'
STAMP='2026-11-06T10:00:00+08:00'

def put(name,value):
    p=DEST/name;p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def metric(key,value,scope='single_quarter',end='2026-09-30'):
    return dict(metric=key,value=value,standard_value=value,unit='percentage' if key=='roe' else 'yuan',
                scope=scope,period_end=end,verified=True,source_page=2,
                verification_method='pdf_table_column',quote='虚构格式示例，并非真实财报证据。',demo=True)

def main():
    reports=[];tracking=[]
    dimensions=['财务质量','成长与财报变化','行业与竞争优势','估值与安全边际','热点与市场关注','交易时机']
    for i,(name,code,market,industry,score,conclusion,change) in enumerate([
        ('示例制造（虚构）','600001','SH','高端制造',72.4,'当季营收同比增长，利润环比改善；现金流与订单兑现仍需持续核对。',3.2),
        ('示例软件（虚构）','300001','SZ','工业软件',58.6,'单季扭亏并不代表全年盈利；关注毛利率修复和回款质量。',-1.8),
        ('示例材料（虚构）','688001','SH','新材料',64.1,'营收增长但单季毛利率收缩；新品放量与原料成本是后续检验重点。',.6),
    ]):
        rid=f'demo-report-{i+1}';scale=1+i*.15
        current={'revenue':191430300*scale,'net_profit':29431800*scale,'adjusted_profit':26648300*scale,
                 'operating_cashflow':33292800*scale,'inventory':129113100*scale,'roe':3.8}
        previous={'revenue':156348000*scale,'net_profit':25926600*scale,'adjusted_profit':21865100*scale,
                  'operating_cashflow':28253800*scale,'inventory':122000000*scale,'roe':3.4}
        lastyear={k:v*.8 for k,v in current.items()}
        rows=[{'metric':key,'current':metric(key,value),'previous':metric(key,previous[key],end='2026-06-30'),
               'prior_year':metric(key,lastyear[key],end='2025-09-30'),
               'previous_year_ago':metric(key,previous[key]*.85,end='2025-06-30'),
               'previous_previous':metric(key,previous[key]*.9,end='2026-03-31')}
              for key,value in current.items()]
        summary=dict(id=rid,document_id=f'demo-doc-{i+1}',company=name,code=code,market=market,industry=industry,
            period='2026Q3',date='2026-11-06',generated_at=STAMP,rating='unrated',score=score,
            score_status='provisional',score_coverage=80,score_range=[score,min(100,score+20)],
            evidence_status='insufficient',conclusion='虚构演示：'+conclusion,
            evidence='仅用于展示界面。所有金额、评分和来源核对标识均由虚构样例构造。',
            valuation='估值样例未接入真实市场数据。',risks='全部为虚构样例，不能用于投资判断。',
            provider='demo',model='fictional-fixture',test_mode=True,demo=True,
            topics=['虚构演示'],detail_url=f'./data/report-details/{rid}.json',source='https://example.com/fictional-filing')
        full={**summary,'original_interpreted_at':STAMP,'financial_analysis':{
            'metrics':[metric(k,v*2.7 if k not in ('roe','inventory') else v,'instant' if k=='inventory' else 'cumulative') for k,v in current.items()],
            'data_quality':{'verified_count':6,'required_count':6,'expected_count':6,'total':6,'status':'complete',
                'checked_at':STAMP,'note':'虚构界面样例，非真实核对','missing':[],
                'applicable_fields':{k:'instant' if k=='inventory' else 'cumulative' for k in current}},'sections':[], 'management_claims':[]},
            'financial_comparisons':{'period':'2026Q3','rows':rows,
                'previous_rows':[{'metric':row['metric'],'current':row['previous'],'prior_year':row['previous_year_ago'],
                                  'previous':row['previous_previous']} for row in rows]},
            'decision':{'rating_blockers':['虚构示例，不形成真实评级','行业与估值证据尚未接入']},
            'scoring':{'possible_range':[score,min(100,score+20)],'items':[]},
            'research_profile':{'version':'demo-v1','capture_kind':'at_generation','original_score':{'score':score},
                'current':{'dimensions':[{'name':n,'score':v,'complete':v is not None,'range':[0,100] if v is None else [v,v],
                     'coverage_pct':0 if v is None else 100,'factors':[]} for n,v in zip(dimensions,[81.5,73.0,None,62.0,None,65.1])],
                    'composites':{'short':{'complete':False,'range':[42,78],'coverage_pct':64},
                                  'medium':{'complete':False,'range':[46,82],'coverage_pct':64}},
                    'market_environment':{'label':'虚构演示','note':'未获取真实大盘行情。'},'risk_flags':[],
                    'interpretation':'所有数值为虚构样例。','enhancement_gaps':['需要使用者提供真实、独立且可核对的证据。']}}}
        put(f'data/report-details/{rid}.json',full)
        reports.append(summary)
        tracking.append(dict(report_id=rid,company=name,code=code,market=market,industry=industry,generated_at=STAMP,
            price=20+i*5,price_asof=STAMP,price_checked_at=STAMP,current_price=round((20+i*5)*(1+change/100),2),
            current_asof=STAMP,current_checked_at=STAMP,price_collected_at=STAMP,current_change_pct=change,rating='unrated',score=score,score_status='provisional',
            provenance='backfilled',provider='demo',model='fictional-fixture',test_mode=True,demo=True,
            horizons={str(n):{'state':'pending','sessions':1,'evaluable':False} for n in [3,7,15,30,60,90]}))
    put('data/reports.json',{'generated_at':STAMP,'reports':reports,'demo':True})
    put('data/filings.json',{'generated_at':STAMP,'filings':[],'demo':True})
    put('data/historical-reports.json',{'reports':[],'revision':0,'demo':True})
    put('data/tracking.json',{'generated_at':STAMP,'reports':tracking,'horizons':[3,7,15,30,60,90],
        'method':{'basis':'虚构演示数据，不代表实际收益','alignment':'解读时点与冻结记录分开保存','limits':'仅演示排序与弹窗。'},'demo':True})
    put('data/tracking-market.json',{'stocks':{},'demo':True})
    put('data/digests.json',{'daily':{'conclusion':'3份虚构示例，用于演示研究工作流。'},'weekly':{'conclusion':'示例不用于交易或回测。'}})
    put('data/research-quality.json',{'report_count':3,'provisional_count':3,'method':'虚构演示。','factors':[]})
    settings={'provider':'deepseek','model':'未配置','test_mode':False,'profiles':[],'configured':{},'enabled_count':0,
              'daily_api_calls':10,'analysis_per_run':1,'reading_mode':'deep','routing_mode':'fallback','stage_routes':{}}
    put('data/status.json',{'generated_at':STAMP,'document_count':0,'report_count':3,'queue':[],
        'settings':settings,'schedule':{'active':False},'scan':{'sources':[]},'demo':True})
    put('api/settings.json',settings)
    put('api/runtime.json',{'settings':{'enabled':False,'interval_minutes':60,'daily_api_calls':10,'analysis_per_run':1,
        'download_per_run':2,'narrative_refresh_days':0,'prefer_off_peak':False,'quarter_workers':2,'financial_reading_limit_cny':.5},
        'foreground':True,'model_time_policy':{'enabled':False,'deferred':False},'independent_python':True,'service_managed':False,
        'schedule_registered':False,'queue':[],'api_calls_today':0,'latest_run':None,'demo':True})
    put('api/watchlist.json',{'ok':True,'items':[{'company':r['company'],'code':r['code'],'market':r['market']} for r in reports[:2]]})
    put('api/sources.json',{'disclosure':[],'market':[],'demo':True})
    put('api/historical-reports.json',{'reports':[],'revision':0,'demo':True})
    put('api/securities.json',{'ok':True,'items':[],'demo':True})
    print('已生成3份虚构报告；没有网络请求。')

if __name__=='__main__':main()
