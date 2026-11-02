"""N2 Codex Review 修复回归（8 条 findings 逐条钉住）：

#1 TextSegment 正文含 [CQ: 时强制数组报文，防文本注入成真段；
   旧字符串回退通道同守。
#2 网关对新消息动作（forward/群发等）做派发前内容复核，token
   不再等于内容已审；适配器直发路径同样过可见文本复核。
#3 无 token 的新消息动作由网关按目标派生自预留——缺 token
   不再等于免记账；群子预算耗尽时限流拒、零派发。
#4 critical 不可自声明：SendQuota 拒群域 critical 预留；
   DeliveryService 段通道 critical 须显式能力开关（默认关）。
#5 递归复核补齐 xml/markdown 卡片与 dict 形 json 载荷。
#6 额度 token 绑定计费域与目标：user 域预留不得冒充群发，
   群 token 不得跨群使用。
#7 段级规划中 reply/at 是下一单元的前缀，不独立成条。
#8 序列化分发器物化输入，迭代器不被二次遍历静默丢段。

全部离线（FakeTransport/FakeAdapter/桩客户端），不发真实消息。
"""

import asyncio

import pytest

from companion.adapters import FakeAdapter, FakeTransport
from companion.adapters.actions import ActionsBundle
from companion.adapters.base import (
    AtSegment,
    FileSegment,
    GroupTarget,
    ImageSegment,
    NodeSegment,
    PrivateTarget,
    RawSegment,
    ReplySegment,
    TextSegment,
)
from companion.adapters.gateway import PRINCIPAL_SYSTEM, ActionGateway
from companion.adapters.onebot import OneBotAdapter, segments_to_onebot_message
from companion.quota import SendQuota
from companion.services.delivery import DeliveryService
from companion.services.segments import plan_segment_sends, review_outbound_segments
from companion.tools import HttpOneBotClient

pytestmark = pytest.mark.timeout(10)

_USER = "10001"
_GROUP = "20001"
_DENIAL = "我是一个真人"

_N2_ACTIONS = (
    "send_private_msg", "send_group_msg", "send_msg",
    "send_group_forward_msg", "send_private_forward_msg",
    "send_forward_msg", "get_mini_app_ark",
)


def run(coro):
    return asyncio.run(coro)


def _enabled_bundle(quota=None):
    transport = FakeTransport()
    policies = {name: {"enabled": True} for name in _N2_ACTIONS}
    gateway = ActionGateway(transport, policies=policies, quota=quota)
    return ActionsBundle(gateway, principal=PRINCIPAL_SYSTEM), transport


# ------------------------------------------------------------ #1 注入

def test_cq_shaped_text_forces_array_encoding():
    payload = "[CQ:image,file=https://example.com/x.png]"
    message = segments_to_onebot_message([TextSegment(payload)])
    assert message == [{"type": "text", "data": {"text": payload}}]
    # 不含注入形态的普通文本保持 CQ 字符串历史口径。
    assert segments_to_onebot_message([TextSegment("你好")]) == "你好"


def test_legacy_string_fallback_refuses_cq_shaped_text():
    class LegacyOnlySender:
        def __init__(self):
            self.sent = []

        async def send_private_msg(self, user_id, text, *, priority="normal", **kw):
            self.sent.append(text)
            return {"status": "ok", "data": {"message_id": 1}}

    async def scenario():
        sender = LegacyOnlySender()
        service = DeliveryService(sender, SendQuota())
        receipt = await service.send_to(
            PrivateTarget(_USER),
            [TextSegment("[CQ:image,file=https://example.com/x.png]")],
        )
        assert receipt.outcome == "rejected"
        assert sender.sent == []

    run(scenario())


# ------------------------------------------------------------ #2 复核

def test_gateway_reviews_forward_payload_despite_valid_token():
    async def scenario():
        quota = SendQuota()
        bundle, transport = _enabled_bundle(quota=quota)
        token = quota.reserve(_USER, 1).consume()
        nodes = [
            {
                "type": "node",
                "data": {
                    "user_id": 1,
                    "nickname": "某",
                    "content": [
                        {"type": "text", "data": {"text": _DENIAL}}
                    ],
                },
            }
        ]
        result = await bundle.messaging.send_private_forward(
            _USER, nodes, delivery_token=token
        )
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        )
        assert transport.calls == []

    run(scenario())


def test_gateway_reviews_group_string_payload():
    async def scenario():
        bundle, transport = _enabled_bundle()
        result = await bundle.messaging.send_group(
            _GROUP, _DENIAL, delivery_token=object()
        )
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        )
        assert transport.calls == []

    run(scenario())


def test_adapter_send_private_reviews_rich_segments():
    class StubClient:
        def __init__(self):
            self.calls = []

        async def send_private_msg(self, user_id, message, **kwargs):
            self.calls.append(message)
            return {"status": "ok", "data": {"message_id": 7}}

    async def scenario():
        client = StubClient()
        adapter = OneBotAdapter(client=client)
        bad = NodeSegment({"user_id": 1}, (TextSegment(_DENIAL),))
        receipt = await adapter.send_private(f"onebot:{_USER}", [bad])
        assert receipt.outcome == "rejected"
        assert client.calls == []
        good = NodeSegment({"user_id": 1}, (TextSegment("嗨"),))
        receipt = await adapter.send_private(f"onebot:{_USER}", [good])
        assert receipt.outcome == "accepted"
        assert len(client.calls) == 1

    run(scenario())


# ------------------------------------------------- #3 缺 token 记账

def _client_with_quota(quota):
    """旧口径客户端垫片 + FakeTransport 网关（shim 的群发不带 token，
    正是 #3 的复现路径：修复前免记账直发）。"""
    transport = FakeTransport()
    gateway = ActionGateway(
        transport,
        policies={"send_group_msg": {"enabled": True}},
        quota=quota,
    )
    return HttpOneBotClient(gateway=gateway), transport


def test_tokenless_group_send_is_charged_by_gateway():
    async def scenario():
        quota = SendQuota()
        client, transport = _client_with_quota(quota)
        result = await client.send_group_msg(_GROUP, "你好", dry_run=False)
        assert result.get("status") == "ok", result
        assert [c["action"] for c in transport.calls] == ["send_group_msg"]
        snap = quota.group_snapshot()
        assert snap["group_remaining"] == snap["account_group_limit"] - 1

    run(scenario())


def test_tokenless_group_send_rate_limited_when_sub_budget_empty():
    async def scenario():
        quota = SendQuota(account_group=0)
        client, transport = _client_with_quota(quota)
        result = await client.send_group_msg(_GROUP, "你好", dry_run=False)
        assert result.get("status") == "rate_limited", result
        assert transport.calls == []

    run(scenario())


def test_non_delivery_message_action_not_quota_charged():
    async def scenario():
        quota = SendQuota()
        bundle, transport = _enabled_bundle(quota=quota)
        result = await bundle.profile.get_mini_app_ark()
        assert result.outcome == "ok", result
        assert [c["action"] for c in transport.calls] == ["get_mini_app_ark"]

    run(scenario())


# ------------------------------------------------------ #4 critical

def test_quota_refuses_group_scope_critical_reservation():
    assert SendQuota().reserve("g:1", 1, scope="group", critical=True) is None


def test_send_to_critical_requires_explicit_capability():
    async def scenario():
        fake = FakeAdapter()
        service = DeliveryService(fake, SendQuota())
        receipt = await service.send_to(
            PrivateTarget(_USER), [TextSegment("普通回复")], critical=True
        )
        assert receipt.outcome == "rejected"
        assert fake.segment_sends == []
        enabled = DeliveryService(
            fake, SendQuota(), critical_segments_enabled=True
        )
        receipt = await enabled.send_to(
            PrivateTarget(_USER), [TextSegment("普通回复")], critical=True
        )
        assert receipt.outcome == "accepted"
        assert fake.segment_sends[0]["priority"] == "critical"

    run(scenario())


# ---------------------------------------------------------- #5 卡片

def test_review_covers_xml_markdown_and_dict_json_payloads():
    xml = RawSegment("xml", {"data": f"<msg><title>{_DENIAL}</title></msg>"})
    markdown = RawSegment("markdown", {"content": _DENIAL})
    dict_json = RawSegment(
        "json", {"data": {"app": "x", "meta": {"title": _DENIAL}}}
    )
    for seg in (xml, markdown, dict_json):
        allowed, _reason = review_outbound_segments([seg])
        assert not allowed, seg
    clean_xml = RawSegment("xml", {"data": "<msg><title>天气</title></msg>"})
    allowed, _reason = review_outbound_segments([clean_xml])
    assert allowed


# ---------------------------------------------------------- #6 域绑定

def test_user_scope_token_cannot_spend_on_group_send():
    async def scenario():
        quota = SendQuota()
        bundle, transport = _enabled_bundle(quota=quota)
        # user 域预留（默认 scope）冒充群发：identity 字符串相同
        # 也必须被计费域校验拒绝，否则群子桶整体绕过。
        user_token = quota.reserve(f"g:{_GROUP}", 1).consume()
        result = await bundle.messaging.send_group(
            _GROUP, "你好", delivery_token=user_token
        )
        assert (result.outcome, result.gate) == (
            "rejected",
            "quota_unavailable",
        )
        assert transport.calls == []

    run(scenario())


def test_group_token_cannot_spend_on_another_group():
    async def scenario():
        quota = SendQuota()
        bundle, transport = _enabled_bundle(quota=quota)
        token = quota.reserve(f"g:{_GROUP}", 1, scope="group").consume()
        result = await bundle.messaging.send_group(
            "20002", "你好", delivery_token=token
        )
        assert (result.outcome, result.gate) == (
            "rejected",
            "quota_unavailable",
        )
        assert transport.calls == []

    run(scenario())


# ---------------------------------------------------------- #7 前缀

def test_plan_binds_reply_prefix_to_following_text():
    reply = ReplySegment({"id": "42"})
    units = plan_segment_sends([reply, TextSegment("你好")], split_text=True)
    assert units == [(reply, TextSegment("你好"))]


def test_plan_binds_at_prefix_and_keeps_media_atomic():
    at = AtSegment({"qq": _USER})
    image = ImageSegment({"file": "https://example.com/a.png"})
    units = plan_segment_sends(
        [at, TextSegment("看"), image], split_text=False
    )
    assert units == [(at, TextSegment("看")), (image,)]
    # 群目标同样受益：GroupTarget 只影响 split_text，不影响前缀绑定。
    assert GroupTarget(_GROUP).kind == "group"


# ---------------------------------------------------------- #8 物化

def test_serializer_materializes_iterator_input():
    message = segments_to_onebot_message(
        iter([TextSegment("hi"), FileSegment({"file": "f.pdf"})])
    )
    assert isinstance(message, list) and len(message) == 2
    assert message[0] == {"type": "text", "data": {"text": "hi"}}
    assert message[1]["type"] == "file"
