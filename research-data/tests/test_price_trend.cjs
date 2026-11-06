const {test}=require('node:test'),assert=require('node:assert/strict');
require('../../stock-site/price-trend.js');
const prepare=globalThis.StockPriceTrend.prepare;
const base={price:10,generated_at:'2026-10-17T20:00:00+08:00'};
const at=new Date('2026-10-20T16:00:00+08:00');
test('解读时原始价格固定为起点，随后使用有效未复权收盘价',()=>{
 const result=prepare({adjustment:'none',points:[{date:'2026-10-21',close:12},{date:'2026-10-20',close:11},{date:'2026-10-19',close:10.5},{date:'2026-10-20',close:11},{date:'2026-10-17',close:9}]},base,{},'2026-10-20',at);
 assert.deepEqual(result.points.map(p=>[p.date,p.close,p.kind]),[['2026-10-17',10,'initial'],['2026-10-19',10.5,'close'],['2026-10-20',11,'close']]);
});
test('盘中只使用最新报价，较旧盘中价不能覆盖已存收盘价',()=>{
 const series={adjustment:'none',points:[{date:'2026-10-20',close:12}]};
 const quote={current_price:11,current_asof:'2026-10-20T14:00:00+08:00'};
 assert.equal(prepare(series,base,quote,'2026-10-20',new Date('2026-10-20T14:05:00+08:00')).points.at(-1).kind,'latest');
 assert.equal(prepare(series,base,quote,'2026-10-20',at).points.at(-1).close,12);
 assert.equal(prepare(series,base,{...quote,current_price:12.5,current_asof:'2026-10-20T15:00:00+08:00'},'2026-10-20',at).points.at(-1).close,12.5);
});
test('不混入复权或冲突数据，不伪造缺失起点、未来价或解读前价格',()=>{
 const series={adjustment:'qfq',points:[{date:'2026-10-20',close:99}]};
 assert.equal(prepare(series,base,{},'2026-10-20',at).points.length,1);
 assert.equal(prepare({...series,adjustment:'none',cross_validation:{status:'conflict'}},base,{},'2026-10-20',at).dailyState,'conflict');
 assert.equal(prepare(series,{...base,price:null},{},'2026-10-20',at).state,'missing_baseline');
 for(const asof of ['2026-10-17T15:00:00+08:00','2026-10-21T15:00:00+08:00'])assert.equal(prepare(series,base,{current_price:20,current_asof:asof},'2026-10-20',at).points.length,1);
});
test('盘中解读可保留同日收盘点，涨幅始终比较解读价和当前价',()=>{
 const intraday={...base,generated_at:'2026-10-20T10:00:00+08:00'};
 const result=prepare({adjustment:'none',points:[{date:'2026-10-20',close:11}]},intraday,{},'2026-10-20',at);
 assert.deepEqual(result.points.map(p=>p.close),[10,11]);
 const change=globalThis.StockPriceTrend.change;
 assert.equal(change(11,10),10);assert.equal(change(9,10),-10);assert.equal(change(10,10),0);
 assert.equal(change(10,0),null);assert.equal(change(null,10),null);
 assert.equal(change(11,10,{fields:{price:{matches:false}}}),10);
});
test('双源差异保留状态，展示价、涨幅和盘中走势始终一致',()=>{
 const baseline={price:27.37,generated_at:'2026-10-21T07:18:28+08:00'};
 const quote={current_price:27.02,current_asof:'2026-10-21T13:00:27+08:00',current_cross_validation:{status:'conflict',fields:{price:{primary:27.02,secondary:26.87,matches:false}}}};
 const points=prepare({adjustment:'none',points:[]},baseline,quote,'2026-10-21',new Date('2026-10-21T13:05:00+08:00')).points;
 assert.deepEqual(points.map(p=>p.close),[27.37,27.02]);
 assert.equal(points.at(-1).priceConflict,true);
 assert.equal(globalThis.StockPriceTrend.change(quote.current_price,baseline.price,quote.current_cross_validation),-1.28);
 assert.equal(globalThis.StockPriceTrend.change(34.50,34.66),-.46);
 const resolved={...quote,current_cross_validation:{...quote.current_cross_validation,price_resolution:{status:'resolved'}}};
 assert.equal(prepare(null,baseline,resolved,'2026-10-21',new Date('2026-10-21T13:05:00+08:00')).points.at(-1).priceConflict,false);
});
