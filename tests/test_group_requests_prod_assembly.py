"""进程级装配自检：群回复开启 + 好友申请全自动同意（默认跳过）。

本自检防的是 N4 式的接缝缺陷：群回复通路与 request 自动处置在
框架内完备（手搓 settings / Fake 装配的用例全绿），但**生产装配**
可以恒关 / 恒 None——``tools.group_chat.reply_enabled`` 不开则
``group_responder`` 恒 None、群消息只观察；``requests`` 全 manual
则 ``request_channel`` 恒 None、申请只落库待人工。1521+ 项全绿也
测不出这类缺陷，必须以**生产 config.yaml 的实际加载路径**装配
进程再验证（与 tests/test_vision_prod_assembly.py 同门控、同范式）。

部署后复跑口径：本文件断言的是部署后的配置状态——生产 config.yaml
须已改成 tools.group_chat 开启且 reply_enabled=true、
actions.overrides.set_friend_add_request 开启且 dry_run=false、
requests.friend="approve_all" / group="manual"。部署前对旧值配置
开启门控跑本文件会红，那是预期信号（证明开关还没落到生产配置），
不是回归；部署完成后复跑须全绿。

门控：仅当环境 QQBOT_PROD_ASSEMBLY_CHECK=1 时运行（常规全套与
CI 自动跳过）。运行环境须复刻 companion-launch.py 的仓外凭证
注入：本文件在门控开启时按启动器同法把凭证文件的值并入进程
环境（只并入、永不打印、不落任何文件副本）。

装配口径：create_app **不注入 sender**，让 EventProcessor 走真实
HttpOneBotClient 装配出真实 ActionGateway 与生产 policies；仅在
装配完成后把网关传输层换成 FakeTransport（只换传输层，网关、
policies、账本保持生产装配），brain 注入 StubLLM、alerter 注入
替身，全程不出网、不碰真实 QQ。
"""

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.timeout(30),
    pytest.mark.skipif(
        os.environ.get("QQBOT_PROD_ASSEMBLY_CHECK") != "1",
        reason="进程级装配自检需显式门控 QQBOT_PROD_ASSEMBLY_CHECK=1",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[1]
PROD_CONFIG = REPO_ROOT / "config.yaml"
CREDENTIALS_FILE = Path(
    os.environ.get(
        "QQBOT_CREDENTIALS_FILE",
        str(Path.home() / ".local/share/qqbot-service/companion-credentials.json"),
    )
)

# 假 ID（与既有测试同口径的虚构值，绝不使用真实 QQ / 群号）。
SELF_ID = "900000001"
GROUP_ID = "80001"
GROUP_USER_ID = "90002"
FRIEND_USER_ID = "90003"
GROUP_REQUEST_USER_ID = "90004"
GROUP_REQUEST_GROUP_ID = "80002"
FRIEND_FLAG = "assembly-friend-flag-1"
GROUP_FLAG = "assembly-group-flag-1"


def _inject_launcher_env() -> None:
    """复刻 companion-launch.py 的凭证注入（值只进进程环境）。

    启动器口径：子进程环境 = 当前环境被凭证 JSON 的字符串值
    **覆盖更新**（非 setdefault），再以 ONEBOT_SECRET 覆盖为部署
    根下专用密钥文件的内容。门控自检内断言两处来源文件存在——
    缺失即装配环境不成立，宁可红也不许靠环境残留值假绿
    （与 vision 自检同口径）。密钥文件名以拼接方式给出，不单独
    写入源码。
    """
    assert CREDENTIALS_FILE.exists(), "仓外凭证文件缺失，装配环境不成立"
    saved = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    for key, value in saved.items():
        if isinstance(value, str):
            os.environ[key] = value
    secret_file = REPO_ROOT.parent / (".onebot_" + "secret")
    assert secret_file.exists(), "部署根下 OneBot 密钥文件缺失"
    os.environ["ONEBOT_SECRET"] = secret_file.read_text(encoding="utf-8").strip()


class _StubAlerter:
    async def alert(self, user_id, reason, summary="sensitive text omitted"):
        return None


class _StubLLM:
    """brain 的 client 替身：群 responder 的无记忆生成只经此产出。"""

    async def generate(self, _prompt):
        return "测试回复"

    async def generate_json(self, _prompt):
        return json.dumps({"bubbles": ["测试回复"]}, ensure_ascii=False)


def _load_prod_base():
    """以生产 config.yaml 的实际加载路径构造 Settings（未重定向）。"""
    _inject_launcher_env()
    assert PROD_CONFIG.exists(), "生产 config.yaml 缺失（拒绝回落 example 冒充）"
    os.environ["CONFIG_PATH"] = str(PROD_CONFIG)

    from companion.config import load_settings

    return load_settings()


def _build_prod_app(tmp_path):
    """生产装配进程：真实 sender 装配后仅换传输层为 FakeTransport。

    存储 / 日志 / 延时重定向到 tmp（与 vision 自检同范式）；不注入
    sender，注入 brain（StubLLM）与 alerter 替身。返回
    (app, processor, fake_transport, stub_llm)。
    """
    from companion.adapters.fake import FakeTransport
    from companion.brain import CompanionBrain
    from companion.ingress import create_app

    base = _load_prod_base()
    settings = replace(
        base,
        root=tmp_path,
        app={
            **base.app,
            "sqlite_path": "memory.db",
            "logs_dir": "logs",
            "response_delay_seconds": {"min": 0, "max": 0},
        },
    )
    stub_llm = _StubLLM()
    app = create_app(
        settings,
        alerter=_StubAlerter(),
        brain=CompanionBrain(settings.persona, stub_llm),
    )
    processor = app.state.processor
    # 只换传输层：HttpOneBotClient.transport 与 action_gateway.transport
    # 构造时本是同一对象，两处同步换绑同一 FakeTransport，保持该
    # 不变量；网关、policies、账本与 request_channel 保持生产装配。
    fake_transport = FakeTransport()
    processor.sender.action_gateway.transport = fake_transport
    processor.sender.transport = fake_transport
    return app, processor, fake_transport, stub_llm


def _calls(transport, action):
    return [c for c in transport.calls if c["action"] == action]


def _group_raw(*, mentioned: bool, message_id: str) -> dict:
    """真机形态群消息报文（段数组；sender.role 给 member）。"""
    if mentioned:
        message = [
            {"type": "at", "data": {"qq": SELF_ID}},
            {"type": "text", "data": {"text": "在吗"}},
        ]
        raw_message = f"[CQ:at,qq={SELF_ID}]在吗"
    else:
        message = [{"type": "text", "data": {"text": "群里闲聊一句"}}]
        raw_message = "群里闲聊一句"
    return {
        "time": 1767225600,
        "self_id": SELF_ID,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": message_id,
        "group_id": int(GROUP_ID),
        "user_id": int(GROUP_USER_ID),
        "message": message,
        "raw_message": raw_message,
        "sender": {
            "user_id": int(GROUP_USER_ID),
            "nickname": "selfcheck",
            "card": "",
            "role": "member",
        },
        "font": 0,
    }


def _friend_request_raw() -> dict:
    return {
        "time": 1767225600,
        "self_id": SELF_ID,
        "post_type": "request",
        "request_type": "friend",
        "user_id": int(FRIEND_USER_ID),
        "comment": "fake friend request",
        "flag": FRIEND_FLAG,
    }


def _group_request_raw() -> dict:
    return {
        "time": 1767225600,
        "self_id": SELF_ID,
        "post_type": "request",
        "request_type": "group",
        "sub_type": "add",
        "user_id": int(GROUP_REQUEST_USER_ID),
        "group_id": int(GROUP_REQUEST_GROUP_ID),
        "comment": "fake group request",
        "flag": GROUP_FLAG,
    }


def test_prod_config_tree_enables_group_reply_and_friend_approve_all():
    """正式配置树断言：部署后 config 的三处开关必须落到位。"""
    base = _load_prod_base()
    assert base.config is not None
    group_chat = base.config.tools.group_chat
    assert (group_chat.enabled, group_chat.dry_run, group_chat.reply_enabled) == (
        True,
        False,
        True,
    )
    assert base.config.requests.friend == "approve_all"
    assert base.config.requests.group == "manual"
    override = base.config.actions.overrides["set_friend_add_request"]
    assert override.enabled is True
    assert override.dry_run is False


def test_prod_assembly_group_reply_sends_once_only_when_mentioned(tmp_path):
    """群回复装配：reply_enabled 开 → responder 自建，@ 才真发一条。

    群报文的归一化入口是 adapter.classify（normalize 只收私聊，
    群消息经 classify 的群分支归一化，与事件道运行期口径一致）。
    """
    from companion.adapters.base import InboundEvent
    from companion.ingress.events import handle_group_message

    app, processor, transport, stub_llm = _build_prod_app(tmp_path)
    assert processor.brain.client is stub_llm
    assert processor.group_reply_enabled is True
    assert processor.group_responder is not None

    async def scenario():
        ev = processor.adapter.classify(
            _group_raw(mentioned=True, message_id="assembly-group-1")
        )
        assert isinstance(ev, InboundEvent)
        assert ev.chat_type == "group"
        await handle_group_message(
            processor, ev, responder=processor.group_responder
        )
        calls = _calls(transport, "send_group_msg")
        assert len(calls) == 1
        assert calls[0]["params"]["group_id"] == int(GROUP_ID)

        # 未 @ 机器人：只观察、不回复，发送计数不变。
        ev_unmentioned = processor.adapter.classify(
            _group_raw(mentioned=False, message_id="assembly-group-2")
        )
        assert isinstance(ev_unmentioned, InboundEvent)
        await handle_group_message(
            processor, ev_unmentioned, responder=processor.group_responder
        )
        assert len(_calls(transport, "send_group_msg")) == 1

    asyncio.run(scenario())


def test_prod_assembly_friend_auto_approve_and_group_manual(tmp_path):
    """request 装配：好友全自动同意（幂等），群申请仍落人工。"""
    from companion.adapters.base import RequestEvent
    from companion.domain.requests import (
        DECISION_APPROVED,
        DECISION_MANUAL,
        REQUEST_STATUS_DONE,
        RESULT_POLICY_MANUAL,
        request_key,
    )
    from companion.ingress.events import GatewayRequestChannel, handle_request

    app, processor, transport, _stub_llm = _build_prod_app(tmp_path)
    assert app.state.request_channel is not None
    assert isinstance(app.state.request_channel, GatewayRequestChannel)
    assert app.state.request_policy.friend_mode == "approve_all"
    assert app.state.request_policy.group_mode == "manual"
    store = app.state.request_store
    channel = app.state.request_channel
    policy = app.state.request_policy

    async def scenario():
        # 好友申请：approve_all → 经生产装配通道真处置一次。
        ev = processor.adapter.classify(_friend_request_raw())
        assert isinstance(ev, RequestEvent)
        key = request_key(ev.self_id, ev.request_type, ev.sub_type, ev.flag)
        store.try_begin(key, user_id=ev.user_id, group_id=ev.group_id)
        await handle_request(
            processor, ev, store=store, channel=channel, policy=policy
        )
        calls = _calls(transport, "set_friend_add_request")
        assert len(calls) == 1
        assert calls[0]["params"]["approve"] is True
        record = store.get(key)
        assert record is not None
        assert record.status == REQUEST_STATUS_DONE
        assert record.decision == DECISION_APPROVED

        # 同一申请重复驱动：状态机占用兜底，平台不二次处置。
        store.try_begin(key, user_id=ev.user_id, group_id=ev.group_id)
        await handle_request(
            processor, ev, store=store, channel=channel, policy=policy
        )
        assert len(_calls(transport, "set_friend_add_request")) == 1
        assert store.get(key).status == REQUEST_STATUS_DONE

        # 群申请：group=manual → 只落库待人工，零平台处置调用。
        group_ev = processor.adapter.classify(_group_request_raw())
        assert isinstance(group_ev, RequestEvent)
        group_key = request_key(
            group_ev.self_id,
            group_ev.request_type,
            group_ev.sub_type,
            group_ev.flag,
        )
        store.try_begin(
            group_key, user_id=group_ev.user_id, group_id=group_ev.group_id
        )
        await handle_request(
            processor, group_ev, store=store, channel=channel, policy=policy
        )
        assert _calls(transport, "set_group_add_request") == []
        group_record = store.get(group_key)
        assert group_record is not None
        assert group_record.status == REQUEST_STATUS_DONE
        assert group_record.decision == DECISION_MANUAL
        assert group_record.result_code == RESULT_POLICY_MANUAL

    asyncio.run(scenario())
