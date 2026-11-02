"""N1 类型化门面契约（方案 §3.1）：FakeAdapter.actions 全量 L0 + 门控。

- 全部 canonical L0 动作（清单从 action_registry 程序化生成；N1
  时 62 个，N6 终审 .get_word_slices 改判后 61 个）
  经门面逐一调用：outcome=ok，且 FakeTransport 记录到的动作名
  与登记表 canonical 名逐一一致。
- 别名经 Gateway 归一到 canonical 名才到达 transport。
- SensitiveActions 八个 L3 方法全部被 Gateway hard_banned、不派发。
- MessagingActions 私聊无 DeliveryService 句柄被拒；有句柄时经
  Gateway 到达 transport。群发送 N1 时硬关（门面一律拒）；N2 起
  改判：两级额度落地后门面接线，有句柄 + policy 显式开启时经
  Gateway 到达 transport（运行时默认硬关移到 policy/DeliveryService
  层，见 test_message_forms_n2）——本文件该断言已按 N2 契约更新。
- 错误主体（llm_tool）调 L2 动作被 Gateway 以 principal_forbidden 拒。
"""

import asyncio
import json
from pathlib import Path

import pytest

from companion.adapters import FakeAdapter
from companion.adapters.action_registry import ACTION_REGISTRY
from companion.adapters.actions import ActionsBundle
from companion.adapters.gateway import PRINCIPAL_LLM_TOOL, PRINCIPAL_SYSTEM

_FIXTURES = Path(__file__).parent / "fixtures" / "napcat"

pytestmark = pytest.mark.timeout(10)

_USER = "10001"
_GROUP = "20001"


def _l0_canonical_names() -> list[str]:
    return sorted(
        entry.name
        for entry in ACTION_REGISTRY
        if entry.level == "L0"
        and entry.alias_of is None
        and entry.disposition == "registered"
    )


def _l0_calls(bundle) -> dict:
    """动作名 -> 无参构造的门面调用（必填参数按 OneBot 惯例给齐）。"""
    m, g, s, md, f = (
        bundle.messaging,
        bundle.group,
        bundle.social,
        bundle.media,
        bundle.files,
    )
    return {
        # A 域
        "get_msg": lambda: m.get_msg(message_id="30001"),
        "get_forward_msg": lambda: m.get_forward_msg(id="fwd-1"),
        "fetch_emoji_like": lambda: m.fetch_emoji_like(message_id="30001"),
        "get_ai_characters": lambda: m.get_ai_characters(group_id=_GROUP),
        "translate_en2zh": lambda: m.translate_en2zh(text="hello"),
        # N6 终审：.get_word_slices 已改判 excluded_internal 并下线
        # 门面（注册表有键、无 handler 类），不再是 L0 在册动作。
        # B 域
        "get_group_info": lambda: g.get_group_info(group_id=_GROUP),
        "get_group_info_ex": lambda: g.get_group_info_ex(group_id=_GROUP),
        "get_group_list": lambda: g.get_group_list(),
        "get_group_member_info": lambda: g.get_group_member_info(
            group_id=_GROUP, user_id=_USER
        ),
        "get_group_member_list": lambda: g.get_group_member_list(group_id=_GROUP),
        "get_group_honor_info": lambda: g.get_group_honor_info(group_id=_GROUP),
        "get_group_shut_list": lambda: g.get_group_shut_list(group_id=_GROUP),
        "get_group_signed_list": lambda: g.get_group_signed_list(group_id=_GROUP),
        "get_group_at_all_remain": lambda: g.get_group_at_all_remain(group_id=_GROUP),
        "get_group_system_msg": lambda: g.get_group_system_msg(),
        "get_group_ignored_notifies": lambda: g.get_group_ignored_notifies(),
        "get_essence_msg_list": lambda: g.get_essence_msg_list(group_id=_GROUP),
        "_get_group_notice": lambda: g.get_group_notice(group_id=_GROUP),
        "get_group_detail_info": lambda: g.get_group_detail_info(group_id=_GROUP),
        "get_group_share_link": lambda: g.get_group_share_link(group_id=_GROUP),
        # C 域
        "get_friend_list": lambda: s.get_friend_list(),
        "get_unidirectional_friend_list": lambda: s.get_unidirectional_friend_list(),
        "get_stranger_info": lambda: s.get_stranger_info(user_id=_USER),
        "get_recent_contact": lambda: s.get_recent_contact(),
        "get_profile_like": lambda: s.get_profile_like(),
        "nc_get_user_status": lambda: s.get_user_status(user_id=_USER),
        "get_robot_uin_range": lambda: s.get_robot_uin_range(),
        "fetch_custom_face": lambda: s.fetch_custom_face(),
        "get_collection_list": lambda: s.get_collection_list(),
        "get_friends_with_category": lambda: s.get_friends_with_category(),
        "get_doubt_friends_add_request": lambda: s.get_doubt_friends_add_request(),
        "fetch_custom_face_detail": lambda: s.fetch_custom_face_detail(),
        # D 域
        "get_group_msg_history": lambda: md.get_group_msg_history(group_id=_GROUP),
        "get_friend_msg_history": lambda: md.get_friend_msg_history(user_id=_USER),
        "get_image": lambda: md.get_image(file="img-1"),
        "get_record": lambda: md.get_record(file="rec-1"),
        "get_file": lambda: md.get_file(file_id="file-1"),
        "can_send_image": lambda: md.can_send_image(),
        "can_send_record": lambda: md.can_send_record(),
        "ocr_image": lambda: md.ocr_image(image="img-1"),
        "fetch_ptt_text": lambda: md.fetch_ptt_text(message_id="30001"),
        "get_emoji_likes": lambda: md.get_emoji_likes(message_id="30001"),
        # E/E2/F 域
        "get_private_file_url": lambda: f.get_private_file_url(
            user_id=_USER, file_id="file-1"
        ),
        "get_group_file_system_info": lambda: f.get_group_file_system_info(
            group_id=_GROUP
        ),
        "get_group_root_files": lambda: f.get_group_root_files(group_id=_GROUP),
        "get_group_files_by_folder": lambda: f.get_group_files_by_folder(
            group_id=_GROUP, folder_id="folder-1"
        ),
        "get_group_file_url": lambda: f.get_group_file_url(
            group_id=_GROUP, file_id="file-1"
        ),
        # N6 Review P2-1 修复后签名：必填按 bundle schema 证据给齐
        # （fileset_id / share_code），字段名与证据夹具逐一对齐的
        # 契约断言见 test_e2_read_facade_params_match_bundle_schema。
        "get_flash_file_list": lambda: f.get_flash_file_list(fileset_id="fs-1"),
        "get_flash_file_url": lambda: f.get_flash_file_url(fileset_id="fs-1"),
        "get_fileset_info": lambda: f.get_fileset_info(fileset_id="fs-1"),
        "get_fileset_id": lambda: f.get_fileset_id(share_code="sc-1"),
        "get_share_link": lambda: f.get_share_link(fileset_id="fs-1"),
        # N5 定案：门面签名按 bundle 证据由 message_id 改 user_id。
        "get_online_file_msg": lambda: f.get_online_file_msg(user_id=_USER),
        "get_qun_album_list": lambda: f.get_qun_album_list(group_id=_GROUP),
        "get_group_album_media_list": lambda: f.get_group_album_media_list(
            group_id=_GROUP
        ),
        # G 域
        "get_online_clients": lambda: bundle.profile.get_online_clients(),
        # H 域
        "get_login_info": lambda: bundle.system.get_login_info(),
        "get_status": lambda: bundle.system.get_status(),
        "get_version_info": lambda: bundle.system.get_version_info(),
        "nc_get_packet_status": lambda: bundle.system.get_packet_status(),
        "check_url_safely": lambda: bundle.system.check_url_safely(
            url="https://example.com/check"
        ),
    }


def test_all_61_canonical_l0_actions_reach_transport_via_facades():
    # N1 时为 62；N6 终审 .get_word_slices 改判 excluded_internal
    # 后 canonical L0 为 61（计数由登记表派生、逐名断言不变）。
    names = _l0_canonical_names()
    assert len(names) == 61
    fake = FakeAdapter()
    calls = _l0_calls(fake.actions)
    assert set(calls) == set(names)

    async def run():
        results = {}
        for name in names:
            results[name] = await calls[name]()
        return results

    results = asyncio.run(run())
    for name, result in results.items():
        assert result.outcome == "ok", (name, result)
    recorded = [call["action"] for call in fake.transport.calls]
    assert recorded == names or sorted(recorded) == names
    assert len(recorded) == 61


def test_alias_is_canonicalized_by_gateway_before_transport():
    fake = FakeAdapter()

    async def run():
        return await fake.action_gateway.invoke(
            "get_group_ignore_add_request", {}, principal=PRINCIPAL_SYSTEM
        )

    result = asyncio.run(run())
    assert result.outcome == "ok"
    assert fake.transport.calls[-1]["action"] == "get_group_ignored_notifies"


def test_sensitive_actions_are_all_hard_banned():
    fake = FakeAdapter()
    sensitive = fake.actions.sensitive

    async def run():
        return [
            # domain/cmd/data 为门面必填（补审第二段 P4 与网关规则
            # 对齐）：硬禁在参数校验之前，本用例口径不变。
            await sensitive.get_cookies("example.com"),
            await sensitive.get_csrf_token(),
            await sensitive.get_credentials("example.com"),
            await sensitive.get_clientkey(),
            await sensitive.get_rkey(),
            await sensitive.get_rkey_server(),
            await sensitive.nc_get_rkey(),
            # N6：send_packet 门面已类型化（cmd/data 必填）。
            await sensitive.send_packet(cmd="synthetic", data="00"),
        ]

    results = asyncio.run(run())
    assert len(results) == 8
    for result in results:
        assert result.outcome == "rejected"
        assert result.gate == "hard_banned"
    assert fake.transport.calls == []


def test_messaging_send_requires_delivery_handle():
    fake = FakeAdapter()

    async def run():
        refused = await fake.actions.messaging.send_private(_USER, "你好")
        assert refused.outcome == "rejected"
        assert refused.gate == "delivery_handle_required"
        assert fake.transport.calls == []
        # 有句柄时私聊经 Gateway 到达 transport（发送策略在此显式
        # 开启，句柄本身只代表 DeliveryService 已签发）。
        fake.action_gateway.policies["send_private_msg"] = {"enabled": True}
        fake.action_gateway.policies["send_group_msg"] = {"enabled": True}
        sent_private = await fake.actions.messaging.send_private(
            _USER, "你好", delivery_token="handle-1"
        )
        # 群发送 N2 改判（路线图 §6 N2 / C8）：N1 的门面一律拒被
        # 「两级额度落地后接线」取代——此处 policy 已显式开启
        # send_group_msg、有句柄，群发经 Gateway 到达 transport；
        # 默认关的硬关断言在 test_message_forms_n2（policy 未开 →
        # disabled、DeliveryService 群开关关 → 零派发）。
        sent_group = await fake.actions.messaging.send_group(
            _GROUP, "你好", delivery_token="handle-2"
        )
        return sent_private, sent_group

    sent_private, sent_group = asyncio.run(run())
    assert sent_private.outcome == "ok"
    assert sent_group.outcome == "ok"
    actions = [call["action"] for call in fake.transport.calls]
    assert actions == ["send_private_msg", "send_group_msg"]


def test_llm_tool_principal_is_forbidden_from_l2_actions():
    fake = FakeAdapter()
    llm_bundle = ActionsBundle(fake.action_gateway, principal=PRINCIPAL_LLM_TOOL)
    assert llm_bundle.principal == PRINCIPAL_LLM_TOOL

    async def run():
        return await fake.action_gateway.invoke(
            "set_friend_add_request",
            {"flag": "req-1"},
            principal=PRINCIPAL_LLM_TOOL,
        )

    result = asyncio.run(run())
    assert result.outcome == "rejected"
    assert result.gate == "principal_forbidden"
    assert fake.transport.calls == []


def test_e2_read_facade_params_match_bundle_schema():
    # N6 Review P2-1 回归：E2 读族五名的门面出参必须逐字段对齐
    # bundle schema 证据（字段名集合 = 必填 ∪ 实际给出的可选），
    # 且缺必填时网关本地即 invalid_params、不再到平台才 400。
    n5 = json.loads((_FIXTURES / "bundle_action_schemas.json").read_text("utf-8"))
    n6 = json.loads(
        (_FIXTURES / "bundle_action_schemas_n6.json").read_text("utf-8")
    )
    evidence = {**n5["actions"], **n6["actions"]}
    calls = {
        "get_flash_file_list": lambda f: f.get_flash_file_list(fileset_id="fs-1"),
        "get_flash_file_url": lambda f: f.get_flash_file_url(
            fileset_id="fs-1", file_name="a.txt", file_index=2
        ),
        "get_fileset_info": lambda f: f.get_fileset_info(fileset_id="fs-1"),
        "get_fileset_id": lambda f: f.get_fileset_id(share_code="sc-1"),
        "get_share_link": lambda f: f.get_share_link(fileset_id="fs-1"),
    }
    fake = FakeAdapter()
    files = fake.actions.files

    async def run():
        results = {}
        for name, call in calls.items():
            results[name] = await call(files)
        return results

    results = asyncio.run(run())
    sent = {c["action"]: c["params"] for c in fake.transport.calls}
    for name in calls:
        assert results[name].outcome == "ok", name
        fields = {f["name"] for f in evidence[name]["payload_fields"]}
        required = {
            f["name"] for f in evidence[name]["payload_fields"] if not f["optional"]
        }
        assert set(sent[name]) <= fields, (name, sent[name])
        assert required <= set(sent[name]), (name, sent[name])

    async def run_missing():
        return await fake.action_gateway.invoke(
            "get_share_link", {}, principal=PRINCIPAL_SYSTEM
        )

    missing = asyncio.run(run_missing())
    assert (missing.outcome, missing.gate) == ("rejected", "invalid_params")


#: bundle schema fixture 未覆盖的 N4/N5 CASES 动作（显式排除清单，
#: 补审第二段 P3）：两份 fixture 只含 N5/N6 抽取面（E/E2/F 文件域
#: 与 L3），N4 群域 35 个动作的 schema 证据在阶段文档而非 fixture。
#: 清单与 CASES 的差集由下方断言钉死——fixture 覆盖面扩大时本
#: 清单必须同步缩小，新覆盖的动作自动进入实质对账。
_SWEEP_ABSENT_FROM_FIXTURES = frozenset({
    ".handle_quick_operation", "_del_group_notice", "_send_group_notice",
    "cancel_group_todo", "clean_cache", "complete_group_todo",
    "delete_custom_face", "delete_essence_msg", "delete_msg",
    "delete_qzone_msg", "send_group_sign", "send_qzone_msg",
    "set_doubt_friends_add_request", "set_essence_msg",
    "set_friend_remark", "set_group_add_option", "set_group_add_request",
    "set_group_ban", "set_group_card", "set_group_kick",
    "set_group_kick_members", "set_group_member_invite_policy",
    "set_group_member_permissions", "set_group_name",
    "set_group_new_member_history_visibility", "set_group_portrait",
    "set_group_remark", "set_group_robot_add_option", "set_group_search",
    "set_group_special_title", "set_group_todo", "set_group_whole_ban",
    "set_qq_avatar", "set_qq_profile", "set_self_longnick",
})


def test_n4_n5_facade_params_match_bundle_schema():
    # 补审第二段 P3：参数名扫描从 E2 读族 5 个推广到 N4/N5 全部
    # CASES（复用两文件的参数化用例表与全接线 Fake）——门面实际
    # 出参逐个对 fixture 的 payload_fields 对账：出参名 ⊆ 字段表、
    # 证据必填（optional=False 且无 schema default，与网关
    # _PARAM_RULES 的派生口径一致）⊆ 出参。堵住 N6 P2-1 同类缺陷
    # （门面与手写期望一起改、平台字段名对不上仍全绿）。
    from tests.test_files_media_n5 import CASES as n5_cases
    from tests.test_files_media_n5 import wired_fake as n5_wired_fake
    from tests.test_group_domain_n4 import _cases as n4_cases
    from tests.test_group_domain_n4 import wired_fake as n4_wired_fake

    n5 = json.loads((_FIXTURES / "bundle_action_schemas.json").read_text("utf-8"))
    n6 = json.loads((_FIXTURES / "bundle_action_schemas_n6.json").read_text("utf-8"))
    evidence = {**n5["actions"], **n6["actions"]}

    absent = set()
    checked = 0
    for cases, make_fake in ((n4_cases(), n4_wired_fake), (n5_cases, n5_wired_fake)):
        for action, _expected, call in cases:
            fake = make_fake()
            result = asyncio.run(call(fake))
            assert result.outcome == "ok", action
            assert len(fake.transport.calls) == 1, action
            sent = fake.transport.calls[0]["params"]
            entry = evidence.get(action)
            if entry is None:
                absent.add(action)
                continue
            checked += 1
            fields = entry["payload_fields"]
            names = {f["name"] for f in fields}
            required = {
                f["name"]
                for f in fields
                if not f["optional"] and not f["has_default"]
            }
            assert set(sent) <= names, (action, sent)
            assert required <= set(sent), (action, sent)
    assert checked == 28
    assert absent == _SWEEP_ABSENT_FROM_FIXTURES
