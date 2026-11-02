"""Agent 大脑：人格、记忆和当前消息的组合及 LLM 接口。"""

import asyncio
import json
import logging
import os
import shutil
from abc import ABC, abstractmethod

import httpx

from companion.config import EnvironmentSecretProvider
from companion.multipart import MULTIPART_INSTRUCTION
from companion.tool_loop import (
    FALLBACK_REPLY,
    SYNTHESIS_FALLBACK,
    TOOL_DATA_INSTRUCTION,
    ProviderTurn,
    ToolCallRequest,
    ToolLoopLimits,
    ToolTurnError,
    run_tool_loop,
)

logger = logging.getLogger("companion.brain")


def _get_secret(provider, name):
    """经 SecretProvider 按名读取密钥；provider 异常按“缺 key”收敛。

    自定义 provider 可能把密钥值写进异常消息，异常原文绝不能经
    traceback 进入日志或回复：这里捕获后只以异常类型名记一条日志，
    并返回 None，由各调用点按既有缺 key 语义收敛（普通生成走
    FALLBACK_REPLY、tool_turn 抛 ToolTurnError("missing_api_key")、
    工具入口返回 (FALLBACK_REPLY, False)）。捕获后直接返回、不再
    抛出，异常链就此切断（等效 from None），原文无任何外泄路径。
    """
    try:
        return provider.get(name)
    except Exception as exc:  # noqa: BLE001 — 收敛口径见 docstring
        logger.warning("secret_provider get failed: %s", type(exc).__name__)
        return None


__all__ = [
    "FALLBACK_REPLY",
    "SYNTHESIS_FALLBACK",
    "TOOL_DATA_INSTRUCTION",
    "TIME_SAFETY_INSTRUCTION",
    "LLMClient",
    "EchoStub",
    "CodexClient",
    "DeepSeekClient",
    "DeepSeekVisionClient",
    "OpenAICompatibleClient",
    "CompanionBrain",
    "create_llm_client",
]


class LLMClient(ABC):
    """LLMProvider 协议（方案 §5.4）：能力以声明为准，不以类型判断。

    ``supports_tools`` 是能力声明：宿主只对声明支持工具的 provider
    运行工具循环，其余 provider 走普通生成降级路径。
    ``generate_with_tools`` 只在 supports_tools 为真时被调用。
    声明支持的 provider 须实现单轮原语 ``tool_turn``，且必须自带有界
    超时（状态机不代为计时；DeepSeek 实现以 wait_for 包裹单轮请求）；
    返回的 ProviderTurn.calls 还会由状态机复核形态与数量上限。
    """

    supports_tools: bool = False
    model_name: str = "unknown"

    @abstractmethod
    async def generate(self, prompt: str) -> str:
        """根据完整 prompt 生成回复。"""

    async def generate_json(self, prompt: str) -> str:
        """请求结构化 JSON；默认退回普通生成，由回复路径严格校验。"""
        return await self.generate(prompt)

    async def generate_with_tools(self, prompt, registry, identity, *, user_message=""):
        """工具循环入口：未声明 supports_tools 时降级为普通生成。

        声明支持的 provider 只需实现单轮原语 tool_turn，循环本身由
        companion.tool_loop 的显式状态机统一驱动。
        """
        if not self.supports_tools:
            return await self.generate(prompt), False
        result = await run_tool_loop(
            self, prompt, registry, identity,
            user_message=user_message,
            limits=getattr(self, "tool_limits", None),
        )
        return result.reply, result.tool_calls_seen


class EchoStub(LLMClient):
    """开发期安全占位实现，不调用外部模型。"""

    model_name = "echo-stub"

    async def generate(self, prompt: str) -> str:
        latest = prompt.rsplit("当前消息：", 1)[-1].strip()
        return f"我听到了：{latest}。愿意多和我说说吗？"

    async def generate_json(self, prompt: str) -> str:
        answer = await self.generate(prompt)
        return json.dumps({"bubbles": [answer]}, ensure_ascii=False)


class CodexClient(LLMClient):
    """调用受限的本地 Codex CLI；进程并发最多两个。"""

    _semaphore = asyncio.Semaphore(2)

    def __init__(self, timeout: float = 20):
        self.timeout = timeout

    async def generate(self, prompt: str) -> str:
        executable = shutil.which("codex")
        if not executable:
            return FALLBACK_REPLY
        command = [
            executable,
            "exec",
            "-m",
            "gpt-6-luna",
            "-c",
            "model_reasoning_effort=low",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "-",
        ]
        try:
            async with self._semaphore:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd="/tmp",
                )
                output, _ = await asyncio.wait_for(proc.communicate(prompt.encode()), self.timeout)
            result = output.decode(errors="replace").strip()
            return result if proc.returncode == 0 and result else FALLBACK_REPLY
        except (OSError, asyncio.TimeoutError):
            return FALLBACK_REPLY


class DeepSeekClient(LLMClient):
    """OpenAI 兼容接口客户端，凭据只在请求时经 SecretProvider 读取。

    声明 supports_tools=True：工具循环由 companion.tool_loop 的显式
    状态机驱动，本类只提供单轮原语 tool_turn 与普通生成。
    """

    supports_tools = True
    model_name = "deepseek-chat"

    def __init__(
        self,
        timeout: float = 20,
        *,
        tool_limits: ToolLoopLimits | None = None,
        secret_provider=None,
    ):
        self.timeout = timeout
        self.tool_limits = tool_limits or ToolLoopLimits()
        self.last_tool_diagnostics: tuple = ()
        self.last_tool_states: tuple = ()
        self.last_tool_results: tuple = ()
        self.secret_provider = (
            secret_provider if secret_provider is not None else EnvironmentSecretProvider()
        )

    async def _chat(self, prompt: str, *, json_mode: bool = False) -> str:
        key = _get_secret(self.secret_provider, "DEEPSEEK_API_KEY")
        if not key:
            return FALLBACK_REPLY
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                )
                response.raise_for_status()
                answer = response.json()["choices"][0]["message"]["content"].strip()
                return answer or FALLBACK_REPLY
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            return FALLBACK_REPLY

    async def generate(self, prompt: str) -> str:
        return await self._chat(prompt)

    async def generate_json(self, prompt: str) -> str:
        return await self._chat(prompt, json_mode=True)

    async def tool_turn(self, messages, schemas, *, tools_enabled: bool) -> ProviderTurn:
        """单轮原语：一次 chat/completions 请求，结构校验后规范化返回。

        传输/响应结构失败抛 ToolTurnError（只带异常类别名）；助手消息
        本身结构不合法（role 不符、tool_calls 形态错误）返回
        malformed 的 ProviderTurn，交状态机 fail-closed。
        """
        key = _get_secret(self.secret_provider, "DEEPSEEK_API_KEY")
        if not key:
            raise ToolTurnError("missing_api_key")
        payload = {"model": self.model_name, "messages": messages, "tools": schemas}
        if not tools_enabled:
            payload["tool_choice"] = "none"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await asyncio.wait_for(client.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={"Authorization": f"Bearer {key}"}, json=payload,
                ), timeout=self.timeout)
                response.raise_for_status()
                body = response.json()
                message = body["choices"][0]["message"]
        except (httpx.HTTPError, asyncio.TimeoutError, KeyError, IndexError,
                TypeError, ValueError) as exc:
            raise ToolTurnError(type(exc).__name__) from exc
        usage = body.get("usage") if isinstance(body, dict) else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            # 角色无效在现行实现里先于 calls_seen 置位返回，此处不
            # 置 calls_present，保持逐项等价。
            return ProviderTurn(malformed=True, usage=usage)
        calls = message.get("tool_calls")
        if not calls:
            return ProviderTurn(content=message.get("content"), usage=usage,
                                raw_message=message)
        if (not isinstance(calls, list) or len(calls) > 32
                or any(not isinstance(c, dict) or not isinstance(c.get("id"), str)
                       or not c["id"] or c.get("type") != "function"
                       or not isinstance(c.get("function"), dict) for c in calls)
                or len({c["id"] for c in calls}) != len(calls)):
            # 调用确实出现过、只是形态非法：calls_present 让状态机
            # 按「见过调用」收尾（与现行 calls_seen 先置位再校验的
            # 顺序逐项一致）。
            return ProviderTurn(malformed=True, calls_present=True, usage=usage)
        parsed = tuple(
            ToolCallRequest(
                id=c["id"],
                name=c["function"].get("name"),
                arguments=c["function"].get("arguments"),
                raw=c,
            )
            for c in calls
        )
        return ProviderTurn(content=message.get("content"), calls=parsed,
                            usage=usage, raw_message=message)

    async def generate_with_tools(self, prompt, registry, identity, *, user_message=""):
        """Return (reply, tool_calls_seen); never used by the safety judge.

        工具循环由 companion.tool_loop 的显式状态机驱动；本方法只做
        入口收敛（无可用工具/无密钥时与现行口径一致）与结果转交，
        并把结构化诊断留在 last_tool_diagnostics（不含正文）。
        诊断三元组在入口先清空：早退路径不得残留上一请求的数据。
        """
        self.last_tool_diagnostics = ()
        self.last_tool_states = ()
        self.last_tool_results = ()
        schemas = registry.schemas()
        if not schemas:
            return await self.generate(prompt), False
        key = _get_secret(self.secret_provider, "DEEPSEEK_API_KEY")
        if not key:
            return FALLBACK_REPLY, False
        result = await run_tool_loop(
            self, prompt, registry, identity,
            user_message=user_message, limits=self.tool_limits,
        )
        self.last_tool_diagnostics = result.diagnostics
        self.last_tool_states = result.states
        self.last_tool_results = result.tool_results
        return result.reply, result.tool_calls_seen


class DeepSeekVisionClient:
    """DeepSeek 视觉客户端：deepseek-flash 原生多模态，只做图片生成。

    与 DeepSeekClient 分开成独立类而不是并入普通生成：图片只能
    出现在 user 消息的 content 块数组里，且视觉路径的失败口径是
    返回 None 让调用方回落旧固定回复，不走普通生成的
    FALLBACK_REPLY。凭据同样只在请求时经 SecretProvider 按名读。
    本类不实现 LLMClient 的普通 generate，不参与工具循环。
    """

    def __init__(self, *, model: str = "deepseek-flash", timeout: float = 30.0,
                 secret_provider=None):
        self.model_name = model
        self.timeout = timeout
        self.secret_provider = (
            secret_provider if secret_provider is not None else EnvironmentSecretProvider()
        )

    def has_credentials(self) -> bool:
        """是否已配置 DEEPSEEK_API_KEY；供调用方在下载图片前先判定。"""
        return bool(_get_secret(self.secret_provider, "DEEPSEEK_API_KEY"))

    async def generate_with_images(self, prompt: str, image_data_urls) -> str | None:
        """带图生成；无密钥/无图/任何请求失败都返回 None，不重试不抛。

        data URL 含图片内容，只进请求体，绝不进日志；失败日志只记
        异常类型名（与 _get_secret 的收敛口径一致）。
        """
        if not image_data_urls:
            return None
        key = _get_secret(self.secret_provider, "DEEPSEEK_API_KEY")
        if not key:
            return None
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}]
                    + [
                        {"type": "image_url", "image_url": {"url": url}}
                        for url in image_data_urls
                    ],
                }
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await asyncio.wait_for(
                    client.post(
                        "https://api.deepseek.com/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json=payload,
                    ),
                    timeout=self.timeout,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                answer = content.strip() if isinstance(content, str) else ""
                return answer or None
        except (
            httpx.HTTPError,
            asyncio.TimeoutError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ) as exc:
            logger.warning(
                "deepseek vision generate failed: %s", type(exc).__name__
            )
            return None


class OpenAICompatibleClient(LLMClient):
    """OpenAI-compatible endpoint configured only through environment variables."""

    def __init__(self, timeout: float = 20, secret_provider=None):
        self.timeout = timeout
        self.secret_provider = (
            secret_provider if secret_provider is not None else EnvironmentSecretProvider()
        )

    async def _chat(self, prompt: str, *, json_mode: bool = False) -> str:
        key = _get_secret(self.secret_provider, "OPENAI_COMPATIBLE_API_KEY")
        # OPENAI_COMPATIBLE_BASE_URL / OPENAI_COMPATIBLE_MODEL 不是密钥、
        # 不在 provider 名册内，保持从进程环境读取。
        base_url, model = (
            os.getenv(name)
            for name in (
                "OPENAI_COMPATIBLE_BASE_URL",
                "OPENAI_COMPATIBLE_MODEL",
            )
        )
        if not all((key, base_url, model)):
            return FALLBACK_REPLY
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                )
                response.raise_for_status()
                answer = response.json()["choices"][0]["message"]["content"].strip()
                return answer or FALLBACK_REPLY
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            return FALLBACK_REPLY

    async def generate(self, prompt: str) -> str:
        return await self._chat(prompt)

    async def generate_json(self, prompt: str) -> str:
        return await self._chat(prompt, json_mode=True)


def create_llm_client(
    provider: str,
    *,
    tool_limits: ToolLoopLimits | None = None,
    secret_provider=None,
) -> LLMClient:
    if provider == "deepseek":
        return DeepSeekClient(tool_limits=tool_limits, secret_provider=secret_provider)
    if provider == "openai_compatible":
        return OpenAICompatibleClient(secret_provider=secret_provider)
    if provider == "codex":
        return CodexClient()
    raise ValueError("Unsupported LLM provider")


# This instruction is probabilistic; it does not guard unconstrained model output.
TIME_SAFETY_INSTRUCTION = "没有成功的当前时间工具结果时，不要断言准确的当前时间，也不要猜测。"


class CompanionBrain:
    """装配人设、短期记忆和当前消息并生成回答。"""

    def __init__(self, persona: str, client: LLMClient | None = None):
        self.persona, self.client = persona, client or EchoStub()

    def _build_prompt(
        self, message: str, history: list[tuple[str, str]], *, persona=None, repeat_level=0
    ) -> str:
        transcript = "\n".join(f"{role}：{content}" for role, content in history)
        prompt = (f"{self.persona if persona is None else persona}\n{TIME_SAFETY_INSTRUCTION}\n"
                  f"短期记忆：\n{transcript}\n当前消息：{message}")
        if repeat_level:
            repair = ("温暖地承认之前的解释可能没有说到点上，换一种方式回答，"
                      "并提出一个具体的替代方法。" if repeat_level == 1 else
                      "用更温暖自然的语气回应，简短而有用地重述或换个例子。")
            prompt += ("\n这可能是用户重复的问题。" + repair +
                       "保持耐心，不要调侃、责备、拒绝或表现出不耐烦。")
        return prompt

    def build_prompt(
        self, message, history, *, persona=None, repeat_level=0
    ) -> str:
        """公开 prompt 拼装入口：供 ingress 视觉路径与普通生成同口径。

        视觉请求需要把与 respond 完全一致的 prompt 交给视觉
        client（图片另以 content 块传入），故在此公开转调私有
        实现，避免调用方复制拼装逻辑造成口径漂移。
        """
        return self._build_prompt(
            message, history, persona=persona, repeat_level=repeat_level
        )

    async def respond(
        self, message: str, history: list[tuple[str, str]], *, persona=None, repeat_level=0
    ) -> str:
        prompt = self._build_prompt(message, history, persona=persona,
                                    repeat_level=repeat_level)
        return await self.client.generate(prompt)

    async def respond_multipart(
        self, message: str, history: list[tuple[str, str]], *, persona=None, repeat_level=0
    ) -> str:
        """Ask for one bounded structured object; validation never retries the model."""
        prompt = self._build_prompt(message, history, persona=persona,
                                    repeat_level=repeat_level)
        prompt += "\n" + MULTIPART_INSTRUCTION
        generator = getattr(self.client, "generate_json", None)
        if generator is None:
            generator = self.client.generate
        return await generator(prompt)

    async def respond_with_tools(self, message, history, registry, identity, *, persona=None):
        # 能力声明优先于类型判断（§5.4）：未声明 supports_tools 的
        # provider 走普通生成降级路径，工具永不被提供。
        if not getattr(self.client, "supports_tools", False):
            return await self.respond(message, history, persona=persona), False
        transcript = "\n".join(f"{role}：{content}" for role, content in history)
        prompt = (f"{self.persona if persona is None else persona}\n{TIME_SAFETY_INSTRUCTION}\n"
                  f"短期记忆：\n{transcript}\n当前消息：{message}")
        return await self.client.generate_with_tools(
            prompt, registry, identity, user_message=message
        )
