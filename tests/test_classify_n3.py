"""N3 classify 契约测试（全接口方案 §2.3/§3.3、C1 判别联合）。

适配器级、不走 webhook：``classify_onebot_event`` 把原始报文分类为
判别联合四型（InboundEvent / NoticeEvent / RequestEvent / MetaEvent）
或带 reason_code 的 Ignore。覆盖矩阵与
tests/fixtures/napcat/event_coverage.json 的 32 行逐行对齐（私聊
friend/group_temp、群 normal/anonymous/notice、message_sent 自身回流、
notice 全部子类含 notify 8 子类、request friend/group add/invite、
meta lifecycle/heartbeat），外加新旧 reason 分工与既有 normalize
行为不变的抽查。
"""

import pytest

from companion.adapters import (
    REASON_NOT_MESSAGE,
    REASON_UNKNOWN_EVENT_TYPE,
    FakeAdapter,
    Ignore,
    InboundEvent,
    MetaEvent,
    NoticeEvent,
    OneBotAdapter,
    RequestEvent,
    classify_onebot_event,
    event_type_summary,
    normalize_onebot_event,
)
from companion.domain import events as domain_events

pytestmark = pytest.mark.timeout(10)

SELF_ID = "900000001"
USER_ID = "90001"
GROUP_ID = "80001"


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def private_event(**overrides):
    event = {
        "time": 1767225600,
        "self_id": SELF_ID,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": "n3-p1",
        "user_id": USER_ID,
        "message": "你好",
        "raw_message": "你好",
    }
    event.update(overrides)
    return event


def group_event(**overrides):
    event = {
        "time": 1767225600,
        "self_id": SELF_ID,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": "n3-g1",
        "group_id": GROUP_ID,
        "user_id": USER_ID,
        "message": "群里你好",
        "raw_message": "群里你好",
    }
    event.update(overrides)
    return event


def notice_event(notice_type, **overrides):
    event = {
        "time": 1767225600,
        "self_id": SELF_ID,
        "post_type": "notice",
        "notice_type": notice_type,
        "user_id": USER_ID,
        "group_id": GROUP_ID,
    }
    event.update(overrides)
    return event


def request_event(request_type, **overrides):
    event = {
        "time": 1767225600,
        "self_id": SELF_ID,
        "post_type": "request",
        "request_type": request_type,
        "user_id": USER_ID,
        "flag": "n3-flag-1",
        "comment": "fake",
    }
    event.update(overrides)
    return event


def meta_event(meta_event_type, **overrides):
    event = {
        "time": 1767225600,
        "self_id": SELF_ID,
        "post_type": "meta_event",
        "meta_event_type": meta_event_type,
    }
    event.update(overrides)
    return event


def reason_of(result) -> str:
    assert isinstance(result, Ignore), f"expected Ignore, got {result!r}"
    return result.reason_code


# ---------------------------------------------------------------------------
# Constants & type shapes
# ---------------------------------------------------------------------------


def test_new_reason_constants_have_contract_values():
    assert REASON_NOT_MESSAGE == "not_message"
    assert REASON_UNKNOWN_EVENT_TYPE == "unknown_event_type"


def test_domain_event_constants_have_contract_values():
    assert domain_events.EVENT_KIND_MESSAGE == "message"
    assert domain_events.EVENT_KIND_NOTICE == "notice"
    assert domain_events.EVENT_KIND_REQUEST == "request"
    assert domain_events.EVENT_KIND_META == "meta"
    assert domain_events.SCENE_PRIVATE_CHAT == "private_chat"
    assert domain_events.SCENE_GROUP_CHAT == "group_chat"
    assert domain_events.SCENE_EVENT_NOTICE == "event_notice"
    assert domain_events.SCENE_EVENT_REQUEST == "event_request"
    assert domain_events.SCENE_SYSTEM == "system"


def test_inbound_event_v2_defaults_keep_legacy_construction_working():
    event = InboundEvent(
        platform="onebot",
        self_id=SELF_ID,
        user_id=USER_ID,
        session_id=f"onebot:{USER_ID}",
        segments=(),
        text="",
        has_media=False,
        message_id="m-1",
        raw={},
    )
    assert event.event_kind == "message"
    assert event.chat_type == "private"
    assert event.group_id is None
    assert event.actor_id is None
    assert event.target_id is None
    assert event.sub_type is None
    assert event.direction == "inbound"


def test_meta_event_defaults_are_constructible():
    event = MetaEvent(
        platform="onebot", self_id=SELF_ID,
        meta_event_type="heartbeat", sub_type=None,
    )
    assert event.direction == "inbound"
    assert event.message_id is None
    assert event.user_id == ""
    assert event.group_id is None
    assert event.status_online is None
    assert event.status_good is None
    assert event.raw == {}


def test_adapters_declare_all_event_kinds():
    expected = frozenset({"message", "notice", "request", "meta"})
    assert OneBotAdapter().capabilities.event_kinds == expected
    assert FakeAdapter().capabilities.event_kinds == expected


def test_event_type_summary_covers_five_type_dimensions_in_order():
    assert event_type_summary(
        {"post_type": "message", "message_type": "group"}
    ) == "post_type=message/message_type=group"
    assert event_type_summary(
        {"post_type": "notice", "notice_type": "friend_add"}
    ) == "post_type=notice/notice_type=friend_add"
    assert event_type_summary(
        {"post_type": "request", "request_type": "group"}
    ) == "post_type=request/request_type=group"
    assert event_type_summary(
        {"post_type": "meta_event", "meta_event_type": "heartbeat"}
    ) == "post_type=meta_event/meta_event_type=heartbeat"
    # 既有 invalid_type 口径不变。
    assert event_type_summary({"post_type": "notice\n"}) == "post_type=invalid_type"


# ---------------------------------------------------------------------------
# Matrix rows 1-2: private friend / group_temp
# ---------------------------------------------------------------------------


def test_classify_private_friend():
    result = classify_onebot_event(private_event())
    assert isinstance(result, InboundEvent)
    assert result.event_kind == "message"
    assert result.chat_type == "private"
    assert result.sub_type == "friend"
    assert result.direction == "inbound"
    assert result.group_id is None
    assert result.session_id == f"onebot:{USER_ID}"
    assert result.text == "你好"


def test_classify_private_group_temp_by_sub_type():
    result = classify_onebot_event(private_event(sub_type="group_temp"))
    assert isinstance(result, InboundEvent)
    assert result.chat_type == "group_temp"
    assert result.sub_type == "group_temp"


def test_classify_private_group_temp_by_temp_source_key():
    result = classify_onebot_event(private_event(temp_source=12345))
    assert isinstance(result, InboundEvent)
    assert result.chat_type == "group_temp"


def test_classify_private_ignore_reasons_pass_through_from_normalize():
    event = private_event()
    del event["message_id"]
    assert reason_of(classify_onebot_event(event)) == "no_message_id"
    event = private_event()
    del event["self_id"]
    assert reason_of(classify_onebot_event(event)) == "no_self_id"
    event = private_event()
    del event["user_id"]
    assert reason_of(classify_onebot_event(event)) == "malformed"


# ---------------------------------------------------------------------------
# Matrix rows 3-5: group normal / anonymous / notice
# ---------------------------------------------------------------------------


def test_classify_group_normal():
    result = classify_onebot_event(group_event())
    assert isinstance(result, InboundEvent)
    assert result.event_kind == "message"
    assert result.chat_type == "group"
    assert result.group_id == GROUP_ID
    assert result.session_id == f"onebot:g{GROUP_ID}"
    assert result.sub_type == "normal"
    assert result.user_id == USER_ID
    assert result.text == "群里你好"
    assert result.has_media is False


def test_classify_group_missing_sub_type_defaults_to_normal():
    event = group_event()
    del event["sub_type"]
    result = classify_onebot_event(event)
    assert isinstance(result, InboundEvent)
    assert result.sub_type == "normal"


def test_classify_group_segment_list_parsing():
    result = classify_onebot_event(
        group_event(
            message=[
                {"type": "text", "data": {"text": "看图"}},
                {"type": "image", "data": {"file": "fake.jpg"}},
            ],
            raw_message="看图[CQ:image,file=fake.jpg]",
        )
    )
    assert isinstance(result, InboundEvent)
    assert result.text == "看图"
    assert result.has_media is True


def test_classify_group_anonymous_without_user_id_is_lenient():
    event = group_event(sub_type="anonymous")
    del event["user_id"]
    result = classify_onebot_event(event)
    assert isinstance(result, InboundEvent)
    assert result.chat_type == "group"
    assert result.sub_type == "anonymous"
    assert result.user_id == ""


def test_classify_group_anonymous_bad_content_falls_back_empty():
    event = group_event(sub_type="anonymous", message=12345)
    del event["raw_message"]
    del event["user_id"]
    result = classify_onebot_event(event)
    assert isinstance(result, InboundEvent)
    assert result.segments == ()
    assert result.text == ""
    assert result.has_media is False


def test_classify_group_notice_message_without_user_id_is_lenient():
    event = group_event(sub_type="notice")
    del event["user_id"]
    del event["message"]
    del event["raw_message"]
    result = classify_onebot_event(event)
    assert isinstance(result, InboundEvent)
    assert result.sub_type == "notice"
    assert result.user_id == ""
    assert result.text == ""


def test_classify_group_missing_message_id_is_no_message_id():
    event = group_event()
    del event["message_id"]
    assert reason_of(classify_onebot_event(event)) == "no_message_id"


def test_classify_group_missing_self_id_is_no_self_id():
    event = group_event()
    del event["self_id"]
    assert reason_of(classify_onebot_event(event)) == "no_self_id"


def test_classify_group_missing_group_id_is_malformed():
    event = group_event()
    del event["group_id"]
    assert reason_of(classify_onebot_event(event)) == "malformed"


def test_classify_group_normal_missing_user_id_is_malformed():
    event = group_event()
    del event["user_id"]
    assert reason_of(classify_onebot_event(event)) == "malformed"


def test_classify_group_normal_bad_content_is_unsupported_type():
    event = group_event(message=12345)
    del event["raw_message"]
    assert reason_of(classify_onebot_event(event)) == "unsupported_type"


# ---------------------------------------------------------------------------
# Matrix row 6: message_sent self echo
# ---------------------------------------------------------------------------


def test_classify_message_sent_is_self_echo_meta():
    result = classify_onebot_event(
        {
            "time": 1767225600,
            "self_id": SELF_ID,
            "post_type": "message_sent",
            "sub_type": "friend",
            "message_id": 555,
            "user_id": USER_ID,
            "status": {"online": True, "good": True},
        }
    )
    assert isinstance(result, MetaEvent)
    assert result.meta_event_type == "message_sent"
    assert result.direction == "self_echo"
    assert result.message_id == "555"
    assert result.user_id == USER_ID
    assert result.sub_type == "friend"
    assert result.status_online is True
    assert result.status_good is True


def test_classify_message_sent_with_message_type_stays_self_echo():
    # 真实 message_sent 报文带 message_type（golden message_sent_private
    # 同形）：方向先行，仍须定为 self_echo，绝不进私聊消息分支。
    result = classify_onebot_event(
        {
            "self_id": SELF_ID,
            "post_type": "message_sent",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": "golden-ms01",
            "user_id": SELF_ID,
            "message": "机器人自己发出的私聊",
            "raw_message": "机器人自己发出的私聊",
        }
    )
    assert isinstance(result, MetaEvent)
    assert result.direction == "self_echo"


def test_classify_message_sent_without_status_gives_none_flags():
    result = classify_onebot_event(
        {"self_id": SELF_ID, "post_type": "message_sent", "message_id": "s-1"}
    )
    assert isinstance(result, MetaEvent)
    assert result.status_online is None
    assert result.status_good is None
    assert result.group_id is None


# ---------------------------------------------------------------------------
# Matrix rows 7-19 + 28: notice subclasses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "notice_type,overrides",
    [
        ("group_upload", {"file": {"id": "f-1", "name": "fake.txt"}}),
        ("group_admin", {"sub_type": "set"}),
        ("group_decrease", {"sub_type": "kick", "operator_id": "777"}),
        ("group_increase", {"sub_type": "approve"}),
        ("group_ban", {"sub_type": "ban", "operator_id": "777", "duration": 60}),
        ("friend_add", {}),
        ("group_recall", {"operator_id": "777", "message_id": 4242}),
        ("friend_recall", {"message_id": 4243}),
        ("group_card", {"card_old": "旧", "card_new": "新"}),
        ("group_msg_emoji_like", {"message_id": 4244}),
        ("essence", {"sub_type": "add", "operator_id": "777", "message_id": 4245}),
        ("online_file_receive", {}),
        ("online_file_send", {}),
        ("bot_offline", {}),
    ],
)
def test_classify_notice_subclasses(notice_type, overrides):
    overrides.pop("group_id", None)
    event = notice_event(notice_type, **overrides)
    if notice_type == "friend_add" or notice_type == "friend_recall":
        event.pop("group_id", None)
    result = classify_onebot_event(event)
    assert isinstance(result, NoticeEvent), notice_type
    assert result.notice_type == notice_type
    assert result.self_id == SELF_ID
    assert result.user_id == USER_ID
    assert result.raw is event


def test_classify_notice_group_fields_and_actor_target_split():
    result = classify_onebot_event(
        notice_event("group_recall", operator_id=777, message_id=-42)
    )
    assert isinstance(result, NoticeEvent)
    assert result.group_id == GROUP_ID
    assert result.actor_id == "777"
    # operator 存在时 target 即事件对象（user_id 本人）。
    assert result.target_id == USER_ID
    assert result.message_id == "-42"


def test_classify_notice_without_operator_has_no_actor_or_target():
    result = classify_onebot_event(notice_event("friend_add"))
    assert isinstance(result, NoticeEvent)
    assert result.actor_id is None
    assert result.target_id is None
    assert result.group_id == GROUP_ID  # builder 带有 group_id，原样归一


def test_classify_notice_missing_self_id_is_not_ignored():
    event = notice_event("friend_add")
    del event["self_id"]
    result = classify_onebot_event(event)
    assert isinstance(result, NoticeEvent)
    assert result.self_id == ""


def test_classify_notice_sub_type_preserved():
    result = classify_onebot_event(notice_event("group_admin", sub_type="unset"))
    assert isinstance(result, NoticeEvent)
    assert result.sub_type == "unset"


# ---------------------------------------------------------------------------
# Matrix rows 20-27: notify subclasses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sub_type",
    [
        "poke",
        "lucky_king",
        "honor",
        "title",
        "profile_like",
        "input_status",
        "gray_tip",
        "group_name",
    ],
)
def test_classify_notify_subclasses(sub_type):
    result = classify_onebot_event(notice_event("notify", sub_type=sub_type))
    assert isinstance(result, NoticeEvent), sub_type
    assert result.notice_type == "notify"
    assert result.sub_type == sub_type


def test_classify_notify_doubtful_sub_types_are_accepted():
    # lucky_king/honor 单源存疑但官方事件表在列：classify 接受，
    # 存疑标记由下游负责，分类层不得拒绝（不断言平台存在性）。
    for sub_type in ("lucky_king", "honor"):
        result = classify_onebot_event(notice_event("notify", sub_type=sub_type))
        assert isinstance(result, NoticeEvent), sub_type


def test_classify_notify_unknown_sub_type_is_unknown_event_type():
    result = classify_onebot_event(notice_event("notify", sub_type="mystery_sub"))
    assert reason_of(result) == "unknown_event_type"


def test_classify_unknown_notice_type_is_unknown_event_type():
    result = classify_onebot_event(notice_event("group_mystery"))
    assert reason_of(result) == "unknown_event_type"


# ---------------------------------------------------------------------------
# Matrix rows 29-30: request friend / group add / group invite
# ---------------------------------------------------------------------------


def test_classify_request_friend():
    result = classify_onebot_event(request_event("friend"))
    assert isinstance(result, RequestEvent)
    assert result.request_type == "friend"
    assert result.sub_type is None
    assert result.user_id == USER_ID
    assert result.flag == "n3-flag-1"
    assert result.group_id is None


@pytest.mark.parametrize("sub_type", ["add", "invite"])
def test_classify_request_group_sub_types(sub_type):
    result = classify_onebot_event(
        request_event("group", sub_type=sub_type, group_id=GROUP_ID)
    )
    assert isinstance(result, RequestEvent)
    assert result.request_type == "group"
    assert result.sub_type == sub_type
    assert result.group_id == GROUP_ID


def test_classify_request_group_bad_sub_type_is_unknown_event_type():
    result = classify_onebot_event(
        request_event("group", sub_type="kick", group_id=GROUP_ID)
    )
    assert reason_of(result) == "unknown_event_type"


def test_classify_request_unknown_type_is_unknown_event_type():
    result = classify_onebot_event(request_event("channel"))
    assert reason_of(result) == "unknown_event_type"


def test_classify_request_missing_flag_is_malformed():
    event = request_event("friend")
    del event["flag"]
    assert reason_of(classify_onebot_event(event)) == "malformed"


def test_classify_request_empty_flag_is_malformed():
    assert reason_of(classify_onebot_event(request_event("friend", flag=""))) == "malformed"


def test_classify_request_missing_user_id_is_malformed():
    event = request_event("friend")
    del event["user_id"]
    assert reason_of(classify_onebot_event(event)) == "malformed"


# ---------------------------------------------------------------------------
# Matrix rows 31-32: meta lifecycle / heartbeat
# ---------------------------------------------------------------------------


def test_classify_meta_lifecycle():
    result = classify_onebot_event(meta_event("lifecycle", sub_type="connect"))
    assert isinstance(result, MetaEvent)
    assert result.meta_event_type == "lifecycle"
    assert result.sub_type == "connect"
    assert result.direction == "inbound"
    assert result.self_id == SELF_ID


def test_classify_meta_heartbeat_status_flags():
    result = classify_onebot_event(
        meta_event(
            "heartbeat",
            status={"online": True, "good": False},
            interval=5000,
        )
    )
    assert isinstance(result, MetaEvent)
    assert result.meta_event_type == "heartbeat"
    assert result.status_online is True
    assert result.status_good is False


def test_classify_meta_heartbeat_non_bool_status_gives_none():
    result = classify_onebot_event(
        meta_event("heartbeat", status={"online": "yes", "good": 1})
    )
    assert isinstance(result, MetaEvent)
    assert result.status_online is None
    assert result.status_good is None


def test_classify_unknown_meta_type_is_unknown_event_type():
    result = classify_onebot_event(meta_event("mystery"))
    assert reason_of(result) == "unknown_event_type"


# ---------------------------------------------------------------------------
# Reason division: unknown_event_type vs malformed
# ---------------------------------------------------------------------------


def test_classify_unknown_post_type_token_is_unknown_event_type():
    assert reason_of(classify_onebot_event({"post_type": "teleport"})) == (
        "unknown_event_type"
    )


def test_classify_unknown_message_type_is_unknown_event_type():
    result = classify_onebot_event(private_event(message_type="channel"))
    assert reason_of(result) == "unknown_event_type"


def test_classify_bare_dict_is_malformed():
    assert reason_of(classify_onebot_event({})) == "malformed"
    assert reason_of(classify_onebot_event({"unrelated": 1})) == "malformed"


@pytest.mark.parametrize("bad", [[1, 2, 3], "text", 42, None])
def test_classify_non_object_body_is_malformed(bad):
    result = classify_onebot_event(bad)
    assert reason_of(result) == "malformed"
    assert result.event_type_summary == "body:non_object"


def test_classify_invalid_post_type_shape_is_malformed():
    result = classify_onebot_event({"post_type": {"x": 1}})
    assert reason_of(result) == "malformed"


# ---------------------------------------------------------------------------
# Adapter method delegation
# ---------------------------------------------------------------------------


def test_adapter_classify_methods_delegate_to_module_function():
    notice = notice_event("friend_add")
    for adapter in (OneBotAdapter(), FakeAdapter()):
        result = adapter.classify(notice)
        assert isinstance(result, NoticeEvent), type(adapter).__name__
        grouped = adapter.classify(group_event())
        assert isinstance(grouped, InboundEvent), type(adapter).__name__
        assert grouped.chat_type == "group"


# ---------------------------------------------------------------------------
# Existing normalize behaviour is unchanged (spot checks)
# ---------------------------------------------------------------------------


def test_normalize_still_rejects_group_as_not_private():
    result = normalize_onebot_event(group_event())
    assert reason_of(result) == "not_private"
    assert "group" in result.event_type_summary


def test_normalize_still_rejects_notice_as_not_private():
    assert reason_of(normalize_onebot_event(notice_event("friend_add"))) == (
        "not_private"
    )


def test_normalize_private_defaults_unchanged():
    result = normalize_onebot_event(private_event())
    assert isinstance(result, InboundEvent)
    assert result.chat_type == "private"  # v2 默认值，未经 classify 补写
    assert result.sub_type is None
    assert result.session_id == f"onebot:{USER_ID}"


# ---------------------------------------------------------------------------
# F5 改判：结构畸形（字段缺失/非 str/非 token 形）→ malformed；
# 合法 token 但枚举外 → unknown_event_type（既有断言保持）
# ---------------------------------------------------------------------------


def test_classify_notice_missing_notice_type_is_malformed():
    # F5 改判：post_type=notice 但缺 notice_type 是结构失败。
    assert reason_of(classify_onebot_event({"post_type": "notice"})) == "malformed"


def test_classify_notice_type_dict_is_malformed():
    # F5 改判：notice_type 非 str 是结构失败。
    result = classify_onebot_event(notice_event({"x": 1}))
    assert reason_of(result) == "malformed"


def test_classify_request_type_list_is_malformed():
    # F5 改判：request_type 非 str 是结构失败。
    result = classify_onebot_event(request_event(["friend"]))
    assert reason_of(result) == "malformed"


def test_classify_meta_event_type_int_is_malformed():
    # F5 改判：meta_event_type 非 str 是结构失败。
    result = classify_onebot_event(meta_event(42))
    assert reason_of(result) == "malformed"


def test_classify_message_type_uppercase_is_malformed():
    # F5 改判：message_type 非 token 形（大写）是结构失败。
    result = classify_onebot_event(private_event(message_type="GROUP"))
    assert reason_of(result) == "malformed"
