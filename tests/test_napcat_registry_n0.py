"""N0 three-way consistency contract for the NapCat action registry.

Three independent sources are reconciled (full-interface integration plan
§2.6, verification record in docs/napcat-verification-n0.md):

- the research report's named action set (144 = 136 supported + 8 marked
  unsupported), frozen in ``tests/fixtures/napcat/report_action_set.json``;
- the local NapCat 4.18.41 bundle action-registry object (182 uncommented
  + 8 commented entries), frozen in
  ``tests/fixtures/napcat/bundle_registry_snapshot.json`` (re-extract with
  ``tests/scripts_n0/extract_bundle_registry.py`` after a NapCat upgrade);
- the code-side registry skeleton ``companion.adapters.action_registry``,
  which must carry an explicit disposition for every name in the union.

The event/segment coverage matrices (fixtures + docs/napcat-*-coverage.md)
are asserted here as well. Everything in this file is pure data: no
network, no live NapCat, no runtime behaviour.
"""

import ast
import json
from pathlib import Path

from companion.adapters.action_registry import (
    ACTION_REGISTRY,
    BUNDLE_MD5,
    REGISTRY_BY_NAME,
    integrated_count,
)

FIXTURES = Path(__file__).parent / "fixtures" / "napcat"
ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


SNAPSHOT = _load("bundle_registry_snapshot.json")
REPORT = _load("report_action_set.json")
EVENTS = _load("event_coverage.json")
SEGMENTS = _load("segment_coverage.json")

ACTIVE = {e["action"] for e in SNAPSHOT["active"]}
COMMENTED = {e["action"] for e in SNAPSHOT["commented"]}
SUPPORTED = set(REPORT["supported"])
UNSUPPORTED = set(REPORT["unsupported"])

DOC_CONFLICTS = {".get_word_slices", "get_unidirectional_friend_list", "set_restart"}
ABSENT_UNSUPPORTED = {
    "set_group_anonymous",
    "set_group_anonymous_ban",
    "qidian_get_account_info",
    "delete_unidirectional_friend",
    "reload_event_filter",
}
EXCLUDED_INTERNAL = {
    "clean_stream_temp_file",
    "test_auto_register_01",
    "test_auto_register_02",
    "unknown",
}
BUNDLE_ONLY_SUPPLEMENTS_EXCLUDED = 4


# ---------------------------------------------------------------- snapshot


def test_bundle_snapshot_integrity():
    assert len(SNAPSHOT["active"]) == 182
    assert len(SNAPSHOT["commented"]) == 8
    assert len(ACTIVE) == 182  # action names unique
    assert len({e["key"] for e in SNAPSHOT["active"]}) == 182  # keys unique
    assert SNAPSHOT["source"]["bundle_md5"] == BUNDLE_MD5
    assert SNAPSHOT["source"]["napcat_version"] == "4.18.41"
    assert COMMENTED == ABSENT_UNSUPPORTED | {
        "get_unidirectional_friend_list",  # duplicate registration, commented out
        "reboot_normal",
        "upload_forward_msg",
    }


def test_report_action_set_counts():
    assert len(SUPPORTED) == 136
    assert len(UNSUPPORTED) == 8
    assert not (SUPPORTED & UNSUPPORTED)


# ------------------------------------------------------- three-way reconcile


def test_report_supported_all_registered_in_bundle():
    # Plan §2.6 conclusion 2: report-supported names missing from the
    # uncommented bundle registry = 0.
    assert SUPPORTED - ACTIVE == set()


def test_report_unsupported_reconciliation():
    # The 8 report-unsupported names split into 3 in-registry doc conflicts
    # (local bundle wins) and 5 commented-only absences.
    assert UNSUPPORTED & ACTIVE == DOC_CONFLICTS
    assert UNSUPPORTED - ACTIVE == ABSENT_UNSUPPORTED
    assert ABSENT_UNSUPPORTED <= COMMENTED
    for name in DOC_CONFLICTS:
        entry = REGISTRY_BY_NAME[name]
        # N6 终审：.get_word_slices 经动作类抽取证实无 handler 类，
        # 自 registered 改判 excluded_internal（证据升级推翻 N0 的
        # 「仅凭注册表对象在册」登记，见其条目 notes）；其余两名
        # 维持 N0 裁决。
        allowed = {"registered", "ops_only"}
        if name == ".get_word_slices":
            allowed = {"excluded_internal"}
        assert entry.disposition in allowed
        assert entry.existence == "verified"


def test_bundle_only_candidates_all_dispositioned():
    bundle_only = ACTIVE - SUPPORTED - UNSUPPORTED
    assert len(bundle_only) == 43
    registered = {
        n for n in bundle_only if REGISTRY_BY_NAME[n].disposition == "registered"
    }
    excluded = {
        n for n in bundle_only if REGISTRY_BY_NAME[n].disposition == "excluded_internal"
    }
    assert excluded == EXCLUDED_INTERNAL
    assert len(registered) == 43 - BUNDLE_ONLY_SUPPLEMENTS_EXCLUDED
    assert registered | excluded == bundle_only


def test_registry_covers_union_of_all_sources():
    union = ACTIVE | COMMENTED | SUPPORTED | UNSUPPORTED
    assert union <= set(REGISTRY_BY_NAME)
    assert len(ACTION_REGISTRY) == 191  # 182 in-registry + 9 holds
    assert len(REGISTRY_BY_NAME) == len(ACTION_REGISTRY)  # names unique


def test_registry_arithmetic():
    by_disp = {}
    for entry in ACTION_REGISTRY:
        by_disp[entry.disposition] = by_disp.get(entry.disposition, 0) + 1
    # 2026-11-01 待验复核改判（证据升级，非口径漂移）：
    # pending_verification 两条经 bundle 全文穷举 + 现网复探销账，
    # get_group_todo → nonexistent（1 → 2）、
    # delete_unidirectional_friend → not_supported（4 → 5），
    # pending_verification 2 → 0（计数为零故键不再出现），
    # 见 docs/napcat-action-verify-2026-11-01.md。
    assert by_disp == {
        "registered": 172,
        "excluded_internal": 6,
        "ops_only": 2,
        "deprecated": 2,
        "not_supported": 5,
        "nonexistent": 2,
        "not_registered": 2,
    }
    canonical = [
        e for e in ACTION_REGISTRY if e.disposition == "registered" and not e.alias_of
    ]
    # N5 定案纠正（非口径漂移）：move/rename 经 bundle schema 证据
    # 复核为独立 canonical，别名条目 7 → 5、canonical 167 → 169。
    # N6 终审改判（证据升级）：test_download_stream 与
    # .get_word_slices 自 registered 改判 excluded_internal，
    # registered 174 → 172、canonical 169 → 167。
    assert len(canonical) == 167  # 172 registered - 5 alias entries


# ------------------------------------------------------------- entry hygiene


def test_registered_entries_are_complete():
    for entry in ACTION_REGISTRY:
        if entry.disposition != "registered":
            continue
        assert entry.domain and entry.tier and entry.level and entry.kind
        assert entry.owner_stage in {"N1", "N2", "N4", "N5", "N6"}
        assert entry.existence == "verified"
        assert entry.review_point


def test_holds_carry_reasons_and_review_points():
    for entry in ACTION_REGISTRY:
        if entry.disposition == "registered":
            continue
        assert entry.notes, entry.name
        assert entry.review_point, entry.name
        if entry.disposition in {
            "not_supported",
            "pending_verification",
            "nonexistent",
            "not_registered",
        }:
            assert entry.existence == "absent", entry.name
            # Plan §1.1: holds must carry a responsible stage — N6 closeout
            # owns the final registry audit for every open item.
            assert entry.owner_stage == "N6", entry.name
        if entry.disposition in {"ops_only", "excluded_internal", "deprecated"}:
            assert entry.owner_stage is None, entry.name


def test_alias_entries_point_at_canonical():
    for entry in ACTION_REGISTRY:
        if entry.alias_of is None:
            continue
        target = REGISTRY_BY_NAME[entry.alias_of]
        assert target.alias_of is None
        assert target.disposition == "registered"


# N2 landed set (plan §6 N2: message-form sends + L1 interaction
# mutations + get_ai_record), hand-listed like EXPECTED_METADATA so a
# silent registry edit fails here. N1 landed the canonical L0 set;
# N2 adds exactly these 31 canonical entries (aliases never count).
N2_LANDED = frozenset(
    {
        "send_private_msg", "send_group_msg", "send_msg",
        "send_group_forward_msg", "send_private_forward_msg",
        "send_forward_msg", "forward_friend_single_msg",
        "forward_group_single_msg", "send_group_ai_record",
        "send_ark_share", "send_group_ark_share", "get_mini_app_ark",
        "send_like", "set_msg_emoji_like", "friend_poke", "group_poke",
        "send_poke", "mark_msg_as_read", "mark_private_msg_as_read",
        "mark_group_msg_as_read", "_mark_all_as_read", "set_input_status",
        "click_inline_keyboard_button", "create_collection",
        "add_custom_face", "set_custom_face_desc", "set_online_status",
        "set_diy_online_status", "_get_model_show", "_set_model_show",
        "get_ai_record",
    }
)


# N4 landed set (plan §6 N4: L2 group/relationship/profile/ops actions
# behind the gateway authorization chain + L1 group sign/todo/remark and
# send_qzone_msg), hand-listed the same way: exactly these 39 canonical
# entries (the set_group_sign alias never counts).
N4_LANDED = frozenset(
    {
        "delete_msg", ".handle_quick_operation",
        "set_group_kick", "set_group_ban", "set_group_whole_ban",
        "set_group_admin", "set_group_card", "set_group_name",
        "set_group_special_title", "set_group_leave", "set_group_portrait",
        "set_group_add_request", "set_essence_msg", "delete_essence_msg",
        "_send_group_notice", "_del_group_notice", "set_group_kick_members",
        "set_group_add_option", "set_group_member_invite_policy",
        "set_group_member_permissions",
        "set_group_new_member_history_visibility", "set_group_robot_add_option",
        "set_group_search", "set_group_remark",
        "set_friend_add_request", "delete_friend",
        "set_doubt_friends_add_request", "delete_qzone_msg",
        "delete_custom_face", "set_qq_profile", "set_qq_avatar",
        "set_self_longnick", "clean_cache",
        "send_group_sign", "set_group_todo", "complete_group_todo",
        "cancel_group_todo", "set_friend_remark", "send_qzone_msg",
    }
)


# N6 landed set (plan §6 N6: the 8 L3 sensitive actions behind the
# controlled unlock — facade + gateway path with full Fake contracts;
# default remains hard-banned, see tests/test_l3_unlock_n6.py).
N6_LANDED = frozenset(
    {
        "get_cookies", "get_csrf_token", "get_credentials",
        "get_clientkey", "nc_get_rkey", "get_rkey", "get_rkey_server",
        "send_packet",
    }
)


# N5 landed set (plan §6 N5: file/media domain writes — E 9 + E2 12
# + F 7 canonical entries; move/rename count as canonical since the
# N5 alias correction, test_download_stream deliberately excluded).
N5_LANDED = frozenset(
    {
        "upload_private_file", "download_file", "upload_group_file",
        "delete_group_file", "create_group_file_folder", "delete_group_folder",
        "trans_group_file", "move_group_file", "rename_group_file",
        "create_flash_task", "send_flash_msg", "download_fileset",
        "send_online_file", "send_online_folder", "receive_online_file",
        "refuse_online_file", "cancel_online_file", "upload_file_stream",
        "download_file_stream", "download_file_image_stream",
        "download_file_record_stream",
        "upload_image_to_qun_album", "upload_images_to_qun_album",
        "upload_video_to_qun_album", "set_group_album_media_like",
        "cancel_group_album_media_like", "del_group_album_media",
        "do_group_album_comment",
    }
)


def test_five_tuple_state_and_level_defaults():
    # N1 landed the canonical L0 set, N2 the N2_LANDED set, N4 the
    # N4_LANDED set, N5 the N5_LANDED set and N6 the N6_LANDED (L3)
    # set (plan §1.1 counts only facade-implemented +
    # behaviour-verified entries): exactly those
    # 61 + 31 + 39 + 28 + 8 = 167 entries are True (L0 is 61 since N6
    # reclassified .get_word_slices to excluded_internal); every
    # other registered entry and every non-registered entry stays
    # False — i.e. every canonical registered entry is now landed.
    assert integrated_count() == 167
    for entry in ACTION_REGISTRY:
        landed = (
            entry.disposition == "registered"
            and entry.alias_of is None
            and (
                entry.level == "L0"
                or entry.name in N2_LANDED
                or entry.name in N4_LANDED
                or entry.name in N5_LANDED
                or entry.name in N6_LANDED
            )
        )
        assert entry.implemented is landed, entry.name
        assert entry.behavior_verified is landed, entry.name
        if entry.disposition != "registered":
            assert entry.default_enabled is False
        elif entry.level == "L0":
            assert entry.default_enabled is True
        else:  # L1/L2 default off, L3 hard-disabled by default
            assert entry.default_enabled is False


def test_schema_status_counts_and_unverified_families():
    verified = [e for e in ACTION_REGISTRY if e.schema == "verified"]
    # N5: 28 entries upgraded unverified -> verified on bundle payload
    # schema evidence (docs/napcat-schema-evidence-n5.md, the
    # "official action class" tier of the two-tier schema rule).
    # N6: 8 more upgraded on the same evidence tier (the L3 sensitive
    # family, get_share_link, get_unidirectional_friend_list and the
    # two ops_only entries; docs/napcat-schema-evidence-n6.md).
    assert len(verified) == 138
    # Flash-transfer / online-file / streaming family (E2): all
    # bundle-evidenced entries are verified since N5, and the last
    # holdout get_share_link was upgraded by N6 bundle evidence.
    e2 = [e for e in ACTION_REGISTRY if e.domain == "E2"]
    assert e2
    assert {e.name for e in e2 if e.schema == "unverified"} == set()
    # The L3 credential family was deliberately never probed live in
    # N0; N6 verified it from bundle action-class evidence instead
    # (extraction only, no live credential call ever happened).
    for name in ("get_rkey", "get_rkey_server", "get_clientkey", "send_packet"):
        assert REGISTRY_BY_NAME[name].schema == "verified"


def test_n0_naming_conclusions():
    # Canonical names settled in N0 (docs/napcat-verification-n0.md §3).
    assert REGISTRY_BY_NAME["trans_group_file"].alias_of is None
    # N5 correction (plan-assigned, not drift): bundle schema evidence
    # shows trans/move/rename are three independent actions with
    # different required fields, so move/rename are independent
    # canonical entries since N5, no longer aliases of trans.
    assert REGISTRY_BY_NAME["move_group_file"].alias_of is None
    assert REGISTRY_BY_NAME["rename_group_file"].alias_of is None
    assert REGISTRY_BY_NAME["get_group_ignored_notifies"].alias_of is None
    assert (
        REGISTRY_BY_NAME["get_group_ignore_add_request"].alias_of
        == "get_group_ignored_notifies"
    )
    for name in (
        "refuse_online_file",
        "get_online_file_msg",
        "del_group_album_media",
        "do_group_album_comment",
        "set_group_kick_members",
        "get_group_signed_list",
        "delete_group_folder",
        "get_private_file_url",
    ):
        entry = REGISTRY_BY_NAME[name]
        assert entry.disposition == "registered", name
        assert entry.existence == "verified", name
    # get_group_todo was an open verification item until the
    # 2026-11-01 re-verification (whole-bundle exhaustive search +
    # repeated live probe, both negative) closed it as nonexistent —
    # a concluded absence with an evidence trail, not a silent one
    # (docs/napcat-action-verify-2026-11-01.md).
    todo = REGISTRY_BY_NAME["get_group_todo"]
    assert todo.disposition == "nonexistent"
    assert todo.existence == "absent"
    # delete_unidirectional_friend closed in the same round as
    # not_supported: official ❌ plus a commented-out bundle line as
    # its only trace (static evidence only — never probed live,
    # delete semantics are a red line).
    unidirectional = REGISTRY_BY_NAME["delete_unidirectional_friend"]
    assert unidirectional.disposition == "not_supported"
    assert unidirectional.existence == "absent"


def test_level2_probe_names_are_registered():
    # The read-only get_* probes of docs/napcat-verification-n0.md §2 all
    # hit registered actions (400 schema-validation), never unsupported.
    probed = (
        "get_ai_characters",
        "get_ai_record",
        "get_fileset_id",
        "get_fileset_info",
        "get_share_link",
        "get_flash_file_list",
        "get_flash_file_url",
        "get_online_file_msg",
        "get_group_signed_list",
        "get_private_file_url",
        "get_emoji_likes",
        "get_group_detail_info",
        "get_group_share_link",
    )
    for name in probed:
        assert name in ACTIVE, name
        assert REGISTRY_BY_NAME[name].existence == "verified", name


def test_integrated_count_normalizes_aliases():
    # Plan §1.1: aliases are normalized before counting — one canonical
    # action plus its alias fully landed still counts as 1. (N5 note:
    # the trans/move/rename trio no longer serves as the alias example
    # — all three are independent canonical entries since the N5
    # correction and now count as 3; the sign pair is used instead.)
    from dataclasses import replace

    def landed(name):
        return replace(
            REGISTRY_BY_NAME[name], implemented=True, behavior_verified=True
        )

    pair = (landed("send_group_sign"), landed("set_group_sign"))
    assert integrated_count(pair) == 1
    assert integrated_count((landed("set_group_sign"),)) == 0  # alias alone
    trio = (
        landed("trans_group_file"),
        landed("move_group_file"),
        landed("rename_group_file"),
    )
    assert integrated_count(trio) == 3  # N5: three independent canonicals
    assert integrated_count((landed("bot_exit"),)) == 0  # ops_only never counts
    assert integrated_count((landed("get_msg"),)) == 1


# Per-action golden expectations, hand-written from the plan (§2.1 tables,
# §2.5 holds, §2.6 conclusions) for the load-bearing entries: L2/L3 risk
# surface, doc conflicts, naming settlements, alias canonicals, E2 family,
# and one representative per domain. A silent edit to any of these fields
# in the registry must fail here, not just shift a total.
EXPECTED_METADATA = {
    # name: (domain, tier, level, kind, schema, owner_stage)
    "delete_msg": ("A", "OB11", "L2", "mutation", "verified", "N4"),
    "send_private_msg": ("A", "OB11", "L1", "message", "verified", "N2"),
    "send_group_msg": ("A", "OB11", "L1", "message", "verified", "N2"),
    "send_forward_msg": ("A", "NC", "L1", "message", "verified", "N2"),
    "get_msg": ("A", "OB11", "L0", "query", "verified", "N1"),
    "translate_en2zh": ("A", "NC", "L0", "query", "unverified", "N1"),
    "get_ai_characters": ("A", "NC", "L0", "query", "verified", "N1"),
    "get_ai_record": ("A", "NC", "L1", "query", "unverified", "N2"),
    "set_msg_emoji_like": ("A", "NC", "L1", "mutation", "verified", "N2"),
    ".handle_quick_operation": ("A", "GCQ", "L2", "mutation", "verified", "N4"),
    # .get_word_slices left this table in N6: reclassified
    # excluded_internal (see test_n6_reclassified_entries below).
    "set_group_kick": ("B", "OB11", "L2", "mutation", "verified", "N4"),
    "set_group_kick_members": ("B", "NC", "L2", "mutation", "unverified", "N4"),
    "get_group_signed_list": ("B", "NC", "L0", "query", "unverified", "N1"),
    "get_group_ignored_notifies": ("B", "NC", "L0", "query", "unverified", "N1"),
    "send_group_sign": ("B", "GCQ", "L1", "mutation", "verified", "N4"),
    "set_friend_add_request": ("C", "OB11", "L2", "mutation", "verified", "N4"),
    "delete_friend": ("C", "GCQ", "L2", "mutation", "verified", "N4"),
    "get_unidirectional_friend_list": ("C", "OB11", "L0", "query", "verified", "N1"),
    "ocr_image": ("D", "GCQ", "L0", "query", "verified", "N5"),
    "upload_group_file": ("E", "GCQ", "L1", "mutation", "verified", "N5"),
    "trans_group_file": ("E", "NC", "L1", "mutation", "verified", "N5"),
    "move_group_file": ("E", "NC", "L1", "mutation", "verified", "N5"),
    "rename_group_file": ("E", "NC", "L1", "mutation", "verified", "N5"),
    "create_flash_task": ("E2", "NC", "L1", "mutation", "verified", "N5"),
    "send_flash_msg": ("E2", "NC", "L1", "message", "verified", "N5"),
    # test_download_stream left this table in N6: reclassified
    # excluded_internal (see test_n6_reclassified_entries below).
    "del_group_album_media": ("F", "NC", "L2", "mutation", "verified", "N5"),
    "upload_images_to_qun_album": ("F", "NC", "L1", "mutation", "verified", "N5"),
    "set_qq_profile": ("G", "GCQ", "L2", "mutation", "verified", "N4"),
    "send_ark_share": ("G", "NC", "L1", "message", "unverified", "N2"),
    "get_status": ("H", "OB11", "L0", "query", "verified", "N1"),
    "clean_cache": ("H", "OB11", "L2", "mutation", "verified", "N4"),
    "get_cookies": ("H", "OB11", "L3", "sensitive", "verified", "N6"),
    "get_clientkey": ("H", "NC", "L3", "sensitive", "verified", "N6"),
    "send_packet": ("H", "NC", "L3", "sensitive", "verified", "N6"),
    "nc_get_packet_status": ("H", "NC", "L0", "query", "verified", "N1"),
}


def test_golden_metadata_expectations():
    for name, expected in EXPECTED_METADATA.items():
        entry = REGISTRY_BY_NAME[name]
        actual = (
            entry.domain,
            entry.tier,
            entry.level,
            entry.kind,
            entry.schema,
            entry.owner_stage,
        )
        assert actual == expected, name
        assert entry.disposition == "registered", name


def test_n6_reclassified_entries():
    # N6 final-audit reclassifications (evidence upgrades, recorded in
    # the entries' notes and docs/napcat-schema-evidence-n6.md):
    # test_download_stream is NapCat-side test scaffolding (synthetic
    # stream, no core API); .get_word_slices has a registry key but no
    # handler class anywhere in the bundle. Both are terminal
    # excluded_internal, never facades, never landed.
    for name in ("test_download_stream", ".get_word_slices"):
        entry = REGISTRY_BY_NAME[name]
        assert entry.disposition == "excluded_internal", name
        assert entry.owner_stage is None, name
        assert entry.implemented is False, name
        assert entry.behavior_verified is False, name
        assert entry.default_enabled is False, name
        assert entry.alias_of is None, name
    # The extraction fact stands even though the disposition moved.
    assert REGISTRY_BY_NAME["test_download_stream"].schema == "verified"


def test_registry_module_is_pure_data():
    tree = ast.parse(
        (ROOT / "companion" / "adapters" / "action_registry.py").read_text(
            encoding="utf-8"
        )
    )
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"dataclasses"}


# ------------------------------------------------------------- matrices


def test_event_matrix_covers_report_vocabulary():
    rows = EVENTS["rows"]
    assert len(rows) == 32
    types = {r["type"] for r in rows} | {r["post_type"] for r in rows}
    subs = {r["sub_type"] for r in rows}
    for name in REPORT["notice_types"]:
        assert name in types or name in subs, name
    assert {"friend", "normal", "anonymous", "notice"} <= subs
    temp = [r for r in rows if r["sub_type"] == "group_temp"]
    assert temp and "temp_source" in temp[0]["notes"]


def test_event_matrix_doubtful_and_bundle_only_rows():
    rows = {(r["type"], r["sub_type"]): r for r in EVENTS["rows"]}
    for sub in ("lucky_king", "honor"):
        row = rows[("notify", sub)]
        assert row["verification"] == "single_source_doubtful"
        assert row["sources"] == ["report"]
    for typ in ("online_file_receive", "online_file_send"):
        assert rows[(typ, "")]["verification"] == "bundle_only"
    for sub in ("gray_tip", "group_name"):
        assert rows[("notify", sub)]["verification"] == "bundle_only"
    for sub in ("profile_like", "input_status"):
        assert rows[("notify", sub)]["verification"] == "bundle_confirmed"
    echo = [r for r in EVENTS["rows"] if r["post_type"] == "message_sent"]
    assert echo and echo[0]["direction"] == "self_echo"
    # Direction contract: self-echo is observation-only, never chained.
    assert echo[0]["disposition"] == "log_only"


def test_event_matrix_dispositions():
    rows = {(r["post_type"], r["type"], r["sub_type"]): r for r in EVENTS["rows"]}
    assert rows[("message", "private", "friend")]["disposition"] == "chain_private"
    assert rows[("message", "group", "normal")]["disposition"] == "chain_group"
    assert rows[("message", "group", "anonymous")]["disposition"] == "log_only"
    assert rows[("request", "friend", "")]["disposition"] == "chain_event"
    assert rows[("request", "group", "add/invite")]["disposition"] == "chain_event"
    assert rows[("notice", "notify", "poke")]["disposition"] == "chain_event"
    assert rows[("meta_event", "heartbeat", "")]["disposition"] == "log_only"


def test_segment_matrix():
    rows = SEGMENTS["rows"]
    assert len(rows) == 23
    by_type = {r["type"]: r for r in rows}
    # N5 rename note: the online-file segment's platform literal is
    # "onlinefile" (no underscore) — "online_file" has no bundle
    # segment evidence and only appears in action/event names.
    assert set(by_type) == {
        "text", "at", "reply", "face", "mface", "image", "record", "video",
        "file", "poke", "dice", "rps", "forward", "node", "json", "xml",
        "music", "contact", "location", "markdown", "miniapp",
        "flashtransfer", "onlinefile",
    }
    assert by_type["forward"]["disposition"] == "structured"
    assert by_type["node"]["disposition"] == "structured"
    assert by_type["node"]["direction"] == "outbound_primary"
    assert by_type["text"]["direction"] == "both"
    assert by_type["text"]["disposition"] == "first_class"
    assert by_type["image"]["current_model"] == "ImageSegment"
    assert by_type["json"]["disposition"] == "raw_with_fields"
    # N5: both segments are receive-only (inbound RawSegment with
    # required-field validation; outbound serialization rejects them —
    # the platform ob11->raw converters are empty stubs).
    for name in ("flashtransfer", "onlinefile"):
        assert by_type[name]["verification"] == "bundle_confirmed"
        assert by_type[name]["disposition"] == "receive_only"
        assert by_type[name]["direction"] == "inbound"
