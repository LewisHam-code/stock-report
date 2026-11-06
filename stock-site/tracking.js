(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const names={buy:'买入',overweight:'增持',neutral:'中性',underweight:'减持',sell:'卖出',unrated:'未评级'};
  let data=null,horizon=3,page=1,markets={},refreshBusy=false,chartReportId=null;
  const viewNames={all:'全部',watchlist:'我的关注',recommended:'近期推荐',recent:'近期解读'};
  let view=new URLSearchParams(location.search).get('view')||'all';if(!viewNames[view])view='all';
  let sortKey=new URLSearchParams(location.search).get('sort');if(!['change','score'].includes(sortKey))sortKey='date';
  let sortDirection=sortKey!=='date'&&new URLSearchParams(location.search).get('order')==='asc'?'asc':'desc';
  let watched=new Set(),watchReady=false,watchError=false,recommended=new Set(),recommendationsReady=false,recommendationsBusy=false,reportIndex=[],indexReady=false,indexError=false,recommendationRevision='';
  const recommendationDetails=new Map();
  const perPage=30;
  const fmt=n=>typeof n==='number'&&Number.isFinite(n)?n.toFixed(2):'—';
  const pct=n=>typeof n==='number'&&Number.isFinite(n)?(n>0?'+':'')+n.toFixed(2)+'%':'—';
  const date=s=>s?String(s).slice(0,10):'—';
  const timestamp=s=>{const at=new Date(s);return s&&Number.isFinite(at.getTime())?at.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'未留存'};
  const today=()=>{const parts=Object.fromEntries(new Intl.DateTimeFormat('en',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()).map(p=>[p.type,p.value]));return parts.year+'-'+parts.month+'-'+parts.day};
  function quoteCheck(cross){
    const resolution=cross?.price_resolution;
    if(resolution?.status==='resolved')return '差异后三源并行刷新，腾讯与'+({eastmoney:'东方财富',ths:'同花顺'}[resolution.selected_provider]||resolution.selected_provider)+'在可比时点报价接近，采用该报价（腾讯 '+fmt(resolution.price)+' 元，行情时点 '+timestamp(resolution.asof)+'）；盘中为近时点核对';
    if(cross?.status==='not_comparable')return '行情时点不可比：'+(cross.reason||'来源时间不同')+(resolution?.resampled_concurrently?'；已三源并行刷新，仍未能对齐，保留来源报价':'');
    if(cross?.fields?.price?.matches===false){const p=cross.fields.price;return '双源价格有差异（采用价 '+fmt(p.primary)+' 元，另一来源 '+fmt(p.secondary)+' 元），涨幅按采用价计算，待核对'+(resolution?.status==='unresolved'?'；腾讯未能确定一致报价':resolution?.status==='unavailable'?'；腾讯暂不可用':'')}
    if(cross?.fields?.price?.matches===true)return '双源价格核对一致';
    return cross?.status==='matched'?'双源核对一致':'双源核对未完成';
  }
  function current(row){
    if(row.current_price==null&&Number.isFinite(row.price)&&row.price>0)row={...row,current_price:row.price,current_asof:row.price_asof,
      current_checked_at:row.price_collected_at,current_cross_validation:row.price_cross_validation,current_change_pct:0,current_is_initial:true,
      current_after_interpretation:Date.parse(row.price_asof)>Date.parse(row.generated_at)};
    row={...row,current_change_pct:StockPriceTrend.change(row.current_price,row.price)};
    const record=markets[row.code],quote=record?.quote;
    if(!quote||!Number.isFinite(quote.price)||quote.price<=0||!Number.isFinite(Date.parse(quote.asof))||Date.parse(quote.asof)<Date.parse(row.current_asof||'1970-01-01'))return row;
    return {...row,current_price:quote.price,current_asof:quote.asof,current_checked_at:record.quote_checked_at||quote.fetched_at,
            current_cross_validation:quote.cross_validation,current_is_initial:false,current_after_interpretation:Date.parse(quote.asof)>Date.parse(row.generated_at),
            current_change_pct:StockPriceTrend.change(quote.price,row.price)};
  }
  function cell(row,value,sub,cls){const td=row.insertCell();if(cls)td.className=cls;td.textContent=value;if(sub){const small=document.createElement('small');small.textContent=sub;td.appendChild(small)}return td}
  function breakdown(summary){
    const target=$('trackingBreakdown');target.replaceChildren();
    const scope=document.createElement('p');scope.className='subtle';
    scope.textContent=`正式检验：${summary.evaluable||0}份报告、${summary.companies||0}家公司。暂定、事后补录或测试报告中，已有${summary.exploratory?.mature||0}份走满观察期，单独保留且不计入吻合率。同公司多份报告不是独立样本；走势模式仅用于排查，不能直接认定原因或自动修改模型。`;
    target.appendChild(scope);
    if(!summary.evaluable){const p=document.createElement('p');p.textContent='尚无可检验的正式评级与完整评分；分组统计和偏差归因待观察期成熟后显示。';target.appendChild(p);return}
    for(const [title,rows] of [['按评级',summary.by_rating||[]],['按评分段',summary.by_score||[]],['按原始行业',summary.by_industry||[]],['按解读模型',summary.by_model||[]],['按评分版本',summary.by_version||[]]]){
      const section=document.createElement('section');const heading=document.createElement('h3');heading.textContent=title;section.appendChild(heading);
      const table=document.createElement('table');const head=table.createTHead().insertRow();['分组','样本','方向吻合','平均超额'].forEach(v=>{const th=document.createElement('th');th.textContent=v;head.appendChild(th)});
      const body=table.createTBody();for(const r of rows){const tr=body.insertRow();[names[r.group]||r.group,r.count,r.alignment_pct+'%',pct(r.average_excess_pct)].forEach(v=>cell(tr,String(v)))}
      section.appendChild(table);target.appendChild(section);
    }
    if(summary.patterns?.length){const section=document.createElement('section');const h=document.createElement('h3');h.textContent='方向不符的走势模式';section.appendChild(h);const p=document.createElement('p');p.textContent=summary.patterns.map(x=>x.name+' '+x.count+' 份').join(' · ');section.appendChild(p);target.appendChild(section)}
  }
  function render(){
    if(!data)return;
    const rows=StockTrackingViews.sort(StockTrackingViews.select(data.reports,view,watched,recommended).map(current),sortKey,sortDirection);
    const s=StockTrackingViews.summarize(rows,horizon);
    const waiting=view==='watchlist'&&!watchReady&&!watchError||view==='recommended'&&!recommendationsReady;
    [...$('trackingViewTabs').children].forEach(b=>b.setAttribute('aria-selected',String(b.dataset.trackingView===view)));
    $('trackingViewNote').textContent=view==='watchlist'?(watchError?'关注列表暂不可用，请检查本机设置服务。':waiting?'正在读取关注列表……':'只显示已关注股票的全部解读追踪；关注不代表推荐。'):view==='recommended'?(indexError?'推荐索引暂不可用，稍后自动重试。':waiting?'正在核对近期推荐的证据与评分……':'与报告库使用同一套最近7天的推荐条件，每家公司仅纳入最新符合条件的解读。'):view==='recent'?'按解读生成时间显示最近7天的追踪，按解读时间从新到旧排列。':'显示全部已保存的解读追踪，按解读时间从新到旧排列。';
    $('trackedCount').textContent=waiting?'—':rows.length;
    if(sortKey==='change')$('trackingViewNote').textContent=$('trackingViewNote').textContent.replace(/，?按解读时间从新到旧排列。/g,'。')+` 当前按涨幅${sortDirection==='asc'?'从低到高':'从高到低'}排列，缺少涨幅的记录暂按0%排序。`;
    if(sortKey==='score')$('trackingViewNote').textContent=$('trackingViewNote').textContent.replace(/，?按解读时间从新到旧排列。/g,'。')+` 当前按当时评分${sortDirection==='asc'?'从低到高':'从高到低'}排列，同分按解读时间从新到旧，缺少有效评分的记录排在最后。`;
    document.querySelectorAll('[data-tracking-sort]').forEach(button=>{
      const active=button.dataset.trackingSort===sortKey;
      const ascending=active&&sortDirection==='asc';
      button.closest('th').setAttribute('aria-sort',active?(ascending?'ascending':'descending'):'none');button.setAttribute('aria-pressed',String(active));
      button.querySelector('.sort-indicator').textContent=active?(ascending?'↑':'↓'):'⇣';
      if(['change','score'].includes(button.dataset.trackingSort)){
        const label=button.dataset.trackingSort==='score'?'当时评分':'涨幅';
        button.title=active?`当前${ascending?'升序':'降序'}，点击按${label}${ascending?'从高到低':'从低到高'}排序`:`按${label}从高到低排序，再次点击切换升序`;
      }
    });
    $('matureCount').textContent=s.mature||0;
    $('eligibleCount').textContent=s.evaluable||0;
    $('alignmentRate').textContent=s.alignment_pct==null?'待积累':s.alignment_pct+'%';
    $('trackingStatus').textContent=[`${horizon}个交易日`,`${rows.length}份解读`,`${s.pending||0}份待观察`,
      s.data_gap?`${s.data_gap}份个股行情待补齐`:null,s.benchmark_gap?`${s.benchmark_gap}份基准行情待补齐`:null,
      `方向吻合率${s.alignment_pct==null?'待积累':s.alignment_pct+'%'}`,
      s.relative_alignment_pct==null?null:`相对沪深300吻合率 ${s.relative_alignment_pct}%`].filter(Boolean).join(' · ');
    breakdown(s);
    [...$('horizonButtons').children].forEach(b=>b.setAttribute('aria-pressed',String(Number(b.dataset.horizon)===horizon)));
    const total=Math.max(1,Math.ceil(rows.length/perPage));page=Math.min(page,total);
    $('trackingRows').replaceChildren();
    for(const original of rows.slice((page-1)*perPage,page*perPage)){
      const r=current(original);
      const tr=$('trackingRows').insertRow();
      const first=cell(tr,'');
      first.insertAdjacentHTML('afterbegin',StockWatchAction.label(r.company,r.code,'company-name',{market:r.market}));
      const detail=document.createElement('div');detail.className='tracking-company-note';
      const interpreted=document.createElement('time');interpreted.dateTime=r.generated_at||'';interpreted.textContent='解读日期：'+timestamp(r.generated_at);detail.appendChild(interpreted);first.appendChild(detail);
      const priceBasis=r.price_basis==='closed_session_reconstruction'?'解读前收盘价 · 事后补录；':r.price_basis==='publication_capture'?'报告入库时采集；':'';
      cell(tr,r.price==null?'待补齐':fmt(r.price)+' 元',priceBasis+'更新时间：'+timestamp(r.price_collected_at));
      cell(tr,r.current_price==null?'暂未获取':fmt(r.current_price)+' 元',
        '更新时间：'+timestamp(r.current_checked_at));
      const change=cell(tr,'');
      const changeValue=document.createElement('div');changeValue.className='price-change';
      const missingChange=!Number.isFinite(r.current_change_pct);
      const amount=document.createElement('span');amount.textContent=pct(missingChange?0:r.current_change_pct);amount.className=r.current_change_pct>0?'up':r.current_change_pct<0?'down':'';changeValue.appendChild(amount);
      const button=document.createElement('button');button.type='button';button.className='trend-button';button.dataset.trend=r.report_id;
      button.setAttribute('aria-label','查看'+r.company+'（'+r.code+'）的股价走势图');button.title='查看解读日起的股价走势';
      button.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3v18h18M5 16l5-6 4 3 6-8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      changeValue.appendChild(button);change.insertBefore(changeValue,change.firstChild);
      if(missingChange){const note=document.createElement('small');note.textContent='缺价暂按0';change.appendChild(note)}
      cell(tr,names[r.rating]||'未评级',`${r.score==null?'—':fmt(r.score)} 分 · ${r.provenance==='on_time'?'发布时已冻结':'事后补录，仅作参考'}`);
      const result=r.horizons[String(horizon)]||{};
      if(result.state==='evaluated')cell(tr,pct(result.return_pct),`沪深300 ${pct(result.benchmark_pct)} · 超额 ${pct(result.excess_pct)} · ${result.entry_date} 至 ${result.end_date}`);
      else cell(tr,result.state==='data_gap'?'行情待补齐':'待观察',result.reason||`已获得 ${result.sessions||0} / ${horizon} 个交易日`);
      cell(tr,result.state==='data_gap'?'行情待补齐':result.state!=='evaluated'?'未到检验时间':!result.evaluable?'不计入吻合率':result.aligned?'方向吻合':'方向不符',!result.evaluable&&result.state==='evaluated'?(r.provenance==='backfilled'?'原始评分未同步留存':'评级或评分资料不完整'):'');
      const reportCell=cell(tr,'',null,'tracking-report-link');
      const buttonReport=document.createElement('button');buttonReport.type='button';buttonReport.className='link-btn';buttonReport.dataset.report=r.report_id;buttonReport.textContent='解读报告 →';reportCell.appendChild(buttonReport);
    }
    $('trackingEmpty').hidden=waiting||rows.length>0;
    $('trackingEmpty').querySelector('strong').textContent=view==='watchlist'?'关注股票暂无解读追踪':view==='recommended'?'暂无符合条件的近期推荐':'暂无可追踪的解读';
    $('trackingEmpty').querySelector('p').textContent=watchError&&view==='watchlist'?'关注列表读取失败，请稍后重试。':view==='recommended'?'需正式评级、完整评分及成长和风险证据达到报告库的推荐条件。':view==='watchlist'?'可先在报告库添加关注；已有解读的关注股票会显示在这里。':'发布解读报告后会自动进入这里。';
    $('trackingPagination').hidden=rows.length<=perPage;
    $('trackingRange').textContent=rows.length?`第 ${(page-1)*perPage+1}–${Math.min(page*perPage,rows.length)} 条，共 ${rows.length} 条`:'共 0 条';
    $('trackingPage').textContent=`第 ${page} / ${total} 页`;
    $('trackingPrev').disabled=page===1;$('trackingNext').disabled=page===total;
  }
  function renderChart(){
    const original=data?.reports.find(r=>r.report_id===chartReportId);if(!original)return;
    const row=current(original),live=markets[row.code]?.trend;
    const series=live?.adjustment==='none'?live:row.price_trend?.adjustment==='none'?row.price_trend:null;
    $('trendTitle').innerHTML=StockWatchAction.label(row.company,row.code,'stock-heading-company',{market:row.market,watch:false})+'<span>股价走势</span>';
    $('trendRange').textContent='解读时间 '+timestamp(row.generated_at)+' 至 '+today()+' · 实际价格（元）';
    $('trendChart').replaceChildren(StockPriceTrend.create(series,row,row,today()));
    $('trendQuoteNote').textContent='解读时价格行情时点：'+timestamp(row.price_asof)+'；采集时间：'+timestamp(row.price_collected_at)+
      (row.price_basis==='closed_session_reconstruction'?'；解读前收盘价为同次休市期间事后补录，非原模型输入':row.price_basis==='publication_capture'?'；报告入库时采集，非原模型输入':'')+
      '。最新价格行情时点：'+timestamp(row.current_asof)+'；更新时间：'+timestamp(row.current_checked_at)+'；'+quoteCheck(row.current_cross_validation)+'。'+
      (row.current_is_initial?'最新价格暂以解读时股价作为初始值。':'')+(row.current_after_interpretation===false?'最新行情时点早于或等于解读时间。':'')+
      (markets[row.code]?.errors?.quote?'本轮报价采集未成功，保留上次报价。':'');
    const check={matched:'双源日线核对一致',conflict:'双源日线存在差异',not_comparable:'双源日线缺少可比日期',unavailable:'双源日线核对未完成'};
    $('trendSource').textContent=(check[series?.cross_validation?.status]||'日线待采集')+'；更新 '+timestamp(series?.checked_at)+(markets[row.code]?.errors?.series?'；本轮日线采集未成功，保留原数据。':'');
    if(series?.source){
      try{const url=new URL(series.source);if(url.protocol==='https:'){const link=document.createElement('a');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';link.textContent=' 查看行情来源 ↗';$('trendSource').appendChild(link)}}catch{}
    }
  }
  async function loadReportDetail(report){
    if(!/^\.\/data\/report-details\/[a-zA-Z0-9-]+\.json$/.test(report.detail_url||''))throw Error('详细文件地址无效');
    if(recommendationDetails.has(report.id))return recommendationDetails.get(report.id);
    const pending=(async()=>{
      const response=await fetch(report.detail_url,{cache:'no-store'});if(!response.ok)throw Error('详细文件暂不可用');
      const detail=await response.json();if(detail?.id!==report.id)throw Error('报告版本不匹配');return detail;
    })();
    recommendationDetails.set(report.id,pending);
    try{return await pending}catch(error){if(recommendationDetails.get(report.id)===pending)recommendationDetails.delete(report.id);throw error}
  }
  let reportOverflow='';
  async function openReport(id){
    const dialog=$('detailDialog'),body=$('detailBody');body.dataset.reportId=id;
    $('detailTitle').textContent='解读报告';body.textContent='正在读取报告……';body.scrollTop=0;
    $('detailMetadata').replaceChildren();
    $('detailAssessment').replaceChildren();
    if(!dialog.open){reportOverflow=document.documentElement.style.overflow;document.documentElement.style.overflow='hidden';dialog.showModal()}
    try{
      const report=await StockReportCollections.resolveReport(id,reportIndex,loadReportDetail);
      if(body.dataset.reportId!==id||!dialog.open)return;
      if(!report||report.id!==id)throw Error('报告版本不匹配');
      $('detailTitle').replaceChildren();
      $('detailTitle').insertAdjacentHTML('beforeend',StockWatchAction.label(report.company,report.code,'stock-heading-company',{market:report.market}));
      $('detailMetadata').innerHTML=StockReportView.headingMetadata(report);
      $('detailAssessment').innerHTML=StockReportView.ratingSummary(report);
      body.innerHTML=StockReportView.render(report,undefined,{externalRating:true});
      const detail=await loadReportDetail(report);
      if(body.dataset.reportId===id&&dialog.open){$('detailMetadata').innerHTML=StockReportView.headingMetadata(report,detail);$('detailAssessment').innerHTML=StockReportView.ratingSummary(report,detail);body.innerHTML=StockReportView.render(report,detail,{externalRating:true})}
    }catch{
      if(body.dataset.reportId===id&&dialog.open){
        if(body.textContent==='正在读取报告……')body.textContent='';
        const message=document.createElement('p');message.className='subtle';message.textContent='详细文件暂不可用，请关闭后重试。';body.appendChild(message);
      }
    }
  }
  $('trackingRows').addEventListener('click',event=>{
    const report=event.target.closest('[data-report]');if(report){void openReport(report.dataset.report);return}
    const button=event.target.closest('[data-trend]');if(!button)return;chartReportId=button.dataset.trend;renderChart();$('trendDialog').showModal();
  });
  ['closeDetail','closeDetailBottom'].forEach(id=>$(id).addEventListener('click',()=>$('detailDialog').close()));
  $('detailDialog').addEventListener('close',()=>{delete $('detailBody').dataset.reportId;document.documentElement.style.overflow=reportOverflow});
  ['closeTrend','closeTrendBottom'].forEach(id=>$(id).addEventListener('click',()=>$('trendDialog').close()));
  $('trendDialog').addEventListener('close',()=>{chartReportId=null});
  $('trackingPrev').addEventListener('click',()=>{page--;render()});
  $('trackingNext').addEventListener('click',()=>{page++;render()});
  document.querySelectorAll('[data-tracking-sort]').forEach(button=>button.addEventListener('click',()=>{
    const nextKey=button.dataset.trackingSort;
    sortDirection=nextKey!=='date'&&sortKey===nextKey&&sortDirection==='desc'?'asc':'desc';
    sortKey=nextKey;page=1;const params=new URLSearchParams(location.search);
    if(sortKey!=='date'){params.set('sort',sortKey);if(sortDirection==='asc')params.set('order','asc');else params.delete('order')}
    else{params.delete('sort');params.delete('order')}
    history.replaceState(null,'',location.pathname+'?'+params.toString());render();
  }));
  $('trackingViewTabs').addEventListener('click',event=>{const button=event.target.closest('[data-tracking-view]');if(!button)return;view=button.dataset.trackingView;page=1;const params=new URLSearchParams(location.search);params.set('view',view);history.replaceState(null,'',location.pathname+'?'+params.toString());render();if(view==='recommended')void ensureRecommendations()});
  window.addEventListener('stock-watchlist-updated',event=>{watched=new Set((event.detail.items||[]).map(r=>r.code));watchReady=true;watchError=false;page=1;render()});
  async function ensureRecommendations(){
    if(!indexReady||recommendationsReady||recommendationsBusy)return;
    recommendationsBusy=true;const revision=recommendationRevision;
    try{
      const rows=await StockReportCollections.recommendations(reportIndex,loadReportDetail);
      if(revision===recommendationRevision){recommended=new Set(rows.map(r=>r.id));recommendationsReady=true;render()}
    }finally{recommendationsBusy=false}
  }
  async function start(){
    if(refreshBusy||$('detailDialog').open)return;refreshBusy=true;
    try{
      const results=await Promise.allSettled(['./data/tracking.json','./data/tracking-market.json','./data/reports.json','/api/watchlist'].map(async url=>{
        if(url==='/api/watchlist')return StockWatchAction.request();
        const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw Error();return response.json();
      }));
      if(results[0].status==='fulfilled')data=results[0].value;
      if(results[1].status==='fulfilled')markets=results[1].value.stocks||{};
      indexError=results[2].status!=='fulfilled';
      if(!indexError){
        indexReady=true;
        reportIndex=results[2].value.reports||[];const revision=JSON.stringify(reportIndex.map(r=>[r.id,r.rating,r.score,r.score_status,r.score_coverage,r.evidence_status]));
        if(revision!==recommendationRevision){recommendationRevision=revision;recommendationsReady=false;recommendationDetails.clear()}
      }
      watchError=results[3].status!=='fulfilled'||!results[3].value.ok;
      if(!watchError){watched=new Set((results[3].value.items||[]).map(r=>r.code));watchReady=true}
    }finally{refreshBusy=false}
    if(!data){$('updatedAt').textContent='追踪数据暂不可用';$('trackingStatus').textContent='尚未生成追踪数据，请在设置中运行一次研究流程。';return}
    $('updatedAt').textContent='数据更新 '+new Date(data.generated_at).toLocaleString('zh-CN',{hour12:false});
    $('trackingMethod').textContent=data.method?.basis+'。'+data.method?.alignment;
    $('trackingLimits').textContent=data.method?.limits;
    if(!$('horizonButtons').children.length)for(const n of data.horizons){const b=document.createElement('button');b.type='button';b.dataset.horizon=n;b.textContent=n+' 天';b.addEventListener('click',()=>{horizon=n;page=1;render()});$('horizonButtons').appendChild(b)}
    if($('detailDialog').open)return;
    render();
    if(view==='recommended')void ensureRecommendations();
    if($('trendDialog').open)renderChart();
  }
  start();
  setInterval(()=>{if(!document.hidden)start()},60000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)start()});
})();
