(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const safe=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const today=()=>new Date();
  let watched=null, watchlistError=false, revision=0;
  const detailCache=new Map();
  function link(view,report){
    const params=new URLSearchParams({view,report:report.id});
    return './reports.html?'+params;
  }
  function fill(id,view,reports,empty,limit){
    $(id).innerHTML=reports.slice(0,limit).map(report=>
      '<div class="research-item"><span class="research-item-title"><span class="research-item-name">'+StockWatchAction.label(report.company,report.code,'company-name',{market:report.market,href:link(view,report)})+'</span><time>'+safe(report.date)+'</time></span><a class="research-item-summary" href="'+safe(link(view,report))+'">'+safe(StockReportView.brief(report.conclusion))+'</a></div>'
    ).join('')||'<div class="research-empty">'+safe(empty)+'</div>';
  }
  function renderWatchedDigest(reports){
    const summary=$('watchedDigestSummary');
    const list=$('watchedDigestList');
    if(!summary||!list)return;
    if(watchlistError){summary.textContent='关注列表暂不可用';list.innerHTML='';return;}
    if(!watched){summary.textContent='正在读取关注列表';list.innerHTML='';return;}
    const now=today();
    const weekStart=new Date(now.getFullYear(),now.getMonth(),now.getDate()-((now.getDay()+6)%7));
    const dateString=date=>[date.getFullYear(),String(date.getMonth()+1).padStart(2,'0'),String(date.getDate()).padStart(2,'0')].join('-');
    const start=dateString(weekStart),end=dateString(now);
    const codes=new Set(watched.map(item=>item.code));
    const weekly=reports.filter(report=>codes.has(report.code)&&report.date>=start&&report.date<=end);
    const shown=weekly.slice(0,4);
    summary.textContent=watched.length+' 只关注股票 · 本周 '+weekly.length+' 份解读'+(weekly.length>4?' · 显示前 4 条':'');
    list.innerHTML=shown.map(report=>'<li><span class="digest-row">'+StockWatchAction.label(report.company,report.code,'company-name',{market:report.market,href:link('watchlist',report)})+'<span class="digest-rating">'+safe(({buy:'买入',overweight:'增持',neutral:'中性',underweight:'减持',sell:'卖出',unrated:'未评级'})[report.rating]||'未评级')+'</span></span></li>').join('')||'<li class="digest-empty">'+(watched.length?'本周暂无关注股票的解读':'尚未添加关注股票')+'</li>';
  }
  async function render(){
    const current=++revision;
    const reports=window.stockMainData?.reports||[];
    const codes=new Set((watched||[]).map(item=>item.code));
    const watchedReports=reports.filter(report=>codes.has(report.code));
    const recentReports=StockReportCollections.sortReports(reports.filter(report=>StockReportCollections.recentEnough(report,today())));
    renderWatchedDigest(reports);
    const recommended=await StockReportCollections.recommendations(window.stockMainData?.systemReports||[],async report=>{
      if(!detailCache.has(report.id)){
        const pending=fetch(report.detail_url,{cache:'no-store'}).then(response=>{if(!response.ok)throw Error('详细报告不可用');return response.json()})
          .catch(error=>{detailCache.delete(report.id);throw error});
        detailCache.set(report.id,pending);
      }
      return detailCache.get(report.id);
    },today());
    if(current!==revision)return;
    const limit=5;
    $('recentSummary').textContent='最近7天 '+recentReports.length+' 份解读'+(recentReports.length?' · 首页显示 '+Math.min(recentReports.length,limit)+' 条':'');
    fill('recentReports','recent',recentReports,'最近7天暂无解读报告，可在报告库“全部”查看历史记录',limit);
    if(watchlistError){
      $('watchlistSummary').textContent='关注列表暂不可用';
      fill('watchlistReports','watchlist',[],'请检查本机设置服务',limit);
    }else if(watched){
      $('watchlistSummary').textContent=watched.length+' 只关注股票 · '+watchedReports.length+' 份解读';
      fill('watchlistReports','watchlist',watchedReports,watched.length?'关注股票尚无解读':'尚未添加关注股票',limit);
    }
    $('recommendSummary').textContent='最近7天 '+recommended.length+' 份符合推荐门槛';
    fill('recommendedReports','recommended',recommended,'暂无符合条件的正式推荐；证据不足或评分未齐的报告不计入',limit);
  }
  window.addEventListener('stock-main-data-rendered',render);
  window.addEventListener('stock-watchlist-updated',event=>{watched=event.detail.items||[];watchlistError=false;render()});
  window.addEventListener('stock-watchlist-error',()=>{watchlistError=true;render()});
  if(window.stockMainData)render();
})();
