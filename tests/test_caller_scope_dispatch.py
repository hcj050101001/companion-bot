"""invoke() 宿主挂钩分发契约测试（fix1 复审 P3 潜伏项）。

source_guard 与 caller_scope 必须累积透传、不互斥：任一组合下
都不许静默丢弃其一。source_guarded 在注册层仅许 web_search 名，
故夹具以该名构造合成工具（进程内、无网络）。
"""

import asyncio
import json

from companion.tools.read_only import ReadOnlyRegistry, ReadOnlyTool

SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "maxLength": 30}},
    "required": ["query"],
    "additionalProperties": False,
}


def _registry(tool):
    instance = ReadOnlyRegistry(
        enabled=True, policies={"web_search": {"enabled": True, "rate_limit": 10}}
    )
    instance.register(tool)
    return instance


def _invoke(instance, identity):
    return asyncio.run(
        instance.invoke(
            "web_search", '{"query": "hello"}', identity, user_message="hello"
        )
    )


def _scope(identity):
    return "owner" if identity == "10001" else "public"


def _recording_tool(seen, **kwargs):
    async def action(query, **host):
        seen.append(host)
        return "ok:" + query

    return ReadOnlyTool("web_search", "synthetic", SCHEMA, action, **kwargs)


def test_source_guarded_and_caller_scope_compose():
    seen = []
    reg = _registry(
        _recording_tool(seen, source_guarded=True, caller_scope=_scope)
    )
    result = _invoke(reg, "10001")
    assert json.loads(result)["data"] == "ok:hello"
    (host,) = seen
    # 两挂钩同时到达动作：scope 按身份算出，guard 保持既有语义。
    assert host["caller_scope"] == "owner"
    assert callable(host["source_guard"])
    assert host["source_guard"]("no identity here") is True
    assert host["source_guard"]("from 10001 directly") is False


def test_source_guarded_without_caller_scope_unchanged():
    seen = []
    reg = _registry(_recording_tool(seen, source_guarded=True))
    result = _invoke(reg, "10001")
    assert json.loads(result)["data"] == "ok:hello"
    (host,) = seen
    assert callable(host["source_guard"])
    assert "caller_scope" not in host


def test_caller_scope_without_source_guard():
    seen = []
    reg = _registry(_recording_tool(seen, caller_scope=_scope))
    result = _invoke(reg, "10002")
    assert json.loads(result)["data"] == "ok:hello"
    (host,) = seen
    assert host == {"caller_scope": "public"}
