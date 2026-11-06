/* Pure rendering helpers; no network, stored credentials, or generated financial facts. */
(function(root){
  'use strict';
  const labels={buy:'买入',overweight:'增持',neutral:'中性',underweight:'减持',sell:'卖出',unrated:'未评级'};
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const url=s=>{try{return ['https:','http:'].includes(new URL(s).protocol)}catch{return false}};
  const link=(s,text)=>url(s)?'<a href="'+esc(s)+'" target="_blank" rel="noopener noreferrer">'+esc(text||s)+'</a>':'';
  const lines=text=>String(text||'').split(/\n+/).map(x=>x.trim()).filter(Boolean);
  const sentences=text=>String(text||'').match(/[^。！？；\n]+[。！？；]?/g)||[];
  function paragraphs(text){return lines(text).map(p=>'<p>'+esc(p)+'</p>').join('')}
  function readableTime(value){
    if(!value)return '未留存';
    if(/^\d{4}-\d{2}-\d{2}$/.test(String(value)))return String(value);
    const date=new Date(value);
    return Number.isFinite(date.getTime())?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(date):String(value);
  }
  function list(values){return '<ul>'+values.filter(Boolean).map(v=>'<li>'+esc(v)+'</li>').join('')+'</ul>'}
  function details(title,body,cls=''){return '<details class="report-details '+cls+'"><summary>'+esc(title)+'</summary><div class="report-details-content">'+body+'</div></details>'}
  function cleanConclusion(text){
    return String(text||'').replace(/^(?:测试研究\s*[·：:]\s*)?(?:买入候选|继续观察|暂不参与|证据不足|买入|增持|中性|减持|卖出|未评级)[。·：:]\s*/,'').replace(/^结论[：:]\s*/,'').split(/\n?关键缺口[：:]/)[0].trim();
  }
  function brief(text){
    const s=sentences(cleanConclusion(text));
    const main=s.find(x=>!/评级由程序|证据分/.test(x))||s[0]||'尚无核心结论，请核对报告原文。';
    return main.replace(/^核心事实是[：:]\s*/,'').replace(/[，,]初步程序评级[^。]*[。]?/,'。');
  }
  function header(r,compact=false){
    const rating=labels[r.rating]?r.rating:'unrated';
    const score=typeof r.score==='number'&&Number.isFinite(r.score)?r.score.toFixed(1):'待计算';
    if(compact)return '<div class="report-title-assessment" aria-label="评分与评级"><div class="report-assessment-main"><span class="report-assessment-score"><span class="report-assessment-label">评分</span><strong class="'+(r.score_status==='calculated'?'':'is-provisional')+'">'+esc(score)+(score==='待计算'?'':' <small>/ 100</small>')+'</strong></span><span class="report-assessment-rating"><span class="report-assessment-label">评级</span><strong class="report-rating-value rating-'+rating+'">'+labels[rating]+'</strong></span></div><div class="report-assessment-period">'+periodTag(r)+'</div><div class="report-assessment-note">'+(r.score_status==='calculated'?'':'保守下限 · ')+'覆盖 '+esc(r.score_coverage??0)+'%'+scoreRange(r).replace('<br>',' · ')+'</div></div>';
    return '<div class="report-score-header"><div><span class="subtle">研究评级</span><p><span class="badge rating-'+rating+'">'+labels[rating]+'</span></p>'+(r.evidence_status==='insufficient'?'<span class="subtle">证据不足，暂不形成方向评级</span>':'')+'</div><div><span class="subtle">买入评分'+(r.score_status==='calculated'?'':' · 保守下限')+'</span><p class="report-score-number '+(r.score_status==='calculated'?'':'is-provisional')+'">'+esc(score)+(score==='待计算'?'':' <small>/ 100</small>')+'</p><span class="subtle">数据覆盖 '+esc(r.score_coverage??0)+'%'+scoreRange(r)+'</span></div></div>';
  }
  function ratingSummary(r,full){return header({...r,...full,...(full?.scoring?{score_range:full.scoring.possible_range}:{})},true)}
  function scoreRange(r){
    const range=r.score_range;
    return r.score_status!=='calculated'&&Array.isArray(range)&&range.length===2&&range.every(v=>typeof v==='number'&&Number.isFinite(v))?'<br>待补齐区间 '+esc(range[0].toFixed(1))+'–'+esc(range[1].toFixed(1)):'';
  }
  function scoreCell(r){
    if(typeof r.score!=='number'||!Number.isFinite(r.score))return '<span class="subtle">待计算</span>';
    return '<strong style="white-space:nowrap;color:var(--ink)">'+esc(r.score.toFixed(1))+'<small style="font-weight:500"> / 100</small></strong><span class="company-code">'+(r.score_status==='calculated'?'':'保守下限 · ')+'覆盖 '+esc(r.score_coverage??0)+'%'+scoreRange(r)+'</span>';
  }
  const metricNames={revenue:'营业收入',net_profit:'归母净利润',adjusted_profit:'扣非净利润',operating_cashflow:'经营现金流',inventory:'存货',receivables:'应收账款',roe:'加权ROE',cash:'货币资金',total_assets:'总资产',total_liabilities:'总负债',capex:'资本开支',gross_margin:'毛利率',eps:'每股收益',operating_revenue:'营业收入（不含利息等）',operating_cost:'营业成本',total_equity:'所有者权益合计'};
  const verificationLabel=m=>({pdf_table_column:'表格行列已核对',pdf_statement_column:'合并报表行列已核对',derived_from_verified:'依据已核对原表计算'}[m?.verification_method]||'引文已核对');
  const metricLabel=m=>m?.metric==='revenue'&&/营业总收入/.test(m.quote||'')?'营业总收入':metricNames[m?.metric]||m?.metric;
  const scopeNames={cumulative:'年初至报告期末',single_quarter:'单季度',instant:'期末时点'};
  function formatMetric(m){
    const v=m.standard_value;
    if(typeof v!=='number'||!Number.isFinite(v))return '未披露';
    if(m.unit==='percentage')return v.toFixed(2)+'%';
    if(m.unit==='ratio')return v.toFixed(2);
    if(m.metric==='eps')return v.toFixed(3)+' 元/股';
    const a=Math.abs(v);
    return (a>=1e8?(v/1e8).toFixed(2)+' 亿元':a>=1e4?(v/1e4).toFixed(2)+' 万元':v.toFixed(2)+' 元');
  }
  function headingMetadata(report,full){
    const industry=String(full?.industry||report?.industry||'').trim()||'待核实';
    return '<span class="report-heading-industry">行业：'+esc(industry)+'</span>';
  }
  function periodTag(report){
    const period=String(report?.period||'').trim().replace(/FY$/,'年报');
    return period?'<span class="report-period-tag">报告期：'+esc(period)+'</span>':'';
  }
  function formatFinancial(m){
    if(m?.unit==='percentage'||m?.unit==='ratio'||m?.metric==='eps')return formatMetric(m);
    if(!Number.isFinite(m?.standard_value))return '待补齐';
    const billions=Math.abs(m.standard_value)>=100000000;
    const value=m.standard_value/(billions?100000000:10000);
    return value.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})+(billions?' 亿元':' 万元');
  }
  function financialChange(current,previous,metric){
    if(!Number.isFinite(current)||!Number.isFinite(previous))return null;
    const delta=current-previous, tone=delta>0?'finance-up':delta<0?'finance-down':'finance-flat';
    const pct=previous!==0?Math.abs(delta)/Math.abs(previous)*100:null;
    const flow=metric==='operating_cashflow', profit=['net_profit','adjusted_profit'].includes(metric);
    let text;
    if(delta===0)text='持平';
    else if(current===0&&previous<0)text=flow?'净流出归零':profit?'亏损归零':'负值归零';
    else if(previous<0&&current>0)text=flow?'转为净流入':profit?'扭亏为盈':'负转正';
    else if(previous>0&&current<0)text=(flow?'转为净流出':profit?'由盈转亏':'正转负')+' −'+pct.toFixed(2)+'%';
    else if(previous<0&&current<0)text=(flow?(delta>0?'净流出收窄 ':'净流出扩大 '):profit?(delta>0?'减亏 ':'亏损扩大 '):(delta>0?'负值收窄 ':'负值扩大 '))+pct.toFixed(2)+'%';
    else if(previous===0)text=current>0?(profit?'由零转盈':flow?'由零转为净流入':'由零增加'):(profit?'由零转亏':flow?'由零转为净流出':'由零减少');
    else text=(delta>0?'+':'−')+pct.toFixed(2)+'%';
    return {text,tone,delta};
  }
  function financialComparison(current,prior,metric,fallback){
    if(!current?.verified||!Number.isFinite(current.standard_value))return null;
    const result=financialChange(current.standard_value,prior?.verified?prior.standard_value:null,metric);
    if(result){
      if(current.unit==='percentage')result.text=(result.delta>0?'+':'')+result.delta.toFixed(2)+'个百分点';
      return result;
    }
    if(Number.isFinite(fallback))return {text:(fallback>0?'+':'')+fallback.toFixed(2)+'%',tone:fallback<0?'finance-down':fallback>0&&current.standard_value>=0?'finance-up':'finance-flat',printed:true};
    return null;
  }
  function financialValueTone(current,yoyPrior,metric,yoyPct,qoqPrior){
    if(!current?.verified||!Number.isFinite(current.standard_value))return 'finance-flat';
    if(current.standard_value<0)return 'finance-down';
    if(current.standard_value===0)return 'finance-flat';
    return (financialComparison(current,yoyPrior,metric,yoyPct)||financialComparison(current,qoqPrior,metric))?.tone||'finance-flat';
  }
  function changeHtml(current,prior,metric,fallback){
    const result=financialComparison(current,prior,metric,fallback);
    if(result)return '<span class="financial-change '+result.tone+'">'+esc(result.text)+'</span>'+(result.printed?'<small class="comparison-note">原文同比 · 基数金额待补</small>':'');
    return '<span class="subtle">待补齐</span>';
  }
  function quarterCards(full,part='cards'){
    const c=full.financial_comparisons||{},all=full.financial_analysis?.metrics||[];
    const period=full.period||c.period||'', match=/^(20\d{2})(Q1|H1|Q3|FY)$/.exec(period);
    if(!match)return '';
    const q={Q1:1,H1:2,Q3:3,FY:4}[match[2]],year=Number(match[1]);
    const rows=c.rows||['revenue','net_profit','adjusted_profit','operating_cashflow'].map(metric=>({metric,current:all.find(m=>m.metric===metric&&m.verified&&Number.isFinite(m.standard_value)&&m.scope===(q===1?'cumulative':'single_quarter'))}));
    const amount=(m,metric,current=false,tone='finance-flat')=>{
      if(!m?.verified||!Number.isFinite(m.standard_value))return '<span class="subtle">待补齐</span>';
      const profit=['net_profit','adjusted_profit'].includes(metric),amountTone=m.standard_value<0?'finance-down':current?tone:'finance-flat';
      return '<span class="quarter-amount '+amountTone+'" title="'+esc(m.standard_value.toLocaleString('en-US',{maximumFractionDigits:2})+' 元')+'">'+esc(formatFinancial({...m,unit:'yuan'}))+'</span>'+(current&&profit?'<small class="comparison-note '+tone+'">'+(m.standard_value>0?'当季盈利':m.standard_value<0?'当季亏损':'盈亏平衡')+'</small>':'')+(current&&m.derived?'<small class="comparison-note">计算值 · '+esc(m.method)+'</small>':'');
    };
    const body=rows.map(row=>'<tr><th scope="row">'+esc(metricNames[row.metric])+'</th><td class="quarter-current">'+amount(row.current,row.metric,true,financialValueTone(row.current,row.prior_year,row.metric,row.current?.yoy_pct,row.previous))+'</td><td>'+amount(row.previous,row.metric)+'</td><td>'+amount(row.prior_year,row.metric)+'</td><td>'+changeHtml(row.current,row.prior_year,row.metric,row.current?.yoy_pct)+'</td><td>'+changeHtml(row.current,row.previous,row.metric)+'</td></tr>').join('');
    const proof=rows.flatMap(row=>['current','previous','prior_year'].filter(k=>row[k]?.verified).map(k=>{
      const m=row[k],inputs=m.inputs||[m];
      return '<li>'+esc(metricNames[row.metric]+' · '+({current:'本季',previous:'上季',prior_year:'去年同季'}[k])+'：'+(m.formula||m.method||'原文单季'))+'；'+inputs.map(x=>esc(x.period||period)+' 第'+esc(x.source_page||'待核实')+'页 '+link(x.source_url,'原文')).join('；')+'</li>';
    }));
    if(part==='details')return details('单季对比金额与计算依据','<div class="quarter-table-wrap"><table class="quarter-table"><thead><tr><th>指标</th><th>本季 '+year+'Q'+q+'</th><th>上季 '+(q>1?year+'Q'+(q-1):(year-1)+'Q4')+'</th><th>去年同季 '+(year-1)+'Q'+q+'</th><th>同比</th><th>环比</th></tr></thead><tbody>'+body+'</tbody></table></div><ul>'+proof.join('')+'</ul><p class="subtle">Q1累计即单季；Q2＝半年−Q1；Q3＝前三季−半年；Q4＝全年−前三季。缺少已核对且同口径的原文时保留待补齐，不倒推四舍五入的同比。</p>');
    const previous=part==='previous';
    const displayedRows=previous?(c.previous_rows||[]):rows;
    const displayedMetrics=previous?(c.previous_metrics||[]):all;
    const cards=['revenue','net_profit','adjusted_profit','operating_cashflow','inventory','roe'].map(field=>{
      const row=displayedRows.find(r=>r.metric===field);
      const m=row?.current||displayedMetrics.find(m=>m.metric===field&&m.verified&&(previous||m.scope===(field==='inventory'?'instant':q===1?'cumulative':'single_quarter')));
      const verified=m?.verified&&Number.isFinite(m.standard_value);
      const profit=['net_profit','adjusted_profit'].includes(field),tone=financialValueTone(m,row?.prior_year,field,m?.yoy_pct,row?.previous);
      const value=verified?esc(formatFinancial(m)).replace(/ (万元|亿元)$/,' <small>$1</small>'):'待补齐';
      const label=field==='inventory'?'期末存货':metricNames[field];
      const growth='<div class="kpi-trend">同比 '+(row?changeHtml(m,row.prior_year,field,m?.yoy_pct):'<span class="subtle">'+(field==='inventory'?'时点指标':'待补齐')+'</span>')+'</div><div class="kpi-trend">环比 '+(row?changeHtml(m,row.previous,field):'<span class="subtle">'+(field==='inventory'?'时点指标':'待补齐')+'</span>')+'</div>';
      const note=field==='inventory'?'期末时点':m?.derived?'差额计算 · 已核对':verified?(profit?(m.standard_value>0?(previous?'上季盈利':'当季盈利'):m.standard_value<0?(previous?'上季亏损':'当季亏损'):'盈亏平衡'):'单季')+' · 已核对'+(m.source_page?' · 第'+m.source_page+'页':''):'单季原文待补齐';
      return '<div class="report-kpi '+(!verified?'tone-pending':'')+'" data-metric="'+field+'"><span class="kpi-label">'+esc(label)+'</span><strong class="kpi-value '+tone+(verified&&formatFinancial(m).length>15?' is-long-value':'')+'">'+value+'</strong>'+growth+'<div class="kpi-note">'+esc(note)+'</div></div>';
    }).join('');
    return '<h4 class="financial-row-heading">'+(previous?'上季指标':'当季指标')+' <span>'+esc(previous?(c.previous_quarter||((q>1?year:year-1)+'Q'+(q>1?q-1:4))):year+'Q'+q+' · '+((q-1)*3+1)+'—'+q*3+'月')+'</span></h4><div class="financial-row-scroll"><div class="report-kpis financial-kpis '+(previous?'previous-kpis':'quarter-kpis')+'">'+cards+'</div></div>';
  }
  function metricCards(full){
    const usePriorAnnual=/Q1$/.test(full.period||'');
    const all=usePriorAnnual?(full.financial_comparisons?.annual_metrics||[]):(full.financial_analysis?.metrics||[]);
    const chosen=['revenue','net_profit','adjusted_profit','operating_cashflow','inventory','roe'].map(name=>all.find(m=>m.metric===name&&m.scope===(name==='inventory'?'instant':'cumulative')&&m.verified)||all.find(m=>m.metric===name&&m.scope===(name==='inventory'?'instant':'cumulative')));
    return '<section class="report-section financial-section"><h3>关键财务指标 <small class="financial-unit">金额：万元 / 亿元 · 两位小数</small></h3>'+quarterCards(full)+quarterCards(full,'previous')+'<h4 class="financial-row-heading cumulative-heading">年度指标 <span>'+(usePriorAnnual?'上一年全年 · '+(Number(String(full.period).slice(0,4))-1):String(full.period||'').endsWith('FY')?'全年累计与期末状况':'年初至今累计与期末状况')+'</span></h4><div class="financial-row-scroll"><div class="report-kpis financial-kpis cumulative-kpis">'+chosen.map((m,i)=>{
      const name=metricNames[['revenue','net_profit','adjusted_profit','operating_cashflow','inventory','roe'][i]];
      const verified=m?.verified&&typeof m.standard_value==='number'&&Number.isFinite(m.standard_value);
      const risk=verified&&['net_profit','adjusted_profit','operating_cashflow'].includes(m.metric)&&m.standard_value<0;
      const field=['revenue','net_profit','adjusted_profit','operating_cashflow','inventory','roe'][i];
      const gap=usePriorAnnual?null:full.financial_analysis?.data_quality?.missing?.find(x=>x.metric===field);
      const pendingLabel=gap?.status==='source_not_applicable'?'原文不适用':m?.verification_status==='not_disclosed'?'原文未披露':(!m||m.value===null)?'未提取':'核对未通过';
      const value=verified?esc(formatFinancial(m)).replace(/ (万元|亿元|元\/股)$/,' <small>$1</small>'):pendingLabel;
      const method=verificationLabel(m);
      const scope={cumulative:'累计',single_quarter:'单季',instant:'期末'}[m?.scope]||'';
      const bridge=(usePriorAnnual?full.financial_comparisons?.annual_rows:full.financial_comparisons?.rows)?.find(row=>row.metric===field);
      const yoy='<div class="kpi-trend">同比 '+(verified?changeHtml(m,bridge?.prior_cumulative,field,m.yoy_pct):'<span class="subtle">待补齐</span>')+'</div>';
      const qoq='<div class="kpi-trend">环比 <span class="subtle">'+(m?.scope==='instant'?'时点指标':'累计不适用')+'</span></div>';
      const tone=financialValueTone(m,bridge?.prior_cumulative,field,m?.yoy_pct);
      return '<div class="report-kpi '+(!verified&&gap?.status!=='source_not_applicable'?'tone-pending':'')+'" data-metric="'+field+'"><span class="kpi-label">'+esc(m?metricLabel(m):name)+(risk?' · 负值':'')+'</span><strong class="kpi-value '+tone+(verified&&formatFinancial(m).length>15?' is-long-value':'')+'"'+(verified?' title="'+esc(m.unit==='percentage'?formatMetric(m):m.standard_value.toLocaleString('en-US',{maximumFractionDigits:2})+' 元')+'"':'')+'>'+value+'</strong>'+yoy+qoq+'<div class="kpi-note">'+(verified?'<span title="'+esc(method+'；'+(scopeNames[m.scope]||''))+'">'+esc(scope)+' · 已核对 · 第'+esc(m.source_page)+'页</span>':esc(gap?.status==='source_not_applicable'?gap.reason:m?.verification_reason||'未提取到可核实数据，不能据此断定财报未披露'))+'</div></div>';
    }).join('')+'</div></div><p class="financial-legend">已核实负数金额为绿；正金额优先按同比、缺同比时按环比，增长红、下降绿；零值、方向不确定或未核实为黑。同比与环比分别着色，扭亏/减亏红，转亏/扩亏绿。存货为期末值；Q1年度行取上年全年，其他中报取本年累计；累计不作单季环比。原文核对不等于审计。</p>'+quarterCards(full,'details')+supplementalMetrics(full)+qualitySummary(full)+'</section>';
  }
  function supplementalMetrics(full){
    const all=full.financial_analysis?.metrics||[];
    const metrics=['gross_margin','receivables','cash','total_assets','total_liabilities'].map(name=>all.find(m=>m.metric===name&&m.verified&&Number.isFinite(m.standard_value)&&m.scope!=='single_quarter')).filter(Boolean);
    return metrics.length?details('补充指标 · 毛利率、回款与资产负债','<div class="financial-supplement">'+metrics.map(m=>'<div><span>'+esc(metricNames[m.metric])+'</span><strong>'+esc(formatFinancial(m))+'</strong><small>'+esc(scopeNames[m.scope])+' · 第'+esc(m.source_page)+'页</small></div>').join('')+'</div>'):'';
  }
  function qualitySummary(full){
    const fa=full.financial_analysis||{},q=fa.data_quality;if(!q)return '';
    const core={revenue:'cumulative',net_profit:'cumulative',adjusted_profit:'cumulative',operating_cashflow:'cumulative',capex:'cumulative',roe:'cumulative',gross_margin:'cumulative',eps:'cumulative',total_assets:'instant',total_liabilities:'instant',cash:'instant',receivables:'instant',inventory:'instant'};
    const rows=Object.entries(q.applicable_fields||core).map(([name,scope])=>{
      const m=(fa.metrics||[]).find(m=>m.metric===name&&m.scope===scope&&m.verified);
      const valid=m&&typeof m.standard_value==='number'&&Number.isFinite(m.standard_value);
      const gap=(q.missing||[]).find(m=>m.metric===name&&m.scope===scope);
      const state=valid?verificationLabel(m):gap?.status==='source_not_applicable'?'原文不适用':gap?.status==='source_blank'?'原表空白 · 待核实':'待核对';
      const pages=valid?['第'+m.source_page+'页',m.header_source_page&&m.header_source_page!==m.source_page?'表头第'+m.header_source_page+'页':'',m.unit_source_page&&m.unit_source_page!==m.source_page?'单位第'+m.unit_source_page+'页':''].filter(Boolean).join('；'):(gap?.source_pages||[]).map(p=>'第'+p+'页').join('；');
      return '<tr><td>'+esc(m?metricLabel(m):metricNames[name])+'<br><span class="subtle">'+esc(scopeNames[scope])+'</span></td><td>'+esc(valid?formatFinancial(m):gap?.status==='source_not_applicable'?'不适用':'待核实')+'</td><td class="'+(valid?'metric-ok':'metric-pending')+'">'+esc(state)+(m?.formula?'<br><span class="subtle">'+esc(m.formula)+'</span>':'')+'</td><td>'+esc(pages||'尚未可靠定位')+'</td></tr>';
    }).join('');
    const warnings=[...(q.not_applicable||[]).map(m=>(metricNames[m.metric]||m.metric)+'：行业不适用（'+m.reason+'）'),...(q.industry_note?[q.industry_note]:[]),...(q.missing||[]).map(m=>(metricNames[m.metric]||m.metric)+'：'+m.reason),...(q.conflicts||[]).map(c=>(metricNames[c.metric]||c.metric)+'：'+c.reason),...(q.failed_checks||[]).map(c=>c.name+'未通过')];
    if(q.parser_status&&q.parser_status!=='checked')warnings.push('原表核对未完成，当前完整性不能确认');
    return details('核心指标核对 '+q.verified_count+' / '+q.expected_count+(q.status==='complete'?' · 已核对':' · 存在待复核项'),(warnings.length?'<div class="metric-pending">'+list(warnings)+'</div>':'')+'<div class="table-wrap"><table><thead><tr><th>指标与口径</th><th>数值</th><th>状态</th><th>原表页码</th></tr></thead><tbody>'+rows+'</tbody></table></div><p class="subtle">核对时间：'+esc(q.checked_at||'未记录')+'。'+esc(q.note)+'。毛利率按已核对的营业收入、营业成本计算；资本开支指购建固定资产、无形资产和其他长期资产支付的现金。</p>');
  }
  function marketCards(full){
    const q=full.market_at_publication||full.market_at_analysis||full.quote_at_analysis||{};
    const quoteFailure=full.market_publication_context?.state==='unavailable'?'<p class="metric-pending">入库行情暂未取得，等待来源恢复后补采；未填入推测价格。</p>':'';
    const publication=full.market_at_publication?full.market_publication_context:null;
    const quoteLabel=publication?(publication.kind==='backfill'?'补采股价':'解读完成时股价'):'解读时股价';
    const quoteNote=publication?'报告完成后采集；未送回模型':'原解读行情快照';
    const check=q.cross_validation;
    const checkNames={matched:'双来源可比字段已核对一致',conflict:'来源数值存在差异，需复核',unavailable:'备用来源暂不可用，尚未完成双源核对',not_comparable:'来源时点不同，暂不能比较'};
    const verification=check?'<p class="subtle">行情核对：'+esc(checkNames[check.status]||'未完成')+(check.source?' · '+link(check.source,'辅助行情来源'):'')+'。</p>':'';
    const valid=v=>typeof v==='number'&&Number.isFinite(v);
    const money=v=>valid(v)?(Math.abs(v)>=1e8?(v/1e8).toFixed(2)+' 亿元':v.toFixed(2)+' 元'):'未采集';
    const fields=[[quoteLabel,valid(q.price)?q.price.toFixed(2)+' 元':'未采集',quoteNote],['滚动市盈率 PE(TTM)',valid(q.pe_ttm)&&q.pe_ttm>0?q.pe_ttm.toFixed(2)+' 倍':'不适用或未采集','最近12个月 · 来源值'],['动态市盈率（年化）',valid(q.pe_dynamic)?q.pe_dynamic.toFixed(2)+' 倍':'不适用或证据不足',q.dynamic_pe_basis==='provider'?'行情来源值 · 基期以来源为准':'按本报告累计利润简单年化'],['静态市盈率',valid(q.pe_static)?q.pe_static.toFixed(2)+' 倍':'不适用或证据不足',q.static_pe_basis==='provider'?'行情来源值 · 基期以来源为准':'最近完整年度利润'],['市净率 PB',valid(q.pb)&&q.pb>0?q.pb.toFixed(2)+' 倍':'不适用或未采集','行情来源值'],['总市值',money(q.market_cap),quoteNote],['换手率',valid(q.turnover_pct)?q.turnover_pct.toFixed(2)+'%':'未采集','行情来源值'],['当日涨跌幅',valid(q.change_pct)?(q.change_pct>0?'+':'')+q.change_pct.toFixed(2)+'%':'未采集','行情来源值'],['基本每股收益',valid(q.eps)?q.eps.toFixed(4)+' 元/股':'未提取','财报累计口径']];
    const cards=fields.map(([label,value,note])=>'<div class="report-kpi"><span class="kpi-label">'+esc(label)+'</span><strong class="kpi-value">'+esc(value)+'</strong><div class="kpi-note">'+esc(note)+'</div></div>').join('');
    const more=[['每股净资产',valid(q.bps)?q.bps.toFixed(3)+' 元':valid(q.bps_estimate)?q.bps_estimate.toFixed(3)+' 元（估算）':'未采集',valid(q.bps)?'结构化财务辅助源':'股价÷PB；受行情四舍五入影响'],['报告期末总股本',valid(q.total_shares)?(q.total_shares/1e8).toFixed(4)+' 亿股':'未采集','结构化财务辅助源'],['每股经营现金流',valid(q.operating_cashflow_per_share)?q.operating_cashflow_per_share.toFixed(3)+' 元':'未采集','累计口径 · 结构化财务辅助源'],['毛利率',valid(q.gross_margin)?q.gross_margin.toFixed(2)+'%':'未提取','报告期累计口径'],['报告期末流通A股',valid(q.a_float_shares)?(q.a_float_shares/1e8).toFixed(4)+' 亿股':'未采集','结构化财务辅助源'],['每股资本公积',valid(q.capital_reserve_per_share)?q.capital_reserve_per_share.toFixed(3)+' 元':'未采集','结构化财务辅助源'],['每股未分配利润',valid(q.retained_earnings_per_share)?q.retained_earnings_per_share.toFixed(3)+' 元':'未采集','结构化财务辅助源'],['质押股数及比例','未采集','尚未接入股权质押明细来源']];
    const basis=details('估值口径与基期','<p>行情快照保存后不随最新价格覆盖；亏损或缺少分母时不计算市盈率。动态、静态市盈率优先展示行情来源值，缺失时才展示带基期的本地测算，不能把历史年报年化值冒充来源当前动态市盈率。累计利润简单年化并非盈利预测。</p><p>动态市盈率基期：'+esc(q.dynamic_pe_period||'未提供')+'；静态市盈率基期：'+esc(q.static_pe_period||'未提供')+'；每股收益报告期：'+esc(q.eps_period||'未提供')+'。</p><p class="subtle">财务数据核对更新：'+esc(q.financial_checked_at||'未记录')+'；'+esc(q.financial_refresh_note||'保留原记录')+'。</p>');
    const title=publication?(publication.kind==='backfill'?'补采的股价与估值':'解读完成时的股价与估值'):'解读时的股价与估值';
    const timing=publication?'<p class="subtle">采集时间：'+esc(publication.captured_at)+'；行情时间：'+esc(q.asof||'未提供')+'。'+(publication.kind==='backfill'?'本段为后续补采，不冒充原解读时行情。':'解读完成后采集，作为报告入库快照。')+'不反向修改模型原文、评分或预测基准。</p>':'<p class="subtle">原行情时点：'+esc(q.asof||'未留存')+'</p>';
    const names={price:'股价',previous_close:'前收盘价',pe_ttm:'滚动市盈率',pe_dynamic_source:'动态市盈率',pe_static_source:'静态市盈率',pb:'市净率',market_cap:'总市值',turnover_pct:'换手率',change_pct:'涨跌幅'};
    const checks=check?details('行情逐字段交叉核对','<p>来源时间或计算口径不同的字段不能据此认定一致；未返回的字段不算已核对。</p><div class="table-wrap"><table><thead><tr><th>字段</th><th>采用来源</th><th>辅助来源</th><th>核对结果</th></tr></thead><tbody>'+Object.entries(names).map(([key,name])=>{const c=check.fields?.[key];return '<tr><td>'+name+'</td><td>'+esc(c?.primary??'—')+'</td><td>'+esc(c?.secondary??'—')+'</td><td>'+(!c?'未核对':c.matches?'一致':'存在差异')+'</td></tr>'}).join('')+'</tbody></table></div>'):'';
    return '<section class="report-section"><h3>'+title+'</h3>'+quoteFailure+timing+'<p class="subtle">'+link(q.source,'行情来源')+'</p>'+verification+checks+'<div class="report-kpis market-kpis">'+cards+'</div>'+basis+details('股本、每股指标及其他数据','<p class="subtle">财务报告期：'+esc(q.financial_period||'未提供')+'；'+esc(q.auxiliary_basis||'辅助指标未归档')+' '+link(q.financial_source,'财务辅助来源')+'</p><div class="table-wrap"><table><thead><tr><th>指标</th><th>数值</th><th>口径与状态</th></tr></thead><tbody>'+more.map(row=>'<tr>'+row.map(v=>'<td>'+esc(v)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>')+'</section>';
  }
  function paragraphSection(title,body){return body?'<section class="report-section"><h3>'+esc(title)+'</h3>'+paragraphs(body)+'</section>':''}
  function longText(text){return paragraphs(sentences(text).map(s=>s.trim()).filter(Boolean).join('\n'))}
  function scoreSection(full){
    const sc=full.scoring;if(!sc)return '';
    const rows=(sc.dimensions||[]).map(d=>'<tr><td>'+esc(d.name)+'</td><td>'+esc(d.points)+' / '+esc(d.maximum)+'</td><td>'+esc(d.covered_weight)+'</td></tr>').join('');
    const factors=(sc.items||[]).map(i=>'<tr><td>'+esc(i.name)+'</td><td>'+esc(i.points)+' / '+esc(i.weight)+'</td><td>'+esc(i.available?i.formula:i.missing_reason)+'</td></tr>').join('');
    const refs=(sc.references||[]).filter(r=>url(r.url)).map(r=>'<li>'+link(r.url,r.name)+'：'+esc(r.use)+'</li>').join('');
    const observed=sc.observed_reference;
    const diagnostic='<p>已覆盖因子贡献 '+esc(sc.raw_points)+' 分 / 已覆盖权重 '+esc(sc.coverage_pct)+' 分；缺失权重 '+esc(sc.uncovered_weight??100-sc.coverage_pct)+' 分。风险扣分 '+esc(sc.penalty)+' 分；风险上限 '+esc(sc.risk_cap)+' 分。</p>'+(typeof observed?.score==='number'?'<p>已覆盖因子表现参考：<strong>'+esc(observed.score.toFixed(1))+' / 100</strong>。'+esc(observed.formula)+'。</p><p class="subtle">'+esc(observed.usage)+'</p>':'');
    return details('评分拆解与计算方法','<p>'+esc(sc.formula)+'</p>'+diagnostic+'<div class="table-wrap"><table><thead><tr><th>维度</th><th>得分 / 满分</th><th>已覆盖权重</th></tr></thead><tbody>'+rows+'</tbody></table></div>'+list((sc.penalties||[]).map(x=>x.reason+'：'+x.points+'分'))+details('指标公式与缺失项','<div class="table-wrap"><table><thead><tr><th>指标</th><th>得分</th><th>公式或缺失原因</th></tr></thead><tbody>'+factors+'</tbody></table></div>')+'<p class="subtle">'+esc(sc.interpretation)+'</p><p class="subtle">评分更新：'+esc(sc.asof)+'；行情时点：'+esc(sc.price_asof||'未提供')+'；版本：'+esc(sc.version)+'</p><h4>公开方法来源</h4><ul>'+refs+'</ul>');
  }
  function chapters(full){
    const sections=full.financial_analysis?.sections||[];
    const groups=[['经营与成长',/营收|单季度|分业务|新产品|订单|毛利|费用|扣非/],['现金流与财务质量',/现金|资产|偿债|存货|应收|ROE|资本/],['行业与竞争',/行业|竞争/],['治理与其他风险',/.*/]];
    const used=new Set();
    return groups.map(([title,pattern])=>{
      const items=sections.filter((s,i)=>{if(used.has(i)||!pattern.test(s.title))return false;used.add(i);return true});
      return items.length?details(title+' · '+items.length+' 项',items.map(s=>'<article><h4>'+esc(s.title)+'</h4>'+longText(s.analysis)+'<p class="subtle">原文依据：PDF 第 '+esc((s.source_pages||[]).join('、')||'未提供')+' 页</p></article>').join('')):'';
    }).join('');
  }
  function provenance(full){
    const metrics=full.financial_analysis?.metrics||[];
    const rows=metrics.map(m=>'<tr><td>'+esc(metricLabel(m))+'<br><span class="subtle">'+esc(scopeNames[m.scope]||m.scope)+'</span></td><td>'+esc(formatMetric(m))+'</td><td class="'+(m.verified?'metric-ok':'metric-pending')+'">'+(m.verified?verificationLabel(m):'核对未通过')+'</td><td>'+esc(m.source_page)+(m.source_column?' · 第'+esc(m.source_column)+'列':'')+'</td><td>'+esc(m.quote)+'</td></tr>').join('');
    const sources=(full.industry_evidence||[]).map(s=>'<p>'+link(s.url,s.title)+' · '+esc(s.published_at)+'</p>'+(s.verification_warning?'<p class="metric-pending">'+esc(s.verification_warning)+'</p>':'')+paragraphs(s.quote)).join('');
    const claims=(full.financial_analysis?.management_claims||[]).map(c=>'<h4>'+esc(c.claim)+'</h4>'+paragraphs(c.quote)+'<p class="subtle">PDF 第 '+esc(c.source_page)+' 页；待验证：'+esc(c.verification_needed)+'</p>').join('');
    const industry=full.industry_sources;
    const taxonomyNames={eastmoney_2016:'东方财富行业',csrc_display:'来源标注的证监会行业',shenwan_display:'来源标注的申万行业'};
    const classifications=industry?details('公开行业归属与交叉核对','<p>'+esc(industry.cross_validation?.reason)+'</p><p class="subtle">核验时点：'+esc(industry.observed_at)+'。行业归属不能代替行业景气证据，也不代表已取得同行估值样本。</p>'+(industry.sources||[]).map(source=>'<p>'+link(source.source_url,source.provider==='ths'?'同花顺':'东方财富')+'</p>'+list((source.classifications||[]).map(c=>(taxonomyNames[c.taxonomy]||c.taxonomy)+'：'+(c.path||[]).join(' → ')))).join('')):'';
    return details('数据来源与逐项核对记录','<p>'+link(full.source,'查看上市公司财报原文 PDF')+'</p><div class="table-wrap"><table><thead><tr><th>指标与口径</th><th>提取值（未核实值仅供核查）</th><th>状态</th><th>页码</th><th>原文引文</th></tr></thead><tbody>'+rows+'</tbody></table></div>'+classifications+details('管理层表述与待验证事项',claims||'<p>暂无可展示记录。</p>')+details('外部行业证据',sources||'<p class="metric-pending">缺少已核实的外部行业证据，不据此判断景气度。</p>'));
  }
  function researchSection(full,part='all'){
    const study=full.research_profile;
    if(!study?.current)return '';
    const current=study.current;
    const num=v=>typeof v==='number'&&Number.isFinite(v)?v.toFixed(1):'—';
    const range=x=>Array.isArray(x)&&x.length===2?num(x[0])+'–'+num(x[1]):'待补齐';
    const cards=(current.dimensions||[]).map(d=>'<div class="report-kpi"><span class="kpi-label">'+esc(d.name)+'</span><strong class="kpi-value'+(d.complete?'':' is-range')+'">'+(d.complete?esc(num(d.score))+' <small>/ 100</small>':esc(range(d.range)))+'</strong><div class="kpi-note">'+(d.complete?'':'区间 · ')+'有效覆盖 '+esc(num(d.coverage_pct))+'%'+(d.template_gap?' · '+esc(d.template_gap):'')+'</div></div>').join('');
    const composite=Object.entries(current.composites||{}).map(([key,c])=>(key==='short'?'短期关注':'中期关注')+'：'+(c.complete?num(c.score):'待补齐 '+range(c.range))+'；覆盖 '+num(c.coverage_pct)+'%').join('　·　');
    const factors=(current.dimensions||[]).map(d=>'<h4>'+esc(d.name)+'</h4><div class="table-wrap"><table><thead><tr><th>因子 / 权重</th><th>已知值</th><th>状态 / 缺口</th></tr></thead><tbody>'+(d.factors||[]).map(f=>'<tr><td>'+esc(f.name)+' / '+esc(f.weight)+'%<br><span class="subtle">'+esc(f.formula)+'</span></td><td>'+esc(typeof f.value==='object'?JSON.stringify(f.value):f.value??'—')+'</td><td>'+esc(({known:'已取得且有效',missing:'缺失',stale:'已过期',conflict:'来源冲突',not_applicable:'不适用'})[f.state]||f.state)+(f.reason?'<br>'+esc(f.reason):'')+(f.evidence||[]).map(e=>'<br>'+link(e.url||e.source,'核对来源')+(e.page?' · 第'+esc(e.page)+'页':'')).join('')+'</td></tr>').join('')+'</tbody></table></div>').join('');
    const env=current.market_environment||{};
    const old=study.original_score;
    const origin=study.capture_kind==='at_generation'?'生成时冻结':'上线后重建，仅作回顾';
    const audit='<p class="subtle">版本：'+esc(study.version)+' · '+esc(origin)+'；原时点旧分 '+esc(old?.score==null?'未保存':num(old.score))+'，当前修复旧分 '+esc(num(study.repaired_legacy_score?.score))+'。原记录分别保留。</p>';
    const risks=(current.risk_flags||[]).length?'<div class="metric-pending">'+list(current.risk_flags.map(x=>x.label))+'</div>':'';
    const checks=study.operating_catalysts?.checks||[];
    const attention=checks.filter(c=>['watch','strengthening','weakening'].includes(c.status));
    const operatingAlert=attention.length?'<p class="metric-pending">'+esc(attention.map(c=>c.name+' · '+c.status_label).join('；'))+'</p>':'';
    const catalysts=checks.map(c=>'<h4>'+esc(c.name)+' · '+esc(c.status_label)+'</h4>'+((c.evidence||[]).length?(c.evidence||[]).map(e=>'<p>'+esc(e.quote)+'<br><span class="subtle">'+(e.page?'第'+esc(e.page)+'页 · ':'')+esc(e.quote_verified?'原文引文已匹配；经营兑现尚待核实':'引文尚未定位')+' · '+link(e.url,'原文')+'</span></p>').join(''):'<p class="subtle">本次材料未发现明确线索，不代表经营事实不存在。</p>')+'<p class="subtle">'+esc((c.missing||[]).join('；'))+'</p>').join('');
    const overview='<section class="report-section research-shadow research-overview" aria-label="六维评分"><div class="research-overview-heading"><h3>六维评分</h3><span class="report-run-tag">试运行'+(study.pilot_member?' · 试点样本':'')+'</span></div><div class="report-kpis">'+cards+'</div><p class="subtle research-composites">'+esc(composite)+'</p><p class="subtle research-overview-note">独立于原规则评分；缺项保留区间，覆盖率不代表胜率。</p></section>';
    const supporting='<section class="report-section research-shadow research-supporting"><h3>研究依据与经营催化</h3>'+risks+'<p class="subtle">大盘环境：'+esc(env.label||'待补齐')+'；'+esc(env.note||'不向个股统一加减分。')+(env.last_date?' 数据日 '+esc(env.last_date):'')+'</p>'+details('因子、覆盖率与版本记录','<p class="subtle">'+esc(current.interpretation)+' 覆盖率仅针对本版基础因子；增强项另外列示。</p>'+audit+factors+'<h4>增强项 · 尚待补齐或专项验证</h4>'+list(current.enhancement_gaps||[]))+'<div class="research-catalysts">'+operatingAlert+details('经营催化 · '+checks.length+' 项固定检查',catalysts+'<p class="subtle">关键词只发现线索，不能直接加分；同一供需事件不重复贡献。重点关注不等于买入。</p>')+'</div></section>';
    return part==='overview'?overview:part==='supporting'?supporting:overview+supporting;
  }
  function render(r,full,{externalRating=false}={}){
    if(full)r={...r,...full};
    if(full?.scoring)r={...r,score_range:full.scoring.possible_range};
    const historical=r.report_kind==='historical_foundation';
    const corrected=full?.financial_recheck,narrativeStale=corrected?.narrative_status==='requires_review'||full?.narrative_consistency?.state==='review';
    const versions=(r.review_versions||[]).filter(v=>/^[a-zA-Z0-9-]{1,160}$/.test(v.id||''));
    const historicalNote=historical?'<p>历史补建底稿：基于已披露财报补建，不代表报告期当时作出的评级或预测，不计入正式方向吻合率。'+(r.review_state==='accepted'?'核心指标已核对，仍需结合证据阅读。':'仍存在待复核项，请查看原文核对记录。')+'</p>'+(versions.length>1?'<p>保留版本：'+versions.map(v=>'<a href="./reports.html?view=recent&report='+encodeURIComponent(v.id)+'">'+esc(v.role==='pro_review'?'Pro复核':'Flash精读')+'</a>').join(' · ')+'</p>':''):'';
    const correctionNote=corrected?'<p>原文重新核对'+(narrativeStale?' · 旧正文待复核':'已完成')+'：'+esc(corrected.note)+(narrativeStale?'此前展示的部分数字已更正，相关旧正文收起保留，不作为已核实结论。':'')+'</p>':'';
    const sourceCorrection=r.correction_context?'<p>官方更正处理：'+esc(r.correction_context.mode==='original_with_errata'?'原稿逐项修正，与官方更正全文核对一致':'采用官方更正全文')+'；'+link(r.correction_context.notice_url||r.correction_context.effective_url,'查看有效来源')+'</p>':'';
    const processingNotes=(historicalNote||correctionNote||sourceCorrection)?'<div class="report-meta report-processing-notes">'+historicalNote+correctionNote+sourceCorrection+'</div>':'';
    const qr=full?.quarter_review;
    const quarterNote=qr?.summary?'<p class="summary-quarter-review"><strong>关键变化 · 补充核对</strong> '+esc(qr.summary)+'</p><p class="report-meta">核对时间：'+esc(readableTime(qr.asof))+' · 同口径单季计算；不据此判断是否低于市场预期。</p>':qr?.missing?.length?'<p class="report-meta">单季环比比较资料尚未核对齐全，不能仅凭累计同比判断盈利持续改善。</p>':'';
    const summary='<section class="report-summary" aria-label="核心结论"><h3>核心结论</h3><p class="summary-lead">'+esc(brief(r.conclusion).replace(/[；;]$/,'。'))+'</p>'+quarterNote+'<div class="report-meta report-summary-meta"><span>'+(historical?'补建解读日期 ':'原解读日期 ')+esc(readableTime(full?.generated_at||r.date))+'</span>'+(full?.assessment_asof?'<span>评分与规则判断更新：'+esc(readableTime(full.assessment_asof))+'</span><span>下文文字保留原解读时点。</span>':'')+'</div></section>';
    const basic='<div class="report-reading">'+(externalRating?'':header(r))+summary+(full?researchSection(full,'overview'):'');
    if(!full)return basic+'<p class="subtle" role="status">正在读取详细指标与分析……</p></div>';
    const missing=[...new Set([...(full.decision?.missing||[]),...(full.scoring?.missing||[])])];
    const risk=[...(full.decision?.red_flags||[]),...sentences(r.risks)].slice(0,3);
    const positives=sentences(r.evidence).slice(0,2);
    const panels='<p class="subtle" style="margin-top:18px">以下保留模型解读，包含推断；其中数字是否已核对，以指标卡及核对记录为准。</p><div class="report-panels"><section class="report-panel positive"><h3>支持结论的线索</h3>'+list(positives)+details('展开完整证据解读',longText(r.evidence))+'</section><section class="report-panel risk"><h3>重点风险 · 优先复核</h3>'+list(risk)+details('展开完整风险分析',longText(r.risks))+'</section></div>';
    const pending=missing.length?'<section class="report-panel pending"><h3>待核实 · '+missing.length+' 项证据缺口</h3>'+list(missing.slice(0,3))+(missing.length>3?details('查看其余 '+(missing.length-3)+' 项',list(missing.slice(3))):'')+'<p class="subtle">资料不足会影响评分和评级；低分不等同于公司质量差。</p></section>':'';
    const cases=(full.scenarios||[]).map(s=>'<h4>'+esc(s.name)+'</h4>'+paragraphs(s.assumptions)+paragraphs(s.implication)).join('');
    const research=details('情景、催化因素与失效条件','<h4>什么情况需要重新判断</h4>'+list(full.falsification||[])+'<h4>待跟踪的催化因素</h4>'+list(full.catalysts||[])+cases);
    const original=details('完整结论与历史正文说明',longText(cleanConclusion(full.original_conclusion||r.conclusion))+(full.legacy_rule_score!==undefined?'<p class="subtle">历史正文中的旧规则分保留原始记录；顶部评分按新版百分制计算，请以顶部及评分拆解为准。</p>':''),'report-original');
    const blockers=full.decision?.rating_blockers||r.rating_blockers||[];
    const valuationPending=full.scoring?.items?.some(i=>i.group==='估值'&&!i.available);
    const caution=valuationPending?'<p class="metric-pending">估值比较依据未补齐，历史正文中的“合理、偏高、偏低”等估值判断仍属待验证的模型观点。</p>':'';
    const gate=blockers.length?details('为何未评级 · '+blockers.length+' 项条件待满足','<p>缺项时显示的是保守下限，不等于公司完整评分。补齐资料后才能形成买入、增持、中性、减持或卖出的方向评级。</p>'+list(blockers)+caution):'';
    const consistency=full.narrative_consistency;
    const consistencyNotes=consistency?.findings?.length?'<div class="metric-pending">'+list(consistency.findings.map(x=>x.reason))+'</div>':'';
    const narrative=consistencyNotes+panels+paragraphSection('原解读中的估值与交易条件',r.valuation)+research+'<section class="report-section"><h3>详细财报解读</h3><p class="subtle">按主题展开阅读，保留原报告的全部分析与页码。</p>'+chapters(full)+'</section>'+original;
    const follow=full.filing_performance;
    const performance=follow?details('财报发布后股价表现','<p class="subtle">'+esc(follow.basis)+'</p><p class="subtle">公告日期：'+esc(String(follow.disclosed_at).slice(0,10))+'；观察更新时间：'+esc(follow.observed_at)+'</p><div class="table-wrap"><table><thead><tr><th>交易日数</th><th>个股涨跌幅</th><th>相对沪深300</th></tr></thead><tbody>'+Object.entries(follow.horizons||{}).map(([days,result])=>'<tr><td>'+esc(days)+'日</td><td>'+esc(result.state==='evaluated'?String(result.return_pct)+'%':result.state==='pending'?'观察期未满':'行情待补齐')+'</td><td>'+esc(result.state==='evaluated'&&result.excess_pct!=null?String(result.excess_pct)+'%':'—')+'</td></tr>').join('')+'</tbody></table></div>'):'';
    return basic+metricCards(full)+gate+'<div class="report-legend" aria-label="颜色含义"><span>支持线索</span><span class="risk">风险关注</span><span class="pending">待核实</span></div>'+marketCards(full)+researchSection(full,'supporting')+pending+(narrativeStale?details('旧版模型正文（涉及已修正数据，待复核）',narrative):narrative)+scoreSection(full)+performance+provenance(full)+processingNotes+'<div class="source-footnote">模型：'+esc(r.provider)+' / '+esc(r.model)+'<br>生成时间：'+esc(full.generated_at||r.date)+'<br>'+link(r.source,'核对财报原文')+'</div></div>';
  }
  root.StockReportView={render,brief,cleanConclusion,headingMetadata,formatMetric,formatFinancial,metricCards,financialChange,financialValueTone,quarterCards,marketCards,scoreCell,researchSection,ratingSummary};
})(typeof window==='undefined'?globalThis:window);
