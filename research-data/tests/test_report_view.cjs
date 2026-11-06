const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const context={};vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../../stock-site/report-view.js'),'utf8'),context);
const view=context.StockReportView;

test('verified quarter supplement is visible inside core conclusion and preserves original text',()=>{
  const report={conclusion:'前三季度同比增长，现金流强劲。',date:'2026-10-22'};
  const html=view.render(report,{quarter_review:{summary:'三季度：归母净利润环比下降24.36%；毛利率由48.37%降至41.53%。',asof:'2026-10-23T11:00:00+08:00'},financial_analysis:{metrics:[]}});
  const core=html.slice(html.indexOf('aria-label="核心结论"'),html.indexOf('关键财务指标'));
  assert.ok(core.includes('前三季度同比增长'));assert.ok(core.includes('环比下降24.36%'));
  assert.ok(core.includes('48.37%降至41.53%'));assert.ok(core.includes('补充核对'));
  assert.ok(core.includes('核对时间：'));assert.ok(!core.includes('不及市场预期'));
});

test('missing quarter data is explicit and supplement cannot inject HTML',()=>{
  const html=view.render({conclusion:'原结论'},{quarter_review:{summary:'',missing:['net_profit']},financial_analysis:{metrics:[]}});
  assert.ok(html.includes('不能仅凭累计同比判断盈利持续改善'));
  const escaped=view.render({},{quarter_review:{summary:'<img src=x>',asof:'2026-10-23'},financial_analysis:{metrics:[]}});
  assert.ok(escaped.includes('&lt;img'));assert.ok(!escaped.includes('<img'));
});

test('report heading metadata uses full industry safely and footnote no longer repeats it',()=>{
  const html=view.headingMetadata({industry:'旧行业',period:'2026Q3'},{industry:'智能照明与磁性器件制造'});
  assert.ok(html.includes('行业：智能照明与磁性器件制造'));assert.ok(!html.includes('报告期：'));
  assert.ok(!html.includes('旧行业'));
  assert.ok(view.headingMetadata({industry:'<img src=x onerror=1>',period:'2025FY'}).includes('&lt;img'));
  assert.ok(view.ratingSummary({period:'2025FY'}).includes('报告期：2025年报'));
  assert.ok(view.headingMetadata({}).includes('行业：待核实'));
  const body=view.render({industry:'智能照明与磁性器件制造',date:'2026-10-22'},{financial_analysis:{metrics:[]}});
  assert.ok(!body.slice(body.indexOf('class="source-footnote"')).includes('行业：'));
});

test('compact assessment orders score then rating, period then coverage without changing score bounds',()=>{
  const html=view.ratingSummary({score:11,score_status:'provisional',score_coverage:75,score_range:[11,36],rating:'unrated',period:'2026Q3'});
  assert.ok(html.includes('>评分</span>'));assert.ok(!html.includes('原规则买入评分'));
  assert.ok(html.indexOf('11.0')<html.indexOf('>评级</span>'));
  assert.ok(html.indexOf('report-assessment-rating')<html.indexOf('报告期：2026Q3'));
  assert.ok(html.indexOf('报告期：2026Q3')<html.indexOf('覆盖 75%'));
  assert.ok(html.includes('11.0–36.0'));assert.ok(html.includes('未评级'));
});

test('compact historical summaries load the full evidence with clear historical context and retained versions',()=>{
  const report={report_kind:'historical_foundation',review_state:'needs_review',period:'2025FY',date:'2026-10-19',conclusion:'底稿摘要',review_versions:[{id:'flash-draft',role:'flash_deep'},{id:'pro-review',role:'pro_review'}]};
  const html=view.render(report,{evidence:'精读详细支持证据',risks:'精读详细风险',financial_analysis:{sections:[],metrics:[]}});
  assert.ok(html.includes('历史补建底稿'));assert.ok(html.includes('不计入正式方向吻合率'));
  assert.ok(html.includes('仍存在待复核项'));assert.ok(html.includes('精读详细支持证据'));
  assert.ok(html.includes('精读详细风险'));assert.ok(html.includes('report=flash-draft'));
  assert.ok(html.includes('report=pro-review'));assert.ok(html.includes('补建解读日期'));
  assert.ok(html.indexOf('核心结论')<html.indexOf('report-processing-notes'));
  assert.ok(html.indexOf('关键财务指标')<html.indexOf('report-processing-notes'));
});

test('rating and core conclusion precede metrics, with explicit assessment time',()=>{
  const report={rating:'unrated',score:40,score_status:'provisional',score_coverage:55,evidence_status:'insufficient',conclusion:'未评级。营收增长，现金流需要复核。',date:'2026-10-17'};
  const html=view.render(report,{assessment_asof:'2026-10-18',financial_analysis:{metrics:[],sections:[]},decision:{missing:[]}});
  assert.ok(html.indexOf('研究评级')<html.indexOf('核心结论'));
  assert.ok(html.indexOf('核心结论')<html.indexOf('关键财务指标'));
  assert.ok(html.includes('评分与规则判断更新：2026-10-18'));
  assert.ok(html.includes('下文文字保留原解读时点'));
});

test('unverified metric values never become headline financial facts',()=>{
  const html=view.metricCards({financial_analysis:{metrics:[{metric:'revenue',standard_value:987654321,verified:false,scope:'cumulative',unit:'yuan'}]}});
  assert.ok(html.includes('核对未通过'));
  assert.ok(!html.includes('9.88'));
});

test('an explicit source not-applicable value is distinct from extraction failure',()=>{
  const html=view.metricCards({financial_analysis:{metrics:[],data_quality:{missing:[
    {metric:'roe',scope:'cumulative',status:'source_not_applicable',source_pages:[7],reason:'原文对应期间明确标注不适用，不能填零或用其他期间替代'}
  ]}}});
  assert.ok(html.includes('原文不适用'));
  assert.ok(html.includes('不能填零或用其他期间替代'));
  assert.ok(!html.includes('>0.00%<'));
});

test('model text is escaped and non-web source URLs are not links',()=>{
  const report={conclusion:'<img src=x onerror=alert(1)>',industry:'<script>x</script>',source:'javascript:alert(1)',risks:'<iframe src=x>',date:'2026-10-17'};
  const html=view.render(report,{financial_analysis:{sections:[],metrics:[]},decision:{missing:[]}});
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<iframe'));
  assert.ok(!html.includes('href="javascript:'));
  assert.ok(html.includes('&lt;img'));
});

test('core snippet removes old mode and rating prefixes without deleting facts',()=>{
  assert.equal(view.brief('测试研究 · 证据不足。营收增长但现金流为负。\n关键缺口：估值未核实'),'营收增长但现金流为负。');
});

test('compact financial cards preserve scope, negative warning, growth and original page',()=>{
  const html=view.metricCards({financial_analysis:{metrics:[{metric:'operating_cashflow',value:-41.62,standard_value:-4162000000,verified:true,scope:'cumulative',unit:'yuan',source_page:1,verification_method:'pdf_table_column',yoy_pct:-254.05}]}});
  assert.ok(html.includes('financial-kpis'));
  assert.ok(html.includes('-41.62'));
  assert.ok(html.includes('经营现金流 · 负值'));
  assert.ok(html.includes('-254.05%'));
  assert.ok(html.includes('finance-down'));
  assert.ok(html.includes('累计 · 已核对 · 第1页'));
  assert.ok(html.includes('表格行列已核对；年初至报告期末'));
});

test('negative profit bases show economic direction rather than a misleading positive growth',()=>{
  for(const [now,before,text,tone] of [[-200,-100,'亏损扩大 100.00%','finance-down'],[-50,-100,'减亏 50.00%','finance-up'],[20,-100,'扭亏为盈','finance-up'],[-20,100,'由盈转亏 −120.00%','finance-down'],[0,-100,'亏损归零','finance-up'],[-20,0,'由零转亏','finance-down'],[20,0,'由零转盈','finance-up'],[0,0,'持平','finance-flat'],[120,100,'+20.00%','finance-up'],[80,100,'−20.00%','finance-down']]){
    const change=view.financialChange(now,before,'net_profit');
    assert.equal(change.text,text);assert.equal(change.tone,tone);
  }
  assert.equal(view.financialChange(null,100,'net_profit'),null);
  assert.equal(view.financialChange(NaN,100,'net_profit'),null);
  assert.equal(view.financialChange(-200,-100,'operating_cashflow').text,'净流出扩大 100.00%');
});

test('quarter profits remain visible next to YTD loss and missing previous quarters stay missing',()=>{
  const html=view.metricCards({period:'2026Q3',financial_analysis:{metrics:[
    {metric:'net_profit',standard_value:-5495.6,scope:'cumulative',verified:true,unit:'yuan',source_page:2},
    {metric:'net_profit',standard_value:29431752.01,scope:'single_quarter',verified:true,unit:'yuan',source_page:2,yoy_pct:1482.18}
  ]}});
  for(const text of ['当季指标','2,943.18 万元','当季盈利','2026Q3','2026Q2','2025Q3','环比','待补齐','-0.55','年初至今累计'])assert.ok(html.includes(text),text);
  assert.ok(!html.includes('半年累计即上季'));
});

test('current previous and annual use six aligned compact cards and keep change order',()=>{
  const full={period:'2026Q3',financial_analysis:{metrics:[
    {metric:'revenue',standard_value:100,scope:'single_quarter',verified:true,unit:'yuan'},
    {metric:'roe',standard_value:1.43,scope:'single_quarter',verified:true,unit:'percentage'},
    {metric:'inventory',standard_value:100,scope:'instant',verified:true,unit:'yuan'}
  ]}};
  const quarter=view.quarterCards(full),html=view.metricCards(full);
  assert.equal((quarter.match(/data-metric=/g)||[]).length,6);
  assert.equal((html.match(/data-metric=/g)||[]).length,18);
  assert.ok(quarter.indexOf('kpi-value')<quarter.indexOf('同比'));
  assert.ok(quarter.indexOf('同比')<quarter.indexOf('环比'));
  assert.ok(quarter.includes('finance-flat'));assert.ok(quarter.includes('1.43%'));
  assert.ok(!quarter.includes('<table'));assert.ok(html.includes('单季对比金额与计算依据'));
  const annual=html.slice(html.indexOf('cumulative-kpis'),html.indexOf('单季对比金额与计算依据'));
  assert.ok(!annual.includes('1.43%'));
});

test('Jinlong positive revenue with negative YOY is green in quarter and cumulative cards',()=>{
  const full={period:'2026Q3',financial_analysis:{metrics:[
    {metric:'revenue',standard_value:60867816.37,scope:'single_quarter',verified:true,unit:'yuan',yoy_pct:-73.05},
    {metric:'revenue',standard_value:410194388.25,scope:'cumulative',verified:true,unit:'yuan',yoy_pct:-36.21}
  ]}};
  const quarter=view.quarterCards(full),html=view.metricCards(full);
  assert.match(quarter,/data-metric="revenue"[\s\S]*?<strong class="kpi-value finance-down/);
  assert.match(html.slice(html.indexOf('cumulative-kpis')),/data-metric="revenue"[\s\S]*?<strong class="kpi-value finance-down/);
});

test('headline color follows YOY first, QOQ second, and unknown evidence remains neutral',()=>{
  const m=(value)=>({standard_value:value,verified:true,unit:'yuan'});
  assert.equal(view.financialValueTone(m(80),m(100),'revenue',undefined,m(50)),'finance-down');
  assert.equal(view.financialValueTone(m(120),m(100),'revenue',undefined,m(150)),'finance-up');
  assert.equal(view.financialValueTone(m(80),null,'revenue',undefined,m(100)),'finance-down');
  assert.equal(view.financialValueTone(m(80),null,'revenue',undefined,null),'finance-flat');
  assert.equal(view.financialValueTone(m(-50),m(-100),'net_profit'),'finance-down');
  assert.equal(view.financialValueTone(m(-200),m(-100),'net_profit'),'finance-down');
  assert.equal(view.financialValueTone(m(80),{...m(10),verified:false},'revenue',-20),'finance-down');
  assert.equal(view.financialValueTone({...m(80),verified:false},m(100),'revenue',-20),'finance-flat');
  assert.equal(view.financialValueTone({...m(-50),verified:false},m(-100),'net_profit',50),'finance-flat');
  assert.equal(view.financialValueTone(m(-50),null,'operating_cashflow',undefined,null),'finance-down');
  assert.equal(view.financialValueTone(m(0),m(-100),'net_profit',100),'finance-flat');
});

test('Zhonglan loss amounts remain green while verified loss narrowing stays red',()=>{
  const fact=(metric,value)=>({metric,standard_value:value,verified:true,scope:'single_quarter',unit:'yuan'});
  const net=fact('net_profit',-15067831.71),adjusted=fact('adjusted_profit',-4521883.81);
  const full={period:'2026Q3',financial_analysis:{metrics:[net,adjusted]},financial_comparisons:{rows:[
    {metric:'net_profit',current:net,prior_year:fact('net_profit',-26259111.25),previous:fact('net_profit',-16634541.92)},
    {metric:'adjusted_profit',current:adjusted,prior_year:fact('adjusted_profit',-34150297.82),previous:fact('adjusted_profit',-11833716.32)}
  ]}};
  const html=view.quarterCards(full);
  for(const metric of ['net_profit','adjusted_profit'])assert.match(html,new RegExp('data-metric="'+metric+'"[\\s\\S]*?<strong class="kpi-value finance-down'));
  assert.ok(html.includes('financial-change finance-up">减亏 42.62%'));
  assert.ok(html.includes('financial-change finance-up">减亏 86.76%'));
  const detail=view.quarterCards(full,'details');
  assert.match(detail,/quarter-amount finance-down[^>]*>\-1,506\.78 万元/);
  assert.match(detail,/quarter-amount finance-down[^>]*>\-1,663\.45 万元/);
});

test('percentage metric changes remain percentage points when sharing comparison colors',()=>{
  const current={metric:'roe',standard_value:10,verified:true,scope:'cumulative',unit:'percentage'};
  const prior={...current,standard_value:8};
  const html=view.metricCards({period:'2025FY',financial_analysis:{metrics:[current]},financial_comparisons:{rows:[{metric:'roe',prior_cumulative:prior}]}});
  assert.ok(html.includes('+2.00个百分点'));assert.ok(!html.includes('+25.00%'));
});

test('Q1 annual row uses prior FY values and never relabels current Q1 as last full year',()=>{
  const html=view.metricCards({period:'2026Q1',financial_analysis:{metrics:[{metric:'revenue',standard_value:11110000,scope:'cumulative',verified:true,unit:'yuan'}]},financial_comparisons:{annual_period:'2025FY',annual_metrics:[{metric:'revenue',standard_value:99990000,scope:'cumulative',verified:true,unit:'yuan'}]}});
  const annual=html.slice(html.indexOf('cumulative-kpis'),html.indexOf('单季对比金额与计算依据'));
  assert.ok(html.includes('上一年全年 · 2025'));assert.ok(annual.includes('9,999.00'));assert.ok(!annual.includes('1,111.00'));
});

test('financial amounts use at least ten-thousand yuan and switch to hundred-million at the absolute threshold',()=>{
  for(const [value,expected] of [[510409430.23,'5.10 亿元'],[29431752.01,'2,943.18 万元'],[-5495.6,'-0.55 万元'],[0.01,'0.00 万元'],[0,'0.00 万元'],[100000000,'1.00 亿元'],[-100000000,'-1.00 亿元'],[99990000,'9,999.00 万元'],[-99990000,'-9,999.00 万元']])assert.equal(view.formatFinancial({metric:'net_profit',unit:'yuan',standard_value:value}),expected);
  assert.equal(view.formatFinancial({metric:'roe',unit:'percentage',standard_value:7.7}),'7.70%');
  const html=view.metricCards({financial_analysis:{metrics:[{metric:'revenue',unit:'yuan',standard_value:510409430.23,verified:true,scope:'cumulative'}]}});
  assert.ok(html.includes('金额：万元 / 亿元'));assert.ok(html.includes('5.10 <small>亿元</small>'));
});

test('annual reports label Q4 calculations and retain full-year cumulative section',()=>{
  const html=view.metricCards({period:'2025FY',financial_comparisons:{rows:[{metric:'net_profit',current:{verified:true,standard_value:100,derived:true,method:'全年减前三季度',formula:'2025FY累计 − 2025Q3累计',inputs:[]}}]},financial_analysis:{metrics:[]}});
  for(const text of ['2025Q4','2025Q3','全年减前三季度','全年累计与期末状况','计算值'])assert.ok(html.includes(text),text);
});

test('market cards keep original quote and all valuation bases accessible when folded',()=>{
  const html=view.marketCards({market_at_analysis:{price:9.39,pe_ttm:75.91,pe_dynamic:75.19,pe_static:31.08,asof:'2026-10-16T16:14:45+08:00',dynamic_pe_period:'2026-09-30',static_pe_period:'2025-12-31',eps_period:'2026-09-30',source:'https://gu.qq.com/sz002416'},decision:{quote:{price:999}}});
  assert.ok(html.includes('market-kpis'));
  assert.ok(html.includes('9.39 元'));
  assert.ok(!html.includes('999'));
  for(const text of ['75.91 倍','75.19 倍','31.08 倍','2026-10-16T16:14:45+08:00','2026-09-30','2025-12-31','估值口径与基期','并非盈利预测','尚未接入股权质押明细来源'])assert.ok(html.includes(text),text);
});

test('行情备用不能显示为双源核对成功，冲突保留复核提示',()=>{
  for(const [status,text] of [['unavailable','尚未完成双源核对'],['conflict','来源数值存在差异']]){
    const html=view.marketCards({market_at_analysis:{price:10,cross_validation:{status}}});
    assert.ok(html.includes(text));
    assert.ok(!html.includes('双来源已核对一致'));
  }
});

test('incomplete scores are shown as bounds with reasons, never as complete low grades',()=>{
  const report={score:49.4,score_status:'provisional',score_coverage:70,score_range:[49.4,79.4],rating:'unrated',evidence_status:'insufficient',conclusion:'现金流改善。'};
  assert.ok(view.scoreCell(report).includes('保守下限'));
  assert.ok(view.scoreCell(report).includes('49.4–79.4'));
  const html=view.render(report,{decision:{rating_blockers:['估值尚未补齐'],missing:[]},financial_analysis:{metrics:[]}});
  assert.ok(html.includes('为何未评级 · 1 项条件待满足'));
  assert.ok(html.includes('估值尚未补齐'));
  assert.ok(html.includes('缺项时显示的是保守下限'));
  assert.ok(!view.scoreCell({...report,score_status:'calculated'}).includes('保守下限'));
  assert.ok(!view.scoreCell({...report,score_range:['<img>',79]}).includes('<img>'));
});

test('core completeness exposes source blanks, calculation basis and all core metrics',()=>{
  const html=view.metricCards({financial_analysis:{metrics:[{metric:'gross_margin',standard_value:25,unit:'percentage',scope:'cumulative',source_page:8,verified:true,verification_method:'derived_from_verified',formula:'(营业收入−营业成本)/营业收入×100'}],data_quality:{verified_count:12,expected_count:13,status:'review',parser_status:'checked',checked_at:'2026-10-18',missing:[{metric:'capex',scope:'cumulative',status:'source_blank',source_pages:[11],reason:'已定位原表对应期间列为空，不能擅自按零处理'}]}}});
  for(const text of ['核心指标核对 12 / 13','原表空白 · 待核实','第11页','毛利率','资本开支','总负债','依据已核对原表计算','(营业收入−营业成本)/营业收入×100','不能擅自按零处理'])assert.ok(html.includes(text),text);
  assert.ok(!html.includes('资本开支</td><td>0'));
});

test('public industry sources retain taxonomy and escape page text without implying peer verification',()=>{
  const industry={observed_at:'2026-10-19',cross_validation:{reason:'分类体系不同，尚未形成同口径同行样本'},sources:[{provider:'ths',source_url:'https://basic.10jqka.com.cn/600519/company.html',classifications:[{taxonomy:'shenwan_display',path:['食品饮料','<img src=x>白酒Ⅱ']}]}]};
  const html=view.render({conclusion:'核查行业。'},{industry_sources:industry});
  for(const text of ['公开行业归属与交叉核对','来源标注的申万行业','分类体系不同','尚未形成同口径同行样本','也不代表已取得同行估值样本','2026-10-19','&lt;img src=x&gt;'])assert.ok(html.includes(text),text);
  assert.ok(!html.includes('<img'));
  assert.doesNotThrow(()=>view.render({}, {industry_sources:{errors:[{provider:'ths'}]}}));
});
