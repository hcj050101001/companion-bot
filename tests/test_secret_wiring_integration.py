"""合并接缝回归：SecretProvider 注入链（P4 插件形态 × P5 密钥接线）。

宿主注入的 provider 必须真正到达两个消费端：LLM 客户端（经
create_llm_client）与搜索插件（经 build_plugin_registry →
WebSearchPlugin → web_search_tool → search）。漏传时两者都会静默
回退 EnvironmentSecretProvider，因此用记录型假 provider 断言注入
对象被实际咨询，而不是只断言构造成功。全部进程内合成，不联网。
"""

import asyncio
import json
import logging

import pytest

from companion.brain import FALLBACK_REPLY, DeepSeekClient, create_llm_client
from companion.config import SECRET_ENV_NAMES, EnvironmentSecretProvider
from companion.tool_loop import ToolLoopLimits, ToolTurnError
from companion.tools.plugins import build_plugin_registry
from companion.tools.read_only import UNAVAILABLE, ReadOnlyRegistry


class RecordingProvider:
    """按名记录咨询并返回固定值（None 表示无密钥，不触发任何 HTTP）。"""

    def __init__(self, value=None):
        self.value = value
        self.names: list[str] = []

    def get(self, name):
        self.names.append(name)
        return self.value


@pytest.mark.timeout(10)
def test_llm_client_threads_injected_provider_and_tool_limits():
    provider = RecordingProvider()
    limits = ToolLoopLimits(max_tool_rounds=1, max_tool_calls=1)
    client = create_llm_client(
        "deepseek", tool_limits=limits, secret_provider=provider
    )
    assert isinstance(client, DeepSeekClient)
    assert client.secret_provider is provider
    assert client.tool_limits is limits
    assert client.supports_tools is True
    # 单轮原语与普通生成都经注入 provider 取密钥（无 key 时收敛，
    # 不回退环境提供者）。
    with pytest.raises(ToolTurnError):
        asyncio.run(client.tool_turn([], [], tools_enabled=True))
    assert provider.names == ["DEEPSEEK_API_KEY"]
    provider.names.clear()
    assert asyncio.run(client.generate("prompt"))  # FALLBACK_REPLY 非空
    assert provider.names == ["DEEPSEEK_API_KEY"]


@pytest.mark.timeout(10)
def test_plugin_registry_threads_injected_provider_to_search():
    provider = RecordingProvider()
    registry = ReadOnlyRegistry(
        enabled=True, policies={"web_search": {"enabled": True}}
    )
    plugins = build_plugin_registry(registry, secret_provider=provider)

    async def run():
        await plugins.load_all()
        try:
            return await registry.invoke(
                "web_search",
                json.dumps({"query": "test query"}),
                None,
                user_message="please test query now",
            )
        finally:
            await plugins.unload_all()

    # 无密钥 → 网关收敛为 UNAVAILABLE；关键断言是注入 provider 被咨询。
    # 搜索防线现也经同一 provider 按 SECRET_ENV_NAMES 名册逐个按名取
    # 凭证值（查询门控与 search 各构建一次），故咨询名单不再只有
    # TAVILY_API_KEY，但必须只按名册内的名字咨询、且含搜索密钥名。
    assert asyncio.run(run()) == UNAVAILABLE
    assert "TAVILY_API_KEY" in provider.names
    assert set(provider.names) <= SECRET_ENV_NAMES


class ExplodingProvider:
    """get() 恒抛把合成密钥值写进消息的异常（模拟不守规矩的自定义 provider）。"""

    SYNTHETIC = "synthetic-leaked-value-7391"

    def __init__(self):
        self.names: list[str] = []

    def get(self, name):
        self.names.append(name)
        raise RuntimeError(f"provider blew up with {self.SYNTHETIC}")


@pytest.mark.timeout(10)
def test_provider_exception_with_secret_in_message_never_leaks(caplog):
    # 三个 DeepSeek 密钥读取点（普通生成 / 单轮原语 / 工具入口）都须
    # 按既有“缺 key”语义收敛，且日志与异常链中绝不出现异常原文
    # 里夹带的合成密钥值；日志只记异常类型名。
    provider = ExplodingProvider()
    client = DeepSeekClient(secret_provider=provider)

    class _Registry:
        def schemas(self):
            return [{"type": "function", "function": {"name": "stub"}}]

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(client.generate("prompt")) == FALLBACK_REPLY
        with pytest.raises(ToolTurnError) as excinfo:
            asyncio.run(client.tool_turn([], [], tools_enabled=True))
        assert str(excinfo.value) == "missing_api_key"
        assert excinfo.value.__cause__ is None
        assert asyncio.run(
            client.generate_with_tools("prompt", _Registry(), "synthetic-user")
        ) == (FALLBACK_REPLY, False)
    assert provider.names == ["DEEPSEEK_API_KEY"] * 3
    assert ExplodingProvider.SYNTHETIC not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.timeout(10)
def test_uninjected_defaults_fall_back_to_environment_provider():
    # 未注入时保持 P5 原口径：客户端自带 EnvironmentSecretProvider。
    assert isinstance(
        DeepSeekClient().secret_provider, EnvironmentSecretProvider
    )
    assert isinstance(
        create_llm_client("deepseek").secret_provider,
        EnvironmentSecretProvider,
    )
