"""Protocol-specific endpoints and explicit consent for custom destinations."""
import ipaddress
import socket
from urllib.parse import urlsplit

DEFAULTS = {"deepseek": {"openai":"https://api.deepseek.com", "anthropic":"https://api.deepseek.com/anthropic"},
            "openai":{"openai":"https://api.openai.com/v1"},
            "anthropic":{"anthropic":"https://api.anthropic.com"},
            "gemini":{"openai":"https://generativelanguage.googleapis.com/v1beta/openai"}}


def endpoint_settings(provider, protocol=None, base_url=None):
    from .core import ServiceError
    protocol = protocol or ("anthropic" if provider == "anthropic" else "openai")
    if provider not in DEFAULTS or protocol not in ("openai", "anthropic"):
        raise ServiceError("invalid_protocol", "请选择 OpenAI 或 Anthropic 接口协议")
    if provider == "gemini" and protocol != "openai":
        raise ServiceError("invalid_protocol", "Google Gemini 使用 OpenAI 兼容接口协议")
    base = str(base_url or DEFAULTS[provider].get(protocol) or "").strip().rstrip("/")
    try:
        parsed = urlsplit(base)
        valid = parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment and parsed.port in (None,443)
    except ValueError:
        valid = False
    if not valid or any(c.isspace() for c in base):
        raise ServiceError("invalid_base_url", "base_url 需为有效的 HTTPS 基础地址，不能包含凭证、查询参数或接口末尾路径")
    if parsed.path.endswith(("/messages", "/responses", "/chat/completions", "/models")):
        raise ServiceError("invalid_base_url", "请填写基础地址，不要填写 messages、responses、models 或 chat/completions 接口路径")
    host = parsed.hostname.lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith((".local", ".localhost")):
        raise ServiceError("invalid_base_url", "不允许将模型密钥发送到本机控制接口")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ServiceError("invalid_base_url", "模型地址不能指向私有或保留网络")
    except ValueError:
        pass
    official_host = urlsplit(next(iter(DEFAULTS[provider].values()))).hostname
    custom = host != official_host
    if not custom:
        paths = {urlsplit(u).path for u in DEFAULTS[provider].values()}
        if provider != "gemini":
            paths |= {p+"/v1" for p in paths if not p.endswith("/v1")}
        if parsed.path not in paths or protocol not in DEFAULTS[provider]:
            raise ServiceError("invalid_base_url", "该服务商官方地址与所选协议不匹配")
        if provider == "deepseek" and (("/anthropic" in parsed.path) != (protocol == "anthropic")):
            raise ServiceError("invalid_base_url", "DeepSeek 的 Anthropic 协议请使用 /anthropic 基础地址")
    return {"api_protocol":protocol,"base_url":base,"custom_base_url":custom}


def api_url(settings, resource):
    base = settings["base_url"]
    protocol = settings["api_protocol"]
    if resource == "models" and settings.get("provider") == "deepseek" and not settings.get("custom_base_url"):
        return "https://api.deepseek.com/models"  # Both protocols share the provider's catalogue.
    if protocol == "anthropic":
        return base + ("" if base.endswith("/v1") else "/v1") + "/" + ("models" if resource=="models" else "messages")
    suffix = "responses" if settings.get("provider") == "openai" else "chat/completions"
    return base + "/" + ("models" if resource == "models" else suffix)


def check_custom_destination(settings):
    from .core import ServiceError
    if not settings.get("custom_base_url"):
        return
    if not settings.get("base_url_confirmed"):
        raise ServiceError("custom_destination_confirmation", "请先确认将本项密钥与公开财报发送至此自定义地址")
    try:
        addresses = socket.getaddrinfo(urlsplit(settings["base_url"]).hostname,443,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ServiceError("invalid_base_url", "模型域名不能解析到私有或保留网络")
    except socket.gaierror:
        raise ServiceError("model_network", "模型基础地址暂时无法解析") from None
