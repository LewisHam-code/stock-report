(function(root){
  'use strict';
  const formal=r=>r.provenance==='on_time'&&r.test_mode!==true&&r.score_status==='calculated'&&Number.isFinite(r.score)&&r.score>=0&&r.score<=100&&['buy','overweight','neutral','underweight','sell'].includes(r.rating);
  function select(reports,view,codes,recommended,at=new Date()){
    return reports.filter(r=>view==='watchlist'?codes.has(r.code):view==='recommended'?recommended.has(r.report_id):view==='recent'?
      root.StockReportCollections.recentEnough(r,at):true);
  }
  function sort(reports,key='date',direction='desc'){
    const time=r=>{const value=Date.parse(r.generated_at);return Number.isFinite(value)?value:0};
    const change=r=>Number.isFinite(r.current_change_pct)?r.current_change_pct:0;
    const scored=r=>Number.isFinite(r.score)&&r.score>=0&&r.score<=100;
    const order=direction==='asc'?1:-1;
    return [...reports].sort((a,b)=>{
      if(key==='score'){
        // Missing scores stay last in both directions; unrated reports may still have a frozen score.
        if(scored(a)!==scored(b))return scored(a)?-1:1;
        if(scored(a)&&a.score!==b.score)return (a.score-b.score)*order;
      }
      return (key==='change'?(change(a)>change(b)?order:change(a)<change(b)?-order:0):0)||time(b)-time(a)||String(a.report_id).localeCompare(String(b.report_id));
    });
  }
  function summarize(reports,horizon){
    const outcome=r=>r.horizons?.[String(horizon)]||{};
    const mature=reports.filter(r=>outcome(r).state==='evaluated');
    const eligible=mature.filter(r=>formal(r)&&outcome(r).evaluable===true);
    const relative=eligible.filter(r=>typeof outcome(r).relative_aligned==='boolean');
    const exploratory=mature.filter(r=>!formal(r));
    const percentage=(n,total)=>total?Math.round(n/total*1000)/10:null;
    function groups(key){
      const map=new Map();for(const row of eligible){const name=key(row);if(!map.has(name))map.set(name,[]);map.get(name).push(outcome(row))}
      return [...map.entries()].map(([group,items])=>{
        const excess=items.map(x=>x.excess_pct).filter(Number.isFinite);
        return {group,count:items.length,alignment_pct:percentage(items.filter(x=>x.aligned===true).length,items.length),
          average_excess_pct:excess.length?Math.round(excess.reduce((a,b)=>a+b,0)/excess.length*100)/100:null};
      });
    }
    const band=r=>r.score<25?'0–24':r.score<45?'25–44':r.score<65?'45–64':r.score<80?'65–79':'80–100';
    const patterns=new Map();
    for(const row of eligible){
      const result=outcome(row);if(result.aligned!==false)continue;
      const name=['buy','overweight'].includes(row.rating)?(Number.isFinite(result.benchmark_pct)&&result.benchmark_pct<0?'大盘同跌':'个股走弱'):
        ['underweight','sell'].includes(row.rating)?(Number.isFinite(result.benchmark_pct)&&result.benchmark_pct<=0?'个股逆势走强':'个股走强'):'中性观察带外';
      patterns.set(name,(patterns.get(name)||0)+1);
    }
    return {reports:reports.length,mature:mature.length,evaluable:eligible.length,companies:new Set(eligible.map(r=>r.code)).size,
      alignment_pct:percentage(eligible.filter(r=>outcome(r).aligned===true).length,eligible.length),
      relative_alignment_pct:percentage(relative.filter(r=>outcome(r).relative_aligned===true).length,relative.length),
      pending:reports.filter(r=>outcome(r).state==='pending').length,data_gap:reports.filter(r=>outcome(r).state==='data_gap').length,
      benchmark_gap:reports.filter(r=>outcome(r).benchmark_gap===true).length,
      exploratory:{mature:exploratory.length,companies:new Set(exploratory.map(r=>r.code)).size},
      by_rating:groups(r=>r.rating),by_score:groups(band),by_industry:groups(r=>r.industry||'原始行业未知'),
      by_model:groups(r=>[r.provider||'未知',r.model||'未知'].join('/')),by_version:groups(r=>r.scoring_version||'原始版本未知'),
      patterns:[...patterns].map(([name,count])=>({name,count})).sort((a,b)=>b.count-a.count)};
  }
  root.StockTrackingViews={select,sort,summarize};
})(typeof window==='undefined'?globalThis:window);
