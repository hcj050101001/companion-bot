"""N2 投递与门面契约（方案 §3.2 / C8 / §6 N2 验收）：

- SendQuota 两级额度：群子桶（per-group + 账号群发子预算）与私聊
  共享同一账号总池，合计永不超过总上限；critical 预留池不可被
  普通/群发送借用。
- DeliveryService.send_to（SendTarget + segments）：私聊逐项等价
  的段通道、群发送运行时默认硬关（投递开关 + Gateway policy 两层
  零派发）、Fake 端到端可达、critical 不可自声明提级、媒体门与
  递归复核门、预留按规划单元记账。
- N2 门面全量：message 类无句柄拒、Fake 契约三态、send_group 真
  token 单次消费与跨目标拒用。

全部离线（FakeTransport/FakeAdapter）：不出网、不发真实消息。
"""

import asyncio

import pytest

from companion.adapters import FakeAdapter, FakeTransport
from companion.adapters.actions import ActionsBundle
from companion.adapters.base import (
    FileSegment,
    GroupTarget,
    ImageSegment,
    NodeSegment,
    PrivateTarget,
    TextSegment,
)
from companion.adapters.gateway import PRINCIPAL_SYSTEM, ActionGateway
from companion.quota import SendQuota
from companion.services.delivery import DeliveryService
from companion.tools import HttpOneBotClient

pytestmark = pytest.mark.timeout(10)

_USER = "10001"
_GROUP = "20001"

_N2_ACTIONS = (
    "send_private_msg", "send_group_msg", "send_msg",
    "send_group_forward_msg", "send_private_forward_msg", "send_forward_msg",
    "forward_friend_single_msg", "forward_group_single_msg",
    "send_group_ai_record", "send_ark_share", "send_group_ark_share",
    "get_mini_app_ark", "send_like", "set_msg_emoji_like", "friend_poke",
    "group_poke", "send_poke", "mark_msg_as_read",
    "mark_private_msg_as_read", "mark_group_msg_as_read",
    "_mark_all_as_read", "set_input_status", "click_inline_keyboard_button",
    "create_collection", "add_custom_face", "set_custom_face_desc",
    "set_online_status", "set_diy_online_status", "_get_model_show",
    "_set_model_show", "get_ai_record",
)


def run(coro):
    return asyncio.run(coro)


def _enabled_bundle(responses=None, quota=None):
    transport = FakeTransport(responses)
    policies = {name: {"enabled": True} for name in _N2_ACTIONS}
    gateway = ActionGateway(transport, policies=policies, quota=quota)
    return ActionsBundle(gateway, principal=PRINCIPAL_SYSTEM), transport


# ------------------------------------------------------------ 两级额度


def test_group_buckets_per_group_and_account_sub_budget():
    quota = SendQuota(
        normal_per_user=100, account_normal=100,
        group_per_group=1, account_group=2,
    )
    assert quota.reserve("g:1", 1, scope="group") is not None
    assert quota.reserve("g:1", 1, scope="group") is None  # per-group 子桶满
    assert quota.reserve("g:2", 1, scope="group") is not None
    assert quota.reserve("g:3", 1, scope="group") is None  # 账号群发子预算满
    snap = quota.group_snapshot()
    assert snap["group_remaining"] == 0
    assert snap["per_group_limit"] == 1


def test_private_and_group_share_one_account_cap():
    # 私聊占满总池 → 群发无额度（子桶叠加不突破总上限）。
    quota = SendQuota(
        normal_per_user=100, account_normal=3,
        group_per_group=10, account_group=10,
    )
    assert quota.reserve("u1", 3) is not None
    assert quota.reserve("g:1", 1, scope="group") is None
    # 反之亦然：群发占满总池 → 私聊无额度。
    quota2 = SendQuota(
        normal_per_user=100, account_normal=3,
        group_per_group=10, account_group=10,
    )
    assert quota2.reserve("g:1", 3, scope="group") is not None
    assert quota2.reserve("u1", 1) is None


def test_critical_pool_never_borrowed_by_group_or_normal():
    quota = SendQuota(
        normal_per_user=100, account_normal=1, account_critical=2,
        group_per_group=10, account_group=10,
    )
    assert quota.reserve("g:1", 1, scope="group") is not None
    # 总池已空：群发/私聊普通预留都失败，critical 预留池纹丝不动。
    assert quota.reserve("g:1", 1, scope="group") is None
    assert quota.reserve("u1", 1) is None
    assert quota.snapshot()["critical_remaining"] == 2
    assert quota.reserve("u9", 1, critical=True) is not None


# ------------------------------------------------------------ DeliveryService


def test_send_to_private_text_via_fake_adapter():
    async def scenario():
        fake = FakeAdapter()
        delivery = DeliveryService(fake, SendQuota())
        receipt = await delivery.send_to(PrivateTarget(_USER), [TextSegment("你好")])
        assert receipt.outcome == "accepted"
        assert receipt.platform_message_id == "9001"
        assert len(fake.segment_sends) == 1
        assert fake.segment_sends[0]["target"] == PrivateTarget(_USER)

    run(scenario())


def test_send_to_private_legacy_sender_fallback():
    class LegacySender:
        def __init__(self):
            self.calls = []

        async def send_private_msg(self, user_id, text, *, priority="normal", **kw):
            self.calls.append((user_id, text, priority))
            return {"status": "ok", "data": {"message_id": 5}}

    async def scenario():
        sender = LegacySender()
        delivery = DeliveryService(sender, SendQuota())
        receipt = await delivery.send_to(PrivateTarget(_USER), [TextSegment("你好")])
        assert receipt.outcome == "accepted"
        assert sender.calls == [(_USER, "你好", "normal")]

    run(scenario())


def test_send_to_media_gate_default_closed():
    async def scenario():
        fake = FakeAdapter()
        delivery = DeliveryService(fake, SendQuota())
        blocked = await delivery.send_to(
            PrivateTarget(_USER), [ImageSegment({"file": "a.png"})]
        )
        assert blocked.outcome == "rejected"
        assert fake.segment_sends == []
        allowed = await delivery.send_to(
            PrivateTarget(_USER), [ImageSegment({"file": "a.png"})],
            allow_media=True,
        )
        assert allowed.outcome == "accepted"
        assert len(fake.segment_sends) == 1

    run(scenario())


def test_send_to_review_gate_blocks_unreviewed_text():
    async def scenario():
        fake = FakeAdapter()
        delivery = DeliveryService(fake, SendQuota())
        receipt = await delivery.send_to(
            PrivateTarget(_USER), [TextSegment("我是一个真人")]
        )
        assert receipt.outcome == "rejected"
        node = NodeSegment({"user_id": 1}, (TextSegment("我不是AI"),))
        receipt2 = await delivery.send_to(PrivateTarget(_USER), [node])
        assert receipt2.outcome == "rejected"
        assert fake.segment_sends == []

    run(scenario())


def test_send_to_reserves_per_planned_unit():
    async def scenario():
        quota = SendQuota()
        fake = FakeAdapter()
        delivery = DeliveryService(fake, quota)
        receipt = await delivery.send_to(
            PrivateTarget(_USER), [TextSegment("一。\n\n二。\n\n三。")]
        )
        assert receipt.outcome == "accepted"
        assert len(fake.segment_sends) == 3
        assert quota.snapshot()["normal_remaining"] == 60 - 3

    run(scenario())


def test_send_to_undersized_reservation_partial_is_ambiguous():
    async def scenario():
        fake = FakeAdapter()
        delivery = DeliveryService(fake, SendQuota())
        reservation = delivery.reserve_for(PrivateTarget(_USER), 1)
        receipt = await delivery.send_to(
            PrivateTarget(_USER),
            [TextSegment("一。\n\n二。")],
            reservation=reservation,
        )
        # 一个单元送达、一个单元无额度被拒：整条结果不干净 → ambiguous。
        assert receipt.outcome == "ambiguous"
        assert len(fake.segment_sends) == 1

    run(scenario())


def test_group_send_hard_off_by_default_at_delivery_layer():
    async def scenario():
        quota = SendQuota()
        transport = FakeTransport()
        gateway = ActionGateway(
            transport, policies={"send_group_msg": {"enabled": True}}, quota=quota
        )
        client = HttpOneBotClient(gateway=gateway)
        delivery = DeliveryService(client, quota)  # 群开关默认关
        receipt = await delivery.send_to(GroupTarget(_GROUP), [TextSegment("群消息")])
        assert receipt.outcome == "rejected"
        assert transport.calls == []  # 零派发
        assert quota.snapshot()["normal_remaining"] == 60  # 零预留

    run(scenario())


def test_group_send_hard_off_at_gateway_policy_layer():
    async def scenario():
        quota = SendQuota()
        transport = FakeTransport()
        gateway = ActionGateway(transport, policies={}, quota=quota)
        client = HttpOneBotClient(gateway=gateway)
        delivery = DeliveryService(client, quota, group_delivery_enabled=True)
        receipt = await delivery.send_to(GroupTarget(_GROUP), [TextSegment("群消息")])
        # 投递开关已开、但 Gateway policy 默认关：仍 rejected、零派发。
        assert receipt.outcome == "rejected"
        assert transport.calls == []

    run(scenario())


def test_group_send_end_to_end_via_fake_when_fully_enabled():
    async def scenario():
        quota = SendQuota()
        transport = FakeTransport()
        gateway = ActionGateway(
            transport, policies={"send_group_msg": {"enabled": True}}, quota=quota
        )
        client = HttpOneBotClient(gateway=gateway)
        delivery = DeliveryService(client, quota, group_delivery_enabled=True)
        receipt = await delivery.send_to(
            GroupTarget(_GROUP),
            [TextSegment("看文件"), FileSegment({"file": "f.pdf"})],
            allow_media=True,
        )
        assert receipt.outcome == "accepted"
        actions = [call["action"] for call in transport.calls]
        assert actions == ["send_group_msg", "send_group_msg"]
        first, second = transport.calls
        assert first["params"]["group_id"] == 20001
        assert first["params"]["message"] == "看文件"
        # file 段不可 CQ 化 → 该单元走 segment 数组报文。
        assert second["params"]["message"] == [
            {"type": "file", "data": {"file": "f.pdf"}}
        ]
        # 群发只扣普通总池，critical 预留池不动。
        assert quota.snapshot()["critical_remaining"] == 10
        assert quota.group_snapshot()["group_remaining"] == 20 - 2

    run(scenario())


def test_group_critical_cannot_be_self_declared():
    async def scenario():
        quota = SendQuota()
        fake = FakeAdapter()
        delivery = DeliveryService(fake, quota, group_delivery_enabled=True)
        receipt = await delivery.send_to(
            GroupTarget(_GROUP), [TextSegment("冒充告警")], critical=True
        )
        assert receipt.outcome == "rejected"
        assert fake.segment_sends == []
        assert delivery.reserve_for(GroupTarget(_GROUP), 1, critical=True) is None
        assert quota.snapshot()["critical_remaining"] == 10

    run(scenario())


# ------------------------------------------------------------ N2 门面


def test_message_facades_require_delivery_handle():
    async def scenario():
        bundle, transport = _enabled_bundle()
        m = bundle.messaging
        calls = [
            lambda: m.send_group(_GROUP, "hi"),
            lambda: m.send_msg("hi", user_id=_USER),
            lambda: m.send_private_forward(_USER, []),
            lambda: m.send_group_forward(_GROUP, []),
            lambda: m.send_forward([], user_id=_USER),
            lambda: m.forward_friend_single("1", _USER),
            lambda: m.forward_group_single("1", _GROUP),
            lambda: m.send_group_ai_record(_GROUP, "char", "text"),
            lambda: m.send_ark_share(user_id=_USER),
            lambda: m.send_group_ark_share(_GROUP),
        ]
        for make_call in calls:
            result = await make_call()
            assert (result.outcome, result.gate) == (
                "rejected",
                "delivery_handle_required",
            )
        assert transport.calls == []

    run(scenario())


def test_message_facades_reach_transport_with_handle():
    async def scenario():
        bundle, transport = _enabled_bundle()
        m = bundle.messaging
        token = object()
        nodes = [{"type": "node", "data": {"user_id": 1, "content": []}}]
        results = [
            await m.send_msg("hi", user_id=_USER, delivery_token=token),
            await m.send_msg("hi", group_id=_GROUP, delivery_token=token),
            await m.send_private_forward(_USER, nodes, delivery_token=token),
            await m.send_group_forward(_GROUP, nodes, delivery_token=token),
            await m.send_forward(nodes, user_id=_USER, delivery_token=token),
            await m.forward_friend_single("11", _USER, delivery_token=token),
            await m.forward_group_single("11", _GROUP, delivery_token=token),
            await m.send_group_ai_record(_GROUP, "char", "你好", delivery_token=token),
            await m.send_ark_share(user_id=_USER, delivery_token=token),
            await m.send_group_ark_share(_GROUP, delivery_token=token),
        ]
        assert all(r.outcome == "ok" for r in results), results
        assert [c["action"] for c in transport.calls] == [
            "send_msg", "send_msg",
            "send_private_forward_msg", "send_group_forward_msg",
            "send_forward_msg",
            "forward_friend_single_msg", "forward_group_single_msg",
            "send_group_ai_record", "send_ark_share", "send_group_ark_share",
        ]

    run(scenario())


def test_send_msg_target_validation():
    async def scenario():
        bundle, transport = _enabled_bundle()
        m = bundle.messaging
        neither = await m.send_msg("hi", delivery_token=object())
        both = await m.send_msg(
            "hi", user_id=_USER, group_id=_GROUP, delivery_token=object()
        )
        assert (neither.outcome, neither.gate) == ("rejected", "invalid_params")
        assert (both.outcome, both.gate) == ("rejected", "invalid_params")
        assert transport.calls == []

    run(scenario())


def test_interaction_facades_reach_transport():
    async def scenario():
        bundle, transport = _enabled_bundle()
        m, s, md, p = bundle.messaging, bundle.social, bundle.media, bundle.profile
        results = [
            await m.set_msg_emoji_like("11", "76"),
            await m.mark_msg_as_read("11"),
            await m.mark_private_msg_as_read(_USER),
            await m.mark_group_msg_as_read(_GROUP),
            await m.mark_all_as_read(),
            await m.set_input_status(_USER, event_type=1),
            await m.click_inline_keyboard_button(group_id=_GROUP, user_id=_USER),
            await s.send_like(_USER, times=1),
            await s.friend_poke(_USER),
            await s.group_poke(_GROUP, _USER),
            await s.poke(user_id=_USER, group_id=_GROUP),
            await s.poke(user_id=_USER),
            await s.create_collection(title="t"),
            await s.add_custom_face(file="a.png"),
            await s.set_custom_face_desc(face_id="1", desc="d"),
            await md.get_ai_record(_GROUP, "char", "你好"),
            await p.get_mini_app_ark(app_id="x"),
            await p.set_online_status(status=20),
            await p.set_diy_online_status(text="忙"),
            await p.get_model_show(model="x"),
            await p.set_model_show(model="x"),
        ]
        assert all(r.outcome == "ok" for r in results), results
        assert [c["action"] for c in transport.calls] == [
            "set_msg_emoji_like", "mark_msg_as_read",
            "mark_private_msg_as_read", "mark_group_msg_as_read",
            "_mark_all_as_read", "set_input_status",
            "click_inline_keyboard_button", "send_like", "friend_poke",
            "group_poke", "group_poke", "friend_poke",
            "create_collection", "add_custom_face", "set_custom_face_desc",
            "get_ai_record", "get_mini_app_ark", "set_online_status",
            "set_diy_online_status", "_get_model_show", "_set_model_show",
        ]
        neither = await s.poke()
        assert (neither.outcome, neither.gate) == ("rejected", "invalid_params")

    run(scenario())


def test_facade_three_states_via_fake_transport():
    async def scenario():
        from companion.adapters.transport import TransportError

        bundle, _ = _enabled_bundle(
            responses={
                "set_msg_emoji_like": {"status": "failed", "retcode": 1400},
                "send_like": TransportError("send_like"),
            }
        )
        rejected = await bundle.messaging.set_msg_emoji_like("11", "76")
        assert rejected.outcome == "rejected"
        ambiguous = await bundle.social.send_like(_USER)
        assert ambiguous.outcome == "ambiguous"

    run(scenario())


def test_send_group_with_real_quota_token_single_use():
    async def scenario():
        quota = SendQuota()
        bundle, transport = _enabled_bundle(quota=quota)
        reservation = quota.reserve(f"g:{_GROUP}", 1, scope="group")
        token = reservation.consume()
        sent = await bundle.messaging.send_group(
            _GROUP, "你好", delivery_token=token
        )
        assert sent.outcome == "ok", sent
        assert [c["action"] for c in transport.calls] == ["send_group_msg"]
        # 同 token 二次使用 → 单次消费校验拒。
        reused = await bundle.messaging.send_group(
            _GROUP, "你好", delivery_token=token
        )
        assert (reused.outcome, reused.gate) == ("rejected", "quota_unavailable")
        # 伪造 token → 拒。
        bogus = await bundle.messaging.send_group(
            _GROUP, "你好", delivery_token=object()
        )
        assert (bogus.outcome, bogus.gate) == ("rejected", "quota_unavailable")
        # 私聊 token 跨目标用于群发 → identity 不符拒。
        private_token = quota.reserve(_USER, 1).consume()
        cross = await bundle.messaging.send_group(
            _GROUP, "你好", delivery_token=private_token
        )
        assert (cross.outcome, cross.gate) == ("rejected", "quota_unavailable")
        assert [c["action"] for c in transport.calls] == ["send_group_msg"]

    run(scenario())


def test_gateway_param_rules_for_new_actions():
    async def scenario():
        bundle, transport = _enabled_bundle()
        # send_group_ai_record 的 text 缺失（None 被门面剔除）→
        # Gateway 已验证 schema 的必填校验拒。
        result = await bundle.messaging.send_group_ai_record(
            _GROUP, "char", None, delivery_token=object()
        )
        assert (result.outcome, result.gate) == ("rejected", "invalid_params")
        assert transport.calls == []

    run(scenario())
