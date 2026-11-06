(() => {
  'use strict';
  const panel=document.getElementById('researchQuality');
  if(!panel)return;
  let loaded=false,loading=false;
  panel.addEventListener('toggle',async()=>{
    if(!panel.open||loaded||loading)return;
    loading=true;
    const content=document.getElementById('researchQualityBody');
    try{
      const response=await fetch('./data/research-quality.json',{cache:'no-store'});
      if(!response.ok)throw Error();
      const data=await response.json();
      content.replaceChildren();
      const note=document.createElement('p');note.className='subtle';
      note.textContent=`${data.report_count}份最新解读，${data.provisional_count}份暂定评分。${data.method}`;
      content.appendChild(note);
      const table=document.createElement('table');
      const head=table.createTHead().insertRow();
      ['缺失因子','涉及报告','处理状态','补齐条件'].forEach(label=>{const cell=document.createElement('th');cell.textContent=label;head.appendChild(cell)});
      const body=table.createTBody();
      for(const factor of data.factors){const row=body.insertRow();[factor.factor,factor.reports,factor.state,factor.action].forEach(value=>{row.insertCell().textContent=String(value)})}
      const wrap=document.createElement('div');wrap.className='table-wrap';wrap.appendChild(table);content.appendChild(wrap);
      loaded=true;
    }catch{content.textContent='数据缺口清单暂不可用，请稍后重试。'}finally{loading=false;}
  });
})();
