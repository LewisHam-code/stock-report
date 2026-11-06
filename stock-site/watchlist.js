(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const safe = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const states = {pending:'待下载',parsed:'待解读',complete:'已解读',review:'待复核'};
  let items = [], busy = false;
  const confirmation=document.createElement('dialog');
  confirmation.id='watchlistConfirm';
  confirmation.setAttribute('aria-labelledby','watchlistConfirmTitle');
  confirmation.setAttribute('aria-describedby','watchlistConfirmText');
  confirmation.innerHTML='<div class="modal-head"><h2 id="watchlistConfirmTitle">确定取消关注吗？</h2></div><div class="modal-body"><p id="watchlistConfirmText"></p><p class="subtle">取消后不再优先采集和解读这只股票，已有财报和解读报告会保留。</p></div><div class="modal-actions"><button class="btn" type="button" data-keep autofocus>继续关注</button><button class="btn primary" type="button" data-confirm-remove>取消关注</button></div>';
  document.body.appendChild(confirmation);
  let removingCode='',removeOpener;
  confirmation.querySelector('[data-keep]').addEventListener('click',()=>confirmation.close());
  confirmation.querySelector('[data-confirm-remove]').addEventListener('click',()=>{
    const code=removingCode;confirmation.close();if(code)change('remove',code);
  });
  confirmation.addEventListener('close',()=>{removingCode='';if(removeOpener?.isConnected)removeOpener.focus()});
  let searchTimer,searchVersion=0;
  const choices=document.createElement('div');choices.id='watchlistChoices';choices.className='watchlist-choices';choices.hidden=true;
  choices.setAttribute('role','group');choices.setAttribute('aria-label','匹配的股票');
  $('watchlistForm').after(choices);
  $('watchlistInput').setAttribute('aria-controls',choices.id);
  $('watchlistInput').addEventListener('input',()=>{
    clearTimeout(searchTimer);const term=$('watchlistInput').value.trim(),version=++searchVersion;
    choices.hidden=true;choices.replaceChildren();if(!term)return;
    searchTimer=setTimeout(async()=>{
      try{
        const response=await fetch('/api/securities/search?q='+encodeURIComponent(term),{cache:'no-store'});
        if(!response.ok)return;const data=await response.json();if(version!==searchVersion)return;
        choices.innerHTML=(data.items||[]).map(row=>'<button type="button" class="btn small" data-security-choice="'+safe(row.code)+'">'+StockWatchAction.label(row.company,row.code,'company-name',{market:row.market,watch:false})+'</button>').join('');
        choices.hidden=!choices.childElementCount;
      }catch{/* Adding still supports a verified server-side fallback. */}
    },200);
  });
  choices.addEventListener('click',event=>{
    const choice=event.target.closest('[data-security-choice]');if(!choice)return;
    $('watchlistInput').value=choice.dataset.securityChoice;choices.hidden=true;++searchVersion;$('watchlistAdd').focus();
  });

  const request = StockWatchAction.request;

  function render() {
    const data = window.stockMainData || {reports:[],filings:[]};
    $('watchlistCount').textContent = items.length + ' 只股票';
    $('watchlistRows').innerHTML = items.map(item => {
      const filings = data.filings.filter(f => f.code === item.code);
      const latest = filings[0] || item.latest_filing;
      const related = data.reports.filter(r => r.code === item.code);
      const report = latest?.report_id ? related.find(r => r.id === latest.report_id) : null;
      const prior = report ? null : related[0];
      const company = item.company || latest?.company || '名称待核实';
      const filingText = latest ? safe((latest.fiscal_period || latest.period) + ' · ' + (latest.disclosed_at || latest.published_at || '').slice(0,10)) : '尚未发现财报';
      const status = item.security_status==='delisted'?'已退市 · 保留历史资料':latest ? (states[latest.state] || latest.status || '已收录') : '等待公告扫描';
      const reportText = report ? '<button class="link-btn" type="button" data-report="'+safe(report.id)+'">'+safe(report.period)+' · '+safe(report.date)+' →</button>' :
        prior ? '<button class="link-btn" type="button" data-report="'+safe(prior.id)+'">既往 '+safe(prior.period)+' →</button>' : '暂无解读';
      return '<tr><td>'+StockWatchAction.label(company,item.code,'company-name',{market:item.market})+'</td>'+
        '<td>'+filingText+'</td><td><span class="badge rating-watch">'+safe(status)+'</span></td><td>'+reportText+'</td>'+
        '<td><button class="link-btn watchlist-remove" type="button" data-remove="'+safe(item.code)+'" aria-label="取消关注 '+safe(company)+'">取消关注</button></td></tr>';
    }).join('');
    $('watchlistEmpty').hidden = items.length > 0;
    window.dispatchEvent(new CustomEvent('stock-watchlist-updated',{detail:{items}}));
  }

  function message(value) { $('watchlistMessage').textContent = value; }
  async function change(action, value) {
    if (busy) return;
    busy = true;
    $('watchlistAdd').disabled = true;
    message('正在保存关注列表……');
    try {
      const result = await request({action,value});
      items = result.items || [];
      render();
      if (action === 'add') { $('watchlistInput').value = ''; choices.hidden=true; ++searchVersion; }
      message(action === 'add' ? '已添加。下轮采集发现财报后将优先处理。' : '已取消关注。已有财报和解读仍保留。');
    } catch (error) { message(error.message || '保存失败，请稍后重试'); }
    finally { busy = false; $('watchlistAdd').disabled = false; }
  }

  $('watchlistForm').addEventListener('submit', event => {
    event.preventDefault();
    const value = $('watchlistInput').value.trim();
    if (value) change('add', value);
  });
  $('watchlistManage').addEventListener('click',()=>$('watchlistDialog').showModal());
  ['watchlistClose','watchlistCloseBottom'].forEach(id=>$(id).addEventListener('click',()=>$('watchlistDialog').close()));
  $('watchlistRows').addEventListener('click', event => {
    const report = event.target.closest('[data-report]');
    if (report) { location.href = './reports.html?view=watchlist&report=' + encodeURIComponent(report.dataset.report); return; }
    const remove = event.target.closest('[data-remove]');
    if (remove && !busy) {
      removingCode=remove.dataset.remove;removeOpener=remove;
      const item=items.find(item=>item.code===removingCode);
      confirmation.querySelector('#watchlistConfirmText').innerHTML=StockWatchAction.label(item?.company||'这只股票',removingCode,'company-name',{market:item?.market,watch:false});
      confirmation.showModal();confirmation.querySelector('[data-keep]').focus();
    }
  });
  window.addEventListener('stock-main-data-rendered', render);
  window.addEventListener('stock-watchlist-updated', event => {
    if (event.detail.source === 'quick-add') { items = event.detail.items || []; render(); }
  });
  request().then(result => { items = result.items || []; render(); message(''); })
    .catch(() => { $('watchlistCount').textContent = '本机服务暂不可用'; message('请检查本机设置服务，关注列表暂时无法读取。'); window.dispatchEvent(new Event('stock-watchlist-error')); });
})();
