(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const safe=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let filings=[],loadError=false,page=StockPagination.parsePage(new URLSearchParams(location.search).get('page'));
  function updatePageUrl(){const params=new URLSearchParams(location.search);if(page>1)params.set('page',String(page));else params.delete('page');history.replaceState(null,'',location.pathname+(params.size?'?'+params.toString():''))}
  function changePage(next){page=next;updatePageUrl();render();document.querySelector('.library').scrollIntoView({block:'start'})}
  function resetPage(){page=1;updatePageUrl();render()}
  function allowedSource(raw){
    try{const url=new URL(raw);return url.protocol==='https:'&&['static.cninfo.com.cn','static.sse.com.cn','www.sse.com.cn','disc.static.szse.cn','www.bse.cn','bse.cn'].includes(url.hostname)&&!url.username&&!url.password&&(!url.port||url.port==='443')&&url.pathname.toLowerCase().endsWith('.pdf')}
    catch{return false}
  }
  function normalize(item){
    if(!item||!allowedSource(item.source_url)||!item.code||!item.company||!item.market_name||!item.disclosed_at)return null;
    return {...item,code:String(item.code),company:String(item.company),market_name:String(item.market_name),industry:String(item.industry||'未核实'),fiscal_period:String(item.fiscal_period||''),disclosed_at:String(item.disclosed_at),status:String(item.status||'待解读'),source_url:String(item.source_url),report_id:/^[a-zA-Z0-9-]+$/.test(String(item.report_id||''))?String(item.report_id):''};
  }
  function render(){
    const select=$('filingIndustry'),old=select.value;
    select.replaceChildren(new Option('全部行业',''));
    [...new Set(filings.map(item=>item.industry))].sort((a,b)=>a.localeCompare(b,'zh-CN')).forEach(industry=>select.add(new Option(industry,industry)));
    select.value=old;if(select.selectedIndex<0)select.value='';
    const q=$('filingSearch').value.trim().normalize('NFKC').toLocaleLowerCase(),market=$('filingMarket').value,industry=select.value,date=$('filingDate').value;
    const shown=filings.filter(item=>(!market||item.market_name===market)&&(!industry||item.industry===industry)&&(!date||item.disclosed_at.slice(0,10)===date)&&(!q||[item.code,StockWatchAction.searchNames(item),item.title,item.market_name,item.industry].join(' ').normalize('NFKC').toLocaleLowerCase().includes(q)));
    const paged=StockPagination.paginate(shown,page);
    if(page!==paged.page){page=paged.page;updatePageUrl()}
    $('filingCount').textContent=filings.length+' 份原文';
    $('filingResultCount').textContent=shown.length+' 条结果';
    $('filingRows').innerHTML=paged.items.map(item=>{
      const report=item.report_id?'<a class="link-btn" href="./reports.html?view=recent&amp;report='+encodeURIComponent(item.report_id)+'">查看解读 →</a>':'';
      const fallback=item.source_fallback&&allowedSource(item.download_url)?'<a class="link-btn" href="'+safe(item.download_url)+'" target="_blank" rel="noopener noreferrer">巨潮副本 →</a>':'';
      return '<tr><td>'+StockWatchAction.label(item.company,item.code,'company-name',{market:item.market})+'</td><td>'+safe(item.market_name)+'<span class="company-code">'+safe(item.industry)+'</span></td><td>'+safe(item.fiscal_period)+'</td><td>'+safe(item.disclosed_at.slice(0,10))+'</td><td><span class="badge rating-watch">'+safe(item.status)+'</span></td><td><div class="filing-links"><a class="link-btn" href="'+safe(item.source_url)+'" target="_blank" rel="noopener noreferrer" aria-label="查看 '+safe(item.company)+' 财报原文">查看 PDF →</a>'+fallback+report+'</div></td></tr>';
    }).join('');
    $('filingEmpty').hidden=shown.length>0;
    $('filingPagination').hidden=paged.pages<2;
    $('filingPageRange').textContent='显示 '+paged.from+'–'+paged.to+' 条，共 '+paged.total+' 条';
    $('filingPageInfo').textContent='第 '+paged.page+' / '+paged.pages+' 页';
    $('filingPrev').disabled=paged.page<=1;
    $('filingNext').disabled=paged.page>=paged.pages;
    $('filingEmptyReason').textContent=loadError?'原文索引暂不可用，请检查本机网站文件。':'可调整筛选条件。';
  }
  ['filingSearch','filingMarket','filingIndustry','filingDate'].forEach(id=>$(id).addEventListener(id==='filingSearch'?'input':'change',resetPage));
  $('filingReset').addEventListener('click',()=>{['filingSearch','filingMarket','filingIndustry','filingDate'].forEach(id=>$(id).value='');resetPage()});
  $('filingPrev').addEventListener('click',()=>changePage(page-1));
  $('filingNext').addEventListener('click',()=>changePage(page+1));
  window.addEventListener('popstate',()=>{page=StockPagination.parsePage(new URLSearchParams(location.search).get('page'));render()});
  fetch('./data/filings.json',{cache:'no-store'}).then(response=>{if(!response.ok)throw Error();return response.json()})
    .then(data=>{filings=(Array.isArray(data.filings)?data.filings:[]).map(normalize).filter(Boolean).sort((a,b)=>b.disclosed_at.localeCompare(a.disclosed_at));render()})
    .catch(()=>{loadError=true;render()});
})();
