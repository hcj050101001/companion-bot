"""N5 集成契约（NapCat 全接口方案 §6 N5 / §4 C18）：文件与媒体域。

覆盖面：

- 新接通 28 个 canonical（E 写 9 + E2 写 12 + F 写 7）经门面 +
  网关到达 Fake transport，动作名与参数逐字对齐 bundle schema
  证据（docs/napcat-schema-evidence-n5.md）；每动作 Fake 三态
  （成功 ok / 平台拒绝 rejected / 传输异常 ambiguous）；
- L2 删除三动作（delete_group_file / delete_group_folder /
  del_group_album_media）的授权链：授权缺失/越界、无令牌
  simulated、令牌错配拒绝、角色未核实/不足 fail closed；
- 网关相册熔断（默认 F 域派生）：连续失败打开、circuit_open
  拒绝 + warning 告警、门禁拒绝不计失败、Fake 时钟冷却后
  half-open 试探成功关闭 / 失败 reopen、非 F 域不受影响；
- 落盘审计：文件域审计行尾资源摘要（digest + size），不含真实
  路径/URL 原文；非文件域审计行无摘要；
- ResourceResolver 接线：根外/扩展名/base64 超限拒绝、合规放行、
  未注入时行为不变；
- 任务契约示范：FileTaskManager.run 包裹门面调用（成功/超时）、
  stream_upload 驱动 upload_file_stream 分块进度与中途取消；
- OCR 整合：经网关 Fake 三态 + 资源边界校验参与；
- receive-only 两段：入站解析产出 RawSegment 并校验关键字段
  （缺失判 malformed）、出站序列化明确拒绝。

全部离线：FakeTransport + tmp_path 本地临时文件，不出网、不发
真实消息、不真实上传下载。
"""

import asyncio
import base64
import hashlib
import logging
import os

import pytest

from companion.adapters import segments_to_onebot_message
from companion.adapters.base import Ignore, RawSegment, TextSegment
from companion.adapters.circuit import ActionCircuitBreaker
from companion.adapters.fake import FakeAdapter, FakeTransport
from companion.adapters.file_tasks import FileTaskManager, FileTaskState
from companion.adapters.gateway import (
    _PARAM_RULES,
    ActionGateway,
    AuthorizationContext,
)
from companion.adapters.onebot import normalize_onebot_event
from companion.adapters.resources import ResourceResolver
from companion.adapters.transport import TransportError

pytestmark = pytest.mark.timeout(10)

GROUP = "80001"
OTHER_GROUP = "80002"
USER = "90001"
ALBUM = "alb-1"

AUTH_GROUP = AuthorizationContext(authorizer="owner-10001", groups=frozenset({GROUP}))
AUTH_ACCOUNT = AuthorizationContext(authorizer="owner-10001", account=True)

OK = {"status": "ok", "retcode": 0, "data": {}}
FAILED = {"status": "failed", "retcode": 1200, "data": None}


def run(coro):
    return asyncio.run(coro)


class RoleStub:
    def __init__(self, role):
        self.role = role
        self.calls = []

    async def __call__(self, group_id):
        self.calls.append(group_id)
        return self.role


def _l2(call_with_token, action, params):
    """把需令牌的 L2 调用包装成 fake->coroutine（令牌按网关统一
    签发入口 issue_confirmation 绑定动作+目标+参数摘要）。"""

    def call(fake):
        token = fake.action_gateway.issue_confirmation(action, params)
        return call_with_token(fake, token)

    return call


def _cases():
    """(动作名, 期望 params, 调用函数) ——期望 params 与证据字段表逐字对齐。"""
    g, u = GROUP, USER
    url = "https://example.com/f.pdf"
    img = "https://example.com/a.png"
    return [
        ("upload_private_file", {"user_id": u, "file": url, "name": "f.pdf"},
         lambda f: f.actions.files.upload_private_file(u, url, "f.pdf")),
        ("download_file", {"url": url},
         lambda f: f.actions.files.download_file(url=url)),
        ("upload_group_file", {"group_id": g, "file": url, "name": "f.pdf"},
         lambda f: f.actions.files.upload_group_file(g, url, "f.pdf")),
        ("delete_group_file", {"group_id": g, "file_id": "file-1"},
         _l2(lambda f, t: f.actions.files.delete_group_file(
             g, "file-1", authorization=AUTH_GROUP, confirmation_token=t),
             "delete_group_file", {"group_id": g, "file_id": "file-1"})),
        ("create_group_file_folder", {"group_id": g, "folder_name": "资料"},
         lambda f: f.actions.files.create_group_file_folder(g, folder_name="资料")),
        ("delete_group_folder", {"group_id": g, "folder_id": "folder-1"},
         _l2(lambda f, t: f.actions.files.delete_group_folder(
             g, folder_id="folder-1", authorization=AUTH_GROUP, confirmation_token=t),
             "delete_group_folder", {"group_id": g, "folder_id": "folder-1"})),
        ("trans_group_file", {"group_id": g, "file_id": "file-1"},
         lambda f: f.actions.files.trans_group_file(g, "file-1")),
        ("move_group_file",
         {"group_id": g, "file_id": "file-1",
          "current_parent_directory": "/a", "target_parent_directory": "/b"},
         lambda f: f.actions.files.move_group_file(g, "file-1", "/a", "/b")),
        ("rename_group_file",
         {"group_id": g, "file_id": "file-1",
          "current_parent_directory": "/a", "new_name": "新名.pdf"},
         lambda f: f.actions.files.rename_group_file(g, "file-1", "/a", "新名.pdf")),
        ("create_flash_task", {"files": [url]},
         lambda f: f.actions.files.create_flash_task([url])),
        ("send_flash_msg", {"fileset_id": "fs-1", "group_id": g},
         lambda f: f.actions.files.send_flash_msg(
             "fs-1", group_id=g, delivery_token="n5-handle")),
        ("download_fileset", {"fileset_id": "fs-1"},
         lambda f: f.actions.files.download_fileset("fs-1")),
        ("send_online_file", {"user_id": u, "file_path": "docs/f.pdf"},
         lambda f: f.actions.files.send_online_file(
             u, "docs/f.pdf", delivery_token="n5-handle")),
        ("send_online_folder", {"user_id": u, "folder_path": "docs/dir"},
         lambda f: f.actions.files.send_online_folder(
             u, "docs/dir", delivery_token="n5-handle")),
        ("receive_online_file", {"user_id": u, "msg_id": "42", "element_id": "el-1"},
         lambda f: f.actions.files.receive_online_file(u, "42", "el-1")),
        ("refuse_online_file", {"user_id": u, "msg_id": "42", "element_id": "el-1"},
         lambda f: f.actions.files.refuse_online_file(u, "42", "el-1")),
        ("cancel_online_file", {"user_id": u, "msg_id": "42"},
         lambda f: f.actions.files.cancel_online_file(u, "42")),
        ("upload_file_stream",
         {"stream_id": "s-1", "chunk_data": "QUJD", "chunk_index": 0,
          "total_chunks": 3, "file_size": 3, "is_complete": False},
         lambda f: f.actions.files.upload_file_stream(
             "s-1", chunk_data="QUJD", chunk_index=0, total_chunks=3,
             file_size=3, is_complete=False)),
        ("download_file_stream", {"file_id": "file-1"},
         lambda f: f.actions.files.download_file_stream(file_id="file-1")),
        ("download_file_image_stream", {"file": img},
         lambda f: f.actions.files.download_file_image_stream(file=img)),
        ("download_file_record_stream", {"file_id": "file-1", "out_format": "mp3"},
         lambda f: f.actions.files.download_file_record_stream(
             file_id="file-1", out_format="mp3")),
        ("upload_image_to_qun_album",
         {"group_id": g, "album_id": ALBUM, "album_name": "相册", "file": img},
         lambda f: f.actions.album.upload_image_to_qun_album(g, ALBUM, "相册", img)),
        ("upload_images_to_qun_album",
         {"group_id": g, "album_id": ALBUM, "album_name": "相册",
          "files": [img, "https://example.com/b.png"]},
         lambda f: f.actions.album.upload_images_to_qun_album(
             g, ALBUM, "相册", [img, "https://example.com/b.png"])),
        ("upload_video_to_qun_album",
         {"group_id": g, "album_id": ALBUM, "album_name": "相册",
          "file": "https://example.com/v.mp4"},
         lambda f: f.actions.album.upload_video_to_qun_album(
             g, ALBUM, "相册", "https://example.com/v.mp4")),
        ("set_group_album_media_like",
         {"group_id": g, "album_id": ALBUM, "batch_id": "batch-1"},
         lambda f: f.actions.album.set_group_album_media_like(g, ALBUM, "batch-1")),
        ("cancel_group_album_media_like",
         {"group_id": g, "album_id": ALBUM, "batch_id": "batch-1"},
         lambda f: f.actions.album.cancel_group_album_media_like(g, ALBUM, "batch-1")),
        ("del_group_album_media",
         {"group_id": g, "album_id": ALBUM, "lloc": "lloc-1"},
         _l2(lambda f, t: f.actions.album.del_group_album_media(
             g, ALBUM, "lloc-1", authorization=AUTH_GROUP, confirmation_token=t),
             "del_group_album_media",
             {"group_id": g, "album_id": ALBUM, "lloc": "lloc-1"})),
        ("do_group_album_comment",
         {"group_id": g, "album_id": ALBUM, "batch_id": "batch-1", "content": "评论内容"},
         lambda f: f.actions.album.do_group_album_comment(
             g, ALBUM, "评论内容", batch_id="batch-1")),
    ]


CASES = _cases()
ALL_ACTIONS = tuple(c[0] for c in CASES)


def wired_fake(responses=None, role="admin"):
    """全接线 Fake：28 动作 policy 逐个开 + dry_run 显式关 + 角色替身。

    资源边界（补审第二段 P2）：网关步骤 d 的路径语义校验须有
    roots/base——本装配以进程 cwd 为基准根，CASES 的相对路径
    （docs/...）据此解析放行；穿越与界外路径的拒绝另有专测。
    """
    fake = FakeAdapter(
        responses=responses,
        policies={name: {"enabled": True, "dry_run": False} for name in ALL_ACTIONS},
    )
    fake.action_gateway.role_resolver = RoleStub(role)
    fake.action_gateway.resource_roots = (os.getcwd(),)
    fake.action_gateway.resource_base_dir = os.getcwd()
    return fake


# ------------------------------------------------------------ 三态契约


@pytest.mark.parametrize("action,params,call", CASES, ids=[c[0] for c in CASES])
def test_n5_actions_reach_transport(action, params, call):
    fake = wired_fake()
    result = run(call(fake))
    assert result.outcome == "ok", (action, result)
    assert fake.transport.calls == [
        {"method": "post_action", "action": action, "params": params}
    ]


@pytest.mark.parametrize("action,params,call", CASES, ids=[c[0] for c in CASES])
def test_n5_actions_platform_rejected(action, params, call):
    fake = wired_fake(responses={action: FAILED})
    result = run(call(fake))
    assert result.outcome == "rejected"
    assert result.error_code == "1200"
    assert len(fake.transport.calls) == 1


@pytest.mark.parametrize("action,params,call", CASES, ids=[c[0] for c in CASES])
def test_n5_actions_transport_error_is_ambiguous(action, params, call):
    fake = wired_fake(responses={action: TransportError(action, "TimeoutException")})
    result = run(call(fake))
    assert result.outcome == "ambiguous"
    assert result.error_code == "transport"
    assert len(fake.transport.calls) == 1


def test_n5_param_rules_match_evidence_required_fields():
    # 新增 24 条（download_file 与三个 download_*_stream 全字段可选、
    # 无必填，不设条目）；带 schema default 的 upload_file /
    # file_retention 不作必填。
    expected = {
        "upload_private_file": ("user_id", "file", "name"),
        "upload_group_file": ("group_id", "file", "name"),
        "delete_group_file": ("group_id", "file_id"),
        "create_group_file_folder": ("group_id",),
        "delete_group_folder": ("group_id",),
        "trans_group_file": ("group_id", "file_id"),
        "move_group_file": (
            "group_id", "file_id", "current_parent_directory", "target_parent_directory",
        ),
        "rename_group_file": (
            "group_id", "file_id", "current_parent_directory", "new_name",
        ),
        "create_flash_task": ("files",),
        "send_flash_msg": ("fileset_id",),
        "download_fileset": ("fileset_id",),
        "send_online_file": ("user_id", "file_path"),
        "send_online_folder": ("user_id", "folder_path"),
        "receive_online_file": ("user_id", "msg_id", "element_id"),
        "refuse_online_file": ("user_id", "msg_id", "element_id"),
        "cancel_online_file": ("user_id", "msg_id"),
        "upload_file_stream": ("stream_id",),
        "upload_image_to_qun_album": ("group_id", "album_id", "album_name", "file"),
        "upload_images_to_qun_album": ("group_id", "album_id", "album_name", "files"),
        "upload_video_to_qun_album": ("group_id", "album_id", "album_name", "file"),
        "set_group_album_media_like": ("group_id", "album_id", "batch_id"),
        "cancel_group_album_media_like": ("group_id", "album_id", "batch_id"),
        "del_group_album_media": ("group_id", "album_id", "lloc"),
        "do_group_album_comment": ("group_id", "album_id", "content"),
    }
    for name, required in expected.items():
        assert _PARAM_RULES[name] == (required, ()), name
    for name in (
        "download_file", "download_file_stream",
        "download_file_image_stream", "download_file_record_stream",
    ):
        assert name not in _PARAM_RULES, name


def test_n5_verified_schema_required_params_enforced():
    # schema 升 verified 后缺必填在网关 invalid_params、零派发：
    # move 缺 target_parent_directory（独立 canonical 的字段表）。
    fake = wired_fake()
    result = run(
        fake.action_gateway.invoke(
            "move_group_file",
            {"group_id": GROUP, "file_id": "file-1", "current_parent_directory": "/a"},
            principal="system",
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "invalid_params")
    assert fake.transport.calls == []


def test_n5_album_comment_requires_lloc_or_batch_id():
    # bundle 类级 check() 约束：lloc 与 batch_id 至少一个（门面拒绝）。
    fake = wired_fake()
    result = run(fake.actions.album.do_group_album_comment(GROUP, ALBUM, "评论内容"))
    assert (result.outcome, result.gate) == ("rejected", "invalid_params")
    assert fake.transport.calls == []
    result = run(
        fake.actions.album.do_group_album_comment(GROUP, ALBUM, "评论内容", lloc="lloc-1")
    )
    assert result.outcome == "ok"


def test_n5_move_rename_dispatch_under_their_own_canonical_names():
    # 定案纠正的网关面：别名表随登记表派生，move/rename 不再解析到 trans。
    from companion.adapters.gateway import resolve_canonical_name

    assert resolve_canonical_name("move_group_file") == "move_group_file"
    assert resolve_canonical_name("rename_group_file") == "rename_group_file"
    assert resolve_canonical_name("trans_group_file") == "trans_group_file"


# ------------------------------------------------------------ L2 删除授权链

L2_DELETES = (
    ("delete_group_file", {"group_id": GROUP, "file_id": "file-1"}),
    ("delete_group_folder", {"group_id": GROUP, "folder_id": "folder-1"}),
    ("del_group_album_media", {"group_id": GROUP, "album_id": ALBUM, "lloc": "lloc-1"}),
)


def _delete_call(fake, action, params, **gate):
    files = fake.actions.files
    album = fake.actions.album
    if action == "delete_group_file":
        return files.delete_group_file(GROUP, "file-1", **gate)
    if action == "delete_group_folder":
        return files.delete_group_folder(GROUP, folder_id="folder-1", **gate)
    return album.del_group_album_media(GROUP, ALBUM, "lloc-1", **gate)


@pytest.mark.parametrize("action,params", L2_DELETES, ids=[c[0] for c in L2_DELETES])
def test_n5_l2_delete_missing_authorization(action, params):
    fake = wired_fake()
    result = run(_delete_call(fake, action, params))
    assert (result.outcome, result.gate) == ("rejected", "authorization_missing")
    assert fake.transport.calls == []


@pytest.mark.parametrize("action,params", L2_DELETES, ids=[c[0] for c in L2_DELETES])
def test_n5_l2_delete_out_of_scope(action, params):
    fake = wired_fake()
    other = AuthorizationContext(
        authorizer="owner-10001", groups=frozenset({OTHER_GROUP})
    )
    token = fake.action_gateway.issue_confirmation(action, params)
    result = run(
        _delete_call(fake, action, params, authorization=other, confirmation_token=token)
    )
    assert (result.outcome, result.gate) == ("rejected", "authorization_scope")
    assert fake.transport.calls == []


@pytest.mark.parametrize("action,params", L2_DELETES, ids=[c[0] for c in L2_DELETES])
def test_n5_l2_delete_without_token_is_simulated(action, params):
    fake = wired_fake()
    result = run(_delete_call(fake, action, params, authorization=AUTH_GROUP))
    assert (result.outcome, result.gate) == ("simulated", "pending_confirmation")
    assert fake.transport.calls == []


@pytest.mark.parametrize("action,params", L2_DELETES, ids=[c[0] for c in L2_DELETES])
def test_n5_l2_delete_mismatched_token_rejected(action, params):
    fake = wired_fake()
    # 令牌绑定的是另一组参数（摘要不同）→ confirmation_invalid。
    mismatched_params = dict(params, file_id="other-file") if "file_id" in params else {
        **params, "lloc": "other-lloc",
    }
    if action == "delete_group_folder":
        mismatched_params = {**params, "folder_id": "other-folder"}
    token = fake.action_gateway.issue_confirmation(action, mismatched_params)
    result = run(
        _delete_call(fake, action, params, authorization=AUTH_GROUP, confirmation_token=token)
    )
    assert (result.outcome, result.gate) == ("rejected", "confirmation_invalid")
    assert fake.transport.calls == []


@pytest.mark.parametrize("action,params", L2_DELETES, ids=[c[0] for c in L2_DELETES])
def test_n5_l2_delete_role_unverified_fail_closed(action, params):
    # 无 role_resolver：令牌与授权齐全仍 fail closed（role_unverified）。
    fake = FakeAdapter(
        policies={action: {"enabled": True, "dry_run": False} for action in ALL_ACTIONS}
    )
    token = fake.action_gateway.issue_confirmation(action, params)
    result = run(
        _delete_call(fake, action, params, authorization=AUTH_GROUP, confirmation_token=token)
    )
    assert (result.outcome, result.gate) == ("rejected", "role_unverified")
    assert fake.transport.calls == []


@pytest.mark.parametrize("action,params", L2_DELETES, ids=[c[0] for c in L2_DELETES])
def test_n5_l2_delete_role_insufficient(action, params):
    fake = wired_fake(role="member")
    token = fake.action_gateway.issue_confirmation(action, params)
    result = run(
        _delete_call(fake, action, params, authorization=AUTH_GROUP, confirmation_token=token)
    )
    assert (result.outcome, result.gate) == ("rejected", "role_insufficient")
    assert fake.transport.calls == []


# ------------------------------------------------------------ 熔断


class FakeClock:
    def __init__(self, start=1000.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


ALBUM_ACTION = "upload_image_to_qun_album"
ALBUM_PARAMS = {
    "group_id": GROUP, "album_id": ALBUM, "album_name": "相册",
    "file": "https://example.com/a.png",
}


def _circuit_gateway(responses, clock, *, actions=None):
    breaker = ActionCircuitBreaker(threshold=3, cooldown_seconds=300, clock=clock)
    transport = FakeTransport(responses=responses)
    gateway = ActionGateway(
        transport,
        policies={ALBUM_ACTION: {"enabled": True}},
        circuit_breaker=breaker,
    )
    return gateway, transport, breaker


def _invoke_album(gateway):
    return run(
        gateway.invoke(ALBUM_ACTION, ALBUM_PARAMS, principal="system", rate_limit=1000)
    )


def test_circuit_default_actions_are_f_domain_canonicals():
    gateway = ActionGateway(FakeTransport())
    assert gateway.circuit_actions == frozenset(
        {
            "get_qun_album_list", "upload_image_to_qun_album",
            "upload_images_to_qun_album", "upload_video_to_qun_album",
            "set_group_album_media_like", "get_group_album_media_list",
            "del_group_album_media", "do_group_album_comment",
            "cancel_group_album_media_like",
        }
    )
    assert gateway.circuit_breaker.threshold == 3
    assert gateway.circuit_breaker.cooldown_seconds == 300


def test_circuit_opens_after_consecutive_failures_and_warns(caplog):
    clock = FakeClock()
    gateway, transport, breaker = _circuit_gateway({ALBUM_ACTION: FAILED}, clock)
    for _ in range(3):
        result = _invoke_album(gateway)
        assert result.outcome == "rejected" and result.error_code == "1200"
    assert breaker.state(ALBUM_ACTION) == "open"
    with caplog.at_level(logging.WARNING, logger="companion.actions"):
        result = _invoke_album(gateway)
    assert (result.outcome, result.gate) == ("rejected", "circuit_open")
    assert len(transport.calls) == 3  # 打开后零派发
    assert ALBUM_ACTION in caplog.text and "circuit_open" in caplog.text
    assert "open" in caplog.text


def test_circuit_gate_rejections_do_not_count_as_failures():
    clock = FakeClock()
    breaker = ActionCircuitBreaker(threshold=3, cooldown_seconds=300, clock=clock)
    transport = FakeTransport()
    gateway = ActionGateway(
        transport,
        policies={ALBUM_ACTION: {"enabled": False}},
        circuit_breaker=breaker,
    )
    for _ in range(5):  # disabled 门禁拒绝不计失败
        result = _invoke_album(gateway)
        assert (result.outcome, result.gate) == ("rejected", "disabled")
    assert breaker.state(ALBUM_ACTION) == "closed"
    gateway.policies[ALBUM_ACTION]["enabled"] = True
    # invalid_params（缺必填）同样不计。
    for _ in range(3):
        result = run(
            gateway.invoke(ALBUM_ACTION, {"group_id": GROUP}, principal="system")
        )
        assert result.gate == "invalid_params"
    assert breaker.state(ALBUM_ACTION) == "closed"
    assert transport.calls == []


def test_circuit_half_open_probe_success_closes():
    clock = FakeClock()
    gateway, transport, breaker = _circuit_gateway({ALBUM_ACTION: FAILED}, clock)
    for _ in range(3):
        _invoke_album(gateway)
    assert breaker.state(ALBUM_ACTION) == "open"
    clock.advance(301)  # 冷却已过 → half-open 试探
    transport.responses[ALBUM_ACTION] = OK
    result = _invoke_album(gateway)
    assert result.outcome == "ok"
    assert breaker.state(ALBUM_ACTION) == "closed"
    assert _invoke_album(gateway).outcome == "ok"
    assert len(transport.calls) == 5


def test_circuit_half_open_probe_failure_reopens():
    clock = FakeClock()
    gateway, transport, breaker = _circuit_gateway({ALBUM_ACTION: FAILED}, clock)
    for _ in range(3):
        _invoke_album(gateway)
    clock.advance(301)
    result = _invoke_album(gateway)  # 试探仍失败 → 立即 reopen、刷新冷却
    assert result.outcome == "rejected" and result.error_code == "1200"
    assert breaker.state(ALBUM_ACTION) == "open"
    result = _invoke_album(gateway)
    assert (result.outcome, result.gate) == ("rejected", "circuit_open")
    assert len(transport.calls) == 4
    clock.advance(301)  # 新冷却已过，可再试探
    transport.responses[ALBUM_ACTION] = OK
    assert _invoke_album(gateway).outcome == "ok"
    assert breaker.state(ALBUM_ACTION) == "closed"


def test_circuit_success_resets_consecutive_failures():
    clock = FakeClock()
    gateway, transport, breaker = _circuit_gateway({ALBUM_ACTION: FAILED}, clock)
    _invoke_album(gateway)
    _invoke_album(gateway)
    transport.responses[ALBUM_ACTION] = OK
    _invoke_album(gateway)  # 成功清零
    transport.responses[ALBUM_ACTION] = FAILED
    _invoke_album(gateway)
    _invoke_album(gateway)
    assert breaker.state(ALBUM_ACTION) == "closed"  # 2+2 不连续达阈值


def test_circuit_does_not_affect_non_f_domain():
    transport = FakeTransport(responses={"get_group_info": FAILED})
    gateway = ActionGateway(transport)  # 默认熔断器与默认管控面
    for _ in range(5):
        result = run(
            gateway.invoke(
                "get_group_info", {"group_id": GROUP},
                principal="system", rate_limit=1000,
            )
        )
        assert result.outcome == "rejected" and result.gate is None
    assert len(transport.calls) == 5  # B 域从不被 circuit_open 拦


# ------------------------------------------------------------ 落盘审计


def test_audit_line_for_file_domain_has_digest_and_size_no_raw_path(caplog):
    secret_path = "https://cdn.example.com/secret-dir/report.pdf?sign=TOPSECRET"
    transport = FakeTransport()
    gateway = ActionGateway(
        transport, policies={"upload_group_file": {"enabled": True}}
    )
    with caplog.at_level(logging.INFO, logger="companion.actions"):
        result = run(
            gateway.invoke(
                "upload_group_file",
                {"group_id": GROUP, "file": secret_path, "name": "report.pdf"},
                principal="system",
            )
        )
    assert result.outcome == "ok"
    line = [r.getMessage() for r in caplog.records if "upload_group_file" in r.getMessage()]
    assert len(line) == 1
    expected_digest = hashlib.sha256(secret_path.encode("utf-8")).hexdigest()[:16]
    assert f"resources={expected_digest}" in line[0]
    assert "size=none" in line[0]
    assert "outcome=ok" in line[0]  # 结果码在行内
    assert "secret-dir" not in line[0] and "TOPSECRET" not in line[0]
    assert secret_path not in caplog.text


def test_audit_line_declared_size_and_platform_rejection_code(caplog):
    transport = FakeTransport(responses={"upload_file_stream": FAILED})
    gateway = ActionGateway(
        transport, policies={"upload_file_stream": {"enabled": True}}
    )
    with caplog.at_level(logging.INFO, logger="companion.actions"):
        result = run(
            gateway.invoke(
                "upload_file_stream",
                {"stream_id": "s-1", "chunk_data": "QUJD", "file_size": 4096},
                principal="system",
            )
        )
    assert result.outcome == "rejected"
    line = [r.getMessage() for r in caplog.records if "upload_file_stream" in r.getMessage()]
    assert len(line) == 1
    chunk_digest = hashlib.sha256("QUJD".encode("utf-8")).hexdigest()[:16]
    assert f"resources={chunk_digest}" in line[0]
    assert "size=4096" in line[0]
    assert "outcome=rejected" in line[0]


def test_audit_line_for_non_file_domain_is_unchanged(caplog):
    gateway = ActionGateway(FakeTransport())
    with caplog.at_level(logging.INFO, logger="companion.actions"):
        result = run(
            gateway.invoke("get_group_info", {"group_id": GROUP}, principal="system")
        )
    assert result.outcome == "ok"
    line = [r.getMessage() for r in caplog.records if "get_group_info" in r.getMessage()]
    assert len(line) == 1
    assert "resources=" not in line[0]


# ------------------------------------------------------------ resolver 接线


def _resolver_gateway(tmp_path, resolver, *, transport=None):
    return ActionGateway(
        transport or FakeTransport(),
        policies={
            "upload_group_file": {"enabled": True},
            "upload_file_stream": {"enabled": True},
        },
        resource_roots=(tmp_path,),
        resource_resolver=resolver,
    )


def test_resolver_wiring_allows_compliant_local_file(tmp_path):
    target = tmp_path / "root_a" / "a.txt"
    target.parent.mkdir()
    target.write_bytes(b"hi")
    resolver = ResourceResolver(
        allowed_roots=(tmp_path / "root_a",), allowed_exts={"txt"}
    )
    gateway = _resolver_gateway(tmp_path, resolver)
    result = run(
        gateway.invoke(
            "upload_group_file",
            {"group_id": GROUP, "file": str(target), "name": "a.txt"},
            principal="system",
        )
    )
    assert result.outcome == "ok"
    assert len(gateway.transport.calls) == 1


def test_resolver_wiring_rejects_outside_root_and_bad_extension(tmp_path):
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    root_a.mkdir()
    root_b.mkdir()
    outside = root_b / "b.txt"
    outside.write_bytes(b"x")
    bad_ext = root_a / "pic.png"
    bad_ext.write_bytes(b"x")
    resolver = ResourceResolver(allowed_roots=(root_a,), allowed_exts={"txt"})
    gateway = _resolver_gateway(tmp_path, resolver)
    # root_b 在网关既有 resource_roots（tmp_path）内、过旧校验，
    # 只被 resolver 的更窄根目录拒绝 → 证明 resolver 参与。
    result = run(
        gateway.invoke(
            "upload_group_file",
            {"group_id": GROUP, "file": str(outside), "name": "b.txt"},
            principal="system",
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    result = run(
        gateway.invoke(
            "upload_group_file",
            {"group_id": GROUP, "file": str(bad_ext), "name": "pic.png"},
            principal="system",
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    assert gateway.transport.calls == []


def test_resolver_wiring_rejects_oversized_base64_chunk(tmp_path):
    resolver = ResourceResolver(max_bytes=8)
    gateway = _resolver_gateway(tmp_path, resolver)
    big_chunk = base64.b64encode(b"x" * 30).decode()  # 估算 30 字节 > 8
    result = run(
        gateway.invoke(
            "upload_file_stream",
            {"stream_id": "s-1", "chunk_data": big_chunk},
            principal="system",
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    small_chunk = base64.b64encode(b"xy").decode()
    result = run(
        gateway.invoke(
            "upload_file_stream",
            {"stream_id": "s-1", "chunk_data": small_chunk},
            principal="system",
        )
    )
    assert result.outcome == "ok"
    assert len(gateway.transport.calls) == 1


def test_resolver_absent_keeps_previous_behaviour(tmp_path):
    # 未注入 resolver：扩展名/根外口径只剩网关既有校验，png 放行。
    target = tmp_path / "pic.png"
    target.write_bytes(b"x")
    gateway = ActionGateway(
        FakeTransport(),
        policies={"upload_group_file": {"enabled": True}},
        resource_roots=(tmp_path,),
    )
    result = run(
        gateway.invoke(
            "upload_group_file",
            {"group_id": GROUP, "file": str(target), "name": "pic.png"},
            principal="system",
        )
    )
    assert result.outcome == "ok"


# ------------------------------------------------------------ 任务契约示范


class SlowFakeTransport(FakeTransport):
    """指定动作带固定延迟的 Fake（超时示范用，不出网）。"""

    def __init__(self, responses=None, *, slow_action=None, delay=0.3):
        super().__init__(responses)
        self.slow_action = slow_action
        self.delay = delay

    async def post_action(self, action, params=None):
        if action == self.slow_action:
            await asyncio.sleep(self.delay)
        return await super().post_action(action, params)


def test_file_task_run_wraps_facade_upload_success():
    fake = wired_fake()
    manager = FileTaskManager()

    async def main():
        task = manager.create("upload_group_file")
        result = await manager.run(
            task,
            lambda: fake.actions.files.upload_group_file(
                GROUP, "https://example.com/f.pdf", "f.pdf"
            ),
        )
        return task, result

    task, result = run(main())
    assert result.outcome == "ok"
    assert task.state == FileTaskState.SUCCEEDED
    assert task.result_code == "ok"
    assert [c["action"] for c in fake.transport.calls] == ["upload_group_file"]


def test_file_task_run_wraps_facade_download_timeout():
    transport = SlowFakeTransport(slow_action="download_file", delay=0.3)
    gateway = ActionGateway(transport, policies={"download_file": {"enabled": True}})
    from companion.adapters import make_actions

    actions = make_actions(gateway)
    manager = FileTaskManager()

    async def main():
        task = manager.create("download_file")
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await manager.run(
                task,
                lambda: actions.files.download_file(url="https://example.com/f.pdf"),
                timeout=0.05,
            )
        return task

    task = run(main())
    assert task.state == FileTaskState.TIMED_OUT
    assert task.result_code == "timeout"


def test_stream_upload_drives_facade_chunks_with_progress():
    fake = wired_fake()
    manager = FileTaskManager()
    progress_at_call = []

    async def main():
        task = manager.create("upload_file_stream")
        chunks = [(0, b"AAA"), (1, b"BBB"), (2, b"CCC")]

        async def send_chunk(index, data):
            progress_at_call.append(task.progress)
            result = await fake.actions.files.upload_file_stream(
                "s-1",
                chunk_data=base64.b64encode(data).decode(),
                chunk_index=index,
                total_chunks=3,
                is_complete=index == 2,
            )
            assert result.outcome == "ok"

        await manager.stream_upload(task, chunks, send_chunk)
        return task

    task = run(main())
    assert progress_at_call == [None, 1 / 3, 2 / 3]  # 每块成功后才推进
    assert task.progress == 1.0
    assert task.sent_chunks == 3
    calls = [c for c in fake.transport.calls if c["action"] == "upload_file_stream"]
    assert [c["params"]["chunk_index"] for c in calls] == [0, 1, 2]


def test_stream_upload_cancel_stops_facade_invokes():
    fake = wired_fake()
    manager = FileTaskManager()

    async def main():
        task = manager.create("upload_file_stream")
        chunks = [(i, b"x") for i in range(5)]

        async def send_chunk(index, data):
            result = await fake.actions.files.upload_file_stream(
                "s-1", chunk_data=base64.b64encode(data).decode(), chunk_index=index,
                total_chunks=5,
            )
            assert result.outcome == "ok"
            await asyncio.sleep(0.02)

        runner = asyncio.create_task(manager.stream_upload(task, chunks, send_chunk))
        while len(fake.transport.calls) < 2:
            await asyncio.sleep(0.005)
        assert manager.cancel(task.task_id) is True
        with pytest.raises(asyncio.CancelledError):
            await runner
        count_at_cancel = len(fake.transport.calls)
        await asyncio.sleep(0.05)
        # 取消后后续块零 invoke。
        assert len(fake.transport.calls) == count_at_cancel < 5
        return task

    task = run(main())
    assert task.state == FileTaskState.CANCELLED


# ------------------------------------------------------------ OCR 整合


def test_ocr_image_three_states_via_gateway():
    fake = FakeAdapter()
    result = run(fake.actions.media.ocr_image("https://example.com/a.png"))
    assert result.outcome == "ok"
    assert fake.transport.calls == [
        {"method": "post_action", "action": "ocr_image",
         "params": {"image": "https://example.com/a.png"}}
    ]
    fake = FakeAdapter(responses={"ocr_image": FAILED})
    result = run(fake.actions.media.ocr_image("https://example.com/a.png"))
    assert result.outcome == "rejected" and result.error_code == "1200"
    fake = FakeAdapter(
        responses={"ocr_image": TransportError("ocr_image", "TimeoutException")}
    )
    result = run(fake.actions.media.ocr_image("https://example.com/a.png"))
    assert result.outcome == "ambiguous"


def test_ocr_image_resource_boundary_participates(tmp_path):
    # 查询类动作，图片资源经网关资源边界（image 键）：根外绝对
    # 路径在派发前 resource_rejected；根内本地文件放行。
    target = tmp_path / "a.png"
    target.write_bytes(b"x")
    gateway = ActionGateway(FakeTransport(), resource_roots=(tmp_path,))
    from companion.adapters import make_actions

    actions = make_actions(gateway)
    result = run(actions.media.ocr_image("/outside/root/a.png"))
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    result = run(actions.media.ocr_image(str(target)))
    assert result.outcome == "ok"
    assert len(gateway.transport.calls) == 1


# ------------------------------------------------------------ receive-only 段


def _event(message):
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": 90001,
        "user_id": 10001,
        "message_id": 42,
        "message": message,
    }


ONLINEFILE_DATA = {
    "msgId": "42", "elementId": "el-1", "fileName": "f.pdf",
    "fileSize": "123", "isDir": False,
}


def test_receive_only_segments_parse_as_raw_with_valid_data():
    result = normalize_onebot_event(
        _event([{"type": "flashtransfer", "data": {"fileSetId": "fs-1"}}])
    )
    assert not isinstance(result, Ignore)
    assert result.segments == (RawSegment("flashtransfer", {"fileSetId": "fs-1"}),)
    assert result.has_media is True

    result = normalize_onebot_event(
        _event([{"type": "onlinefile", "data": dict(ONLINEFILE_DATA)}])
    )
    assert not isinstance(result, Ignore)
    assert result.segments == (RawSegment("onlinefile", dict(ONLINEFILE_DATA)),)


def test_receive_only_segments_missing_fields_are_malformed():
    for message in (
        [{"type": "flashtransfer", "data": {}}],
        [{"type": "flashtransfer", "data": {"fileSetId": ""}}],
        [{"type": "onlinefile", "data": {"msgId": "42"}}],
        [{"type": "onlinefile", "data": {**ONLINEFILE_DATA, "isDir": "yes"}}],
        [{"type": "onlinefile", "data": {**ONLINEFILE_DATA, "fileSize": 123}}],
    ):
        result = normalize_onebot_event(_event(message))
        assert isinstance(result, Ignore), message
        assert result.reason_code == "malformed"


def test_receive_only_segments_outbound_serialization_rejected():
    assert segments_to_onebot_message(
        [RawSegment("flashtransfer", {"fileSetId": "fs-1"})]
    ) is None
    assert segments_to_onebot_message(
        [RawSegment("onlinefile", dict(ONLINEFILE_DATA))]
    ) is None
    # 混合文本也整体拒绝（不静默丢段）。
    assert segments_to_onebot_message(
        [TextSegment("看这个"), RawSegment("flashtransfer", {"fileSetId": "fs-1"})]
    ) is None
    # node 内容含 receive-only 段同样整体拒绝。
    from companion.adapters.base import NodeSegment

    node = NodeSegment(
        {"user_id": 1}, (RawSegment("onlinefile", dict(ONLINEFILE_DATA)),)
    )
    assert segments_to_onebot_message([node]) is None


def test_receive_only_segment_types_registered():
    from companion.adapters.base import (
        KNOWN_RAW_SEGMENT_TYPES,
        RECEIVE_ONLY_SEGMENT_TYPES,
        SUPPORTED_SEGMENT_TYPES,
    )

    assert RECEIVE_ONLY_SEGMENT_TYPES == frozenset({"flashtransfer", "onlinefile"})
    assert RECEIVE_ONLY_SEGMENT_TYPES <= KNOWN_RAW_SEGMENT_TYPES
    assert RECEIVE_ONLY_SEGMENT_TYPES <= SUPPORTED_SEGMENT_TYPES
    # 正名：online_file（下划线）不是段类型，不在任何段集合内。
    assert "online_file" not in SUPPORTED_SEGMENT_TYPES


# ------------------------------------------------- 补审第二段修复


def test_file_facades_require_delivery_handle():
    """P3：message 类文件门面与 MessagingActions 同纪律（§3.1 1b）
    ——无句柄门面层直接拒绝、零派发（修复前三者可无句柄直发任意
    目标、绕过 DeliveryService 媒体门与额度记账）。"""
    fake = wired_fake()
    for call in (
        lambda: fake.actions.files.send_online_file(USER, "docs/f.pdf"),
        lambda: fake.actions.files.send_online_folder(USER, "docs/dir"),
        lambda: fake.actions.files.send_flash_msg("fs-1", group_id=GROUP),
    ):
        result = run(call())
        assert result.outcome == "rejected"
        assert result.gate == "delivery_handle_required"
    assert fake.transport.calls == []


def test_relative_path_traversal_rejected_at_gateway(tmp_path):
    """P2：相对路径穿越在网关步骤 d 被拒——注入专用 roots/base 的
    装配下 ../../data/memory.db 与 ../secret.txt 均零派发；base 内
    的相对路径照常放行；未注入 roots/base 时相对路径形态一律
    拒绝（fail closed，与绝对路径的既有口径对称）。"""
    fake = wired_fake()
    gateway = fake.action_gateway
    gateway.resource_roots = (str(tmp_path),)
    gateway.resource_base_dir = str(tmp_path)
    result = run(
        fake.actions.files.send_online_file(
            USER, "../../data/memory.db", delivery_token="h-1"
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    result = run(
        gateway.invoke(
            "upload_group_file",
            {"group_id": GROUP, "file": "../secret.txt", "name": "x"},
            principal="llm_tool",
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    assert fake.transport.calls == []
    # base 内的相对路径放行（文件不必存在，网关只判归属不判存在）。
    result = run(
        fake.actions.files.send_online_file(USER, "docs/f.pdf", delivery_token="h-2")
    )
    assert result.outcome == "ok"

    bare = wired_fake()
    bare.action_gateway.resource_roots = ()
    bare.action_gateway.resource_base_dir = None
    result = run(
        bare.actions.files.send_online_file(USER, "docs/f.pdf", delivery_token="h-3")
    )
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    assert bare.transport.calls == []


def test_download_file_ssrf_and_on_disk_name_rejected():
    """P3：download_file 的元数据/内网 URL 与穿越落盘名在网关被
    拒、零派发；公网 URL + basename 落盘名照常放行；
    upload_file_stream 的 filename 同口径。"""
    fake = wired_fake()
    result = run(
        fake.actions.files.download_file(
            url="http://169.254.169.254/latest/meta-data/", name="x"
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    result = run(
        fake.actions.files.download_file(
            url="https://example.com/f.pdf", name="../../tmp/x"
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    result = run(fake.actions.files.upload_file_stream("s-1", filename="../evil.bin"))
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    # 畸形 URL（复审第 3 轮 P3）：校验器的裸 ValueError 不许穿透
    # 网关 invoke——须收敛为 resource_rejected、零派发。
    result = run(
        fake.actions.files.download_file(url="http://[::1/x", name="x")
    )
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    assert fake.transport.calls == []
    result = run(
        fake.actions.files.download_file(url="https://example.com/f.pdf", name="f.pdf")
    )
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def test_flash_msg_requires_exactly_one_target():
    """复审 P3：send_flash_msg 目标恰一（与 MessagingActions
    .send_msg 同纪律）——双给/双缺在门面层拒 invalid_params、
    零派发。修复前门面按群优先派生 identity、网关额度域按
    「群目标须 user_id 缺席」派生，双给时合法群句柄被按用户域
    验票误拒 quota_unavailable；恰一约束使两层派生恒一致。"""
    fake = wired_fake()
    result = run(
        fake.actions.files.send_flash_msg(
            "fs-1", user_id=USER, group_id=GROUP, delivery_token="h-both"
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "invalid_params")
    result = run(
        fake.actions.files.send_flash_msg("fs-1", delivery_token="h-none")
    )
    assert (result.outcome, result.gate) == ("rejected", "invalid_params")
    assert fake.transport.calls == []
    # 单目标照常（与 CASES 的群目标用例互补，此处钉用户目标）。
    result = run(
        fake.actions.files.send_flash_msg(
            "fs-1", user_id=USER, delivery_token="h-user"
        )
    )
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1
