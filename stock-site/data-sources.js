(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const names={eastmoney:'东方财富',ths:'同花顺',cninfo:'巨潮资讯',sh:'上交所',bj:'北交所'};
  const checks={matched:'已核对一致',conflict:'存在差异，待复核',unavailable:'辅助源暂不可用',not_comparable:'时间或口径暂不可比'};
  fetch('/api/sources',{cache:'no-store'})
    .then(response=>{if(!response.ok)throw Error();return response.json()})
    .then(data=>{
      $('sourcePolicy').textContent='沪市：上交所优先；深市：巨潮资讯；北交所：北交所优先。官方源不可用时，沪市和北交所使用巨潮补采并标明状态。';
      const scans=data.last_scan||[];
      $('disclosureSources').textContent=scans.length?'最近采集：'+scans.map(source=>source.name+' · '+(source.state==='fallback'?'官方源暂不可用，已用巨潮补采':source.state==='unavailable'?'暂不可用':source.provider?(names[source.provider]||source.provider)+'已返回数据':'等待按新规则采集')).join('；'):'等待按新规则采集。';
      const quote=data.last_market_check||{};
      $('marketSources').textContent='行情主源：东方财富；交叉与备用：同花顺。'+(quote.asof?'最近核验 '+quote.asof+'；实际报价 '+(names[quote.quote_provider]||'未获取')+'、日线 '+(names[quote.bars_provider]||'未获取')+'；报价 '+(checks[quote.quote_check]||'未完成')+'，日线 '+(checks[quote.bars_check]||'未完成')+'。':'首次实际获取后显示核验状态。');
      const pdf=data.last_pdf_check||{};
      if(pdf.fallback_reason)$('disclosureSources').textContent+=' 最近原文下载使用精确匹配的巨潮副本，官网原文暂不可直接下载。';
    }).catch(()=>{$('sourcePolicy').textContent='数据来源状态暂不可用，请检查本机设置服务。'});
})();
