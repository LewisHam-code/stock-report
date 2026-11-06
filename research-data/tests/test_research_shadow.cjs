const test=require('node:test');
const assert=require('node:assert/strict');
require('../../stock-site/report-view.js');
test('shadow display separates missing range, original score and coverage',()=>{
 const html=globalThis.StockReportView.researchSection({research_profile:{version:'v1',capture_kind:'reconstructed',original_score:{score:50},repaired_legacy_score:{score:60},current:{dimensions:[{name:'财务质量',complete:false,range:[30,100],coverage_pct:30,factors:[]}],composites:{short:{complete:false,range:[10,90],coverage_pct:20}},market_environment:{label:'指数趋势偏强'},risk_flags:[]},operating_catalysts:{checks:[{name:'市场拓展',status_label:'待验证线索',evidence:[{quote:'<script>alert(1)</script>',url:'javascript:alert(1)',quote_verified:true}],missing:[]}]}}});
 assert.match(html,/上线后重建/);assert.match(html,/原时点旧分 50.0/);assert.match(html,/当前修复旧分 60.0/);assert.match(html,/30.0–100.0/);assert.match(html,/有效覆盖 30.0%/);
 assert.ok(!html.includes('<script>'));assert.ok(!html.includes('href="javascript:'));
});
test('old reports without a shadow payload remain renderable',()=>{assert.equal(globalThis.StockReportView.researchSection({}), '');});
require('../../stock-site/report-collections.js');
test('dimension sorting puts complete scores first without ranking incomplete floors',()=>{
 const rows=[{id:'incomplete',generated_at:'2026-10-21',research_scores:{quality:{score:99,complete:false}}},{id:'low',generated_at:'2026-10-20',research_scores:{quality:{score:40,complete:true}}},{id:'high',generated_at:'2026-10-19',research_scores:{quality:{score:80,complete:true}}}];
 assert.deepEqual(globalThis.StockReportCollections.sortReports(rows,'research_quality').map(r=>r.id),['high','low','incomplete']);
});
