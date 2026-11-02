"""RequestsConfig + RequestPolicy 分类型模式契约（群回复/好友自动同意任务）。

覆盖：friend approve_all、group/friend allowlist 分类型覆盖、
既有 manual/allowlist 口径回归、非法模式校验、config 层
requests 段（friend 可 approve_all、group 拒绝 approve_all、
缺省默认值）。
"""

import pytest

from companion.adapters.base import RequestEvent
from companion.config import ConfigValidationError, validate_app_config
from companion.ingress.events import RequestPolicy

SELF_ID = "10000"
USER = "20001"
OTHER_USER = "20002"
STRANGER = "99999"
GROUP = "30001"
OTHER_GROUP = "30002"


def _ev(request_type="friend", *, user_id=USER, group_id=None, flag="flag-1"):
    if request_type == "group" and group_id is None:
        group_id = GROUP
    return RequestEvent(
        platform="onebot",
        self_id=SELF_ID,
        request_type=request_type,
        sub_type="add" if request_type == "group" else None,
        user_id=user_id,
        group_id=group_id,
        flag=flag,
        raw={},
    )


def _minimal(**overrides):
    raw = {"allowed_private_user_ids": ["10001"]}
    raw.update(overrides)
    return raw


# ---------------------------------------------------------------------------
# friend approve_all


def test_friend_approve_all_approves_any_stranger():
    policy = RequestPolicy(friend_mode="approve_all")
    assert policy.decide(_ev("friend", user_id=STRANGER)) == "approve"
    assert policy.decide(_ev("friend", user_id=OTHER_USER, flag="f2")) == "approve"
    assert policy.decide(_ev("friend", user_id=USER, flag="f3")) == "approve"


def test_friend_approve_all_does_not_affect_group_events():
    policy = RequestPolicy(friend_mode="approve_all")
    # group 事件的有效模式回落基础 mode（默认 manual），不随 friend 走。
    assert policy.decide(_ev("group", user_id=STRANGER)) == "manual"
    assert policy.decide(_ev("group", user_id=USER)) == "manual"


# ---------------------------------------------------------------------------
# 分类型 allowlist


def test_group_mode_allowlist_hit_and_miss():
    policy = RequestPolicy(
        group_mode="allowlist",
        approve_user_ids={USER},
        approve_group_ids={GROUP},
    )
    assert policy.decide(_ev("group", user_id=USER, group_id=GROUP)) == "approve"
    # 群不在限定名单内 -> manual
    assert (
        policy.decide(_ev("group", user_id=USER, group_id=OTHER_GROUP, flag="g2"))
        == "manual"
    )
    # 申请人不在名单内 -> manual
    assert (
        policy.decide(_ev("group", user_id=OTHER_USER, group_id=GROUP, flag="g3"))
        == "manual"
    )
    # friend 事件不受 group_mode 影响，回落基础 manual
    assert policy.decide(_ev("friend", user_id=USER, flag="f9")) == "manual"


def test_group_mode_allowlist_without_group_scope_only_checks_user():
    policy = RequestPolicy(group_mode="allowlist", approve_user_ids={USER})
    assert policy.decide(_ev("group", user_id=USER, group_id=OTHER_GROUP)) == "approve"
    assert policy.decide(_ev("group", user_id=OTHER_USER, flag="g2")) == "manual"


def test_friend_mode_allowlist_outside_list_is_manual():
    policy = RequestPolicy(friend_mode="allowlist", approve_user_ids={USER})
    assert policy.decide(_ev("friend", user_id=USER)) == "approve"
    assert policy.decide(_ev("friend", user_id=STRANGER, flag="f2")) == "manual"
    # group 事件回落基础 manual，即使申请人命中名单也不自动同意
    assert policy.decide(_ev("group", user_id=USER, flag="g1")) == "manual"


def test_friend_mode_manual_overrides_base_allowlist_for_friend_only():
    policy = RequestPolicy(
        "allowlist", approve_user_ids={USER}, friend_mode="manual"
    )
    assert policy.decide(_ev("friend", user_id=USER)) == "manual"
    assert policy.decide(_ev("group", user_id=USER)) == "approve"


# ---------------------------------------------------------------------------
# 既有口径回归


def test_default_policy_is_all_manual():
    policy = RequestPolicy()
    assert policy.decide(_ev("friend", user_id=USER)) == "manual"
    assert policy.decide(_ev("group", user_id=USER)) == "manual"
    assert policy.has_auto() is False


def test_legacy_allowlist_mode_unchanged():
    policy = RequestPolicy("allowlist", approve_user_ids={USER})
    assert policy.decide(_ev("friend", user_id=USER)) == "approve"
    assert policy.decide(_ev("friend", user_id=OTHER_USER, flag="f2")) == "manual"
    assert policy.decide(_ev("group", user_id=USER)) == "approve"
    scoped = RequestPolicy(
        "allowlist", approve_user_ids={USER}, approve_group_ids={OTHER_GROUP}
    )
    assert scoped.decide(_ev("group", user_id=USER, group_id=GROUP)) == "manual"
    assert scoped.decide(_ev("group", user_id=USER, group_id=OTHER_GROUP)) == "approve"


def test_legacy_invalid_base_mode_still_rejected():
    with pytest.raises(ValueError):
        RequestPolicy("auto")
    with pytest.raises(ValueError):
        RequestPolicy("approve_all")


# ---------------------------------------------------------------------------
# 非法分类型模式 + has_auto


def test_invalid_friend_mode_raises_value_error():
    with pytest.raises(ValueError):
        RequestPolicy(friend_mode="auto")


def test_invalid_group_mode_raises_value_error():
    with pytest.raises(ValueError):
        RequestPolicy(group_mode="approve-all")


def test_has_auto_reflects_any_non_manual_mode():
    assert RequestPolicy().has_auto() is False
    assert RequestPolicy("allowlist").has_auto() is True
    assert RequestPolicy(friend_mode="approve_all").has_auto() is True
    assert RequestPolicy(group_mode="allowlist").has_auto() is True
    assert RequestPolicy(friend_mode="manual", group_mode="manual").has_auto() is False


# ---------------------------------------------------------------------------
# config 层


def test_config_accepts_requests_with_friend_approve_all():
    config = validate_app_config(
        _minimal(
            requests={
                "friend": "approve_all",
                "group": "allowlist",
                "approve_user_ids": [USER],
                "approve_group_ids": [GROUP],
            }
        )
    )
    assert config.requests.friend == "approve_all"
    assert config.requests.group == "allowlist"
    assert config.requests.approve_user_ids == (USER,)
    assert config.requests.approve_group_ids == (GROUP,)


def test_config_rejects_group_approve_all():
    with pytest.raises(ConfigValidationError) as raised:
        validate_app_config(_minimal(requests={"group": "approve_all"}))
    assert "requests.group" in str(raised.value)


def test_config_requests_defaults_when_section_missing():
    config = validate_app_config(_minimal())
    assert config.requests.friend == "manual"
    assert config.requests.group == "manual"
    assert config.requests.approve_user_ids == ()
    assert config.requests.approve_group_ids == ()
