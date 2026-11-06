const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

function page(result){
  const events=[];
  const buttons=[];
  let click,request;
  const toast={hidden:true,textContent:'',setAttribute(){}};
  const body={querySelector:()=>null,appendChild:()=>{}};
  class Element{
    constructor(){this.attrs={};this.classList={toggle:(name,active)=>{this[name]=active}}}
    closest(){return this}
    setAttribute(key,value){this.attrs[key]=value}
  }
  class CustomEvent{constructor(type,options){this.type=type;this.detail=options.detail}}
  const document={
    body,
    addEventListener:(name,handler)=>{if(name==='click')click=handler},
    querySelector:()=>null,
    querySelectorAll:()=>buttons,
    createElement:()=>toast,
  };
  const window={dispatchEvent:event=>events.push(event)};
  const context={window,document,Element,CustomEvent,AbortController,setTimeout:()=>1,clearTimeout:()=>{},fetch:async(url,options)=>{
    request={url,options};return typeof result==='function'?result(url,options):result;
  }};
  vm.runInNewContext(fs.readFileSync(require.resolve('../../stock-site/stock-watch-action.js'),'utf8'),context);
  return {window,events,buttons,click:code=>{
    const control=new Element();control.dataset={watchCode:code};control.disabled=false;buttons.push(control);
    return click({target:control,preventDefault(){},stopPropagation(){}});
  },get request(){return request},toast};
}

test('统一按钮只发送六位代码，并对公司名称转义',async()=>{
  const view=page((url,options)=>({ok:true,json:async()=>({ok:true,items:options.method==='POST'?[{code:'600001'}]:[]})}));
  const markup=view.window.StockWatchAction.label('<公司>', '600001.SH');
  assert.match(markup,/data-watch-code="600001"/);
  assert.match(markup,/&lt;公司&gt;/);
  await view.click('600001');
  assert.equal(JSON.parse(view.request.options.body).value,'600001');
  assert.equal(view.request.options.method,'POST');
  assert.equal(view.events[0].detail.source,'quick-add');
  assert.equal(view.toast.textContent,'已加入关注');
});

test('已经关注时提示现有状态，不广播新增事件',async()=>{
  let addedElsewhere=false;
  const view=page((url,options)=>{
    if(options.method==='POST'){addedElsewhere=true;return {ok:false,json:async()=>({ok:false,code:'already_watched',error:'已存在'})}}
    return {ok:true,json:async()=>({ok:true,items:addedElsewhere?[{code:'600001'}]:[]})};
  });
  await view.click('600001');
  assert.equal(view.toast.textContent,'已在关注列表');
  assert.equal(view.events.length,0);
  assert.equal(view.buttons[0].attrs['aria-pressed'],'true');
});

test('检索同时保留原名称、现名称和全称，并统一全角字符',()=>{
  const view=page({ok:true,json:async()=>({ok:true,items:[]})});
  const text=view.window.StockWatchAction.searchNames({company:'旧名称Ａ',company_search:'新名称 公司完整名称'});
  for(const value of ['旧名称a','新名称','公司完整名称'])assert.ok(text.includes(value));
});

test('统一板块标识遵循项目证券范围，北交所需明确市场身份',()=>{
  const {board,label}=page({}).window.StockWatchAction;
  for(const [code,market,name] of [['600519','SH','沪A'],['002832','SZ','深A'],['300750','SZ','创业板'],['301001','SZ','创业板'],['688475','SH','科创板'],['920247','BJ','京A'],['830001','BJ','京A']])assert.equal(board(code,market),name);
  assert.equal(board('830001'),'板块待核实');
  assert.equal(board('200001','SZ'),'板块待核实');
  const html=label('比音勒芬','002832');
  assert.match(html,/data-security-board>深A<\/span><span>002832<\/span>/);
  assert.ok(html.indexOf('data-security-name')<html.indexOf('stock-security-meta'));
  const choice=label('<测试>','920247','company-name',{market:'BJ',watch:false,href:'javascript:alert(1)'});
  assert.ok(!choice.includes('<button'));assert.ok(!choice.includes('javascript:'));assert.ok(choice.includes('&lt;测试&gt;'));
});

test('空心星与实心星使用真实关注状态，点击可添加和取消，同股票状态同步',async()=>{
 let items=[];const actions=[];
 const view=page((url,options)=>{
   if(options.method==='POST'){
     const payload=JSON.parse(options.body);actions.push(payload.action);
     items=payload.action==='add'?[{code:payload.value}]:[];
   }
   return {ok:true,json:async()=>({ok:true,items})};
 });
 assert.ok(view.window.StockWatchAction.button('600001','公司').includes('aria-pressed="false"'));
 await view.click('600001');
 assert.equal(view.events[0].detail.action,'add');
 assert.ok(view.window.StockWatchAction.button('600001','公司').includes('is-watched'));
 assert.equal(view.buttons[0].attrs['aria-pressed'],'true');
 await view.click('600001');
 assert.deepEqual(actions,['add','remove']);assert.equal(view.buttons[0].attrs['aria-pressed'],'false');
 assert.equal(view.toast.textContent,'已取消关注');
});

test('读取关注列表后，新生成的同股票按钮直接显示实心星',async()=>{
 const view=page({ok:true,json:async()=>({ok:true,items:[{code:'002832'}]})});
 await view.window.StockWatchAction.request();
 const html=view.window.StockWatchAction.label('比音勒芬','002832');
 assert.ok(html.includes('aria-pressed="true"'));assert.ok(html.includes('取消关注'));
 assert.ok(html.includes('<svg'));assert.ok(!html.includes('>+</button>'));
});
