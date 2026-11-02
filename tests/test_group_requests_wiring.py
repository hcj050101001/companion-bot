"""群回复生产 responder 装配 + request 自动同意接线测试（2026-10-31）。

覆盖面（全部 Fake/stub，不出网、不连真实 NapCat）：

- EventProcessor 装配：tools.group_chat.reply_enabled 开时默认生产
  responder 被构造（可调用、非 None），关时恒 None（与接线前一致），
  外部注入 responder 时以注入为准；
- 群回复 E2E：默认 responder 经 handle_group_message 通路在 @ 事件
  时恰发送一次（内容为 stub brain 文本），未 @ 时零发送；responder
  拿到的 prompt 不含任何私聊 history（短期记忆段为空）且正文不
  带 @ 前缀（文本取自 TextSegment 拼接）；
- create_app 的 request 装配：requests.friend="approve_all" 时
  GatewayRequestChannel 被装配（gateway/principal/confirmed/
  authorization 逐项断言）、policy 对好友 approve/对群 manual；
  requests 缺省时 channel 为 None、policy 全 manual（与接线前一致）；
  自动处置已配置但 sender 无网关时启动期 fail fast（RuntimeError）；
- request E2E：create_app 装配出的 channel（网关换成显式 policies
  的 Fake 网关——生产 policies 由生产 config 的 actions.overrides
  承担，不在本测试范围）+ SqliteRequestStore 落 tmp DB：好友事件
  恰派发一次 set_friend_add_request（approve=True）、重复驱动不
  二次派发、群事件零派发且落 done/policy_manual；
- 崩溃恢复：processing 未 complete 即弃置 store，同 DB 新 store
  代次推进后旧记录转 ambiguous，再次 handle_request 不调用网关、
  记录保持 ambiguous；
- Review 回归（2026-10-31）：危机关键词 @ 消息不进 responder
  （P2-1）、CQ 字符串形 @ 触发与 @全体不触发（P3-1）、原始 dict
  注入 group=approve_all 被 create_app 拒绝（P3-2）、工具路径与
  基础人设断言钉死 + webhook→consumer 消费道 E2E（P4-2）、注入
  responder 在开关关时不被消费（P4-3）。
"""

import asyncio
from dataclasses import replace

import pytest
import yaml

from companion.adapters.base import AtSegment, InboundEvent, TextSegment
from companion.adapters.fake import FakeAdapter
from companion.brain import CompanionBrain
from companion.config import load_settings
from companion.domain.requests import request_key
from companion.ingress import EventProcessor, create_app
from companion.ingress import events as events_mod
from companion.memory import Database
from companion.memory.requests import SqliteRequestStore
from companion.quota import SendQuota
from companion.services.delivery import DeliveryService

SELF_ID = "900000001"
GROUP = "80001"
USER = "90001"
OTHER_USER = "90002"

STUB_REPLY = "群里你好呀"


def run(coro):
    return asyncio.run(coro)


async def _no_sleep(_delay):
    pass


class StubLLM:
    """brain client 替身：记录 prompt、返回固定文本。

    supports_tools=True 且 generate_with_tools 记录调用（Review
    P4-2）：默认群 responder 若回归到工具路径，tool_calls 非空会
    被 E2E 断言钉住，不再有「替身不支持工具而自动回落」的假绿。
    """

    def __init__(self, reply=STUB_REPLY):
        self.reply = reply
        self.prompts = []
        self.tool_calls = []
        self.supports_tools = True

    async def generate(self, prompt):
        self.prompts.append(prompt)
        return self.reply

    async def generate_json(self, prompt):
        self.prompts.append(prompt)
        return self.reply

    async def generate_with_tools(self, prompt, tools=None, **_kwargs):
        self.tool_calls.append(prompt)
        return self.reply


class FakeAlerter:
    def __init__(self):
        self.items = []

    async def alert(self, user_id, reason, summary="sensitive text omitted"):
        self.items.append((user_id, reason, summary))


class BareSender:
    """无 action_gateway 的 sender 替身（只有旧私聊发送面）。"""

    def __init__(self):
        self.calls = []

    async def send_private_msg(self, user_id, message, *, priority="normal"):
        self.calls.append({"user_id": int(user_id), "message": message})
        return {"status": "ok"}

    async def send_alert_private_msg(self, user_id, message):
        return await self.send_private_msg(user_id, message)


def _make_settings(tmp_path, monkeypatch, app_overrides=None):
    """参照 tests/test_app.py 的 settings 构造：dict 型 app 注入。"""
    monkeypatch.setenv("ONEBOT_SECRET", "test-secret")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ALERT_QQ", raising=False)
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"allowed_private_user_ids": ["10001"]}), encoding="utf-8"
    )
    base = load_settings()
    app = {
        **base.app,
        "allowed_private_user_ids": ["10001"],
        "sqlite_path": "memory.db",
        "logs_dir": "logs",
        "response_delay_seconds": {"min": 0, "max": 0},
    }
    if app_overrides:
        app.update(app_overrides)
    return replace(
        base,
        onebot_secret="test-secret",
        dev=False,
        root=tmp_path,
        app=app,
        safety={
            **base.safety,
            "helplines": ["12356", "110", "120"],
            "crisis_keywords": ["活着没意思", "想自杀", "准备好了"],
            "categories": [
                {"name": "低落", "severity": 1, "keywords": ["活着没意思"]},
                {
                    "name": "明确自伤计划",
                    "severity": 3,
                    "keywords": ["想自杀", "准备好了"],
                },
            ],
            "crisis_copy": {
                "notify_enabled": "已经通知了真人。",
                "notify_disabled": "我是 AI，没办法替你联系别人。",
            },
        },
    )


def _make_processor(settings, stub_llm=None, **kwargs):
    stub_llm = stub_llm or StubLLM()
    processor = EventProcessor(
        settings,
        brain=CompanionBrain(settings.persona, stub_llm),
        sender=kwargs.pop("sender", FakeAdapter()),
        alerter=FakeAlerter(),
        sleep=_no_sleep,
        random_uniform=lambda _a, _b: 0,
        **kwargs,
    )
    return processor, stub_llm


def _make_app(tmp_path, monkeypatch, app_overrides=None, sender=None):
    settings = _make_settings(tmp_path, monkeypatch, app_overrides)
    app = create_app(
        settings,
        sender=sender if sender is not None else FakeAdapter(),
        brain=CompanionBrain(settings.persona, StubLLM()),
        alerter=FakeAlerter(),
        sleep=_no_sleep,
        random_uniform=lambda _a, _b: 0,
    )
    return app, settings


def _group_event(segments, text, user=USER, role="member"):
    return InboundEvent(
        platform="onebot",
        self_id=SELF_ID,
        user_id=user,
        session_id=f"onebot:g{GROUP}",
        segments=tuple(segments),
        text=text,
        has_media=False,
        message_id="gm-1",
        raw={
            "post_type": "message",
            "message_type": "group",
            "sender": {"user_id": int(user), "role": role},
        },
        chat_type="group",
        group_id=GROUP,
    )


def _request_event(flag, request_type="friend", user=USER, group=None):
    raw = {
        "time": 1767225600,
        "self_id": int(SELF_ID),
        "post_type": "request",
        "request_type": request_type,
        "user_id": int(user),
        "comment": "fake request",
        "flag": flag,
    }
    if request_type == "group":
        raw["sub_type"] = "add"
        raw["group_id"] = int(group or GROUP)
    return FakeAdapter().classify(raw)


class _ProcStub:
    """handle_request 用 proc 替身：metrics + _log 两面。"""

    def __init__(self):
        from companion.pipeline.observers import StageMetrics

        self.metrics = StageMetrics()
        self.records = []

    def _log(self, event_type, length, allowed, latency_ms, **kwargs):
        self.records.append({"event_type": event_type, **kwargs})


# ---------------------------------------------------------------------------
# EventProcessor 群 responder 装配
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_group_responder_assembled_when_reply_enabled(tmp_path, monkeypatch):
    settings = _make_settings(
        tmp_path, monkeypatch, {"tools": {"group_chat": {"reply_enabled": True}}}
    )
    processor, _stub = _make_processor(settings)
    assert processor.group_reply_enabled is True
    assert processor.group_responder is not None
    assert callable(processor.group_responder)
    # 自建 DeliveryService 的群门与开关同步打开（既有口径不变）。
    assert processor.delivery._group_delivery_enabled is True


@pytest.mark.timeout(10)
def test_group_responder_none_when_reply_disabled(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path, monkeypatch)
    processor, _stub = _make_processor(settings)
    assert processor.group_reply_enabled is False
    assert processor.group_responder is None
    assert processor.delivery._group_delivery_enabled is False


@pytest.mark.timeout(10)
def test_group_responder_injection_wins(tmp_path, monkeypatch):
    settings = _make_settings(
        tmp_path, monkeypatch, {"tools": {"group_chat": {"reply_enabled": True}}}
    )

    async def custom_responder(_ctx):
        return "注入的回复"

    processor, _stub = _make_processor(settings, group_responder=custom_responder)
    assert processor.group_responder is custom_responder


# ---------------------------------------------------------------------------
# 群回复 E2E（默认生产 responder + Fake 交付）
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_group_reply_e2e_default_responder(tmp_path, monkeypatch):
    settings = _make_settings(
        tmp_path, monkeypatch, {"tools": {"group_chat": {"reply_enabled": True}}}
    )
    processor, stub_llm = _make_processor(settings)
    fake = FakeAdapter()
    processor.delivery = DeliveryService(
        fake, SendQuota(), group_delivery_enabled=True
    )
    responder = processor.group_responder
    assert responder is not None

    # 未 @ 机器人：零发送、brain 不被调用。
    ev = _group_event([TextSegment("普通群聊")], text="普通群聊")
    run(events_mod.handle_group_message(processor, ev, responder=responder))
    assert fake.segment_sends == []
    assert stub_llm.prompts == []

    # @ 机器人：恰一次群发送，内容为 stub 文本；prompt 的短期记忆
    # 段为空（无私聊 history）、正文取 TextSegment 拼接（无 @ 前缀）。
    ev = _group_event(
        [AtSegment({"qq": SELF_ID}), TextSegment("在吗")], text="@阿年 在吗"
    )
    run(events_mod.handle_group_message(processor, ev, responder=responder))
    assert len(fake.segment_sends) == 1
    sent_texts = [
        segment.text
        for segment in fake.segment_sends[0]["segments"]
        if isinstance(segment, TextSegment)
    ]
    assert sent_texts == [STUB_REPLY]
    assert len(stub_llm.prompts) == 1
    assert "短期记忆：\n\n当前消息：在吗" in stub_llm.prompts[0]
    # 上下文隔离钉死（Review P4-2）：prompt 以基础人设开头（未带
    # 任何用户级 persona pack），且全程未走工具路径。
    assert stub_llm.prompts[0].startswith(settings.persona)
    assert stub_llm.tool_calls == []


# ---------------------------------------------------------------------------
# Review 回归（2026-10-31）：危机门 / CQ 字符串形 / 消费道 / 注入语义
# ---------------------------------------------------------------------------


def _wired_processor(tmp_path, monkeypatch, app_overrides=None, **kwargs):
    """reply_enabled 开 + Fake 交付替换的 processor（E2E 共用装配）。"""
    settings = _make_settings(
        tmp_path,
        monkeypatch,
        app_overrides or {"tools": {"group_chat": {"reply_enabled": True}}},
    )
    processor, stub_llm = _make_processor(settings, **kwargs)
    fake = FakeAdapter()
    processor.delivery = DeliveryService(
        fake, SendQuota(), group_delivery_enabled=True
    )
    return processor, stub_llm, fake


@pytest.mark.timeout(10)
def test_group_reply_crisis_keyword_never_reaches_responder(
    tmp_path, monkeypatch
):
    """危机不入群链（Review P2-1）：@ 消息命中危机关键词时只观察
    登记，responder/brain 零调用、零发送——不进 LLM 自由回复。"""
    processor, stub_llm, fake = _wired_processor(tmp_path, monkeypatch)
    responder = processor.group_responder
    assert responder is not None
    ev = _group_event(
        [AtSegment({"qq": SELF_ID}), TextSegment("活着没意思")],
        text="@阿年 活着没意思",
    )
    run(events_mod.handle_group_message(processor, ev, responder=responder))
    assert fake.segment_sends == []
    assert stub_llm.prompts == []
    assert stub_llm.tool_calls == []


@pytest.mark.timeout(10)
def test_group_reply_cq_string_form_triggers(tmp_path, monkeypatch):
    """CQ 字符串形群消息（Review P3-1）：[CQ:at,qq=<self>] 与数组形
    同口径触发；[CQ:at,qq=all] 不触发。"""
    processor, stub_llm, fake = _wired_processor(tmp_path, monkeypatch)
    responder = processor.group_responder
    assert responder is not None

    def classify_group(message: str):
        raw = {
            "time": 1767225600,
            "self_id": int(SELF_ID),
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": 777,
            "group_id": int(GROUP),
            "user_id": int(USER),
            "message": message,
            "raw_message": message,
            "sender": {"user_id": int(USER), "role": "member"},
            "font": 0,
        }
        return FakeAdapter().classify(raw)

    ev = classify_group(f"[CQ:at,qq={SELF_ID}] 在吗")
    assert events_mod.is_group_trigger(ev) is True
    run(events_mod.handle_group_message(processor, ev, responder=responder))
    assert len(fake.segment_sends) == 1
    assert len(stub_llm.prompts) == 1

    ev_all = classify_group("[CQ:at,qq=all] 在吗")
    assert events_mod.is_group_trigger(ev_all) is False
    run(events_mod.handle_group_message(processor, ev_all, responder=responder))
    assert len(fake.segment_sends) == 1  # 未新增发送
    assert len(stub_llm.prompts) == 1


@pytest.mark.timeout(10)
def test_group_responder_injection_not_consumed_when_disabled(
    tmp_path, monkeypatch
):
    """注入语义钉死（Review P4-3）：装配层注入优先（属性为注入
    对象），但事件道消费以 group_reply_enabled 门控——开关关时
    注入不被消费、零发送零 brain 调用。"""
    settings = _make_settings(tmp_path, monkeypatch)

    async def custom_responder(_ctx):
        return "不应发出"

    processor, stub_llm = _make_processor(
        settings, group_responder=custom_responder
    )
    assert processor.group_reply_enabled is False
    assert processor.group_responder is custom_responder
    fake = FakeAdapter()
    processor.delivery = DeliveryService(
        fake, SendQuota(), group_delivery_enabled=False
    )
    # 与 create_app 的 event_consumer 同式：开关关传 None。
    responder = (
        processor.group_responder if processor.group_reply_enabled else None
    )
    assert responder is None
    ev = _group_event(
        [AtSegment({"qq": SELF_ID}), TextSegment("在吗")], text="@阿年 在吗"
    )
    run(events_mod.handle_group_message(processor, ev, responder=responder))
    assert fake.segment_sends == []
    assert stub_llm.prompts == []


@pytest.mark.timeout(10)
def test_group_reply_via_event_consumer(tmp_path, monkeypatch):
    """消费道 E2E（Review P4-2）：经 webhook 投入的群 @ 事件由
    app.state.event_consumer 真正走到群回复发送（装配胶水被覆盖，
    不再只直调 handle_group_message）。"""
    import hashlib
    import hmac as hmac_mod

    from tests import test_app
    from tests.test_adapter_contract_p0 import _post, encode

    fake = FakeAdapter()
    app, settings = _make_app(
        tmp_path,
        monkeypatch,
        {"tools": {"group_chat": {"enabled": True, "dry_run": False,
                                   "reply_enabled": True}}},
        sender=fake,
    )
    event = {
        "time": 1767225600,
        "self_id": int(SELF_ID),
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 888,
        "group_id": int(GROUP),
        "user_id": int(USER),
        "message": [
            {"type": "at", "data": {"qq": SELF_ID}},
            {"type": "text", "data": {"text": "在吗"}},
        ],
        "raw_message": f"[CQ:at,qq={SELF_ID}]在吗",
        "sender": {"user_id": int(USER), "nickname": "wiring",
                   "card": "", "role": "member"},
        "font": 0,
    }
    body = encode(event)
    # 用 app 自身 settings 的 secret 签名（不跨模块借常量）。
    signature = "sha1=" + hmac_mod.new(
        settings.onebot_secret.encode(), body, hashlib.sha1
    ).hexdigest()

    async def scenario():
        response = await _post(app, body, {"X-Signature": signature})
        assert response.status_code == 204
        consumer = asyncio.create_task(app.state.event_consumer())
        try:
            await test_app.drain(app)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    run(scenario())
    assert len(fake.segment_sends) == 1
    sent_texts = [
        segment.text
        for segment in fake.segment_sends[0]["segments"]
        if isinstance(segment, TextSegment)
    ]
    assert sent_texts == [STUB_REPLY]


def _group_at_body(user, role, message_id):
    from tests.test_adapter_contract_p0 import encode

    event = {
        "time": 1767225600,
        "self_id": int(SELF_ID),
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": message_id,
        "group_id": int(GROUP),
        "user_id": int(user),
        "message": [
            {"type": "at", "data": {"qq": SELF_ID}},
            {"type": "text", "data": {"text": "在吗"}},
        ],
        "raw_message": f"[CQ:at,qq={SELF_ID}]在吗",
        "sender": {"user_id": int(user), "nickname": "wiring",
                   "card": "", "role": role},
        "font": 0,
    }
    return encode(event)


async def _post_and_drain(app, settings, body):
    import hashlib
    import hmac as hmac_mod

    from tests import test_app
    from tests.test_adapter_contract_p0 import _post

    signature = "sha1=" + hmac_mod.new(
        settings.onebot_secret.encode(), body, hashlib.sha1
    ).hexdigest()
    response = await _post(app, body, {"X-Signature": signature})
    assert response.status_code == 204
    await test_app.drain(app)


def test_group_reply_allowlist_forwarded_via_event_consumer(tmp_path, monkeypatch):
    """复审 P3 装配覆盖：create_app 消费道须把 processor 的准入
    名单透传给 handle_group_message——reply_allowed_persons=
    ["owner"] 时 stranger 的 @ 被消费道拦下（零发送）、owner
    的 @ 正常回复；名单未透传则 stranger 也会回复、本测试转红。"""
    fake = FakeAdapter()
    app, settings = _make_app(
        tmp_path,
        monkeypatch,
        {"tools": {"group_chat": {"enabled": True, "dry_run": False,
                                   "reply_enabled": True,
                                   "reply_allowed_persons": ["owner"]}}},
        sender=fake,
    )

    async def scenario():
        consumer = asyncio.create_task(app.state.event_consumer())
        try:
            await _post_and_drain(app, settings, _group_at_body(USER, "member", 901))
            assert fake.segment_sends == []
            await _post_and_drain(
                app, settings, _group_at_body("10001", "owner", 902)
            )
            assert len(fake.segment_sends) == 1
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    run(scenario())


def test_group_reply_blocked_persons_forwarded_via_event_consumer(
    tmp_path, monkeypatch
):
    """复审 P3 装配覆盖：reply_blocked_persons 加入 "stranger"
    经消费道透传后，stranger 的 @ 零发送（默认名单下本会回复，
    见 test_group_reply_via_event_consumer）。"""
    fake = FakeAdapter()
    app, settings = _make_app(
        tmp_path,
        monkeypatch,
        {"tools": {"group_chat": {"enabled": True, "dry_run": False,
                                   "reply_enabled": True,
                                   "reply_blocked_persons": ["system", "stranger"]}}},
        sender=fake,
    )

    async def scenario():
        consumer = asyncio.create_task(app.state.event_consumer())
        try:
            await _post_and_drain(app, settings, _group_at_body(USER, "member", 903))
            assert fake.segment_sends == []
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    run(scenario())


def test_group_reply_allowed_roles_forwarded_via_event_consumer(
    tmp_path, monkeypatch
):
    """复审 P3 装配覆盖（第 3 轮补钉）：reply_allowed_roles=
    ["owner"] 时 EventProcessor 须解析出非 None 名单、create_app
    消费道须透传给 handle_group_message——member 角色的 @ 零
    发送、owner 角色的 @ 恰一次回复。删 __init__.py 的
    allowed_roles 透传行或解析行，本测试必须转红（复审已以
    变异验证此缺口：删透传行时 90 项装配/接线测试全绿）。"""
    fake = FakeAdapter()
    app, settings = _make_app(
        tmp_path,
        monkeypatch,
        {"tools": {"group_chat": {"enabled": True, "dry_run": False,
                                   "reply_enabled": True,
                                   "reply_allowed_roles": ["owner"]}}},
        sender=fake,
    )
    assert app.state.processor.group_reply_allowed_roles == frozenset({"owner"})

    async def scenario():
        consumer = asyncio.create_task(app.state.event_consumer())
        try:
            await _post_and_drain(app, settings, _group_at_body(USER, "member", 904))
            assert fake.segment_sends == []
            await _post_and_drain(app, settings, _group_at_body(USER, "owner", 905))
            assert len(fake.segment_sends) == 1
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    run(scenario())


# ---------------------------------------------------------------------------
# create_app 的 request 装配
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_create_app_requests_default_manual_no_channel(tmp_path, monkeypatch):
    # sender 无网关也无妨：全 manual 时根本不装配通道（与接线前一致）。
    app, _settings = _make_app(tmp_path, monkeypatch, sender=BareSender())
    assert app.state.request_channel is None
    policy = app.state.request_policy
    assert policy.has_auto() is False
    assert policy.decide(_request_event("d-flag-1")) == "manual"
    assert (
        policy.decide(_request_event("d-flag-2", request_type="group")) == "manual"
    )


@pytest.mark.timeout(10)
def test_create_app_requests_friend_approve_all_wires_channel(
    tmp_path, monkeypatch
):
    sender = FakeAdapter()
    app, _settings = _make_app(
        tmp_path,
        monkeypatch,
        {"requests": {"friend": "approve_all"}},
        sender=sender,
    )
    channel = app.state.request_channel
    assert channel is not None
    assert isinstance(channel, events_mod.GatewayRequestChannel)
    assert channel.gateway is sender.action_gateway
    assert channel.principal == "system"
    assert channel.confirmed is True
    assert channel.authorization.authorizer == "requests-auto-policy"
    assert channel.authorization.account is True
    policy = app.state.request_policy
    assert policy.has_auto() is True
    assert policy.decide(_request_event("w-flag-1")) == "approve"
    assert (
        policy.decide(_request_event("w-flag-2", request_type="group")) == "manual"
    )


@pytest.mark.timeout(10)
def test_create_app_requests_auto_without_gateway_fails_fast(
    tmp_path, monkeypatch
):
    settings = _make_settings(
        tmp_path, monkeypatch, {"requests": {"friend": "approve_all"}}
    )
    with pytest.raises(RuntimeError, match="action_gateway"):
        create_app(
            settings,
            sender=BareSender(),
            brain=CompanionBrain(settings.persona, StubLLM()),
            alerter=FakeAlerter(),
            sleep=_no_sleep,
            random_uniform=lambda _a, _b: 0,
        )


@pytest.mark.timeout(10)
def test_create_app_rejects_group_approve_all(tmp_path, monkeypatch):
    """纵深防御（Review P3-2）：原始 dict 视图手工注入 group=
    approve_all 时 create_app 显式拒绝——「全自动同意只限好友
    申请」的边界不只依赖 Pydantic 配置层单一校验。"""
    settings = _make_settings(
        tmp_path,
        monkeypatch,
        {"requests": {"friend": "approve_all", "group": "approve_all"}},
    )
    with pytest.raises(RuntimeError, match="approve_all"):
        create_app(
            settings,
            sender=FakeAdapter(),
            brain=CompanionBrain(settings.persona, StubLLM()),
            alerter=FakeAlerter(),
            sleep=_no_sleep,
            random_uniform=lambda _a, _b: 0,
        )


# ---------------------------------------------------------------------------
# request E2E（装配出的 channel + 落库状态机 + FakeTransport）
# ---------------------------------------------------------------------------


def _wired_app_channel(tmp_path, monkeypatch):
    """create_app 装配出的 channel，网关换成显式 policies 的 Fake。

    测的是真实装配（policy/principal/confirmed/authorization 来自
    create_app）；Fake 网关的 policies 显式开 set_friend_add_request
    （enabled=true、dry_run=false）才能真派发到 FakeTransport——
    生产 policies 由生产 config 的 actions.overrides 承担。
    """
    app, _settings = _make_app(
        tmp_path,
        monkeypatch,
        {"requests": {"friend": "approve_all"}},
        sender=FakeAdapter(),
    )
    channel = app.state.request_channel
    assert channel is not None
    fake = FakeAdapter(
        policies={
            "set_friend_add_request": {"enabled": True, "dry_run": False},
            "set_group_add_request": {"enabled": True, "dry_run": False},
        }
    )
    channel.gateway = fake.action_gateway
    return app, channel, fake


@pytest.mark.timeout(10)
def test_request_e2e_friend_approved_once_group_manual(tmp_path, monkeypatch):
    app, channel, fake = _wired_app_channel(tmp_path, monkeypatch)
    policy = app.state.request_policy
    store = SqliteRequestStore(Database(tmp_path / "requests-wiring.db"))
    proc = _ProcStub()

    friend_ev = _request_event("e-flag-friend")
    friend_key = request_key(
        friend_ev.self_id, friend_ev.request_type, friend_ev.sub_type, friend_ev.flag
    )
    store.try_begin(friend_key, user_id=friend_ev.user_id, group_id=friend_ev.group_id)
    run(
        events_mod.handle_request(
            proc, friend_ev, store=store, channel=channel, policy=policy
        )
    )
    calls = [c for c in fake.transport.calls if c["action"] == "set_friend_add_request"]
    assert len(calls) == 1
    assert calls[0]["params"]["flag"] == "e-flag-friend"
    assert calls[0]["params"]["approve"] is True
    record = store.get(friend_key)
    assert record.status == "done" and record.decision == "approved"

    # 同一事件重复驱动：状态机占用兜底，平台不二次处置。
    run(
        events_mod.handle_request(
            proc, friend_ev, store=store, channel=channel, policy=policy
        )
    )
    calls = [c for c in fake.transport.calls if c["action"] == "set_friend_add_request"]
    assert len(calls) == 1

    # 群事件在 friend=approve_all 下仍 manual：零派发、落 done/policy_manual。
    group_ev = _request_event("e-flag-group", request_type="group")
    group_key = request_key(
        group_ev.self_id, group_ev.request_type, group_ev.sub_type, group_ev.flag
    )
    store.try_begin(group_key, user_id=group_ev.user_id, group_id=group_ev.group_id)
    run(
        events_mod.handle_request(
            proc, group_ev, store=store, channel=channel, policy=policy
        )
    )
    assert [c for c in fake.transport.calls if c["action"] == "set_group_add_request"] == []
    record = store.get(group_key)
    assert record.status == "done"
    assert record.decision == "manual"
    assert record.result_code == "policy_manual"


@pytest.mark.timeout(10)
def test_request_crash_recovery_keeps_ambiguous(tmp_path, monkeypatch):
    app, channel, fake = _wired_app_channel(tmp_path, monkeypatch)
    policy = app.state.request_policy
    db_path = tmp_path / "requests-crash.db"
    ev = _request_event("c-flag-1")
    key = request_key(ev.self_id, ev.request_type, ev.sub_type, ev.flag)

    # 第一代 store：登记 + 占用后不 complete 即弃置（模拟崩溃）。
    store1 = SqliteRequestStore(Database(db_path))
    store1.try_begin(key, user_id=ev.user_id, group_id=ev.group_id)
    assert store1.claim_processing(key) is True
    del store1

    # 同一 DB 新建 store（换 boot 身份 + 心跳阈值归零模拟新进程
    # 接管已失联的前任，补审第三段 P3 / 复审 P3 的心跳语义）：
    # 代次推进，旧 processing 转 ambiguous。
    monkeypatch.setattr(
        "companion.memory.requests._PROCESS_BOOT_ID", "another-boot"
    )
    monkeypatch.setattr("companion.memory.requests._BOOT_STALE_SECONDS", 0.0)
    store2 = SqliteRequestStore(Database(db_path))
    record = store2.get(key)
    assert record is not None and record.status == "ambiguous"

    # 再次处置同 key：claim 失败 → 不调用网关、记录保持 ambiguous。
    proc = _ProcStub()
    run(
        events_mod.handle_request(
            proc, ev, store=store2, channel=channel, policy=policy
        )
    )
    assert fake.transport.calls == []
    record = store2.get(key)
    assert record is not None and record.status == "ambiguous"
