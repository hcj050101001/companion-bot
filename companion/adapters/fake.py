"""FakeAdapter：内存实现，契约测试与剧本测试的底座（方案 §5.2）。

记录全部 send 调用，支持注入结果三态、延迟与健康值；normalize 与
OneBotAdapter 共用同一归一化函数，保证契约测试测的是同一套语义。
"""

import asyncio

from companion.adapters.actions import ActionsBundle
from companion.adapters.base import (
    SUPPORTED_SEGMENT_TYPES,
    AdapterCapabilities,
    AdapterHealth,
    ClassifiedEvent,
    DeliveryHandle,
    Ignore,
    InboundEvent,
    SendReceipt,
)
from companion.adapters.gateway import (
    PRINCIPAL_SYSTEM,
    ActionGateway,
    build_action_levels,
)
from companion.adapters.onebot import (
    EVENT_KINDS,
    classify_onebot_event,
    normalize_onebot_event,
)


class FakeTransport:
    """内存传输替身：与 OneBotTransport 同契约（post_action/get_action）。

    按 action 名查预置响应表：值可为 dict（原样返回）或 Exception
    实例（原样抛出）；未预置的动作回缺省成功响应。全部调用记入
    ``calls``（{"method", "action", "params"}），不出网。
    """

    DEFAULT_RESPONSE = {"status": "ok", "retcode": 0, "data": {}}

    def __init__(self, responses: dict | None = None):
        self.responses = dict(responses or {})
        self.calls: list[dict] = []

    async def _respond(self, method: str, action: str, params):
        self.calls.append({"method": method, "action": action, "params": params})
        response = self.responses.get(action, self.DEFAULT_RESPONSE)
        if isinstance(response, Exception):
            raise response
        return response

    async def post_action(self, action: str, params: dict | None = None) -> dict:
        return await self._respond("post_action", action, params)

    async def get_action(self, action: str) -> dict:
        return await self._respond("get_action", action, None)


class FakeAdapter:
    """不连任何平台的假适配器：发送只记账、不出网。"""

    name = "fake"
    capabilities = AdapterCapabilities(
        segment_types=SUPPORTED_SEGMENT_TYPES,
        event_kinds=EVENT_KINDS,
        action_levels=build_action_levels(),
    )

    def __init__(
        self,
        outcome: str = "accepted",
        delay_seconds: float = 0.0,
        health_value: AdapterHealth | None = None,
        responses: dict | None = None,
        policies: dict | None = None,
    ):
        self.sends: list[dict] = []
        self.segment_sends: list[dict] = []
        self.outcome = outcome
        self.delay_seconds = delay_seconds
        self.health_value = health_value if health_value is not None else AdapterHealth(True, True)
        # 门面契约与真实适配器同构：FakeTransport + ActionGateway
        # （policies 缺省 {}；L0 的 default_enabled=True 来自 ActionSpec，
        # 故 L0 动作在 Fake 下可直接调用；L1 动作须由测试显式以
        # policies 开启，与生产默认关口径一致）+ system 主体的
        # ActionsBundle。send_private 旧路径不经 gateway，行为不变。
        self.transport = FakeTransport(responses)
        self.action_gateway = ActionGateway(self.transport, policies=policies or {})
        self.actions = ActionsBundle(self.action_gateway, principal=PRINCIPAL_SYSTEM)

    async def start(self) -> None:
        """no-op。"""

    async def stop(self) -> None:
        """no-op。"""

    def normalize(self, raw: dict, *, require_keys: bool = True) -> InboundEvent | Ignore:
        """委托 normalize_onebot_event，与 OneBotAdapter 同语义。"""
        return normalize_onebot_event(raw, require_keys=require_keys)

    def classify(self, raw, *, require_keys: bool = True) -> ClassifiedEvent | Ignore:
        """委托 classify_onebot_event，与 OneBotAdapter 同语义。"""
        return classify_onebot_event(raw, require_keys=require_keys)

    async def send_private(
        self, session_id: str, segments, *, delivery: DeliveryHandle | None = None
    ) -> SendReceipt:
        """先记账，再按注入的延迟/结果返回回执；accepted 时生成 fake 平台 id。

        序号在记账时（首个 await 之前）捕获，延迟并发发送也不会重号
        （Codex P2 审）。
        """
        self.sends.append(
            {"session_id": session_id, "segments": segments, "delivery": delivery}
        )
        sequence = len(self.sends)
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        platform_message_id = f"fake-{sequence}" if self.outcome == "accepted" else None
        return SendReceipt(self.outcome, platform_message_id)

    async def send_segments(
        self, target, segments, *, priority: str = "normal",
        quota_token=None, preflight=None,
    ) -> dict:
        """DeliveryService.send_to 的 Fake sender 通道：只记账，返回
        旧 dict 语义（与 HttpOneBotClient.send_segments 同口径，经
        receipt_from_result 映射）：accepted → status ok + 平台 id；
        rejected → status unavailable；ambiguous → 无 status 的异形
        dict（映射为 ambiguous）。"""
        self.segment_sends.append(
            {"target": target, "segments": tuple(segments), "priority": priority}
        )
        sequence = len(self.segment_sends)
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        if self.outcome == "accepted":
            return {"status": "ok", "data": {"message_id": 9000 + sequence}}
        if self.outcome == "rejected":
            return {"status": "unavailable"}
        return {}

    async def health(self) -> AdapterHealth:
        """返回注入的 health_value。"""
        return self.health_value
