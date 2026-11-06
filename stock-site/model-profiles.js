(() => {
  'use strict';
  const API = '/api';
  const $ = id => document.getElementById(id);
  const bases = {deepseek:{openai:'https://api.deepseek.com',anthropic:'https://api.deepseek.com/anthropic'},openai:{openai:'https://api.openai.com/v1'},anthropic:{anthropic:'https://api.anthropic.com'},gemini:{openai:'https://generativelanguage.googleapis.com/v1beta/openai'}};
  const providerName=provider=>provider==='gemini'?'Google Gemini':provider;
  function showConnection(profile){
    const presets=bases[$('modelProvider').value];
    for(const option of $('modelProtocol').options){
      option.disabled=$('modelProvider').value==='gemini'&&option.value!=='openai';
      if(option.value==='openai')option.textContent=$('modelProvider').value==='gemini'?'OpenAI 兼容接口':'OpenAI';
    }
    $('modelProtocol').value=profile?.api_protocol||Object.keys(presets)[0];
    $('modelBaseUrl').value=profile?.base_url||presets[$('modelProtocol').value]||'';
    $('customBaseConsent').checked=Boolean(profile?.base_url_confirmed);
  }
  const stagesOf=p=>[...new Set((p.stages||['extraction','deep_analysis']).map(s=>s==='analysis'?'deep_analysis':s))];
  const stageNames={extraction:'财务提取',deep_analysis:'精读'};
  function showStages(p){$('stageExtraction').checked=!p||stagesOf(p).includes('extraction');$('stageDeep').checked=!p||stagesOf(p).includes('deep_analysis');}
  let settings, editingId = null, busy = false, catalogVersion = 0;
  const message = text => {$('modelMessage').textContent = text;};
  async function request(path, data) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), path.endsWith('/test') ? 50000 : 30000);
    try {
      const options = {cache:'no-store',signal:controller.signal};
      if(data !== undefined)Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json','X-Stock-Research':'1'},body:JSON.stringify(data)});
      const response = await fetch(API+path, options), result = await response.json();
      if(!response.ok)throw new Error(result.error || '本机设置服务请求失败');
      return result;
    } finally {clearTimeout(timer);}
  }
  function setBusy(value) {
    busy=value;
    document.querySelectorAll('#model-settings button,#savedModelList input').forEach(el=>{el.disabled=value;});
    if(!value)renderProfiles();
  }
  function selectedProfile() {
    return (settings?.profiles||[]).find(p=>p.id===editingId) || (settings?.profiles||[]).find(p=>p.provider===$('modelProvider').value&&p.model===$('modelName').value&&p.api_protocol===$('modelProtocol').value&&p.base_url===$('modelBaseUrl').value.trim().replace(/\/+$/,''));
  }
  function providerState() {
    const provider=$('modelProvider').value, profile=selectedProfile();
    const base=$('modelBaseUrl').value.trim().replace(/\/+$/,''),protocol=$('modelProtocol').value;
    const suffix=protocol==='anthropic'?(base.endsWith('/v1')?'/messages':'/v1/messages'):provider==='openai'?'/responses':'/chat/completions';
    $('modelEndpoint').textContent='实际解读接口：'+base+suffix+'。仅发送公开财报与研究资料。';
    let custom=false;try{custom=new URL(base).hostname!==new URL(Object.values(bases[provider])[0]).hostname}catch{}
    $('customBaseConsentRow').hidden=!custom;
    const configured=profile?.configured||settings?.configured?.[provider];
    $('modelKeyState').textContent=configured?'已有此服务商密钥；留空保留或复用，输入新密钥只更新本项配置。':'尚未配置密钥；填写后可点击“读取可用模型”，再保存所选项。';
    $('saveModel').textContent=editingId?'更新此模型':'保存此模型';
    $('modelBudget').textContent='日常策略：每轮最多解读 '+(settings?.analysis_per_run||'不限')+' 份、每日最多调用模型 '+(settings?.daily_api_calls||'不限')+' 次。下载财报不调用模型，解读按启用的模型配置执行。';
  }
  async function loadCatalog(preferred='') {
    const version=++catalogVersion, provider=$('modelProvider').value;
    const profile=(settings?.profiles||[]).find(p=>p.id===editingId&&p.provider===provider);
    const payload={provider,profile_id:profile?.id,api_key:$('modelKey').value.trim(),api_protocol:$('modelProtocol').value,base_url:$('modelBaseUrl').value.trim(),base_url_confirmed:$('customBaseConsent').checked};
    if(!payload.api_key&&!(profile?.configured||settings?.configured?.[provider])){
      $('modelName').replaceChildren(new Option('填写密钥后读取模型',''));
      $('modelCatalogState').textContent='请填写'+providerName(provider)+'的 API 密钥，再点击“读取可用模型”。';
      return;
    }
    $('modelCatalogState').textContent='正在向服务商读取当前可用模型……';
    try {
      const catalog=await request('/models',payload);
      payload.api_key='';
      if(version!==catalogVersion)return;
      const select=$('modelName');select.replaceChildren(new Option('请选择模型',''));
      catalog.models.forEach(model=>{
        const label=model.id+(model.research_compatible?'':'（非财报解读模型）');
        const option=new Option(label,model.id);option.disabled=!model.research_compatible;select.add(option);
      });
      if(preferred&&!catalog.models.some(m=>m.id===preferred))select.add(new Option(preferred+'（已保存的兼容名称）',preferred));
      select.value=preferred||catalog.models.find(m=>m.research_compatible)?.id||'';
      $('modelCatalogState').textContent='已读取服务商返回的 '+catalog.models.length+' 个模型；可自行选择文本生成模型。';
      providerState();
    } catch(error) {
      payload.api_key='';
      if(version!==catalogVersion)return;
      $('modelName').replaceChildren(new Option(preferred?preferred+'（已保存；列表暂不可用）':'请先读取可用模型',preferred||''));
      $('modelCatalogState').textContent=error.message+'；不会用预设名单冒充实时模型列表。';
    }
  }
  function button(label, title, action, disabled=false) {
    const el=document.createElement('button');el.type='button';el.className='btn small';el.textContent=label;el.setAttribute('aria-label',title);el.disabled=busy||disabled;el.addEventListener('click',action);return el;
  }
  function renderProfiles() {
    if(!settings)return;
    $('modelReadingPolicy').textContent='所有报告直接精读；财务提取与精读可配置不同模型。超出完整阅读范围的报告转为待分章复核。下载财报和结构化采集不使用模型。';
    const list=$('savedModelList');list.replaceChildren();
    const profiles=settings.profiles||[];
    $('savedModelCount').textContent='已保存 '+profiles.length+' 项';
    profiles.forEach((p,index)=>{
      const row=document.createElement('div');row.className='model-profile';
      const priority=document.createElement('span');priority.className='model-priority';priority.textContent=String(index+1).padStart(2,'0');row.appendChild(priority);
      const label=document.createElement('label'), box=document.createElement('input');box.type='checkbox';box.checked=p.enabled;box.disabled=busy;box.setAttribute('aria-label','启用 '+p.provider+' / '+p.model);
      box.addEventListener('change',()=>saveOrder(profiles.map(item=>({...item,enabled:item.id===p.id?box.checked:item.enabled}))));
      const text=document.createElement('span'), title=document.createElement('strong'), status=document.createElement('span');
      title.textContent=providerName(p.provider)+' / '+p.model;status.className='company-code';status.textContent=(p.configured?'密钥已保存':'尚无密钥')+(p.enabled?' · 已启用':' · 未启用')+' · '+(p.api_protocol==='anthropic'?'Anthropic':p.provider==='gemini'?'OpenAI兼容':'OpenAI')+' · '+stagesOf(p).map(s=>stageNames[s]||s).join('、');
      text.append(title,status);label.append(box,text);row.appendChild(label);
      const actions=document.createElement('div');actions.className='model-actions';
      const remove=button('删除','删除 '+p.provider+' / '+p.model,()=>removeProfile(p));remove.classList.add('danger');
      actions.append(button('编辑','编辑 '+p.model,()=>edit(p)),button('检查连接','检查连接 '+p.model,()=>test(p.id)),button('↑','上移 '+p.model,()=>move(index,-1),index===0),button('↓','下移 '+p.model,()=>move(index,1),index===profiles.length-1),remove);row.appendChild(actions);list.appendChild(row);
    });
    if(!profiles.length){const empty=document.createElement('p');empty.className='subtle';empty.textContent='暂无已保存模型，请在上方选择模型并保存。';list.appendChild(empty);}
    const enabled=profiles.filter(p=>p.enabled);
    $('modelRouteSummary').textContent=['extraction','deep_analysis'].map(stage=>{const route=enabled.filter(p=>stagesOf(p).includes(stage));return stageNames[stage]+'：'+(route.length?route.map(p=>p.provider+' / '+p.model).join(' → '):'尚未配置')}).join('\n')+'\n所有报告直接精读。下载财报不使用模型；解读使用上方勾选的模型配置。';
    window.dispatchEvent(new CustomEvent('stock-model-settings-updated',{detail:settings}));
  }
  async function saveOrder(profiles) {
    setBusy(true);
    try {const result=await request('/settings/order',{profiles:profiles.map(p=>({id:p.id,enabled:p.enabled}))});settings=result.settings;message(result.message+'。');}
    catch(error){message('保存未成功：'+error.message);}
    finally{setBusy(false);}
  }
  async function removeProfile(profile) {
    if(!window.confirm('删除 '+profile.provider+' / '+profile.model+'？\n将移出调用列表，本机密钥和历史报告保留，可重新添加。未勾选的模型不会自动启用。'))return;
    setBusy(true);
    try{
      const result=await request('/settings/remove',{id:profile.id});settings=result.settings;
      if(editingId===profile.id){editingId=null;$('modelKey').value='';}
      providerState();message(result.message+'。');
    }catch(error){message('删除未成功：'+error.message);}
    finally{setBusy(false);}
  }
  function move(index,delta) {
    const profiles=settings.profiles.map(p=>({...p})), target=index+delta;
    if(target<0||target>=profiles.length)return;
    [profiles[index],profiles[target]]=[profiles[target],profiles[index]];saveOrder(profiles);
  }
  async function edit(profile) {
    editingId=profile.id;$('modelProvider').value=profile.provider;$('modelKey').value='';showStages(profile);showConnection(profile);
    providerState();message('正在编辑 '+profile.provider+' / '+profile.model+'。');await loadCatalog(profile.model);
  }
  async function test(profileId) {
    if(!profileId){message('请先保存此模型，再检查连接。');return;}
    setBusy(true);message('正在检查所选模型连接，请稍候……');
    try{const result=await request('/settings/test',{profile_id:profileId});message(result.message+' · '+result.provider+' / '+result.model);}
    catch(error){message('连接检查未通过：'+error.message);}
    finally{setBusy(false);}
  }
  $('modelProvider').addEventListener('change',()=>{editingId=null;$('modelKey').value='';showStages();showConnection();providerState();loadCatalog();});
  $('modelProtocol').addEventListener('change',()=>{$('modelBaseUrl').value=bases[$('modelProvider').value][$('modelProtocol').value]||'';$('customBaseConsent').checked=false;providerState();if($('modelBaseUrl').value)loadCatalog($('modelName').value);});
  $('modelBaseUrl').addEventListener('input',()=>{$('customBaseConsent').checked=false;providerState();});
  $('modelName').addEventListener('change',providerState);
  $('refreshModels').addEventListener('click',()=>loadCatalog($('modelName').value));
  $('newModelProfile').addEventListener('click',()=>{editingId=null;$('modelKey').value='';showStages();showConnection();providerState();message('选择模型、接口和用途后保存；相同服务商、模型和接口配置会更新已有项。');loadCatalog();});
  $('modelForm').addEventListener('submit',async event=>{
    event.preventDefault();
    if(!$('modelName').value){message('请先读取并选择模型。');return;}
    const stages=[$('stageExtraction').checked?'extraction':'',$('stageDeep').checked?'deep_analysis':''].filter(Boolean);
    if(!stages.length){message('请至少选择一种模型用途。');return;}
    const payload={id:editingId,provider:$('modelProvider').value,model:$('modelName').value,api_key:$('modelKey').value.trim(),test_mode:false,stages,api_protocol:$('modelProtocol').value,base_url:$('modelBaseUrl').value.trim(),base_url_confirmed:$('customBaseConsent').checked};
    setBusy(true);
    try{const result=await request('/settings',payload);settings=result.settings;editingId=null;$('modelKey').value='';payload.api_key='';providerState();message('模型已保存。下方可勾选启用，并用上下按钮调整备用顺序。');}
    catch(error){message(error.message);}
    finally{setBusy(false);}
  });
  $('testModel').addEventListener('click',()=>{const p=selectedProfile();if(p&&(p.api_protocol!==$('modelProtocol').value||p.base_url!==$('modelBaseUrl').value.trim().replace(/\/+$/,''))){message('接口设置已修改，请先保存再检查连接。');return;}test(p?.id);});
  $('runPipeline').addEventListener('click',async()=>{
    setBusy(true);
    try{message((await request('/run',{})).message+'。将按下方勾选的备用顺序调用模型。');}
    catch(error){message(error.message);}
    finally{setBusy(false);}
  });
  window.addEventListener('stock-runtime-settings-updated',event=>{if(settings){Object.assign(settings,event.detail);providerState();}});
  (async()=>{
    try{settings=await request('/settings');$('modelProvider').value=settings.provider;const primary=(settings.profiles||[]).find(p=>p.enabled);showStages(primary);showConnection(primary);$('modelConnection').textContent='本机设置服务已连接';providerState();renderProfiles();await loadCatalog(settings.model);}
    catch{ $('modelConnection').textContent='本机设置服务未连接';message('本机设置服务暂不可用。密钥不会保存到浏览器。');}
  })();
})();
