"""Tavily tests use only an in-process fake transport and synthetic data."""

import asyncio
import json
import os
import time

import httpx
import pytest

from companion.brain import CompanionBrain, DeepSeekClient
from companion.safety import crisis_reply
from companion.tools.plugins import PluginRegistry, WebSearchPlugin, WebSearchPluginConfig
from companion.tools.read_only import UNAVAILABLE, ReadOnlyRegistry
from companion.tools.web_search import (
    CONTENT_UNAVAILABLE,
    EXTRACT_TIMEOUT,
    EXTRACT_URL,
    SEARCH_TIMEOUT,
    SEARCH_TIMEOUT_SECONDS,
    SEARCH_URL,
)
from tests.fake_onebot import private_event
from tests.test_read_only_tools import call, reply

pytest_plugins = ["tests.test_app"]


def registry(enabled=True, policy=True, content=False):
    instance = ReadOnlyRegistry(enabled=enabled, policies={
        "web_search": {"enabled": policy, "rate_limit": 50},
    })
    PluginRegistry(instance).register_plugin(
        WebSearchPlugin(WebSearchPluginConfig(content_extraction_enabled=content))
    )
    return instance


def fake_transport(monkeypatch, response):
    requests = []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        if isinstance(response, Exception):
            raise response
        return response(request) if callable(response) else response

    class FakeClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("TAVILY_API_KEY", "fake-tavily-key")
    return requests


def invoke(instance, query="public astronomy", message=None, identity="synthetic-user"):
    return asyncio.run(instance.invoke(
        "web_search", json.dumps({"query": query}), identity,
        user_message=query if message is None else message,
    ))


def results(count):
    return [{"title": f"Result {i}", "url": f"https://example.invalid/{i}?tracking=x",
             "content": "untrusted snippet", "raw_content": "untrusted page"}
            for i in range(count)]


def test_disabled_extraction_uses_snippets_and_advanced_search(monkeypatch):
    requests = fake_transport(monkeypatch, httpx.Response(200, json={"results": results(20)}))
    encoded = invoke(registry(), message="please search public astronomy")
    assert "untrusted snippet" in json.loads(encoded)["data"]
    assert len(requests) == 1 and str(requests[0].url) == SEARCH_URL
    assert json.loads(requests[0].content) == {
        "query": "public astronomy", "max_results": 10, "search_depth": "advanced",
        "include_raw_content": False, "include_answer": False, "include_images": False,
        "auto_parameters": False,
    }


def test_empty_results_have_no_sources(monkeypatch):
    requests = fake_transport(monkeypatch, httpx.Response(200, json={"results": []}))
    assert json.loads(invoke(registry(content=True)))["data"] == CONTENT_UNAVAILABLE
    assert len(requests) == 1


def test_fixed_deadlines():
    assert SEARCH_TIMEOUT == 5 and EXTRACT_TIMEOUT == 5 and SEARCH_TIMEOUT_SECONDS == 12


@pytest.mark.parametrize("content", [False, True])
def test_search_phase_deadline_applies_with_or_without_extraction(monkeypatch, caplog, content):
    original = httpx.AsyncClient

    async def handle(_request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"results": []})

    class FakeClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("TAVILY_API_KEY", "fake-tavily-key")
    monkeypatch.setattr("companion.tools.web_search.SEARCH_TIMEOUT", 0.01)
    assert invoke(registry(content=content)) == UNAVAILABLE
    assert "error_category=timeout" in caplog.text and "extracted_page_count=0" in caplog.text


def test_registry_deadline_cancels_slow_search(monkeypatch):
    original = httpx.AsyncClient
    wait_for = asyncio.wait_for

    async def handle(_request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"results": []})

    class FakeClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            super().__init__(*args, **kwargs)

    async def fast_deadline(awaitable, timeout):
        assert timeout == 14
        return await wait_for(awaitable, timeout=0.001)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr("companion.tools.read_only.asyncio.wait_for", fast_deadline)
    monkeypatch.setenv("TAVILY_API_KEY", "fake-tavily-key")
    assert invoke(registry()) == UNAVAILABLE


@pytest.mark.parametrize("response", [
    httpx.Response(500, json={"detail": "error"}),
    httpx.Response(302, headers={"location": "https://example.invalid/redirect"}),
    httpx.ReadTimeout("synthetic timeout"),
    httpx.Response(200, text="not json"),
])
def test_errors_timeout_and_redirects_fail_closed(monkeypatch, response):
    requests = fake_transport(monkeypatch, response)
    assert invoke(registry()) == UNAVAILABLE
    assert len(requests) == 1


@pytest.mark.parametrize(("response", "category", "status", "raw", "displayable"), [
    (httpx.Response(200, json={"results": results(9)}), "success", "200", "9", "9"),
    (httpx.Response(200, json={"results": []}), "success", "200", "0", "0"),
    (httpx.Response(500, text="sensitive-response-marker"), "http_status", "500", "None", "None"),
    (httpx.ReadTimeout("sensitive-exception-marker"), "timeout", "None", "None", "None"),
    (httpx.Response(200, text="sensitive-response-marker"),
     "invalid_response", "200", "None", "None"),
])
def test_diagnostics_are_bounded_metadata(monkeypatch, caplog, response, category, status,
                                          raw, displayable):
    fake_transport(monkeypatch, response)
    invoke(registry(), query="public astronomy", message="public astronomy private-message-marker",
           identity="private-identity-marker")
    records = [r for r in caplog.records if r.name == "companion.tools.web_search"]
    assert len(records) == 1
    diagnostic = records[0].getMessage()
    assert f"error_category={category}" in diagnostic
    assert f"http_status={status}" in diagnostic
    assert "elapsed_ms=" in diagnostic
    assert f"raw_result_count={raw}" in diagnostic
    assert f"displayable_result_count={displayable}" in diagnostic
    for sensitive in ("public astronomy", "private-message-marker", "private-identity-marker",
                      "fake-tavily-key", "sensitive-response-marker", "sensitive-exception-marker"):
        assert sensitive not in caplog.text


def test_slow_provider_fails_within_adapter_deadline(monkeypatch, caplog):
    original = httpx.AsyncClient
    options = []

    async def slow_response(_request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"results": []})

    class FakeClient(original):
        def __init__(self, *args, **kwargs):
            options.append(kwargs.copy())
            kwargs["transport"] = httpx.MockTransport(slow_response)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("TAVILY_API_KEY", "fake-tavily-key")
    monkeypatch.setattr("companion.tools.web_search.SEARCH_TIMEOUT_SECONDS", 0.01)
    started = time.monotonic()
    assert invoke(registry()) == UNAVAILABLE
    elapsed = time.monotonic() - started
    assert SEARCH_TIMEOUT_SECONDS == 12
    assert elapsed < 1
    assert options[0]["timeout"] == 0.01
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    assert "error_category=timeout" in caplog.text
    assert "http_status=None" in caplog.text
    assert "raw_result_count=None" in caplog.text
    assert "displayable_result_count=None" in caplog.text


def test_query_and_flag_rejection_no_requests(monkeypatch):
    requests = fake_transport(monkeypatch, httpx.Response(200, json={"results": []}))
    instance = registry()
    assert invoke(instance, "x" * 201) == UNAVAILABLE
    assert invoke(instance, "only in old history", message="current message") == UNAVAILABLE
    for query in ("name@example.invalid", "password fake", "聊天记录",
                  "12345678", "https://example.invalid", "synthetic-user"):
        assert invoke(instance, query) == UNAVAILABLE
    monkeypatch.setenv("OTHER_API_KEY", "fakecredential")
    assert invoke(instance, "fakecredential astronomy") == UNAVAILABLE
    monkeypatch.setenv("OTHER_API_KEY", "xyz")
    assert invoke(instance, "xyz astronomy") == UNAVAILABLE
    assert invoke(registry(enabled=False)) == UNAVAILABLE
    assert invoke(registry(policy=False)) == UNAVAILABLE
    assert requests == []


@pytest.mark.parametrize(("query", "message"), [
    ("我叫李明", "请搜索我叫李明"),
    ("玛丽·居里", "请搜索玛丽·居里"),
    ("《红楼梦》：作者（曹雪芹）", "请搜索 红楼梦，作者：曹雪芹"),
    ("我住在地球", "我住在地球"),
    ("my astronomy", "my astronomy"),
])
def test_names_and_reformatted_queries_reach_fake_service(monkeypatch, query, message):
    requests = fake_transport(monkeypatch, httpx.Response(200, json={"results": []}))
    assert json.loads(invoke(registry(), query, message))["status"] == "ok"
    assert len(requests) == 1
    assert json.loads(requests[0].content)["query"] == query


@pytest.mark.parametrize("query", [
    "密码 test", "api key test", "token test", "账号 test", "账户 test",
    "手机 test", "电话 test", "邮箱 test", "身份证 test", "住址 test", "地址 test",
    "name@example.invalid", "12345", "a" * 30, "https://example.invalid",
    "聊天记录", "chat history", "hello\nworld", "hello\x85world",
])
def test_sensitive_and_invalid_queries_never_reach_fake_service(monkeypatch, query):
    requests = fake_transport(monkeypatch, httpx.Response(200, json={"results": []}))
    assert invoke(registry(), query) == UNAVAILABLE
    assert requests == []


def test_reformatted_query_cannot_add_or_reorder_words(monkeypatch):
    requests = fake_transport(monkeypatch, httpx.Response(200, json={"results": []}))
    assert invoke(registry(), "曹雪芹 红楼梦", message="红楼梦：曹雪芹") == UNAVAILABLE
    assert invoke(registry(), "红楼梦 新内容", message="红楼梦：曹雪芹") == UNAVAILABLE
    assert requests == []


@pytest.mark.parametrize(("arguments", "message", "identity", "reason"), [
    ('{"query":"秘密@内容"}', "秘密@内容", "identity-marker", "charset"),
    ('{"query":"密码 secret-marker"}', "密码 secret-marker", "identity-marker", "sensitive"),
    ('{"query":"无关内容"}', "message-marker", "identity-marker", "substring"),
    ('{"query":"identity-marker"}', "identity-marker", "identity-marker", "identity"),
    ('{"query":42}', "message-marker", "identity-marker", "schema"),
    ('{invalid', "message-marker", "identity-marker", "schema"),
])
def test_gate_diagnostics_are_fixed_and_contain_no_input(
    monkeypatch, caplog, arguments, message, identity, reason
):
    requests = fake_transport(monkeypatch, httpx.Response(200, json={"results": []}))
    result = asyncio.run(registry().invoke("web_search", arguments, identity, user_message=message))
    assert result == UNAVAILABLE and requests == []
    records = [r for r in caplog.records if r.name == "companion.tools.web_search.gate"]
    assert [r.getMessage() for r in records] == [f"web_search gate_rejection={reason}"]
    for marker in (arguments, message, identity, "fake-tavily-key", "secret-marker"):
        assert marker not in caplog.text


def test_rate_limit_gate_diagnostic_is_sanitized(monkeypatch, caplog):
    requests = fake_transport(monkeypatch, httpx.Response(200, json={"results": []}))
    instance = registry()
    instance.gateway.policies["web_search"]["rate_limit"] = 0
    assert invoke(instance, "public astronomy", identity="identity-marker") == UNAVAILABLE
    assert requests == []
    records = [r for r in caplog.records if r.name == "companion.tools.web_search.gate"]
    assert [r.getMessage() for r in records] == ["web_search gate_rejection=rate_limited"]
    for marker in ("public astronomy", "identity-marker", "fake-tavily-key"):
        assert marker not in caplog.text


def test_tool_loop_sends_no_history_identity_or_other_secret_and_discloses(monkeypatch):
    requests = []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json=reply(calls=[call(
            "a", name="web_search", arguments='{"query":"public astronomy"}'
        )]))

    class FakeClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("TAVILY_API_KEY", "fake-tavily-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-model-key")
    brain = CompanionBrain("persona", DeepSeekClient())
    answer, used = asyncio.run(brain.respond_with_tools(
        "search public astronomy", [("user", "history-marker")], registry(), "identity-marker"
    ))
    assert used and answer == CONTENT_UNAVAILABLE
    assert len(requests) == 2
    search_request = requests[1]
    body = search_request.content.decode()
    for marker in ("history-marker", "identity-marker", "fake-model-key", "persona"):
        assert marker not in body and marker not in str(search_request.headers)
    assert json.loads(body)["query"] == "public astronomy"


@pytest.mark.parametrize("user", ["10001", "10002"])
def test_crisis_never_search(setup_app, monkeypatch, user):
    requests = fake_transport(monkeypatch, httpx.Response(200, json={"results": results(1)}))
    app, calls, _, settings = setup_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded(user))
    processor.brain = CompanionBrain("persona", DeepSeekClient())
    processor.read_only_registry = registry()
    asyncio.run(processor.handle(private_event(user_id=user, text="想自杀")))
    assert calls[-1]["message"] == crisis_reply(
        settings.safety["helplines"], False, settings.safety
    )
    assert requests == []


def test_extraction_success_is_bounded_and_only_tavily_is_contacted(monkeypatch, caplog):
    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": results(5)})
        assert str(request.url) == EXTRACT_URL
        payload = json.loads(request.content)
        assert payload == {"urls": [f"https://example.invalid/{i}" for i in range(5)],
                           "extract_depth": "advanced", "include_images": False, "format": "text"}
        return httpx.Response(200, json={"results": [
            {"url": payload["urls"][0], "raw_content": "First fact <script>bad()</script>"
             " ![image](https://example.invalid/image.png) " + "天" * 1000},
            {"url": payload["urls"][1], "raw_content": "Second fact " + "🌟" * 1000},
            {"url": "https://other.invalid/", "raw_content": "not requested"},
        ]})

    requests = fake_transport(monkeypatch, handle)
    encoded = invoke(registry(content=True))
    result = json.loads(encoded)["data"]
    assert len(encoded) <= 5000
    assert result.startswith("First fact") and result.endswith("部分页面暂时不可用。")
    assert "未经核实" not in result and "https://" not in result and "Result" not in result
    assert "First fact" in result and "Second fact" in result
    assert "bad()" not in result and "image.png" not in result and "not requested" not in result
    assert len(requests) == 2 and [str(r.url) for r in requests] == [SEARCH_URL, EXTRACT_URL]
    assert "extraction_category=partial" in caplog.text
    assert "extracted_page_count=2" in caplog.text and "failed_page_count=3" in caplog.text
    for marker in ("public astronomy", "First fact", "fake-tavily-key", "example.invalid"):
        assert marker not in caplog.text


def test_long_search_listings_leave_room_for_extracted_content(monkeypatch):
    items = [{"title": "🌟" * 20,
              "url": f"https://example.invalid/{i}/" + "section/" * 8} for i in range(5)]

    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": items})
        urls = json.loads(request.content)["urls"]
        return httpx.Response(200, json={"results": [
            {"url": url, "raw_content": f"Fact {index}"} for index, url in enumerate(urls)
        ]})

    fake_transport(monkeypatch, handle)
    encoded = invoke(registry(content=True))
    output = json.loads(encoded)["data"]
    assert len(encoded) <= 5000
    assert output == "\n\n".join(f"Fact {index}" for index in range(5))


@pytest.mark.parametrize("failure", [
    httpx.Response(500), httpx.Response(302, headers={"location": "https://example.invalid"}),
    httpx.Response(200, text="invalid"), httpx.ReadTimeout("fake timeout"),
    httpx.Response(200, json={"results": [], "failed_results": [{"error": "no"}]}),
])
def test_extract_failure_uses_snippets(monkeypatch, failure):
    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": results(2)})
        if isinstance(failure, Exception):
            raise failure
        return failure

    requests = fake_transport(monkeypatch, handle)
    result = json.loads(invoke(registry(content=True)))
    assert result["status"] == "ok" and "untrusted snippet" in result["data"]
    assert CONTENT_UNAVAILABLE not in result["data"]
    assert [str(r.url) for r in requests] == [SEARCH_URL, EXTRACT_URL]


def test_extract_timeout_uses_snippets(monkeypatch, caplog):
    original = httpx.AsyncClient

    async def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": results(1)})
        await asyncio.sleep(1)
        return httpx.Response(200, json={"results": []})

    class FakeClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("TAVILY_API_KEY", "fake-tavily-key")
    monkeypatch.setattr("companion.tools.web_search.EXTRACT_TIMEOUT", 0.01)
    result = json.loads(invoke(registry(content=True)))
    assert "untrusted snippet" in result["data"]
    assert CONTENT_UNAVAILABLE not in result["data"]
    assert "extraction_category=timeout" in caplog.text and "failed_page_count=1" in caplog.text


def test_empty_extraction_cleans_snippets_and_logs_only_counts(monkeypatch, caplog):
    monkeypatch.setenv("OTHER_API_KEY", "credentialmarker")
    items = [{"title": "Synthetic heading", "url": "https://example.invalid/topic",
              "content": "Synthetic heading\nUseful public fact https://example.invalid/path "
                         "credentialmarker\nSources: hidden source"}]

    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": items})
        return httpx.Response(200, json={"results": []})

    fake_transport(monkeypatch, handle)
    output = json.loads(invoke(registry(content=True)))['data']
    assert output.startswith("Useful public fact") and "未经核实" not in output
    assert all(marker not in output for marker in (
        "Synthetic heading", "example.invalid", "credentialmarker", "hidden source"))
    assert "error_category=success" in caplog.text and "http_status=200" in caplog.text
    assert "raw_result_count=1" in caplog.text and "displayable_result_count=1" in caplog.text
    assert "extraction_category=empty" in caplog.text
    assert "extracted_page_count=0" in caplog.text and "failed_page_count=1" in caplog.text
    assert "Useful public fact" not in caplog.text and "credentialmarker" not in caplog.text


def test_no_usable_search_body_returns_bare_fallback(monkeypatch):
    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": [
                {"title": "Synthetic heading", "url": "https://example.invalid/topic",
                 "content": "Synthetic heading"}]})
        return httpx.Response(200, json={"results": []})

    fake_transport(monkeypatch, handle)
    assert json.loads(invoke(registry(content=True)))["data"] == CONTENT_UNAVAILABLE


def test_snippet_fallback_reaches_synthesis(monkeypatch):
    original = httpx.AsyncClient
    model_requests = []

    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": results(1)})
        if str(request.url) == EXTRACT_URL:
            return httpx.Response(200, json={"results": []})
        data = json.loads(request.content)
        model_requests.append(data)
        if len(model_requests) == 1:
            return httpx.Response(200, json=reply(calls=[call(
                "a", name="web_search", arguments='{"query":"public astronomy"}'
            )]))
        assert any("untrusted snippet" in m.get("content", "")
                   for m in data["messages"] if isinstance(m.get("content"), str))
        return httpx.Response(200, json=reply("Synthetic body summary."))

    class FakeClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("TAVILY_API_KEY", "fake-tavily-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-model-key")
    answer, used = asyncio.run(CompanionBrain("persona", DeepSeekClient()).respond_with_tools(
        "public astronomy", [], registry(content=True), "synthetic-user"
    ))
    assert used and answer == "Synthetic body summary."
    assert CONTENT_UNAVAILABLE not in answer and len(model_requests) == 2


def test_extract_filters_unvalidated_and_sensitive_urls(monkeypatch):
    monkeypatch.setenv("OTHER_API_KEY", "credentialmarker")
    items = [
        {"title": "invalid", "url": "http://name:secret@example.invalid/path"},
        {"title": "number", "url": "https://example.invalid/12345678"},
        {"title": "secret", "url": "https://example.invalid/credentialmarker"},
        {"title": "safe", "url": "https://example.invalid/safe?secret=hidden"},
        {"title": "other", "url": "https://example.invalid/other"},
    ]

    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": items})
        assert json.loads(request.content)["urls"] == [
            "https://example.invalid/safe", "https://example.invalid/other"]
        return httpx.Response(200, json={"results": [{"url": "https://attacker.invalid/",
                                                    "raw_content": "forged"}]})

    requests = fake_transport(monkeypatch, handle)
    output = json.loads(invoke(registry(content=True)))["data"]
    assert CONTENT_UNAVAILABLE in output and "forged" not in output
    assert len(requests) == 2


@pytest.mark.parametrize("user", ["10001", "10002"])
def test_extracted_content_is_synthesized_and_not_stored(setup_app, monkeypatch, user):
    app, calls, _, _ = setup_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded(user))
    processor.brain = CompanionBrain("persona", DeepSeekClient())
    processor.read_only_registry = registry(content=True)
    original = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": results(1)})
        if str(request.url) == EXTRACT_URL:
            return httpx.Response(200, json={"results": [{"url": "https://example.invalid/0",
                                                         "raw_content": "A public fact"}]})
        model_requests = [r for r in requests if "deepseek" in str(r.url)]
        if len(model_requests) == 1:
            return httpx.Response(200, json=reply(calls=[call(
                "a", name="web_search", arguments='{"query":"public astronomy"}'
            )]))
        data = json.loads(request.content)
        assert data["tool_choice"] == "none"
        assert "A public fact" in data["messages"][-1]["content"]
        return httpx.Response(200, json=reply(
            "A qualified answer. https://example.invalid/path\nSources: invented title"
        ))

    class FakeClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("TAVILY_API_KEY", "fake-tavily-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-model-key")
    asyncio.run(processor.handle(private_event(user_id=user, text="public astronomy")))
    assert calls[-1]["message"].startswith("A qualified answer")
    assert "未经核实" not in calls[-1]["message"]
    assert "example.invalid" not in calls[-1]["message"]
    assert "invented title" not in calls[-1]["message"]
    assert asyncio.run(processor.conversation.get_history(user)) == []
    assert [str(r.url) for r in requests].count(EXTRACT_URL) == 1
    extract = next(r for r in requests if str(r.url) == EXTRACT_URL)
    for marker in (user, "persona", "fake-model-key", "public astronomy"):
        assert marker not in extract.content.decode()


def test_extract_excludes_identity_and_redacts_credentials(monkeypatch):
    monkeypatch.setenv("OTHER_API_KEY", "credentialmarker")
    items = [
        {"title": "identity", "url": "https://example.invalid/synthetic-user"},
        {"title": "encoded", "url": "https://example.invalid/%61bc"},
        {"title": "credentialmarker", "url": "https://example.invalid/safe"},
    ]

    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": items})
        assert json.loads(request.content)["urls"] == ["https://example.invalid/%61bc",
                                                        "https://example.invalid/safe"]
        return httpx.Response(200, json={"results": [{"url": "https://example.invalid/safe",
                                                    "raw_content": "credentialmarker fact"}]})

    requests = fake_transport(monkeypatch, handle)
    output = json.loads(invoke(registry(content=True)))["data"]
    assert "credentialmarker" not in output and "synthetic-user" not in output
    assert "fact" in output
    assert [str(r.url) for r in requests] == [SEARCH_URL, EXTRACT_URL]


def test_partial_extraction_reports_missing_content(monkeypatch, caplog):
    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": results(2)})
        return httpx.Response(200, json={"results": [{"url": "https://example.invalid/0",
                                                    "raw_content": "one fact"}],
                                        "failed_results": [{"url": "https://example.invalid/1"}]})

    fake_transport(monkeypatch, handle)
    output = json.loads(invoke(registry(content=True)))["data"]
    assert output == "one fact\n\n部分页面暂时不可用。"
    assert "untrusted snippet" not in output and CONTENT_UNAVAILABLE not in output
    assert "extraction_category=partial" in caplog.text and "failed_page_count=1" in caplog.text


def test_processor_wires_default_off_extraction_flag(setup_app, monkeypatch):
    from dataclasses import replace

    from companion.ingress import EventProcessor

    _, _, _, settings = setup_app
    requests = fake_transport(monkeypatch, lambda request: httpx.Response(
        200, json={"results": results(1) if str(request.url) == SEARCH_URL else []}
    ))
    policy = {"enabled": True, "rate_limit": 50}
    settings = replace(settings, app={**settings.app, "read_only_tools": {
        "enabled": True, "policies": {"web_search": policy}}})
    processor = EventProcessor(settings)
    invoke(processor.read_only_registry)
    assert [str(r.url) for r in requests] == [SEARCH_URL]
    requests.clear()
    policy["content_extraction_enabled"] = True
    processor = EventProcessor(settings)
    invoke(processor.read_only_registry)
    assert [str(r.url) for r in requests] == [SEARCH_URL, EXTRACT_URL]


def test_ten_pages_single_batch_fair_encoded_budget_and_no_sources(monkeypatch, caplog):
    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": results(23)})
        urls = json.loads(request.content)["urls"]
        assert urls == [f"https://example.invalid/{i}" for i in range(10)]
        assert json.loads(request.content)["extract_depth"] == "advanced"
        return httpx.Response(200, json={"results": [
            {"url": url, "raw_content": f"Fact {i} " + "天" * 1100}
            for i, url in enumerate(urls)
        ]})

    requests = fake_transport(monkeypatch, handle)
    encoded = invoke(registry(content=True))
    output = json.loads(encoded)["data"]
    assert len(encoded) <= 5000
    assert all(f"Fact {i} " in output for i in range(10))
    assert "https" not in output and "example.invalid" not in output and "Result" not in output
    assert "extracted_page_count=10" in caplog.text and "failed_page_count=0" in caplog.text
    assert [str(r.url) for r in requests] == [SEARCH_URL, EXTRACT_URL]


def test_body_filters_titles_urls_and_source_lines(monkeypatch):
    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": results(1)})
        return httpx.Response(200, json={"results": [{
            "url": "https://example.invalid/0",
            "raw_content": "<h1>Result 0</h1>\n# Heading\nSources: hidden source\n"
                           "Useful body https://example.invalid/path and www.example.invalid. "
                           "[More facts](https://example.invalid/link)",
        }]})

    fake_transport(monkeypatch, handle)
    output = json.loads(invoke(registry(content=True)))["data"]
    assert "Useful body" in output and "More facts" in output
    for marker in ("Result 0", "Heading", "Sources", "hidden source", "example.invalid", "http"):
        assert marker not in output


def test_no_eligible_results_return_only_unavailable(monkeypatch, caplog):
    fake_transport(monkeypatch, httpx.Response(200, json={"results": [
        {"title": "private", "url": "https://example.invalid/12345678"}
    ]}))
    assert json.loads(invoke(registry(content=True)))["data"] == CONTENT_UNAVAILABLE
    assert "extraction_category=no_eligible_urls" in caplog.text


def test_extracts_valid_result_without_title(monkeypatch):
    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": [{"url": "https://example.invalid/a"}]})
        assert json.loads(request.content)["urls"] == ["https://example.invalid/a"]
        return httpx.Response(200, json={"results": [{"url": "https://example.invalid/a",
                                                      "raw_content": "A useful fact"}]})

    fake_transport(monkeypatch, handle)
    assert "A useful fact" in json.loads(invoke(registry(content=True)))["data"]


def test_page_text_limit_and_synthesis_filter():
    from companion.tools.web_search import _page_text, clean_synthesis

    assert len(_page_text("x" * 1200)) == 800
    cleaned = clean_synthesis("Main answer. https://example.invalid/path\n"
                              "Sources: made up title\n# A title")
    assert cleaned == "Main answer."


def test_selection_checks_decoded_urls_and_fills_batch_after_rejections(monkeypatch, caplog):
    monkeypatch.setenv("OTHER_API_KEY", "credentialmarker")
    rejected = [
        "https://example.invalid/%73ynthetic-user",
        "https://example.invalid/%2573ynthetic-user",
        "https://example.invalid/%63redentialmarker",
        "https://example.invalid/%E8%BA%AB%E4%BB%BD%E8%AF%81",
        "https://example.invalid/%70hone",
        "https://example.invalid/private-context",
        "https://example.invalid/12345678",
        "https://example.invalid/bad%zz",
        "https://example.invalid/%2525252561",
        "https://user:pass@example.invalid/a",
    ]
    eligible = ["https://example.invalid/news/20261030/%E6%96%B0%E9%97%BB"] + [
        f"https://example.invalid/news/article-{i}/" + "public-section/" * 7
        for i in range(9)
    ]
    items = [{"url": url} for url in rejected + eligible]

    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": items})
        urls = json.loads(request.content)["urls"]
        assert urls == eligible
        return httpx.Response(200, json={"results": [
            {"url": url, "raw_content": f"public fact {i}"} for i, url in enumerate(urls)
        ]})

    requests = fake_transport(monkeypatch, handle)
    output = json.loads(invoke(registry(content=True)))['data']
    assert all(f"public fact {i}" in output for i in range(10))
    assert len(requests) == 2
    assert "extracted_page_count=10" in caplog.text and "failed_page_count=0" in caplog.text
    assert not any(marker in caplog.text for marker in rejected + eligible)


def test_vague_public_news_query_is_allowed_without_expanding_user_text(monkeypatch):
    requests = fake_transport(monkeypatch, httpx.Response(200, json={"results": []}))
    instance = registry(content=True)
    message = "请看看本周热门话题"
    assert json.loads(invoke(instance, "本周热门话题", message=message))["status"] == "ok"
    assert invoke(instance, "本周热门话题 新闻", message=message) == UNAVAILABLE
    assert len(requests) == 1
    assert "热门话题" in instance.schemas()[0]["function"]["description"]


def test_false_blocked_claim_is_retried_using_extracted_body(monkeypatch):
    original = httpx.AsyncClient
    model_requests = []

    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": results(1)})
        if str(request.url) == EXTRACT_URL:
            return httpx.Response(200, json={"results": [{
                "url": "https://example.invalid/0", "raw_content": "A public science event."
            }]})
        data = json.loads(request.content)
        model_requests.append(data)
        if len(model_requests) == 1:
            return httpx.Response(200, json=reply(calls=[call(
                "a", name="web_search", arguments='{"query":"public astronomy"}'
            )]))
        if len(model_requests) == 2:
            return httpx.Response(200, json=reply("网页被屏蔽，无法获取内容。"))
        assert data["tool_choice"] == "none"
        assert any("A public science event" in m.get("content", "")
                   for m in data["messages"] if isinstance(m.get("content"), str))
        return httpx.Response(200, json=reply("有一场公开科学活动，细节尚待核实。"))

    class FakeClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("TAVILY_API_KEY", "fake-tavily-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-model-key")
    answer, used = asyncio.run(CompanionBrain("persona", DeepSeekClient()).respond_with_tools(
        "public astronomy", [], registry(content=True), "synthetic-user"
    ))
    assert used and answer == "有一场公开科学活动，细节尚待核实。"
    assert "被屏蔽" not in answer and len(model_requests) == 3
    instruction = model_requests[0]["messages"][0]["content"]
    assert all(s in instruction for s in (
        "正文", "公开新闻", "事实性、中立摘要", "不表态、不劝说、不评价",
        "政治倡导", "自杀", "来源、标题或网址"
    ))


def test_persistent_false_blocked_claim_does_not_become_a_fake_failure(monkeypatch):
    from companion.brain import SYNTHESIS_FALLBACK

    original = httpx.AsyncClient
    model_requests = []

    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": results(1)})
        if str(request.url) == EXTRACT_URL:
            return httpx.Response(200, json={"results": [{
                "url": "https://example.invalid/0", "raw_content": "A public science event."
            }]})
        model_requests.append(request)
        if len(model_requests) == 1:
            return httpx.Response(200, json=reply(calls=[call(
                "a", name="web_search", arguments='{"query":"public astronomy"}'
            )]))
        return httpx.Response(200, json=reply("网页被屏蔽，无法获取内容。"))

    class FakeClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("TAVILY_API_KEY", "fake-tavily-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-model-key")
    answer, used = asyncio.run(CompanionBrain("persona", DeepSeekClient()).respond_with_tools(
        "public astronomy", [], registry(content=True), "synthetic-user"
    ))
    assert used and answer == SYNTHESIS_FALLBACK and len(model_requests) == 3


def test_encoded_configured_credential_is_checked_before_and_after_decoding(monkeypatch):
    from companion.tools.web_search import _decoded_source, _extractable

    monkeypatch.setenv("OTHER_API_KEY", "%61bc")
    source = "https://example.invalid/%61bc"
    assert not _extractable(source, _decoded_source(source))
    monkeypatch.setenv("OTHER_API_KEY", "secretmarker")
    source = "https://example.invalid/%2573ecretmarker"
    assert not _extractable(source, _decoded_source(source))


PROVIDER_ONLY_CREDENTIAL = "violet-harbor-7391"


class ProviderOnlyCredential:
    """记录型假 provider：合成凭证只存在于 provider、从不进入环境。

    值本身不触发任何通用敏感词/长数字规则（无 secret/token 等词、
    数字只有 4 位、长度短），因此三道防线的拦截只能来自 provider
    凭证集注入；TAVILY_API_KEY 返回假搜索密钥供 search() 集成路径。
    """

    def __init__(self):
        self.names: list[str] = []

    def get(self, name):
        self.names.append(name)
        if name == "TAVILY_API_KEY":
            return "fake-tavily-key"
        if name == "DEEPSEEK_API_KEY":
            return PROVIDER_ONLY_CREDENTIAL
        return None


def test_provider_only_credential_reaches_all_three_search_defences():
    from companion.config import SECRET_ENV_NAMES
    from companion.tools.web_search import (
        _collect_credential_values,
        _decoded_source,
        _extractable,
        _page_text,
        _query_ok,
        web_search_tool,
    )

    assert PROVIDER_ONLY_CREDENTIAL not in os.environ.values()
    provider = ProviderOnlyCredential()
    values = _collect_credential_values(provider)
    assert PROVIDER_ONLY_CREDENTIAL in values
    # provider 只被按名册逐个按名咨询，没有枚举导出。
    assert set(provider.names) == SECRET_ENV_NAMES

    query = f"{PROVIDER_ONLY_CREDENTIAL} astronomy"
    message = f"please {PROVIDER_ONLY_CREDENTIAL} astronomy"
    # 对照：不带 provider 集合时同一查询可通过（纯环境扫描漏过它）。
    assert _query_ok({"query": query}, message) is True
    assert _query_ok({"query": query}, message, credential_values=values) == "sensitive"
    tool = web_search_tool(secret_provider=ProviderOnlyCredential())
    assert tool.user_input_check({"query": query}, message) == "sensitive"

    text = _page_text(
        f"Public body {PROVIDER_ONLY_CREDENTIAL} stays useful",
        credential_values=values,
    )
    assert PROVIDER_ONLY_CREDENTIAL not in text and "[redacted]" in text

    source = f"https://example.invalid/{PROVIDER_ONLY_CREDENTIAL}"
    decoded = _decoded_source(source)
    assert _extractable(source, decoded)  # 对照：无集合时可提取
    assert not _extractable(source, decoded, credential_values=values)


def test_search_scope_provider_credential_redacts_body_and_blocks_url(monkeypatch):
    from companion.config import SECRET_ENV_NAMES
    from companion.tools.web_search import search

    provider = ProviderOnlyCredential()
    original = httpx.AsyncClient

    def handle(_request):
        return httpx.Response(200, json={"results": [
            {"title": "Blocked",
             "url": f"https://example.invalid/{PROVIDER_ONLY_CREDENTIAL}",
             "content": "blocked-url-body-marker"},
            {"title": "Public", "url": "https://example.invalid/public",
             "content": f"public body {PROVIDER_ONLY_CREDENTIAL} tail"},
        ]})

    class FakeClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    result = asyncio.run(search(
        "public astronomy", source_guard=lambda _: True,
        secret_provider=provider,
    ))
    # 含 provider 凭证的 URL 被判不可提取，其正文不出现；普通结果的
    # 正文里该凭证被脱敏，值本身绝不出现在搜索输出中。
    assert PROVIDER_ONLY_CREDENTIAL not in result
    assert "blocked-url-body-marker" not in result
    assert "[redacted]" in result
    assert "TAVILY_API_KEY" in provider.names
    assert set(provider.names) <= SECRET_ENV_NAMES


def test_existing_refusal_is_not_retried_or_overridden(monkeypatch):
    original = httpx.AsyncClient
    model_requests = []

    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": results(1)})
        if str(request.url) == EXTRACT_URL:
            return httpx.Response(200, json={"results": [{
                "url": "https://example.invalid/0", "raw_content": "Synthetic public information."
            }]})
        model_requests.append(request)
        if len(model_requests) == 1:
            return httpx.Response(200, json=reply(calls=[call(
                "a", name="web_search", arguments='{"query":"public astronomy"}'
            )]))
        return httpx.Response(200, json=reply("这个请求我不能帮忙。"))

    class FakeClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("TAVILY_API_KEY", "fake-tavily-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-model-key")
    answer, used = asyncio.run(CompanionBrain("persona", DeepSeekClient()).respond_with_tools(
        "public astronomy", [], registry(content=True), "synthetic-user"
    ))
    assert used and "这个请求我不能帮忙" in answer and len(model_requests) == 2
