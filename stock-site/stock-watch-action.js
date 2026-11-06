/* Shared stock identity and watchlist state; local requests, no model calls. */
(function(root){
  'use strict';
  const API='/api/watchlist';
  const escape=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const validCode=value=>/^\d{6}(?:\.(?:SH|SZ|BJ))?$/i.test(String(value||''));
  const pending=new Set();
  let toastTimer,reading=null,watchReady=false;
  let watched=new Set();
  const star='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 2.78 5.63 6.22.9-4.5 4.39 1.06 6.19L12 17.19l-5.56 2.92 1.06-6.19L3 9.53l6.22-.9Z"/></svg>';

  function button(code,company){
    if(!validCode(code))return '';
    const shortCode=String(code).slice(0,6);
    const active=watched.has(shortCode),action=active?'取消关注':'添加关注';
    return '<button class="stock-watch-add'+(active?' is-watched':'')+'" type="button" data-watch-code="'+shortCode+'" data-watch-name="'+escape(company)+'" aria-pressed="'+active+'" aria-label="'+action+' '+escape(company)+'（'+shortCode+'）" title="'+(active?'已关注，点击取消关注':'点击关注')+'">'+star+'</button>';
  }
  function board(code,market=''){
    if(!validCode(code))return '';
    const value=String(code).slice(0,6),exchange=String(market||String(code).split('.')[1]||'').toUpperCase();
    if(exchange==='BJ')return '京A';
    if((!exchange||exchange==='SH')&&/^(600|601|603|605)/.test(value))return '沪A';
    if((!exchange||exchange==='SH')&&/^688/.test(value))return '科创板';
    if((!exchange||exchange==='SZ')&&/^(000|001[2-9]|00[2-4])/.test(value))return '深A';
    if((!exchange||exchange==='SZ')&&Number(value)>=300000&&Number(value)<=309799)return '创业板';
    return '板块待核实';
  }
  function label(company,code,cls='company-name',options={}){
    const valid=validCode(code),shortCode=valid?String(code).slice(0,6):'';
    const href=typeof options.href==='string'&&/^\.\/reports\.html\?/.test(options.href)?options.href:'';
    const name=href?'<a class="'+escape(cls)+'" data-security-name href="'+escape(href)+'">'+escape(company)+'</a>':'<span class="'+escape(cls)+'" data-security-name>'+escape(company)+'</span>';
    const meta=valid?'<span class="stock-security-meta"><span class="security-board" data-security-board>'+board(code,options.market)+'</span><span>'+shortCode+'</span></span>':'';
    return '<span class="stock-watch-label" data-security-code="'+shortCode+'" data-record-name="'+escape(company)+'">'+(options.watch===false?'':button(code,company))+'<span class="stock-identity-text"><span class="stock-identity-main">'+name+'<span class="security-state-badge" data-security-state hidden></span></span>'+meta+'</span></span>';
  }
  function syncButtons(){
    document.querySelectorAll('button[data-watch-code]').forEach(control=>{
      const code=control.dataset.watchCode,active=watched.has(code),action=active?'取消关注':'添加关注';
      control.classList.toggle('is-watched',active);
      control.setAttribute('aria-pressed',String(active));
      control.setAttribute('aria-label',action+' '+(control.dataset.watchName||'这只股票')+'（'+code+'）');
      control.title=active?'已关注，点击取消关注':'点击关注';
    });
  }
  function setWatched(items){watched=new Set(items.map(item=>item.code));watchReady=true;syncButtons()}
  async function performRequest(payload){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),30000);
    try{
      const options={cache:'no-store',signal:controller.signal};
      if(payload)Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json','X-Stock-Research':'1'},body:JSON.stringify(payload)});
      const response=await fetch(API,options),result=await response.json();
      if(!response.ok||!result.ok){const error=new Error(result.error||'关注列表暂不可用');error.code=result.code;throw error}
      return result;
    }finally{clearTimeout(timer)}
  }
  async function request(payload){
    if(!payload&&reading)return reading;
    const task=performRequest(payload);if(!payload)reading=task;
    try{const result=await task;if(Array.isArray(result.items))setWatched(result.items);return result}
    finally{if(reading===task)reading=null}
  }
  function showToast(message){
    const parent=document.querySelector('dialog[open]')||document.body;
    let toast=parent.querySelector('.stock-watch-toast');
    if(!toast){toast=document.createElement('div');toast.className='stock-watch-toast';toast.setAttribute('role','status');toast.setAttribute('aria-live','polite');parent.appendChild(toast)}
    toast.textContent=message;toast.hidden=false;
    clearTimeout(toastTimer);toastTimer=setTimeout(()=>{toast.hidden=true},3200);
  }
  function setBusy(code,busy){
    document.querySelectorAll('button[data-watch-code]').forEach(button=>{
      if(button.dataset.watchCode===code)button.disabled=busy;
    });
  }
  document.addEventListener('click',async event=>{
    const target=event.target;
    const control=target instanceof Element?target.closest('button[data-watch-code]'):null;
    if(!control)return;
    event.preventDefault();event.stopPropagation();
    const code=control.dataset.watchCode;
    if(!validCode(code)||pending.has(code))return;
    pending.add(code);setBusy(code,true);
    try{
      if(!watchReady)await request();
      const action=watched.has(code)?'remove':'add';
      const result=await request({action,value:code});
      root.dispatchEvent(new CustomEvent('stock-watchlist-updated',{detail:{items:result.items||[],source:'quick-add',action}}));
      showToast(action==='remove'?'已取消关注':'已加入关注');
    }catch(error){if(error.code==='already_watched')await request().catch(()=>{});showToast(error.code==='already_watched'?'已在关注列表':error.code?error.message:'操作失败，请检查本机设置服务')}
    finally{pending.delete(code);setBusy(code,false)}
  });
  const searchNames=item=>[item.company,item.company_search].filter(Boolean).join(' ').normalize('NFKC').toLocaleLowerCase();
  root.StockWatchAction={button,label,board,request,searchNames};
  if(root.addEventListener)root.addEventListener('stock-watchlist-updated',event=>{if(Array.isArray(event.detail?.items))setWatched(event.detail.items)});
  // One batched local lookup serves all pages, including newly paginated rows.
  // No per-stock remote request and no background polling are introduced.
  if(typeof MutationObserver!=='undefined'){
    const states=new Map();let timer,loading=false,rerun=false;
    async function updateLabels(){
      if(loading){rerun=true;return}loading=true;
      try{
        const labels=[...document.querySelectorAll('[data-security-code]')];
        const codes=[...new Set(labels.map(node=>node.dataset.securityCode).filter(code=>/^\d{6}$/.test(code)))];
        const stale=codes.filter(code=>!states.has(code)||Date.now()-states.get(code).at>300000);
        for(let offset=0;offset<stale.length;offset+=100){
          const batch=stale.slice(offset,offset+100),params=new URLSearchParams();batch.forEach(code=>params.append('code',code));
          batch.forEach(code=>states.set(code,{...states.get(code),at:Date.now()}));
          try{
            const response=await fetch('/api/securities/display?'+params,{cache:'no-store'});
            if(response.ok){const data=await response.json();(data.items||[]).forEach(row=>states.set(row.code,{at:Date.now(),row:{...row,catalog_warning:!!data.read_warning}}))}
          }catch{/* Original record names remain available offline. */}
        }
        for(const node of labels){
          const row=states.get(node.dataset.securityCode)?.row;if(!row)continue;
          const name=node.querySelector('[data-security-name]'),badge=node.querySelector('[data-security-state]');
          const market=node.querySelector('[data-security-board]'),marketText=board(row.code,row.market);
          if(market&&market.textContent!==marketText)market.textContent=marketText;
          if(name&&row.company&&name.textContent!==row.company)name.textContent=row.company;
          if(name)name.title=(node.dataset.recordName!==row.company?'原记录名称：'+node.dataset.recordName+'；':'')+'当前名录核验：'+row.verified_at;
          const button=node.querySelector('button[data-watch-code]');
          if(button&&row.company)button.dataset.watchName=row.company;
          const text=[row.risk_flag,row.catalog_warning?'名录待核实':'',row.status==='delisted'?'已退市':row.status==='delisting_announced'?'已公告退市':['pending_confirmation','unknown'].includes(row.status)?'状态待核实':''].filter(Boolean).join(' · ');
          if(badge){if(badge.textContent!==text)badge.textContent=text;badge.hidden=!text}
        }
        syncButtons();
      }finally{loading=false;if(rerun){rerun=false;schedule()}}
    }
    const schedule=()=>{clearTimeout(timer);timer=setTimeout(updateLabels,120)};
    function start(){new MutationObserver(schedule).observe(document.body,{childList:true,subtree:true});void request().catch(()=>{});schedule()}
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
    root.addEventListener('focus',()=>{schedule();void request().catch(()=>{})});
  }
})(window);
