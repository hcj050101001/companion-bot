"""T2 适配层独立测试（AC28 / AC8c / AC18 的 at 还原侧）。

只以构造的原始响应样本经 FakeTransport 预置驱动 ``GatewayHistorySource``，
断言五类归一字段：会话标识（peer_id）、发送时刻（sent_at，含毫秒与
ISO 形态）、message_id、at 还原（以 ``is_group_trigger(entry.event)``
与样本预期对照）、条目类型（非消息类被弃）。样本覆盖：list 形段、
CQ 字符串形、毫秒时间戳、自发条目、缺字段条目、非消息类条目。

业务层纪律的对照面：业务层测试只构造归一化结构、不构造原始响应；
原始响应样本只许出现在本文件（设计 §6.6）。
"""

import asyncio

import pytest

from companion.adapters.base import InboundEvent
from companion.adapters.fake import FakeAdapter
from companion.adapters.history import (
    HISTORY_FETCH_COUNT,
    GatewayHistorySource,
    HistorySource,
    NormalizedContact,
    NormalizedHistoryEntry,
)
from companion.ingress.events import is_group_trigger

pytestmark = pytest.mark.timeout(10)

SELF_ID = "900000001"
USER_ID = "10001"
GROUP_ID = "20001"


def _ok(data):
    return {"status": "ok", "retcode": 0, "data": data}


def _source(responses, *, self_id=SELF_ID):
    adapter = FakeAdapter(responses=responses)
    source = GatewayHistorySource(adapter.actions, self_id=self_id)
    return source, adapter


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# recent_contacts：会话标识 / 时刻归一 / None 与空元组区分
# ---------------------------------------------------------------------------


def test_recent_contacts_normalises_private_and_group():
    source, adapter = _source(
        {
            "get_recent_contact": _ok(
                [
                    # 私聊：peerUin + 秒级 epoch（字段链为推测，样本锁定）
                    {"peerUin": USER_ID, "chatType": 1, "lastMsgTime": 1700000100},
                    # 群：group_id + 毫秒时间戳 → 除 1000
                    {
                        "group_id": int(GROUP_ID),
                        "chatType": 2,
                        "lastMsgTime": 1700000200000,
                    },
                    # uin 键 + 嵌套 lastMsg.time 形态
                    {"uin": "10002", "lastMsg": {"time": 1700000050}},
                    # 无 peer 标识 → 丢弃
                    {"chatType": 1, "lastMsgTime": 1700000000},
                    # peerUid 非纯数字串 → 不作标识、且无其他键 → 丢弃
                    {"peerUid": "uid-not-numeric", "lastMsgTime": 1700000000},
                ]
            )
        }
    )
    contacts = _run(source.recent_contacts())
    assert contacts == (
        NormalizedContact(kind="private", peer_id=USER_ID, last_activity_at=1700000100.0),
        NormalizedContact(kind="group", peer_id=GROUP_ID, last_activity_at=1700000200.0),
        NormalizedContact(kind="private", peer_id="10002", last_activity_at=1700000050.0),
    )
    calls = [c for c in adapter.transport.calls if c["action"] == "get_recent_contact"]
    assert len(calls) == 1


def test_recent_contacts_dict_container_form():
    source, _adapter = _source(
        {"get_recent_contact": _ok({"contacts": [{"peerUin": USER_ID, "time": 1700000000}]})}
    )
    assert _run(source.recent_contacts()) == (
        NormalizedContact(kind="private", peer_id=USER_ID, last_activity_at=1700000000.0),
    )


def test_recent_contacts_unavailable_vs_empty():
    failed, _ = _source(
        {"get_recent_contact": {"status": "failed", "retcode": 1200, "data": None}}
    )
    assert _run(failed.recent_contacts()) is None

    empty, _ = _source({"get_recent_contact": _ok([])})
    result = _run(empty.recent_contacts())
    assert result == ()
    assert result is not None

    unusable, _ = _source({"get_recent_contact": _ok({"unexpected": 1})})
    assert _run(unusable.recent_contacts()) is None


def test_actions_none_source_is_unavailable_not_raising():
    source = GatewayHistorySource(None)
    assert _run(source.recent_contacts()) is None
    assert _run(source.friend_history(USER_ID)) is None
    assert _run(source.group_history(GROUP_ID)) is None


def test_source_satisfies_history_source_protocol():
    source, _adapter = _source({})
    assert isinstance(source, HistorySource)


# ---------------------------------------------------------------------------
# friend_history：list 形段 / 毫秒时间戳 / 排序 / 自发条目 / 请求参数
# ---------------------------------------------------------------------------


def _friend_entry(**overrides):
    entry = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 111,
        "user_id": USER_ID,
        "self_id": SELF_ID,
        "time": 1700000100,
        "message": [{"type": "text", "data": {"text": "后一条"}}],
        "raw_message": "后一条",
    }
    entry.update(overrides)
    return entry


def test_friend_history_list_form_fields_and_sorting():
    earlier = _friend_entry(
        message_id="109",
        time=1700000050000,  # 毫秒时间戳 → 1700000050.0
        message=[{"type": "text", "data": {"text": "先一条"}}],
        raw_message="先一条",
    )
    later = _friend_entry()
    source, adapter = _source({"get_friend_msg_history": _ok([later, earlier])})

    entries = _run(source.friend_history(USER_ID))

    assert isinstance(entries, tuple) and len(entries) == 2
    first, second = entries  # 适配层按 sent_at 升序稳定排序
    assert isinstance(first, NormalizedHistoryEntry)
    assert first.message_id == "109"
    assert first.sender_id == USER_ID
    assert first.sent_at == 1700000050.0
    assert first.at_known is True  # 私聊恒 True
    assert isinstance(first.event, InboundEvent)
    assert first.event.text == "先一条"
    assert second.message_id == "111"  # int message_id 归一为 str
    assert second.sent_at == 1700000100.0
    assert second.event.text == "后一条"

    calls = [c for c in adapter.transport.calls if c["action"] == "get_friend_msg_history"]
    assert len(calls) == 1
    assert calls[0]["params"] == {"user_id": USER_ID, "count": HISTORY_FETCH_COUNT}
    assert HISTORY_FETCH_COUNT == 50


def test_friend_history_cq_string_form():
    entry = _friend_entry(
        message_id=222,
        time=1700000200,
        message="在吗[CQ:face,id=14]",
        raw_message="在吗[CQ:face,id=14]",
    )
    source, _adapter = _source({"get_friend_msg_history": _ok([entry])})
    (got,) = _run(source.friend_history(USER_ID))
    assert got.message_id == "222"
    assert got.event.text == "在吗"  # CQ 码剥离、文本口径与实时同源
    assert got.event.has_media is True


def test_friend_history_self_sent_entry_is_kept_with_sender_as_self():
    # 自发条目在适配层不丢弃（过滤是业务层 §5.4 的职责）；sender_id
    # 与 event.self_id 一致即业务层的自发判定依据。
    entry = _friend_entry(message_id=333, user_id=SELF_ID)
    source, _adapter = _source({"get_friend_msg_history": _ok([entry])})
    (got,) = _run(source.friend_history(USER_ID))
    assert got.sender_id == SELF_ID
    assert got.event.user_id == got.event.self_id == SELF_ID


def test_friend_history_sender_dict_used_when_user_id_missing():
    # 条目自带 user_id 缺失：sender 子 dict（真机已见形态）提供方向。
    from_sender = _friend_entry(message_id=401)
    del from_sender["user_id"]
    from_sender["sender"] = {"user_id": int(USER_ID)}
    source, _adapter = _source({"get_friend_msg_history": _ok([from_sender])})
    entries = _run(source.friend_history(USER_ID))
    assert [e.sender_id for e in entries] == [USER_ID]


def test_friend_history_senderless_entry_dropped_no_sender():
    # 审查 P3 修复（fix-round-1）：顶层 user_id 与 sender.user_id 皆
    # 缺失 → 方向不明即丢弃（计 no_sender），不做 peer 兜底。此形态可
    # 为机器人自发条目：兜底会把它归属对方、绕过业务层自发过滤。
    senderless = _friend_entry(message_id=402)
    del senderless["user_id"]
    # 无 sender 子 dict、且 self_id 为机器人：旧兜底口径下会被归一为
    # 对方发来的消息（event.user_id == 会话 peer、self_id 为机器人）。
    source, _adapter = _source({"get_friend_msg_history": _ok([senderless])})
    entries = _run(source.friend_history(USER_ID))
    assert entries == ()
    assert source.drop_counts.get("no_sender") == 1


def test_friend_history_iso_time_string():
    entry = _friend_entry(message_id=444, time="2023-11-14T22:13:20+00:00")
    source, _adapter = _source({"get_friend_msg_history": _ok([entry])})
    (got,) = _run(source.friend_history(USER_ID))
    assert got.sent_at == 1700000000.0


def test_friend_history_drops_missing_fields_and_non_message():
    no_time = _friend_entry(message_id=501)
    del no_time["time"]
    no_message_id = _friend_entry()
    del no_message_id["message_id"]
    # 非消息类条目（撤回通知形态：无 message_id、无消息段）→ 适配层收口
    notice = {
        "post_type": "notice",
        "notice_type": "friend_recall",
        "user_id": USER_ID,
        "self_id": SELF_ID,
        "time": 1700000300,
    }
    good = _friend_entry(message_id=502, time=1700000400)
    source, _adapter = _source(
        {"get_friend_msg_history": _ok([no_time, no_message_id, notice, good])}
    )
    entries = _run(source.friend_history(USER_ID))
    assert [e.message_id for e in entries] == ["502"]
    assert source.drop_counts.get("no_time") == 1
    assert source.drop_counts.get("not_message") == 2


def test_friend_history_unavailable_vs_empty():
    failed, _ = _source(
        {"get_friend_msg_history": {"status": "failed", "retcode": 1200, "data": None}}
    )
    assert _run(failed.friend_history(USER_ID)) is None

    empty, _ = _source({"get_friend_msg_history": _ok([])})
    assert _run(empty.friend_history(USER_ID)) == ()

    raising, _ = _source({"get_friend_msg_history": RuntimeError("boom")})
    assert _run(raising.friend_history(USER_ID)) is None


def test_history_dict_container_messages_key():
    source, _adapter = _source(
        {"get_friend_msg_history": _ok({"messages": [_friend_entry()]})}
    )
    (got,) = _run(source.friend_history(USER_ID))
    assert got.message_id == "111"


# ---------------------------------------------------------------------------
# group_history：at 还原（AC18 适配侧）/ 非消息类收口
# ---------------------------------------------------------------------------


def _group_entry(**overrides):
    entry = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 611,
        "user_id": "10002",
        "group_id": GROUP_ID,
        "self_id": SELF_ID,
        "time": 1700000100,
        "message": [
            {"type": "at", "data": {"qq": SELF_ID}},
            {"type": "text", "data": {"text": "在吗"}},
        ],
        "raw_message": "[CQ:at,qq=900000001]在吗",
    }
    entry.update(overrides)
    return entry


def test_group_history_at_list_form_triggers():
    source, adapter = _source({"get_group_msg_history": _ok([_group_entry()])})
    (got,) = _run(source.group_history(GROUP_ID))
    assert got.at_known is True
    assert is_group_trigger(got.event) is True
    assert got.event.chat_type == "group"
    assert got.event.group_id == GROUP_ID
    calls = [c for c in adapter.transport.calls if c["action"] == "get_group_msg_history"]
    assert calls[0]["params"] == {"group_id": GROUP_ID, "count": HISTORY_FETCH_COUNT}


def test_group_history_at_cq_string_form_triggers():
    entry = _group_entry(
        message_id=612,
        message="[CQ:at,qq=900000001]在吗",
        raw_message="[CQ:at,qq=900000001]在吗",
    )
    source, _adapter = _source({"get_group_msg_history": _ok([entry])})
    (got,) = _run(source.group_history(GROUP_ID))
    assert got.at_known is True
    assert is_group_trigger(got.event) is True


def test_group_history_at_other_person_known_but_not_trigger():
    entry = _group_entry(
        message_id=613,
        message=[
            {"type": "at", "data": {"qq": "10002"}},
            {"type": "text", "data": {"text": "找你"}},
        ],
        raw_message="[CQ:at,qq=10002]找你",
    )
    source, _adapter = _source({"get_group_msg_history": _ok([entry])})
    (got,) = _run(source.group_history(GROUP_ID))
    assert got.at_known is True  # 段载体可判读
    assert is_group_trigger(got.event) is False  # @ 的不是机器人


def test_group_history_text_only_projection_at_unknown():
    # 仅有纯文本投影、无段载体 → at_known=False（业务层据此整群跳过，
    # AC18）；条目本身保留、is_group_trigger 恒 False。
    entry = _group_entry(message_id=614)
    del entry["message"]
    del entry["raw_message"]
    entry["text"] = "在吗"
    entry["content"] = "在吗"
    source, _adapter = _source({"get_group_msg_history": _ok([entry])})
    (got,) = _run(source.group_history(GROUP_ID))
    assert got.at_known is False
    assert is_group_trigger(got.event) is False


def test_group_history_non_message_entry_dropped():
    notice = {
        "post_type": "notice",
        "notice_type": "group_recall",
        "user_id": "10002",
        "group_id": GROUP_ID,
        "self_id": SELF_ID,
        "time": 1700000300,
    }
    source, _adapter = _source(
        {"get_group_msg_history": _ok([notice, _group_entry(message_id=615)])}
    )
    entries = _run(source.group_history(GROUP_ID))
    assert [e.message_id for e in entries] == ["615"]


# ---------------------------------------------------------------------------
# self_id 解析（设计 §6.5）：注入 → get_login_info 缓存 → 条目自带
# ---------------------------------------------------------------------------


def test_self_id_resolved_from_login_info_and_cached():
    entry = _friend_entry(message_id=701)
    del entry["self_id"]
    source, adapter = _source(
        {
            "get_login_info": _ok({"user_id": int(SELF_ID), "nickname": "bot"}),
            "get_friend_msg_history": _ok([entry]),
        },
        self_id=None,
    )
    (got,) = _run(source.friend_history(USER_ID))
    assert got.event.self_id == SELF_ID
    # 第二次调用走缓存：get_login_info 总调用数仍为 1
    _run(source.friend_history(USER_ID))
    login_calls = [c for c in adapter.transport.calls if c["action"] == "get_login_info"]
    assert len(login_calls) == 1


def test_entry_self_id_takes_precedence_over_login_cache():
    entry = _friend_entry(message_id=702, self_id="900000002")
    source, _adapter = _source(
        {
            "get_login_info": _ok({"user_id": int(SELF_ID)}),
            "get_friend_msg_history": _ok([entry]),
        },
        self_id=None,
    )
    (got,) = _run(source.friend_history(USER_ID))
    assert got.event.self_id == "900000002"


def test_entry_without_any_self_id_source_is_dropped():
    entry = _friend_entry(message_id=703)
    del entry["self_id"]
    source, _adapter = _source(
        {
            "get_login_info": {"status": "failed", "retcode": 1200, "data": None},
            "get_friend_msg_history": _ok([entry]),
        },
        self_id=None,
    )
    assert _run(source.friend_history(USER_ID)) == ()
    assert source.drop_counts.get("no_self_id") == 1
