"""Bounded Tavily search and optional provider-side extraction."""

import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from companion.config import SECRET_ENV_NAMES, EnvironmentSecretProvider
from companion.tools.read_only import WEB_SEARCH_RESULT_CHARS, ReadOnlyTool

SEARCH_URL = "https://api.tavily.com/search"
EXTRACT_URL = "https://api.tavily.com/extract"
PARTIAL_NOTE = "部分页面暂时不可用。"
CONTENT_UNAVAILABLE = "页面内容暂时不可用。"
SEARCH_TIMEOUT = 5
EXTRACT_TIMEOUT = 5
MAX_RESULTS = 10
MAX_PAGE_CHARS = 800
MAX_RESPONSE_BYTES = 256_000
SEARCH_TIMEOUT_SECONDS = 12
logger = logging.getLogger("companion.tools.web_search")
# Reject likely identifiers, credentials, URLs and conversational/private context.
_ALLOWED_QUERY = re.compile(r"[\w\s\-\u3400-\u9fff?？,，.。!！·・《》（）():：、]+\Z")
_SENSITIVE = re.compile(
    r"(?:\d{5,}|[a-z0-9_]{30,}|https?|www|密码|口令|密钥|令牌|账号|账户|"
    r"邮箱|手机|电话|住址|地址|身份证|聊天记录|"
    r"password|secret|token|api.?key|email|phone|address|chat\s*history)", re.I
)
_URL_SENSITIVE = re.compile(
    r"(?:密码|口令|密钥|令牌|账号|账户|邮箱|手机|电话|住址|地址|身份证|"
    r"聊天记录|私人|隐私|password|secret|token|api.?key|email|phone|telephone|"
    r"address|identity|passport|credential|chat.?history|private|conversation|"
    r"personal.?data|contact|messages?|手机号|证件|护照|对话|私信)", re.I
)
_CREDENTIAL_NAME = re.compile(r"(?:SECRET|TOKEN|PASSWORD|API_KEY|PRIVATE_KEY)", re.I)


def _environment_credential_values():
    """现行环境扫描：凭证形态环境变量名对应的非空值（防线原口径）。"""
    return tuple(
        value for name, value in os.environ.items()
        if value and _CREDENTIAL_NAME.search(name)
    )


def _collect_credential_values(secret_provider=None):
    """构建防线用凭证值集合：环境扫描 ∪ 注入 provider 按名取到的值。

    provider 只许按名取值（协议禁止枚举导出）：逐个按
    ``SECRET_ENV_NAMES`` 名册咨询，取值异常/缺失/非字符串一律静默
    跳过，异常原文不进日志、不进集合。无 provider 时集合恒等于
    现行环境扫描，防线行为与此前逐字一致。
    """
    values = list(_environment_credential_values())
    if secret_provider is not None:
        for name in sorted(SECRET_ENV_NAMES):
            try:
                value = secret_provider.get(name)
            except Exception:  # noqa: BLE001 — 静默跳过，见 docstring
                continue
            if isinstance(value, str) and value:
                values.append(value)
    return tuple(dict.fromkeys(values))


def _merged_credential_values(credential_values):
    """防线实际比对集：环境扫描 ∪ 传入集合（传入集通常已含环境值）。"""
    values = list(_environment_credential_values())
    if credential_values:
        values.extend(
            value for value in credential_values
            if isinstance(value, str) and value
        )
    return tuple(dict.fromkeys(values))


def _query_ok(args, message, credential_values=None):
    query = args["query"]
    if not isinstance(message, str) or not query.strip() or query != query.strip():
        return "schema"
    if (not _ALLOWED_QUERY.fullmatch(query)
            or any(unicodedata.category(c) == "Cc" for c in query)):
        return "charset"
    if _SENSITIVE.search(query):
        return "sensitive"
    # Never send configured credential values, including short ones.
    # 比对集 = 环境扫描 ∪ 注入集合：仅存于 SecretProvider、未进环境
    # 的密钥也须拦下（集合由 search()/web_search_tool() 作用域构建
    # 后传入）；无传入时退化为现行纯环境扫描。
    for value in _merged_credential_values(credential_values):
        if value.casefold() in query.casefold():
            return "sensitive"
    # Ignore formatting only: the remaining text must be contiguous in this turn.
    def normalize(text):
        return "".join(c for c in text if not c.isspace()
                       and not unicodedata.category(c).startswith("P"))

    normalized_query = normalize(query)
    if not normalized_query or normalized_query not in normalize(message):
        return "substring"
    return True


def _source(value):
    if not isinstance(value, str) or len(value) > 2048 or not value.isascii():
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"https", "http"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
            return None
        # Strip query and fragment, which may contain tracking or credentials.
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return None


class _PageText(HTMLParser):
    """Discard HTML tags, images and script/style bodies from provider text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _redact_credentials(value, credential_values=None):
    for secret in _merged_credential_values(credential_values):
        value = re.sub(re.escape(secret), "[redacted]", value, flags=re.I)
    return value


def _page_text(value, title="", credential_values=None):
    if not isinstance(value, str):
        return ""
    # Drop headings and link destinations before flattening the body text.
    value = re.sub(r"(?is)<(?:title|h[1-6])\b[^>]*>.*?</(?:title|h[1-6])\s*>", " ", value)
    value = re.sub(r"(?m)^\s*#{1,6}\s+.*$", " ", value)
    value = re.sub(r"(?im)^\s*(?:sources?|references?|来源|参考资料)\s*[:：].*$", " ", value)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    parser = _PageText()
    parser.feed(value)
    text = " ".join(" ".join(parser.parts).split())
    text = re.sub(r"(?i)\b(?:https?://|www\.)[^\s<>]+", "", text)
    text = re.sub(r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?(?:/[^\s<>]*)?", "", text)
    if isinstance(title, str) and title.strip():
        text = re.sub(re.escape(title.strip()), "", text, flags=re.I)
    text = " ".join(text.split())
    return _redact_credentials(text, credential_values)[:MAX_PAGE_CHARS].strip()


def clean_synthesis(value):
    """Remove obvious source references from the untrusted model synthesis."""
    value = re.sub(r"(?im)^\s*(?:#{1,6}\s+|sources?\s*[:：]|references?\s*[:：]|"
                   r"来源\s*[:：]|参考资料\s*[:：]).*$", "", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"(?i)\b(?:https?://|www\.)[^\s<>]+", "", value)
    value = re.sub(r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?(?:/[^\s<>]*)?", "", value)
    return value.strip()


def _decoded_source(source):
    """Decode bounded layers for checks; never send the decoded form to the provider."""
    decoded = source
    for _ in range(4):
        if re.search(r"%(?![0-9a-fA-F]{2})", decoded):
            return None
        try:
            next_value = unquote(decoded, errors="strict")
        except UnicodeDecodeError:
            return None
        if next_value == decoded:
            break
        decoded = next_value
    if "%" in decoded or any(unicodedata.category(c).startswith("C") for c in decoded):
        return None
    decoded = unicodedata.normalize("NFKC", decoded)
    try:
        original, parts = urlsplit(source), urlsplit(decoded)
        if (parts.scheme != original.scheme or parts.netloc != original.netloc
                or parts.query or parts.fragment or "\\" in decoded):
            return None
    except ValueError:
        return None
    return decoded


def _extractable(source, decoded, credential_values=None):
    # Dates are common public-news path components; other long numeric IDs stay blocked.
    def is_date(match):
        try:
            date = datetime.strptime(match.group(), "%Y%m%d")
            return 1900 <= date.year <= 2100
        except ValueError:
            return False

    without_dates = re.sub(r"(?<!\d)\d{8}(?!\d)",
                           lambda m: "date" if is_date(m) else m.group(), decoded)
    if (_URL_SENSITIVE.search(decoded) or re.search(r"\d{5,}|[a-z0-9_]{30,}",
                                                   without_dates, re.I)):
        return False
    return not any(
        any(unicodedata.normalize("NFKC", value).casefold() in candidate.casefold()
            for candidate in (source, decoded))
        for value in _merged_credential_values(credential_values)
    )


async def _post(client, url, key, payload, status_holder=None):
    async with client.stream(
        "POST", url,
        headers={"Authorization": f"Bearer {key}", "Accept-Encoding": "identity"}, json=payload
    ) as response:
        status = response.status_code
        if status_holder is not None:
            status_holder.append(status)
        response.raise_for_status()
        if 300 <= status < 400:
            raise ValueError("redirect refused")
        if "application/json" not in response.headers.get("content-type", "").lower():
            raise ValueError("invalid response type")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ValueError("encoded response refused")
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=8192):
            if len(chunk) > MAX_RESPONSE_BYTES - len(body):
                raise ValueError("response too large")
            body.extend(chunk)
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("invalid response")
    return status, data


def _fit_output(pages, partial):
    # Account for JSON escaping and the registry's complete encoded envelope.
    text = ""
    suffix = "\n\n" + PARTIAL_NOTE if partial else ""
    for index, content in enumerate(pages):
        prefix = "\n\n" if text else ""
        current_size = len(json.dumps({"status": "ok", "data": text + suffix},
                                      ensure_ascii=True, separators=(",", ":")))
        budget = current_size + (WEB_SEARCH_RESULT_CHARS - current_size) // (len(pages) - index)
        low, high = 0, len(content)
        while low < high:
            mid = (low + high + 1) // 2
            encoded = json.dumps({"status": "ok", "data": text + prefix + content[:mid] + suffix},
                                 ensure_ascii=True, separators=(",", ":"))
            if len(encoded) <= budget:
                low = mid
            else:
                high = mid - 1
        if low:
            text += prefix + content[:low]
    return text + suffix


async def search(query, *, content_extraction_enabled=False, source_guard=lambda _: False,
                 secret_provider=None):
    if secret_provider is None:
        secret_provider = EnvironmentSecretProvider()
    started = time.monotonic()
    category = "success"
    extraction_category = "disabled" if not content_extraction_enabled else "not_attempted"
    status_holder = []
    raw_count = displayable_count = None
    extracted_count = failed_count = 0
    try:
        key = (secret_provider.get("TAVILY_API_KEY") or "").strip()
        if not key:
            category = "missing_key"
            raise ValueError("search unavailable")
        # 防线凭证集在本作用域构建一次（环境 ∪ provider 按名取），
        # 供 URL 过滤与正文脱敏共用；provider 异常已在构建时静默跳过。
        credential_values = _collect_credential_values(secret_provider)
        payload = {
            "query": query,
            "max_results": MAX_RESULTS,
            "search_depth": "advanced",
            "include_raw_content": False,
            "include_answer": False,
            "include_images": False,
            "auto_parameters": False,
        }
        async with asyncio.timeout(SEARCH_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                timeout=SEARCH_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
            ) as client:
                async with asyncio.timeout(SEARCH_TIMEOUT):
                    _, data = await _post(client, SEARCH_URL, key, payload, status_holder)
                raw_count = len(data["results"])
                sources = []
                titles = {}
                snippets = []
                displayable_count = 0
                for item in data["results"]:
                    if not isinstance(item, dict):
                        continue
                    source = _source(item.get("url"))
                    title = item.get("title")
                    if not source:
                        continue
                    displayable_count += 1
                    decoded = _decoded_source(source)
                    if (len(sources) < MAX_RESULTS and decoded is not None
                            and _extractable(source, decoded, credential_values)
                            and source_guard(source)
                            and source_guard(decoded)
                            and source not in sources):
                        sources.append(source)
                        titles[source] = title
                        snippet = _page_text(item.get("content"), title,
                                             credential_values)
                        if snippet:
                            snippets.append(snippet)
                if content_extraction_enabled and sources:
                    try:
                        async with asyncio.timeout(EXTRACT_TIMEOUT):
                            _, extracted = await _post(client, EXTRACT_URL, key, {
                                "urls": sources, "extract_depth": "advanced",
                                "include_images": False, "format": "text",
                            })
                        pages = {}
                        for item in extracted["results"][:MAX_RESULTS]:
                            if not isinstance(item, dict) or item.get("url") not in sources:
                                continue
                            if item["url"] in pages:
                                continue
                            content = _page_text(item.get("raw_content"),
                                                 titles[item["url"]],
                                                 credential_values)
                            if content:
                                pages[item["url"]] = content
                        extracted_count = len(pages)
                        failed_count = len(sources) - extracted_count
                        if pages:
                            extraction_category = "success" if not failed_count else "partial"
                            return _fit_output([pages[s] for s in sources if s in pages],
                                               bool(failed_count))
                        extraction_category = "empty"
                    except (TimeoutError, httpx.TimeoutException):
                        extraction_category = "timeout"
                        failed_count = len(sources)
                    except Exception:
                        extraction_category = "failed"
                        failed_count = len(sources)
                elif content_extraction_enabled:
                    extraction_category = "no_eligible_urls"
                return _fit_output(snippets, False) if snippets else CONTENT_UNAVAILABLE
    except (TimeoutError, httpx.TimeoutException):
        category = "timeout"
        raise
    except httpx.HTTPStatusError:
        category = "http_status"
        raise
    except httpx.RequestError:
        category = "network"
        raise
    except asyncio.CancelledError:
        category = "cancelled"
        raise
    except ValueError:
        if category != "missing_key":
            category = "invalid_response"
        raise
    except Exception:
        category = "other"
        raise
    finally:
        logger.warning(
            "web_search error_category=%s http_status=%s elapsed_ms=%d "
            "raw_result_count=%s displayable_result_count=%s extraction_category=%s "
            "extracted_page_count=%d failed_page_count=%d",
            category, status_holder[0] if status_holder else None,
            round((time.monotonic() - started) * 1000),
            raw_count, displayable_count, extraction_category, extracted_count, failed_count,
        )


def web_search_tool(*, content_extraction_enabled=False, secret_provider=None):
    async def action(query, *, source_guard):
        return await search(query, content_extraction_enabled=content_extraction_enabled is True,
                            source_guard=source_guard, secret_provider=secret_provider)

    if secret_provider is None:
        # 无注入 provider 时查询防线与现行逐字一致（纯函数、无咨询）。
        query_check = _query_ok
    else:
        def query_check(args, message):
            # 每次门控时在本作用域按当前 provider 构建凭证集传入，
            # 使仅存于 provider 的密钥也能拦下查询。
            return _query_ok(
                args, message,
                credential_values=_collect_credential_values(secret_provider),
            )

    return ReadOnlyTool(
        "web_search",
        "需要最新公开信息或本周热门话题时应使用搜索，即使请求宽泛；"
        "查询词须直接取自当前消息的连续文字，可去空格和标点，不得添加词语、"
        "个人信息或秘密。遵守既有人设和安全边界。",
        {"type": "object", "properties": {"query": {"type": "string", "minLength": 1,
                                                      "maxLength": 200}},
         "required": ["query"], "additionalProperties": False},
        action,
        query_check,
        source_guarded=True,
    )
