const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
require('../../stock-site/report-collections.js');
require('../../stock-site/tracking-views.js');

async function page(search=''){
 class Element{
  constructor(){this.children=[];this.dataset={};this.attrs={};this.listeners={};this.textContent='';this.open=false}
  addEventListener(name,handler){this.listeners[name]=handler}
  setAttribute(name,value){this.attrs[name]=value}
  appendChild(child){this.children.push(child);return child}
  replaceChildren(){this.children=[]}
  insertRow(){return this.appendChild(new Element())}
  insertCell(){return this.appendChild(new Element())}
  insertBefore(child){this.children.unshift(child)}
  insertAdjacentHTML(_,html){this.html=html}
  querySelector(key){return this[key]||(this[key]=new Element())}
  closest(){return this.th||(this.th=new Element())}
 }
 const elements=new Map(),get=id=>elements.get(id)||elements.set(id,new Element()).get(id);
 const buttons=['date','change','score'].map(key=>{const b=new Element();b.dataset.trackingSort=key;return b});
 const reports=Array.from({length:35},(_,i)=>({report_id:String(i),company:'测试',code:String(i).padStart(6,'0'),
  score:i*2,rating:'unrated',generated_at:`2026-10-${String(1+i%25).padStart(2,'0')}`,horizons:{},price:10,current_price:10+i}));
 const location={search,pathname:'/stock/tracking.html'};let refresh;
 const sandbox={document:{getElementById:get,createElement:()=>new Element(),querySelectorAll:()=>buttons,addEventListener(){}},
  window:{addEventListener(){}},location,history:{replaceState(_,__,url){location.search=new URL(url,'http://localhost').search}},
  URLSearchParams,StockTrackingViews:globalThis.StockTrackingViews,StockReportCollections:globalThis.StockReportCollections,
  StockPriceTrend:{change:(now,then)=>(now/then-1)*100},StockWatchAction:{label:(_,code)=>code,request:async()=>({ok:true,items:[]})},
  setInterval:handler=>{refresh=handler},fetch:async url=>({ok:true,json:async()=>url.includes('tracking.json')?{reports,horizons:[3],method:{}}:{}})};
 vm.runInNewContext(fs.readFileSync(require.resolve('../../stock-site/tracking.js'),'utf8'),sandbox);
 await new Promise(setImmediate);
 return {get,location,buttons,click:key=>buttons.find(b=>b.dataset.trackingSort===key).listeners.click(),
  first:()=>get('trackingRows').children[0].children[0].html,refresh:async()=>{refresh();await new Promise(setImmediate)}};
}

test('评分表头点击切换双向、重置分页、保留URL，刷新与原涨幅排序正常',async()=>{
 const p=await page('?view=all');
 p.get('trackingNext').listeners.click();assert.match(p.get('trackingRange').textContent,/31–35/);
 p.click('score');assert.equal(p.first(),'000034');assert.match(p.get('trackingRange').textContent,/1–30/);
 assert.equal(new URLSearchParams(p.location.search).get('sort'),'score');
 assert.equal(p.buttons[2].th.attrs['aria-sort'],'descending');
 p.click('score');assert.equal(p.first(),'000000');assert.equal(p.buttons[2].th.attrs['aria-sort'],'ascending');
 assert.match(p.get('trackingViewNote').textContent,/当时评分从低到高/);
 assert.equal(new URLSearchParams(p.location.search).get('order'),'asc');
 await p.refresh();assert.equal(p.first(),'000000');assert.equal(p.buttons[2].th.attrs['aria-sort'],'ascending');
 p.click('change');assert.equal(p.first(),'000034');p.click('change');assert.equal(p.first(),'000000');
 p.click('score');assert.equal(p.first(),'000034');
 p.click('date');assert.equal(new URLSearchParams(p.location.search).has('sort'),false);
 assert.equal(new URLSearchParams(p.location.search).get('view'),'all');
 assert.equal(p.buttons[2].th.attrs['aria-sort'],'none');
 const restored=await page('?view=all&sort=score&order=asc');
 assert.equal(restored.first(),'000000');assert.equal(restored.buttons[2].th.attrs['aria-sort'],'ascending');
});
