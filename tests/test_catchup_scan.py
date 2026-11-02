"""断线重连补回扫描集成测试（设计 §9 fake-OneBot 用例矩阵，T5）。

手法（设计 §9 钉死）：stub ``HistorySource`` **只构造归一化结构**
（``NormalizedHistoryEntry``，其 ``event`` 以 ``InboundEvent(...)`` 直构，
不构造任何原始 OneBot 响应——AC28 的业务层纪律）+ 真实
``EventProcessor``（EchoStub 系 brain + 记录型 fake sender 的既有夹具
形态，装配镜像 ``tests.test_app.setup_app``）+ 临时 SQLite，驱动
``CatchupService.run_scan``，断言对象为 sender 发送记录、claim 表状态、
ledger 行、events 日志记录与 ``ScanReport`` 字段。

覆盖：AC5、AC6、AC7、AC8（自发/窗外/空）、AC9（claim/ledger/messages
三类独立构造）、AC10、AC11、AC12、AC13、AC14、AC15、AC16、AC18、
AC19、AC21、AC22、AC23、AC24、AC25、AC26a、AC26b、AC29。
AC→测试函数映射表见本实例自测报告 handoffs/04-dev-reports/T5.md。
全部离线：不连真实 NapCat/QQ、不发真实消息，会话/用户标识均为虚构值。
"""

import asyncio
import json
import re
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from companion.adapters.base import (
    AtSegment,
    ImageSegment,
    InboundEvent,
    SendReceipt,
    TextSegment,
)
from companion.adapters.history import NormalizedContact, NormalizedHistoryEntry
from companion.brain import CompanionBrain, EchoStub
from companion.config import load_settings
from companion.domain.claims import LANE_ORDINARY, ClaimOutcome
from companion.ingress import catchup as catchup_module
from companion.ingress import create_app
from companion.ingress.catchup import CatchupService, CatchupSettings
from companion.multipart import MULTIPART_INSTRUCTION
from companion.perception import ImageRef, UserMessage
from companion.pipeline import STAGE_ORDER
from companion.safety import ModelLLMJudge, crisis_reply
from companion.vision import PreparedImages

SECRET = "catchup-scan-test-secret"
SELF_ID = "900000001"
GROUP_ID = "80001"
W0 = 1_700_000_000.0
W1 = W0 + 600.0
DEFAULT_PREFIX = "（刚信号不好，才看到你之前的消息）"
DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

ALLOWED_USERS = [str(n) for n in range(10001, 10200)] + ["20001", "20002", "30001"]


# ---------------------------------------------------------------------------
# 夹具形态（镜像 tests.test_app.setup_app：EchoStub brain + fake sender）
# ---------------------------------------------------------------------------


class CountingEchoStub(EchoStub):
    """EchoStub + 调用计数/提示词记录；generate_json 剥离 multipart 指令尾。

    EchoStub.generate 只按「当前消息：」回显，multipart 提示词尾部的
    指令段会被一并回显进气泡并被管线判废；本替身在 generate_json 里
    先剥离该指令段再回显（与既有 FakeLLM 直回 bubbles 的夹具口径等价），
    气泡内换行压成空格（多气泡契约要求单气泡单行，合成文本的条目
    顺序断言改经 prompts 记录与回显内容联合判定）。
    """

    def __init__(self):
        self.calls = 0
        self.prompts = []

    async def generate(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        return await super().generate(prompt)

    async def generate_json(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        core = prompt.split("\n" + MULTIPART_INSTRUCTION)[0]
        answer = await EchoStub.generate(self, core)
        return json.dumps(
            {"bubbles": [answer.replace("\n", " ")]}, ensure_ascii=False
        )


class RecordingSender:
    """记录型 fake sender：私聊文本通道 + 段通道（群）双记录，不出网。"""

    def __init__(self):
        self.private_calls = []
        self.segment_calls = []

    async def send_private_msg(self, user_id, message, *, priority="normal"):
        self.private_calls.append({"user_id": str(user_id), "message": message})
        return {"status": "ok"}

    async def send_alert_private_msg(self, user_id, message):
        return await self.send_private_msg(user_id, message)

    async def send_segments(self, target, segments, *, priority="normal"):
        self.segment_calls.append(
            {"target": target, "segments": tuple(segments)}
        )
        return SendReceipt("accepted", f"fake-seg-{len(self.segment_calls)}")


class RecordingAlerter:
    def __init__(self):
        self.items = []

    async def alert(self, user_id, reason, summary="sensitive text omitted"):
        self.items.append((user_id, reason, summary))


class _SafeJudgeLLM:
    async def generate(self, _prompt):
        return "NO"


async def _no_sleep(_delay):
    return None


def make_rig(
    tmp_path,
    monkeypatch,
    *,
    group_enabled=False,
    catchup_yaml=None,
    catchup_app=None,
    config_none=False,
    sender=None,
    health_enabled=False,
    group_chat_app=None,
):
    """组装真实 EventProcessor（create_app）+ 全部 fake 替身。

    fix-round-1 扩展（装配用例用）：``catchup_yaml`` 写入 config.yaml
    的 reconnect_catchup 段（正式配置树形态）；``catchup_app`` 直接
    塞进 app 字典；``config_none=True`` 时把 Settings.config 置 None
    走组合根的 dict 回退解析。
    """
    monkeypatch.setenv("ONEBOT_SECRET", SECRET)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ALERT_QQ", raising=False)
    # 密封（复审轮 2 P2 修复）：其他测试文件经 load_settings 的
    # setdefault 可把 NAPCAT_BASE_URL=http://test 遗留在 os.environ，
    # 本 rig 的健康装配会读到坏 URL 而在全量顺序下必红；钉死回环值。
    monkeypatch.setenv("NAPCAT_BASE_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    config_yaml = {"allowed_private_user_ids": ALLOWED_USERS}
    if catchup_yaml is not None:
        config_yaml["reconnect_catchup"] = catchup_yaml
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(config_yaml),
        encoding="utf-8",
    )
    base = load_settings()
    app_dict = {
        **base.app,
        "allowed_private_user_ids": ALLOWED_USERS,
        "sqlite_path": "memory.db",
        "logs_dir": "logs",
        "response_delay_seconds": {"min": 0, "max": 0},
    }
    if catchup_app is not None:
        app_dict = {**app_dict, "reconnect_catchup": catchup_app}
    if config_none:
        base = replace(base, config=None)
    if group_enabled:
        group_chat_cfg = {"reply_enabled": True}
        if group_chat_app:
            group_chat_cfg.update(group_chat_app)
        app_dict = {
            **app_dict,
            "tools": {"group_chat": group_chat_cfg},
        }
    if health_enabled:
        app_dict = {**app_dict, "connection_health": {"enabled": True}}
    settings = replace(
        base,
        onebot_secret=SECRET,
        dev=False,
        root=tmp_path,
        app=app_dict,
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
    sender = sender if sender is not None else RecordingSender()
    alerter = RecordingAlerter()
    stub = CountingEchoStub()
    brain = CompanionBrain(settings.persona, stub)
    app = create_app(
        settings,
        sender=sender,
        alerter=alerter,
        sleep=_no_sleep,
        random_uniform=lambda _a, _b: 0,
        brain=brain,
    )
    processor = app.state.processor
    # 与 tests.test_app 同口径：judge 替身恒回 NO，避免无 key 的
    # DeepSeek FALLBACK 被当成疑似危机污染普通回合断言。
    processor.safety.judge = ModelLLMJudge(_SafeJudgeLLM())
    processor.limiter.limit = 50
    return SimpleNamespace(
        app=app,
        processor=processor,
        sender=sender,
        alerter=alerter,
        brain=stub,
        settings=settings,
        claim_store=app.state.claim_store,
        scan_store=app.state.catchup_scan_store,
    )


# ---------------------------------------------------------------------------
# stub HistorySource：只构造归一化结构（AC28 业务层纪律）
# ---------------------------------------------------------------------------


class StubHistorySource:
    """HistorySource 协议替身：归一化结构进出，计数全部拉取调用。

    ``friend_map`` / ``group_map`` 的值可为条目元组、Exception 实例
    （拉取抛错）或异步 callable（AC26 的挂起/门控场景）。
    """

    def __init__(self, contacts=(), friend_map=None, group_map=None):
        self._contacts = contacts
        self._friend = friend_map or {}
        self._group = group_map or {}
        self.contacts_calls = 0
        self.friend_calls = []
        self.group_calls = []

    async def recent_contacts(self):
        self.contacts_calls += 1
        if isinstance(self._contacts, Exception):
            raise self._contacts
        return self._contacts

    async def _resolve(self, mapping, key):
        value = mapping.get(key, ())
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return await value()
        return value

    async def friend_history(self, user_id):
        self.friend_calls.append(str(user_id))
        return await self._resolve(self._friend, str(user_id))

    async def group_history(self, group_id):
        self.group_calls.append(str(group_id))
        return await self._resolve(self._group, str(group_id))


def pentry(user_id, text, message_id, sent_at, *, images=(), has_media=False):
    """私聊归一化条目：InboundEvent 直构，不构造原始响应。"""
    segments = [TextSegment(text)] if text else []
    segments += [
        ImageSegment({"url": ref.ref, "file": ref.file_id}) for ref in images
    ]
    event = InboundEvent(
        platform="onebot",
        self_id=SELF_ID,
        user_id=str(user_id),
        session_id=str(user_id),
        segments=tuple(segments),
        text=text,
        has_media=has_media,
        message_id=message_id,
        raw={},
        images=tuple(images),
    )
    return NormalizedHistoryEntry(
        message_id=message_id,
        sender_id=str(user_id),
        sent_at=sent_at,
        at_known=True,
        event=event,
    )


def gentry(user_id, text, message_id, sent_at, *, with_at=True, at_known=True):
    """群聊归一化条目：段内含 AtSegment（@ 机器人）与否可控。"""
    segments = []
    if with_at:
        segments.append(AtSegment({"qq": SELF_ID}))
    if text:
        segments.append(TextSegment(text))
    event = InboundEvent(
        platform="onebot",
        self_id=SELF_ID,
        user_id=str(user_id),
        session_id=f"onebot:g{GROUP_ID}",
        segments=tuple(segments),
        text=text,
        has_media=False,
        message_id=message_id,
        raw={
            "post_type": "message",
            "message_type": "group",
            "sender": {"user_id": int(user_id), "role": "member"},
        },
        chat_type="group",
        group_id=GROUP_ID,
    )
    return NormalizedHistoryEntry(
        message_id=message_id,
        sender_id=str(user_id),
        sent_at=sent_at,
        at_known=at_known,
        event=event,
    )


def contact(kind, peer_id, last_activity_at):
    return NormalizedContact(kind, str(peer_id), last_activity_at)


def make_service(rig, source, **config_kwargs):
    config_kwargs.setdefault("enabled", True)
    return CatchupService(
        rig.processor,
        history=source,
        claim_store=rig.claim_store,
        scan_store=rig.scan_store,
        ledger=rig.processor.ledger,
        config=CatchupSettings(**config_kwargs),
    )


def window(k=0):
    """第 k 个互不重叠的扫描窗口（同 rig 多轮扫描时逐轮错开）。"""
    return W0 + k * 10_000.0, W1 + k * 10_000.0


def read_events(rig):
    path = rig.processor.log_path
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def private_sends(rig, user_id=None):
    calls = rig.sender.private_calls
    if user_id is not None:
        calls = [c for c in calls if c["user_id"] == str(user_id)]
    return [c["message"] for c in calls]


def claim_key(message_id, *, group=False):
    return ("onebot", SELF_ID, f"g:{message_id}" if group else message_id)


async def onboard(rig, *user_ids):
    for user_id in user_ids:
        await rig.processor.conversation.mark_onboarded(str(user_id))


# ---------------------------------------------------------------------------
# AC5：扫描幂等落盘——标记已存在时零历史动作
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac5_completed_mark_blocks_rescan_with_zero_history_calls(
    tmp_path, monkeypatch
):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    assert rig.scan_store.try_begin(start, end) is True
    assert rig.scan_store.complete(start, end, "completed") is True
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={"10001": (pentry("10001", "你好", "a5-1", start + 10),)},
    )

    async def scenario():
        await onboard(rig, "10001")
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.outcome == "store_blocked"
    assert source.contacts_calls == 0
    assert source.friend_calls == [] and source.group_calls == []
    assert private_sends(rig) == []
    with rig.processor.database.transaction() as conn:
        rows = conn.execute(
            "SELECT state, outcome FROM catchup_scans "
            "WHERE window_start=? AND window_end=?",
            (start, end),
        ).fetchall()
    assert rows == [("completed", "completed")]


# ---------------------------------------------------------------------------
# AC6：会话发现——仅窗口内会话进入拉取名单
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac6_only_in_window_contacts_are_fetched(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    source = StubHistorySource(
        contacts=(
            contact("private", "10001", start + 100),
            contact("private", "10002", start - 50),
            contact("group", GROUP_ID, end + 50),
        ),
        friend_map={
            "10001": (pentry("10001", "窗口内的消息", "a6-1", start + 10),),
            "10002": (pentry("10002", "窗口外的消息", "a6-2", start + 10),),
        },
        group_map={GROUP_ID: (gentry("90001", "群消息", "a6-3", start + 10),)},
    )

    async def scenario():
        await onboard(rig, "10001", "10002")
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.outcome == "completed"
    assert report.conversations_seen == 1
    assert source.friend_calls == ["10001"]
    assert source.group_calls == []
    assert len(private_sends(rig, "10001")) == 1
    assert private_sends(rig, "10002") == []


# ---------------------------------------------------------------------------
# AC7：发现降级——recent_contacts 不可用时试探本地私聊会话集合
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac7_fallback_probes_only_local_private_conversations(
    tmp_path, monkeypatch
):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    source = StubHistorySource(
        contacts=None,
        friend_map={
            "20001": (pentry("20001", "降级也能看到", "a7-1", start + 10),),
            "20002": (pentry("20002", "我也来了", "a7-2", start + 20),),
            "30001": (pentry("30001", "不应被试探", "a7-3", start + 30),),
        },
    )

    async def scenario():
        await onboard(rig, "20001", "20002", "30001")
        # 本地 messages 表只与 20001/20002 有往来（30001 无往来记录）
        await rig.processor.conversation.add_turn("20001", "之前聊过", "嗯")
        await rig.processor.conversation.add_turn("20002", "之前也聊过", "嗯")
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.outcome == "completed"
    assert sorted(source.friend_calls) == ["20001", "20002"]
    assert source.group_calls == []
    assert len(private_sends(rig, "20001")) == 1
    assert len(private_sends(rig, "20002")) == 1
    assert private_sends(rig, "30001") == []
    scan_logs = [r for r in read_events(rig) if r["event_type"] == "catchup_scan"]
    assert scan_logs and scan_logs[-1]["discovery"] == "fallback_private_only"


# ---------------------------------------------------------------------------
# AC8：逐条过滤——自发 / 窗外（早+晚）/ 空内容入选数均为 0
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac8_self_outside_window_and_empty_entries_filtered(
    tmp_path, monkeypatch
):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    self_entry = pentry(SELF_ID, "机器人自发的", "a8-self", start + 10)
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={
            "10001": (
                self_entry,
                pentry("10001", "太早了", "a8-early", start - 5),
                pentry("10001", "太晚了", "a8-late", end + 5),
                pentry("10001", "   ", "a8-empty", start + 20),
            )
        },
    )

    async def scenario():
        await onboard(rig, "10001")
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.outcome == "completed"
    assert report.entries_selected == 0
    assert report.turns_dispatched == 0
    assert report.entries_dropped["self_sent"] == 1
    assert report.entries_dropped["outside_window"] == 2
    assert report.entries_dropped["empty"] == 1
    assert private_sends(rig) == []


# ---------------------------------------------------------------------------
# AC9：已处理零重复——claim / ledger / messages 三类独立构造
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac9_claim_row_blocks_redelivery(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    key = claim_key("a9-claim")
    assert rig.claim_store.try_claim(key, LANE_ORDINARY) is ClaimOutcome.CLAIMED
    assert rig.claim_store.finish(key, "done") is True
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={
            "10001": (pentry("10001", "已处理过的", "a9-claim", start + 10),)
        },
    )

    async def scenario():
        await onboard(rig, "10001")
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.entries_dropped["already_processed"] == 1
    assert report.turns_dispatched == 0
    assert private_sends(rig) == []


@pytest.mark.timeout(10)
def test_ac9_ledger_row_from_real_dispatch_blocks_redelivery(
    tmp_path, monkeypatch
):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={
            "10001": (pentry("10001", "实时已回", "a9-ledger", start + 10),)
        },
    )

    async def scenario():
        await onboard(rig, "10001")
        # 经一次真实 dispatch 产生 ledger 行（turn_id = message_id）
        task = await rig.processor.dispatch_parsed(
            UserMessage(
                user_id="10001",
                text="实时已回",
                message_id="a9-ledger",
                has_media=False,
                images=(),
            )
        )
        assert task is not None and await task is True
        _had, state = await rig.processor.ledger.turn_had_send_with_state(
            "10001", "a9-ledger"
        )
        assert state is not None
        sends_before = len(private_sends(rig))
        report = await make_service(rig, source).run_scan(start, end)
        return report, sends_before

    report, sends_before = asyncio.run(scenario())
    assert sends_before == 1
    assert report.entries_dropped["already_processed"] == 1
    assert report.turns_dispatched == 0
    assert len(private_sends(rig)) == sends_before


@pytest.mark.timeout(10)
def test_ac9_messages_row_only_blocks_redelivery(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    # 第三类独立构造：仅 messages 行（turn_id 同 id），无 claim、无 ledger
    with rig.processor.database.transaction() as conn:
        conn.execute(
            "INSERT INTO messages (user_id, role, content, turn_id) "
            "VALUES (?, 'user', ?, ?)",
            ("10001", "很久以前处理过", "a9-msg"),
        )
    assert rig.scan_store.message_turn_exists("10001", "a9-msg") is True
    assert rig.claim_store.get(claim_key("a9-msg")) is None
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={
            "10001": (pentry("10001", "很久以前处理过", "a9-msg", start + 10),)
        },
    )

    async def scenario():
        await onboard(rig, "10001")
        _had, state = await rig.processor.ledger.turn_had_send_with_state(
            "10001", "a9-msg"
        )
        assert state is None
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.entries_dropped["already_processed"] == 1
    assert report.turns_dispatched == 0
    assert private_sends(rig) == []


# ---------------------------------------------------------------------------
# AC10：逐会话失败隔离——拉取抛错跳过不重试，其余会话照常
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac10_history_failure_isolated_per_conversation(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    source = StubHistorySource(
        contacts=(
            contact("private", "10001", start + 200),
            contact("private", "10002", start + 100),
        ),
        friend_map={
            "10001": RuntimeError("history boom"),
            "10002": (pentry("10002", "我这边正常", "a10-1", start + 10),),
        },
    )

    async def scenario():
        await onboard(rig, "10001", "10002")
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.outcome == "completed"
    assert source.friend_calls.count("10001") == 1  # 不重试
    assert report.entries_dropped["history_error"] == 1
    assert len(private_sends(rig, "10002")) == 1
    conv_logs = [
        r for r in read_events(rig) if r["event_type"] == "catchup_conversation"
    ]
    assert any(r["reason_code"] == "history_error" for r in conv_logs)


# ---------------------------------------------------------------------------
# AC11：私聊一会话一回合——3 条合成 1 回合、投递 ≤1、按序包含
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac11_private_messages_merge_into_single_turn(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={
            "10001": (
                pentry("10001", "第一条内容", "a11-1", start + 10),
                pentry("10001", "第二条内容", "a11-2", start + 20),
                pentry("10001", "第三条内容", "a11-3", start + 30),
            )
        },
    )

    async def scenario():
        await onboard(rig, "10001")
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.turns_dispatched == 1
    assert report.entries_selected == 3
    sends = private_sends(rig, "10001")
    assert len(sends) == 1
    # 合成输入按 sent_at 升序包含 3 条（经 brain 提示词记录断言）
    merged_prompts = [p for p in rig.brain.prompts if "第一条内容" in p]
    assert merged_prompts
    assert "第一条内容\n第二条内容\n第三条内容" in merged_prompts[0]
    reply = sends[0]
    positions = [
        reply.index(t) for t in ("第一条内容", "第二条内容", "第三条内容")
    ]
    assert positions == sorted(positions)


# ---------------------------------------------------------------------------
# AC12：走既有管线同一路径——Stage 轨迹与实时私聊一致
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac12_stage_trace_matches_realtime_private_turn(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    source = StubHistorySource(
        contacts=(contact("private", "10002", start + 100),),
        friend_map={
            "10002": (pentry("10002", "补回的一条", "a12-1", start + 10),)
        },
    )

    def stage_delta(before, after):
        return {
            k: after.get(k, 0) - before.get(k, 0)
            for k in set(before) | set(after)
            if after.get(k, 0) - before.get(k, 0)
        }

    async def scenario():
        await onboard(rig, "10001", "10002")
        before = dict(rig.processor.metrics.stage_counts)
        task = await rig.processor.dispatch_parsed(
            UserMessage(
                user_id="10001",
                text="实时的一条",
                message_id="a12-rt",
                has_media=False,
                images=(),
            )
        )
        assert task is not None and await task is True
        realtime_delta = stage_delta(
            before, dict(rig.processor.metrics.stage_counts)
        )
        before = dict(rig.processor.metrics.stage_counts)
        report = await make_service(rig, source).run_scan(start, end)
        catchup_delta = stage_delta(
            before, dict(rig.processor.metrics.stage_counts)
        )
        return report, realtime_delta, catchup_delta

    report, realtime_delta, catchup_delta = asyncio.run(scenario())
    assert report.turns_dispatched == 1
    expected = {stage: 1 for stage in STAGE_ORDER}
    assert realtime_delta == expected
    assert catchup_delta == expected


# ---------------------------------------------------------------------------
# AC13：回合标识 = 最新条 id；被合并 id 均处 claim 终态（设计 §9 钉死层级）
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac13_turn_key_and_merged_ids_claim_terminal(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={
            "10001": (
                pentry("10001", "合并一", "a13-1", start + 10),
                pentry("10001", "合并二", "a13-2", start + 20),
                pentry("10001", "合并三", "a13-3", start + 30),
            )
        },
    )

    async def scenario():
        await onboard(rig, "10001")
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.turns_dispatched == 1
    for mid in ("a13-1", "a13-2", "a13-3"):
        record = rig.claim_store.get(claim_key(mid))
        assert record is not None and record.state == "done"
        # 以 webhook 同口径再 try_claim：终态存在即 DUPLICATE，不经 dispatch 重投
        assert (
            rig.claim_store.try_claim(claim_key(mid), LANE_ORDINARY)
            is ClaimOutcome.DUPLICATE
        )


# ---------------------------------------------------------------------------
# AC14：每会话取材上限 10 条——12 条入选最新 10、舍弃计数 2
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac14_conversation_limit_keeps_latest_ten(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    entries = tuple(
        pentry("10001", f"内容-{i:02d}", f"a14-{i:02d}", start + i * 10)
        for i in range(1, 13)
    )
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 200),),
        friend_map={"10001": entries},
    )

    async def scenario():
        await onboard(rig, "10001")
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.entries_selected == 10
    assert report.entries_dropped["over_conversation_limit"] == 2
    sends = private_sends(rig, "10001")
    assert len(sends) == 1
    assert "内容-01" not in sends[0] and "内容-02" not in sends[0]
    assert "内容-03" in sends[0] and "内容-12" in sends[0]


# ---------------------------------------------------------------------------
# AC15：图片消息随管线——视觉桩调用与实时图片消息一致
# ---------------------------------------------------------------------------


class FakeVisionService:
    """视觉服务替身（与 tests.test_vision_images 同形）：记录三方法调用。"""

    def __init__(self, reply="看到啦，是一只猫", description="图片里是一只猫"):
        self.reply = reply
        self.description = description
        self.prepare_calls = []
        self.generate_calls = []
        self.describe_calls = []

    async def prepare(self, refs):
        self.prepare_calls.append(tuple(refs))
        return PreparedImages(
            data_urls=tuple(DATA_URL for _ in refs),
            received=len(refs),
            skipped=0,
        )

    async def generate(self, prompt, data_urls):
        self.generate_calls.append((prompt, tuple(data_urls)))
        return self.reply

    async def describe(self, data_urls):
        self.describe_calls.append(tuple(data_urls))
        return self.description


@pytest.mark.timeout(10)
def test_ac15_image_entry_follows_vision_pipeline(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    vision = FakeVisionService()
    rig.processor.vision_service = vision
    ref = ImageRef("https://images.example.test/cat0.png", None, "cat0.png")
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={
            "10001": (
                pentry(
                    "10001", "", "a15-1", start + 10,
                    images=(ref,), has_media=True,
                ),
            )
        },
    )

    async def scenario():
        await onboard(rig, "10001", "10002")
        # 实时图片回合（对照）：同 UserMessage 形态经 dispatch_parsed
        task = await rig.processor.dispatch_parsed(
            UserMessage(
                user_id="10002",
                text="",
                message_id="a15-rt",
                has_media=True,
                images=(ref,),
            )
        )
        assert task is not None and await task is True
        realtime_counts = (
            len(vision.prepare_calls),
            len(vision.generate_calls),
            len(vision.describe_calls),
        )
        report = await make_service(rig, source).run_scan(start, end)
        catchup_counts = (
            len(vision.prepare_calls),
            len(vision.generate_calls),
            len(vision.describe_calls),
        )
        return report, realtime_counts, catchup_counts

    report, realtime_counts, catchup_counts = asyncio.run(scenario())
    assert report.turns_dispatched == 1
    assert realtime_counts[0] == 1 and realtime_counts[1] == 1
    # 补回回合的视觉调用增量与实时一致（prepare/generate 各 +1，筛查同增）
    assert catchup_counts[0] - realtime_counts[0] == 1
    assert catchup_counts[1] - realtime_counts[1] == 1
    assert catchup_counts[2] - realtime_counts[2] == realtime_counts[2]
    assert private_sends(rig, "10002") == ["看到啦，是一只猫"]
    assert private_sends(rig, "10001") == [DEFAULT_PREFIX + "看到啦，是一只猫"]


# ---------------------------------------------------------------------------
# AC16：群聊只补 @——无 @ 入选 0；AC18：@ 还原不了整群跳过
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac16_group_only_at_entries_selected(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, group_enabled=True)
    start, end = window()
    source = StubHistorySource(
        contacts=(contact("group", GROUP_ID, start + 100),),
        group_map={
            GROUP_ID: (
                gentry("90001", "群里有人吗", "a16-1", start + 10),
                gentry(
                    "90001", "没艾特的闲聊", "a16-2", start + 20, with_at=False
                ),
            )
        },
    )
    report = asyncio.run(make_service(rig, source).run_scan(start, end))
    assert report.turns_dispatched == 1
    assert report.entries_selected == 1
    assert report.entries_dropped["group_not_trigger"] == 1
    assert len(rig.sender.segment_calls) == 1
    sent_texts = [
        seg.text
        for seg in rig.sender.segment_calls[0]["segments"]
        if isinstance(seg, TextSegment)
    ]
    assert any("群里有人吗" in t for t in sent_texts)
    assert all("没艾特的闲聊" not in t for t in sent_texts)
    record = rig.claim_store.get(claim_key("a16-1", group=True))
    assert record is not None and record.state == "done"


@pytest.mark.timeout(10)
def test_ac18_group_at_unknown_skips_whole_group(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, group_enabled=True)
    start, end = window()
    source = StubHistorySource(
        contacts=(contact("group", GROUP_ID, start + 100),),
        group_map={
            GROUP_ID: (
                gentry(
                    "90001", "好像艾特了", "a18-1", start + 10, at_known=False
                ),
            )
        },
    )
    report = asyncio.run(make_service(rig, source).run_scan(start, end))
    assert report.turns_dispatched == 0
    assert report.entries_dropped["group_at_unknown"] == 1
    assert rig.sender.segment_calls == []
    conv_logs = [
        r for r in read_events(rig) if r["event_type"] == "catchup_conversation"
    ]
    assert any(r["reason_code"] == "group_at_unknown" for r in conv_logs)


# ---------------------------------------------------------------------------
# AC19：危机不降级——回复逐字等于 crisis_reply 产出、brain 零调用、无前缀
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac19_crisis_turn_uses_fixed_reply_without_brain(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={
            "10001": (pentry("10001", "活着没意思", "a19-1", start + 10),)
        },
    )

    async def scenario():
        await onboard(rig, "10001", "10002")
        # 实时危机回合（对照）：同词经同一 Stage 链
        task = await rig.processor.dispatch_parsed(
            UserMessage(
                user_id="10002",
                text="活着没意思",
                message_id="a19-rt",
                has_media=False,
                images=(),
            )
        )
        assert task is not None and await task is True
        brain_before = rig.brain.calls
        alerts_before = len(rig.alerter.items)
        report = await make_service(rig, source).run_scan(start, end)
        return report, brain_before, alerts_before

    report, brain_before, alerts_before = asyncio.run(scenario())
    assert report.turns_dispatched == 1
    expected = crisis_reply(
        rig.settings.safety["helplines"], False, rig.settings.safety
    )
    realtime_send = private_sends(rig, "10002")
    catchup_send = private_sends(rig, "10001")
    assert realtime_send == [expected]
    assert catchup_send == [expected]  # 逐字一致且不带补回前缀
    assert rig.brain.calls == brain_before  # 危机回合 brain 零调用
    assert len(rig.alerter.items) == alerts_before + 1  # 既有告警路径照常


# ---------------------------------------------------------------------------
# AC21：限流与额度照常计入——额度耗尽时补回与实时同为被限
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac21_rate_limit_applies_to_catchup_like_realtime(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    rig.processor.limiter.limit = 0  # 入站限流额度耗尽（既有夹具注入面）
    start, end = window()
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={
            "10001": (pentry("10001", "限流时段的消息", "a21-1", start + 10),)
        },
    )

    async def scenario():
        await onboard(rig, "10001")
        task = await rig.processor.dispatch_parsed(
            UserMessage(
                user_id="10001",
                text="实时消息",
                message_id="a21-rt",
                has_media=False,
                images=(),
            )
        )
        realtime_result = await task if task is not None else None
        report = await make_service(rig, source).run_scan(start, end)
        return report, realtime_result

    report, realtime_result = asyncio.run(scenario())
    assert realtime_result is False  # 实时同条件被限
    assert report.outcome == "completed"
    assert private_sends(rig) == []  # 补回同样零投递、无豁免


# ---------------------------------------------------------------------------
# AC22：措辞前缀可配、默认开可关（三段）；AC23：措辞克制（前缀 ≤1、无逐条时间）
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac22_prefix_configurable_and_disableable(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)

    async def scenario():
        await onboard(rig, "10001", "10002", "10003", "10004")
        # 实时对照（无前缀）
        task = await rig.processor.dispatch_parsed(
            UserMessage(
                user_id="10004", text="你好呀", message_id="a22-rt",
                has_media=False, images=(),
            )
        )
        assert task is not None and await task is True
        reports = []
        for k, user in enumerate(("10001", "10002", "10003")):
            start, end = window(k)
            source = StubHistorySource(
                contacts=(contact("private", user, start + 100),),
                friend_map={
                    user: (pentry(user, "你好呀", f"a22-{k}", start + 10),)
                },
            )
            kwargs = (
                {},
                {"prefix_text": "【补回】刚看到："},
                {"prefix_enabled": False},
            )[k]
            reports.append(
                await make_service(rig, source, **kwargs).run_scan(start, end)
            )
        return reports

    reports = asyncio.run(scenario())
    assert all(r.turns_dispatched == 1 for r in reports)
    default_send = private_sends(rig, "10001")[0]
    custom_send = private_sends(rig, "10002")[0]
    disabled_send = private_sends(rig, "10003")[0]
    realtime_send = private_sends(rig, "10004")[0]
    assert default_send.startswith(DEFAULT_PREFIX)
    assert custom_send.startswith("【补回】刚看到：")
    assert not disabled_send.startswith(DEFAULT_PREFIX)
    assert disabled_send == realtime_send  # 关前缀后其余内容与实时一致


@pytest.mark.timeout(10)
def test_ac23_prefix_appears_at_most_once_without_timestamps(
    tmp_path, monkeypatch
):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={
            "10001": (
                pentry("10001", "早上好", "a23-1", start + 10),
                pentry("10001", "吃了吗", "a23-2", start + 20),
                pentry("10001", "在忙什么", "a23-3", start + 30),
            )
        },
    )

    async def scenario():
        await onboard(rig, "10001")
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.turns_dispatched == 1
    sends = private_sends(rig, "10001")
    assert len(sends) == 1
    assert sends[0].count(DEFAULT_PREFIX) == 1
    assert not re.search(r"\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}", sends[0])
    assert str(int(start)) not in sends[0]


# ---------------------------------------------------------------------------
# AC24：单轮会话上限 20——25 会话处理 20、截断计数 5、被截断零 claim
# ---------------------------------------------------------------------------


@pytest.mark.timeout(30)
def test_ac24_scan_caps_at_twenty_conversations(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    users = [str(10100 + i) for i in range(1, 26)]  # 10101..10125
    contacts = tuple(
        contact("private", user, start + i * 10)
        for i, user in enumerate(users, start=1)
    )
    source = StubHistorySource(
        contacts=contacts,
        friend_map={
            user: (pentry(user, f"{user}的消息", f"a24-{user}", start + i * 10),)
            for i, user in enumerate(users, start=1)
        },
    )

    async def scenario():
        await onboard(rig, *users)
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.outcome == "completed"
    assert report.conversations_seen == 25
    assert report.conversations_processed == 20
    assert report.conversations_truncated == 5
    # 按最后活动倒序取前 20：活动最早的 5 个（10101..10105）被截断
    for user in users[:5]:
        assert rig.claim_store.get(claim_key(f"a24-{user}")) is None
        assert private_sends(rig, user) == []
    assert sum(len(private_sends(rig, u)) for u in users) == 20
    scan_logs = [r for r in read_events(rig) if r["event_type"] == "catchup_scan"]
    assert scan_logs and scan_logs[-1]["truncated_count"] == 5


# ---------------------------------------------------------------------------
# AC25：日志/审计不落正文——canary 串在 events 日志与 catchup_scans 中零命中
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac25_canary_body_absent_from_audit_surfaces(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    canary = f"CANARY-BODY-{uuid.uuid4().hex[:8]}"
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={
            "10001": (
                pentry("10001", f"秘密 {canary} 内容", "a25-1", start + 10),
            )
        },
    )

    async def scenario():
        await onboard(rig, "10001")
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.turns_dispatched == 1
    # canary 确实流经了管线（回显在回复里），排除「根本没处理」的假阴性
    assert canary in private_sends(rig, "10001")[0]
    events_text = rig.processor.log_path.read_text(encoding="utf-8")
    assert canary not in events_text
    with rig.processor.database.transaction() as conn:
        dump = "\n".join(
            repr(row)
            for row in conn.execute("SELECT * FROM catchup_scans").fetchall()
        )
    assert canary not in dump


# ---------------------------------------------------------------------------
# AC26a：扫描不阻塞实时——慢拉取期间实时回合先完成
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac26a_realtime_turn_completes_during_slow_scan(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()

    async def scenario():
        await onboard(rig, "10001", "10002")
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def slow_history():
            entered.set()
            await gate.wait()
            return (pentry("10001", "慢拉取的消息", "a26-1", start + 10),)

        source = StubHistorySource(
            contacts=(contact("private", "10001", start + 100),),
            friend_map={"10001": slow_history},
        )
        scan_task = asyncio.create_task(
            make_service(rig, source).run_scan(start, end)
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        # 扫描挂在第一会话拉取上时，实时消息走完整个回合
        task = await rig.processor.dispatch_parsed(
            UserMessage(
                user_id="10002", text="实时优先", message_id="a26-rt",
                has_media=False, images=(),
            )
        )
        assert task is not None and await task is True
        assert len(private_sends(rig, "10002")) == 1
        assert not scan_task.done()
        gate.set()
        report = await asyncio.wait_for(scan_task, timeout=5)
        return report

    report = asyncio.run(scenario())
    assert report.outcome == "completed"
    assert report.turns_dispatched == 1
    assert len(private_sends(rig, "10001")) == 1


# ---------------------------------------------------------------------------
# AC26b：整轮总时限——拉取永不返回时扫描以 timeout 收口、已完成结果保留
# （生产常量 120.0 的机制断言：常量值本身先验真，运行时以 monkeypatch
# 把截止压缩到 1.0s 以满足单测时限，截止/收口路径与生产同一代码）
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac26b_scan_times_out_at_deadline_keeping_completed(
    tmp_path, monkeypatch
):
    assert catchup_module.SCAN_TOTAL_TIMEOUT_SECONDS == 120.0
    monkeypatch.setattr(catchup_module, "SCAN_TOTAL_TIMEOUT_SECONDS", 1.0)
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()

    async def hang():
        await asyncio.Event().wait()

    source = StubHistorySource(
        contacts=(
            contact("private", "10003", start + 300),
            contact("private", "10002", start + 200),
            contact("private", "10001", start + 100),
        ),
        friend_map={
            "10003": (pentry("10003", "先完成的", "a26b-3", start + 30),),
            "10002": hang,
            "10001": (pentry("10001", "来不及的", "a26b-1", start + 10),),
        },
    )

    async def scenario():
        await onboard(rig, "10001", "10002", "10003")
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.outcome == "timeout"
    assert len(private_sends(rig, "10003")) == 1  # 已完成会话结果保留
    assert private_sends(rig, "10001") == []  # 截止后会话跳过
    assert rig.claim_store.get(claim_key("a26b-1")) is None


# ---------------------------------------------------------------------------
# AC29：开关默认关、关时零行为变化——服务层零动作 + 接线层零装配
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ac29_disabled_by_default_zero_behavior(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    # 配置结构侧：默认值即关（CatchupSettings 与配置段同口径）
    assert CatchupSettings().enabled is False
    # 接线侧：默认配置下 create_app 不装配 watcher（无从触发任何扫描）
    assert rig.app.state.catchup_watcher is None
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={
            "10001": (pentry("10001", "不该被补回", "a29-1", start + 10),)
        },
    )

    async def scenario():
        await onboard(rig, "10001")
        return await make_service(rig, source, enabled=False).run_scan(
            start, end
        )

    report = asyncio.run(scenario())
    assert report.outcome == "disabled"
    assert source.contacts_calls == 0
    assert source.friend_calls == [] and source.group_calls == []
    assert private_sends(rig) == []


# ---------------------------------------------------------------------------
# fix-round-1：审查 P3/P4 修复回归
# （P3-1 派发挂死受整轮时限钳制 / P4 合并 id claim FULL 剔除 /
#  P3-3 enabled=true 组合根装配覆盖）
# ---------------------------------------------------------------------------


@pytest.mark.timeout(15)
def test_fix1_private_dispatch_hang_times_out_whole_scan(
    tmp_path, monkeypatch
):
    # 与 AC26b 对称：拉取挂死已被 wait_for 兜住，派发回合挂死也必须
    # 在整轮截止处被截断，run_scan 以 timeout 返回、后续会话不再处理。
    monkeypatch.setattr(catchup_module, "SCAN_TOTAL_TIMEOUT_SECONDS", 1.0)
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()

    async def hang_dispatch(parsed, *, catchup_prefix=None):
        return asyncio.create_task(asyncio.Event().wait())

    monkeypatch.setattr(rig.processor, "dispatch_parsed", hang_dispatch)
    source = StubHistorySource(
        contacts=(
            contact("private", "10002", start + 200),
            contact("private", "10001", start + 100),
        ),
        friend_map={
            "10002": (pentry("10002", "先挂死的", "f1-2", start + 20),),
            "10001": (pentry("10001", "不应被处理的", "f1-1", start + 10),),
        },
    )

    async def scenario():
        await onboard(rig, "10001", "10002")
        return await asyncio.wait_for(
            make_service(rig, source).run_scan(start, end), timeout=8
        )

    report = asyncio.run(scenario())
    assert report.outcome == "timeout"
    assert report.entries_dropped.get("dispatch_timeout") == 1
    # 挂死回合的 claim 以 abandoned 终结，整窗后续会话未被处理
    record = rig.claim_store.get(claim_key("f1-2"))
    assert record is not None and record.state == "abandoned"
    assert rig.claim_store.get(claim_key("f1-1")) is None
    assert private_sends(rig) == []


@pytest.mark.timeout(15)
def test_fix1_group_dispatch_hang_times_out_whole_scan(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(catchup_module, "SCAN_TOTAL_TIMEOUT_SECONDS", 1.0)
    rig = make_rig(tmp_path, monkeypatch, group_enabled=True)
    start, end = window()

    async def hang_group(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(catchup_module, "handle_group_message", hang_group)
    source = StubHistorySource(
        contacts=(contact("group", GROUP_ID, start + 100),),
        group_map={
            GROUP_ID: (gentry("10001", "群里挂死", "f1-g1", start + 10),)
        },
    )

    async def scenario():
        return await asyncio.wait_for(
            make_service(rig, source).run_scan(start, end), timeout=8
        )

    report = asyncio.run(scenario())
    assert report.outcome == "timeout"
    assert report.entries_dropped.get("group_dispatch_timeout") == 1
    record = rig.claim_store.get(claim_key("f1-g1", group=True))
    assert record is not None and record.state == "abandoned"


@pytest.mark.timeout(10)
def test_fix1_merged_id_claim_full_excluded_from_turn(tmp_path, monkeypatch):
    # P4：ordinary 分区打满时被合并 id 的 try_claim 返回 FULL——该 id
    # 须与 DUPLICATE 同处置从合成输入剔除并计 claim_full，不得在
    # 无去重留痕的情况下被合并进回合（回合本身走 reserved 分道）。
    rig = make_rig(tmp_path, monkeypatch)
    rig.claim_store.ordinary_capacity = 0
    start, end = window()
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={
            "10001": (
                pentry("10001", "被合并的普通消息", "f1-m1", start + 10),
                pentry("10001", "想自杀", "f1-m2", start + 20),
            )
        },
    )

    async def scenario():
        await onboard(rig, "10001")
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.outcome == "completed"
    assert report.turns_dispatched == 1
    assert report.entries_dropped.get("claim_full") == 1
    # 合成输入只剩回合条目本身；被合并 id 未留 claim 痕（已剔除）
    assert report.entries_selected == 1
    assert rig.claim_store.get(claim_key("f1-m1")) is None
    # 回合走危机口径（reserved 分道）：固定话术直发，与既有口径一致
    expected = crisis_reply(
        rig.settings.safety["helplines"], False, rig.settings.safety
    )
    assert private_sends(rig, "10001") == [expected]


@pytest.mark.timeout(10)
def test_fix1_assembly_enabled_formal_config_tree(tmp_path, monkeypatch):
    # P3-3：正式配置树 enabled=true 时组合根完整装配。
    rig = make_rig(
        tmp_path,
        monkeypatch,
        catchup_yaml={
            "enabled": True,
            "prefix_enabled": False,
            "prefix_text": "（正式树前缀）",
        },
    )
    watcher = rig.app.state.catchup_watcher
    assert watcher is not None
    assert "reconnect_catchup" in rig.app.state.scheduler.status()
    # CatchupSettings 投影与配置逐字一致
    assert watcher._service._config == CatchupSettings(
        enabled=True, prefix_enabled=False, prefix_text="（正式树前缀）"
    )
    assert rig.app.state.catchup_scan_store is not None


@pytest.mark.timeout(10)
def test_fix1_assembly_enabled_dict_fallback_and_strict_bool(
    tmp_path, monkeypatch
):
    # P3-3：config=None 时 dict 回退形态同样装配，投影口径一致。
    rig = make_rig(
        tmp_path,
        monkeypatch,
        config_none=True,
        catchup_app={"enabled": True, "prefix_text": "（字典前缀）"},
    )
    watcher = rig.app.state.catchup_watcher
    assert watcher is not None
    assert "reconnect_catchup" in rig.app.state.scheduler.status()
    # prefix_enabled 缺省：is not False → True（与组合根口径逐字一致）
    assert watcher._service._config == CatchupSettings(
        enabled=True, prefix_enabled=True, prefix_text="（字典前缀）"
    )
    # 严格布尔口径：字符串 "true" 不算启用（is True 判定）
    second = tmp_path / "second"
    second.mkdir()
    rig2 = make_rig(
        second,
        monkeypatch,
        config_none=True,
        catchup_app={"enabled": "true"},
    )
    assert rig2.app.state.catchup_watcher is None


@pytest.mark.timeout(10)
def test_fix1_assembly_enabled_sender_without_actions_fallback_scan(
    tmp_path, monkeypatch
):
    # P3-3：sender 无 actions 时 watcher 仍装配；扫描经历史源不可用
    # 降级为 fallback 发现（本地 messages 表），不抛、不发消息。
    rig = make_rig(
        tmp_path,
        monkeypatch,
        catchup_yaml={"enabled": True},
    )
    watcher = rig.app.state.catchup_watcher
    assert watcher is not None
    assert getattr(rig.processor.sender, "actions", None) is None
    start, end = window()

    async def scenario():
        await onboard(rig, "20001")
        await rig.processor.conversation.add_turn("20001", "之前聊过", "嗯")
        return await watcher._service.run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.outcome == "completed"
    assert report.turns_dispatched == 0
    assert private_sends(rig) == []
    scan_logs = [r for r in read_events(rig) if r["event_type"] == "catchup_scan"]
    assert scan_logs and scan_logs[-1]["discovery"] == "fallback_private_only"


# ---------------------------------------------------------------------------
# 集成审查修复轮 2（部署前复审 2026-11-02）：通道对齐与接缝覆盖
# P3-1 群匿名条目剔除 / P3-2 群合并逐条准入 / P3-3 群危机路径 /
# P3-4 装配链端到端 / P4-2 私聊 sender 对齐 / P4-5 群前缀断言
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_fix2_group_anonymous_entries_dropped(tmp_path, monkeypatch):
    # P3-1：sub_type=anonymous 的群条目在实时通道只登记不回复，补回
    # 通道同口径剔除；正常 @ 条目不受影响。
    rig = make_rig(tmp_path, monkeypatch, group_enabled=True)
    start, end = window()
    anon = gentry("80000000", "匿名说的话", "f2-a1", start + 10)
    anon = replace(anon, event=replace(anon.event, sub_type="anonymous"))
    source = StubHistorySource(
        contacts=(contact("group", GROUP_ID, start + 100),),
        group_map={
            GROUP_ID: (
                anon,
                gentry("90001", "群里有人吗", "f2-a2", start + 20),
            )
        },
    )
    report = asyncio.run(make_service(rig, source).run_scan(start, end))
    assert report.entries_dropped["group_anonymous"] == 1
    assert report.turns_dispatched == 1
    sent_texts = [
        seg.text
        for call in rig.sender.segment_calls
        for seg in call["segments"]
        if isinstance(seg, TextSegment)
    ]
    assert any("群里有人吗" in t for t in sent_texts)
    assert all("匿名说的话" not in t for t in sent_texts)


@pytest.mark.timeout(10)
def test_fix2_group_merged_turn_admission_per_entry(tmp_path, monkeypatch):
    # P3-2：合并回合逐条准入——未准入发送者的 @ 内容不得搭准入者的
    # 身份被回复；准入者的内容照常合成一回合。
    rig = make_rig(
        tmp_path,
        monkeypatch,
        group_enabled=True,
        group_chat_app={"reply_allowed_persons": ["owner"]},
    )
    start, end = window()
    source = StubHistorySource(
        contacts=(contact("group", GROUP_ID, start + 100),),
        group_map={
            GROUP_ID: (
                gentry("90001", "陌生人的话", "f2-b1", start + 10),
                gentry("10001", "主人也在群里", "f2-b2", start + 20),
            )
        },
    )
    report = asyncio.run(make_service(rig, source).run_scan(start, end))
    assert report.entries_dropped["group_not_admitted"] == 1
    assert report.turns_dispatched == 1
    sent_texts = [
        seg.text
        for call in rig.sender.segment_calls
        for seg in call["segments"]
        if isinstance(seg, TextSegment)
    ]
    assert any("主人也在群里" in t for t in sent_texts)
    assert all("陌生人的话" not in t for t in sent_texts)


@pytest.mark.timeout(10)
def test_fix2_private_sender_mismatch_dropped(tmp_path, monkeypatch):
    # P4-2：friend_history 返回发送者≠对端的条目（平台串话）时丢弃，
    # 不得把内容记到对端名下派发；对端本人条目照常。
    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    foreign = pentry("10009", "串话内容", "f2-c1", start + 10)
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={
            "10001": (
                foreign,
                pentry("10001", "本人消息", "f2-c2", start + 20),
            )
        },
    )

    async def scenario():
        await onboard(rig, "10001")
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.entries_dropped["sender_mismatch"] == 1
    assert report.turns_dispatched == 1
    sends = private_sends(rig, "10001")
    assert len(sends) == 1
    assert "本人消息" in sends[0]
    assert "串话内容" not in sends[0]
    assert private_sends(rig, "10009") == []


@pytest.mark.timeout(10)
def test_fix2_group_crisis_entry_observed_not_answered(tmp_path, monkeypatch):
    # P3-3：群危机补回与实时群口径一致——observe 段拦下，零发送、
    # responder/brain 零调用；claim 与计数口径钉死为现行已验行为
    # （回合 claim done、turns_dispatched 计 1）。
    rig = make_rig(tmp_path, monkeypatch, group_enabled=True)
    start, end = window()
    source = StubHistorySource(
        contacts=(contact("group", GROUP_ID, start + 100),),
        group_map={
            GROUP_ID: (gentry("90001", "想自杀", "f2-d1", start + 10),)
        },
    )
    report = asyncio.run(make_service(rig, source).run_scan(start, end))
    assert rig.sender.segment_calls == []
    assert rig.sender.private_calls == []
    assert rig.brain.calls == 0
    assert report.turns_dispatched == 1
    record = rig.claim_store.get(claim_key("f2-d1", group=True))
    assert record is not None and record.state == "done"


@pytest.mark.timeout(10)
def test_fix2_group_prefix_asserted_and_disableable(tmp_path, monkeypatch):
    # P4-5：群补回前缀必须真实出现在发送段首；关前缀时不得出现。
    rig = make_rig(tmp_path, monkeypatch, group_enabled=True)
    start, end = window()
    source = StubHistorySource(
        contacts=(contact("group", GROUP_ID, start + 100),),
        group_map={GROUP_ID: (gentry("90001", "群里有人吗", "f2-e1", start + 10),)},
    )
    report = asyncio.run(make_service(rig, source).run_scan(start, end))
    assert report.turns_dispatched == 1
    segments = rig.sender.segment_calls[0]["segments"]
    assert isinstance(segments[0], TextSegment)
    # 前缀段与回复正文在群发送通道合并为首段文本：逐字以默认前缀起头
    assert segments[0].text.startswith(DEFAULT_PREFIX)
    assert "群里有人吗" in segments[0].text

    rig2_path = tmp_path / "nopf"
    rig2_path.mkdir()
    rig2 = make_rig(rig2_path, monkeypatch, group_enabled=True)
    source2 = StubHistorySource(
        contacts=(contact("group", GROUP_ID, start + 100),),
        group_map={GROUP_ID: (gentry("90001", "群里有人吗", "f2-e2", start + 10),)},
    )
    report2 = asyncio.run(
        make_service(rig2, source2, prefix_enabled=False).run_scan(start, end)
    )
    assert report2.turns_dispatched == 1
    texts2 = [
        seg.text
        for call in rig2.sender.segment_calls
        for seg in call["segments"]
        if isinstance(seg, TextSegment)
    ]
    assert texts2 and all(not t.startswith(DEFAULT_PREFIX) for t in texts2)
    assert any("群里有人吗" in t for t in texts2)


class ActionsRecordingSender(RecordingSender):
    """RecordingSender + actions 门面（装配链测试用）：组合根经
    processor.sender.actions 构造 GatewayHistorySource，替身须同形。"""

    def __init__(self, actions):
        super().__init__()
        self.actions = actions


@pytest.mark.timeout(30)
def test_fix2_assembled_chain_health_transition_drives_scan(
    tmp_path, monkeypatch
):
    # P3-3/P3-4 接缝：全装配链端到端——create_app(enabled) 的真实
    # RecoveryWatcher 经真实 ConnectionHealth 快照坏→好→稳定转移触发，
    # 经真实 GatewayHistorySource（FakeTransport 原始响应）拉取历史，
    # 由真实 CatchupService 走完整管线发出恰好一条带前缀的补回回复。
    from companion.adapters.fake import FakeAdapter
    from companion.ops.connection_health import STABLE_SECONDS, Snapshot

    adapter = FakeAdapter(responses={})
    sender = ActionsRecordingSender(adapter.actions)
    rig = make_rig(
        tmp_path,
        monkeypatch,
        catchup_yaml={"enabled": True},
        health_enabled=True,
        sender=sender,
    )
    watcher = rig.app.state.catchup_watcher
    assert watcher is not None
    health = watcher._health_provider()
    assert health is not None

    def snap(qq):
        return Snapshot(
            companion_process="active",
            napcat_process="active",
            companion_result="success",
            napcat_result="success",
            ingress="ready",
            onebot="reachable",
            qq=qq,
            action="none" if qq == "reported_online" else "operator_action_needed",
        )

    async def scenario():
        await onboard(rig, "10001")
        health.snapshot = snap("offline")
        assert await watcher.poll_once() is False
        # 模拟断线已持续 10 分钟：回拨断线起点，历史条目落窗口中段
        down_since = watcher._down_since_wall
        assert down_since is not None
        watcher._down_since_wall = down_since - 600.0
        mid = down_since - 300.0
        adapter.transport.responses.update(
            {
                "get_recent_contact": {
                    "status": "ok",
                    "retcode": 0,
                    "data": [
                        {"peerUin": "10001", "chatType": 1, "lastMsgTime": mid}
                    ],
                },
                "get_friend_msg_history": {
                    "status": "ok",
                    "retcode": 0,
                    "data": [
                        {
                            "post_type": "message",
                            "message_type": "private",
                            "message_id": 777001,
                            "user_id": 10001,
                            "self_id": int(SELF_ID),
                            "time": mid,
                            "message": [
                                {"type": "text", "data": {"text": "断线时发的"}}
                            ],
                            "raw_message": "断线时发的",
                        }
                    ],
                },
            }
        )
        health.snapshot = snap("reported_online")
        health.stable_since = health.clock() - (STABLE_SECONDS + 5.0)
        assert await watcher.poll_once() is True
        task = watcher._scan_task
        assert task is not None
        await task

    asyncio.run(scenario())
    sends = private_sends(rig, "10001")
    assert len(sends) == 1
    assert DEFAULT_PREFIX in sends[0]
    assert "断线时发的" in sends[0]


# ---------------------------------------------------------------------------
# 集成审查修复轮 3（复审轮 2：P2×1 / P3×1 / P4×3 的测试面 + P3 生产修复）
# P3 群 group_id 归属 / P4 审计白名单 / P4 协程关闭单测 /
# P4 notice 与全员未准入分支 / P4 群装配端到端
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_fix3_group_foreign_group_entry_dropped(tmp_path, monkeypatch):
    # P3：条目自带 group_id 与被扫群不一致（平台串话）时 fail-closed
    # 丢弃——异群内容不得并入本群回合，更不得把回复发往异群（异群
    # 条目置于最新位，钉死最坏情形）。
    rig = make_rig(tmp_path, monkeypatch, group_enabled=True)
    start, end = window()
    foreign = gentry("90001", "异群的内容", "f3-x1", start + 30)
    foreign = replace(foreign, event=replace(foreign.event, group_id="999999999"))
    source = StubHistorySource(
        contacts=(contact("group", GROUP_ID, start + 100),),
        group_map={
            GROUP_ID: (
                gentry("90001", "本群的话", "f3-x2", start + 10),
                foreign,
            )
        },
    )
    report = asyncio.run(make_service(rig, source).run_scan(start, end))
    assert report.entries_dropped["group_id_mismatch"] == 1
    assert report.turns_dispatched == 1
    sent_texts = [
        seg.text
        for call in rig.sender.segment_calls
        for seg in call["segments"]
        if isinstance(seg, TextSegment)
    ]
    assert any("本群的话" in t for t in sent_texts)
    assert all("异群的内容" not in t for t in sent_texts)
    assert rig.claim_store.get(claim_key("f3-x1", group=True)) is None


@pytest.mark.timeout(10)
def test_fix3_group_notice_entries_dropped(tmp_path, monkeypatch):
    # P4：匿名剔除等价类的 notice 变体同样计数剔除、零派发。
    rig = make_rig(tmp_path, monkeypatch, group_enabled=True)
    start, end = window()
    notice = gentry("90001", "通知类内容", "f3-n1", start + 10)
    notice = replace(notice, event=replace(notice.event, sub_type="notice"))
    source = StubHistorySource(
        contacts=(contact("group", GROUP_ID, start + 100),),
        group_map={GROUP_ID: (notice,)},
    )
    report = asyncio.run(make_service(rig, source).run_scan(start, end))
    assert report.entries_dropped["group_anonymous"] == 1
    assert report.turns_dispatched == 0
    assert rig.sender.segment_calls == []


@pytest.mark.timeout(10)
def test_fix3_group_all_entries_not_admitted(tmp_path, monkeypatch):
    # P4：逐条准入后全员未准入——零派发、零发送、零 claim，
    # group_not_admitted 逐条计数。
    rig = make_rig(
        tmp_path,
        monkeypatch,
        group_enabled=True,
        group_chat_app={"reply_allowed_persons": ["owner"]},
    )
    start, end = window()
    source = StubHistorySource(
        contacts=(contact("group", GROUP_ID, start + 100),),
        group_map={
            GROUP_ID: (
                gentry("90001", "陌生人一", "f3-m1", start + 10),
                gentry("90002", "陌生人二", "f3-m2", start + 20),
            )
        },
    )
    report = asyncio.run(make_service(rig, source).run_scan(start, end))
    assert report.entries_dropped["group_not_admitted"] == 2
    assert report.turns_dispatched == 0
    assert rig.sender.segment_calls == []
    assert rig.claim_store.get(claim_key("f3-m1", group=True)) is None
    assert rig.claim_store.get(claim_key("f3-m2", group=True)) is None


@pytest.mark.timeout(10)
def test_fix3_history_call_closes_coro_on_exhausted_budget(tmp_path, monkeypatch):
    # P4：_history_call 在时限耗尽时必须先关闭未 await 的协程再抛
    # TimeoutError（回滚此修复须有测试变红）。
    rig = make_rig(tmp_path, monkeypatch)
    service = make_service(rig, StubHistorySource())

    class TrackedCoro:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

        def __await__(self):  # pragma: no cover - 本路径不应被 await
            yield from ()

    tracked = TrackedCoro()

    async def scenario():
        with pytest.raises(TimeoutError):
            await service._history_call(tracked, service._monotonic() - 1.0)

    asyncio.run(scenario())
    assert tracked.close_calls == 1


@pytest.mark.timeout(10)
def test_fix3_audit_record_has_no_raw_message_ids(tmp_path, monkeypatch):
    # P4：catchup_conversation 审计记录只许 §5.8 白名单键——原始
    # message_id 不得以任何键/值形式落进 events.jsonl。
    import json as json_module

    rig = make_rig(tmp_path, monkeypatch)
    start, end = window()
    source = StubHistorySource(
        contacts=(contact("private", "10001", start + 100),),
        friend_map={"10001": (pentry("10001", "审计检查", "f3-audit-1", start + 10),)},
    )

    async def scenario():
        await onboard(rig, "10001")
        return await make_service(rig, source).run_scan(start, end)

    report = asyncio.run(scenario())
    assert report.turns_dispatched == 1
    records = [
        r for r in read_events(rig) if r["event_type"] == "catchup_conversation"
    ]
    assert records
    for record in records:
        assert "message_ids" not in record
        assert "turn_message_id" not in record
        assert "f3-audit-1" not in json_module.dumps(record)


@pytest.mark.timeout(30)
def test_fix3_assembled_group_history_end_to_end(tmp_path, monkeypatch):
    # P4：群装配接缝——原始 OneBot 群历史响应经真实
    # GatewayHistorySource 归一化后由真实 CatchupService 处理，
    # 恰好一条带前缀的群补回，claim 以 g: 前缀落 done。
    from companion.adapters.fake import FakeAdapter
    from companion.adapters.history import GatewayHistorySource

    adapter = FakeAdapter(responses={})
    rig = make_rig(tmp_path, monkeypatch, group_enabled=True)
    start, end = window()
    mid = (start + end) / 2
    adapter.transport.responses.update(
        {
            "get_recent_contact": {
                "status": "ok",
                "retcode": 0,
                "data": [
                    {"peerUin": GROUP_ID, "chatType": 2, "lastMsgTime": mid}
                ],
            },
            "get_group_msg_history": {
                "status": "ok",
                "retcode": 0,
                "data": [
                    {
                        "post_type": "message",
                        "message_type": "group",
                        "message_id": 777002,
                        "user_id": 90001,
                        "group_id": int(GROUP_ID),
                        "self_id": int(SELF_ID),
                        "time": mid,
                        "message": [
                            {"type": "at", "data": {"qq": SELF_ID}},
                            {"type": "text", "data": {"text": "断线时群里喊你"}},
                        ],
                        "raw_message": "断线时群里喊你",
                        "sender": {"role": "member"},
                    }
                ],
            },
        }
    )
    source = GatewayHistorySource(adapter.actions, self_id=SELF_ID)
    report = asyncio.run(make_service(rig, source).run_scan(start, end))
    assert report.turns_dispatched == 1
    assert len(rig.sender.segment_calls) == 1
    texts = [
        seg.text
        for seg in rig.sender.segment_calls[0]["segments"]
        if isinstance(seg, TextSegment)
    ]
    assert any(DEFAULT_PREFIX in t for t in texts)
    assert any("断线时群里喊你" in t for t in texts)
    record = rig.claim_store.get(claim_key("777002", group=True))
    assert record is not None and record.state == "done"
