const {test}=require('node:test'),assert=require('node:assert/strict');
require('../../stock-site/report-collections.js');
require('../../stock-site/tracking-views.js');
const {select,summarize}=globalThis.StockTrackingViews;
const row={report_id:'r1',code:'600001',rating:'buy',score:80,score_status:'calculated',provenance:'on_time',test_mode:false,horizons:{'3':{state:'evaluated',evaluable:true,aligned:true,relative_aligned:false,return_pct:10,excess_pct:-2}}};
test('股票范围与观察期限独立，关注按代码、推荐按最新报告编号筛选',()=>{
 const rows=[row,{...row,report_id:'r2'},{...row,report_id:'r3',code:'600002'}];
 assert.deepEqual(select(rows,'watchlist',new Set(['600001']),new Set()).map(r=>r.report_id),['r1','r2']);
 assert.deepEqual(select(rows,'recommended',new Set(),new Set(['r2'])).map(r=>r.report_id),['r2']);
 assert.equal(select(rows,'all',new Set(),new Set()).length,3);
});
test('全部不限制日期，近期解读仅显示近7天，关注后仍可返回全部',()=>{
 const rows=[{...row,generated_at:'2026-10-20T12:00:00+08:00'},{...row,report_id:'old',generated_at:'2026-10-12T12:00:00+08:00'}];
 assert.equal(select(rows,'recent',new Set(),new Set(),new Date('2026-10-20T22:00:00+08:00')).length,1);
 assert.equal(select(rows,'watchlist',new Set(['600001']),new Set()).length,2);
 assert.equal(select(rows,'all',new Set(),new Set()).length,2);
});
test('筛选后的统计不混入其他股票或事后补录评级，同公司报告单独标公司数',()=>{
 const reports=[row,{...row,report_id:'r2'},{...row,report_id:'r3',provenance:'backfilled'},
   {...row,report_id:'r4',code:'600002',horizons:{'3':{state:'pending'}}}];
 const s=summarize(reports,3);
 assert.equal(s.reports,4);assert.equal(s.evaluable,2);assert.equal(s.companies,1);assert.equal(s.pending,1);
 assert.equal(s.alignment_pct,100);assert.equal(s.relative_alignment_pct,0);assert.equal(s.exploratory.mature,1);
 assert.equal(s.by_rating[0].count,2);
 assert.equal(summarize(select(reports,'recommended',new Set(),new Set(['r4'])),3).mature,0);
});

test('涨幅全量降序后再分页，缺失值按0参与排序，同涨幅按解读日期',()=>{
 const rows=Array.from({length:35},(_,i)=>({report_id:String(i),generated_at:'2026-10-20',current_change_pct:i-10}));
 rows.push({report_id:'missing',generated_at:'2026-10-21',current_change_pct:null},
   {report_id:'invalid',current_change_pct:NaN},{report_id:'tie',generated_at:'2026-10-21',current_change_pct:24});
 const sorted=globalThis.StockTrackingViews.sort(rows,'change');
 assert.equal(sorted[0].report_id,'tie');assert.equal(sorted[1].report_id,'34');
 assert.equal(sorted[30].current_change_pct,-3);
 assert.equal(sorted[25].report_id,'missing');
 assert.equal(sorted[26].report_id,'10');
 assert.equal(sorted[27].report_id,'invalid');
 assert.equal(sorted.at(-1).current_change_pct,-10);
 assert.equal(rows[0].report_id,'0');
 assert.equal(globalThis.StockTrackingViews.sort(rows,'date')[0].report_id,'missing');
});

test('涨幅升序先排跌幅，缺失值与真实0同组，再排涨幅；原数据不改写',()=>{
 const rows=[{report_id:'gain',current_change_pct:12},{report_id:'loss',current_change_pct:-5},
  {report_id:'zero',current_change_pct:0,generated_at:'2026-10-20'},
  {report_id:'missing',current_change_pct:null,generated_at:'2026-10-21'},
  {report_id:'invalid',current_change_pct:NaN},{report_id:'infinite',current_change_pct:Infinity}];
 const sorted=globalThis.StockTrackingViews.sort(rows,'change','asc');
 assert.deepEqual(sorted.map(r=>r.report_id),['loss','missing','zero','infinite','invalid','gain']);
 assert.equal(rows[3].current_change_pct,null);assert.equal(rows[0].report_id,'gain');
 assert.deepEqual(globalThis.StockTrackingViews.sort(rows,'change','desc').map(r=>r.report_id),['gain','missing','zero','infinite','invalid','loss']);
});

test('当时评分双向排序跨分页，未评级的有效分参与，缺失分始终最后',()=>{
 const rows=Array.from({length:35},(_,i)=>({report_id:String(i),score:i*2,rating:'unrated',generated_at:'2026-10-20'}));
 rows.push({report_id:'tie-new',score:68,generated_at:'2026-10-21'},
  {report_id:'missing',score:null,generated_at:'2026-10-23'},
  {report_id:'nan',score:NaN},{report_id:'infinite',score:Infinity},
  {report_id:'negative',score:-1},{report_id:'outside',score:101},{report_id:'string',score:'90'});
 const sort=globalThis.StockTrackingViews.sort;
 const desc=sort(rows,'score','desc'),asc=sort(rows,'score','asc');
 assert.deepEqual(desc.slice(0,2).map(r=>r.report_id),['tie-new','34']);
 assert.equal(desc[30].score,10);assert.equal(desc[35].score,0);
 assert.equal(asc[0].score,0);assert.equal(asc[30].score,60);
 assert.deepEqual(asc.slice(34,36).map(r=>r.report_id),['tie-new','34']);
 assert.equal(desc[36].report_id,'missing');assert.equal(asc[36].report_id,'missing');
 assert.deepEqual(desc.slice(36).map(r=>r.report_id),asc.slice(36).map(r=>r.report_id));
 assert.equal(rows[0].report_id,'0');assert.equal(rows[36].score,null);
});
