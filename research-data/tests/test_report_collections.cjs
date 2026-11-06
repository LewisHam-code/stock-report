const {test}=require('node:test');
const assert=require('node:assert/strict');
require('../../stock-site/report-collections.js');
const {recommendationCandidate,qualifies,recommendations,resolveReport,previewCount}=globalThis.StockReportCollections;
const today=new Date('2026-10-18T12:00:00+08:00');
const report={id:'r1',date:'2026-10-18',rating:'buy',evidence_status:'sufficient',score_status:'calculated',score_coverage:90,score:72,detail_url:'./data/report-details/r1.json'};
const detail={scoring:{dimensions:[{name:'成长',points:17,covered_weight:25}]},decision:{growth:{revenue_yoy_pct:12,adjusted_profit_yoy_pct:8},red_flags:[]}};

test('只纳入证据完整且成长、风险门槛通过的正式评级',()=>{
  assert.equal(qualifies(report,detail,today),true);
  assert.equal(recommendationCandidate({...report,rating:'unrated'},today),false);
  assert.equal(recommendationCandidate({...report,score_status:'provisional'},today),false);
  assert.equal(recommendationCandidate({...report,score_coverage:70},today),false);
  assert.equal(qualifies(report,{...detail,decision:{...detail.decision,red_flags:['现金流异常']}},today),false);
  assert.equal(qualifies(report,{...detail,decision:{...detail.decision,growth:{...detail.decision.growth,adjusted_profit_yoy_pct:-1}}},today),false);
  assert.equal(recommendationCandidate({...report,date:'2026-01-01'},today),false);
});

test('详情读取失败时不把股票误列为推荐',async()=>{
  const result=await recommendations([report],async()=>{throw Error('missing')},today);
  assert.deepEqual(result,[]);
});

test('历史补建不能变成正式推荐，也不遮挡同公司的当前报告',async()=>{
  const live={...report,id:'live',code:'600001',date:'2026-10-17'};
  const historical={...report,id:'historical',code:'600001',report_kind:'historical_foundation',formal_tracking_eligible:false};
  assert.equal(recommendationCandidate(historical,today),false);
  const result=await recommendations([live,historical],async()=>detail,today);
  assert.deepEqual(result.map(r=>r.id),['live']);
});

test('最新未评级报告阻止同一公司旧报告继续被推荐',async()=>{
  const old={...report,id:'old',code:'600001',date:'2026-10-17'};
  const latest={...report,id:'latest',code:'600001',rating:'unrated'};
  let reads=0;
  const result=await recommendations([old,latest],async()=>{reads++;return detail},today);
  assert.deepEqual(result,[]);
  assert.equal(reads,0);
});

test('推荐详情并发请求不超过四个',async()=>{
  let active=0,peak=0;
  const reports=Array.from({length:12},(_,n)=>({...report,id:'r'+n,code:String(600000+n)}));
  const result=await recommendations(reports,async()=>{
    active++;peak=Math.max(peak,active);await new Promise(resolve=>setTimeout(resolve,2));active--;return detail;
  },today);
  assert.equal(result.length,12);
  assert.equal(peak,4);
});

test('历史版本可从详情文件打开，拒绝路径穿越及错配版本',async()=>{
  let reads=0;
  const archived={...report,id:'old-version',code:'600001'};
  const result=await resolveReport('old-version',[report],async reference=>{
    reads++;assert.equal(reference.detail_url,'./data/report-details/old-version.json');return archived;
  });
  assert.equal(result.id,'old-version');assert.equal(reads,1);
  await assert.rejects(resolveReport('../private',[],async()=>{throw Error('should not fetch')}),/不合法/);
  await assert.rejects(resolveReport('old-version',[],async()=>report),/不匹配/);
});

test('稀疏关注列表不阻止近期报告显示，首页仍限制长度',()=>{
  assert.equal(previewCount([1,0,23]),4);
  assert.equal(previewCount([5,0,23]),5);
  assert.equal(previewCount([2,4,9]),4);
  assert.equal(previewCount([10,10,20]),10);
  assert.equal(previewCount([0,0,23]),4);
  assert.equal(previewCount([0,0,0]),0);
});
