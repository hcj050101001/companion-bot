"""N4 behaviour tests (NapCat 全接口方案 §6 N4 / §4 C3、C4 / §5 L2).

覆盖面：

- L2 全量接通：owner=N4 的 39 个动作（L2 33 + L1 6）经门面 + 网关
  授权链到达 Fake transport，动作名与登记表 canonical 一致、参数
  逐字对齐；
- 逐资源授权：无授权 / 范围不符在网关被拒且零派发（参数化）；
  llm_tool 主体禁 L2；每动作单独开关（开一个不带动其余）；
- L2 默认 dry_run=true：policy 未显式关 dry_run 时只 simulated；
- 确认令牌绑定：不可逆档（退群/删好友）无令牌只 simulated、
  令牌跨动作/跨目标/跨参数复用被拒、单次消费、revoke 后全失效、
  过期失效；
- 角色复核：resolver 缺失 fail closed、角色不足被拒、群主专属
  动作、缓存与 revoke 清缓存、生产 resolver 经 L0 查询工作；
- legacy 先例回归：qzone_post 迁 send_qzone_msg 后口径不变、
  friend_management 路径不走 canonical 授权链、但有授权下限
  （补审第二段 P3 的 c2b：account 级 AuthorizationContext 必备）；
- request 策略与通道：默认 manual、allowlist 只自动同意名单内、
  通道经授权上下文在 Fake 端到端处置群申请恰一次；
- 群回复通路：默认只观察（开关关零回复）、@ 触发、双维主体、
  默认不引用（剥 ReplySegment）、responder 故障不影响观察；
  装配断言默认子链只含解析/分类/观察。
"""

import asyncio

import pytest

from companion.adapters.base import (
    AtSegment,
    GroupTarget,
    InboundEvent,
    NodeSegment,
    ReplySegment,
    TextSegment,
)
from companion.adapters.fake import FakeAdapter, FakeTransport
from companion.adapters.gateway import (
    PRINCIPAL_LLM_TOOL,
    ActionGateway,
    AuthorizationContext,
    ConfirmationTokenStore,
    make_gateway_role_resolver,
    params_digest,
)
from companion.ingress import events as events_mod
from companion.ingress.events import (
    GROUP_SUBCHAIN,
    GatewayRequestChannel,
    GroupSubject,
    RequestDeferred,
    RequestPolicy,
    group_subject,
    handle_group_message,
    handle_request,
)
from companion.quota import SendQuota
from companion.services.delivery import DeliveryService
from tests import test_app

setup_app = test_app.setup_app

SELF_ID = "900000001"
GROUP = "80001"
OTHER_GROUP = "80002"
USER = "90001"
OTHER_USER = "90002"

AUTH_GROUP = AuthorizationContext(authorizer="owner-10001", groups=frozenset({GROUP}))
AUTH_USER = AuthorizationContext(authorizer="owner-10001", users=frozenset({USER}))
AUTH_ACCOUNT = AuthorizationContext(authorizer="owner-10001", account=True)

N4_POLICY_ACTIONS = (
    "delete_msg", ".handle_quick_operation",
    "set_group_kick", "set_group_ban", "set_group_whole_ban", "set_group_admin",
    "set_group_card", "set_group_name", "set_group_special_title", "set_group_leave",
    "set_group_portrait", "set_group_add_request", "set_essence_msg",
    "delete_essence_msg", "_send_group_notice", "_del_group_notice",
    "set_group_kick_members", "set_group_add_option", "set_group_member_invite_policy",
    "set_group_member_permissions", "set_group_new_member_history_visibility",
    "set_group_robot_add_option", "set_group_search", "set_group_remark",
    "set_friend_add_request", "delete_friend", "set_doubt_friends_add_request",
    "delete_qzone_msg", "delete_custom_face", "set_qq_profile", "set_qq_avatar",
    "set_self_longnick", "clean_cache",
    "send_group_sign", "set_group_todo", "complete_group_todo", "cancel_group_todo",
    "set_friend_remark", "send_qzone_msg",
)


def run(coro):
    return asyncio.run(coro)


class RoleStub:
    """role_resolver 替身：固定角色 + 记录被查群（验缓存）。"""

    def __init__(self, role):
        self.role = role
        self.calls = []

    async def __call__(self, group_id):
        self.calls.append(group_id)
        return self.role


def wired_fake(role="admin", actions=N4_POLICY_ACTIONS):
    """全接线 Fake：policy 逐动作开 + dry_run 显式关 + 角色替身。"""
    fake = FakeAdapter(
        policies={name: {"enabled": True, "dry_run": False} for name in actions}
    )
    fake.action_gateway.role_resolver = RoleStub(role)
    return fake


def issue(gateway, action, target, params):
    return gateway.confirmation_tokens.issue(
        action=action, target=target, params_digest=params_digest(params)
    )


# ---------------------------------------------------------------------------
# L2/L1 全量：经门面 + 授权链到达 transport
# ---------------------------------------------------------------------------


def _cases():
    """(期望动作名, 期望参数, 调用函数) 三元组：调用函数吃 (fake) 返回协程。"""
    g, u = GROUP, USER
    return [
        ("delete_msg", {"message_id": "42"},
         lambda f: f.actions.messaging.delete_msg("42", authorization=AUTH_ACCOUNT)),
        (".handle_quick_operation",
         {"context": {"post_type": "request"}, "operation": {"approve": True}},
         lambda f: f.actions.messaging.handle_quick_operation(
             {"post_type": "request"}, {"approve": True}, authorization=AUTH_ACCOUNT)),
        ("set_group_kick", {"group_id": g, "user_id": u},
         lambda f: f.actions.group.set_group_kick(g, u, authorization=AUTH_GROUP)),
        ("set_group_kick_members", {"group_id": g, "user_id": [u]},
         lambda f: f.actions.group.set_group_kick_members(g, [u], authorization=AUTH_GROUP)),
        ("set_group_ban", {"group_id": g, "user_id": u, "duration": 60},
         lambda f: f.actions.group.set_group_ban(g, u, duration=60, authorization=AUTH_GROUP)),
        ("set_group_whole_ban", {"group_id": g, "enable": True},
         lambda f: f.actions.group.set_group_whole_ban(g, enable=True, authorization=AUTH_GROUP)),
        ("set_group_card", {"group_id": g, "user_id": u, "card": "名片"},
         lambda f: f.actions.group.set_group_card(g, u, card="名片", authorization=AUTH_GROUP)),
        ("set_group_name", {"group_id": g, "group_name": "新群名"},
         lambda f: f.actions.group.set_group_name(g, "新群名", authorization=AUTH_GROUP)),
        ("set_group_special_title", {"group_id": g, "user_id": u, "special_title": "头衔"},
         lambda f: f.actions.group.set_group_special_title(
             g, u, special_title="头衔", authorization=AUTH_GROUP)),
        ("set_group_portrait", {"group_id": g, "file": "https://example.com/a.png"},
         lambda f: f.actions.group.set_group_portrait(
             g, "https://example.com/a.png", authorization=AUTH_GROUP)),
        ("set_group_add_request", {"flag": "flag-1", "sub_type": "add", "approve": True},
         lambda f: f.actions.group.set_group_add_request(
             "flag-1", "add", approve=True, authorization=AUTH_ACCOUNT)),
        ("set_essence_msg", {"message_id": "42"},
         lambda f: f.actions.group.set_essence_msg("42", authorization=AUTH_ACCOUNT)),
        ("delete_essence_msg", {"message_id": "42"},
         lambda f: f.actions.group.delete_essence_msg("42", authorization=AUTH_ACCOUNT)),
        ("_send_group_notice", {"group_id": g, "content": "公告内容"},
         lambda f: f.actions.group.send_group_notice(g, "公告内容", authorization=AUTH_GROUP)),
        ("_del_group_notice", {"group_id": g, "notice_id": "n1"},
         lambda f: f.actions.group.del_group_notice(g, "n1", authorization=AUTH_GROUP)),
        ("set_group_add_option", {"group_id": g},
         lambda f: f.actions.group.set_group_add_option(g, authorization=AUTH_GROUP)),
        ("set_group_member_invite_policy", {"group_id": g},
         lambda f: f.actions.group.set_group_member_invite_policy(g, authorization=AUTH_GROUP)),
        ("set_group_member_permissions", {"group_id": g},
         lambda f: f.actions.group.set_group_member_permissions(g, authorization=AUTH_GROUP)),
        ("set_group_new_member_history_visibility", {"group_id": g},
         lambda f: f.actions.group.set_group_new_member_history_visibility(
             g, authorization=AUTH_GROUP)),
        ("set_group_robot_add_option", {"group_id": g},
         lambda f: f.actions.group.set_group_robot_add_option(g, authorization=AUTH_GROUP)),
        ("set_group_search", {"group_id": g},
         lambda f: f.actions.group.set_group_search(g, authorization=AUTH_GROUP)),
        ("set_group_remark", {"group_id": g},
         lambda f: f.actions.group.set_group_remark(g, authorization=AUTH_GROUP)),
        ("set_doubt_friends_add_request", {"flag": "flag-2"},
         lambda f: f.actions.social.set_doubt_friends_add_request(
             "flag-2", authorization=AUTH_ACCOUNT)),
        ("delete_qzone_msg", {},
         lambda f: f.actions.social.delete_qzone_msg(authorization=AUTH_ACCOUNT)),
        ("delete_custom_face", {},
         lambda f: f.actions.social.delete_custom_face(authorization=AUTH_ACCOUNT)),
        ("set_qq_profile", {"nickname": "阿年"},
         lambda f: f.actions.profile.set_qq_profile(
             authorization=AUTH_ACCOUNT, nickname="阿年")),
        ("set_qq_avatar", {"file": "https://example.com/a.png"},
         lambda f: f.actions.profile.set_qq_avatar(
             "https://example.com/a.png", authorization=AUTH_ACCOUNT)),
        ("set_self_longnick", {"longnick": "签名"},
         lambda f: f.actions.profile.set_self_longnick("签名", authorization=AUTH_ACCOUNT)),
        ("clean_cache", {},
         lambda f: f.actions.system.clean_cache(authorization=AUTH_ACCOUNT)),
        ("send_group_sign", {"group_id": g},
         lambda f: f.actions.group.send_group_sign(g)),
        ("set_group_todo", {"group_id": g},
         lambda f: f.actions.group.set_group_todo(g)),
        ("complete_group_todo", {"group_id": g},
         lambda f: f.actions.group.complete_group_todo(g)),
        ("cancel_group_todo", {"group_id": g},
         lambda f: f.actions.group.cancel_group_todo(g)),
        ("set_friend_remark", {"user_id": u, "remark": "备注"},
         lambda f: f.actions.social.set_friend_remark(u, remark="备注")),
        ("send_qzone_msg", {"message": "说说内容"},
         lambda f: f.actions.social.send_qzone_msg("说说内容")),
    ]


@pytest.mark.timeout(10)
@pytest.mark.parametrize("action,params,call", _cases(), ids=[c[0] for c in _cases()])
def test_n4_actions_reach_transport_with_full_wiring(action, params, call):
    fake = wired_fake()
    result = run(call(fake))
    assert result.outcome == "ok", (action, result)
    assert fake.transport.calls == [
        {"method": "post_action", "action": action, "params": params}
    ]


@pytest.mark.timeout(10)
def test_n4_admin_and_token_tier_actions_reach_transport():
    # set_group_admin 须群主角色；退群/删好友/批好友申请走令牌（或确认）档。
    fake = wired_fake(role="owner")
    result = run(
        fake.actions.group.set_group_admin(
            GROUP, USER, enable=True, authorization=AUTH_GROUP
        )
    )
    assert result.outcome == "ok"

    fake = wired_fake()
    token = issue(fake.action_gateway, "set_group_leave", f"group:{GROUP}", {"group_id": GROUP})
    result = run(
        fake.actions.group.set_group_leave(
            GROUP, authorization=AUTH_GROUP, confirmation_token=token
        )
    )
    assert result.outcome == "ok"

    fake = wired_fake()
    token = issue(fake.action_gateway, "delete_friend", f"user:{USER}", {"user_id": USER})
    result = run(
        fake.actions.social.delete_friend(
            USER, authorization=AUTH_USER, confirmation_token=token
        )
    )
    assert result.outcome == "ok"

    fake = wired_fake()
    params = {"flag": "flag-9", "approve": True}
    token = issue(fake.action_gateway, "set_friend_add_request", "account", params)
    result = run(
        fake.actions.social.set_friend_add_request(
            "flag-9", approve=True, authorization=AUTH_ACCOUNT,
            confirmation_token=token,
        )
    )
    assert result.outcome == "ok"
    assert fake.transport.calls[0]["action"] == "set_friend_add_request"


# ---------------------------------------------------------------------------
# 逐资源授权：缺失 / 越界 / 主体 / 单独开关 / 默认 dry_run
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_l2_missing_authorization_is_rejected_without_dispatch():
    fake = wired_fake()
    for call in (
        lambda: fake.actions.group.set_group_kick(GROUP, USER),
        lambda: fake.actions.social.delete_friend(USER),
        lambda: fake.actions.profile.set_qq_profile(nickname="x"),
        lambda: fake.actions.group.set_essence_msg("42"),
    ):
        result = run(call())
        assert result.outcome == "rejected"
        assert result.gate == "authorization_missing"
    assert fake.transport.calls == []


@pytest.mark.timeout(10)
def test_l2_out_of_scope_targets_are_rejected_without_dispatch():
    fake = wired_fake()
    result = run(
        fake.actions.group.set_group_kick(OTHER_GROUP, USER, authorization=AUTH_GROUP)
    )
    assert (result.outcome, result.gate) == ("rejected", "authorization_scope")
    # user 目标拿群授权：范围种类不符同样拒（令牌在范围校验之后）。
    token = issue(
        fake.action_gateway, "delete_friend",
        f"user:{OTHER_USER}", {"user_id": OTHER_USER},
    )
    result = run(
        fake.actions.social.delete_friend(
            OTHER_USER, authorization=AUTH_GROUP, confirmation_token=token
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "authorization_scope")
    # account 动作拿群授权：拒。
    result = run(fake.actions.system.clean_cache(authorization=AUTH_GROUP))
    assert (result.outcome, result.gate) == ("rejected", "authorization_scope")
    assert fake.transport.calls == []


@pytest.mark.timeout(10)
def test_l2_llm_tool_principal_stays_forbidden():
    fake = wired_fake()
    result = run(
        fake.action_gateway.invoke(
            "set_group_kick",
            {"group_id": GROUP, "user_id": USER},
            principal=PRINCIPAL_LLM_TOOL,
            authorization=AUTH_GROUP,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "principal_forbidden")
    assert fake.transport.calls == []


@pytest.mark.timeout(10)
def test_l2_enable_is_per_action_not_per_domain():
    fake = wired_fake(actions=("set_group_kick",))
    ok = run(fake.actions.group.set_group_kick(GROUP, USER, authorization=AUTH_GROUP))
    assert ok.outcome == "ok"
    refused = run(fake.actions.group.set_group_ban(GROUP, USER, authorization=AUTH_GROUP))
    assert (refused.outcome, refused.gate) == ("rejected", "disabled")
    assert [c["action"] for c in fake.transport.calls] == ["set_group_kick"]


@pytest.mark.timeout(10)
def test_l2_dry_run_defaults_to_true_on_canonical_path():
    # policy 只开 enabled、未显式写 dry_run：L2 一律只模拟。
    fake = FakeAdapter(policies={"set_group_kick": {"enabled": True}})
    fake.action_gateway.role_resolver = RoleStub("admin")
    result = run(fake.actions.group.set_group_kick(GROUP, USER, authorization=AUTH_GROUP))
    assert (result.outcome, result.gate) == ("simulated", "dry_run")
    assert fake.transport.calls == []
    # 调用方显式 dry_run=True 时，policy 关了 dry_run 也只模拟。
    fake = wired_fake()
    result = run(
        fake.action_gateway.invoke(
            "set_group_kick",
            {"group_id": GROUP, "user_id": USER},
            principal="system",
            dry_run=True,
            authorization=AUTH_GROUP,
        )
    )
    assert (result.outcome, result.gate) == ("simulated", "dry_run")
    assert fake.transport.calls == []


# ---------------------------------------------------------------------------
# 确认令牌：绑定 / 单次 / 撤销 / 过期
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_irreversible_actions_need_a_bound_token_not_a_bool():
    fake = wired_fake()
    # 无令牌：只模拟（两阶段确认的第一阶段），零派发。
    result = run(fake.actions.group.set_group_leave(GROUP, authorization=AUTH_GROUP))
    assert (result.outcome, result.gate) == ("simulated", "pending_confirmation")
    # confirmed=True 布尔标记对不可逆档不算数（直接网关调用验证）。
    result = run(
        fake.action_gateway.invoke(
            "delete_friend",
            {"user_id": USER},
            principal="system",
            confirmed=True,
            authorization=AUTH_USER,
        )
    )
    assert (result.outcome, result.gate) == ("simulated", "pending_confirmation")
    assert fake.transport.calls == []


@pytest.mark.timeout(10)
def test_confirmation_token_binding_and_single_use():
    fake = wired_fake()
    gateway = fake.action_gateway
    params = {"group_id": GROUP}
    # 跨目标令牌：签给别的群 → invalid。
    foreign = issue(gateway, "set_group_leave", f"group:{OTHER_GROUP}", params)
    result = run(
        fake.actions.group.set_group_leave(
            GROUP, authorization=AUTH_GROUP, confirmation_token=foreign
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "confirmation_invalid")
    # 跨参数令牌：摘要按 is_dismiss=true 签、实际调用不带 → invalid。
    mismatched = issue(
        gateway, "set_group_leave", f"group:{GROUP}",
        {"group_id": GROUP, "is_dismiss": True},
    )
    result = run(
        fake.actions.group.set_group_leave(
            GROUP, authorization=AUTH_GROUP, confirmation_token=mismatched
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "confirmation_invalid")
    # 正确令牌：放行且单次——同令牌第二次调用即 invalid。
    token = issue(gateway, "set_group_leave", f"group:{GROUP}", params)
    result = run(
        fake.actions.group.set_group_leave(
            GROUP, authorization=AUTH_GROUP, confirmation_token=token
        )
    )
    assert result.outcome == "ok"
    result = run(
        fake.actions.group.set_group_leave(
            GROUP, authorization=AUTH_GROUP, confirmation_token=token
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "confirmation_invalid")
    assert [c["action"] for c in fake.transport.calls] == ["set_group_leave"]


@pytest.mark.timeout(10)
def test_confirmation_tokens_revoke_and_expire():
    now = [1000.0]
    store = ConfirmationTokenStore(clock=lambda: now[0])
    digest = params_digest({"group_id": GROUP})
    token = store.issue(
        action="set_group_leave", target=f"group:{GROUP}",
        params_digest=digest, ttl_seconds=30,
    )
    now[0] += 31  # 过期
    assert not store.consume(
        token, action="set_group_leave", target=f"group:{GROUP}", params_digest=digest
    )
    token = store.issue(
        action="set_group_leave", target=f"group:{GROUP}", params_digest=digest
    )
    store.revoke_all()  # 权限变更：已签发令牌立即失效
    assert not store.consume(
        token, action="set_group_leave", target=f"group:{GROUP}", params_digest=digest
    )

    # 网关级 revoke_authorizations 同步清角色缓存。
    fake = wired_fake()
    resolver = fake.action_gateway.role_resolver
    run(fake.actions.group.set_group_kick(GROUP, USER, authorization=AUTH_GROUP))
    run(fake.actions.group.set_group_kick(GROUP, OTHER_USER, authorization=AUTH_GROUP))
    assert resolver.calls == [GROUP]  # 第二次命中缓存
    fake.action_gateway.revoke_authorizations()
    run(fake.actions.group.set_group_kick(GROUP, USER, authorization=AUTH_GROUP))
    assert resolver.calls == [GROUP, GROUP]


# ---------------------------------------------------------------------------
# 角色复核
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_role_check_fail_closed_and_ranked():
    # 无 resolver：fail closed。
    fake = FakeAdapter(
        policies={"set_group_kick": {"enabled": True, "dry_run": False}}
    )
    result = run(fake.actions.group.set_group_kick(GROUP, USER, authorization=AUTH_GROUP))
    assert (result.outcome, result.gate) == ("rejected", "role_unverified")
    # 角色不足（member 踢人）：拒。
    fake = wired_fake(role="member")
    result = run(fake.actions.group.set_group_kick(GROUP, USER, authorization=AUTH_GROUP))
    assert (result.outcome, result.gate) == ("rejected", "role_insufficient")
    # 群主专属：admin 设管理员仍不足，owner 才行。
    fake = wired_fake(role="admin")
    result = run(
        fake.actions.group.set_group_admin(GROUP, USER, enable=True, authorization=AUTH_GROUP)
    )
    assert (result.outcome, result.gate) == ("rejected", "role_insufficient")
    # 解散须群主：admin + is_dismiss 令牌齐全仍被角色门拒。
    # Review P2-2 改判（原断言「令牌已在授权链消费」）：令牌改为
    # peek/consume 两段——非执行结果（角色门拒）不烧单次 nonce，
    # 角色修正后同一令牌仍可完成执行（且只执行一次）。
    fake = wired_fake(role="admin")
    params = {"group_id": GROUP, "is_dismiss": True}
    token = issue(fake.action_gateway, "set_group_leave", f"group:{GROUP}", params)
    result = run(
        fake.actions.group.set_group_leave(
            GROUP, is_dismiss=True, authorization=AUTH_GROUP, confirmation_token=token
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "role_insufficient")
    assert fake.transport.calls == []
    fake.action_gateway.role_resolver.role = "owner"
    fake.action_gateway._role_cache.clear()  # 正向缓存仍是旧角色，测试内手动清
    result = run(
        fake.actions.group.set_group_leave(
            GROUP, is_dismiss=True, authorization=AUTH_GROUP, confirmation_token=token
        )
    )
    assert result.outcome == "ok"
    assert [c["action"] for c in fake.transport.calls] == ["set_group_leave"]


@pytest.mark.timeout(10)
def test_gateway_role_resolver_via_l0_queries():
    transport = FakeTransport(
        responses={
            "get_login_info": {"status": "ok", "retcode": 0, "data": {"user_id": 900000001}},
            "get_group_member_info": {
                "status": "ok", "retcode": 0, "data": {"role": "admin"},
            },
        }
    )
    query_gateway = ActionGateway(transport)
    resolver = make_gateway_role_resolver(query_gateway)
    assert run(resolver(GROUP)) == "admin"
    assert run(resolver(GROUP)) == "admin"
    login_calls = [c for c in transport.calls if c["action"] == "get_login_info"]
    assert len(login_calls) == 1  # self_id 只查一次
    # 成员查询参数钉死（Review P3-9）：目标群与机器人 user_id 均
    # 按 OneBot 惯例转 int。
    member_calls = [c for c in transport.calls if c["action"] == "get_group_member_info"]
    assert member_calls and all(
        c["params"] == {"group_id": int(GROUP), "user_id": 900000001}
        for c in member_calls
    )

    # 平台拒绝时解析为 None（网关据此 role_unverified）。
    transport = FakeTransport(
        responses={
            "get_login_info": {"status": "failed", "retcode": 1200, "data": None},
        }
    )
    resolver = make_gateway_role_resolver(ActionGateway(transport))
    assert run(resolver(GROUP)) is None


# ---------------------------------------------------------------------------
# legacy 先例：qzone 迁移与 friend_management 路径口径不变
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_qzone_post_migrated_to_send_qzone_msg_with_same_gates():
    from companion.tools import HttpOneBotClient

    transport = FakeTransport()
    gateway = ActionGateway(
        transport, policies={"qzone": {"enabled": True, "dry_run": False, "rate_limit": 2}}
    )
    client = HttpOneBotClient(gateway=gateway)
    result = run(client.qzone_post("一条说说", dry_run=False))
    assert result["status"] == "ok"
    assert [c["action"] for c in transport.calls] == ["send_qzone_msg"]
    assert transport.calls[0]["params"] == {"message": "一条说说"}

    # 未配置 qzone：仍抛 PermissionError（旧 publish_message 口径）。
    client = HttpOneBotClient(gateway=ActionGateway(FakeTransport(), policies={}))
    with pytest.raises(PermissionError):
        run(client.qzone_post("一条说说", dry_run=False))
    # dry_run 默认真：模拟不派发（translate 回 dry_run 状态词）。
    transport = FakeTransport()
    gateway = ActionGateway(
        transport, policies={"qzone": {"enabled": True, "dry_run": False}}
    )
    client = HttpOneBotClient(gateway=gateway)
    assert run(client.qzone_post("一条说说")) == {"status": "dry_run"}
    assert transport.calls == []


@pytest.mark.timeout(10)
def test_friend_management_legacy_path_keeps_precedent_gates():
    from companion.tools import HttpOneBotClient

    transport = FakeTransport()
    gateway = ActionGateway(
        transport,
        policies={"friend_management": {"enabled": True, "dry_run": False}},
    )
    client = HttpOneBotClient(gateway=gateway)
    # 未确认 → pending（先例口径：授权上下文由垫片注入，确认门在此
    # 拦下；不走 canonical 授权链的令牌/角色段）。
    result = run(client.manage_friend(USER, "approve", dry_run=False, confirmed=False))
    assert result == {"status": "pending"}
    assert transport.calls == []
    # 已确认 → 经 set_friend_add_request 真派发（先例口径冻结；
    # 垫片注入的 account 级授权上下文过 c2b 下限，端到端不变）。
    result = run(client.manage_friend(USER, "approve", dry_run=False, confirmed=True))
    assert result["status"] == "ok"
    assert [c["action"] for c in transport.calls] == ["set_friend_add_request"]


@pytest.mark.timeout(10)
def test_friend_management_legacy_path_requires_authorization_floor():
    """补审第二段 P3：legacy 路径的授权下限（网关 c2b）。

    修复前：调用方自选 policy_key='friend_management' + 自断
    confirmed=True 即可无授权处置任意好友申请。现：绕开垫片直接
    调网关时，无授权上下文 → authorization_missing；只有群范围的
    上下文 → authorization_scope（声明目标为 account 级）；持
    account 级上下文 + confirmed 才按先例口径派发。
    """
    policies = {"friend_management": {"enabled": True, "dry_run": False}}
    transport = FakeTransport()
    gateway = ActionGateway(transport, policies=policies)
    result = run(
        gateway.invoke(
            "set_friend_add_request", {"flag": "flag-x"},
            principal="system", policy_key="friend_management", confirmed=True,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "authorization_missing")
    result = run(
        gateway.invoke(
            "set_friend_add_request", {"flag": "flag-x"},
            principal="system", policy_key="friend_management", confirmed=True,
            authorization=AUTH_GROUP,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "authorization_scope")
    assert transport.calls == []
    result = run(
        gateway.invoke(
            "set_friend_add_request", {"flag": "flag-x"},
            principal="system", policy_key="friend_management", confirmed=True,
            authorization=AUTH_ACCOUNT,
        )
    )
    assert result.outcome == "ok"
    assert [c["action"] for c in transport.calls] == ["set_friend_add_request"]


# ---------------------------------------------------------------------------
# request 策略与处置通道（N4）
# ---------------------------------------------------------------------------


def _request_event(flag, request_type="friend", user=USER, group=None):
    from companion.adapters.fake import FakeAdapter

    raw = {
        "time": 1767225600,
        "self_id": SELF_ID,
        "post_type": "request",
        "request_type": request_type,
        "user_id": user,
        "comment": "fake request",
        "flag": flag,
    }
    if request_type == "group":
        raw["sub_type"] = "add"
        raw["group_id"] = int(group or GROUP)
    return FakeAdapter().classify(raw)


def test_request_policy_defaults_to_manual_and_allowlist_only_approves_listed():
    friend_ev = _request_event("p-flag-1")
    assert RequestPolicy().decide(friend_ev) == "manual"
    policy = RequestPolicy("allowlist", approve_user_ids={USER})
    assert policy.decide(friend_ev) == "approve"
    assert policy.decide(_request_event("p-flag-2", user=OTHER_USER)) == "manual"
    # 群申请：申请人命中 + 群在限定名单内才自动同意；永不自动拒绝。
    group_ev = _request_event("p-flag-3", request_type="group")
    assert policy.decide(group_ev) == "approve"
    scoped = RequestPolicy(
        "allowlist", approve_user_ids={USER}, approve_group_ids={OTHER_GROUP}
    )
    assert scoped.decide(group_ev) == "manual"
    with pytest.raises(ValueError):
        RequestPolicy("auto")


def _make_request_store(tmp_path):
    from companion.memory import Database
    from companion.memory.requests import SqliteRequestStore

    return SqliteRequestStore(Database(tmp_path / "requests-n4.db"))


class _ProcStub:
    def __init__(self):
        from companion.pipeline.observers import StageMetrics

        self.metrics = StageMetrics()
        self.records = []

    def _log(self, event_type, length, allowed, latency_ms, **kwargs):
        self.records.append({"event_type": event_type, **kwargs})


@pytest.mark.timeout(10)
def test_group_request_disposed_once_via_authorized_channel(tmp_path):
    from companion.domain.requests import request_key

    fake = wired_fake(actions=("set_group_add_request", "set_friend_add_request"))
    channel = GatewayRequestChannel(
        fake.action_gateway,
        principal="system",
        confirmed=True,
        authorization=AUTH_ACCOUNT,
    )
    store = _make_request_store(tmp_path)
    proc = _ProcStub()
    ev = _request_event("g-flag-1", request_type="group")
    key = request_key(ev.self_id, ev.request_type, ev.sub_type, ev.flag)
    store.try_begin(key, user_id=ev.user_id, group_id=ev.group_id)

    class ApprovePolicy:
        def decide(self, _ev):
            return "approve"

    run(
        events_mod.handle_request(
            proc, ev, store=store, channel=channel, policy=ApprovePolicy()
        )
    )
    record = store.get(key)
    assert record.status == "done" and record.decision == "approved"
    calls = [c for c in fake.transport.calls if c["action"] == "set_group_add_request"]
    assert len(calls) == 1
    assert calls[0]["params"]["flag"] == "g-flag-1"

    # 重复驱动：状态机占用兜底，平台不二次处置。
    run(
        events_mod.handle_request(
            proc, ev, store=store, channel=channel, policy=ApprovePolicy()
        )
    )
    calls = [c for c in fake.transport.calls if c["action"] == "set_group_add_request"]
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 群回复通路与双维主体
# ---------------------------------------------------------------------------


def _group_event(segments, text="群里的话", user=USER, role="member"):
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


class _GroupProcStub:
    """群通路 proc 替身：metrics/safety/_log/allowed/delivery 五面。"""

    def __init__(self, *, delivery):
        from companion.pipeline.observers import StageMetrics

        self.metrics = StageMetrics()
        self.allowed = {"10001"}
        self.delivery = delivery
        self.logs = []

        class _Safety:
            @staticmethod
            def keyword_match(_text):
                return None

        self.safety = _Safety()

    def _log(self, event_type, length, allowed, latency_ms, **kwargs):
        self.logs.append({"event_type": event_type, **kwargs})


def _group_delivery():
    fake = FakeAdapter()
    delivery = DeliveryService(fake, SendQuota(), group_delivery_enabled=True)
    return fake, delivery


def test_group_subject_is_two_dimensional():
    ev = _group_event([TextSegment("x")], user="10001", role="admin")
    assert group_subject(ev, allowed_ids={"10001"}) == GroupSubject("owner", "admin")
    ev = _group_event([TextSegment("x")], user=USER, role="member")
    assert group_subject(ev, allowed_ids={"10001"}) == GroupSubject("stranger", "member")
    # 名单内用户在群里只是 member 时 person 仍 owner、role 独立判定。
    ev = _group_event([TextSegment("x")], user="10001", role="member")
    assert group_subject(ev, allowed_ids={"10001"}) == GroupSubject("owner", "member")
    ev = _group_event([TextSegment("x")], user=SELF_ID, role="owner")
    assert group_subject(ev, allowed_ids=set()).person == "system"


def test_group_subchain_assembly_default_is_observe_only():
    # Review P3-10 改判：group_subchain()/GROUP_REPLY_STAGES 是不被
    # 运行时使用的装饰性装配，已删除；子链契约只剩「永远只观察」，
    # 回复行为由 handle_group_message 承担并由下方通路测试钉住。
    assert set(GROUP_SUBCHAIN) == {"GroupNormalize", "GroupClassify", "SafetyObserve"}
    from companion.ingress import events as events_mod

    assert events_mod.subchain_for(_group_event([TextSegment("x")])) == GROUP_SUBCHAIN
    assert not hasattr(events_mod, "group_subchain")


@pytest.mark.timeout(10)
def test_group_reply_pathway_trigger_and_safety_boundaries():
    fake, delivery = _group_delivery()
    proc = _GroupProcStub(delivery=delivery)
    seen = []

    async def responder(ctx):
        seen.append(ctx)
        return "群里你好"

    # 无 responder（开关关）：只观察，零发送。
    ev = _group_event([AtSegment({"qq": SELF_ID}), TextSegment("在吗")], text="@阿年 在吗")
    run(handle_group_message(proc, ev, responder=None))
    assert fake.segment_sends == []

    # 有 responder 但未 @ 机器人：不触发。
    ev = _group_event([TextSegment("普通群聊")], text="普通群聊")
    run(handle_group_message(proc, ev, responder=responder))
    assert fake.segment_sends == [] and seen == []

    # @ 全体不算 @ 本人。
    ev = _group_event([AtSegment({"qq": "all"}), TextSegment("大家好")], text="大家好")
    run(handle_group_message(proc, ev, responder=responder))
    assert fake.segment_sends == []

    # @ 机器人：触发，responder 只拿到事件 + 双维主体（无私聊状态），
    # 发送经 DeliveryService 群通道到达 Fake。
    ev = _group_event(
        [AtSegment({"qq": int(SELF_ID)}), TextSegment("在吗")],
        text="@阿年 在吗", user="10001", role="admin",
    )
    run(handle_group_message(proc, ev, responder=responder))
    assert len(fake.segment_sends) == 1
    send = fake.segment_sends[0]
    assert isinstance(send["target"], GroupTarget)
    assert send["target"].group_id == GROUP
    assert [type(s).__name__ for s in send["segments"]] == ["TextSegment"]
    assert seen and seen[0].subject == GroupSubject("owner", "admin")
    assert not hasattr(seen[0], "history") and not hasattr(seen[0], "memory")


@pytest.mark.timeout(10)
def test_group_reply_strips_quote_and_survives_responder_failure():
    fake, delivery = _group_delivery()
    proc = _GroupProcStub(delivery=delivery)
    ev = _group_event([AtSegment({"qq": SELF_ID})], text="@阿年 说句话")

    async def quoting_responder(_ctx):
        return [ReplySegment({"id": "gm-0"}), TextSegment("不引用地回复")]

    run(handle_group_message(proc, ev, responder=quoting_responder))
    assert len(fake.segment_sends) == 1
    sent_types = [type(s).__name__ for s in fake.segment_sends[0]["segments"]]
    assert sent_types == ["TextSegment"]  # ReplySegment 已剥

    async def broken_responder(_ctx):
        raise RuntimeError("boom")

    run(handle_group_message(proc, ev, responder=broken_responder))
    assert len(fake.segment_sends) == 1  # 故障不发送、不外抛


@pytest.mark.timeout(10)
def test_group_reply_blocked_by_delivery_group_gate_when_switch_off():
    # 产品开关开（有 responder）但 DeliveryService 群门关（第二层结构
    # 门）：send_to 在预留前 rejected，零派发——两层开关缺一不可。
    fake = FakeAdapter()
    delivery = DeliveryService(fake, SendQuota(), group_delivery_enabled=False)
    proc = _GroupProcStub(delivery=delivery)
    ev = _group_event([AtSegment({"qq": SELF_ID})], text="@阿年 在吗")

    async def responder(_ctx):
        return "应该发不出去"

    run(handle_group_message(proc, ev, responder=responder))
    assert fake.segment_sends == []
    assert any(
        log.get("event_type") == "group_reply" and log.get("reason_code") == "rejected"
        for log in proc.logs
    )


@pytest.mark.timeout(10)
def test_processor_group_reply_defaults_off(setup_app):
    app, _calls, _alerter, _settings = setup_app
    processor = app.state.processor
    assert processor.group_reply_enabled is False
    assert processor.group_responder is None
    assert processor.delivery._group_delivery_enabled is False


# ---------------------------------------------------------------------------
# Claude Code Review 修复回归（P1-1 / P2-1 / P2-2 / P2-3 / P2-4 / P3）
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_p1_injected_target_params_cannot_rewrite_authorization_target():
    """P1-1：account 级动作注入 group_id/user_id 不得改写授权目标。

    修复前 derive_target 是动作无关的通用链：只持群范围授权即可
    用 clean_cache(group_id=G) 等形态越权执行 account 级动作。
    """
    fake = wired_fake()
    gw = fake.action_gateway
    # 清缓存是 account 动作：塞 group_id + 群授权 → 仍按 account 校验。
    result = run(
        gw.invoke(
            "clean_cache", {"group_id": GROUP},
            principal="system", authorization=AUTH_GROUP,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "authorization_scope")
    # 改头像是 account 动作：塞 user_id + 单用户授权 → 同样拒。
    result = run(
        gw.invoke(
            "set_qq_avatar", {"file": "https://example.com/a.png", "user_id": USER},
            principal="system", authorization=AUTH_USER,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "authorization_scope")
    # 删好友是 user 动作：塞 group_id + 群授权 → 目标仍是 user，拒；
    # 且不可逆档的范围校验先于令牌（未带令牌也先报 scope）。
    result = run(
        gw.invoke(
            "delete_friend", {"user_id": USER, "group_id": GROUP},
            principal="system", authorization=AUTH_GROUP,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "authorization_scope")
    # 群动作缺 group_id：目标派生为 none，account 授权也不放行。
    result = run(
        gw.invoke(
            "set_group_kick", {"user_id": USER},
            principal="system", authorization=AUTH_ACCOUNT,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "authorization_scope")
    assert fake.transport.calls == []
    # 对照：account 授权 + 无注入参数的 clean_cache 正常放行。
    result = run(fake.actions.system.clean_cache(authorization=AUTH_ACCOUNT))
    assert result.outcome == "ok"


@pytest.mark.timeout(10)
def test_token_cannot_cross_actions_and_issue_confirmation_helper():
    fake = wired_fake()
    gateway = fake.action_gateway
    # 跨动作复用（补 Review P3-9 指出的缺口）：为退群签发的令牌
    # 用到删好友上 → invalid（绑定含动作名）。
    foreign = issue(
        gateway, "set_group_leave", f"group:{GROUP}", {"group_id": GROUP}
    )
    result = run(
        fake.actions.social.delete_friend(
            USER, authorization=AUTH_USER, confirmation_token=foreign
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "confirmation_invalid")
    # 统一签发入口（P3-6）：issue_confirmation 内部派生目标/摘要，
    # 签发物可直接执行；参数漂移即 invalid。
    token = gateway.issue_confirmation("set_group_leave", {"group_id": GROUP})
    result = run(
        fake.actions.group.set_group_leave(
            GROUP, authorization=AUTH_GROUP, confirmation_token=token
        )
    )
    assert result.outcome == "ok"
    token = gateway.issue_confirmation(
        "delete_friend", {"user_id": USER}
    )
    both_users = AuthorizationContext(
        authorizer="owner-10001", users=frozenset({USER, OTHER_USER})
    )
    result = run(
        fake.actions.social.delete_friend(
            OTHER_USER, authorization=both_users, confirmation_token=token
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "confirmation_invalid")
    with pytest.raises(ValueError):
        gateway.issue_confirmation("no_such_action", {})


@pytest.mark.timeout(10)
def test_token_survives_dry_run_simulation():
    """P2-2：dry_run 模拟不烧令牌——关掉 dry_run 后同一令牌可执行。"""
    transport = FakeTransport()
    gateway = ActionGateway(
        transport,
        policies={"delete_friend": {"enabled": True}},  # dry_run 缺省 → L2 默认模拟
        role_resolver=RoleStub("admin"),
    )
    from companion.adapters import make_actions

    actions = make_actions(gateway)
    token = gateway.issue_confirmation("delete_friend", {"user_id": USER})
    result = run(
        actions.social.delete_friend(
            USER, authorization=AUTH_USER, confirmation_token=token
        )
    )
    assert (result.outcome, result.gate) == ("simulated", "dry_run")
    gateway.policies["delete_friend"]["dry_run"] = False
    result = run(
        actions.social.delete_friend(
            USER, authorization=AUTH_USER, confirmation_token=token
        )
    )
    assert result.outcome == "ok"
    assert [c["action"] for c in transport.calls] == ["delete_friend"]


@pytest.mark.timeout(10)
def test_role_resolver_scales_across_groups_without_self_throttling():
    """P2-1：生产解析器用专用 identity + 放宽限流，多群复核不被
    网关默认 6/60s 桶打成未核实；失败结果不入缓存（P3-7）。"""
    transport = FakeTransport(
        responses={
            "get_login_info": {"status": "ok", "retcode": 0, "data": {"user_id": 900000001}},
            "get_group_member_info": {
                "status": "ok", "retcode": 0, "data": {"role": "admin"},
            },
        }
    )
    gateway = ActionGateway(transport)
    resolver = make_gateway_role_resolver(gateway)
    for idx in range(8):  # 8 个群 > 默认桶 6/60s（同 identity 时）
        assert run(resolver(f"8100{idx}")) == "admin"
    # 网关层缓存口径：解析失败不缓存，下一次仍重试。
    calls = {"n": 0}

    async def flaky(_group_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else "admin"

    gateway2 = ActionGateway(FakeTransport(), role_resolver=flaky)
    assert run(gateway2._resolve_role(GROUP)) is None
    assert run(gateway2._resolve_role(GROUP)) == "admin"
    assert calls["n"] == 2
    # group_id 为空直接未核实，不查平台。
    assert run(gateway2._resolve_role(None)) is None


@pytest.mark.timeout(10)
def test_qzone_canonical_path_keeps_l2_principals_and_dry_default():
    """P2-4：send_qzone_msg 的 canonical 路径主体不放宽、默认 dry_run。"""
    transport = FakeTransport()
    gateway = ActionGateway(
        transport, policies={"send_qzone_msg": {"enabled": True}}
    )
    result = run(
        gateway.invoke(
            "send_qzone_msg", {"message": "你好"},
            principal=PRINCIPAL_LLM_TOOL,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "principal_forbidden")
    # policy 只开 enabled、未写 dry_run：canonical 默认模拟。
    result = run(
        gateway.invoke("send_qzone_msg", {"message": "你好"}, principal="system")
    )
    assert (result.outcome, result.gate) == ("simulated", "dry_run")
    assert transport.calls == []
    gateway.policies["send_qzone_msg"]["dry_run"] = False
    result = run(
        gateway.invoke("send_qzone_msg", {"message": "你好"}, principal="ops_cli")
    )
    assert result.outcome == "ok"
    assert [c["action"] for c in transport.calls] == ["send_qzone_msg"]


class _StubGateway:
    """request 通道映射测试用网关替身：按脚本返回 ActionResult。"""

    def __init__(self, result):
        self._result = result
        self.calls = []

    async def invoke(self, action, params, **kwargs):
        self.calls.append({"action": action, "params": params, **kwargs})
        return self._result


def _request_channel(result):
    from companion.adapters.gateway import ActionResult  # noqa: F401  (类型参照)

    return GatewayRequestChannel(
        _StubGateway(result),
        principal="system",
        confirmed=True,
        authorization=AUTH_ACCOUNT,
    )


def _friend_request_event(flag="flag-n4-review"):
    return FakeAdapter().classify(
        {
            "post_type": "request",
            "request_type": "friend",
            "sub_type": "",
            "self_id": int(SELF_ID),
            "user_id": int(USER),
            "flag": flag,
            "comment": "hi",
            "time": 1760000000,
        }
    )


@pytest.mark.timeout(10)
def test_request_channel_defers_local_gates_and_simulated(tmp_path):
    """P2-3：本地门禁/模拟未达平台 → ambiguous + 细分 result_code，
    绝不误记 failed/platform_rejected 终态。"""
    from companion.adapters.gateway import ActionResult
    from companion.domain.requests import request_key
    from companion.memory import Database
    from companion.memory.requests import SqliteRequestStore

    class _Proc:
        def __init__(self):
            from companion.pipeline.observers import StageMetrics

            self.metrics = StageMetrics()
            self.logs = []

        def _log(self, event_type, length, allowed, latency_ms, **kwargs):
            self.logs.append({"event_type": event_type, **kwargs})

    async def scenario(result, flag):
        store = SqliteRequestStore(Database(tmp_path / f"{flag}.db"))
        ev = _friend_request_event(flag)
        key = request_key(ev.self_id, ev.request_type, ev.sub_type, ev.flag)
        store.try_begin(key, user_id=ev.user_id, group_id=ev.group_id)
        channel = _request_channel(result)
        await handle_request(
            _Proc(), ev, store=store, channel=channel,
            policy=RequestPolicy(mode="allowlist", approve_user_ids={USER}),
        )
        return store.get(key)

    record = run(scenario(ActionResult(outcome="simulated", gate="dry_run"), "flag-sim"))
    assert record.status == "ambiguous"
    assert record.result_code == "gateway_simulated:dry_run"

    record = run(
        scenario(
            ActionResult(outcome="rejected", gate="authorization_scope"), "flag-scope"
        )
    )
    assert record.status == "ambiguous"
    assert record.result_code == "gateway_gate:authorization_scope"

    # 平台已应答的拒绝（raw 非空）仍是 failed/platform_rejected 终态。
    record = run(
        scenario(
            ActionResult(
                outcome="rejected", gate="platform",
                raw={"status": "failed", "retcode": 1200},
            ),
            "flag-plat",
        )
    )
    assert record.status == "failed"
    assert record.result_code == "platform_rejected"

    # RequestDeferred 本身可直接被通道测试消费。
    channel = _request_channel(ActionResult(outcome="simulated", gate="pending_confirmation"))
    with pytest.raises(RequestDeferred) as excinfo:
        run(channel.dispose(_friend_request_event(), approve=True))
    assert excinfo.value.result_code == "gateway_simulated:pending_confirmation"


@pytest.mark.timeout(10)
def test_group_reply_output_filtering_and_shape_normalization():
    """P3-2/P3-3：产出形态规范化（单段/生成器）、转发结构与 @全体
    被剥、生成器中途抛错只记 reply_error 不外抛。"""
    from companion.adapters.base import ForwardSegment

    fake, delivery = _group_delivery()
    proc = _GroupProcStub(delivery=delivery)
    ev = _group_event([AtSegment({"qq": SELF_ID})], text="@阿年 说句话")

    async def single_segment(_ctx):
        return TextSegment("单段形态")

    run(handle_group_message(proc, ev, responder=single_segment))
    assert len(fake.segment_sends) == 1

    async def fancy(_ctx):
        return [
            NodeSegment((TextSegment("节点内容"),)),
            ForwardSegment({"id": "fwd-1"}),
            AtSegment({"qq": "all"}),
            TextSegment("平面文本"),
        ]

    run(handle_group_message(proc, ev, responder=fancy))
    assert len(fake.segment_sends) == 2
    sent_types = [type(s).__name__ for s in fake.segment_sends[1]["segments"]]
    assert sent_types == ["TextSegment"]

    async def exploding_generator(_ctx):
        yield TextSegment("半句")
        raise RuntimeError("mid-iteration")

    run(handle_group_message(proc, ev, responder=exploding_generator))
    assert len(fake.segment_sends) == 2  # 未发送、不外抛


@pytest.mark.timeout(10)
def test_processor_group_reply_enabled_assembly(setup_app):
    """开关开装配（P3-9；2026-10-31 改判）：processor 读到 reply_enabled、
    DeliveryService 群门同步开，且默认生产 responder 已装配——原断言
    「responder 恒 None（通路就绪 ≠ 已接产品行为）」系 N4 落地时口径，
    用户 2026-10-31 拍板开启群回复后由产品接线取代（responder 语义与
    隔离口径见 test_group_requests_wiring.py）；开关关时仍恒 None 不变。"""
    from dataclasses import replace

    from companion.ingress import create_app

    _app, _calls, _alerter, settings = setup_app
    enabled_settings = replace(
        settings,
        app={**settings.app, "tools": {"group_chat": {"reply_enabled": True}}},
    )
    app = create_app(enabled_settings)
    processor = app.state.processor
    assert processor.group_reply_enabled is True
    assert processor.group_responder is not None
    assert processor.delivery._group_delivery_enabled is True


# ---------------------------------------------------------------------------
# 补审第二段（N4–N6 历史补审）修复回归
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_confirmation_token_concurrent_double_spend_only_one_wins():
    """P3：同令牌并发双花——两个并发 invoke 共用一枚不可逆档令牌，
    只许一个到达 transport，另一个 confirmation_invalid。修复前该
    性质只有注释声称、无并发用例钉住（peek 与 consume 之间存在
    role resolver 等 await 点，两竞态者可同时过 peek）。"""
    fake = wired_fake()
    params = {"user_id": USER}
    token = issue(fake.action_gateway, "delete_friend", f"user:{USER}", params)

    async def both():
        return await asyncio.gather(
            fake.actions.social.delete_friend(
                USER, authorization=AUTH_USER, confirmation_token=token
            ),
            fake.actions.social.delete_friend(
                USER, authorization=AUTH_USER, confirmation_token=token
            ),
        )

    results = run(both())
    outcomes = sorted((r.outcome, r.gate) for r in results)
    assert outcomes == [("ok", None), ("rejected", "confirmation_invalid")]
    assert [c["action"] for c in fake.transport.calls] == ["delete_friend"]


@pytest.mark.timeout(10)
def test_confirmation_token_survives_ledger_failure():
    """P3：账本故障不得烧令牌——try_begin 抛错回 ledger_unavailable
    后同一令牌仍 peek 有效，账本恢复后可真正执行（修复前 consume
    在 try_begin 之前，故障即毁单次确认）。"""
    from companion.adapters.gateway import InMemoryOperationLedger

    class RaisingLedger(InMemoryOperationLedger):
        def try_begin(self, operation_id, action):
            raise RuntimeError("ledger down")

    transport = FakeTransport()
    gateway = ActionGateway(
        transport,
        policies={"delete_friend": {"enabled": True, "dry_run": False}},
        operation_ledger=RaisingLedger(),
    )
    params = {"user_id": USER}
    token = gateway.issue_confirmation("delete_friend", params)
    result = run(
        gateway.invoke(
            "delete_friend", params, principal="system",
            authorization=AUTH_USER, confirmation_token=token,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "ledger_unavailable")
    assert transport.calls == []
    assert gateway.confirmation_tokens.peek(
        token, action="delete_friend", target=f"user:{USER}",
        params_digest=params_digest(params),
    )
    gateway.operation_ledger = InMemoryOperationLedger()
    result = run(
        gateway.invoke(
            "delete_friend", params, principal="system",
            authorization=AUTH_USER, confirmation_token=token,
        )
    )
    assert result.outcome == "ok"
    assert [c["action"] for c in transport.calls] == ["delete_friend"]


@pytest.mark.timeout(10)
def test_group_reply_framework_gate_blocks_system_self_trigger_by_default():
    """P3：群回复框架级准入门——默认拒 person=system 自触发（机器
    人自身回流 @ 自己不得自激），普通群员 @ 触发的产品口径不收窄。"""
    fake, delivery = _group_delivery()
    proc = _GroupProcStub(delivery=delivery)
    seen = []

    async def responder(ctx):
        seen.append(ctx)
        return "不该发出去"

    ev = _group_event(
        [AtSegment({"qq": SELF_ID}), TextSegment("自说自话")],
        text="@阿年 自说自话", user=SELF_ID, role="member",
    )
    run(handle_group_message(proc, ev, responder=responder))
    assert seen == [] and fake.segment_sends == []

    ev = _group_event(
        [AtSegment({"qq": SELF_ID}), TextSegment("在吗")],
        text="@阿年 在吗", user=USER, role="member",
    )
    run(handle_group_message(proc, ev, responder=responder))
    assert len(seen) == 1 and len(fake.segment_sends) == 1
    assert seen[0].subject.person == "stranger"


@pytest.mark.timeout(10)
def test_group_reply_framework_gate_allowlists_are_configurable():
    """P3：准入门的允许名单可配（两维合取）——给出名单时名单外
    主体/角色被框架拦下，名单内照常触发。"""
    fake, delivery = _group_delivery()
    proc = _GroupProcStub(delivery=delivery)
    seen = []

    async def responder(ctx):
        seen.append(ctx)
        return "名单内才回"

    ev = _group_event(
        [AtSegment({"qq": SELF_ID}), TextSegment("在吗")],
        text="@阿年 在吗", user=USER, role="member",
    )
    run(handle_group_message(proc, ev, responder=responder, allowed_persons={"owner"}))
    assert seen == [] and fake.segment_sends == []
    run(handle_group_message(proc, ev, responder=responder, allowed_roles={"admin"}))
    assert seen == [] and fake.segment_sends == []

    ev = _group_event(
        [AtSegment({"qq": SELF_ID}), TextSegment("在吗")],
        text="@阿年 在吗", user="10001", role="admin",
    )
    run(
        handle_group_message(
            proc, ev, responder=responder,
            allowed_persons={"owner"}, allowed_roles={"admin", "owner"},
        )
    )
    assert len(seen) == 1 and len(fake.segment_sends) == 1


@pytest.mark.timeout(10)
def test_ingress_wires_role_resolver_into_self_built_gateway(tmp_path, monkeypatch):
    """P3：角色解析器的生产装配断言（与 L3 解锁装配断言同范式）——
    自建 sender 的网关必须挂上 role_resolver：删掉这行接线，全套
    测试仍绿、但生产一切角色门 L2 动作恒 role_unverified；注入
    sender 时不代为改装其网关。"""
    from dataclasses import replace

    import yaml

    from companion.brain import CompanionBrain
    from companion.config import load_settings
    from companion.ingress import EventProcessor
    from companion.tools import HttpOneBotClient

    monkeypatch.setenv("ONEBOT_SECRET", "n4-assembly-test-secret")
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"allowed_private_user_ids": ["10001"]}), encoding="utf-8"
    )
    base = load_settings()

    class FakeLLM:
        async def generate(self, _prompt):
            return "ok"

        async def generate_json(self, _prompt):
            return "{}"

    def build(sender=None):
        settings = replace(
            base,
            onebot_secret="n4-assembly-test-secret",
            dev=False,
            root=tmp_path,
            app={**base.app, "sqlite_path": "memory.db"},
        )
        kwargs = {"brain": CompanionBrain(settings.persona, FakeLLM())}
        if sender is not None:
            kwargs["sender"] = sender
        return EventProcessor(settings, **kwargs)

    processor = build()
    assert processor.sender.action_gateway.role_resolver is not None
    # 复审 P3：同一装配点的另两条新接线一并钉住（删任一接线必须
    # 转红）——文件域资源根注入 data/files、群回复准入门默认口径。
    assert processor.sender.action_gateway.resource_roots == (
        str(tmp_path / "data" / "files"),
    )
    assert (
        processor.sender.action_gateway.resource_base_dir
        == str(tmp_path / "data" / "files")
    )
    assert processor.group_reply_blocked_persons == ("system",)
    assert processor.group_reply_allowed_persons is None
    assert processor.group_reply_allowed_roles is None

    injected = HttpOneBotClient()
    assert injected.action_gateway.role_resolver is None
    build(sender=injected)
    assert injected.action_gateway.role_resolver is None

    # 复审 P4（装配层下限）：app dict 直读路径下配置给空名单，
    # 有效 blocked 名单仍恒含 "system"（并集下限，防 dict 注入
    # 绕过 GroupChatToolConfig 的 validator）。
    floor_settings = replace(
        base,
        onebot_secret="n4-assembly-test-secret",
        dev=False,
        root=tmp_path,
        app={
            **base.app,
            "sqlite_path": "memory.db",
            "tools": {"group_chat": {"reply_blocked_persons": []}},
        },
    )
    floor_processor = EventProcessor(
        floor_settings, brain=CompanionBrain(floor_settings.persona, FakeLLM())
    )
    assert floor_processor.group_reply_blocked_persons == ("system",)


@pytest.mark.timeout(10)
def test_authorization_context_rejects_string_scopes():
    """P4：AuthorizationContext 构造期防御——groups/users 传 str
    时 covers() 的 in 会退化为子串匹配（短 ID 命中长 ID 范围）；
    现构造即 TypeError，其余集合归一为 frozenset[str]。"""
    with pytest.raises(TypeError):
        AuthorizationContext(authorizer="ops", groups="123456")
    with pytest.raises(TypeError):
        AuthorizationContext(authorizer="ops", users=b"90001")
    with pytest.raises(TypeError):
        AuthorizationContext(authorizer=123, groups=frozenset())
    ctx = AuthorizationContext(authorizer="ops", groups=["80001", 80002], users=[90001])
    assert ctx.groups == frozenset({"80001", "80002"})
    assert ctx.users == frozenset({"90001"})
    assert ctx.covers("group", "80001")
    assert not ctx.covers("group", "8000")


@pytest.mark.timeout(10)
def test_group_chat_config_blocked_persons_floor():
    """复审 P4（配置层）：reply_blocked_persons 必须恒含
    'system'——清空名单或移除 system 在配置校验期即失败，
    与 EventProcessor 的并集下限双层兜底。"""
    from pydantic import ValidationError

    from companion.config import GroupChatToolConfig

    with pytest.raises(ValidationError):
        GroupChatToolConfig(reply_blocked_persons=())
    with pytest.raises(ValidationError):
        GroupChatToolConfig(reply_blocked_persons=("stranger",))
    assert GroupChatToolConfig().reply_blocked_persons == ("system",)
    assert GroupChatToolConfig(
        reply_blocked_persons=("system", "stranger")
    ).reply_blocked_persons == ("system", "stranger")


@pytest.mark.timeout(10)
def test_confirmation_token_survives_dispatch_circuit_open():
    """复审 P3：派发点熔断打开时令牌不烧——circuit_open 退出后
    同一令牌 peek 仍有效，熔断恢复（冷却后 half-open 试探）后
    同一令牌可真正执行。"""
    from companion.adapters.circuit import ActionCircuitBreaker
    from tests.test_file_contracts_n5 import FakeClock

    clock = FakeClock()
    breaker = ActionCircuitBreaker(threshold=1, cooldown_seconds=60, clock=clock)
    transport = FakeTransport()
    gateway = ActionGateway(
        transport,
        policies={"delete_friend": {"enabled": True, "dry_run": False}},
        circuit_breaker=breaker,
        circuit_actions={"delete_friend"},
    )
    breaker.record_failure("delete_friend")
    assert breaker.state("delete_friend") == "open"
    params = {"user_id": USER}
    token = gateway.issue_confirmation("delete_friend", params)
    result = run(
        gateway.invoke(
            "delete_friend", params, principal="system",
            authorization=AUTH_USER, confirmation_token=token,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "circuit_open")
    assert transport.calls == []
    assert gateway.confirmation_tokens.peek(
        token, action="delete_friend", target=f"user:{USER}",
        params_digest=params_digest(params),
    )
    clock.advance(61)
    result = run(
        gateway.invoke(
            "delete_friend", params, principal="system",
            authorization=AUTH_USER, confirmation_token=token,
        )
    )
    assert result.outcome == "ok"
    assert [c["action"] for c in transport.calls] == ["delete_friend"]


@pytest.mark.timeout(10)
def test_confirmation_consume_failure_abandons_half_open_probe():
    """复审 P3：half-open 试探名额已被占时 consume 失败，必须
    abandon_probe 结清试探——否则 probing 悬挂、动作熔断死锁。
    断言：失败回 confirmation_invalid、试探世代已释放、同一
    令牌在下一轮试探中可真正执行（令牌亦未被烧）。"""
    from companion.adapters.circuit import ActionCircuitBreaker
    from companion.adapters.gateway import ConfirmationTokenStore
    from tests.test_file_contracts_n5 import FakeClock

    class FailSwitchStore(ConfirmationTokenStore):
        def __init__(self):
            super().__init__()
            self.fail_consume = False

        def consume(self, *args, **kwargs):
            if self.fail_consume:
                return False
            return super().consume(*args, **kwargs)

    clock = FakeClock()
    breaker = ActionCircuitBreaker(threshold=1, cooldown_seconds=60, clock=clock)
    store = FailSwitchStore()
    transport = FakeTransport()
    gateway = ActionGateway(
        transport,
        policies={"delete_friend": {"enabled": True, "dry_run": False}},
        circuit_breaker=breaker,
        circuit_actions={"delete_friend"},
        confirmation_tokens=store,
    )
    breaker.record_failure("delete_friend")
    clock.advance(61)  # 冷却已过：下一次派发将占 half-open 试探名额
    params = {"user_id": USER}
    token = gateway.issue_confirmation("delete_friend", params)
    store.fail_consume = True
    result = run(
        gateway.invoke(
            "delete_friend", params, principal="system",
            authorization=AUTH_USER, confirmation_token=token,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "confirmation_invalid")
    assert transport.calls == []
    # 试探名额已结清（未悬挂）：世代为空、熔断回到 open 计时。
    assert breaker.probe_generation("delete_friend") is None
    assert breaker.state("delete_friend") == "open"
    store.fail_consume = False
    clock.advance(61)
    result = run(
        gateway.invoke(
            "delete_friend", params, principal="system",
            authorization=AUTH_USER, confirmation_token=token,
        )
    )
    assert result.outcome == "ok"
    assert [c["action"] for c in transport.calls] == ["delete_friend"]
