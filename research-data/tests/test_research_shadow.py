import copy
from datetime import datetime,timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stock_research import core,operating_catalysts as catalysts,research_profile as scoring,research_shadow as shadow,research_validation as validation,research_consistency as consistency
from stock_research import research_inputs as inputs

AT='2026-10-21T15:40:00+08:00'


def report():
    return {'id':'auto-case-v1','code':'600001','market':'SH','company':'样例公司','period':'2026Q3','generated_at':'2026-10-17T20:00:00+08:00',
            'disclosed_at':'2026-10-17T00:00:00+08:00','industry':'制造业','source':'https://static.cninfo.com.cn/case.pdf','financial_analysis':{}}


def fact(name,value,scope='cumulative',**extra):
    return {'metric':name,'standard_value':value,'scope':scope,'period_end':'2026-09-30','source_page':1,'fact_id':name+scope,**extra}


def series():
    # Dates use a real weekday pattern; exact holiday tests below use the
    # project's existing exchange calendar rather than available bar counts.
    start=datetime(2026,6,1)
    rows=[]
    for n in range(143):
        day=start+timedelta(days=n)
        if day.weekday()<5:
            rows.append({'date':day.date().isoformat(),'open':10+n/100,'close':10.02+n/100,'amount':2e8})
    return {'bars':rows,'adjustment':'qfq','fetched_at':AT,'source':'https://quote.eastmoney.com/','cross_validation':{'status':'matched'}}


class SixDimensionTests(unittest.TestCase):
    def build(self,facts=(),snapshot=None,r=None,**kwargs):
        with patch('stock_research.financial_facts.trusted_metrics',return_value=list(facts)):
            return scoring.profile(r or report(),snapshot or {},asof=AT,**kwargs)

    def test_missing_not_reweighted_or_ranked(self):
        p=self.build([fact('net_profit',100),fact('adjusted_profit',100)])
        q=p['dimensions'][0]
        self.assertEqual(q['coverage_pct'],30)
        self.assertEqual(q['range'],[30,100])
        self.assertIsNone(q['score'])
        self.assertFalse(p['composites']['short']['ranking_eligible'])
        self.assertEqual(sum(scoring.WEIGHTS['short'].values()),100)
        self.assertEqual(sum(scoring.WEIGHTS['medium'].values()),100)

    def test_cash_risk_not_double_deducted(self):
        p=self.build([fact('net_profit',100),fact('operating_cashflow',-20)])
        f=p['dimensions'][0]['factors'][0]
        self.assertEqual(f['score'],0)
        self.assertEqual(p['risk_flags'][0]['extra_penalty'],0)
        self.assertEqual(p['risk_flags'][0]['scored_in'],'cash_conversion')

    def test_financial_na_does_not_hide_template_gap(self):
        r=report();r['industry']='银行'
        p=self.build([fact('net_profit',100),fact('adjusted_profit',100)],r=r)
        d=p['dimensions'][0]
        self.assertEqual(d['factors'][0]['state'],'not_applicable')
        self.assertTrue(d['template_gap'])
        self.assertFalse(d['complete'])
        self.assertEqual(d['coverage_pct'],30)

    def test_future_quote_and_bars_are_excluded(self):
        snap={'prices':series(),'quote':{'asof':'2026-10-22T10:00:00+08:00','fetched_at':AT,'price':20}}
        snap['prices']['fetched_at']='2026-10-22T10:00:00+08:00'
        p=self.build(snapshot=snap)
        self.assertTrue(all(f['state']=='missing' for f in p['dimensions'][-1]['factors']))

    def test_intraday_daily_close_cannot_enter_features(self):
        p=series();at='2026-10-21T14:00:00+08:00'
        self.assertFalse(any(b['date']=='2026-10-21' for b in scoring.completed_bars(p,at)))
        self.assertTrue(any(b['date']=='2026-10-21' for b in scoring.completed_bars(p,AT)))

    def test_released_after_cutoff_not_used(self):
        r=report();r['disclosed_at']='2026-10-22T00:00:00+08:00'
        p=self.build([fact('net_profit',100),fact('adjusted_profit',100)],r=r)
        self.assertEqual(p['dimensions'][0]['coverage_pct'],0)

    def test_auxiliary_history_does_not_replace_certified_single_quarter(self):
        p=self.build(snapshot={'financial':{'history':[{'period_end':'2026-09-30','revenue':100,'revenue_yoy':50}]}})
        self.assertEqual(p['dimensions'][1]['coverage_pct'],0)

    def test_actual_single_quarter_yoy_and_scope(self):
        p=self.build([fact('revenue',500,'single_quarter',yoy_pct=30),fact('revenue',1200,'cumulative',yoy_pct=10)])
        self.assertEqual(p['dimensions'][1]['factors'][2]['value'],20)
        self.assertEqual(p['dimensions'][1]['coverage_pct'],60)

    def test_index_only_is_partial_and_not_additive(self):
        p=self.build(snapshot={'benchmark':series()})
        env=p['market_environment']
        self.assertEqual(env['state'],'partial')
        self.assertEqual(env['additive_points'],0)
        self.assertIn('全市场广度',env['missing'])

    def test_peer_dates_earnings_and_count_must_be_verified(self):
        peers=[{'code':str(600100+n),'pe_ttm':20,'industry_verified':True,'same_period':True,'price_date':'2026-10-21','earnings_verified':True,'known_at':AT,'asof':AT} for n in range(20)]
        quote={'asof':AT,'fetched_at':AT,'pe_ttm':10}
        a=self.build(snapshot={'quote':quote,'valuation_peers':peers})
        self.assertEqual(a['dimensions'][3]['factors'][0]['score'],100)
        peers[-1]['earnings_verified']=False
        b=self.build(snapshot={'quote':quote,'valuation_peers':peers})
        self.assertIsNone(b['dimensions'][3]['factors'][0]['score'])

    def test_data_sources_not_mutated(self):
        r=report();s={'prices':series()};before=copy.deepcopy((r,s))
        self.build(snapshot=s,r=r)
        self.assertEqual((r,s),before)

    def test_shadow_fields_add_no_model_facing_input(self):
        from stock_research.analysis import narrative_input
        base={'quote':{},'financial':{}}
        extra={**base,'sector_context':{'members':['600001'],'return_20d_pct':20},'sector_prices':series(),'research_source_gaps':{'unused':'missing'}}
        with patch('stock_research.analysis.stamp',return_value=AT),patch('stock_research.securities.lookup',return_value={}):
            old=narrative_input(report(),{'metrics':[]},base,{},{});new=narrative_input(report(),{'metrics':[]},extra,{},{})
        self.assertEqual(old,new)

    def test_moat_level_is_not_confused_with_direction_of_change(self):
        row={'known_at':AT,'observed_at':AT,'verification':'reviewed','reviewer':'测试复核','direction':'flat','moat_level':'strong',
             'sources':[{'url':'https://example.org/filing','quote':'测试引文','source_sha256':'a'*64}]}
        p=self.build(assessments={'company_moat':row})
        item=next(f for d in p['dimensions'] for f in d['factors'] if f['id']=='company_moat')
        self.assertEqual(item['score'],100)


class CatalystTests(unittest.TestCase):
    def test_all_checks_and_manager_claim_not_promoted(self):
        p=catalysts.discover(report(),[{'page':2,'text':'产品供不应求，订单饱满。新产品进入认证阶段。'}],asof=AT)
        self.assertEqual(len(p['checks']),6)
        self.assertEqual(p['checks'][0]['status'],'lead')
        self.assertEqual(p['checks'][0]['score_contribution'],0)
        self.assertTrue(p['checks'][0]['evidence'][0]['quote_verified'])
        self.assertFalse(p['checks'][0]['evidence'][0]['operationally_verified'])
        self.assertFalse(p['checks'][3]['exceeds_preexisting_expectation'])

    def test_risk_hypothesis_and_negation_excluded(self):
        for text in ('不存在产品供不应求','如果产品供不应求，公司将扩产','风险提示：供应紧张可能导致停产','并非需求旺盛'):
            p=catalysts.discover(report(),[{'page':1,'text':text}],asof=AT)
            self.assertTrue(all(not c['evidence'] for c in p['checks']),text)

    def test_supply_demand_duplicate_has_same_group_and_event(self):
        p=catalysts.discover(report(),[{'page':1,'text':'产品供不应求且供给偏紧、需求旺盛'}],asof=AT)
        a,b=p['checks'][0],p['checks'][-1]
        self.assertEqual(a['economic_group'],b['economic_group'])
        self.assertEqual(a['evidence'][0]['economic_event_id'],b['evidence'][0]['economic_event_id'])

    def test_old_quote_flag_without_page_not_certified(self):
        r=report();r['financial_analysis']['management_claims']=[{'quote':'供不应求','source_page':1,'quote_verified':True}]
        p=catalysts.discover(r,asof=AT)
        self.assertFalse(p['checks'][0]['evidence'][0]['quote_verified'])

    def observation(self):
        return {'signal_id':'new_product','review_status':'operating_verified','basis':'official_operating_data','reviewer':'测试复核',
                'source_url':'https://example.org/report','quote':'销量10台','source_sha256':'a'*64,
                'known_at':'2026-10-20T12:00:00+08:00','reviewed_at':AT,'materiality':'core_revenue','business_scope':'核心产品',
                'falsification':'订单取消','value':10,'unit':'台','period':'2026-09','comparison_key':'product-units'}

    def test_one_strong_signal_suffices_but_no_baseline_not_surprise(self):
        row=self.observation();p=catalysts.discover(report(),asof=AT,observations=[row])
        c=p['checks'][3]
        self.assertEqual(c['status'],'watch')
        self.assertFalse(c['sustained']);self.assertFalse(c['exceeds_preexisting_expectation'])

    def test_future_or_manager_observation_cannot_promote(self):
        for change in ({'reviewed_at':'2026-10-22T00:00:00+08:00'},{'basis':'management_claim'}):
            row={**self.observation(),**change};p=catalysts.discover(report(),asof=AT,observations=[row])
            self.assertEqual(p['checks'][3]['status'],'not_found')

    def test_baseline_must_precede_result_and_match_scope(self):
        row=self.observation();row['result_published_at']='2026-10-20T12:00:00+08:00'
        row['expectation_baseline']={'value':8,'unit':'台','period':'2026-09','business_scope':'核心产品','source_url':'https://example.org/prior','frozen_at':'2026-10-19T12:00:00+08:00'}
        self.assertTrue(catalysts.discover(report(),asof=AT,observations=[row])['checks'][3]['exceeds_preexisting_expectation'])
        row['expectation_baseline']['business_scope']='另一产品'
        self.assertFalse(catalysts.discover(report(),asof=AT,observations=[row])['checks'][3]['exceeds_preexisting_expectation'])


class ArchiveAndValidationTests(unittest.TestCase):
    def test_immutable_existing_file_not_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'snapshot.json';shadow._immutable(path,{'old':1})
            self.assertEqual(shadow._immutable(path,{'new':2}),{'old':1})
            self.assertEqual(json.loads(path.read_text()),{'old':1})

    def test_pilot_has_twenty_distinct_companies_and_is_return_independent(self):
        rows=[]
        for n in range(30):
            r=report();r.update(code=str(600001+n),id='auto-'+str(n),industry='行业'+str(n%3));rows.append(r)
        with tempfile.TemporaryDirectory() as a,tempfile.TemporaryDirectory() as b:
            first=shadow.select_pilot(rows,Path(a))
            for r in rows:r['return_pct']=999;r['score']=0
            second=shadow.select_pilot(list(reversed(rows)),Path(b))
            self.assertEqual([x['code'] for x in first['companies']],[x['code'] for x in second['companies']])
            self.assertEqual(len(first['companies']),20)

    def test_reconstructed_and_missing_scores_excluded_from_forward_stats(self):
        row={'frozen':{'capture_kind':'reconstructed','code':'600001','profile':{}},'outcomes':{'horizons':{'15':{'state':'observed'}}}}
        self.assertEqual(validation.statistics([row])['n'],0)

    def test_exact_trading_days_missing_bar_is_gap(self):
        frozen={'information_asof':'2026-10-16T20:00:00+08:00','capture_kind':'at_generation'}
        bars=[{'date':'2026-10-'+str(n),'open':10,'close':11} for n in (19,21)]
        out=validation.observations(frozen,{'adjustment':'qfq','bars':bars,'fetched_at':AT},{'bars':bars,'fetched_at':AT},asof=AT)
        self.assertEqual(out['horizons']['3']['state'],'data_gap')

    def test_cost_and_t_plus_one_path_are_explicit(self):
        frozen={'information_asof':'2026-10-16T20:00:00+08:00','capture_kind':'at_generation'}
        bars=[{'date':'2026-10-'+str(n),'open':10,'close':close} for n,close in ((19,15),(20,12),(21,11))]
        out=validation.observations(frozen,{'adjustment':'qfq','bars':bars,'fetched_at':AT},{'bars':bars,'fetched_at':AT},asof=AT)['horizons']['3']
        self.assertEqual(out['state'],'observed');self.assertEqual(out['execution_status'],'unverified')
        self.assertEqual(out['t_plus_one_max_close_gain_pct'],20)
        self.assertLess(out['cost_scenarios'][1]['net_return_pct'],out['gross_return_pct'])

    def test_spearman_ties_and_small_samples(self):
        self.assertEqual(validation.rank_correlation([(1,1),(2,2),(2,2),(3,3)]),1)
        self.assertIsNone(validation.rank_correlation([(1,2)]))


class NarrativeConsistencyTests(unittest.TestCase):
    def test_only_explicit_current_amount_corrected_and_original_preserved(self):
        r=report();r['evidence']='前三季度归母净利润12.18亿元；去年归母净利润10.01亿元。'
        with patch('stock_research.research_consistency.trusted_metrics',return_value=[fact('net_profit',1211810000)]):
            result=consistency.apply(r)
        self.assertIn('12.12亿元',result['evidence']);self.assertIn('去年归母净利润10.01亿元',result['evidence'])
        self.assertEqual(r['evidence'],'前三季度归母净利润12.18亿元；去年归母净利润10.01亿元。')

    def test_incomparable_ar_growth_marked_review_without_inventing_fact(self):
        r=report();r['evidence']='应收较期初增加15%，同比增速快于营收。'
        with patch('stock_research.research_consistency.trusted_metrics',return_value=[]):result=consistency.apply(r)
        self.assertEqual(result['narrative_consistency']['state'],'review')
        self.assertEqual(result['evidence'],r['evidence'])

    def test_negative_unverified_status_is_updated_only_for_known_fact(self):
        r=report();r['evidence']='扣非净利润尚未核实；存货未提取。'
        with patch('stock_research.research_consistency.trusted_metrics',return_value=[fact('adjusted_profit',123)]):result=consistency.apply(r)
        self.assertIn('扣非净利润已由原文核对',result['evidence']);self.assertIn('存货未提取',result['evidence'])

    def test_cumulative_scope_carries_across_semicolon_but_not_single_quarter(self):
        r=report();r['evidence']='第三季度归母净利润6.09亿元；累计前三季度营收237.99亿元；归母净利润12.18亿元。'
        with patch('stock_research.research_consistency.trusted_metrics',return_value=[fact('net_profit',1211810000),fact('net_profit',609000000,'single_quarter')]):result=consistency.apply(r)
        self.assertIn('第三季度归母净利润6.09亿元',result['evidence']);self.assertIn('归母净利润12.12亿元',result['evidence'])
        self.assertFalse(result['narrative_consistency']['findings'])

    def test_explicit_previous_year_and_carried_previous_scope_untouched(self):
        r=report();r['evidence']='2025年前三季度归母净利润10.01亿元；扣非净利润9.00亿元。2026年前三季度归母净利润12.18亿元。'
        with patch('stock_research.research_consistency.trusted_metrics',return_value=[fact('net_profit',1211810000),fact('adjusted_profit',1200000000)]):result=consistency.apply(r)
        self.assertIn('2025年前三季度归母净利润10.01亿元；扣非净利润9.00亿元',result['evidence'])
        self.assertIn('2026年前三季度归母净利润12.12亿元',result['evidence'])

    def test_scope_applies_at_amount_position_not_last_marker_in_clause(self):
        r=report();r['evidence']='第三季度归母净利润6.09亿元，前三季度归母净利润12.18亿元。'
        with patch('stock_research.research_consistency.trusted_metrics',return_value=[fact('net_profit',1211810000),fact('net_profit',609000000,'single_quarter')]):result=consistency.apply(r)
        self.assertEqual(result['evidence'],'第三季度归母净利润6.09亿元，前三季度归母净利润12.12亿元。')

    def test_sections_status_and_prior_year_title_and_literal_quote(self):
        r=report();r['financial_analysis']['sections']=[{'title':'当期财务','analysis':'累计扣非净利润123元（财报原文，待核实）；同比数据待核实。'},
            {'title':'2025年比较','analysis':'扣非净利润未提取。'},
            {'title':'原文','analysis':'“前三季度归母净利润10亿元；归母净利润11亿元。”'}]
        with patch('stock_research.research_consistency.trusted_metrics',return_value=[fact('adjusted_profit',123),fact('net_profit',1211810000)]):result=consistency.apply(r)
        sections=result['financial_analysis']['sections']
        self.assertIn('已核对原文',sections[0]['analysis']);self.assertIn('同比数据待核实',sections[0]['analysis'])
        self.assertEqual(sections[1:],r['financial_analysis']['sections'][1:])
        self.assertIn('financial_analysis.sections.0.analysis',result['original_narrative_before_consistency'])

    def test_withdraw_comparison_without_losing_other_valid_conclusions(self):
        r=report();r['conclusion']='收入利润增长，但存货与应收账款增速高于收入，减值仍需核实。';r['evidence']='应收账款较期初增长15.62%。'
        with patch('stock_research.research_consistency.trusted_metrics',return_value=[]):result=consistency.apply(r)
        self.assertIn('收入利润增长',result['conclusion']);self.assertIn('减值仍需核实',result['conclusion'])
        self.assertNotIn('增速高于收入',result['conclusion'])


class SupportingInputTests(unittest.TestCase):
    def test_official_filename_style_titles_keep_period_without_admitting_digest(self):
        from stock_research.sources import report_period
        self.assertEqual(report_period('688721_龙图光罩_2025年_年度报告'),'2025FY')
        self.assertEqual(report_period('600001_样例_2026年_第三季度报告'),'2026Q3')
        self.assertIsNone(report_period('688721_龙图光罩_2025年_年度报告摘要'))
        self.assertIsNone(report_period('关于2025年_年度报告的更正公告'))
    def test_targets_only_necessary_ttm_periods(self):
        self.assertEqual(inputs.required_periods('2026Q3'),['2026Q3','2025FY','2025Q3'])
        self.assertEqual(inputs.required_periods('2026FY'),['2026FY'])
        self.assertEqual(inputs.required_periods('invalid'),[])

    def test_supporting_download_does_not_admit_model_queue(self):
        from stock_research import sources
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);store=core.Store(root/'case.sqlite')
            self.addCleanup(store.conn.close)
            doc={'id':'1225111001','code':'600001','market':'SH','company':'样例公司','period':'2025FY','published_at':'2026-03-01T00:00:00+08:00','source_url':'https://static.cninfo.com.cn/annual.pdf','title':'2025年年度报告'}
            with patch.object(inputs,'_find',return_value=doc),patch.object(sources,'archive_pdf',return_value=('filings/a.pdf','a'*64,root/'a.pdf')),patch.object(sources,'extract_pdf',return_value=('filings/a.pdf','a'*64,'text/a.json',100)),patch('stock_research.financial_evidence.primary_metrics',return_value={'status':'checked'}):
                result=inputs.prepare_company(store,doc,'2025FY',asof=AT,vault=root/'vault')
            self.assertEqual(result['downloaded'],1)
            self.assertEqual(store.rows('SELECT * FROM documents'),[])
            self.assertEqual(store.rows('SELECT * FROM api_calls'),[])

    def test_intraday_fetch_is_reused_but_final_close_gets_one_refresh(self):
        company={'code':'600001','market':'SH'}
        with tempfile.TemporaryDirectory() as d,patch('stock_research.sources.daily_bars',return_value=series()) as call:
            v=Path(d)
            self.assertEqual(inputs._price(company,'2026-10-21T10:00:00+08:00',v)['state'],'updated')
            self.assertEqual(inputs._price(company,'2026-10-21T11:00:00+08:00',v)['state'],'cached')
            self.assertEqual(inputs._price(company,AT,v)['state'],'updated')
            self.assertEqual(inputs._price(company,'2026-10-21T16:00:00+08:00',v)['state'],'cached')
            self.assertEqual(call.call_count,2)

    def test_three_consecutive_source_failures_open_circuit(self):
        company={'code':'600001','market':'SH'}
        with tempfile.TemporaryDirectory() as d,patch('stock_research.sources.daily_bars',side_effect=core.ServiceError('network','test')) as call:
            for _ in range(3):inputs._price(company,AT,Path(d))
            self.assertEqual(inputs._price(company,AT,Path(d))['state'],'circuit_open')
            self.assertEqual(call.call_count,3)


if __name__=='__main__':unittest.main()
