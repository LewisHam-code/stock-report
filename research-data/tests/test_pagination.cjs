const {test}=require('node:test');
const assert=require('node:assert/strict');
require('../../stock-site/pagination.js');

const {PAGE_SIZE,parsePage,paginate}=globalThis.StockPagination;

test('每页最多30条并保留原排序',()=>{
  const rows=Array.from({length:51},(_,index)=>index+1);
  const first=paginate(rows,1);
  const second=paginate(rows,2);
  assert.equal(PAGE_SIZE,30);
  assert.deepEqual([first.from,first.to,first.pages,first.items.length],[1,30,2,30]);
  assert.deepEqual([second.from,second.to,second.pages,second.items.length],[31,51,2,21]);
  assert.deepEqual(second.items,[...rows].slice(30));
});

test('空列表与非法页码安全回到有效页',()=>{
  assert.deepEqual([paginate([],99).page,paginate([],99).from,paginate([],99).to],[1,0,0]);
  assert.equal(paginate(Array.from({length:51}),99).page,2);
  for(const value of [null,'','0','-1','1.5','NaN'])assert.equal(parsePage(value),1);
  assert.equal(parsePage('3'),3);
});
