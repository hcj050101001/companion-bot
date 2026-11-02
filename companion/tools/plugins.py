"""工具插件协议与注册表（方案 §5.5）。

信任模型（与方案 §2.2/§5.5 一致）：插件是同进程、可信的自研代码，
不做进程隔离，本模块不构成沙箱。可强制的边界是模型驱动侧：任何
模型发起的工具调用都只能经 ReadOnlyRegistry.invoke（双开关、闭合
校验、保留参数名、参数体积、全局信号量、统一超时、结果截断、隐私
检查挂钩统一执行），插件动作只拿到校验后的参数，拿不到 identity、
sender、gateway 或记忆对象——register(ctx) 时上下文只暴露一个
登记回调，不暴露注册表本体。插件代码若主动 import 宿主内部模块
属于可信代码的自律范围，不在本协议的强制面内。

结果政策（policy）三类，由插件声明、宿主按政策分发（§5.5）：
- ``direct``：确定性直出（时间/天气），插件提供 render_direct 把网关
  结果渲染为最终单条回复，绕过模型合成与多气泡复核；渲染内容自带
  署名等要素，宿主不得剥离改写。
- ``synthesize``：结果回喂模型合成（搜索）；插件可提供 terminal_reply
  在特定结果（如内容不可用哨兵）时直接终结，或 unavailable_reply 在
  结果非 ok 时直接终结；是否置待合成标记由插件显式声明
  marks_synthesis（仅搜索声明），不由政策自动推导。
- ``unavailable`` 只作 ToolResult 的失败形态，不用于注册声明。

网络类插件（capabilities 含 ``needs_network``）必须在 meta 中声明
allowed_domains（非空），并在登记时向宿主申报自己将访问的固定
端点 guarded_endpoints：宿主在注册时逐个校验端点落在声明域名内，
并在每次调用前由宿主包装层复检，越界即抛错、被网关收敛为
unavailable。这是对插件自报端点的预检，不是网络层沙箱——动作
内部自行发起的其它请求不在预检范围内（可信代码自律）。搜索的
正文抓取是 Tavily 服务端完成的，本进程只访问 api.tavily.com
（见 WebSearchPlugin）。
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from companion.tools.current_time import current_time_tool, format_current_time
from companion.tools.date_countdown import (
    UNAVAILABLE_REPLY,
    date_countdown_tool,
    format_date_countdown,
)
from companion.tools.read_only import ReadOnlyTool
from companion.tools.weather import weather_tool
from companion.tools.web_search import (
    CONTENT_UNAVAILABLE,
    EXTRACT_URL,
    SEARCH_URL,
    web_search_tool,
)

RESULT_POLICIES = frozenset({"direct", "synthesize"})
TOOL_RESULT_POLICIES = frozenset({"direct", "synthesize", "unavailable"})
_NAME_RE = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_HOST_RE = re.compile(r"(?=.{1,253}\Z)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}")


class PluginConfig(BaseModel):
    """插件配置基类：独立命名空间、闭合校验，未知键启动即报错。"""

    model_config = ConfigDict(extra="forbid")


# 兼容别名（内部旧名）。
_PluginConfig = PluginConfig


class EmptyPluginConfig(PluginConfig):
    """无独立配置项的插件使用的空配置。"""


@dataclass(frozen=True)
class PluginMeta:
    """插件清单（§5.5 PluginMeta）。"""

    name: str
    version: str
    description: str
    capabilities: frozenset = frozenset()
    allowed_domains: tuple = ()
    max_output_chars: int = 2000

    def __post_init__(self):
        if not isinstance(self.name, str) or not _NAME_RE.fullmatch(self.name):
            raise ValueError("invalid plugin name")
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("plugin version must be a non-empty string")
        if not isinstance(self.description, str) or not self.description:
            raise ValueError("plugin description must be a non-empty string")
        caps = frozenset(self.capabilities)
        if not all(isinstance(c, str) and c for c in caps):
            raise ValueError("plugin capabilities must be non-empty strings")
        object.__setattr__(self, "capabilities", caps)
        domains = tuple(self.allowed_domains)
        if not all(isinstance(d, str) and _HOST_RE.fullmatch(d) for d in domains):
            raise ValueError("allowed_domains must be valid hostnames")
        if len(set(domains)) != len(domains):
            raise ValueError("allowed_domains must not contain duplicates")
        object.__setattr__(self, "allowed_domains", domains)
        if "needs_network" in caps and not domains:
            raise ValueError("network plugins must declare allowed_domains")
        if (isinstance(self.max_output_chars, bool)
                or not isinstance(self.max_output_chars, int)
                or not 64 <= self.max_output_chars <= 100_000):
            raise ValueError("max_output_chars must be a positive bounded int")


@dataclass(frozen=True)
class ToolResult:
    """带政策的类型化工具结果（§5.5）：不是裸字符串。

    diagnostics 只放结构化元数据（工具名、结果码、耗时、计数），
    绝不放参数、查询或结果正文。
    """

    policy: str
    text: str
    diagnostics: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.policy not in TOOL_RESULT_POLICIES:
            raise ValueError("unknown tool result policy")
        if not isinstance(self.text, str):
            raise ValueError("tool result text must be a string")


def url_host_allowed(meta: PluginMeta, url) -> bool:
    """域名白名单判定：仅 http/https，主机名与声明域名相等或为其子域。"""
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return False
    host = parts.hostname
    if not host:
        return False
    host = host.rstrip(".").lower()
    return any(host == d or host.endswith("." + d) for d in meta.allowed_domains)


class PluginContext:
    """插件注册上下文：唯一能力是登记一个与插件同名的只读工具。

    上下文只持有宿主提供的登记回调与插件自己的 meta，不持有注册表
    本体：插件无法经 ctx 触及 gateway 策略、其它工具或限流状态。
    """

    def __init__(self, meta, registrar):
        self._meta = meta
        self._registrar = registrar
        self.registered_name = None
        self._registration = None

    def register_tool(self, tool, *, policy, render_direct=None, terminal_reply=None,
                      unavailable_reply=None, marks_synthesis=False,
                      guarded_endpoints=()):
        if self.registered_name is not None:
            raise ValueError("a plugin registers exactly one tool")
        if not isinstance(tool, ReadOnlyTool) or tool.name != self._meta.name:
            raise ValueError("plugin tool name must match the plugin meta name")
        if policy not in RESULT_POLICIES:
            raise ValueError("unknown result policy")
        if policy == "direct" and not callable(render_direct):
            raise ValueError("direct plugins must provide render_direct")
        if render_direct is not None and not callable(render_direct):
            raise ValueError("render_direct must be callable")
        if terminal_reply is not None and not callable(terminal_reply):
            raise ValueError("terminal_reply must be callable")
        if unavailable_reply is not None and not isinstance(unavailable_reply, str):
            raise ValueError("unavailable_reply must be a string")
        endpoints = tuple(guarded_endpoints or ())
        if "needs_network" in self._meta.capabilities:
            # 网络插件必须向宿主申报固定端点：注册时逐个校验落在
            # 声明域名内，调用时由宿主包装层复检（见 _guard_endpoints）。
            if not endpoints:
                raise ValueError("network plugins must declare guarded_endpoints")
            for url in endpoints:
                if not url_host_allowed(self._meta, url):
                    raise ValueError("guarded endpoint outside allowed_domains")
            tool = ReadOnlyTool(
                tool.name, tool.description, tool.parameters,
                _guard_endpoints(self._meta, endpoints, tool.action),
                tool.user_input_check, tool.source_guarded, tool.caller_check,
                tool.caller_scope,
            )
        elif endpoints:
            raise ValueError("guarded_endpoints require the needs_network capability")
        registration = _ToolRegistration(
            policy=policy, render_direct=render_direct,
            terminal_reply=terminal_reply, unavailable_reply=unavailable_reply,
            marks_synthesis=marks_synthesis is True,
        )
        self._registrar(tool, registration)
        self.registered_name = tool.name
        self._registration = registration
        return registration


@dataclass(frozen=True)
class _ToolRegistration:
    policy: str
    render_direct: object = None
    terminal_reply: object = None
    unavailable_reply: str | None = None
    marks_synthesis: bool = False


@runtime_checkable
class ToolPlugin(Protocol):
    """工具插件协议（§5.5）。"""

    meta: PluginMeta
    config_schema: type[BaseModel]

    def register(self, ctx: PluginContext) -> None:
        """向宿主登记只读工具（经 PluginContext，门控由网关统一执行）。"""

    async def on_load(self) -> None:
        """插件被装载时调用。"""

    async def on_unload(self) -> None:
        """插件被卸载时调用；必须注销其 handler/缓存/定时器（dispose 语义）。"""


class BaseToolPlugin:
    """插件便利基类：默认生命周期为空操作，配置为空命名空间。"""

    meta: PluginMeta
    config_schema: type[BaseModel] = EmptyPluginConfig

    def register(self, ctx: PluginContext) -> None:
        raise NotImplementedError

    async def on_load(self) -> None:
        return None

    async def on_unload(self) -> None:
        return None


@dataclass
class _PluginRecord:
    plugin: object
    meta: PluginMeta
    registration: _ToolRegistration


class PluginRegistry:
    """插件注册表：集中登记、校验清单、托管生命周期。

    插件一经登记即在进程生命周期内有效（同进程档不做热卸载注册表
    摘除）；on_unload 只负责插件自身资源的 dispose。
    """

    def __init__(self, registry):
        self._registry = registry
        self._records: dict[str, _PluginRecord] = {}
        self._loaded: set[str] = set()

    def _accept_registration(self, meta, tool, registration) -> None:
        """宿主侧登记回调：网关登记与政策挂载在同一处完成。"""
        self._registry.register(tool)
        self._registry.attach_plugin(
            meta.name,
            policy=registration.policy,
            render_direct=registration.render_direct,
            terminal_reply=registration.terminal_reply,
            unavailable_reply=registration.unavailable_reply,
            max_output_chars=meta.max_output_chars,
            marks_synthesis=registration.marks_synthesis,
        )

    def register_plugin(self, plugin) -> PluginMeta:
        meta = getattr(plugin, "meta", None)
        if not isinstance(meta, PluginMeta):
            raise ValueError("plugin must declare a PluginMeta")
        schema = getattr(plugin, "config_schema", None)
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise ValueError("plugin config_schema must be a pydantic model class")
        if meta.name in self._records:
            raise ValueError("plugin already registered")
        # 两阶段登记：registrar 只暂存 (tool, registration)，不触底层
        # 注册表；plugin.register() 抛错或未按名登记时暂存随之丢弃，
        # 底层零痕迹、同名可重试。成功且名字对齐后才统一提交。
        staged: dict = {}

        def _stage(tool, registration):
            staged["tool"] = tool
            staged["registration"] = registration

        ctx = PluginContext(meta, _stage)
        plugin.register(ctx)
        if ctx.registered_name != meta.name:
            raise ValueError("plugin must register its tool during register()")
        self._accept_registration(meta, staged["tool"], staged["registration"])
        self._records[meta.name] = _PluginRecord(plugin, meta, ctx._registration)
        return meta

    def meta(self, name):
        record = self._records.get(name)
        return record.meta if record else None

    def names(self):
        return tuple(self._records)

    def url_allowed(self, name, url) -> bool:
        record = self._records.get(name)
        return record is not None and url_host_allowed(record.meta, url)

    async def _unload_records(self, records) -> None:
        # 逐个隔离异常并逐个清账：单个插件 on_unload 抛错不得中断
        # 其余插件的卸载，记日志后继续，且该插件不再留在 _loaded。
        for record in reversed(list(records)):
            try:
                await record.plugin.on_unload()
            except Exception:  # noqa: BLE001 — dispose 隔离，见上注
                logging.getLogger(__name__).exception(
                    "plugin on_unload failed: %s", record.meta.name
                )
            finally:
                self._loaded.discard(record.meta.name)

    async def load_all(self) -> None:
        loaded_now: list[_PluginRecord] = []
        try:
            for record in self._records.values():
                await record.plugin.on_load()
                # 只有 on_load 成功才记账，本轮装载清单供失败回滚。
                self._loaded.add(record.meta.name)
                loaded_now.append(record)
        except Exception:
            # 任一插件装载失败：逆序卸载本轮已装载者并清账后上抛，
            # 不留半装载状态；卸载异常逐个隔离，不掩盖原始失败。
            await self._unload_records(loaded_now)
            raise

    async def unload_all(self) -> None:
        # 只卸载 _loaded 中确已装载的插件（逆序、逐个清账）；未装载
        # 或已回滚清账的插件不得被重复卸载。
        await self._unload_records(
            [r for r in self._records.values() if r.meta.name in self._loaded]
        )


def validate_plugin_config(plugin, raw) -> BaseModel:
    """按插件自己的 config_schema 校验其配置命名空间（闭合、fail-fast）。"""
    schema = getattr(plugin, "config_schema", None)
    if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        raise ValueError("plugin config_schema must be a pydantic model class")
    return schema.model_validate(raw or {})


def _guard_endpoints(meta: PluginMeta, urls, action):
    """调用时预检：插件自身将访问的固定端点必须落在声明域名内。"""
    async def guarded(**kwargs):
        for url in urls:
            if not url_host_allowed(meta, url):
                raise ValueError("endpoint outside declared allowed_domains")
        return await action(**kwargs)

    return guarded


# --- 现行只读工具的插件形态（行为与既有工具逐项等价） ---


def render_current_time_result(result: str) -> str:
    """direct 渲染：只接受完整自洽的网关结果，否则固定不可用文案。"""
    return format_current_time(result)


class CurrentTimePlugin(BaseToolPlugin):
    meta = PluginMeta(
        name="current_time",
        version="1.0.0",
        description="Get the current time in Asia/Shanghai.",
        max_output_chars=2000,
    )

    def __init__(self, clock=None, zone_factory=None):
        # 时钟/时区工厂可注入是确定性测试的既有口径，生产用系统时钟。
        self._clock = clock
        self._zone_factory = zone_factory

    def register(self, ctx: PluginContext) -> None:
        ctx.register_tool(
            current_time_tool(self._clock, self._zone_factory), policy="direct",
            render_direct=render_current_time_result,
        )


# T9 文案的单一事实源在 date_countdown.py（UNAVAILABLE_REPLY），此处只转引，
# 不得另写一份字面量，避免两处文案漂移。
COUNTDOWN_UNAVAILABLE_REPLY = UNAVAILABLE_REPLY


def render_date_countdown_result(result: str) -> str:
    """direct 渲染：只接受完整自洽的网关结果，否则固定不可用文案。"""
    return format_date_countdown(result)


class DateCountdownPlugin(BaseToolPlugin):
    meta = PluginMeta(
        name="date_countdown",
        version="1.0.0",
        description="Deterministic countdown / anniversary lookup from configured dates.",
        capabilities=frozenset(),
        max_output_chars=2000,
    )

    def __init__(self, anniversaries=(), clock=None, zone_factory=None, *,
                 anniversaries_provider=None, owner_ids=None):
        # 纪念日清单由宿主装配时经 build_plugin_registry 注入（已校验
        # 条目序列）；时钟/时区工厂可注入是确定性测试的既有口径，
        # 生产用系统时钟。配置面在 AppConfig 树，插件不另设命名空间。
        # anniversaries_provider / owner_ids 是体验增强的装配注入：
        # 前者提供主人对话存储条目（仅存储部分，与静态清单的合并
        # 及按来源可见性在工具内完成，查询时实时读取）；后者是
        # 主人身份集合，None/空集为 fail-closed（无人可列表、
        # 存储条目对所有人不可见）。
        self._anniversaries = anniversaries
        self._clock = clock
        self._zone_factory = zone_factory
        self._anniversaries_provider = anniversaries_provider
        self._owner_ids = owner_ids

    def register(self, ctx: PluginContext) -> None:
        ctx.register_tool(
            date_countdown_tool(
                self._anniversaries, self._clock, self._zone_factory,
                anniversaries_provider=self._anniversaries_provider,
                owner_ids=self._owner_ids,
            ),
            policy="direct",
            render_direct=render_date_countdown_result,
            unavailable_reply=COUNTDOWN_UNAVAILABLE_REPLY,
        )


WEATHER_UNAVAILABLE_REPLY = "天气查询暂不可用，请提供本次要查询的城市和日期后再试。"


def render_weather_result(result: str) -> str:
    """direct 渲染：ok 结果的 data 原样直出（署名是结果的一部分）。"""
    try:
        wrapper = json.loads(result)
    except (ValueError, TypeError):
        return WEATHER_UNAVAILABLE_REPLY
    if (isinstance(wrapper, dict) and wrapper.get("status") == "ok"
            and isinstance(wrapper.get("data"), str)):
        return wrapper["data"]
    return WEATHER_UNAVAILABLE_REPLY


class WeatherPlugin(BaseToolPlugin):
    meta = PluginMeta(
        name="weather_lookup",
        version="1.0.0",
        description="Open-Meteo city forecast; the city must come from the current message.",
        capabilities=frozenset({"needs_network"}),
        allowed_domains=("geocoding-api.open-meteo.com", "api.open-meteo.com"),
        max_output_chars=2000,
    )

    def __init__(self, adapter=None):
        # 适配器可注入是确定性测试的既有口径，生产用 Open-Meteo 适配器。
        self._adapter = adapter

    def register(self, ctx: PluginContext) -> None:
        from companion.tools.weather import FORECAST_URL, GEOCODING_URL

        ctx.register_tool(
            weather_tool(self._adapter), policy="direct",
            render_direct=render_weather_result,
            unavailable_reply=WEATHER_UNAVAILABLE_REPLY,
            guarded_endpoints=(GEOCODING_URL, FORECAST_URL),
        )


SEARCH_UNAVAILABLE_REPLY = "外部搜索暂不可用，请提供不含个人信息的简短查询后再试。"


def search_terminal_reply(result: str) -> str | None:
    """内容不可用哨兵是搜索的终结结果：原样直出，不进模型合成。"""
    try:
        wrapper = json.loads(result)
    except (ValueError, TypeError):
        return None
    if (isinstance(wrapper, dict) and wrapper.get("status") == "ok"
            and wrapper.get("data") == CONTENT_UNAVAILABLE):
        return CONTENT_UNAVAILABLE
    return None


class WebSearchPluginConfig(PluginConfig):
    content_extraction_enabled: bool = False


class WebSearchPlugin(BaseToolPlugin):
    meta = PluginMeta(
        name="web_search",
        version="1.0.0",
        description="Tavily search with optional provider-side extraction.",
        capabilities=frozenset({"needs_network"}),
        allowed_domains=("api.tavily.com",),
        max_output_chars=5000,
    )
    config_schema = WebSearchPluginConfig

    def __init__(
        self,
        config: WebSearchPluginConfig | None = None,
        *,
        secret_provider=None,
    ):
        self.config = config or WebSearchPluginConfig()
        # 密钥经宿主注入的 SecretProvider 按名读取（P5 接线）；缺省
        # None 时 web_search_tool 内部回退 EnvironmentSecretProvider。
        self.secret_provider = secret_provider

    def register(self, ctx: PluginContext) -> None:
        ctx.register_tool(
            web_search_tool(
                content_extraction_enabled=self.config.content_extraction_enabled,
                secret_provider=self.secret_provider,
            ),
            policy="synthesize",
            terminal_reply=search_terminal_reply,
            unavailable_reply=SEARCH_UNAVAILABLE_REPLY,
            guarded_endpoints=(SEARCH_URL, EXTRACT_URL),
            # 搜索正文回喂后须走合成校验（无来源清洗/虚假访问声明
            # 重试），与现行 web_content_pending 语义逐项对应。
            marks_synthesis=True,
        )


def build_plugin_registry(
    registry, *, web_search_config=None, secret_provider=None,
    countdown_anniversaries=(),
    countdown_anniversaries_provider=None, countdown_owner_ids=None,
) -> PluginRegistry:
    """把只读工具以插件形态集中登记到既有网关注册表。

    countdown_anniversaries 为倒计时插件的纪念日清单（已校验条目
    序列），缺省为空清单，既有调用方不传该参时行为不变；时钟不在
    此处注入（与 CurrentTimePlugin 的既有口径一致）。
    countdown_anniversaries_provider / countdown_owner_ids 为体验
    增强的装配注入（存储条目动态来源 / 主人身份集合）。静态查询
    行为与原实现逐项一致；countdown_owner_ids 缺省 None 时按
    fail-closed 处理（列表与存储条目对所有人关闭），不再 fail-open。
    """
    plugins = PluginRegistry(registry)
    plugins.register_plugin(CurrentTimePlugin())
    plugins.register_plugin(
        DateCountdownPlugin(
            anniversaries=countdown_anniversaries,
            anniversaries_provider=countdown_anniversaries_provider,
            owner_ids=countdown_owner_ids,
        )
    )
    plugins.register_plugin(WeatherPlugin())
    search = WebSearchPlugin(
        validate_plugin_config(WebSearchPlugin(), web_search_config),
        secret_provider=secret_provider,
    )
    plugins.register_plugin(search)
    return plugins
