"""Explicit, opt-in registry for model-facing read-only tools.

Adapters must only read data, must not send, mutate users, dispatch tools, or
execute URLs from results. They receive validated arguments only, never identity
or a gateway.
"""

import asyncio
import json
import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Awaitable, Callable

from companion.tools import ToolGateway

UNAVAILABLE = '{"status":"unavailable"}'
MAX_RESULT_CHARS = 2000
WEB_SEARCH_RESULT_CHARS = 5000
MAX_ARGUMENT_CHARS = 8192
# A narrow supported schema vocabulary makes unknown validation rules fail closed.
_SCHEMA_KEYS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "description",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "enum",
}
_RESERVED = {"user_id", "qq", "qq_id", "recipient", "recipient_id", "group_id", "tool"}
_GATE_REASONS = {"charset", "sensitive", "substring", "identity", "schema", "rate_limited"}
_gate_logger = logging.getLogger("companion.tools.web_search.gate")


def _gate_rejection(name, reason):
    if name == "web_search" and reason in _GATE_REASONS:
        _gate_logger.warning("web_search gate_rejection=%s", reason)


def _check_schema(schema, *, root=False):
    if not isinstance(schema, dict) or set(schema) - _SCHEMA_KEYS:
        raise ValueError("unsupported tool schema")
    kind = schema.get("type")
    if kind not in {"object", "string", "integer", "number", "boolean"}:
        raise ValueError("unsupported tool schema type")
    if root and kind != "object":
        raise ValueError("tool arguments must be an object")
    allowed = {"type", "description", "enum"}
    if kind == "object":
        allowed |= {"properties", "required", "additionalProperties"}
        props, required = schema.get("properties"), schema.get("required")
        if (
            not isinstance(props, dict)
            or not isinstance(required, list)
            or not all(isinstance(x, str) for x in required)
            or len(set(required)) != len(required)
            or not set(required) <= set(props)
            or schema.get("additionalProperties") is not False
        ):
            raise ValueError("object schema must be closed and explicit")
        for name, child in props.items():
            if not isinstance(name, str) or name.lower() in _RESERVED:
                raise ValueError("invalid argument name")
            _check_schema(child)
    elif kind == "string":
        allowed |= {"minLength", "maxLength"}
        if (
            type(schema.get("maxLength")) is not int
            or not 0 <= schema["maxLength"] <= 2000
            or type(schema.get("minLength", 0)) is not int
            or not 0 <= schema.get("minLength", 0) <= schema["maxLength"]
        ):
            raise ValueError("strings require bounded length")
    elif kind in {"integer", "number"}:
        allowed |= {"minimum", "maximum"}
        for key in ("minimum", "maximum"):
            val = schema.get(key)
            if type(val) not in (int, float) or not math.isfinite(val):
                raise ValueError("numbers require finite bounds")
        if schema["minimum"] > schema["maximum"]:
            raise ValueError("invalid number bounds")
    if set(schema) - allowed:
        raise ValueError("schema rule does not apply to type")
    if "description" in schema and not isinstance(schema["description"], str):
        raise ValueError("invalid description")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError("invalid enum")


def _valid(value, schema):
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict):
            return False
        props = schema["properties"]
        if not set(value) <= set(props) or not set(schema["required"]) <= set(value):
            return False
        valid = all(_valid(v, props[k]) for k, v in value.items())
    elif kind == "string":
        valid = (
            isinstance(value, str)
            and schema.get("minLength", 0) <= len(value) <= schema["maxLength"]
        )
    elif kind == "integer":
        valid = type(value) is int and schema["minimum"] <= value <= schema["maximum"]
    elif kind == "number":
        valid = (
            type(value) in (int, float)
            and math.isfinite(value)
            and schema["minimum"] <= value <= schema["maximum"]
        )
    else:
        valid = type(value) is bool
    return valid and (
        "enum" not in schema or any(type(value) is type(v) and value == v for v in schema["enum"])
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


@dataclass(frozen=True)
class ReadOnlyTool:
    name: str
    description: str
    parameters: dict
    action: Callable[..., Awaitable[str]]
    user_input_check: Callable[[dict, str], bool | str] | None = None
    source_guarded: bool = False
    # 宿主侧主体门（与 user_input_check 同类）：在 invoke 内以
    # (args, identity) 执行，动作本身仍拿不到身份。用于「同一工具
    # 的不同模式按主体分级」（如列表仅主人可见），不是身份下发。
    caller_check: Callable[[dict, str], bool] | None = None
    # 宿主侧可见性档位（与 source_guard 同类）：invoke 以 identity
    # 调本函数得到一个档位标签（如 "owner"/"public"），只把标签
    # 作为 caller_scope 关键字传给动作——动作按档位选择数据视图
    # （如存储条目仅主人档位可见），仍拿不到身份本身。
    caller_scope: Callable[[str], str] | None = None


@dataclass(frozen=True)
class _PluginInfo:
    """插件经 PluginRegistry 挂载的结果政策（方案 §5.5）。

    网关本身不解释政策；这里只保存宿主分发所需的声明与回调，
    由工具循环在 invoke 之后统一询问 disposition。
    """

    policy: str
    render_direct: Callable[[str], str] | None = None
    terminal_reply: Callable[[str], str | None] | None = None
    unavailable_reply: str | None = None
    max_output_chars: int | None = None
    # 显式声明：ok 结果使循环进入待合成（后续轮次禁用工具、回答走
    # 合成校验）。现行只有 web_search 有此语义，不得由 synthesize
    # 政策自动推导，否则普通工具的第二轮会被误判为合成轮。
    marks_synthesis: bool = False


@dataclass(frozen=True)
class ToolDisposition:
    """一次工具结果的宿主分发裁决：direct/terminal 终结并直出 text，
    feed 则把原始结果回喂模型继续循环。"""

    kind: str  # "direct" | "terminal" | "feed"
    text: str | None = None


def tool_result_status(result) -> str:
    """网关结果信封的唯一状态解析（\"ok\" / \"unavailable\"）。

    disposition 与工具循环共用这一份实现，避免两处独立解析在
    信封格式变化时静默分歧。
    """
    if not isinstance(result, str):
        return "unavailable"
    try:
        parsed = json.loads(result)
    except (ValueError, RecursionError):
        return "unavailable"
    return "ok" if isinstance(parsed, dict) and parsed.get("status") == "ok" else "unavailable"


def _result_is_ok(result) -> bool:
    return tool_result_status(result) == "ok"


class ReadOnlyRegistry:
    """Only explicitly registered and individually enabled read-only tools are exposed.

    Policies are keyed by registered name; each requires enabled: true and may
    set rate_limit and window_seconds. The global enabled flag also defaults off.
    Adapters are trusted code with a read-only contract; this is not a sandbox.
    """

    def __init__(self, *, enabled=False, policies=None):
        self.enabled = enabled is True
        self.gateway = ToolGateway(policies or {})
        self._tools = {}
        self._plugin_info: dict[str, _PluginInfo] = {}
        self._semaphore = asyncio.Semaphore(1)

    def attach_plugin(self, name, *, policy, render_direct=None, terminal_reply=None,
                      unavailable_reply=None, max_output_chars=None,
                      marks_synthesis=False):
        """挂载已登记工具的插件结果政策（只由 PluginRegistry 调用）。"""
        if name not in self._tools:
            raise ValueError("plugin info requires a registered tool")
        if policy not in {"direct", "synthesize"}:
            raise ValueError("unknown result policy")
        if policy == "direct" and not callable(render_direct):
            raise ValueError("direct policy requires render_direct")
        if name in self._plugin_info:
            raise ValueError("plugin info already attached")
        self._plugin_info[name] = _PluginInfo(
            policy=policy, render_direct=render_direct,
            terminal_reply=terminal_reply, unavailable_reply=unavailable_reply,
            max_output_chars=max_output_chars,
            marks_synthesis=marks_synthesis is True,
        )

    def result_policy(self, name):
        """工具的插件结果政策；未经插件登记的工具返回 None（按 feed 处理）。"""
        if not isinstance(name, str):
            return None
        info = self._plugin_info.get(name)
        return info.policy if info else None

    def marks_synthesis(self, name):
        """该工具的 ok 结果是否使循环进入待合成（插件显式声明）。"""
        if not isinstance(name, str):
            return False
        info = self._plugin_info.get(name)
        return bool(info and info.marks_synthesis)

    def disposition(self, name, result) -> ToolDisposition:
        """按插件政策裁决一次 invoke 结果的去向（宿主不按工具名分支）。

        工具名来自模型输出，形态不受信任：非字符串名一律按无插件
        信息处理（feed），不得让字典查询本身抛错改变循环语义。
        插件渲染回调的返回值在此校验：非字符串/空串一律抛错，由
        循环收敛为固定回退，绝不把 None 送进发送链。
        """
        if not isinstance(name, str):
            return ToolDisposition("feed")
        info = self._plugin_info.get(name)
        if info is None:
            return ToolDisposition("feed")
        if info.policy == "direct":
            if not self.is_enabled(name):
                # 未启用时不做 direct 渲染：声明了 unavailable_reply
                # 的插件以固定文案终结（现行 weather_lookup 语义），
                # 未声明的按未知工具回喂（现行 current_time 语义）。
                if info.unavailable_reply is not None:
                    return ToolDisposition("terminal", info.unavailable_reply)
                return ToolDisposition("feed")
            text = info.render_direct(result)
            if not isinstance(text, str) or not text:
                raise ValueError("direct rendering must produce non-empty text")
            return ToolDisposition("direct", text)
        if info.terminal_reply is not None:
            text = info.terminal_reply(result)
            if text is not None:
                if not isinstance(text, str) or not text:
                    raise ValueError("terminal reply must be non-empty text")
                return ToolDisposition("terminal", text)
        if info.unavailable_reply is not None and not _result_is_ok(result):
            return ToolDisposition("terminal", info.unavailable_reply)
        return ToolDisposition("feed")

    def register(self, tool: ReadOnlyTool):
        if (
            not isinstance(tool.name, str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool.name)
            or tool.name in self._tools
            or tool.name in {"send_private_msg", "send_group_msg", "qzone", "friend_management"}
            or not isinstance(tool.description, str)
            or not callable(tool.action)
            or (tool.source_guarded and tool.name != "web_search")
        ):
            raise ValueError("invalid tool registration")
        _check_schema(tool.parameters, root=True)
        # Prevent later schema mutation from weakening validation.
        self._tools[tool.name] = ReadOnlyTool(
            tool.name, tool.description, json.loads(json.dumps(tool.parameters)), tool.action,
            tool.user_input_check, tool.source_guarded, tool.caller_check,
            tool.caller_scope,
        )

    def is_enabled(self, name):
        policy = self.gateway.policies.get(name)
        return (self.enabled and name in self._tools and isinstance(policy, dict)
                and policy.get("enabled") is True)

    def schemas(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": json.loads(json.dumps(t.parameters)),
                },
            }
            for t in self._tools.values()
            if self.is_enabled(t.name)
        ]

    async def invoke(self, name, arguments, identity, *, user_message=""):
        if not isinstance(name, str) or not self.is_enabled(name):
            return UNAVAILABLE
        if not isinstance(arguments, str) or len(arguments) > MAX_ARGUMENT_CHARS:
            _gate_rejection(name, "schema")
            return UNAVAILABLE
        try:
            try:
                args = json.loads(
                    arguments,
                    object_pairs_hook=_unique_object,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                )
            except (ValueError, RecursionError):
                _gate_rejection(name, "schema")
                return UNAVAILABLE
            tool = self._tools[name]
            if not _valid(args, tool.parameters):
                _gate_rejection(name, "schema")
                return UNAVAILABLE
            if (name == "web_search" and isinstance(identity, str) and identity
                    and identity.casefold() in args["query"].casefold()):
                _gate_rejection(name, "identity")
                return UNAVAILABLE
            if tool.user_input_check:
                check = tool.user_input_check(args, user_message)
                if check is not True:
                    if isinstance(check, str):
                        _gate_rejection(name, check)
                    return UNAVAILABLE
            if tool.caller_check is not None and not tool.caller_check(args, identity):
                return UNAVAILABLE

            async def run():
                async with self._semaphore:

                    async def action():
                        # 宿主侧挂钩累积组合（不互斥）：source_guard 与
                        # caller_scope 各自独立判定、同时透传，任何
                        # 组合下都不许静默丢弃其一（fix1 复审 P3）。
                        host_kwargs = {}
                        if tool.source_guarded:
                            def source_guard(source):
                                return not (isinstance(identity, str) and identity and
                                            unicodedata.normalize("NFKC", identity).casefold()
                                            in unicodedata.normalize("NFKC", source).casefold())

                            host_kwargs["source_guard"] = source_guard
                        if tool.caller_scope is not None:
                            host_kwargs["caller_scope"] = tool.caller_scope(identity)
                        result = await tool.action(**args, **host_kwargs)
                        if not isinstance(result, str):
                            return {"status": "invalid"}
                        return {"status": "ok", "data": result}

                    return await self.gateway.invoke(name, action, dry_run=False, identity=identity)

            result = await asyncio.wait_for(run(), timeout=14 if name == "web_search" else 10)
            if isinstance(result, dict) and result.get("status") == "rate_limited":
                _gate_rejection(name, "rate_limited")
            if not isinstance(result, dict) or result.get("status") != "ok":
                return UNAVAILABLE
            # Cap the entire encoded tool result, including JSON escaping and metadata.
            # Plugin-declared caps win; the name-based caps remain for tools
            # registered without a plugin manifest.
            info = self._plugin_info.get(name)
            if info is not None and info.max_output_chars:
                limit = info.max_output_chars
            else:
                limit = WEB_SEARCH_RESULT_CHARS if name == "web_search" else MAX_RESULT_CHARS
            if name == "current_time":
                encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
                return encoded if len(encoded) <= limit else UNAVAILABLE
            data = result["data"][:limit]
            low, high = 0, len(data)
            while low < high:
                mid = (low + high + 1) // 2
                encoded = json.dumps(
                    {"status": "ok", "data": data[:mid]}, ensure_ascii=True, separators=(",", ":")
                )
                if len(encoded) <= limit:
                    low = mid
                else:
                    high = mid - 1
            return json.dumps(
                {"status": "ok", "data": data[:low]}, ensure_ascii=True, separators=(",", ":")
            )
        except (Exception, asyncio.TimeoutError):
            return UNAVAILABLE
