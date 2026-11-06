(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  async function fetchJson(url) {
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),8000);
    try{const r=await fetch(url,{cache:'no-store',signal:controller.signal});if(!r.ok)throw Error();return await r.json();}
    finally{clearTimeout(timer);}
  }
  async function json(name) {return fetchJson('./data/'+name+'.json');}
  async function refresh() {
    const results=await Promise.allSettled(['status','reports','filings','digests'].map(json));
    const [state,reports,filings,digests]=results.map(r=>r.status==='fulfilled'?r.value:null);
    if(state){
      $('automationStatus').textContent=state.schedule?.active?(state.schedule.engine==='launchd'?'系统自动运行 · 每 '+(state.schedule.interval_minutes||60)+' 分钟':'每小时自动检查已启用'):'自动流程可运行 · 定时任务未启用';
      $('dataTimestamp').textContent='数据更新 '+new Date(state.generated_at).toLocaleString('zh-CN',{hour12:false});
      const unavailable=(state.scan?.sources||[]).filter(s=>s.state!=='ok');
      $('scanStatus').textContent='已收录 '+state.document_count+' 份'+(unavailable.length?'；部分来源不可用，覆盖不完整':'')+'；待补采 '+(state.scan?.backfill_tasks||0)+' 个日期任务';
      const queue=(state.queue||[]).filter(q=>q.state!=='complete').reduce((n,q)=>n+q.count,0);
      const configured=state.settings?.configured?.[state.settings?.provider];
      $('analysisStatus').textContent=configured?'队列待处理 '+queue+' 份；'+(state.settings.analysis_per_run?'每次最多 '+state.settings.analysis_per_run+' 份':'解读数量不限'):'等待 '+(state.settings?.provider||'模型')+' 密钥；已保留 '+queue+' 份任务';
      if(state.settings?.enabled_count===0)$('analysisStatus').textContent='没有勾选模型，自动解读已停用；采集仍运行，队列保留 '+queue+' 份';
      $('ratingStatus').textContent='研究服务 · '+(state.settings?.provider||'')+' / '+(state.settings?.model||'')+'；已发布 '+state.report_count+' 份';
      const routes=state.settings?.stage_routes;
      if(routes){
        const missing=[['extraction','财务提取'],['analysis','综合解读']].filter(([stage])=>!(routes[stage]||[]).some(p=>p.configured));
        if(missing.length)$('analysisStatus').textContent=missing.map(([,label])=>label).join('、')+'没有已启用且配置密钥的模型；采集继续，队列保留 '+queue+' 份';
        const primary=routes.analysis?.[0];
        $('ratingStatus').textContent=(primary?'综合解读首选 · '+primary.provider+' / '+primary.model:'综合解读未启用模型')+'；已发布 '+state.report_count+' 份';
      }
      const blockers=state.summary?.blockers||[];
      if(blockers.includes('model_off_peak_wait'))$('analysisStatus').textContent='等待模型闲时，采集继续，解读任务已保留';
      if(blockers.some(x=>x==='quota_exhausted'))$('analysisStatus').textContent='模型服务额度不足，任务已保留，稍后自动复核';
      if(state.run?.state==='failed')$('analysisStatus').textContent='最近流程未完成，请检查运行状态；原始资料已保留';
    }
    if(reports||filings)window.dispatchEvent(new CustomEvent('stock-data-updated',{detail:{reports:reports?.reports,filings:filings?.filings}}));
    if(digests)for(const prefix of ['daily','weekly'])if(digests[prefix])$(prefix+'Summary').textContent=digests[prefix].conclusion;
    try{
      const runtime=await fetchJson('/api/runtime');
      $('automationStatus').textContent=runtime.settings.enabled?(runtime.schedule_registered?'系统自动运行 · 每 '+runtime.settings.interval_minutes+' 分钟':'自动运行已开启，但系统任务未登记成功'):'系统自动运行已关闭';
      if(runtime.foreground)$('automationStatus').textContent=runtime.settings.enabled?'前台自动运行 · 每 '+runtime.settings.interval_minutes+' 分钟':'前台服务运行中 · 自动任务关闭';
      if(runtime.latest_run?.state==='running')$('analysisStatus').textContent='系统任务正在采集或解读；完成后自动发布结果。';
      if(runtime.model_time_policy?.deferred)$('analysisStatus').textContent=runtime.model_time_policy.reason+'；下一轮定时检查续跑。';
    }catch{/* Published research stays readable when the local control service is offline. */}
  }
  refresh();
  setInterval(refresh,60000);
})();
