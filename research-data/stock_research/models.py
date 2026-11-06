from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import timedelta

from .core import ServiceError, Store, api_key, config, digest, dumps, model_profiles, now, stamp
from .model_endpoints import endpoint_settings, api_url, check_custom_destination

ENDPOINTS = {"openai": "https://api.openai.com/v1/responses", "deepseek": "https://api.deepseek.com/chat/completions", "anthropic":"https://api.anthropic.com/v1/messages",
             "gemini":"https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ServiceError("unsafe_model_redirect", "模型接口发生跳转，已停止转发密钥；请检查接口地址")


def model_open(request, timeout, custom=False):
    # All providers share this transport, including catalog and connection
    # checks. Never allow urllib to resend an authenticated request on redirect.
    return urllib.request.build_opener(NoRedirect()).open(request,timeout=timeout)


def _completion_once(instructions, prompt, schema=None, store=None, purpose="analysis", max_tokens=None, web=False, settings=None):
    cfg = settings or config()
    if cfg.get('_live_execution_policy'):
        live=config()
        cfg={**cfg,**{key:live[key] for key in ('prefer_off_peak','financial_reading_cost_policy') if key in live}}
    provider, model = cfg.get("provider", "deepseek"), cfg["model"]
    if provider not in ENDPOINTS:
        raise ServiceError("invalid_provider", "不支持的模型服务商")
    endpoint = {"provider":provider, **endpoint_settings(provider,cfg.get("api_protocol"),cfg.get("base_url")), "base_url_confirmed":cfg.get("base_url_confirmed",False)}
    check_custom_destination(endpoint)
    from .model_timing import require_idle
    require_idle(cfg, purpose)
    if cfg.get('one_off_annual_task'):
        from .annual_batch import admit
        admit(cfg)
    if cfg.get('report_batch_task'):
        from .current_batch import admit
        admit(cfg)
    protocol = endpoint["api_protocol"]
    key = api_key(provider, cfg.get("profile_id"))
    circuit_key = "ai_circuit_" + cfg.get("profile_id", provider)
    if store:
        circuit = store.setting(circuit_key, {})
        if circuit.get("until", "") > stamp() and purpose != "connection_test":
            raise ServiceError(circuit.get("code", "ai_circuit_open"), "模型服务暂不可用，等待下次自动复核")
    cap = max_tokens or cfg["max_output_tokens"]
    if protocol == "anthropic":
        if web:
            raise ServiceError("web_search_not_supported", "该协议使用独立公开资料采集")
        body = dict(model=model,system=instructions,messages=[{"role":"user","content":prompt}],max_tokens=cap,stream=False)
        if schema:
            body["tools"] = [{"name":"submit_report","description":"Return the requested structured result","input_schema":schema}]
            body["tool_choice"] = {"type":"tool","name":"submit_report"}
        if provider == "deepseek":
            # DeepSeek rejects forced tool_choice while thinking is enabled.
            # Structured reports require the tool schema, so disable thinking
            # for this compatibility path rather than publishing untyped text.
            if schema or purpose in ("financial_extraction","connection_test"):
                body["thinking"] = {"type":"disabled"}
            else:
                body["output_config"] = {"effort":"low"}
    elif provider != "openai":
        if web:
            raise ServiceError("web_search_not_supported", "该服务商使用独立公开资料采集，不调用模型内置搜索")
        if schema and provider != "gemini":
            instructions += "\n必须输出一个合法JSON对象，严格符合此JSON Schema：" + dumps(schema)
        modern = provider == "deepseek" and (model.startswith("deepseek-v4") or model == "deepseek-flash")
        body = dict(model=model, messages=[{"role": "system", "content": instructions}, {"role": "user", "content": prompt}], max_tokens=cap if provider == "gemini" else min(cap, 32768 if modern else 8192), stream=False)
        if modern:
            if purpose in ("financial_extraction", "connection_test") or (purpose == "comprehensive_report" and cfg.get('initial_report_thinking', 'low') == 'disabled'):
                body["thinking"] = {"type": "disabled"}
            else:
                body["reasoning_effort"] = "low"
        if provider == "gemini" and model.startswith(("gemini-2.5", "gemini-3", "gemini-4")):
            body["reasoning_effort"] = "low"
            if purpose == "connection_test":
                body["max_tokens"] = max(body["max_tokens"], 2048)
        if schema:
            body["response_format"] = {"type":"json_schema", "json_schema":{"name":"stock_research", "strict":True, "schema":schema}} if provider == "gemini" else {"type": "json_object"}
    else:
        body = dict(model=model, instructions=instructions, input=prompt, max_output_tokens=cap, store=False)
        if model.startswith(("gpt-5", "gpt-6")):
            body["reasoning"] = {"effort": "low"}
        if schema:
            body["text"] = {"format": {"type": "json_schema", "name": "stock_research", "strict": True, "schema": schema}}
        if web:
            body["tools"] = [{"type": "web_search", "search_context_size": "low"}]
    prior=cfg.get('_prior_method_inputs',{}).get(purpose)
    if prior:
        from copy import deepcopy
        prior_body=deepcopy(body)
        if protocol=='anthropic':
            prior_body['system']=prior['instructions'];prior_body['messages']=[{'role':'user','content':prior['prompt']}]
        elif provider=='openai':
            prior_body.update(instructions=prior['instructions'],input=prior['prompt'])
        else:
            system=prior['instructions']
            if schema and provider!='gemini':system+='\n必须输出一个合法JSON对象，严格符合此JSON Schema：'+dumps(schema)
            prior_body['messages']=[{'role':'system','content':system},{'role':'user','content':prior['prompt']}]
        cfg={**cfg,'_cost_prior_body':prior_body}
    from .report_cost_guard import require_approved,record_usage
    cost_forecast=require_approved(cfg,purpose,body)
    from .foundation_budget import reserve as reserve_task, settle as settle_task
    try:ticket=reserve_task(cfg,purpose,body)
    except Exception:
        from .reading_costs import cancel_request
        cancel_request(cfg,cost_forecast)
        raise
    usage={}
    tag=('foundation:'+cfg['foundation_task']+':'+purpose) if ticket else purpose
    try:
        call_id = store.reserve_api_call(tag, 10**9 if ticket else cfg["daily_api_calls"]) if store else None
    except Exception:
        # No network request was made; release this task's provisional cost.
        settle_task(ticket,{'input_tokens':0,'output_tokens':0})
        from .reading_costs import cancel_request
        cancel_request(cfg,cost_forecast)
        raise
    headers = {"Content-Type":"application/json"}
    headers.update({"x-api-key":key,"anthropic-version":"2023-06-01"} if protocol == "anthropic" else {"Authorization":"Bearer "+key})
    req = urllib.request.Request(api_url(endpoint,"completion"), data=dumps(body).encode(), headers=headers)
    try:
        with model_open(req, timeout=cfg.get('_request_timeout_seconds',180) if purpose != "connection_test" else 35,custom=endpoint["custom_base_url"]) as response:
            data = json.load(response)
        if not isinstance(data,dict):
            raise ServiceError('model_response_invalid','模型响应结构异常，未发布报告')
        usage=data.get('usage') if isinstance(data.get('usage'),dict) else {}
        def tokens(*names):
            value=next((usage[name] for name in names if usage.get(name) is not None),None)
            return value if isinstance(value,int) and not isinstance(value,bool) and value>=0 else None
        # A returned response can consume tokens even if its JSON, schema or
        # stop reason fails validation. Preserve provider-reported usage first.
        if store:
            store.execute('UPDATE api_calls SET input_tokens=?,output_tokens=? WHERE id=?',
                          (tokens('input_tokens','prompt_tokens'),tokens('output_tokens','completion_tokens'),call_id))
        record_usage(cfg,purpose,body,usage,cost_forecast)
        observer=cfg.get('_model_usage_observer')
        if observer is not None:observer(purpose,usage,data.get('model'))
        response_observer=cfg.get('_model_response_observer')
        if response_observer is not None:response_observer(purpose,data)
        structured = None
        if protocol == "anthropic":
            if data.get("stop_reason") not in ("end_turn","tool_use"):
                raise ServiceError("model_output_incomplete", "模型输出未完整结束，未生成报告")
            blocks = data.get("content") or []
            if schema:
                structured = next((b.get("input") for b in blocks if b.get("type")=="tool_use" and b.get("name")=="submit_report"),None)
            text = "\n".join(b.get("text","") for b in blocks if b.get("type")=="text")
            annotations = []
        elif provider != "openai":
            choices = data.get("choices") or []
            if not choices or choices[0].get("finish_reason") != "stop":
                raise ServiceError("model_output_incomplete", "模型输出未完整结束，未生成报告")
            text = choices[0]["message"].get("content", "")
            annotations = []
        else:
            if data.get("status") != "completed":
                raise ServiceError("model_output_incomplete", "模型输出未完整结束，未生成报告")
            blocks = [b for item in data.get("output", []) for b in item.get("content", []) if b.get("type") == "output_text"]
            text = "\n".join(b["text"] for b in blocks)
            annotations = [a for b in blocks for a in b.get("annotations", [])]
        if not text and structured is None:
            raise ServiceError("model_empty_output", "模型未返回正文")
        local_normalizations = []
        if schema and structured is None and purpose == 'financial_extraction':
            from .response_recovery import parse as parse_saved_format
            parsed, local_normalizations = parse_saved_format(text)
        else:
            parsed = (structured if structured is not None else json.loads(text)) if schema else text
        if schema and purpose == 'financial_extraction':
            from .model_inputs import relocate_unlocated_claims
            parsed, relocations = relocate_unlocated_claims(parsed)
            local_normalizations += relocations
        if schema and (not isinstance(parsed, dict) or set(schema.get("required", [])) - set(parsed)):
            raise ServiceError("model_schema_invalid", "模型缺少必需字段，未发布报告")
        if schema:
            from .analysis import validate
            validate(parsed, schema)
        if cost_forecast and cost_forecast.get('kind') in ('annual-size-average-v1','report-size-average-v2'):
            from .annual_costs import mark_valid
            mark_valid(cost_forecast)
        if store:
            store.execute("UPDATE api_calls SET state='ok' WHERE id=?", (call_id,))
            store.set(circuit_key, {})
        return parsed, {"provider": provider, "model": model, "response_model":data.get("model"), "api_protocol":protocol, "profile_id": cfg.get("profile_id"),
                        "request_fingerprint":digest(body), "prompt_version":digest([instructions,schema]),
                        "input_fingerprint":digest(prompt),
                        **({'_request_body':body} if purpose in ('financial_extraction','comprehensive_report','deep_report') else {}),
                        "reasoning_setting":body.get("reasoning") or body.get("thinking") or body.get("reasoning_effort"),
                        "usage": usage, "annotations": annotations, "test_mode": cfg.get("test_mode", False),
                        **({'local_normalizations': local_normalizations} if local_normalizations else {})}
    except urllib.error.HTTPError as e:
        # Never log response text: a provider may echo user input or credentials.
        try:
            err = json.loads(e.read(8192)).get("error") or {}
            provider_code = str(err.get("code") or err.get("type") or "")
        except Exception:
            provider_code = ""
        code = "quota_exhausted" if e.code == 402 or any(x in provider_code for x in ("quota", "balance", "credit")) else "invalid_key" if e.code == 401 else "model_access_denied" if e.code in (403, 404) else "rate_limited" if e.code == 429 else "model_http_" + str(e.code)
        if store:
            store.execute("UPDATE api_calls SET state=? WHERE id=?", (code, call_id))
            delay = 1 if code in ("quota_exhausted", "rate_limited") else 6
            store.set(circuit_key, {"code": code, "until": (now() + timedelta(hours=delay)).isoformat(timespec="seconds")})
        messages = {"quota_exhausted": "所选模型服务余额或额度不足，请检查服务商账单", "invalid_key": "密钥未通过服务商认证", "model_access_denied": "模型名称无效或项目无访问权限", "rate_limited": "模型服务限流，稍后自动续跑"}
        raise ServiceError(code, messages.get(code, "模型服务暂时不可用")) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        if store:
            store.execute("UPDATE api_calls SET state='network_error' WHERE id=?", (call_id,))
        raise ServiceError("model_network", "无法连接模型服务，请检查网络") from None
    except json.JSONDecodeError:
        if store:
            store.execute("UPDATE api_calls SET state='invalid_json' WHERE id=?", (call_id,))
        raise ServiceError("model_json_invalid", "模型未返回合法JSON，未发布报告") from None
    except (AttributeError, KeyError, IndexError, TypeError):
        if store:
            store.execute("UPDATE api_calls SET state='model_response_invalid' WHERE id=?", (call_id,))
        raise ServiceError("model_response_invalid", "模型响应结构异常，未发布报告") from None
    except ServiceError as e:
        if store:
            store.execute("UPDATE api_calls SET state=? WHERE id=?", (e.code, call_id))
            if e.code=='unsafe_model_redirect':
                store.set(circuit_key,{'code':e.code,'until':(now()+timedelta(hours=6)).isoformat(timespec='seconds')})
        raise
    finally:
        settle_task(ticket,usage)
        if cost_forecast and cost_forecast.get('kind')=='financial-per-report-cap-v1':
            from .reading_costs import record_usage as settle_reading
            settle_reading(cfg,purpose,body,usage,cost_forecast)
        if cost_forecast and cost_forecast.get('kind') in ('annual-size-average-v1','report-size-average-v2'):
            from .annual_costs import transport_finished
            transport_finished(cfg,purpose,body,usage,cost_forecast)


def completion(instructions, prompt, schema=None, store=None, purpose="analysis", max_tokens=None, web=False, settings=None):
    cfg = settings or config()
    if cfg.get("profile_id") or cfg.get("single_model"):
        return _completion_once(instructions, prompt, schema, store, purpose, max_tokens, web, cfg)
    stage = {"financial_extraction": "extraction", "comprehensive_report": "deep_analysis", "analysis": "deep_analysis", "deep_report":"deep_analysis"}.get(purpose)
    profiles = [p for p in model_profiles(cfg) if p["enabled"] and (not stage or stage in p["stages"])]
    if not profiles:
        raise ServiceError("no_enabled_models", "当前处理环节没有启用的模型，请在设置中勾选相应用途")
    failures, last_error = [], None
    for profile in profiles:
        selected = {**cfg, **profile, "profile_id": profile["id"]}
        try:
            result, meta = _completion_once(instructions, prompt, schema, store, purpose, max_tokens, web, selected)
            meta["fallbacks"] = failures
            return result, meta
        except ServiceError as e:
            if e.code in ('unsafe_model_redirect','model_off_peak_wait','report_cost_approval_required','report_cost_baseline_missing','annual_cost_hold','annual_cost_policy_pending','report_cost_limit','report_cost_unestimated'):
                raise  # Deferral must not silently buy another provider's service.
            failures.append({"profile_id": profile["id"], "provider": profile["provider"], "model": profile["model"], "code": e.code})
            last_error = e
            if e.code == "daily_api_limit":
                break
    error = ServiceError(last_error.code if last_error else "no_available_models", "已按勾选顺序尝试模型，均未成功。" + (str(last_error) if last_error else ""))
    error.fallbacks = failures
    raise error


def connection_test(profile_id=None):
    cfg = config()
    if profile_id:
        profile = next((p for p in model_profiles(cfg) if p["id"] == profile_id), None)
        if not profile:
            raise ServiceError("unknown_profile", "模型配置不存在")
        cfg = {**cfg, **profile, "profile_id": profile["id"]}
    store = Store()
    try:
        text, meta = completion("这是最小连接检查。请只回复OK。", "请回复OK。", store=store, purpose="connection_test", max_tokens=256, settings=cfg)
    finally:
        store.conn.close()
    return {"ok": True, "message": "连接成功，模型已实际返回响应", "provider": meta["provider"], "model": meta["model"], "test_mode": meta["test_mode"]}


def list_models(provider, profile_id=None, temporary_key=None, base_url=None, api_protocol=None, base_url_confirmed=False):
    if provider not in ENDPOINTS:
        raise ServiceError("invalid_provider", "不支持的模型服务商")
    profile = next((p for p in model_profiles() if p["id"]==profile_id and p["provider"]==provider),{})
    endpoint = {"provider":provider,**endpoint_settings(provider,api_protocol or profile.get("api_protocol"),base_url or profile.get("base_url")),
                "base_url_confirmed":base_url_confirmed or (bool(profile.get("base_url_confirmed")) and (not base_url or base_url.rstrip('/')==profile.get("base_url")))}
    check_custom_destination(endpoint)
    key = temporary_key or api_key(provider, profile_id)
    headers={"Accept":"application/json"}
    headers.update({"x-api-key":key,"anthropic-version":"2023-06-01"} if provider=="anthropic" or (endpoint["api_protocol"]=="anthropic" and endpoint["custom_base_url"]) else {"Authorization":"Bearer "+key})
    request = urllib.request.Request(api_url(endpoint,"models"),headers=headers)
    try:
        with model_open(request,timeout=20,custom=endpoint["custom_base_url"]) as response:
            payload = json.load(response)
        if not isinstance(payload.get("data"), list):
            raise ServiceError("model_catalog_invalid", "服务商未返回可用的模型列表")
        models = []
        seen = set()
        for item in payload["data"]:
            ident = item.get("id")
            if provider == "gemini" and isinstance(ident,str):
                ident = ident.removeprefix("models/")
            if not isinstance(ident, str) or not ident or ident in seen:
                continue
            seen.add(ident)
            outputs = item.get("output_modalities")
            non_text = (outputs and "text" not in outputs) or any(w in ident.lower() for w in ("embedding", "tts-", "whisper", "dall-e", "gpt-image", "moderation", "sora", "realtime", "audio"))
            if provider == "gemini":
                non_text = non_text or any(w in ident.lower() for w in ("image", "tts", "veo", "lyria", "robotics"))
            models.append({"id": ident, "name": item.get("name") or ident, "research_compatible": not bool(non_text)})
        return {"provider": provider, "models": models, "fetched_at": stamp(), "source": "provider_api"}
    except urllib.error.HTTPError as e:
        code = "invalid_key" if e.code == 401 else "model_catalog_http_" + str(e.code)
        raise ServiceError(code, "模型列表读取失败，请检查该服务商密钥及访问权限") from None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise ServiceError("model_catalog_unavailable", "暂时无法读取服务商模型列表，请稍后重试") from None
