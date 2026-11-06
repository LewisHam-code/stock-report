(() => {
  'use strict';
  const API = '/api';
  const $ = id => document.getElementById(id);
  let settings;
  const endpoints = {deepseek:'https://api.deepseek.com/chat/completions',openai:'https://api.openai.com/v1/responses'};
  function providerState() {
    const provider = $('modelProvider').value;
    $('modelEndpoint').textContent = '接口地址：' + endpoints[provider] + '。仅将公开财报与研究资料发送给所选服务商。';
    $('modelKeyState').textContent = settings?.configured?.[provider] ? '此服务商密钥已配置。留空保留，输入新值会更新。' : '此服务商尚未配置密钥。请在此输入并保存。';
  }
  async function request(path, data) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), path.endsWith('/test') ? 50000 : 12000);
    try {
      const options = {cache:'no-store',signal:controller.signal};
      if (data !== undefined) Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json','X-Stock-Research':'1'},body:JSON.stringify(data)});
      const response = await fetch(API + path, options);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '本机服务请求失败');
      return result;
    } finally { clearTimeout(timer); }
  }
  function setBusy(busy) {
    ['saveModel','testModel','runPipeline'].forEach(id => {$(id).disabled = busy;});
  }
  async function load() {
    try {
      settings = await request('/settings');
      $('modelProvider').value = settings.provider;
      $('modelName').value = settings.model;
      $('modelTestMode').checked = settings.test_mode;
      $('modelConnection').textContent = '本机设置服务已连接';
      providerState();
    } catch {
      $('modelConnection').textContent = '本机设置服务未连接';
      $('modelMessage').textContent = '本机设置服务暂不可用。密钥不会保存在浏览器中。';
    }
  }
  $('modelProvider').addEventListener('change', () => {
    $('modelName').value = $('modelProvider').value === 'deepseek' ? 'deepseek-chat' : 'gpt-5-mini';
    $('modelKey').value = '';
    providerState();
  });
  $('modelForm').addEventListener('submit', async event => {
    event.preventDefault();
    const payload = {provider:$('modelProvider').value,model:$('modelName').value.trim(),api_key:$('modelKey').value.trim(),test_mode:$('modelTestMode').checked};
    setBusy(true);
    try {
      const result = await request('/settings', payload);
      settings = result.settings;
      $('modelKey').value = '';
      payload.api_key = '';
      providerState();
      $('modelMessage').textContent = result.message + '。可继续点击“测试已保存的连接”；运行任务将使用此设置。';
    } catch (error) { $('modelMessage').textContent = error.message; }
    finally { setBusy(false); }
  });
  $('testModel').addEventListener('click', async () => {
    setBusy(true);
    $('modelMessage').textContent = '正在进行一次最小模型调用，请稍候……';
    try {
      const result = await request('/settings/test', {});
      $('modelMessage').textContent = result.message + ' · ' + result.provider + ' / ' + result.model + (result.test_mode ? ' · 测试模式' : ' · 正式模式');
    } catch (error) { $('modelMessage').textContent = '连接测试未通过：' + error.message; }
    finally { setBusy(false); }
  });
  $('runPipeline').addEventListener('click', async () => {
    setBusy(true);
    try { $('modelMessage').textContent = (await request('/run', {})).message + '。运行结果会自动更新到网页。'; }
    catch (error) { $('modelMessage').textContent = error.message; }
    finally { setBusy(false); }
  });
  load();
})();
