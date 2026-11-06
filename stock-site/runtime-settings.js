(() => {
  'use strict';
  const $=id=>document.getElementById(id), API='/api/runtime';
  let dirty=false,busy=false;
  const states={ok:'完成',partial:'部分完成',waiting:'等待续跑',running:'正在运行',failed:'运行失败',interrupted:'曾中断，等待续跑'};
  const issues={model_off_peak_wait:'等待模型闲时，采集继续、解读队列保留',report_cost_approval_required:'单份费用涨幅触发10%保护，等待批准',report_cost_baseline_missing:'缺少同模型费用基线，等待确认',financial_proof_pending:'原文取数尚待本地核验，未继续调用模型',annual_cost_hold:'费用保护触发，需核对费用异常记录',annual_cost_policy_pending:'单份费用硬上限待确认',cost_policy_adjustment:'正在调整规模平均费用保护',quota_exhausted:'模型余额或额度不足',daily_api_limit:'已达到今日模型调用上限',invalid_key:'模型密钥未通过认证',no_enabled_models:'没有勾选启用模型',missing_key:'模型密钥未配置',model_schema_invalid:'模型返回格式未通过验证',deep_reading_requires_partition:'长报告超出单次完整阅读范围，已转待分章复核',ocr_required:'部分扫描件需要核验',source_network:'公开来源网络暂不可用',previous_run_interrupted:'此前任务中断，已保留队列'};
  issues.current_archive_failed='整库备份未完成，原始报告已保存；已暂停领取新任务';
  issues.current_publication_failed='报告已保存，网站发布未完成，等待重试';
  issues.current_unexpected='报告处理发生程序异常，精读队列已暂停，请核查问题记录';
  issues.current_batch_disabled='精读任务已停用，已接收的报告与原始响应保留';
  async function request(data){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),45000);
    try{
      const options={cache:'no-store',signal:controller.signal};
      if(data)Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json','X-Stock-Research':'1'},body:JSON.stringify(data)});
      const response=await fetch(API,options),value=await response.json();
      if(!response.ok)throw Error(value.error||'运行设置请求未成功');
      return value.runtime||value;
    }finally{clearTimeout(timer);}
  }
  function display(state){
    const s=state.settings;
    window.dispatchEvent(new CustomEvent('stock-runtime-settings-updated',{detail:s}));
    if(!dirty){$('automaticEnabled').checked=s.enabled;$('runInterval').value=s.interval_minutes;$('dailyCalls').value=s.daily_api_calls;$('analysisLimit').value=s.analysis_per_run;$('downloadLimit').value=s.download_per_run;$('narrativeRefresh').value=s.narrative_refresh_days??0;}
    $('runInterval').disabled=s.schedule_policy==='adaptive-v1';
    if(!dirty)$('modelTiming').value=s.prefer_off_peak===false?'any':'idle';
    if(!dirty){$('quarterWorkers').value=String(s.quarter_workers??2);$('readingLimit').value=Number(s.financial_reading_limit_cny??.5).toFixed(2);}
    const policy=state.model_time_policy;
    $('modelTimingState').textContent=policy?.deferred?policy.reason+'；实际续跑时间取决于定时检查、剩余额度及电脑唤醒状态。':policy?.enabled?'已启用闲时策略：DeepSeek 官方模型在工作日9:00—12:00、14:00—18:00及开始前5分钟等待；已核实的节假日及周末全天可运行。连接检查不受限。':s.prefer_off_peak===false?'解读不限时段，可能按高峰价格计费。':'已启用闲时优先；未确认峰谷定价的模型及自定义接口沿用原调用时段。';
    $('runtimeState').textContent=s.enabled&&s.schedule_policy==='adaptive-v1'?'全天每15分钟查新；下载后即刻排队精读':s.enabled&&state.schedule_registered?'自动运行已启用':s.enabled?'前台自动运行已启用':'自动运行已关闭';
    $('runtimeHealth').textContent='项目独立环境：'+(state.independent_python?'已就绪':'待部署')+'；登录启动服务：'+(state.service_managed?'已登记':'待登记')+'；系统定时任务：'+(state.schedule_registered?'已登记':'未启用')+'。前台模式下关闭服务即停止运行。';
    if(state.foreground)$('runtimeHealth').textContent='本地前台服务已就绪；自动任务仅在该服务运行期间执行。未安装系统启动项。';
    const q=state.queue||[],count=kind=>q.find(x=>x.state===kind)?.n||0;
    $('runtimeProgress').textContent='原文库记录（含历史版本及排除项）：未下载 '+count('pending')+' 份 · 已解析未完成 '+count('parsed')+' 份 · 已完成 '+count('complete')+' 份 · 待复核 '+count('review')+' 份。今日日常模型调用 '+state.api_calls_today+(s.daily_api_calls?' / '+s.daily_api_calls:'（数量不限）')+' 次。'+(state.tokens_today?'今日模型返回用量：输入 '+state.tokens_today.input.toLocaleString()+'、输出 '+state.tokens_today.output.toLocaleString()+' token；失败且未返回用量的请求不含在内，实际费用以服务商账单为准。':'');
    const last=state.latest_run;
    $('runtimeLastRun').textContent=last?'最近常规流程：'+last.started_at+' · '+(states[last.state]||last.state)+(last.state==='running'?'；完成后汇总本轮新增、提取与解读数量。':'；新增 '+last.counts.new+' 份、提取 '+last.counts.parsed+' 份、解读 '+last.counts.analyzed+' 份。'):'尚无执行记录。';
    const codes=[...new Set([...(last?.blockers||[]),...(last?.error_codes||[])])];
    $('runtimeIssues').textContent=codes.length?'需要关注：'+codes.map(c=>issues[c]||c).join('；'):'暂无本轮阻塞项。';
  }
  $('runtimeForm').addEventListener('input',()=>dirty=true);
  $('runtimeForm').addEventListener('submit',async event=>{
    event.preventDefault();if(busy)return;busy=true;$('saveRuntime').disabled=true;
    $('runtimeMessage').textContent='正在保存运行设置……';
    try{const state=await request({enabled:$('automaticEnabled').checked,interval_minutes:Number($('runInterval').value),daily_api_calls:Number($('dailyCalls').value),analysis_per_run:Number($('analysisLimit').value),download_per_run:Number($('downloadLimit').value),narrative_refresh_days:Number($('narrativeRefresh').value),prefer_off_peak:$('modelTiming').value==='idle',quarter_workers:Number($('quarterWorkers').value),financial_reading_limit_cny:Number($('readingLimit').value)});dirty=false;display(state);$('runtimeMessage').textContent=state.warning||(state.settings.enabled?'已保存，系统会按所选频率运行。':'已关闭自动运行，历史资料和处理队列保留。');}
    catch(error){$('runtimeMessage').textContent=error.message||'保存失败，请稍后重试';}
    finally{busy=false;$('saveRuntime').disabled=false;}
  });
  async function refresh(){if(busy)return;try{display(await request());}catch{$('runtimeState').textContent='本机服务暂不可用';}}
  refresh();setInterval(refresh,15000);
})();
