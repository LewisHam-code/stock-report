(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const STORE='stock-research-local-v1';
  const ratings={buy:'买入',overweight:'增持',neutral:'中性',underweight:'减持',sell:'卖出',unrated:'未评级'};
  const viewNames={all:'全部',watchlist:'我的关注',recommended:'近期推荐',recent:'近期解读'};
  const safe=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let reports=[],systemReports=[],recommended=[],watchlist=null,watchlistError=false,view='all',rating='';
  let reportLoadError=false,reportsReady=false,recommendationsReady=false,page=StockPagination.parsePage(new URLSearchParams(location.search).get('page'));
  let historicalReports=[],historicalRevision=null,historicalFailed=false,historicalBusy=false,historicalFailures=0;
  let requestedPeriod=new URLSearchParams(location.search).get('period')||'';
  const normalizeSort=key=>key==='score'||StockReportCollections.RESEARCH_SORTS[key]?key:'date';
  let sortKey=normalizeSort(new URLSearchParams(location.search).get('sort'));
  const periodName=value=>String(value||'').replace(/FY$/,'年报');
  const detailCache=new Map();
  function parseLocal(){try{const data=JSON.parse(localStorage.getItem(STORE)||'[]');return Array.isArray(data)?data:[]}catch{return []}}
  function normalize(report){
    if(!report||typeof report!=='object'||!report.id||!/^[0-9]{6}(?:\.(?:SH|SZ|BJ))?$/i.test(String(report.code||''))||!report.company||!report.date||!report.conclusion)return null;
    const allowed=/^\.\/data\/report-details\/[a-zA-Z0-9-]+\.json$/;
    return {...report,id:String(report.id),code:String(report.code),company:String(report.company),industry:String(report.industry||'未核实'),date:String(report.date),period:String(report.period||''),rating:ratings[report.rating]?report.rating:'unrated',evidence_status:report.evidence_status==='sufficient'?'sufficient':'insufficient',topics:Array.isArray(report.topics)?report.topics.map(String):[],conclusion:String(report.conclusion),detail_url:allowed.test(String(report.detail_url||''))?report.detail_url:''};
  }
  function merge(system,local){const map=new Map();[...system,...local].forEach(raw=>{const item=normalize(raw);if(item)map.set(item.id,item)});return StockReportCollections.sortReports([...map.values()])}
  async function loadDetail(report){
    if(detailCache.has(report.id))return detailCache.get(report.id);
    if(!report.detail_url)throw Error('没有详细文件');
    const response=await fetch(report.detail_url,{cache:'no-store'});
    if(!response.ok)throw Error('详细文件暂不可用');
    const detail=await response.json();detailCache.set(report.id,detail);return detail;
  }
  function selected(){
    if(view==='watchlist'){
      if(watchlistError||watchlist===null)return [];
      const codes=new Set(watchlist.map(item=>item.code));return reports.filter(report=>codes.has(report.code));
    }
    if(view==='recommended')return recommended;
    if(view==='recent')return reports.filter(report=>StockReportCollections.recentEnough(report));
    return reports;
  }
  function setOptions(id,values,label,display=value=>value){
    const el=$(id),old=el.value;el.replaceChildren(new Option(label,''));
    [...new Set(values.filter(Boolean))].sort((a,b)=>a.localeCompare(b,'zh-CN')).forEach(value=>el.add(new Option(display(value),value)));
    el.value=old;
    if(el.selectedIndex<0)el.value='';
  }
  function queryView(){const requested=new URLSearchParams(location.search).get('view');return viewNames[requested]?requested:'all'}
  function researchSortHint(report){
    const name=StockReportCollections.RESEARCH_SORTS[sortKey];if(!name)return '';
    const id=sortKey.slice(9),item=['short','medium'].includes(id)?report.research_composites?.[id]:report.research_scores?.[id];
    const valid=item?.complete&&typeof item.score==='number'&&Number.isFinite(item.score);
    return '<span class="company-code research-sort-hint">'+safe(name)+'：'+(valid?safe(item.score.toFixed(1))+' 分':'待补齐')+'<br>基础覆盖 '+safe(item?.coverage_pct??0)+'%</span>';
  }
  function updateUrl(params){history.replaceState(null,'',location.pathname+(params.size?'?'+params.toString():''))}
  function updatePageUrl(){const params=new URLSearchParams(location.search);if(page>1)params.set('page',String(page));else params.delete('page');updateUrl(params)}
  function changePage(next){page=next;updatePageUrl();render();document.querySelector('.library').scrollIntoView({block:'start'})}
  function resetPage(){page=1;updatePageUrl();render()}
  function setView(next,update=true){
    view=viewNames[next]?next:'all';
    document.querySelectorAll('.reports-view-tabs [data-view]').forEach(button=>button.setAttribute('aria-selected',String(button.dataset.view===view)));
    for(const [id,active] of [['watchlistNav',view==='watchlist'],['reportsNav',view!=='watchlist']]){
      const item=$(id);item.classList.toggle('active',active);
      if(active)item.setAttribute('aria-current','page');else item.removeAttribute('aria-current');
    }
    $('watchlistManage').hidden=view!=='watchlist';
    $('viewTitle').textContent=viewNames[view];
    if(update){page=1;const params=new URLSearchParams(location.search);params.set('view',view);params.delete('report');params.delete('page');updateUrl(params)}
    render();
  }
  function render(){
    $('researchSort').value=sortKey;
    const base=selected();
    setOptions('industryFilter',base.map(report=>report.industry),'全部行业');
    setOptions('periodFilter',base.map(report=>report.period),'全部报告期',periodName);
    if(requestedPeriod&&[...$('periodFilter').options].some(option=>option.value===requestedPeriod)){$('periodFilter').value=requestedPeriod;requestedPeriod=''}
    setOptions('topicFilter',base.flatMap(report=>report.topics),'全部热点');
    const q=$('search').value.trim().toLocaleLowerCase(),industry=$('industryFilter').value,period=$('periodFilter').value,topic=$('topicFilter').value,evidence=$('evidenceFilter').value,date=$('dateFilter').value;
    const filtered=StockReportCollections.sortReports(base.filter(report=>(!rating||report.rating===rating)&&(!industry||report.industry===industry)&&(!period||report.period===period)&&(!topic||report.topics.includes(topic))&&(!evidence||report.evidence_status===evidence)&&(!date||report.date===date)&&(!q||[report.code,StockWatchAction.searchNames(report),report.industry,report.period,periodName(report.period),report.conclusion,...report.topics].join(' ').normalize('NFKC').toLocaleLowerCase().includes(q.normalize('NFKC')))),sortKey);
    document.querySelectorAll('[data-report-sort]').forEach(button=>{
      const active=button.dataset.reportSort===sortKey;
      button.closest('th').setAttribute('aria-sort',active?'descending':'none');
      button.setAttribute('aria-pressed',String(active));
      button.querySelector('.sort-indicator').textContent=active?'↓':'⇣';
    });
    const waiting=!reportsReady||view==='recommended'&&!recommendationsReady||view==='watchlist'&&watchlist===null&&!watchlistError;
    const paged=StockPagination.paginate(filtered,page);
    if(!waiting&&page!==paged.page){page=paged.page;updatePageUrl()}
    $('resultCount').textContent=waiting?'正在读取报告':filtered.length+' 条结果';
    $('scoreCoverageNote').textContent=filtered.some(report=>report.score_status!=='calculated')?'部分报告评分资料未齐，显示的分数为保守下限；未评级不代表卖出。':'';
    if(StockReportCollections.RESEARCH_SORTS[sortKey])$('scoreCoverageNote').textContent='试运行：仅按该维度完整分降序排列，缺失或不完整值放在最后；不把缺项保守下限和完整分混排。';
    $('reportRows').innerHTML=paged.items.map(report=>'<tr><td>'+StockWatchAction.label(report.company,report.code,'company-name',{market:report.market})+'</td><td class="report-conclusion">'+safe(StockReportView.brief(report.conclusion))+'</td><td>'+safe(periodName(report.period))+'</td><td><span class="badge rating-'+safe(report.rating)+'">'+ratings[report.rating]+'</span>'+(report.report_kind==='historical_foundation'?'<span class="company-code">'+(report.review_state==='accepted'?'核心指标核对齐备':'存在待复核项')+'</span>':report.evidence_status==='insufficient'?'<span class="company-code">证据不足</span>':'')+'</td><td>'+StockReportView.scoreCell(report)+researchSortHint(report)+'</td><td>'+safe(report.date)+'</td><td><button type="button" class="link-btn" data-report="'+safe(report.id)+'">查看 →</button></td></tr>').join('');
    $('libraryEmpty').hidden=waiting||filtered.length>0;
    $('reportPagination').hidden=waiting||paged.pages<2;
    $('reportPageRange').textContent='显示 '+paged.from+'–'+paged.to+' 条，共 '+paged.total+' 条';
    $('reportPageInfo').textContent='第 '+paged.page+' / '+paged.pages+' 页';
    $('reportPrev').disabled=paged.page<=1;
    $('reportNext').disabled=paged.page>=paged.pages;
    const note={
      watchlist:watchlistError?'关注列表暂不可用，请检查本机设置服务。':watchlist===null?'正在读取关注列表……':'显示已关注股票的全部解读；关注不代表推荐。',
      recommended:'每家公司仅看最新解读，筛选最近7天正式买入或增持、买入评分≥65且覆盖率≥85%、成长分≥15/25且覆盖≥20/25、营收与扣非利润同比为正、无重大风险标记的系统报告。需原文和行业估值证据齐全。',
      recent:'按解读生成时间显示最近7天的报告；更早记录可在“全部”查看。',
      all:'显示全部已保存的报告，不限制解读时间。'
    };
    $('viewNote').textContent=note[view]+(StockReportCollections.RESEARCH_SORTS[sortKey]?' 当前按'+StockReportCollections.RESEARCH_SORTS[sortKey]+'完整分降序排列；六维研究仍为试运行。':sortKey==='score'?' 当前按买入评分从高到低排列；同分时最新解读优先。':' 当前按解读时间从新到旧排列。');
    $('historicalLoadState').textContent=historicalFailed?'历史补建索引暂不可用，当前展示已取得的报告；可刷新重试。':historicalReports.length?'已纳入 '+historicalReports.length+' 份历史补建报告，可按报告期筛选；这些底稿不计入正式预测检验。':'';
    $('emptyReason').textContent=reportLoadError?'报告索引暂不可用，请检查本机网站文件。':view==='recommended'?'目前没有符合完整证据和成长门槛的正式推荐。':view==='watchlist'?(watchlistError?'请检查本机设置服务。':watchlist?.length?'关注股票尚无符合筛选条件的解读。':'请先在首页添加关注股票。'):'可调整筛选条件，或从首页新增解读。';
  }
  async function openReport(id){
    const body=$('detailBody');body.dataset.reportId=id;
    $('detailTitle').textContent='解读报告';
    $('detailMetadata').replaceChildren();
    $('detailAssessment').replaceChildren();
    body.textContent='正在读取报告……';body.scrollTop=0;if(!$('detailDialog').open)$('detailDialog').showModal();
    const params=new URLSearchParams(location.search);params.set('view',view);params.set('report',id);updateUrl(params);
    try{
      const report=normalize(await StockReportCollections.resolveReport(id,reports,loadDetail));
      if(!report)throw Error('报告信息不完整');
      if(body.dataset.reportId!==id||!$('detailDialog').open)return;
      $('detailTitle').innerHTML=StockWatchAction.label(report.company,report.code,'stock-heading-company',{market:report.market});
      $('detailMetadata').innerHTML=StockReportView.headingMetadata(report);
      $('detailAssessment').innerHTML=StockReportView.ratingSummary(report);
      body.innerHTML=StockReportView.render(report,undefined,{externalRating:true});
      if(!report.detail_url)return;
      const detail=await loadDetail(report);if(body.dataset.reportId===id&&$('detailDialog').open){$('detailMetadata').innerHTML=StockReportView.headingMetadata(report,detail);$('detailAssessment').innerHTML=StockReportView.ratingSummary(report,detail);body.innerHTML=StockReportView.render(report,detail,{externalRating:true})}
    }
    catch{if(body.dataset.reportId===id&&$('detailDialog').open){if(body.textContent==='正在读取报告……')body.textContent='';body.insertAdjacentHTML('beforeend','<p class="subtle">详细文件暂不可用，请稍后重试。</p>')}}
  }
  function closeReport(){if(!$('detailDialog').open)return;$('detailDialog').close();const params=new URLSearchParams(location.search);params.delete('report');updateUrl(params)}
  document.querySelectorAll('.reports-view-tabs [data-view]').forEach(button=>button.addEventListener('click',()=>setView(button.dataset.view)));
  ['search','industryFilter','periodFilter','topicFilter','evidenceFilter','dateFilter'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',()=>{
    if(id==='periodFilter'){requestedPeriod='';const params=new URLSearchParams(location.search);if($(id).value)params.set('period',$(id).value);else params.delete('period');updateUrl(params)}
    resetPage();
  }));
  document.querySelectorAll('[data-rating]').forEach(button=>button.addEventListener('click',()=>{rating=button.dataset.rating;document.querySelectorAll('[data-rating]').forEach(other=>{other.classList.toggle('active',other===button);other.setAttribute('aria-selected',String(other===button))});resetPage()}));
  $('resetFilters').addEventListener('click',()=>{requestedPeriod='';const params=new URLSearchParams(location.search);params.delete('period');updateUrl(params);['search','industryFilter','periodFilter','topicFilter','evidenceFilter','dateFilter'].forEach(id=>$(id).value='');document.querySelector('[data-rating=""]').click()});
  $('reportRows').addEventListener('click',event=>{const button=event.target.closest('[data-report]');if(button)openReport(button.dataset.report)});
  $('reportPrev').addEventListener('click',()=>changePage(page-1));
  $('reportNext').addEventListener('click',()=>changePage(page+1));
  $('researchSort').addEventListener('change',()=>{sortKey=normalizeSort($('researchSort').value);const params=new URLSearchParams(location.search);if(sortKey==='date')params.delete('sort');else params.set('sort',sortKey);updateUrl(params);resetPage()});
  document.querySelectorAll('[data-report-sort]').forEach(button=>button.addEventListener('click',()=>{
    sortKey=button.dataset.reportSort;
    const params=new URLSearchParams(location.search);
    if(sortKey==='score')params.set('sort','score');else params.delete('sort');
    updateUrl(params);resetPage();
  }));
  ['closeDetail','closeDetailBottom'].forEach(id=>$(id).addEventListener('click',closeReport));
  $('detailDialog').addEventListener('close',()=>{const params=new URLSearchParams(location.search);if(params.has('report')){params.delete('report');updateUrl(params)}});
  $('exportBtn').addEventListener('click',()=>{const blob=new Blob([JSON.stringify({reports:parseLocal()},null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const anchor=document.createElement('a');anchor.href=url;anchor.download='stock-reports-'+new Date().toISOString().slice(0,10)+'.json';anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000)});
  window.addEventListener('stock-watchlist-updated',event=>{watchlist=event.detail.items||[];watchlistError=false;render()});
  window.addEventListener('stock-watchlist-error',()=>{watchlist=null;watchlistError=true;render()});
  window.addEventListener('popstate',()=>{sortKey=normalizeSort(new URLSearchParams(location.search).get('sort'));page=StockPagination.parsePage(new URLSearchParams(location.search).get('page'));setView(queryView(),false);const id=new URLSearchParams(location.search).get('report');if(id)openReport(id);else closeReport()});
  async function loadHistorical(){
    if(historicalBusy)return false;historicalBusy=true;
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),12000);
    try{
      let data;
      try{
        const response=await fetch('/api/historical-reports'+(historicalRevision===null?'':'?revision='+encodeURIComponent(historicalRevision)),{cache:'no-store',signal:controller.signal});
        if(!response.ok)throw Error();data=await response.json();
      }catch{
        const response=await fetch('./data/historical-reports.json',{cache:'no-store',signal:controller.signal});
        if(!response.ok)throw Error();data=await response.json();
      }
      historicalFailed=false;historicalFailures=0;
      if(data.unchanged||data.revision===historicalRevision)return false;
      historicalRevision=data.revision;historicalReports=merge(data.reports||[],[]);return true;
    }catch{historicalFailed=true;historicalFailures++;return false}
    finally{clearTimeout(timer);historicalBusy=false}
  }
  function combineReports(){reports=merge([...systemReports,...historicalReports],parseLocal());window.stockMainData={reports,filings:[]}}
  async function refreshHistorical(){
    if(document.hidden||!reportsReady||historicalFailures>=3)return;
    const changed=await loadHistorical();if(changed){combineReports();window.dispatchEvent(new Event('stock-main-data-rendered'))}render();
  }
  async function start(){
    setView(queryView(),false);
    const [reportResult,watchlistResult]=await Promise.allSettled([
      fetch('./data/reports.json',{cache:'no-store'}).then(response=>{if(!response.ok)throw Error();return response.json()}),
      fetch('/api/watchlist',{cache:'no-store'}).then(response=>{if(!response.ok)throw Error();return response.json()}),
      loadHistorical()
    ]);
    reportLoadError=reportResult.status==='rejected';
    const system=reportResult.status==='fulfilled'?(Array.isArray(reportResult.value)?reportResult.value:reportResult.value.reports||[]):[];
    systemReports=merge(system,[]);
    combineReports();
    reportsReady=true;
    window.stockMainData={reports,filings:[]};
    window.dispatchEvent(new Event('stock-main-data-rendered'));
    watchlistError=watchlistResult.status==='rejected'||!watchlistResult.value.ok;
    watchlist=watchlistError?null:watchlistResult.value.items||[];
    render();
    const id=new URLSearchParams(location.search).get('report');if(id)openReport(id);
    recommended=await StockReportCollections.recommendations(systemReports,loadDetail,new Date());
    recommendationsReady=true;
    render();
  }
  start();
  setInterval(refreshHistorical,30000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshHistorical()});
})();
