"""最小内存版 PlatformAdapter 端到端演示（适配器契约，方案 §5.1/§5.2）。

运行方式（仓库根目录下，全部离线、零网络、零真实 QQ）：

    PYTHONPATH=. python examples/fake_adapter_demo.py

演示的契约点（companion/adapters/base.py）：

- PlatformAdapter 是 runtime_checkable 的 Protocol：不继承、只按
  结构实现 name / capabilities / start / stop / health / normalize /
  classify / send_private，isinstance 检查即通过。业务层只依赖这套
  契约，不接触平台私有字段。
- 入站分工（实际形态，以代码为准）：原始事件的解析确实归适配器——
  normalize(raw) 把平台原始 dict 归一化为统一 InboundEvent（消息段
  已译为 TextSegment 等一等段类型，text / has_media / images 已提取），
  不合格事件不抛异常、返回带 reason_code 的 Ignore；classify 是全
  事件面入口（message / notice / request / meta 四型），消息分支同
  样委托 normalize 的严格级。本演示的最小实现与仓库内 FakeAdapter
  （companion/adapters/fake.py）同口径：normalize / classify 直接
  委托 OneBot 适配器的共享归一化函数，不另写一套解析语义。
- 出站：send_private(session_id, segments) 只做发送并返回回执三态
  SendReceipt（accepted / rejected / ambiguous；结果不明一律
  ambiguous，绝不猜 accepted，适配器从不重试）。额度、优先级与
  三态记账归 DeliveryService 独占，不在适配器内——本演示只打印
  成功态 accepted，并把发送记录留在内存列表里供检查。
"""

import asyncio

from companion.adapters.base import (
    AdapterCapabilities,
    AdapterHealth,
    ClassifiedEvent,
    DeliveryHandle,
    Ignore,
    InboundEvent,
    PlatformAdapter,
    SendReceipt,
    TextSegment,
)
from companion.adapters.onebot import (
    EVENT_KINDS,
    SUPPORTED_SEGMENT_TYPES,
    classify_onebot_event,
    normalize_onebot_event,
)


class MinimalFakeAdapter:
    """最小内存版适配器：发送只记账、不出网；解析委托共享归一化函数。"""

    name = "fake-demo"
    capabilities = AdapterCapabilities(
        segment_types=SUPPORTED_SEGMENT_TYPES,
        event_kinds=EVENT_KINDS,
    )

    def __init__(self, outcome: str = "accepted"):
        self.sends: list[dict] = []
        self.outcome = outcome
        self.started = False

    async def start(self) -> None:
        """无连接型适配器：置位即可，无需建连。"""
        self.started = True

    async def stop(self) -> None:
        """无连接型适配器：清位即可。"""
        self.started = False

    async def health(self) -> AdapterHealth:
        """内存实现恒健康；detail 如实标注来源（未做任何网络探活）。"""
        return AdapterHealth(True, True, "in-memory fake: no network probe")

    def normalize(
        self, raw: dict, *, require_keys: bool = True
    ) -> InboundEvent | Ignore:
        """与 OneBotAdapter 同语义：委托共享归一化函数。"""
        return normalize_onebot_event(raw, require_keys=require_keys)

    def classify(
        self, raw, *, require_keys: bool = True
    ) -> ClassifiedEvent | Ignore:
        """与 OneBotAdapter 同语义：委托共享分类函数。"""
        return classify_onebot_event(raw, require_keys=require_keys)

    async def send_private(
        self, session_id: str, segments, *, delivery: DeliveryHandle | None = None
    ) -> SendReceipt:
        """先记账再回执：accepted 时生成 fake 平台消息 id，其余态为 None。"""
        self.sends.append(
            {"session_id": session_id, "segments": segments, "delivery": delivery}
        )
        sequence = len(self.sends)
        platform_message_id = (
            f"fake-demo-{sequence}" if self.outcome == "accepted" else None
        )
        return SendReceipt(self.outcome, platform_message_id)


def make_private_event() -> dict:
    """一条模拟的 OneBot 风格私聊事件（数组形消息段，标识均为合成值）。"""
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 12345,
        "self_id": "900000001",
        "user_id": "10001",
        "message": [{"type": "text", "data": {"text": "你好，今天过得怎么样？"}}],
        "raw_message": "你好，今天过得怎么样？",
        "sender": {"user_id": "10001", "nickname": "demo-user"},
    }


async def run_demo() -> None:
    adapter = MinimalFakeAdapter()
    print(f"isinstance(adapter, PlatformAdapter): {isinstance(adapter, PlatformAdapter)}")

    await adapter.start()
    health = await adapter.health()
    print(f"health: online={health.online} good={health.good} detail={health.detail!r}")

    raw = make_private_event()
    event = adapter.normalize(raw)
    if isinstance(event, Ignore):
        # 不合格事件的契约形态：不抛异常，只回原因码与事件类型摘要。
        print(f"normalize -> Ignore(reason_code={event.reason_code!r})")
        await adapter.stop()
        return

    print("normalize -> InboundEvent:")
    print(f"  platform={event.platform!r} event_kind={event.event_kind!r} "
          f"chat_type={event.chat_type!r}")
    print(f"  self_id={event.self_id!r} user_id={event.user_id!r} "
          f"message_id={event.message_id!r}")
    print(f"  session_id={event.session_id!r}")
    print(f"  text={event.text!r} has_media={event.has_media}")
    print(f"  segments={event.segments!r}")
    print(f"  images={event.images!r}")

    reply_segments = (TextSegment("我很好，谢谢你！（fake 回复，未发送到任何平台）"),)
    receipt = await adapter.send_private(event.session_id, reply_segments)
    print("send_private -> SendReceipt:")
    print(f"  outcome={receipt.outcome!r} "
          f"platform_message_id={receipt.platform_message_id!r}")
    print(f"  recorded in-memory sends: {len(adapter.sends)} "
          f"(session_id={adapter.sends[0]['session_id']!r})")

    await adapter.stop()


if __name__ == "__main__":
    asyncio.run(run_demo())
