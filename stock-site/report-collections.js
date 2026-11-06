(function(root){
  'use strict';
  const DAY=86400000;
  const RECENT_DAYS=7;
  const RESEARCH_SORTS={research_quality:'财务质量',research_growth:'成长与财报变化',research_industry:'行业与竞争优势',research_valuation:'估值与安全边际',research_attention:'热点与市场关注',research_timing:'交易时机',research_short:'短期关注',research_medium:'中期关注'};
  function reportTime(report){
    const generated=Date.parse(report.generated_at||'');
    if(Number.isFinite(generated))return generated;
    const date=Date.parse(String(report.date||'')+'T00:00:00+08:00');
    return Number.isFinite(date)?date:-Infinity;
  }
  function sortReports(reports,key='date'){
    const score=report=>{if(RESEARCH_SORTS[key]){const id=key.slice(9),item=['short','medium'].includes(id)?report.research_composites?.[id]:report.research_scores?.[id];return item?.complete&&item.ranking_eligible!==false&&typeof item.score==='number'&&Number.isFinite(item.score)?item.score:-Infinity}return typeof report.score==='number'&&Number.isFinite(report.score)?report.score:-Infinity};
    return [...reports].sort((a,b)=>(key==='score'||RESEARCH_SORTS[key]?score(b)-score(a):0)||reportTime(b)-reportTime(a)||String(a.code||'').localeCompare(String(b.code||''))||String(a.id||'').localeCompare(String(b.id||'')));
  }
  function recentEnough(report, today=new Date(), days=RECENT_DAYS){
    const time=reportTime(report);
    return Number.isFinite(time)&&time<=today.getTime()&&time>=today.getTime()-days*DAY;
  }
  function recommendationCandidate(report, today=new Date()){
    return report.formal_tracking_eligible!==false&&report.report_kind!=='historical_foundation'&&recentEnough(report,today)&&['buy','overweight'].includes(report.rating)&&
      report.evidence_status==='sufficient'&&report.score_status==='calculated'&&
      Number(report.score_coverage)>=85&&Number(report.score)>=65&&Boolean(report.detail_url);
  }
  function qualifies(report, detail, today=new Date()){
    if(!recommendationCandidate(report,today)||!detail||typeof detail!=='object')return false;
    const growth=(detail.scoring?.dimensions||[]).find(item=>item.name==='成长');
    const trend=detail.decision?.growth||{};
    return Number(growth?.covered_weight)>=20&&Number(growth?.points)>=15&&
      Number.isFinite(trend.revenue_yoy_pct)&&trend.revenue_yoy_pct>0&&
      Number.isFinite(trend.adjusted_profit_yoy_pct)&&trend.adjusted_profit_yoy_pct>0&&
      Array.isArray(detail.decision?.red_flags)&&detail.decision.red_flags.length===0;
  }
  async function recommendations(reports, loadDetail, today=new Date()){
    // Choose the newest judgment before applying the recommendation gates.
    // A newer neutral/unrated report must supersede an older buy report too.
    const latest=new Map();
    for(const report of reports.filter(r=>r.formal_tracking_eligible!==false&&r.report_kind!=='historical_foundation').sort((a,b)=>String(b.date||'').localeCompare(String(a.date||''))||String(b.generated_at||'').localeCompare(String(a.generated_at||'')))){
      const key=report.code||report.id;
      if(!latest.has(key))latest.set(key,report);
    }
    const candidates=[...latest.values()].filter(report=>recommendationCandidate(report,today));
    const settled=new Array(candidates.length).fill(null);
    let cursor=0;
    await Promise.all(Array.from({length:Math.min(4,candidates.length)},async()=>{
      while(cursor<candidates.length){
        const index=cursor++,report=candidates[index];
        try{const detail=await loadDetail(report);if(qualifies(report,detail,today))settled[index]={...report,growth_points:detail.scoring.dimensions.find(d=>d.name==='成长').points}}
        catch{/* Missing evidence excludes this report from recommendations. */}
      }
    }));
    return settled.filter(Boolean).sort((a,b)=>b.date.localeCompare(a.date)||b.score-a.score||b.growth_points-a.growth_points);
  }
  async function resolveReport(id,reports,loadDetail){
    const current=reports.find(report=>report.id===id);
    if(current)return current;
    if(!/^[a-zA-Z0-9-]{1,160}$/.test(String(id||'')))throw Error('报告编号不合法');
    const reference={id,detail_url:'./data/report-details/'+id+'.json'};
    const detail=await loadDetail(reference);
    if(!detail||detail.id!==id)throw Error('报告版本不匹配');
    return {...detail,detail_url:reference.detail_url};
  }
  function previewCount(counts){
    const positives=counts.filter(count=>Number.isFinite(count)&&count>0);
    if(!positives.length)return 0;
    // A sparse watchlist must not hide recent reports in another card.
    // Four rows fit the compact home cards. A single populated card stays
    // compact; otherwise populated cards grow together up to ten.
    return Math.min(positives.length===1?4:10,Math.max(4,Math.min(...positives)));
  }
  root.StockReportCollections={RECENT_DAYS,RESEARCH_SORTS,recentEnough,recommendationCandidate,qualifies,recommendations,resolveReport,previewCount,sortReports};
})(typeof window==='undefined'?globalThis:window);
