"""补审第三段（N0–N3）修复线：对 fa58d0e 销账后仍成立条目的钉子。

覆盖：
- P2 网关内容审查载荷形态统一：dict 形、CQ json/xml/share 串、
  send_group_ai_record 的 text 键与 list 形同口径受审；
- P3 RequestStore boot 身份：同进程第二实例不扫活行、新进程才扫；
- P3 段规划器尾随悬空前缀不再单独成单元；
- P3 critical 由网关派生：自报 critical 降格（受审/限流/普通池），
  告警通道（system + alert_private_msg）仍走 critical；
- P3 actions.overrides 生产装配合并断言；
- P3 事件覆盖矩阵（event_coverage.json 32 行）与分类器机械对账；
- P4 set_friend_add_request 确认门网关级直接断言（自有键 +
  friend_management 键 + confirmed=True 派发）；
- P2-1（已由 N4 处置链关闭）真网关证明形态补强：本地门拒绝 /
  待确认经真 ActionGateway + GatewayRequestChannel 驱动
  handle_request，只落 ambiguous + 细分码、绝不误记平台终态。
- 复审 r7（a–f 原理收口）：CQ 剥离/解析单一文法、CQ 卡片前缀
  参数并集、畸形 XML 回落剥标签、字段形态等价类（列表值字段 /
  非 dict data / 回退键集审计补漏）、资源键上下文穿透嵌套、
  request_generation 损坏按数据推导下限、critical 派生
  policy_key 合取项反例。
- 复审 r8（顺序/连续性收口）：提取按渲染顺序产出（XML 字符
  数据流 + 文档序交错流、str 载荷余文与 CQ 段按原始位置交
  错、node 昵称在内容之前）、网关拼接整串送审兜底、未知类
  型回落递归嵌套容器、共享子结构资源复验结果级钉、代次下
  限 gen_boot 来源隔离钉、share/onlinefile 良性孪生补齐。
- 集成复审修复轮：CQ 同码重复资源参数逐个提取（同码 file=
  双序/变体钉）；video 段 thumb、music 段 audio 与 CQ 形态
  url/image 参数的段字段审计补漏双形钉；合并强校验相对形
  态在归一键/嵌套 dict/≥2 跳 list/120 层深/共享子结构五类
  新上下文的函数级与 invoke 级钉（相对形态区分强弱校验）。
- 全线复审微修轮（基线 af5f83f）：typed 段可见键提取矩阵
  （typed 类型全集 × 可见键全集 × dict/CQ 双形参数化钉，
  text/markdown/node/forward 四分支同级可见键并集送审）；
  JSON 卡片重复键保对解析（网关 + services 投递层双路，
  首值/末值/嵌套藏词必拦、双良性恰一次派发）；requests
  死代码 _meta_corrupt 删除（零调用核实，行为由既有钉覆盖）。
- 收口轮（基线 3c3e8fd）：提取层迁至共享模块
  companion.content_extract（网关与 services 同一实现，
  services 自维护 8 键集/递归收集整体删除）；node/forward
  剩余键提取回到渲染位（字段在 content 之前）+
  review_bubbles 安全模式双序送审（逆序拼接变体并集判定，
  r8 旧钉 node_reverse_split 由派发翻转为拦截）；services
  路径同矩阵钉（段对象形态 × 可见键全集）；深层 JSON 在
  services 路径 fail-closed（打桩 RecursionError 必转
  unreviewable 拦截、自然深层藏词必拦且异常不穿透）；
  ≥3 重复键全位置钉与大小写变体/容器型剩余键钉。
- 纠正轮（基线 d9deb91）：双序送审撤销（法官复现良性顺
  序回复被逆序拼接误拦，父级裁决回到「只按渲染序审一
  次、提取对齐渲染序」；拆分钉全改按渲染序判定、r8 钉
  恢复派发，法官良性对成必过钉）；services FileSegment
  委托共享提取（矩阵补 file 类）；NodeSegment content
  变体键恒扫（大小写/填充变体矩阵）；services fail-
  closed 双生产 caller 装配钉（DeliveryService /
  OneBotAdapter.send_private）；深层良性断言改原因感知；
  services ForwardSegment 拆分钉补齐。
- 序列化序轮（基线 bb550ce）：提取顺序逐字等于序列化
  顺序成唯一口径——content_extract 五个 typed 分支改单
  遍插入序扫描（废除 node 五键规范序与本分字段置前），
  services NodeSegment/FileSegment 适配改单遍序列化序
  （废除 name 强制置前、变体键回原位）；字段序排列抽测
  （网关 node + services node/file 全排列、判定以序列化
  序为准）；typed 对齐钉（提取流与序列化字段流逐字对
  照）；exact-'content' 例外双向钉与变体键拆分对钉；
  content_extract 双序旧注释清除。
- 语义角色精确键轮（serde 序复审 P2/P3/P4）：语义角色只
  认序列化精确键、归一只许扩覆盖——FileSegment has_name
  改精确 'name'（变体键不再顶替 basename 审查），变体矩
  阵钉 + 精确 name 零变化钉；text/json/xml/markdown 兄弟
  键位置排列钉 + text 主键非首位拆分钉；node content 键
  入排列集钉。语义键复审 P3 补钉：变体 'file' 键 × name
  组合矩阵（归一分发的唯一审查路径钉死，tests-only）。

全部标识均为合成值，不出网、不连真实平台。
"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from companion.adapters.base import (
    AtSegment,
    FileSegment,
    ForwardSegment,
    NodeSegment,
    RawSegment,
    ReplySegment,
    TextSegment,
)
from companion.adapters.fake import FakeAdapter, FakeTransport
from companion.adapters.gateway import (
    PRINCIPAL_SYSTEM,
    ActionGateway,
    AuthorizationContext,
)
from companion.adapters.onebot import classify_onebot_event
from companion.domain.requests import request_key
from companion.ingress.events import (
    GatewayRequestChannel,
    RequestPolicy,
    handle_request,
)
from companion.memory import Database
from companion.memory.requests import SqliteRequestStore
from companion.multipart import review_bubbles
from companion.pipeline.observers import StageMetrics
from companion.quota import SendQuota
from companion.services.segments import (
    plan_segment_sends,
    review_outbound_segments,
    visible_texts,
)
from companion.tools import HttpOneBotClient

pytestmark = pytest.mark.timeout(10)

BANNED = "我不是AI，我是真人"
GROUP = "80001"
USER = "90001"
SELF_ID = "900000001"
AUTH_ACCOUNT = AuthorizationContext(authorizer="owner-10001", account=True)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# P2：内容审查载荷形态统一
# ---------------------------------------------------------------------------


def _gateway_fake():
    return FakeAdapter(
        policies={
            "send_group_msg": {"enabled": True, "dry_run": False},
            "send_group_ai_record": {"enabled": True, "dry_run": False},
        }
    )


def _invoke_group(fake, message, **kwargs):
    return run(
        fake.action_gateway.invoke(
            "send_group_msg",
            {"group_id": GROUP, "message": message},
            principal=PRINCIPAL_SYSTEM,
            identity=f"g:{GROUP}",
            **kwargs,
        )
    )


def test_content_review_list_form_blocked_control():
    fake = _gateway_fake()
    result = _invoke_group(fake, [{"type": "text", "data": {"text": BANNED}}])
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_dict_form_blocked():
    fake = _gateway_fake()
    result = _invoke_group(fake, {"type": "text", "data": {"text": BANNED}})
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_cq_json_card_blocked():
    fake = _gateway_fake()
    payload = '[CQ:json,data={"desc":"%s"}]' % BANNED
    result = _invoke_group(fake, payload)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_cq_xml_card_blocked():
    fake = _gateway_fake()
    payload = (
        '[CQ:xml,data=<msg serviceID="1"><item layout="1">'
        "<title>%s</title></item></msg>]"
    ) % BANNED
    result = _invoke_group(fake, payload)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_cq_share_card_blocked():
    fake = _gateway_fake()
    payload = "[CQ:share,url=https://example.com,title=%s,content=hi]" % BANNED
    result = _invoke_group(fake, payload)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_ai_record_text_blocked():
    fake = _gateway_fake()
    result = run(
        fake.action_gateway.invoke(
            "send_group_ai_record",
            {"group_id": GROUP, "character": "1", "text": BANNED},
            principal=PRINCIPAL_SYSTEM,
            identity=f"g:{GROUP}",
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_benign_forms_still_dispatch():
    fake = _gateway_fake()
    result = _invoke_group(fake, {"type": "text", "data": {"text": "大家好，今天开会"}})
    assert result.outcome == "ok"
    result = _invoke_group(fake, "[CQ:image,file=x.jpg]大家好")
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 2


# ---------------------------------------------------------------------------
# P3：critical 由网关派生，自报降格
# ---------------------------------------------------------------------------


def test_self_declared_critical_is_reviewed_and_rate_limited():
    transport = FakeTransport()
    gateway = ActionGateway(
        transport,
        policies={
            "send_group_msg": {"enabled": True, "dry_run": False, "rate_limit": 1},
        },
        quota=SendQuota(),
    )

    def send(message):
        return run(
            gateway.invoke(
                "send_group_msg",
                {"group_id": GROUP, "message": message},
                principal=PRINCIPAL_SYSTEM,
                identity=f"g:{GROUP}",
                priority="critical",
            )
        )

    # 自报 critical 也过内容复核（降格 normal）。
    result = send(BANNED)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    # 自报 critical 也受限流：limit=1 时第二次同身份调用被限。
    first = send("大家好")
    assert first.outcome == "ok"
    second = send("大家好")
    assert (second.outcome, second.gate) == ("rejected", "rate_limited")
    # 自报 critical 不花危机预留池：critical 余量仍满、普通池已扣。
    snapshot = gateway.quota.snapshot()
    assert snapshot["critical_remaining"] == 10
    assert snapshot["normal_remaining"] == 59


def test_alert_channel_keeps_critical_pool():
    transport = FakeTransport()
    gateway = ActionGateway(
        transport,
        policies={
            "send_private_msg": {"enabled": True, "dry_run": False},
            "alert_private_msg": {"enabled": True, "dry_run": False},
        },
        quota=SendQuota(),
    )
    client = HttpOneBotClient("http://127.0.0.1:3000", gateway=gateway)
    result = run(client.send_alert_private_msg(USER, "固定告警话术"))
    assert result["status"] == "ok"
    assert len(transport.calls) == 1
    # 告警通道走 critical 预留池：critical -1、普通池不动。
    snapshot = gateway.quota.snapshot()
    assert snapshot["critical_remaining"] == 9
    assert snapshot["normal_remaining"] == 60


# ---------------------------------------------------------------------------
# P3：RequestStore boot 身份
# ---------------------------------------------------------------------------


def test_second_store_same_process_does_not_sweep_live_rows(tmp_path):
    db = Database(tmp_path / "memory.db")
    store_a = SqliteRequestStore(db)
    key = ("1000", "friend", "", "flag-live")
    store_a.try_begin(key)
    assert store_a.claim_processing(key) is True

    # 同进程第二实例：复用代次、不清扫，活实例仍可正常收口。
    store_b = SqliteRequestStore(db)
    assert store_b.generation == store_a.generation
    assert store_b.get(key).status == "processing"
    assert store_a.complete(key, status="done", decision="approved") is True
    assert store_b.get(key).status == "done"


def test_claim_processing_stamps_current_generation(tmp_path, monkeypatch):
    db = Database(tmp_path / "memory.db")
    store_a = SqliteRequestStore(db)
    key = ("1000", "friend", "", "flag-old-gen")
    store_a.try_begin(key)
    assert store_a.get(key).generation == store_a.generation

    # 新进程（换 boot 身份）：代次 +1；pending 行保留，占用时打新代次。
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-2")
    store_b = SqliteRequestStore(db)
    assert store_b.generation == store_a.generation + 1
    assert store_b.claim_processing(key) is True
    assert store_b.get(key).generation == store_b.generation


# ---------------------------------------------------------------------------
# P3：段规划器尾随悬空前缀
# ---------------------------------------------------------------------------


def test_plan_segment_sends_drops_trailing_prefix():
    units = plan_segment_sends(
        [TextSegment("hi"), ReplySegment({"id": "42"})], split_text=True
    )
    assert units == [(TextSegment("hi"),)]


def test_plan_segment_sends_prefix_only_yields_no_units():
    assert plan_segment_sends([ReplySegment({"id": "42"})], split_text=True) == []
    assert plan_segment_sends([AtSegment({"qq": "10001"})], split_text=False) == []


def test_plan_segment_sends_prefix_still_binds_to_following_text():
    reply = ReplySegment({"id": "42"})
    units = plan_segment_sends([reply, TextSegment("hi")], split_text=True)
    assert units == [(reply, TextSegment("hi"))]


# ---------------------------------------------------------------------------
# P3：actions.overrides 生产装配合并
# ---------------------------------------------------------------------------


def _settings_with_overrides(tmp_path, monkeypatch, overrides):
    from companion.config import load_settings
    from companion.ingress import EventProcessor

    monkeypatch.setenv("ONEBOT_SECRET", "seg3-assembly-secret")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ALERT_QQ", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("CONFIG_PATH", str(root / "config.yaml"))
    (root / "config.yaml").write_text(
        yaml.safe_dump({"allowed_private_user_ids": ["10001"]}), encoding="utf-8"
    )
    base = load_settings()
    settings = replace(
        base,
        onebot_secret="seg3-assembly" + "-secret",
        dev=False,
        root=root,
        app={
            **base.app,
            "allowed_private_user_ids": ["10001"],
            "sqlite_path": "memory.db",
            "logs_dir": "logs",
            "response_delay_seconds": {"min": 0, "max": 0},
            "actions": {"overrides": overrides},
        },
    )
    return EventProcessor(settings)


def test_actions_overrides_merge_into_gateway_policies(tmp_path, monkeypatch):
    processor = _settings_with_overrides(
        tmp_path,
        monkeypatch,
        {
            # 开一个基线里没有的动作策略。
            "get_group_list": {"enabled": True},
            # 给已有空策略补 dry_run。
            "send_group_msg": {"dry_run": True},
            # None 字段不许覆盖基线值，只合并非空字段。
            "get_image": {"enabled": None, "dry_run": True},
            # 非 dict 条目跳过。
            "get_status": "not-a-dict",
        },
    )
    policies = processor.sender.action_gateway.policies
    assert policies["get_group_list"] == {"enabled": True}
    assert policies["send_group_msg"]["dry_run"] is True
    get_image = policies["get_image"]
    assert get_image["enabled"] is True
    assert get_image["rate_limit"] == 60
    assert get_image["dry_run"] is True
    assert "get_status" not in policies


# ---------------------------------------------------------------------------
# P3：事件覆盖矩阵与分类器机械对账
# ---------------------------------------------------------------------------

_COVERAGE = (
    Path(__file__).parent / "fixtures" / "napcat" / "event_coverage.json"
)


def _coverage_cases():
    rows = json.loads(_COVERAGE.read_text(encoding="utf-8"))["rows"]
    cases = []
    for index, row in enumerate(rows):
        sub_field = row["sub_type"]
        options = sub_field.split("/") if "/" in sub_field else [sub_field or None]
        for option in options:
            cases.append(
                pytest.param(row, option, id=f"row{index}-{row['post_type']}-{option}")
            )
    return cases


def _payload_for(row, sub_type):
    base = {"time": 1760000000, "self_id": 1000}
    post = row["post_type"]
    if post == "message":
        payload = {
            **base,
            "post_type": "message",
            "message_type": row["type"],
            "sub_type": sub_type,
            "message_id": 42,
            "user_id": 2000,
            "message": [{"type": "text", "data": {"text": "hi"}}],
            "raw_message": "hi",
        }
        if row["type"] == "group":
            payload["group_id"] = 3000
        return payload
    if post == "message_sent":
        return {
            **base,
            "post_type": "message_sent",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": 42,
            "user_id": 2000,
            "message": [{"type": "text", "data": {"text": "hi"}}],
            "raw_message": "hi",
        }
    if post == "notice":
        payload = {
            **base,
            "post_type": "notice",
            "notice_type": row["type"],
            "user_id": 2000,
            "group_id": 3000,
        }
        if sub_type:
            payload["sub_type"] = sub_type
        return payload
    if post == "request":
        payload = {
            **base,
            "post_type": "request",
            "request_type": row["type"],
            "user_id": 2000,
            "flag": "flag-matrix",
            "comment": "",
        }
        if sub_type:
            payload["sub_type"] = sub_type
        if row["type"] == "group":
            payload["group_id"] = 3000
        return payload
    if post == "meta_event":
        payload = {
            **base,
            "post_type": "meta_event",
            "meta_event_type": row["type"],
        }
        if row["type"] == "heartbeat":
            payload["status"] = {"online": True, "good": True}
            payload["interval"] = 5000
        if row["type"] == "lifecycle":
            payload["sub_type"] = "connect"
        return payload
    raise AssertionError(f"未知 post_type: {post}")


@pytest.mark.parametrize("row,sub_type", _coverage_cases())
def test_event_coverage_matrix_matches_classifier(row, sub_type):
    from companion.adapters.base import (
        Ignore,
        InboundEvent,
        MetaEvent,
        NoticeEvent,
        RequestEvent,
    )

    result = classify_onebot_event(_payload_for(row, sub_type))
    assert not isinstance(result, Ignore), (
        f"矩阵行被分类器忽略：{row} sub_type={sub_type} -> {result.reason}"
    )
    disposition = row["disposition"]
    if disposition in ("chain_private", "chain_group"):
        assert isinstance(result, InboundEvent)
    elif disposition == "chain_event":
        if row["post_type"] == "request":
            assert isinstance(result, RequestEvent)
            assert result.request_type == row["type"]
        else:  # notify/poke：唯一进事件子链的 notice
            assert isinstance(result, NoticeEvent)
            assert (result.notice_type, result.sub_type) == ("notify", "poke")
    else:  # log_only
        if row["post_type"] == "notice":
            assert isinstance(result, NoticeEvent)
            assert result.notice_type == row["type"]
            if sub_type:
                assert result.sub_type == sub_type
        elif row["post_type"] == "meta_event":
            assert isinstance(result, MetaEvent)
            assert result.meta_event_type == row["type"]
        elif row["post_type"] == "message_sent":
            assert isinstance(result, MetaEvent)
            assert result.direction == "self_echo"
        else:  # 群 anonymous / notice 子类型消息：观察口径入站事件
            assert isinstance(result, InboundEvent)
            assert result.chat_type == "group"
            assert result.sub_type == sub_type


# ---------------------------------------------------------------------------
# P4：set_friend_add_request 确认门网关级断言
# ---------------------------------------------------------------------------


def _friend_gateway(policies):
    return FakeAdapter(policies=policies)


def test_friend_add_request_pending_confirmation_own_policy_key():
    fake = _friend_gateway(
        {"set_friend_add_request": {"enabled": True, "dry_run": False}}
    )
    result = run(
        fake.action_gateway.invoke(
            "set_friend_add_request",
            {"flag": "flag-p4", "approve": True},
            principal=PRINCIPAL_SYSTEM,
            authorization=AUTH_ACCOUNT,
            confirmed=False,
        )
    )
    assert (result.outcome, result.gate) == ("simulated", "pending_confirmation")
    assert fake.transport.calls == []


def test_friend_add_request_pending_confirmation_friend_management_key():
    # 封版集成裁决（与补审第二段 c2b 授权下限对齐）：legacy 路径
    # 的授权下限与 canonical 路径一致，确认门须在授权上下文具备
    # 时到达——生产接线由 manage_friend 垫片注入上下文，此处同形。
    fake = _friend_gateway(
        {"friend_management": {"enabled": True, "dry_run": False}}
    )
    result = run(
        fake.action_gateway.invoke(
            "set_friend_add_request",
            {"flag": "flag-p4", "approve": True},
            principal=PRINCIPAL_SYSTEM,
            policy_key="friend_management",
            authorization=AUTH_ACCOUNT,
            confirmed=False,
        )
    )
    assert (result.outcome, result.gate) == ("simulated", "pending_confirmation")
    assert fake.transport.calls == []


def test_friend_add_request_legacy_path_requires_authorization():
    # c2b 授权下限（补审第二段 P3，封版集成后在合并树上补钉）：
    # legacy 路径缺授权上下文在参数校验之前即 authorization_missing，
    # 不许由调用方仅凭自选 policy_key + 自断 confirmed 到达确认门。
    fake = _friend_gateway(
        {"friend_management": {"enabled": True, "dry_run": False}}
    )
    result = run(
        fake.action_gateway.invoke(
            "set_friend_add_request",
            {"flag": "flag-p4", "approve": True},
            principal=PRINCIPAL_SYSTEM,
            policy_key="friend_management",
            confirmed=False,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "authorization_missing")
    assert fake.transport.calls == []


def test_friend_add_request_confirmed_dispatches():
    fake = _friend_gateway(
        {"set_friend_add_request": {"enabled": True, "dry_run": False}}
    )
    result = run(
        fake.action_gateway.invoke(
            "set_friend_add_request",
            {"flag": "flag-p4", "approve": True},
            principal=PRINCIPAL_SYSTEM,
            authorization=AUTH_ACCOUNT,
            confirmed=True,
        )
    )
    assert result.outcome == "ok"
    assert [call["action"] for call in fake.transport.calls] == [
        "set_friend_add_request"
    ]


# ---------------------------------------------------------------------------
# P2-1（N4 已关闭）真网关处置通道证明形态
# ---------------------------------------------------------------------------


class _Proc:
    def __init__(self):
        self.metrics = StageMetrics()
        self.logs = []

    def _log(self, event_type, length, allowed, latency_ms, **kwargs):
        self.logs.append({"event_type": event_type, **kwargs})


def _friend_request_event(flag):
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


def _drive_handle_request(tmp_path, name, fake, *, confirmed):
    store = SqliteRequestStore(Database(tmp_path / f"{name}.db"))
    event = _friend_request_event(f"flag-{name}")
    key = request_key(event.self_id, event.request_type, event.sub_type, event.flag)
    store.try_begin(key, user_id=event.user_id, group_id=event.group_id)
    channel = GatewayRequestChannel(
        fake.action_gateway,
        principal=PRINCIPAL_SYSTEM,
        confirmed=confirmed,
        authorization=AUTH_ACCOUNT,
    )
    run(
        handle_request(
            _Proc(),
            event,
            store=store,
            channel=channel,
            policy=RequestPolicy(
                mode="manual", friend_mode="approve_all", group_mode="manual"
            ),
        )
    )
    return store.get(key)


def test_handle_request_real_gateway_disabled_gate_not_terminal(tmp_path):
    # 策略未开启 → 网关本地 disabled 门：不终局化为平台结果。
    fake = FakeAdapter(policies={})
    record = _drive_handle_request(tmp_path, "disabled", fake, confirmed=True)
    assert record.status == "ambiguous"
    assert record.result_code == "gateway_gate:disabled"
    assert fake.transport.calls == []


def test_handle_request_real_gateway_pending_confirmation_not_terminal(tmp_path):
    # 策略开启但通道未确认 → simulated/pending_confirmation：同样不终局化。
    fake = FakeAdapter(
        policies={"set_friend_add_request": {"enabled": True, "dry_run": False}}
    )
    record = _drive_handle_request(tmp_path, "pending", fake, confirmed=False)
    assert record.status == "ambiguous"
    assert record.result_code == "gateway_simulated:pending_confirmation"
    assert fake.transport.calls == []


# ---------------------------------------------------------------------------
# 复审轮 1：CQ 多字段/大小写/裸字符串形态、critical 另外两角、
# 双进程 boot 心跳、complete 失败留痕
# ---------------------------------------------------------------------------

from companion.adapters.gateway import PRINCIPAL_LLM_TOOL  # noqa: E402


def test_content_review_cq_json_multifield_blocked():
    # data 值内未转义逗号是内容的一部分：中段/末段字段同样送审。
    fake = _gateway_fake()
    payload = '[CQ:json,data={"app":"com.example","desc":"hello","title":"%s"}]' % BANNED
    result = _invoke_group(fake, payload)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_cq_json_escaped_commas_blocked():
    # 规范转义形（&#44;）：解转义后完整 JSON 送审。
    fake = _gateway_fake()
    payload = '[CQ:json,data={"app":"com.example"&#44;"desc":"%s"}]' % BANNED
    result = _invoke_group(fake, payload)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_cq_xml_comma_attribute_blocked():
    fake = _gateway_fake()
    payload = (
        '[CQ:xml,data=<msg serviceID="1" brief="a,b">'
        "<item><title>%s</title></item></msg>]"
    ) % BANNED
    result = _invoke_group(fake, payload)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_cq_json_multifield_benign_passes():
    fake = _gateway_fake()
    payload = '[CQ:json,data={"app":"com.example","desc":"hello, world","title":"hi"}]'
    result = _invoke_group(fake, payload)
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def test_content_review_case_variant_types_blocked():
    fake = _gateway_fake()
    result = _invoke_group(
        fake, {"type": "JSON", "data": {"data": '{"desc":"%s"}' % BANNED}}
    )
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    result = _invoke_group(fake, '[CQ:JSON,data={"desc":"%s"}]' % BANNED)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_bare_string_list_elements_blocked():
    fake = _gateway_fake()
    result = _invoke_group(fake, [BANNED])
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    result = _invoke_group(
        fake, [{"type": "image", "data": {"file": "x.jpg"}}, BANNED]
    )
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def _private_gateway():
    transport = FakeTransport()
    gateway = ActionGateway(
        transport,
        policies={
            "send_private_msg": {"enabled": True, "dry_run": False, "rate_limit": 1},
        },
        quota=SendQuota(),
    )
    return transport, gateway


def _invoke_private(gateway, principal):
    return run(
        gateway.invoke(
            "send_private_msg",
            {"user_id": USER, "message": "固定话术"},
            principal=principal,
            identity=USER,
            priority="critical",
        )
    )


def test_critical_canonical_private_send_uses_critical_pool():
    # 派生矩阵正例（canonical）：system + send_private_msg +
    # critical → 走危机池、跳限流（rate_limit=1 连发两条都过）。
    transport, gateway = _private_gateway()
    assert _invoke_private(gateway, PRINCIPAL_SYSTEM).outcome == "ok"
    assert _invoke_private(gateway, PRINCIPAL_SYSTEM).outcome == "ok"
    assert len(transport.calls) == 2
    snapshot = gateway.quota.snapshot()
    assert snapshot["critical_remaining"] == 8
    assert snapshot["normal_remaining"] == 60


def test_critical_llm_tool_principal_downgraded():
    # 派生矩阵负例：llm_tool 主体自报 critical → 降格 normal：
    # 受限流（limit=1 第二条被限）、只花普通池。
    transport, gateway = _private_gateway()
    assert _invoke_private(gateway, PRINCIPAL_LLM_TOOL).outcome == "ok"
    second = _invoke_private(gateway, PRINCIPAL_LLM_TOOL)
    assert (second.outcome, second.gate) == ("rejected", "rate_limited")
    assert len(transport.calls) == 1
    snapshot = gateway.quota.snapshot()
    assert snapshot["critical_remaining"] == 10
    assert snapshot["normal_remaining"] == 59


def test_concurrent_boot_processes_do_not_sweep_each_other(tmp_path, monkeypatch):
    # 两个进程共用一库（部署重叠/误起双实例）：后到进程不清扫
    # 心跳新鲜的在飞处置；对方失联（心跳过期）后才接管清扫。
    db = Database(tmp_path / "memory.db")
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-a")
    store_a = SqliteRequestStore(db)
    key1 = ("1000", "friend", "", "flag-peer-1")
    key2 = ("1000", "friend", "", "flag-peer-2")
    store_a.try_begin(key1)
    store_a.try_begin(key2)
    assert store_a.claim_processing(key1) is True
    assert store_a.claim_processing(key2) is True

    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-b")
    store_b = SqliteRequestStore(db)
    assert store_b.generation == store_a.generation + 1
    assert store_b.get(key1).status == "processing"
    assert store_b.get(key2).status == "processing"
    # 在飞处置仍可由原实例正常收口。模拟纪律（复审 P4）：每次
    # 调用前把 boot 身份切回动作所属进程——真实部署里 store_a
    # 的写操作发生在进程 A、刷的是 boot-a 的心跳。
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-a")
    assert store_a.complete(key1, status="done", decision="approved") is True

    # boot-a 失联（阈值归零模拟心跳过期）后，第三个进程才清扫。
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-c")
    monkeypatch.setattr("companion.memory.requests._BOOT_STALE_SECONDS", 0.0)
    store_c = SqliteRequestStore(db)
    assert store_c.get(key2).status == "ambiguous"
    assert store_c.get(key1).status == "done"


def test_handle_request_complete_failure_logs_and_continues(tmp_path, caplog):
    import logging

    real = SqliteRequestStore(Database(tmp_path / "false-complete.db"))

    class _FalseComplete:
        def __init__(self, inner):
            self._inner = inner

        def claim_processing(self, key):
            return self._inner.claim_processing(key)

        def complete(self, key, **kwargs):
            return False

        def __getattr__(self, name):
            return getattr(self._inner, name)

    event = _friend_request_event("flag-false-complete")
    key = request_key(event.self_id, event.request_type, event.sub_type, event.flag)
    real.try_begin(key, user_id=event.user_id, group_id=event.group_id)
    with caplog.at_level(logging.WARNING, logger="companion.ingress.events"):
        run(
            handle_request(
                _Proc(),
                event,
                store=_FalseComplete(real),
                channel=None,
                policy=RequestPolicy(
                    mode="manual", friend_mode="approve_all", group_mode="manual"
                ),
            )
        )
    assert "request complete not applied" in caplog.text
    # 收口未生效如实留痕：记录仍停在 processing，不抛错、不冒充终态。
    assert real.get(key).status == "processing"


# ---------------------------------------------------------------------------
# 复审轮 2：快速重启惰性恢复、老库迁移分支、键名大小写与形态补测
# ---------------------------------------------------------------------------


def test_crash_fast_restart_recovers_lazily_after_heartbeat_stale(
    tmp_path, monkeypatch
):
    # 真实阈值（120s）+ 注入时钟：claim 后立即崩溃的 boot 心跳
    # 尚新鲜，秒级重启的后继构造期不清扫（与活对端不可区分）；
    # 心跳过期后，后继无需重建 store，下一次读写惰性接管清扫。
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731
    db = Database(tmp_path / "memory.db")
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-a")
    store_a = SqliteRequestStore(db, clock=clock)
    key = ("1000", "friend", "", "flag-fast-crash")
    store_a.try_begin(key)
    assert store_a.claim_processing(key) is True

    now[0] = 1000.0 + 119.0
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-b")
    store_b = SqliteRequestStore(db, clock=clock)
    assert store_b.get(key).status == "processing"

    now[0] = 1000.0 + 121.0
    store_b.list_pending()
    assert store_b.get(key).status == "ambiguous"


def test_legacy_db_unknown_owner_generations_are_swept(tmp_path, monkeypatch):
    # 老库形态（本机制上线前：只有 request_generation 与处置
    # 表，无 boot 归属/心跳键族）：归属未知按失联论——
    # processing 转 ambiguous、pending 保留、代次 +1。
    db = Database(tmp_path / "memory.db")
    store = SqliteRequestStore(db)
    key_processing = ("1000", "friend", "", "flag-legacy-processing")
    key_pending = ("1000", "friend", "", "flag-legacy-pending")
    store.try_begin(key_processing)
    store.try_begin(key_pending)
    assert store.claim_processing(key_processing) is True
    with db.transaction() as conn:
        meta_keys = [row[0] for row in conn.execute("SELECT key FROM schema_meta")]
        for meta_key in meta_keys:
            if meta_key.startswith(
                ("request_gen_boot:", "request_boot_seen:", "request_boot_id")
            ):
                conn.execute("DELETE FROM schema_meta WHERE key=?", (meta_key,))
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-next")
    store2 = SqliteRequestStore(db)
    assert store2.generation == store.generation + 1
    assert store2.get(key_processing).status == "ambiguous"
    assert store2.get(key_pending).status == "pending"


def test_content_review_typeless_dict_payloads_blocked():
    fake = _gateway_fake()
    result = _invoke_group(fake, {"text": BANNED})
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    result = _invoke_group(fake, [{"content": BANNED}])
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_case_variant_xml_blocked():
    fake = _gateway_fake()
    xml = "<msg><item><title>%s</title></item></msg>" % BANNED
    result = _invoke_group(fake, {"type": "XML", "data": {"data": xml}})
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    result = _invoke_group(fake, "[CQ:XML,data=%s]" % xml)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_key_casing_and_node_dict_blocked():
    fake = _gateway_fake()
    result = _invoke_group(
        fake, "[CQ:share,url=https://example.com,Title=%s]" % BANNED
    )
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    result = _invoke_group(
        fake, {"type": "share", "data": {"Title": BANNED, "content": "hi"}}
    )
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    result = _invoke_group(
        fake,
        {
            "type": "node",
            "data": {
                "content": {"type": "text", "data": {"text": BANNED}},
            },
        },
    )
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    result = _invoke_group(fake, '[CQ:json,Data={"desc":"%s"}]' % BANNED)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


# ---------------------------------------------------------------------------
# 追加轮：typed 段键名大小写归一、try_begin 惰性清扫触发、
# 大小写归一良性对照（复审 P3/P4 收口）
# ---------------------------------------------------------------------------


def test_crash_fast_restart_recovers_via_try_begin(tmp_path, monkeypatch):
    # 惰性清扫的 try_begin 触发点（复审 P4）：快速重启用例此前
    # 只钉 list_pending 触发；新投递到达走的 try_begin 是最频
    # 繁路径——心跳过期后后继的第一次 try_begin（任意键）即应
    # 把遗留行收敛为 ambiguous，且对遗留键本身的重复投递返回
    # created=False + status='ambiguous'，不再误报 processing。
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731
    db = Database(tmp_path / "memory.db")
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-a")
    store_a = SqliteRequestStore(db, clock=clock)
    key = ("1000", "friend", "", "flag-fast-crash-try-begin")
    store_a.try_begin(key)
    assert store_a.claim_processing(key) is True

    now[0] = 1000.0 + 119.0
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-b")
    store_b = SqliteRequestStore(db, clock=clock)
    assert store_b.get(key).status == "processing"

    now[0] = 1000.0 + 121.0
    begun = store_b.try_begin(("1000", "friend", "", "flag-new-arrival"))
    assert begun.created is True
    assert store_b.get(key).status == "ambiguous"
    again = store_b.try_begin(key)
    assert again.created is False
    assert again.status == "ambiguous"


def test_content_review_typed_segment_key_casing_blocked():
    # typed 分支与 type/data 键本身的大小写变体（复审 P3）：
    # 此前精确键名查找提取为空、整段绕过内容复核，现与 CQ
    # 参数名/无类型回退同口径送审。
    fake = _gateway_fake()
    xml = "<msg><item><title>%s</title></item></msg>" % BANNED
    node_content = [{"type": "text", "data": {"text": BANNED}}]
    for message in (
        {"type": "text", "data": {"Text": BANNED}},
        {"type": "json", "data": {"Data": '{"desc":"%s"}' % BANNED}},
        {"type": "xml", "data": {"Data": xml}},
        {"type": "markdown", "data": {"Content": BANNED}},
        {"type": "node", "data": {"Content": node_content}},
        {"Type": "text", "data": {"text": BANNED}},
        {"type": "text", "Data": {"text": BANNED}},
    ):
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
    assert fake.transport.calls == []


def test_content_review_typed_segment_key_casing_benign_passes():
    # 上一组拦截形态的良性孪生（复审 P4）：同形态良性内容必须
    # 放行恰一次派发——过度拦截回归在此变红。每形态独立装配
    # （同 identity 默认限流 6/窗，逐形态断言恰一次 transport）。
    benign = (
        {"type": "text", "data": {"Text": "hi"}},
        {"type": "json", "data": {"Data": '{"desc":"hi"}'}},
        {
            "type": "xml",
            "data": {"Data": "<msg><item><title>hi</title></item></msg>"},
        },
        {"type": "markdown", "data": {"Content": "hi"}},
        {
            "type": "node",
            "data": {"Content": [{"type": "text", "data": {"text": "hi"}}]},
        },
        {"Type": "text", "data": {"text": "hi"}},
        {"type": "text", "Data": {"text": "hi"}},
    )
    for message in benign:
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_content_review_case_variant_benign_forms_still_dispatch():
    # 复审轮 2 大小写归一拦截用例的良性对照（复审 P4）：大写
    # 类型、CQ Data=、Title 键、node 单 dict 内容各备同形态
    # 良性放行——把任一变体一律判违规的过度拦截会静默误杀
    # 合法卡片/分享/合并转发，必须在此变红。
    benign = (
        {"type": "JSON", "data": {"data": '{"desc":"hi"}'}},
        '[CQ:json,Data={"desc":"hi"}]',
        {"type": "share", "data": {"Title": "hi", "content": "hello"}},
        "[CQ:share,url=https://example.com,Title=hi]",
        {
            "type": "node",
            "data": {"content": {"type": "text", "data": {"text": "hi"}}},
        },
        {
            "type": "XML",
            "data": {"data": "<msg><item><title>hi</title></item></msg>"},
        },
    )
    for message in benign:
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


# ---------------------------------------------------------------------------
# 再追加轮：大小写碰撞并集送审、清扫批量化+剪除、水位线、
# CQ 空格形态、forward/list/嵌套大小写钉、complete 失败留痕
# 全路径、心跳缺失/损坏分支（复审 r4 的 P3×2 + P4×4 收口）
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from companion.ingress.events import RequestDeferred  # noqa: E402


def test_content_review_case_collision_keys_blocked():
    # 大小写键碰撞（复审 P3）：同段同名变体双键、双序都必须
    # 并集送审——单值小写视图后写遮蔽先写，被遮蔽的违禁文本
    # 此前整段逃过复核（内层 data 键与外层 type/data 键同罪）。
    fake = _gateway_fake()
    for message in (
        {"type": "text", "data": {"Text": BANNED, "text": "hi"}},
        {"type": "text", "data": {"text": "hi", "Text": BANNED}},
        {"type": "text", "Data": {"text": BANNED}, "data": {"text": "hi"}},
        {"type": "text", "data": {"text": "hi"}, "Data": {"text": BANNED}},
    ):
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
    assert fake.transport.calls == []


def test_content_review_case_collision_benign_passes():
    # 碰撞形态的良性孪生：双键均为良性内容必须放行恰一次
    # 派发——并集送审不得误伤正常双键载荷。
    for message in (
        {"type": "text", "data": {"Text": "hi", "text": "hello"}},
        {"type": "text", "data": {"text": "hello", "Text": "hi"}},
        {"type": "text", "Data": {"text": "hi"}, "data": {"text": "hello"}},
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_content_review_cq_space_after_comma_blocked():
    # CQ 逗号后空格形（复审 P4）：'[CQ:json, data=...]' 与紧凑
    # 形同口径送审——空格形此前 marker 找不到、参数键带空格
    # 永不命中，整段提取为空漏审。
    fake = _gateway_fake()
    xml = "<msg><item><title>%s</title></item></msg>" % BANNED
    for payload in (
        '[CQ:json, data={"desc":"%s"}]' % BANNED,
        # 空格 + 值内未转义逗号：只有 marker 取码尾整段才审全，
        # 退回逗号切分的通用分支会在此漏掉后段（钉死 marker）。
        '[CQ:json, data={"app":"com.example","desc":"%s"}]' % BANNED,
        "[CQ:xml, data=%s]" % xml,
        "[CQ:share, url=https://example.com, title=%s]" % BANNED,
    ):
        result = _invoke_group(fake, payload)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), payload
    assert fake.transport.calls == []


def test_content_review_cq_space_after_comma_benign_passes():
    for payload in (
        '[CQ:json, data={"desc":"hi"}]',
        "[CQ:share, url=https://example.com, title=hi]",
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, payload)
        assert result.outcome == "ok", payload
        assert len(fake.transport.calls) == 1, payload


def test_content_review_wrapped_and_nested_casing_blocked():
    # 大小写归一的包裹/嵌套形态（复审 P4 补钉）：list 包裹的
    # Type 变体、forward 的 Content 变体、node 子段的
    # Type/Data/Text 变体——行为此前已正确，钉住防回退。
    fake = _gateway_fake()
    child = [{"type": "text", "data": {"text": BANNED}}]
    for message in (
        [{"Type": "text", "data": {"text": BANNED}}],
        {"type": "forward", "data": {"Content": child}},
        {
            "type": "node",
            "data": {"content": [{"Type": "text", "Data": {"Text": BANNED}}]},
        },
    ):
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
    assert fake.transport.calls == []


def test_content_review_wrapped_and_nested_casing_benign_passes():
    benign_child = [{"type": "text", "data": {"text": "hi"}}]
    for message in (
        [{"Type": "text", "data": {"text": "hi"}}],
        {"type": "forward", "data": {"Content": benign_child}},
        {
            "type": "node",
            "data": {"content": [{"Type": "text", "Data": {"Text": "hi"}}]},
        },
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


class _FalseCompleteStore:
    """complete 恒 False 的 store 包装（收口未生效必须留痕）。"""

    def __init__(self, inner):
        self._inner = inner

    def claim_processing(self, key):
        return self._inner.claim_processing(key)

    def complete(self, key, **kwargs):
        return False

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _StubChannel:
    """处置通道替身：按设定返回 outcome 或抛异常。"""

    def __init__(self, *, outcome=None, exc=None):
        self._outcome = outcome
        self._exc = exc
        self.calls = []

    async def dispose(self, ev, *, approve):
        self.calls.append(approve)
        if self._exc is not None:
            raise self._exc
        return SimpleNamespace(outcome=self._outcome)


@pytest.mark.parametrize(
    "channel",
    [
        _StubChannel(outcome="ok"),
        _StubChannel(outcome="rejected"),
        _StubChannel(outcome="ambiguous"),
        _StubChannel(exc=RequestDeferred("gateway_gate:disabled")),
        _StubChannel(exc=RuntimeError("boom")),
    ],
    ids=["ok", "rejected", "ambiguous", "deferred", "error"],
)
def test_handle_request_complete_failure_logs_on_channel_paths(
    tmp_path, caplog, channel
):
    # complete 失败留痕的全路径（复审 P4）：此前只钉 manual
    # 分支；通道的 ok / rejected / ambiguous 三态与异常
    # （RequestDeferred / 一般异常）四条收口路径任一丢掉
    # False 检查、静默吞掉失败收口，都必须在此变红。
    import logging

    real = SqliteRequestStore(Database(tmp_path / "false-complete-paths.db"))
    event = _friend_request_event("flag-false-complete-path")
    key = request_key(event.self_id, event.request_type, event.sub_type, event.flag)
    real.try_begin(key, user_id=event.user_id, group_id=event.group_id)
    with caplog.at_level(logging.WARNING, logger="companion.ingress.events"):
        run(
            handle_request(
                _Proc(),
                event,
                store=_FalseCompleteStore(real),
                channel=channel,
                policy=RequestPolicy(
                    mode="manual", friend_mode="approve_all", group_mode="manual"
                ),
            )
        )
    assert channel.calls == [True]
    assert "request complete not applied" in caplog.text
    assert real.get(key).status == "processing"


def test_sweep_owner_heartbeat_row_missing_is_stale(tmp_path, monkeypatch):
    # 清扫的心跳缺失分支（复审 P4）：代次归属已知、但该 boot
    # 的 request_boot_seen 行缺失——无法证明存活，按失联清扫
    # （processing 转 ambiguous、pending 保留）。
    db = Database(tmp_path / "memory.db")
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-a")
    store_a = SqliteRequestStore(db)
    key_processing = ("1000", "friend", "", "flag-seen-missing-processing")
    key_pending = ("1000", "friend", "", "flag-seen-missing-pending")
    store_a.try_begin(key_processing)
    store_a.try_begin(key_pending)
    assert store_a.claim_processing(key_processing) is True
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM schema_meta WHERE key='request_boot_seen:boot-a'"
        )
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-b")
    store_b = SqliteRequestStore(db)
    assert store_b.get(key_processing).status == "ambiguous"
    assert store_b.get(key_pending).status == "pending"


def test_sweep_owner_heartbeat_corrupt_is_stale(tmp_path, monkeypatch):
    # 清扫的心跳损坏分支（复审 P4）：心跳值非数字（磁盘损坏
    # 形态）与缺失同口径——解析失败归失联、fail-closed 清扫。
    db = Database(tmp_path / "memory.db")
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-a")
    store_a = SqliteRequestStore(db)
    key_processing = ("1000", "friend", "", "flag-seen-corrupt-processing")
    key_pending = ("1000", "friend", "", "flag-seen-corrupt-pending")
    store_a.try_begin(key_processing)
    store_a.try_begin(key_pending)
    assert store_a.claim_processing(key_processing) is True
    with db.transaction() as conn:
        conn.execute(
            "UPDATE schema_meta SET value='not-a-float'"
            " WHERE key='request_boot_seen:boot-a'"
        )
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-b")
    store_b = SqliteRequestStore(db)
    assert store_b.get(key_processing).status == "ambiguous"
    assert store_b.get(key_pending).status == "pending"


def test_sweep_prunes_history_and_try_begin_stays_flat(tmp_path):
    # 清扫成本回归钉（复审 P3）：播种 3000 个历史代次（频繁
    # 重启的累积形态）后，后继构造清扫应剪除 boot 元数据并
    # 推进水位线；其后 try_begin 的清扫状态不随历史代次增长
    # ——以结构性断言（元数据键数 / 水位线）确定性证明，不用
    # 墙钟断言（复审 P4：负载环境下墙钟必假红，属性本身由结
    # 构口径证明）。
    db = Database(tmp_path / "memory.db")
    SqliteRequestStore(db)
    with db.transaction() as conn:
        for generation in range(1, 3001):
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES (?, ?)",
                (f"request_gen_boot:{generation}", f"boot-hist-{generation}"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES (?, ?)",
                (f"request_boot_seen:boot-hist-{generation}", "1.0"),
            )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta VALUES"
            " ('request_generation', '3000')"
        )
    store = SqliteRequestStore(db)
    with db.transaction() as conn:
        boot_keys = conn.execute(
            "SELECT COUNT(*) FROM schema_meta"
            " WHERE key LIKE 'request_gen_boot:%'"
            " OR key LIKE 'request_boot_seen:%'"
        ).fetchone()[0]
        watermark = conn.execute(
            "SELECT value FROM schema_meta"
            " WHERE key='request_sweep_watermark'"
        ).fetchone()[0]
    # 结构断言：只剩当前代次归属 + 本 boot 心跳（个位数），
    # 水位线推进到全部历史代次之后——不随历史总量增长。
    assert boot_keys <= 4
    assert watermark == "3000"
    # 平坦性代理（确定性，无墙钟）：再跑 20 次 try_begin（每次
    # 都触发惰性清扫），清扫状态（boot 元数据键数、水位线）
    # 必须纹丝不动——若清扫成本随历史代次线性增长，其状态
    # 必然随之膨胀，此断言即变红。
    for index in range(20):
        store.try_begin(("1000", "friend", "", f"flag-perf-flat-{index}"))
    with db.transaction() as conn:
        boot_keys_after = conn.execute(
            "SELECT COUNT(*) FROM schema_meta"
            " WHERE key LIKE 'request_gen_boot:%'"
            " OR key LIKE 'request_boot_seen:%'"
        ).fetchone()[0]
        watermark_after = conn.execute(
            "SELECT value FROM schema_meta"
            " WHERE key='request_sweep_watermark'"
        ).fetchone()[0]
    assert boot_keys_after == boot_keys
    assert watermark_after == "3000"


# ---------------------------------------------------------------------------
# 再再追加轮：水位线滞留修复（清扫覆盖全代次）、CQ 碰撞保值、
# node 子内容统一提取、水位线/代次值容错、清扫屏障与批量行
# 补钉、分支碰撞补钉（复审 r5 的 P3×3 + P4×3 收口）
# ---------------------------------------------------------------------------


def test_pruned_boot_claim_after_prune_still_converges(tmp_path, monkeypatch):
    # 法官序列（复审 P3）：B 剪除 gen1（水位线=1）→ 被误判
    # 失联的 A 苏醒、以旧代次再 claim → A 真死 → B 多次清扫
    # 后该行必须收敛 ambiguous。水位线只管元数据剪除、永不
    # 管清扫覆盖面——此行此前落在水位线之前永久停 processing。
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731
    db = Database(tmp_path / "memory.db")
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-a")
    store_a = SqliteRequestStore(db, clock=clock)
    key = ("1000", "friend", "", "flag-pruned-claim")
    store_a.try_begin(key)

    # A 写静默超阈值：B 构造时清扫并剪除 gen1、水位线推进到 1。
    now[0] = 1000.0 + 121.0
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-b")
    store_b = SqliteRequestStore(db, clock=clock)
    with db.transaction() as conn:
        watermark = conn.execute(
            "SELECT value FROM schema_meta"
            " WHERE key='request_sweep_watermark'"
        ).fetchone()[0]
    assert watermark == "1"

    # A 苏醒（只是写静默、并未真死）仍以旧代次 claim。
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-a")
    assert store_a.claim_processing(key) is True
    assert store_b.get(key).generation == 1
    assert store_b.get(key).status == "processing"

    # A 真死（心跳过期）后，B 反复读写清扫：行收敛 ambiguous。
    now[0] = 1000.0 + 121.0 + 121.0
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-b")
    store_b.list_pending()
    store_b.try_begin(("1000", "friend", "", "flag-pruned-other"))
    store_b.list_pending()
    assert store_b.get(key).status == "ambiguous"


def test_corrupt_sweep_watermark_falls_back_to_full_rescan(
    tmp_path, monkeypatch
):
    # 水位线值损坏（复审 P4）：裸 int() 此前把每次 try_begin /
    # list_pending 炸成 ValueError、请求管线整体砖化；防御
    # 解析回落 0 = 全量重扫——不抛错，且失联代次的遗留行
    # 照常被扫成 ambiguous（不是只求不崩）。
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731
    db = Database(tmp_path / "memory.db")
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-a")
    store_a = SqliteRequestStore(db, clock=clock)
    key = ("1000", "friend", "", "flag-corrupt-watermark")
    store_a.try_begin(key)
    assert store_a.claim_processing(key) is True
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta VALUES"
            " ('request_sweep_watermark', 'garbage')"
        )
    now[0] = 1000.0 + 121.0
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-b")
    store_b = SqliteRequestStore(db, clock=clock)
    assert store_b.get(key).status == "ambiguous"
    assert store_b.list_pending() == []
    begun = store_b.try_begin(("1000", "friend", "", "flag-after-corrupt"))
    assert begun.created is True


def test_corrupt_request_generation_does_not_brick_construction(
    tmp_path, monkeypatch
):
    # request_generation 值损坏（复审 P4）：与水位线同口径
    # 防御解析——按无历史论构造，不抛错、store 照常可用，
    # 既有 pending 行不被误扫。
    db = Database(tmp_path / "memory.db")
    store = SqliteRequestStore(db)
    key = ("1000", "friend", "", "flag-corrupt-generation")
    store.try_begin(key)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE schema_meta SET value='garbage'"
            " WHERE key='request_generation'"
        )
    monkeypatch.setattr(
        "companion.memory.requests._PROCESS_BOOT_ID", "boot-next"
    )
    store2 = SqliteRequestStore(db)
    begun = store2.try_begin(("1000", "friend", "", "flag-corrupt-gen-new"))
    assert begun.created is True
    assert store2.get(key).status == "pending"


def _seed_disposition(conn, flag, status, generation):
    conn.execute(
        "INSERT INTO request_dispositions"
        " (self_id, request_type, sub_type, flag, user_id, group_id,"
        " status, decision, result_code, generation, created_at, updated_at)"
        " VALUES ('1000', 'friend', '', ?, '90001', NULL, ?, NULL, NULL,"
        " ?, 1.0, 1.0)",
        (flag, status, generation),
    )


def test_lagging_request_generation_uses_data_floor(tmp_path, monkeypatch):
    # 滞后计数器（复审 P3）：request_generation 可解析但落
    # 后于实存数据（存 2、数据最大代次 5，如部分恢复/旧备
    # 份元数据配新处置行）——此前下限只在值损坏时介入，新
    # boot 按滞后值 +1 复用死 boot 代次并覆盖其归属行，该
    # 代次 processing 行被当作活 boot 自有行永不收敛。下限
    # 恒为 max(已存计数器, 数据推导最大值) 后新代次必为
    # 6，死 boot 两行经构造清扫按既有规则收敛 ambiguous。
    db = Database(tmp_path / "memory.db")
    SqliteRequestStore(db)  # 建表
    with db.transaction() as conn:
        _seed_disposition(conn, "flag-lag-gen5", "processing", 5)
        _seed_disposition(conn, "flag-lag-gen2", "processing", 2)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta VALUES"
            " ('request_generation', '2')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta VALUES"
            " ('request_gen_boot:5', 'dead-boot')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta VALUES"
            " ('request_gen_boot:2', 'dead-boot')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta VALUES"
            " ('request_boot_seen:dead-boot', '1000.0')"
        )
    monkeypatch.setattr(
        "companion.memory.requests._PROCESS_BOOT_ID", "boot-new"
    )
    store = SqliteRequestStore(db, clock=lambda: 5000.0)
    assert store.generation == 6
    with db.transaction() as conn:
        stored = conn.execute(
            "SELECT value FROM schema_meta"
            " WHERE key='request_generation'"
        ).fetchone()[0]
    assert stored == "6"
    assert store.get(("1000", "friend", "", "flag-lag-gen5")).status == (
        "ambiguous"
    )
    assert store.get(("1000", "friend", "", "flag-lag-gen2")).status == (
        "ambiguous"
    )
    begun = store.try_begin(("1000", "friend", "", "flag-lag-new"))
    assert begun.created is True


def test_sweep_live_barrier_holds_watermark_and_sweeps_beyond(tmp_path):
    # 清扫屏障钉（复审 P4）：失联 / 活 / 失联三代次并存、各
    # 带真实处置行——活代次挡住连续已决前缀（水位线停在它
    # 之前、其元数据保留），它之后的失联代次照常清扫（行转
    # ambiguous）但元数据不剪；活代次的行与各 pending 行
    # 一律不动。此前只钉了全失联连续路径，屏障行为无测。
    import time as time_module

    db = Database(tmp_path / "memory.db")
    SqliteRequestStore(db)
    fresh = repr(time_module.time())
    with db.transaction() as conn:
        for generation, owner, seen in (
            (1, "boot-stale-1", "1.0"),
            (2, "boot-live-2", fresh),
            (3, "boot-stale-3", "1.0"),
        ):
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES (?, ?)",
                (f"request_gen_boot:{generation}", owner),
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES (?, ?)",
                (f"request_boot_seen:{owner}", seen),
            )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta VALUES"
            " ('request_generation', '3')"
        )
        _seed_disposition(conn, "flag-barrier-stale-1", "processing", 1)
        _seed_disposition(conn, "flag-barrier-live-2", "processing", 2)
        _seed_disposition(conn, "flag-barrier-stale-3", "processing", 3)
        _seed_disposition(conn, "flag-barrier-pending-3", "pending", 3)
    store = SqliteRequestStore(db)
    assert store.get(("1000", "friend", "", "flag-barrier-stale-1")).status == (
        "ambiguous"
    )
    assert store.get(("1000", "friend", "", "flag-barrier-live-2")).status == (
        "processing"
    )
    assert store.get(("1000", "friend", "", "flag-barrier-stale-3")).status == (
        "ambiguous"
    )
    assert store.get(("1000", "friend", "", "flag-barrier-pending-3")).status == (
        "pending"
    )
    with db.transaction() as conn:
        watermark = conn.execute(
            "SELECT value FROM schema_meta"
            " WHERE key='request_sweep_watermark'"
        ).fetchone()[0]
        meta_keys = {
            row[0] for row in conn.execute("SELECT key FROM schema_meta")
        }
    # 水位线停在活代次之前；只有已决前缀（gen1）的归属被剪。
    assert watermark == "1"
    assert "request_gen_boot:1" not in meta_keys
    assert "request_gen_boot:2" in meta_keys
    assert "request_gen_boot:3" in meta_keys
    # 屏障之后代次的归属仍被引用，其 boot 心跳保留；已剪
    # 代次的失联 boot 心跳一并删除。
    assert "request_boot_seen:boot-live-2" in meta_keys
    assert "request_boot_seen:boot-stale-3" in meta_keys
    assert "request_boot_seen:boot-stale-1" not in meta_keys


def test_sweep_prunes_thousands_of_generations_with_rows(tmp_path):
    # 批量 UPDATE 过真实行（复审 P4）：3000 历史代次场景此前
    # 只播元数据、零处置行，批量清扫是否真覆到行无从断言。
    # 这次在多个失联代次下播 processing 行：后继构造后应全
    # 部转 ambiguous、pending 保留，剪除/水位线口径不变。
    db = Database(tmp_path / "memory.db")
    SqliteRequestStore(db)
    with db.transaction() as conn:
        for generation in range(1, 3001):
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES (?, ?)",
                (f"request_gen_boot:{generation}", f"boot-hist-{generation}"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES (?, ?)",
                (f"request_boot_seen:boot-hist-{generation}", "1.0"),
            )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta VALUES"
            " ('request_generation', '3000')"
        )
        processing_flags = []
        for generation in range(1, 3001, 500):
            flag = f"flag-hist-processing-{generation}"
            _seed_disposition(conn, flag, "processing", generation)
            processing_flags.append(flag)
        _seed_disposition(conn, "flag-hist-pending-7", "pending", 7)
        _seed_disposition(conn, "flag-hist-pending-2999", "pending", 2999)
    store = SqliteRequestStore(db)
    for flag in processing_flags:
        assert store.get(("1000", "friend", "", flag)).status == "ambiguous", flag
    assert store.get(("1000", "friend", "", "flag-hist-pending-7")).status == (
        "pending"
    )
    assert store.get(
        ("1000", "friend", "", "flag-hist-pending-2999")
    ).status == "pending"
    with db.transaction() as conn:
        boot_keys = conn.execute(
            "SELECT COUNT(*) FROM schema_meta"
            " WHERE key LIKE 'request_gen_boot:%'"
            " OR key LIKE 'request_boot_seen:%'"
        ).fetchone()[0]
        watermark = conn.execute(
            "SELECT value FROM schema_meta"
            " WHERE key='request_sweep_watermark'"
        ).fetchone()[0]
    assert boot_keys <= 4
    assert watermark == "3000"


def test_content_review_cq_param_collision_blocked():
    # CQ 参数碰撞（复审 P3）：同名参数的大小写变体与精确重
    # 复此前在 _cq_code_segment 里单 dict 覆写 last-wins，被
    # 遮蔽的违禁值整段逃过复核并实际派发——dict 段形态已关
    # 的同类绕过不得在 CQ 串形态上残留（双序 + 精确重复）。
    fake = _gateway_fake()
    for payload in (
        "[CQ:share,url=https://example.com,title=%s,Title=benign]" % BANNED,
        "[CQ:share,url=https://example.com,Title=benign,title=%s]" % BANNED,
        "[CQ:share,url=https://example.com,title=%s,title=benign]" % BANNED,
        "[CQ:share,url=https://example.com,title=benign,title=%s]" % BANNED,
    ):
        result = _invoke_group(fake, payload)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), payload
    assert fake.transport.calls == []


def test_content_review_cq_param_collision_benign_passes():
    # CQ 碰撞形态的良性孪生：双值均为良性必须放行恰一次
    # 派发——碰撞保值不得误伤正常重复参数载荷。
    for payload in (
        "[CQ:share,url=https://example.com,title=hi,Title=hello]",
        "[CQ:share,url=https://example.com,title=hi,title=hello]",
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, payload)
        assert result.outcome == "ok", payload
        assert len(fake.transport.calls) == 1, payload


def test_content_review_node_children_all_forms_blocked():
    # node 子内容统一提取（复审 P3）：列表内裸字符串子、无
    # type/data 的 dict 子、单 dict 形的无类型子此前经
    # _payload_segment_texts 提取为空、整段绕审并实际派发；
    # 改走 _payload_visible_texts 后与顶层 / forward 同形态
    # 同口径受审。
    fake = _gateway_fake()
    for message in (
        {"type": "node", "data": {"content": [BANNED]}},
        {"type": "node", "data": {"content": [{"text": BANNED}]}},
        {"type": "node", "data": {"content": {"text": BANNED}}},
    ):
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
    assert fake.transport.calls == []


def test_content_review_node_children_benign_passes():
    for message in (
        {"type": "node", "data": {"content": ["hi"]}},
        {"type": "node", "data": {"content": [{"text": "hi"}]}},
        {"type": "node", "data": {"content": {"text": "hi"}}},
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_content_review_collision_branches_blocked():
    # 碰撞并集的分支补钉（复审 P4）：此前只钉 text 分支；
    # Type/type 类型键碰撞、json 与 node 的 data 键碰撞同由
    # 多值循环实现但无测——未来对单分支特化短路会静默重开
    # 卡片 / 合并转发这些高价值藏匿点的绕过，必须在此变红。
    fake = _gateway_fake()
    node_benign = [{"type": "text", "data": {"text": "hi"}}]
    node_banned = [{"type": "text", "data": {"text": BANNED}}]
    for message in (
        {"Type": "image", "type": "text", "data": {"text": BANNED}},
        {"Type": "text", "type": "image", "data": {"text": BANNED}},
        {
            "type": "json",
            "data": {"Data": '{"desc":"hi"}', "data": '{"desc":"%s"}' % BANNED},
        },
        {
            "type": "json",
            "data": {"data": '{"desc":"%s"}' % BANNED, "Data": '{"desc":"hi"}'},
        },
        {"type": "node", "data": {"Content": node_benign, "content": node_banned}},
        {"type": "node", "data": {"content": node_banned, "Content": node_benign}},
    ):
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
    assert fake.transport.calls == []


def test_content_review_collision_branches_benign_passes():
    for message in (
        {"Type": "image", "type": "text", "data": {"text": "hi"}},
        {
            "type": "json",
            "data": {"Data": '{"desc":"hi"}', "data": '{"desc":"hello"}'},
        },
        {
            "type": "node",
            "data": {
                "Content": [{"type": "text", "data": {"text": "hi"}}],
                "content": [{"type": "text", "data": {"text": "hello"}}],
            },
        },
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


# ---------------------------------------------------------------------------
# 原理收口轮：XML 真解析（属性/CDATA 全文本）、JSON 深嵌套不再
# 静默丢弃、提取深度有界（超限即拦）、失联边界精确钉、complete
# 抛异常留痕（复审 r6 的 P2×1 + P3×2 + P4×4 收口）
# ---------------------------------------------------------------------------


def test_content_review_xml_attribute_values_blocked():
    # 属性形态（复审 P2）：brief 等属性值是平台渲染的可见文本，
    # 正则剥标签曾把它们连同标签一起丢弃、整段逃审。
    for message in (
        {"type": "xml", "data": {"data": '<msg brief="%s">x</msg>' % BANNED}},
        {
            "type": "xml",
            "data": {
                "data": '<msg><item layout="1" title="%s">x</item></msg>' % BANNED
            },
        },
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_xml_cdata_blocked():
    # CDATA 形态（复审 P2）：CDATA 是字面元素内容，正则剥标签
    # 曾把整个 CDATA 段当标签丢弃。
    fake = _gateway_fake()
    message = {
        "type": "xml",
        "data": {"data": "<msg><title><![CDATA[%s]]></title></msg>" % BANNED},
    }
    result = _invoke_group(fake, message)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_xml_attribute_cq_form_blocked():
    # CQ 串形态与 dict 形态同口径（复审 P2）：属性值进审查。
    fake = _gateway_fake()
    result = _invoke_group(fake, '[CQ:xml,data=<msg brief="%s">x</msg>]' % BANNED)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_xml_attribute_and_cdata_benign_passes():
    # 良性孪生：属性值与 CDATA 内容良性时不得误拦，恰一次派发。
    fake = _gateway_fake()
    for message in (
        {"type": "xml", "data": {"data": '<msg brief="今日分享">x</msg>'}},
        {
            "type": "xml",
            "data": {
                "data": "<msg><title><![CDATA[大家好，今天开会]]></title></msg>"
            },
        },
    ):
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
    assert len(fake.transport.calls) == 2


def _nested_json_value(depth, leaf):
    value = leaf
    for _ in range(depth):
        value = {"k": value}
    return value


def test_content_review_json_deep_nesting_blocked():
    # 深嵌套形态（复审 P3）：_payload_all_strings 旧版 depth>8
    # 静默返回 []、深层字符串 fail-open 逃审；现改迭代全量收
    # 集，深度 9/12 的违禁文本必须被拦。
    for depth in (9, 12):
        fake = _gateway_fake()
        message = {
            "type": "json",
            "data": {"data": _nested_json_value(depth, BANNED)},
        }
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), depth
        assert fake.transport.calls == [], depth


def test_content_review_json_deep_nesting_string_form_blocked():
    # JSON 串形态同口径：data 是 JSON 字符串、深层嵌套同拦。
    import json as _json

    fake = _gateway_fake()
    message = {
        "type": "json",
        "data": {"data": _json.dumps(_nested_json_value(12, BANNED))},
    }
    result = _invoke_group(fake, message)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_json_deep_nesting_benign_passes():
    # 良性孪生：深层嵌套良性内容正常派发（收集口径放开后不得
    # 把深嵌套一律误判为不可审）。
    fake = _gateway_fake()
    message = {
        "type": "json",
        "data": {"data": _nested_json_value(12, "大家好，今天开会")},
    }
    result = _invoke_group(fake, message)
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def _nested_node_payload(depth, leaf):
    value = leaf
    for _ in range(depth):
        value = {"type": "node", "data": {"content": [value]}}
    return value


def test_content_review_deep_node_nesting_rejected_not_raised():
    # 超深嵌套（复审 P3）：提取贯穿深度界，400/1500 层都必须
    # 得到 content_review_blocked 的 ActionResult，而不是让
    # RecursionError 穿出 invoke（修复前 400 层即抛错）。
    leaf = {"type": "text", "data": {"text": BANNED}}
    for depth in (400, 1500):
        fake = _gateway_fake()
        result = _invoke_group(fake, _nested_node_payload(depth, leaf))
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), depth
        assert fake.transport.calls == [], depth


def test_content_review_deep_forward_nesting_rejected_not_raised():
    # forward 形态同口径：超深嵌套拦截而非异常。
    leaf = {"type": "text", "data": {"text": BANNED}}
    value = leaf
    for _ in range(400):
        value = {"type": "forward", "data": {"content": [value]}}
    fake = _gateway_fake()
    result = _invoke_group(fake, value)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_overdeep_benign_also_rejected():
    # 超限即拦的口径钉：超深结构即使内容良性也判不可审拦截
    # ——提取不完不放行（fail-closed），不因内容未见违禁而过。
    leaf = {"type": "text", "data": {"text": "大家好，今天开会"}}
    fake = _gateway_fake()
    result = _invoke_group(fake, _nested_node_payload(400, leaf))
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_payload_visible_texts_overdeep_raises_unreviewable():
    # 机制钉：提取层超深抛 _PayloadUnreviewable 信号（由网关
    # 转拦截），不抛 RecursionError。
    from companion.adapters.gateway import (
        _payload_visible_texts,
        _PayloadUnreviewable,
    )

    leaf = {"type": "text", "data": {"text": "hi"}}
    with pytest.raises(_PayloadUnreviewable):
        _payload_visible_texts(_nested_node_payload(400, leaf))


def test_boot_stale_exact_boundary_120_seconds(tmp_path, monkeypatch):
    # 失联边界精确钉（复审 P4）：恰好 120.0 秒即判失联（代码
    # >= 口径，常量注释已对齐）；此前只探 119/121，>= 变 >
    # 的变异全绿无信号——本用例使该变异变红。
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731
    db = Database(tmp_path / "memory.db")
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-a")
    store_a = SqliteRequestStore(db, clock=clock)
    key = ("1000", "friend", "", "flag-stale-exact-boundary")
    store_a.try_begin(key)
    assert store_a.claim_processing(key) is True

    now[0] = 1000.0 + 120.0
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-b")
    store_b = SqliteRequestStore(db, clock=clock)
    assert store_b.get(key).status == "ambiguous"


class _RaisingCompleteStore:
    """complete 恒抛异常的 store 包装（收口异常必须留痕、不外抛）。"""

    def __init__(self, inner):
        self._inner = inner

    def claim_processing(self, key):
        return self._inner.claim_processing(key)

    def complete(self, key, **kwargs):
        raise RuntimeError("store boom")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_handle_request_complete_raising_logs_and_continues(tmp_path, caplog):
    # complete 抛异常（复审 P4）：manual 路径——包装层必须吞
    # 异常、记 warning 留痕（含异常类型）、行仍停 processing，
    # handle_request 正常返回不外抛。
    import logging

    real = SqliteRequestStore(Database(tmp_path / "raising-complete.db"))
    event = _friend_request_event("flag-raising-complete")
    key = request_key(event.self_id, event.request_type, event.sub_type, event.flag)
    real.try_begin(key, user_id=event.user_id, group_id=event.group_id)
    with caplog.at_level(logging.WARNING, logger="companion.ingress.events"):
        run(
            handle_request(
                _Proc(),
                event,
                store=_RaisingCompleteStore(real),
                channel=None,
                policy=RequestPolicy(
                    mode="manual", friend_mode="approve_all", group_mode="manual"
                ),
            )
        )
    assert "request complete not applied" in caplog.text
    assert "RuntimeError" in caplog.text
    assert real.get(key).status == "processing"


def test_handle_request_complete_raising_logs_on_channel_path(tmp_path, caplog):
    # complete 抛异常（复审 P4）：通道路径同口径留痕不外抛。
    import logging

    real = SqliteRequestStore(Database(tmp_path / "raising-complete-path.db"))
    event = _friend_request_event("flag-raising-complete-path")
    key = request_key(event.self_id, event.request_type, event.sub_type, event.flag)
    real.try_begin(key, user_id=event.user_id, group_id=event.group_id)
    channel = _StubChannel(outcome="ok")
    with caplog.at_level(logging.WARNING, logger="companion.ingress.events"):
        run(
            handle_request(
                _Proc(),
                event,
                store=_RaisingCompleteStore(real),
                channel=channel,
                policy=RequestPolicy(
                    mode="manual", friend_mode="approve_all", group_mode="manual"
                ),
            )
        )
    assert channel.calls == [True]
    assert "request complete not applied" in caplog.text
    assert "RuntimeError" in caplog.text
    assert real.get(key).status == "processing"


# ---------------------------------------------------------------------------
# 再开一轮（r7）：补扫对已剪代次按心跳判失联、DOCTYPE 判不可审、
# 嵌套列表递归提取、资源引用匹配归一、本轮招牌路径结果级覆盖、
# 深度 64 恰界、裸串 data 判不可审、complete 留痕补请求 key
# （复审 r6 报告 2026-11-01-171342 的 P3×6 + P4×3 收口）
# ---------------------------------------------------------------------------


def test_pruned_boot_revived_claim_survives_within_stale_window(
    tmp_path, monkeypatch
):
    # 法官序列（复审 P3）：B 剪除 gen1（归属行删除、水位线=1）
    # → A 苏醒以旧代次 claim（心跳刷新）→ B 在 1 秒后清扫时
    # 不得把该行扫成 ambiguous——剪除时归属行已删，补扫此前
    # 对已剪代次恒判失联，120 秒窗口对苏醒行完全失效。修法：
    # claim 时若本代次 ≤ 水位线则重注册本代次归属行（代次号
    # 与归属 boot 不变），补扫恢复按心跳判失联；满 120 秒失
    # 联后仍须收敛 ambiguous（与既有收敛钉同向）。
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731
    db = Database(tmp_path / "memory.db")
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-a")
    store_a = SqliteRequestStore(db, clock=clock)
    key = ("1000", "friend", "", "flag-pruned-revived")
    store_a.try_begin(key)

    now[0] = 1000.0 + 121.0
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-b")
    store_b = SqliteRequestStore(db, clock=clock)
    with db.transaction() as conn:
        watermark = conn.execute(
            "SELECT value FROM schema_meta"
            " WHERE key='request_sweep_watermark'"
        ).fetchone()[0]
        gen_boot_1 = conn.execute(
            "SELECT value FROM schema_meta"
            " WHERE key='request_gen_boot:1'"
        ).fetchone()
    assert watermark == "1"
    assert gen_boot_1 is None  # 前置事实：归属行确已被剪除

    # A 苏醒 claim：心跳刷新到 1121，归属行同步重注册。
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-a")
    assert store_a.claim_processing(key) is True
    with db.transaction() as conn:
        gen_boot_1 = conn.execute(
            "SELECT value FROM schema_meta"
            " WHERE key='request_gen_boot:1'"
        ).fetchone()
    assert gen_boot_1 is not None and gen_boot_1[0] == "boot-a"

    # B 在 claim 后 1 秒清扫：心跳新鲜，行必须仍 processing。
    now[0] = 1000.0 + 121.0 + 1.0
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-b")
    store_b.try_begin(("1000", "friend", "", "flag-pruned-revived-peer"))
    assert store_b.get(key).status == "processing"
    # claim 后 119 秒：仍在失联窗口内，不扫。
    now[0] = 1000.0 + 121.0 + 119.0
    store_b.list_pending()
    assert store_b.get(key).status == "processing"
    # claim 后恰 120 秒：达到（>=）失联阈值，收敛 ambiguous。
    now[0] = 1000.0 + 121.0 + 120.0
    store_b.list_pending()
    assert store_b.get(key).status == "ambiguous"


def _doctype_entity_split_xml():
    # 字符级实体拆分：任一违禁短语都不在原文字面连续出现，
    # 只有平台展开内部实体后才拼装成形。
    chars = list(BANNED)
    decls = "".join(
        '<!ENTITY e%d "%s">' % (index, char) for index, char in enumerate(chars)
    )
    refs = "".join("&e%d;" % index for index in range(len(chars)))
    return "<!DOCTYPE m [%s]><m>%s</m>" % (decls, refs)


def test_content_review_xml_doctype_direct_banned_blocked():
    # DOCTYPE 直含违禁（复审 P3-5）：属性与元素文本直含违禁
    # 的 DTD 输入，dict + CQ 双形态必须拦截、零派发。
    for message in (
        {"type": "xml", "data": {"data": '<!DOCTYPE m><m brief="%s">x</m>' % BANNED}},
        {"type": "xml", "data": {"data": "<!DOCTYPE m><m>%s</m>" % BANNED}},
        '[CQ:xml,data=<!DOCTYPE m><m brief="%s">x</m>]' % BANNED,
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_xml_doctype_entity_split_blocked():
    # DOCTYPE 实体拆分（复审 P3-2）：内部实体拼装的违禁短语
    # 在提取层拼不出来——含 DTD 的输入判不可审拦截（合法卡
    # 片不需要 DTD），dict + CQ 双形态同口径。
    raw = _doctype_entity_split_xml()
    for message in (
        {"type": "xml", "data": {"data": raw}},
        "[CQ:xml,data=%s]" % raw,
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_xml_doctype_benign_blocked_by_class():
    # 类别口径钉：含 DTD 的良性 XML 同样判不可审拦截——拦的
    # 是「带 DTD」这一类别而非内容（fail-closed），防未来把
    # DTD 输入改回原文回落送审时静默重开实体拼装绕过。
    fake = _gateway_fake()
    message = {
        "type": "xml",
        "data": {"data": '<!DOCTYPE m><m brief="今日分享">x</m>'},
    }
    result = _invoke_group(fake, message)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_xml_malformed_direct_banned_blocked():
    # 畸形 XML 直含违禁（复审 P3-5）：非 DTD 的解析失败维持
    # 原文整体送审回落，字面违禁必须被审出拦截，双形态。
    for message in (
        {"type": "xml", "data": {"data": "<msg><title>%s" % BANNED}},
        "[CQ:xml,data=<msg><title>%s]" % BANNED,
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_xml_malformed_benign_passes():
    # 良性孪生：畸形 XML 的良性字面内容回落送审后正常派发。
    fake = _gateway_fake()
    result = _invoke_group(
        fake, {"type": "xml", "data": {"data": "<msg><title>大家好"}}
    )
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def test_content_review_xml_charref_banned_blocked():
    # 字符引用形态（复审 P3-5）：良构 XML 中以十进制字符引
    # 用编码的违禁文本由真解析器解码后送审，双形态必拦。
    encoded = "".join("&#%d;" % ord(char) for char in BANNED)
    raw = "<msg><title>%s</title></msg>" % encoded
    for message in (
        {"type": "xml", "data": {"data": raw}},
        "[CQ:xml,data=%s]" % raw,
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_xml_charref_benign_passes():
    encoded = "".join("&#%d;" % ord(char) for char in "大家好，今天开会")
    fake = _gateway_fake()
    result = _invoke_group(
        fake, {"type": "xml", "data": {"data": "<msg><title>%s</title></msg>" % encoded}}
    )
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def test_content_review_xml_malformed_attribute_banned_blocked():
    # 畸形 XML 属性藏词（复审 P2，封版残余 R-S3-1 在此关
    # 闭）：回落剥标签曾把标签连同属性值一并丢弃，brief=
    # 藏词整段逃审；回落改并集双流（剥标签流 + 原文流，替
    # 代关系不拼接）后属性值受审。未闭合根与多根两形态、
    # dict + CQ 双形，另加属性内字符引用形态（原文流同口
    # 径实体解码）。
    encoded = "".join("&#%d;" % ord(char) for char in BANNED)
    for raw in (
        '<msg brief="%s"><title>大家好' % BANNED,
        '<msg brief="%s"><title>大家好</title></msg><x/>' % BANNED,
        '<msg brief="%s"><title>大家好' % encoded,
    ):
        for message in (
            {"type": "xml", "data": {"data": raw}},
            "[CQ:xml,data=%s]" % raw,
        ):
            fake = _gateway_fake()
            result = _invoke_group(fake, message)
            assert (result.outcome, result.gate) == (
                "rejected",
                "content_review_blocked",
            ), message
            assert fake.transport.calls == [], message


def test_content_review_xml_malformed_attribute_benign_passes():
    # 良性孪生：畸形 XML（未闭合/多根）带普通属性，原文流
    # 送审后仍恰一次派发——并集不得把良性属性文本误判。
    for raw in (
        '<msg brief="周末安排"><title>大家好',
        '<msg brief="周末安排"><title>大家好</title></msg><x/>',
    ):
        fake = _gateway_fake()
        result = _invoke_group(
            fake, {"type": "xml", "data": {"data": raw}}
        )
        assert result.outcome == "ok", raw
        assert len(fake.transport.calls) == 1, raw


def test_content_review_xml_malformed_cdata_banned_blocked():
    # 畸形 XML 的 CDATA 藏词（复审 P3，测试缺口）：回落的
    # CDATA 解包步此前无钉（删之全量照绿）；未闭合与多根
    # 两形态、dict + CQ 双形必拦。
    # CDATA 边界拆分另列一形：短语一半在 CDATA 内、一半在
    # 外——原文流中被 ']]>' 隔断，只有解包+剥标签流恢复连
    # 续（删 CDATA 解包步此形必红，变异自验在案）。用「自
    # 杀」作短语：BANNED 两半各自成独立否认形态、拆分不
    # 变红，无变异鉴别力。其 CQ 形须把 ']' 转义为 &#93;
    # （平台 CQ 文法在未转义 ']' 处截断码段，未转义串在平
    # 台侧本就不编码完整 XML，与剥离/解析同文法口径一致）。
    split_raw = "<msg><title><![CDATA[自]]>杀"
    split_cq = "[CQ:xml,data=<msg><title><![CDATA[自&#93;&#93;>杀]"
    for message in (
        {"type": "xml", "data": {"data": "<msg><title><![CDATA[%s]]>" % BANNED}},
        "[CQ:xml,data=<msg><title><![CDATA[%s]]>]" % BANNED,
        {
            "type": "xml",
            "data": {
                "data": "<msg><title><![CDATA[%s]]></title></msg><x/>" % BANNED
            },
        },
        "[CQ:xml,data=<msg><title><![CDATA[%s]]></title></msg><x/>]" % BANNED,
        {"type": "xml", "data": {"data": split_raw}},
        split_cq,
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_xml_malformed_charref_banned_blocked():
    # 畸形 XML 的字符引用藏词（复审 P3，测试缺口）：回落的
    # 实体解码步此前无钉（删之全量照绿）；十进制与十六进
    # 制两编码、未闭合形态、dict + CQ 双形必拦。
    decimal = "".join("&#%d;" % ord(char) for char in BANNED)
    hexadecimal = "".join("&#x%x;" % ord(char) for char in BANNED)
    for encoded in (decimal, hexadecimal):
        raw = "<msg><title>%s" % encoded
        for message in (
            {"type": "xml", "data": {"data": raw}},
            "[CQ:xml,data=%s]" % raw,
        ):
            fake = _gateway_fake()
            result = _invoke_group(fake, message)
            assert (result.outcome, result.gate) == (
                "rejected",
                "content_review_blocked",
            ), message
            assert fake.transport.calls == [], message


def test_content_review_xml_malformed_cdata_charref_benign_passes():
    # 良性孪生：畸形 XML 中 CDATA 包裹与字符引用的良性文
    # 本，解码送审后恰一次派发。
    encoded = "".join("&#%d;" % ord(char) for char in "大家好，今天开会")
    for raw in (
        "<msg><title><![CDATA[大家好，今天开会]]>",
        "<msg><title>%s" % encoded,
    ):
        fake = _gateway_fake()
        result = _invoke_group(
            fake, {"type": "xml", "data": {"data": raw}}
        )
        assert result.outcome == "ok", raw
        assert len(fake.transport.calls) == 1, raw


def test_content_review_padded_type_and_keys_blocked():
    # 填充类型/键（复审 P3）：提取分发归一只 lower 不 strip
    # 时，'json ' 类型、' Text ' 键与 typed 分支永不相等、
    # 整段提取为空绕审；归一统一 strip().lower() 后同口径
    # 受审。json/xml/share/node 至少各一形，外加 markdown
    # 填充键、text 填充键与填充 type 键本身。
    for message in (
        [{"type": "json ", "data": {"data": '{"text":"%s"}' % BANNED}}],
        [
            {
                "type": " xml",
                "data": {"data": "<msg><title>%s</title></msg>" % BANNED},
            }
        ],
        [{"type": "text", "data": {" Text ": BANNED}}],
        [{"type": "markdown", "data": {" Content ": BANNED}}],
        [
            {
                "type": "share",
                "data": {" Title ": BANNED, "url": "https://example.com"},
            }
        ],
        [
            {
                "type": "node ",
                "data": {
                    "name": "小助手",
                    "content": [{"type": "text", "data": {"text": BANNED}}],
                },
            }
        ],
        [{" Type ": "text", "data": {"text": BANNED}}],
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_padded_type_and_keys_benign_passes():
    # 良性孪生：填充类型/键的良性载荷同口径提取、恰一次
    # 派发（归一不得过度拦截）。
    for message in (
        [{"type": "json ", "data": {"data": '{"text":"大家好"}'}}],
        [{"type": "text", "data": {" Text ": "大家好"}}],
        [
            {
                "type": "share",
                "data": {" Title ": "大家好", "url": "https://example.com"},
            }
        ],
        [{" Type ": "text", "data": {"text": "大家好"}}],
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_content_review_nested_list_segments_blocked():
    # 嵌套列表（复审 P3-3）：段列表再包一层列表此前被静默
    # 跳过；顶层、node 子内容、forward 子内容三路同口径受审。
    seg = {"type": "text", "data": {"text": BANNED}}
    for message in (
        [[seg]],
        {"type": "node", "data": {"content": [[seg]]}},
        {"type": "forward", "data": {"content": [[seg]]}},
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_nested_list_benign_passes():
    seg = {"type": "text", "data": {"text": "大家好，今天开会"}}
    for message in (
        [[seg]],
        {"type": "node", "data": {"content": [[seg]]}},
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_resource_refs_key_and_cq_variants_rejected():
    # 资源引用匹配归一（复审 P3-4）：dict 键大小写/空白变体
    # 与 CQ file= 的大小写/等号空白变体此前整段绕过校验，
    # 裸绝对路径必须与正形同口径 resource_rejected。
    for message in (
        {"type": "image", "data": {"File": "/etc/passwd"}},
        {"type": "image", "data": {"FILE": "/etc/passwd"}},
        {"type": "image", "data": {" file ": "/etc/passwd"}},
        "[CQ:image,File=/etc/passwd]",
        "[CQ:image,file =/etc/passwd]",
        "[CQ:image,file= /etc/passwd]",
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "resource_rejected",
        ), message
        assert fake.transport.calls == [], message


def test_resource_refs_normalized_benign_passes():
    # 良性对照：归一形态的合法引用（根内相对名 / http 链接）
    # 不得被归一误伤，照常派发。
    for message in (
        {"type": "image", "data": {"File": "x.jpg"}},
        {"type": "image", "data": {"File": "https://example.com/x.jpg"}},
        "[CQ:image,File=x.jpg]",
        "[CQ:image,file =x.jpg]",
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_resource_refs_deep_nesting_rejected():
    # 资源检查结果级覆盖（复审 P3-6）：违规引用埋在百层嵌
    # 套深处，迭代遍历必须照样检出 resource_rejected——防迭
    # 代改写回归（seen 集误跳分支、栈丢叶）静默重开深嵌套绕过。
    value = {"file": "/etc/passwd"}
    for _ in range(120):
        value = {"k": value}
    fake = _gateway_fake()
    message = {"type": "json", "data": {"data": {"wrap": value}}}
    result = _invoke_group(fake, message)
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    assert fake.transport.calls == []


def test_resource_refs_deep_nesting_benign_passes():
    value = {"file": "x.jpg"}
    for _ in range(120):
        value = {"k": value}
    fake = _gateway_fake()
    message = {"type": "json", "data": {"data": {"wrap": value}}}
    result = _invoke_group(fake, message)
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def test_cyclic_payload_invoke_returns_result():
    # 环结构（复审 P3-6）：自引用 list 经 invoke 必须返回
    # ActionResult（深度界把环收敛为判不可审拦截），不挂死
    # （文件级 timeout=10 兜底）、不抛 RecursionError；函数级
    # 的资源检查与字符串收集对环同样终止。
    from companion.adapters.gateway import _check_resource_refs, _payload_all_strings

    cyclic = []
    cyclic.append(cyclic)
    assert _check_resource_refs(cyclic, ["/tmp/allowed"]) is True
    cyclic_dict = {}
    cyclic_dict["self"] = cyclic_dict
    assert _payload_all_strings(cyclic_dict) == []
    fake = _gateway_fake()
    result = _invoke_group(fake, cyclic)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_depth_boundary_pair():
    # 深度恰界（复审 P4-2）：64 层 node 嵌套在界内——良性派
    # 发、违禁受审拦截（证明界内真提取）；65 层在界外——良
    # 性也判不可审拦截。此前只钉 400/1500 极值，深度常量被
    # 改小（如 64→16）全套测试无信号。
    benign = {"type": "text", "data": {"text": "大家好，今天开会"}}
    banned = {"type": "text", "data": {"text": BANNED}}
    fake = _gateway_fake()
    result = _invoke_group(fake, _nested_node_payload(64, benign))
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1
    fake = _gateway_fake()
    result = _invoke_group(fake, _nested_node_payload(64, banned))
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []
    fake = _gateway_fake()
    result = _invoke_group(fake, _nested_node_payload(65, benign))
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_json_loads_recursion_error_translated_to_unreviewable(monkeypatch):
    # json 解析爆栈翻译分支（复审 P4-2）：json 段的 json.loads
    # 抛 RecursionError 时必须翻译为 _PayloadUnreviewable（由
    # 网关转拦截），不许穿出。用打桩确定性触发该分支（C 扫
    # 描器的栈深不随 Python 递归上限走，构造超深串不可靠）。
    from types import SimpleNamespace

    from companion import content_extract as extract_module
    from companion.adapters.gateway import (
        _payload_visible_texts,
        _PayloadUnreviewable,
    )

    def _raising_loads(_text, **_kwargs):
        # 桩签名与真实 json.loads 对齐：审查解析现带
        # object_pairs_hook（重复键保对解析），桩须容忍该
        # 关键字参数、仍只模拟解析器爆栈这一行为。
        raise RecursionError("parser stack exhausted")

    # 打桩目标随提取层迁至共享模块 companion.content_extract
    # （解析点已不在 gateway 本模块）；钉的行为断言不变。
    monkeypatch.setattr(
        extract_module,
        "json",
        SimpleNamespace(loads=_raising_loads, dumps=json.dumps),
    )
    with pytest.raises(_PayloadUnreviewable):
        _payload_visible_texts([{"type": "json", "data": {"data": "{}"}}])


def test_content_review_bare_string_data_blocked():
    # 裸串 data（复审 P4-1）：text/json 段 data 为裸字符串此
    # 前静默提取为空、整段绕审；现判不可审拦截——良性裸串
    # 同口径（拦的是无从提取的形态本身，不是内容）。
    for message in (
        [{"type": "text", "data": BANNED}],
        [{"type": "json", "data": BANNED}],
        [{"type": "text", "data": "大家好，今天开会"}],
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_handle_request_complete_failure_logs_request_key(tmp_path, caplog):
    # complete 留痕补请求 key（复审 P4-3）：False 与抛异常两
    # 条 warning 都必须带请求定位摘要（键分量 + flag 的
    # sha256 摘要），且绝不带 flag 原文。
    import hashlib
    import logging

    for store_wrapper, db_name in (
        (_FalseCompleteStore, "false-complete-key.db"),
        (_RaisingCompleteStore, "raising-complete-key.db"),
    ):
        caplog.clear()
        real = SqliteRequestStore(Database(tmp_path / db_name))
        event = _friend_request_event("flag-key-trace")
        key = request_key(
            event.self_id, event.request_type, event.sub_type, event.flag
        )
        real.try_begin(key, user_id=event.user_id, group_id=event.group_id)
        with caplog.at_level(logging.WARNING, logger="companion.ingress.events"):
            run(
                handle_request(
                    _Proc(),
                    event,
                    store=store_wrapper(real),
                    channel=None,
                    policy=RequestPolicy(
                        mode="manual",
                        friend_mode="approve_all",
                        group_mode="manual",
                    ),
                )
            )
        expected_digest = hashlib.sha256(b"flag-key-trace").hexdigest()[:16]
        assert "request complete not applied" in caplog.text
        assert "%s/friend/" % SELF_ID in caplog.text
        assert "flag_sha256=%s" % expected_digest in caplog.text
        assert "flag-key-trace" not in caplog.text
        assert real.get(key).status == "processing"


# ---------------------------------------------------------------------------
# 复审 r7 收口轮（a–f）：CQ 单一文法 / 回落剥标签 / 字段形态等价类 /
# 资源键穿透 / 代次下限推导 / critical 合取项反例
# ---------------------------------------------------------------------------


def test_content_review_malformed_cq_brackets_blocked():
    # CQ 文法统一（复审 P2）：剥离与解析共用同一文法后，未被
    # 解析器消费的畸形 CQ 括号（连字符类型、逗号前空格）整段
    # 留在送审余文里按明文受审，不再在两步之间消失。
    for message in (
        "[CQ:mini-app,data=%s]" % BANNED,
        '[CQ:json ,data={"desc":"%s"}]' % BANNED,
        "前缀 [CQ:mini-app,data=%s] 后缀" % BANNED,
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_malformed_cq_benign_passes():
    # 良性对照：畸形括号按明文送审，内容良性则照常派发。
    for message in (
        "[CQ:mini-app,data=大家好]",
        '[CQ:json ,data={"desc":"大家好"}]',
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_content_review_cq_card_prefix_params_blocked():
    # CQ 卡片前缀参数并集（复审 P3）：json/xml 特例里 data=
    # 之前的参数（brief/title，含大小写变体与同名重复）与本
    # 体同口径送审，不再被特例的提前返回整段丢弃。
    for message in (
        "[CQ:xml,brief=%s,data=<msg><title>hi</title></msg>]" % BANNED,
        '[CQ:json,title=%s,data={"desc":"hi"}]' % BANNED,
        '[CQ:json,Title=%s,data={"desc":"hi"}]' % BANNED,
        "[CQ:xml,brief=ok,brief=%s,data=<msg><title>hi</title></msg>]" % BANNED,
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_cq_card_prefix_params_benign_passes():
    for message in (
        "[CQ:xml,brief=摘要,data=<msg><title>hi</title></msg>]",
        '[CQ:json,title=标题,data={"desc":"hi"}]',
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_content_review_xml_malformed_tag_split_blocked():
    # 畸形 XML 回落剥标签（复审 P3）：多根 XML 被标签隔断的
    # 拆字短语在回落送审时恢复连续，与良构路径同口径受审。
    # 回落产出为并集（复审 P2）：剥标签流恢复跨标签连续性，
    # 原文流保属性值连续性，两流替代关系送审——函数级口径
    # 由单流改为双流，本断言随契约更新（网关级拦截断言不变）。
    from companion.adapters.gateway import _xml_visible_texts

    assert _xml_visible_texts("<t>自</t><t>杀</t>") == [
        "自杀",
        "<t>自</t><t>杀</t>",
    ]
    for message in (
        [{"type": "xml", "data": {"data": "<t>自</t><t>杀</t>"}}],
        "[CQ:xml,data=<t>自</t><t>杀</t>]",
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_xml_malformed_tag_split_benign_passes():
    fake = _gateway_fake()
    result = _invoke_group(
        fake, [{"type": "xml", "data": {"data": "<t>你</t><t>好</t>"}}]
    )
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def test_content_review_list_valued_fields_blocked():
    # 字段形态等价类（复审 P3）：typed 段的可见字段为 list
    # 值时走全量收集，不再因 isinstance(str) 过滤静默丢弃。
    for message in (
        [{"type": "text", "data": {"text": [BANNED]}}],
        [{"type": "markdown", "data": {"content": [BANNED]}}],
        [
            {
                "type": "node",
                "data": {"name": [BANNED], "content": []},
            }
        ],
        [
            {
                "type": "xml",
                "data": {"data": ["<msg><title>%s</title></msg>" % BANNED]},
            }
        ],
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_list_valued_fields_benign_passes():
    for message in (
        [{"type": "text", "data": {"text": ["大家好"]}}],
        [{"type": "markdown", "data": {"content": ["大家好"]}}],
        [{"type": "xml", "data": {"data": ["<msg><title>hi</title></msg>"]}}],
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_content_review_non_dict_data_blocked():
    # data 为 list/标量（复审 P4）：与裸串同口径判不可审——
    # 良性形态同拦（拦的是无从按类型路由提取的形态本身）。
    for message in (
        [{"type": "text", "data": [BANNED]}],
        [{"type": "text", "data": ["大家好"]}],
        [{"type": "text", "data": 123}],
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_fallback_rendered_fields_blocked():
    # 回退键集审计补漏（复审 P3）：标准段的渲染文本字段逐段
    # 核对后补入 singer（music）/ address（location）/
    # description（share 等卡片）/ filename（onlinefile 的
    # fileName），藏词于这些字段与 title 同口径受审。
    for message in (
        [
            {
                "type": "location",
                "data": {"lat": 1, "lon": 2, "address": BANNED, "title": "x"},
            }
        ],
        [
            {
                "type": "music",
                "data": {"type": "custom", "singer": BANNED, "title": "x"},
            }
        ],
        [
            {
                "type": "share",
                "data": {"url": "https://example.com", "description": BANNED},
            }
        ],
        [{"type": "onlinefile", "data": {"fileName": BANNED}}],
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_fallback_rendered_fields_benign_passes():
    # 良性孪生（复审 P4 补齐）：与 blocked 侧四形态一一对应
    # ——location/address、music/singer、share/description、
    # onlinefile/fileName，防某一形态被过度拦截而无测变红。
    for message in (
        [
            {
                "type": "location",
                "data": {"lat": 1, "lon": 2, "address": "中山路 1 号", "title": "x"},
            }
        ],
        [
            {
                "type": "music",
                "data": {"type": "custom", "singer": "歌手", "title": "x"},
            }
        ],
        [
            {
                "type": "share",
                "data": {"url": "https://example.com", "description": "日常分享"},
            }
        ],
        [{"type": "onlinefile", "data": {"fileName": "报告.pdf"}}],
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_resource_refs_nested_value_rejected():
    # 资源键上下文穿透（复审 P3）：资源键的值再套一层容器，
    # 后代字符串仍逐个过资源校验，与直形同判 resource_rejected。
    for message in (
        {"type": "image", "data": {"file": [["/etc/passwd"]]}},
        {"type": "image", "data": {"file": {"n": "/etc/passwd"}}},
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "resource_rejected",
        ), message
        assert fake.transport.calls == [], message


def test_resource_refs_nested_value_benign_passes(tmp_path):
    # 良性对照：嵌套的根内绝对路径 / 相对名 / https 链接不被
    # 穿透校验误伤。
    from companion.adapters.gateway import _check_resource_refs

    inner = str(tmp_path / "a.jpg")
    assert _check_resource_refs({"file": [[inner]]}, (str(tmp_path),)) is True
    assert _check_resource_refs({"file": {"n": "x.jpg"}}, ()) is True
    assert (
        _check_resource_refs(
            {"file": {"u": "https://example.com/a.png"}}, ()
        )
        is True
    )
    fake = _gateway_fake()
    result = _invoke_group(
        fake, {"type": "image", "data": {"file": [["x.jpg"]]}}
    )
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def test_corrupt_request_generation_sweeps_high_generation_rows(
    tmp_path, monkeypatch
):
    # request_generation 损坏的代次下限推导（复审 P3）：法官
    # 端到端序列——5 个 boot 后 gen5 留一行 processing，损坏
    # request_generation 并大幅推进时钟，新 boot 构造后该行
    # 必须收敛 ambiguous（旧行为：计数器重置为 1、高代次行
    # 落在一切清扫区间之外永久滞留）。新代次须按数据推导
    # （>5），不是从 1 重启。
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731
    db = Database(tmp_path / "memory.db")
    key = ("1000", "friend", "", "flag-gen5-stranded")
    store = None
    for boot in ("boot-1", "boot-2", "boot-3", "boot-4", "boot-5"):
        monkeypatch.setattr(
            "companion.memory.requests._PROCESS_BOOT_ID", boot
        )
        store = SqliteRequestStore(db, clock=clock)
    assert store.generation == 5
    store.try_begin(key)
    assert store.claim_processing(key) is True
    with db.transaction() as conn:
        conn.execute(
            "UPDATE schema_meta SET value='garbage'"
            " WHERE key='request_generation'"
        )
    now[0] += 10000.0
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-6")
    store6 = SqliteRequestStore(db, clock=clock)
    assert store6.generation == 6
    assert store6.get(key).status == "ambiguous"
    store6.list_pending()
    assert store6.get(key).status == "ambiguous"
    begun = store6.try_begin(("1000", "friend", "", "flag-after-repair"))
    assert begun.created is True


def test_critical_non_quota_policy_key_downgraded(monkeypatch):
    # critical 派生 policy_key 合取项反例（复审 P3）：system
    # 主体 + canonical send_private_msg + 自报 critical，但
    # effective key 落在额度别名集之外时不得派生 critical。
    # 现行 allowed_policy_keys 与 QUOTA_POLICY_KEYS 恰好同集，
    # 此形经公开 invoke 不可达——本钉以测试内放宽 spec 的
    # allowed 集构造该矩阵角：断言限流照常生效（limit=1 第
    # 二条被限）、只派发一条、critical 预留池纹丝不动。删去
    # 派生式中的 policy_key 合取项，本钉必须变红（critical
    # 豁免限流、第二条会派发出去）。
    from companion.adapters import gateway as gateway_module

    spec = gateway_module.ACTION_SPECS["send_private_msg"]
    widened = replace(
        spec,
        allowed_policy_keys=spec.allowed_policy_keys | {"ops_notice"},
    )
    monkeypatch.setitem(
        gateway_module.ACTION_SPECS, "send_private_msg", widened
    )
    transport = FakeTransport()
    gateway = ActionGateway(
        transport,
        policies={
            "ops_notice": {"enabled": True, "dry_run": False, "rate_limit": 1},
        },
        quota=SendQuota(),
    )
    kwargs = dict(
        principal=PRINCIPAL_SYSTEM,
        identity=USER,
        priority="critical",
        policy_key="ops_notice",
    )
    first = run(
        gateway.invoke(
            "send_private_msg",
            {"user_id": USER, "message": "固定话术"},
            **kwargs,
        )
    )
    assert first.outcome == "ok"
    second = run(
        gateway.invoke(
            "send_private_msg",
            {"user_id": USER, "message": "固定话术"},
            **kwargs,
        )
    )
    assert (second.outcome, second.gate) == ("rejected", "rate_limited")
    assert len(transport.calls) == 1
    snapshot = gateway.quota.snapshot()
    assert snapshot["critical_remaining"] == 10
    assert snapshot["normal_remaining"] == 60


# ---------------------------------------------------------------------------
# 复审 r8（顺序/连续性收口）：提取按渲染顺序产出（XML 字符
# 数据流 + 文档序交错流 / str 余文与 CQ 段交错 / node 昵称
# 在前）、网关拼接整串送审兜底、未知类型回落递归、共享子
# 结构资源复验钉、代次下限 gen_boot 来源隔离钉
# ---------------------------------------------------------------------------


def test_content_review_xml_split_across_layout_items_blocked():
    # XML 顺序（复审 P2）：可见字符数据被结构属性（layout）
    # 隔断在两个 item 里时，旧提取把属性值插进字符片段之
    # 间，拼接审查拼不出连续短语、整段派发；字符数据流整
    # 体送审后与无属性对照同口径必拦。
    for data in (
        '<msg serviceID="1"><item layout="1"><title>自</title></item>'
        '<item layout="2"><title>杀</title></item></msg>',
        '<msg><item><title>自</title></item>'
        "<item><title>杀</title></item></msg>",
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, [{"type": "xml", "data": {"data": data}}])
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), data
        assert fake.transport.calls == [], data


def test_content_review_xml_text_attribute_seams_blocked():
    # 跨「文本↔属性」接缝（复审 P2）：短语前半在属性、后半
    # 在正文，以及前半在正文、后半在后继元素属性——文档序
    # 交错流里两种拆分都恢复连续，双向同口径必拦。
    for data in (
        '<msg brief="我想自"><item><title>杀</title></item></msg>',
        '<msg><item><title>我想自</title></item>'
        '<item brief="杀"></item></msg>',
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, [{"type": "xml", "data": {"data": data}}])
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), data
        assert fake.transport.calls == [], data


def test_content_review_xml_layout_items_benign_passes():
    # 良性孪生：带结构属性的卡片与跨接缝的良性拆分不得被
    # 双流送审误拦，恰一次派发。
    for data in (
        '<msg serviceID="1"><item layout="1"><title>今日</title></item>'
        '<item layout="2"><title>分享</title></item></msg>',
        '<msg brief="今日分"><item><title>享</title></item></msg>',
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, [{"type": "xml", "data": {"data": data}}])
        assert result.outcome == "ok", data
        assert len(fake.transport.calls) == 1, data


def test_content_review_string_cq_order_splits_blocked():
    # str 顺序（复审 P2）：卡片在前、明文在后与明文/CQ 交错
    # 的拆分曾被「余文全排前面」反序拼装逃审；按原始位置交
    # 错产出后与明文在前对照同口径必拦。
    for message in (
        '[CQ:json,data={"desc":"我想自"}]杀',
        '我想[CQ:json,data={"desc":"自"}]杀',
        '我想自[CQ:json,data={"desc":"杀"}]',
        '[CQ:share,url=https://example.com,title=我想自]杀',
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_string_cq_order_benign_passes():
    # 良性孪生：交错形态的良性明文/卡片组合照常派发，恰
    # 一次——顺序改写不得误伤正常交错载荷。
    for message in (
        "大家好[CQ:at,qq=90001]欢迎",
        "[CQ:image,file=x.jpg]大家好，今天开会",
        '你好[CQ:json,data={"desc":"世界"}]欢迎',
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_content_review_joined_stream_keeps_benign_dispatch():
    # 拼接整串送审（结构兜底）的良性口径钉：多片段良性载
    # 荷在逐片段 + 整串双重筛查下照常放行、恰一次派发——
    # 兜底只增审查口径、不改派发路径。
    fake = _gateway_fake()
    result = _invoke_group(
        fake,
        [
            {"type": "text", "data": {"text": "大家好"}},
            {"type": "text", "data": {"text": "今天开会"}},
        ],
    )
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def test_content_review_node_nickname_content_seam_blocked():
    # node 顺序（复审 P3）：昵称在渲染流里位于内容之前，
    # 短语前半在昵称、后半在首个内容段时必须拦——旧实现先
    # 内容后昵称，反序拼接整段逃审。
    fake = _gateway_fake()
    message = [
        {
            "type": "node",
            "data": {
                "nickname": "我想自",
                "content": [{"type": "text", "data": {"text": "杀"}}],
            },
        }
    ]
    result = _invoke_group(fake, message)
    assert (result.outcome, result.gate) == (
        "rejected",
        "content_review_blocked",
    )
    assert fake.transport.calls == []


def test_content_review_node_reverse_split_dispatches():
    # 顺序口径三变后的定稿（收口复审 P3 纠正）：本钉 r8 原
    # 断言「派发」——昵称在渲染流里位于内容之前，本放法的
    # 拼接流是「杀|我想自」，拼不出连续短语，按其自身渲染
    # 序判定即不构成绕过。收口轮曾改为双序并集（逆序拼接
    # 亦送审）并把本钉翻转为「拦截」，随即被复现证明双序
    # 会误拦顺序确定的良性回复（见 multipart 的收口复审
    # P3 良性对钉），父级裁决撤销双序：只按渲染顺序审一
    # 次，顺序对齐的责任在提取层。本钉随之恢复「派发」，
    # 与同文件 nickname seam 钉（昵称在前拼出短语必拦）
    # 成对——两种放法各自按自身渲染序判定。
    fake = _gateway_fake()
    message = [
        {
            "type": "node",
            "data": {
                "nickname": "杀",
                "content": [{"type": "text", "data": {"text": "我想自"}}],
            },
        }
    ]
    result = _invoke_group(fake, message)
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def test_content_review_fallback_nested_visible_keys_blocked():
    # 回落递归（复审 P3）：未知类型段的可见字段嵌在普通容
    # 器键之下、无类型 dict 及其列表元素同形态，曾因回落
    # 只看顶层一层整段逃审；递归后与平铺形态同口径必拦。
    for message in (
        [{"type": "share", "data": {"extra": {"title": BANNED}}}],
        [{"type": "share", "data": {"a": {"b": {"c": {"title": BANNED}}}}}],
        [{"type": "share", "data": {"extra": [{"title": BANNED}]}}],
        {"extra": {"title": BANNED}},
        [{"extra": {"title": BANNED}}],
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        ), message
        assert fake.transport.calls == [], message


def test_content_review_fallback_nested_benign_passes():
    for message in (
        [{"type": "share", "data": {"extra": {"title": "日常分享"}}}],
        {"extra": {"title": "日常分享"}},
        [{"extra": [{"title": "日常分享"}]}],
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_resource_refs_shared_substructure_revalidated():
    # 共享子结构复验（复审 P3）：同一容器对象先在普通键下
    # 出现、再在资源键下出现时，资源口径必须重验。探针定
    # 性（本轮第一手复现）：现行环保护按（容器 id, 是否资
    # 源上下文）记账，普通上下文验过不免除资源上下文重验
    # ——双向均 False、结论不受共享影响，故本条只补结果级
    # 钉子、不动实现。若将来环保护退化为纯 id 集，note 先
    # 行的一侧会静默放行、本钉必须变红。
    from companion.adapters.gateway import _check_resource_refs

    shared = ["/etc/passwd"]
    assert (
        _check_resource_refs({"note": shared, "file": shared}, ["/tmp/allowed"])
        is False
    )
    assert (
        _check_resource_refs({"file": shared, "note": shared}, ["/tmp/allowed"])
        is False
    )
    shared_dict = {"n": "/etc/passwd"}
    assert (
        _check_resource_refs(
            {"note": shared_dict, "file": shared_dict}, ["/tmp/allowed"]
        )
        is False
    )
    assert (
        _check_resource_refs(
            {"file": shared_dict, "note": shared_dict}, ["/tmp/allowed"]
        )
        is False
    )
    benign = ["x.jpg"]
    assert _check_resource_refs({"note": benign, "file": benign}, ()) is True
    assert _check_resource_refs({"file": benign, "note": benign}, ()) is True
    cyclic = ["/etc/passwd"]
    cyclic.append(cyclic)
    assert (
        _check_resource_refs({"note": cyclic, "file": cyclic}, ["/tmp/allowed"])
        is False
    )


def test_resource_refs_shared_substructure_invoke_rejected():
    # 网关级同口径：共享子结构经 invoke 也必须
    # resource_rejected，良性共享恰一次派发。
    shared = ["/etc/passwd"]
    fake = _gateway_fake()
    result = _invoke_group(
        fake, [{"type": "image", "data": {"note": shared, "file": shared}}]
    )
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    assert fake.transport.calls == []
    benign = ["x.jpg"]
    fake = _gateway_fake()
    result = _invoke_group(
        fake, [{"type": "image", "data": {"note": benign, "file": benign}}]
    )
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def test_resource_refs_cq_duplicate_file_param_rejected():
    # 同一 CQ 码内重复 file=（集成复审 P2）：旧提取正则把每
    # 次匹配锚定在字面 [CQ: 前缀上、findall 非重叠消费，段内
    # 第二个 file= 永不提取——良性值在前、恶意值在后即整段
    # 逃逸，连弱校验都绕过。改按码段切分、段内逐个出现提取
    # 后，双序与大小写/等号空白变体重复必须与 dict 正形同判
    # resource_rejected。
    for message in (
        "[CQ:image,file=x.jpg,file=/etc/passwd]",
        "[CQ:image,file=/etc/passwd,file=x.jpg]",
        "[CQ:image,file=x.jpg,FILE=/etc/passwd]",
        "[CQ:image,file=x.jpg,file = /etc/passwd]",
        "前缀[CQ:image,file=x.jpg,file=/etc/passwd]后缀",
        "[CQ:image,file=x.jpg][CQ:record,file=a.amr,file=/etc/passwd]",
        {"type": "image", "data": {"file": "/etc/passwd"}},
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "resource_rejected",
        ), message
        assert fake.transport.calls == [], message


def test_resource_refs_cq_duplicate_file_param_benign_passes():
    # 良性孪生：同码重复出现的合法引用逐个校验后照常派发，
    # 多参数良性组合不被逐个提取误伤。
    for message in (
        "[CQ:image,file=x.jpg,file=y.jpg]",
        "[CQ:video,file=v.mp4,thumb=t.jpg]",
        "[CQ:video,thumb=t.jpg,file=v.mp4]",
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_resource_refs_video_thumb_rejected():
    # video 段 thumb 字段（集成复审 P3）：资源键集只有
    # thumb_path 没有 thumb、CQ 提取只有 file=，而仓库自身
    # 序列化器会发出 [CQ:video,file=…,thumb=…]——dict 与 CQ
    # 双形的裸绝对路径 thumb 都整段脱检，必须同判
    # resource_rejected。
    for message in (
        {"type": "video", "data": {"file": "x.mp4", "thumb": "/etc/passwd"}},
        {"type": "video", "data": {"thumb": "/etc/passwd"}},
        "[CQ:video,file=x.mp4,thumb=/etc/passwd]",
        "[CQ:video,thumb=/etc/passwd,file=x.mp4]",
        "[CQ:video,file=x.mp4,Thumb=/etc/passwd]",
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "resource_rejected",
        ), message
        assert fake.transport.calls == [], message


def test_resource_refs_video_thumb_benign_passes(tmp_path):
    # 良性孪生：不透明 id / 根内相对 / https 封面照常放行
    # （函数级带 roots/base 与 invoke 级各一份）。
    from companion.adapters.gateway import _check_resource_refs

    roots = (str(tmp_path),)
    assert _check_resource_refs({"thumb": "t.jpg"}, ()) is True
    assert (
        _check_resource_refs({"thumb": "docs/a.jpg"}, roots, str(tmp_path))
        is True
    )
    assert _check_resource_refs({"thumb": "https://example.com/t.jpg"}, ()) is True
    for message in (
        {"type": "video", "data": {"file": "x.mp4", "thumb": "t.jpg"}},
        "[CQ:video,file=x.mp4,thumb=t.jpg]",
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_resource_refs_segment_field_audit_rejected():
    # 段资源字段审计补漏（集成复审 P3 顺带）：music 自定义段
    # 的 audio 字段（dict 键集缺席）与 share/image/music 段在
    # CQ 形态下的 url/image 参数（CQ 提取缺席），裸绝对路径
    # 此前双双脱检；补齐后与 file/thumb 同口径
    # resource_rejected。
    for message in (
        {"type": "music", "data": {"audio": "/etc/passwd"}},
        "[CQ:music,type=custom,url=https://example.com,audio=/etc/passwd]",
        "[CQ:share,url=/etc/passwd,title=t]",
        "[CQ:image,file=x.jpg,url=/etc/passwd]",
        "[CQ:share,url=https://example.com,image=/etc/passwd]",
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "resource_rejected",
        ), message
        assert fake.transport.calls == [], message


def test_resource_refs_segment_field_audit_benign_passes():
    # 良性孪生：审计补漏不得误伤 https 资源与平台 id 形态。
    # CQ image=/audio= 良性孪生（复审 P4）：两参数此前只在
    # 拒绝钉里出现，补 CQ 形态封面相对名与音频相对名放行，
    # 与拒绝钉成对。
    for message in (
        {"type": "music", "data": {"audio": "https://example.com/a.mp3"}},
        "[CQ:share,url=https://example.com,title=t]",
        "[CQ:music,type=qq,id=123]",
        "[CQ:share,url=https://example.com,image=cover.jpg]",
        "[CQ:music,type=custom,url=https://example.com,audio=a.mp3]",
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_resource_refs_relative_forms_rejected_in_new_contexts(tmp_path):
    # 合并强校验的相对形态钉（集成复审 P3）：归一键 / 嵌套
    # dict / ≥2 跳 list / 120 层深 / 共享子结构五类合并新上
    # 下文下，相对穿越必须与绝对形态同判 False。此前资源拒
    # 绝钉全用 /etc/passwd 绝对形态——弱校验对绝对路径同样
    # 拒绝、对相对路径却原样放行，唯相对形态能区分强弱：
    # 任一上下文的资源叶子回退 validate_resource_ref（或在
    # 该深度丢 base_dir 贯穿），本钉必须变红。
    from companion.adapters.gateway import _check_resource_refs

    roots = (str(tmp_path),)
    base = str(tmp_path)
    for bad in ("../secret.txt", "sub/../../secret.txt"):
        assert _check_resource_refs({"File": bad}, roots, base) is False
        assert _check_resource_refs({"file": {"n": bad}}, roots, base) is False
        assert _check_resource_refs({"file": [[[bad]]]}, roots, base) is False
        shared = [bad]
        assert (
            _check_resource_refs({"note": shared, "file": shared}, roots, base)
            is False
        )
        assert (
            _check_resource_refs({"file": shared, "note": shared}, roots, base)
            is False
        )
        deep = {"file": bad}
        for _ in range(120):
            deep = {"k": deep}
        assert _check_resource_refs(deep, roots, base) is False


def test_resource_refs_relative_forms_benign_twins_pass(tmp_path):
    # 良性孪生：带分隔符的根内相对值在五类上下文同形态放
    # 行——强校验只拒穿越，不得误伤根内相对引用。
    from companion.adapters.gateway import _check_resource_refs

    roots = (str(tmp_path),)
    base = str(tmp_path)
    good = "docs/a.jpg"
    assert _check_resource_refs({"File": good}, roots, base) is True
    assert _check_resource_refs({"file": {"n": good}}, roots, base) is True
    assert _check_resource_refs({"file": [[[good]]]}, roots, base) is True
    shared = [good]
    assert (
        _check_resource_refs({"note": shared, "file": shared}, roots, base)
        is True
    )
    assert (
        _check_resource_refs({"file": shared, "note": shared}, roots, base)
        is True
    )
    deep = {"file": good}
    for _ in range(120):
        deep = {"k": deep}
    assert _check_resource_refs(deep, roots, base) is True


def test_resource_refs_relative_forms_invoke_rejected(tmp_path):
    # invoke 级同口径：网关注入 resource_roots/resource_base_dir
    # 后，五类上下文的相对穿越经真 invoke 必须
    # resource_rejected、零派发；根内相对良性孪生恰一次派发。
    def configured_fake():
        fake = _gateway_fake()
        fake.action_gateway.resource_roots = (str(tmp_path),)
        fake.action_gateway.resource_base_dir = str(tmp_path)
        return fake

    shared = ["../secret.txt"]
    deep = {"file": "../secret.txt"}
    for _ in range(120):
        deep = {"k": deep}
    for message in (
        {"type": "image", "data": {"File": "../secret.txt"}},
        {"type": "image", "data": {"File": "sub/../../secret.txt"}},
        {"type": "image", "data": {"file": {"n": "../secret.txt"}}},
        {"type": "image", "data": {"file": [[["../secret.txt"]]]}},
        {"type": "image", "data": {"note": shared, "file": shared}},
        {"type": "json", "data": {"data": {"wrap": deep}}},
    ):
        fake = configured_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "resource_rejected",
        ), message
        assert fake.transport.calls == [], message
    fake = configured_fake()
    result = _invoke_group(fake, {"type": "image", "data": {"file": "docs/a.jpg"}})
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


#: CQ 资源参数的自然宿主码形（提取与码型无关，取各参数在
#: 平台形态中最常见的宿主段构造矩阵）。
_CQ_PARAM_HOSTS = {
    "file": "[CQ:image,file={v}]",
    "thumb": "[CQ:video,file=v.mp4,thumb={v}]",
    "url": "[CQ:share,url={v},title=t]",
    "image": "[CQ:share,url=https://example.com,image={v}]",
    "audio": "[CQ:music,type=custom,url=https://example.com,audio={v}]",
}


def test_resource_refs_cq_relative_traversal_parity_rejected(tmp_path):
    # CQ 资源参数相对穿越与 dict 同值同判（平价轮裁决）：
    # CQ 候选此前只过弱校验 validate_resource_ref——相对串
    # 按不透明值原样放行，而 dict 同值过路径语义分类被拒
    # （file= 形态即已接受残余 R-S2-1，其记载的既定修法正
    # 是 CQ 候选改走路径语义门）。改走
    # validate_path_semantic_ref 后，资源参数集五成员 × 两
    # 穿越形态的 CQ/dict 双形必须同判 False；任一参数的
    # CQ 候选回退弱校验，本钉立即变红（弱校验对相对串原
    # 样放行，dict 侧不受影响、双形判定当场分叉）。
    from companion.adapters.gateway import _check_resource_refs

    roots = (str(tmp_path),)
    base = str(tmp_path)
    for param, template in _CQ_PARAM_HOSTS.items():
        for bad in ("../../etc/passwd", "sub/../../secret.txt"):
            cq = template.format(v=bad)
            assert _check_resource_refs(cq, roots, base) is False, cq
            assert _check_resource_refs({param: bad}, roots, base) is False, (
                param,
                bad,
            )
    # 裁决复现原例：video 段 thumb 的 CQ 相对穿越与 dict
    # 同值同判（本轮动手前的缺口复现形态）。
    assert (
        _check_resource_refs(
            "[CQ:video,file=v.mp4,thumb=../../etc/passwd]", roots, base
        )
        is False
    )


def test_resource_refs_cq_parity_benign_twins_pass(tmp_path):
    # 良性孪生矩阵：五参数的根内相对 / 不透明 id / https
    # 三类良性值在 CQ 与 dict 双形同判放行。强校验平价只
    # 拒穿越：根内相对若丢 base_dir 贯穿会被语义门 fail
    # closed 误拒、不透明 id 与 URL 须由语义门既有分支承
    # 接——任一类误伤本钉变红。
    from companion.adapters.gateway import _check_resource_refs

    roots = (str(tmp_path),)
    base = str(tmp_path)
    benign = {
        "file": ("docs/a.jpg", "opaque-file-id", "https://example.com/a.jpg"),
        "thumb": ("docs/t.jpg", "opaque-thumb-id", "https://example.com/t.jpg"),
        "url": ("docs/a.jpg", "opaque-url-id", "https://example.com/a.jpg"),
        "image": ("docs/c.jpg", "opaque-image-id", "https://example.com/c.jpg"),
        "audio": ("docs/a.mp3", "opaque-audio-id", "https://example.com/a.mp3"),
    }
    for param, values in benign.items():
        for good in values:
            cq = _CQ_PARAM_HOSTS[param].format(v=good)
            assert _check_resource_refs(cq, roots, base) is True, cq
            assert _check_resource_refs({param: good}, roots, base) is True, (
                param,
                good,
            )


def test_resource_refs_cq_relative_traversal_invoke_parity(tmp_path):
    # invoke 级平价：网关配 roots/base 后，CQ 五参数相对
    # 穿越必须与 dict 同值同判 resource_rejected、零派发；
    # 良性孪生（根内相对 / 不透明 id / https）恰一次派发。
    def configured_fake():
        fake = _gateway_fake()
        fake.action_gateway.resource_roots = (str(tmp_path),)
        fake.action_gateway.resource_base_dir = str(tmp_path)
        return fake

    for message in (
        "[CQ:image,file=../../etc/passwd]",
        "[CQ:video,file=v.mp4,thumb=../../etc/passwd]",
        "[CQ:share,url=../../etc/passwd,title=t]",
        "[CQ:share,url=https://example.com,image=sub/../../secret.txt]",
        "[CQ:music,type=custom,url=https://example.com,audio=../../etc/passwd]",
        {"type": "image", "data": {"file": "../../etc/passwd"}},
    ):
        fake = configured_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "resource_rejected",
        ), message
        assert fake.transport.calls == [], message
    for message in (
        "[CQ:image,file=docs/a.jpg]",
        "[CQ:video,file=v.mp4,thumb=docs/t.jpg]",
        "[CQ:share,url=https://example.com,image=cover.jpg]",
        "[CQ:music,type=custom,url=https://example.com,audio=a.mp3]",
        "[CQ:music,type=custom,url=https://example.com,audio=docs/a.mp3]",
    ):
        fake = configured_fake()
        result = _invoke_group(fake, message)
        assert result.outcome == "ok", message
        assert len(fake.transport.calls) == 1, message


def test_resource_refs_cq_unclosed_span_rejected(tmp_path):
    # 未闭合 CQ 码段（复审 P3-2）：串尾无 ] 时码段跨度延
    # 伸到串尾（_CQ_SPAN_RE 的 (?:\]|$) 分支），段内第二
    # 个 file=/thumb= 的绝对与相对恶意值都必须受检拒绝。
    # 删去该串尾分支后未闭合段整段不被提取、本钉必须变
    # 红（变异自验记录在提交说明）。
    from companion.adapters.gateway import _check_resource_refs

    roots = (str(tmp_path),)
    base = str(tmp_path)
    assert (
        _check_resource_refs("[CQ:image,file=x.jpg,file=/etc/passwd", roots, base)
        is False
    )
    assert (
        _check_resource_refs("[CQ:video,file=x.mp4,thumb=/etc/passwd", roots, base)
        is False
    )
    assert (
        _check_resource_refs(
            "[CQ:image,file=x.jpg,file=../../etc/passwd", roots, base
        )
        is False
    )
    assert (
        _check_resource_refs(
            "[CQ:video,file=x.mp4,thumb=../../etc/passwd", roots, base
        )
        is False
    )
    for message in (
        "[CQ:image,file=x.jpg,file=/etc/passwd",
        "[CQ:video,file=x.mp4,thumb=/etc/passwd",
    ):
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        assert (result.outcome, result.gate) == (
            "rejected",
            "resource_rejected",
        ), message
        assert fake.transport.calls == [], message


def test_resource_refs_cq_unclosed_span_benign_passes():
    # 良性孪生：未闭合码段内的良性重复参数逐个校验后照
    # 常派发，串尾跨度不得误伤未闭合的良性码形。
    from companion.adapters.gateway import _check_resource_refs

    assert _check_resource_refs("[CQ:image,file=x.jpg,file=y.jpg", ()) is True
    fake = _gateway_fake()
    result = _invoke_group(fake, "[CQ:image,file=x.jpg,file=y.jpg")
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def test_generation_floor_from_gen_boot_keys_only(tmp_path, monkeypatch):
    # 代次下限来源隔离（复审 P4）：无任何处置行、只有
    # gen_boot 元数据能证明历史代次时，request_generation
    # 损坏后的新代次必须仍按下限起算——删掉
    # _generation_floor 的 gen_boot 键扫描，本钉的
    # generation==6 断言必须变红（会从 1 重启、高代次行滞
    # 留的重演路径）。处置行来源由
    # test_corrupt_request_generation_sweeps_high_generation_rows
    # 单独钉死，两来源各自隔离、缺一不可。
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731
    db = Database(tmp_path / "memory.db")
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-1")
    SqliteRequestStore(db, clock=clock)
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM request_dispositions"
        ).fetchone()
        assert rows[0] == 0
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta VALUES"
            " ('request_gen_boot:5', 'old-boot')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta VALUES"
            " ('request_boot_seen:old-boot', ?)",
            (repr(now[0]),),
        )
        conn.execute(
            "UPDATE schema_meta SET value='garbage'"
            " WHERE key='request_generation'"
        )
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-2")
    store2 = SqliteRequestStore(db, clock=clock)
    assert store2.generation == 6
    key = ("1000", "friend", "", "flag-genboot-floor")
    store2.try_begin(key)
    assert store2.claim_processing(key) is True
    now[0] += 10000.0
    monkeypatch.setattr("companion.memory.requests._PROCESS_BOOT_ID", "boot-3")
    store3 = SqliteRequestStore(db, clock=clock)
    assert store3.get(key).status == "ambiguous"


# ---------------------------------------------------------------------------
# 全线复审微修轮（基线 af5f83f）：typed 段可见键矩阵 + JSON 重复键保对解析
# ---------------------------------------------------------------------------
#
# 复审 P3×3 的等价类收口：typed 分支只取本分字段时，data 里其余
# 可见键整段逃审（node 的 desc/summary、forward 的 summary/title、
# text 段混入的 title、markdown 的 desc 等系统性同类）。收口方式
# 不是逐条钉实例，而是「typed 类型全集 × 可见键全集 × dict/CQ
# 双形」的参数化矩阵：任一格藏词必拦、任一格良性恰一次派发。
# 类型全集 = 网关 typed 分发六类（text/markdown/json/xml/node/
# forward）+ 回落分支代表 share；可见键全集在此独立钉死（不引
# 生产常量，防常量缩水时矩阵跟着缩水）。

_MATRIX_VISIBLE_KEYS = [
    "text", "content", "name", "nickname", "title", "prompt", "desc",
    "description", "summary", "app", "template", "uin",
    "address", "singer", "filename",
]
_MATRIX_BENIGN = "你好呀"
_MATRIX_BASE_DATA = {
    "text": {"text": "hi"},
    "markdown": {"content": "hi"},
    "json": {"data": '{"desc":"benign"}'},
    "xml": {"data": "<msg><title>benign</title></msg>"},
    "node": {
        "name": "n",
        "uin": "1",
        "content": [{"type": "text", "data": {"text": "hi"}}],
    },
    "forward": {
        "id": "5",
        "content": [{"type": "text", "data": {"text": "hi"}}],
    },
    "share": {"url": "https://example.com", "title": "t", "content": "c"},
}
_MATRIX_TYPES = sorted(_MATRIX_BASE_DATA)
# text 段在 CQ 串里即消息裸串、无参数形态，其 CQ 形由 dict 形
# 矩阵 + 裸串对照（test_content_review_list_form_blocked_control
# 等既有钉）覆盖；CQ 矩阵覆盖其余六类真实 CQ 码。
_MATRIX_CQ_TYPES = [t for t in _MATRIX_TYPES if t != "text"]


def _matrix_dict_segment(seg_type, key, value):
    data = dict(_MATRIX_BASE_DATA[seg_type])
    if key == "content" and seg_type in ("node", "forward"):
        data["content"] = [{"type": "text", "data": {"text": value}}]
    else:
        data[key] = value
    return {"type": seg_type, "data": data}


def _matrix_cq_message(seg_type, key, value):
    if seg_type == "json":
        # 卡片前缀参数形态：可见键在 data= 之前作同级键进段。
        return '[CQ:json,%s=%s,data={"desc":"benign"}]' % (key, value)
    if seg_type == "xml":
        return "[CQ:xml,%s=%s,data=<msg><title>benign</title></msg>]" % (
            key,
            value,
        )
    params = {
        "markdown": {"content": "hi"},
        "node": {"name": "n", "uin": "1", "content": "hi"},
        "forward": {"id": "5"},
        "share": {"url": "https://example.com", "title": "t", "content": "c"},
    }[seg_type]
    params[key] = value
    body = ",".join("%s=%s" % item for item in params.items())
    return "[CQ:%s,%s]" % (seg_type, body)


@pytest.mark.parametrize("seg_type", _MATRIX_TYPES)
@pytest.mark.parametrize("key", _MATRIX_VISIBLE_KEYS)
def test_content_review_matrix_dict_banned_blocked(seg_type, key):
    fake = _gateway_fake()
    result = _invoke_group(fake, [_matrix_dict_segment(seg_type, key, BANNED)])
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


@pytest.mark.parametrize("seg_type", _MATRIX_TYPES)
@pytest.mark.parametrize("key", _MATRIX_VISIBLE_KEYS)
def test_content_review_matrix_dict_benign_dispatches_once(seg_type, key):
    fake = _gateway_fake()
    result = _invoke_group(
        fake, [_matrix_dict_segment(seg_type, key, _MATRIX_BENIGN)]
    )
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


@pytest.mark.parametrize("seg_type", _MATRIX_CQ_TYPES)
@pytest.mark.parametrize("key", _MATRIX_VISIBLE_KEYS)
def test_content_review_matrix_cq_banned_blocked(seg_type, key):
    fake = _gateway_fake()
    result = _invoke_group(fake, _matrix_cq_message(seg_type, key, BANNED))
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


@pytest.mark.parametrize("seg_type", _MATRIX_CQ_TYPES)
@pytest.mark.parametrize("key", _MATRIX_VISIBLE_KEYS)
def test_content_review_matrix_cq_benign_dispatches_once(seg_type, key):
    fake = _gateway_fake()
    result = _invoke_group(
        fake, _matrix_cq_message(seg_type, key, _MATRIX_BENIGN)
    )
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def test_content_review_json_duplicate_key_first_value_blocked():
    fake = _gateway_fake()
    payload = '{"desc":"%s","desc":"benign"}' % BANNED
    result = _invoke_group(fake, [{"type": "json", "data": {"data": payload}}])
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_json_duplicate_key_first_value_blocked_cq():
    fake = _gateway_fake()
    payload = '[CQ:json,data={"desc":"%s","desc":"benign"}]' % BANNED
    result = _invoke_group(fake, payload)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_json_duplicate_key_last_value_blocked():
    fake = _gateway_fake()
    payload = '{"desc":"benign","desc":"%s"}' % BANNED
    result = _invoke_group(fake, [{"type": "json", "data": {"data": payload}}])
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_json_duplicate_key_nested_blocked():
    fake = _gateway_fake()
    payload = '{"card":{"desc":"%s","desc":"benign"}}' % BANNED
    result = _invoke_group(fake, [{"type": "json", "data": {"data": payload}}])
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_json_duplicate_keys_benign_dispatches_once():
    fake = _gateway_fake()
    payload = '{"desc":"one","desc":"two"}'
    result = _invoke_group(fake, [{"type": "json", "data": {"data": payload}}])
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def test_segments_visible_texts_json_duplicate_keys_all_values():
    # 投递层（services.segments）同口径：重复键首值同样进收集。
    segment = RawSegment(
        type="json", data={"data": '{"desc":"%s","desc":"benign"}' % BANNED}
    )
    texts = visible_texts([segment])
    assert any(BANNED in text for text in texts)
    assert any("benign" in text for text in texts)


# ---------------------------------------------------------------------------
# 收口轮（基线 3c3e8fd）：渲染序 + 双序送审、共享提取、services 同矩阵、
# 深层 fail-closed、三重重复键、变体/容器剩余键
# ---------------------------------------------------------------------------

_SPLIT_A = "结束你的"
_SPLIT_B = "生命"


def _split_node_message(first_in_field: bool):
    field, body = (_SPLIT_A, _SPLIT_B) if first_in_field else (_SPLIT_B, _SPLIT_A)
    return [
        {
            "type": "node",
            "data": {
                "name": "n",
                "uin": "1",
                "desc": field,
                "content": [{"type": "text", "data": {"text": body}}],
            },
        }
    ]


def _split_forward_message(first_in_field: bool):
    field, body = (_SPLIT_A, _SPLIT_B) if first_in_field else (_SPLIT_B, _SPLIT_A)
    return [
        {
            "type": "forward",
            "data": {
                "id": "5",
                "summary": field,
                "content": [{"type": "text", "data": {"text": body}}],
            },
        }
    ]


@pytest.mark.parametrize(
    "first_in_field,blocked", [(True, True), (False, False)]
)
def test_content_review_node_field_content_split_by_render_order(
    first_in_field, blocked
):
    # 拆分短语按渲染序判定（收口复审 P3 纠正后的定稿口
    # 径）：node 数据字段在渲染流里先于 content——字段在
    # 前拼出连续短语必拦（当初的绕过形态，提取曾把剩余键
    # 排在 content 之后）；字段在后时拼接流是「生命|结束
    # 你的」，按其自身渲染序拼不出短语，照常派发。收口轮
    # 的双序并集曾把两种放法都判拦，已因误拦良性回复被
    # 裁决撤销，本钉的 [False] 分支即该纠正的载体。
    fake = _gateway_fake()
    result = _invoke_group(fake, _split_node_message(first_in_field))
    if blocked:
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        )
        assert fake.transport.calls == []
    else:
        assert result.outcome == "ok"
        assert len(fake.transport.calls) == 1


@pytest.mark.parametrize(
    "first_in_field,blocked", [(True, True), (False, False)]
)
def test_content_review_forward_field_content_split_by_render_order(
    first_in_field, blocked
):
    # forward 同口径：summary 等数据字段先于 content 渲染。
    fake = _gateway_fake()
    result = _invoke_group(fake, _split_forward_message(first_in_field))
    if blocked:
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        )
        assert fake.transport.calls == []
    else:
        assert result.outcome == "ok"
        assert len(fake.transport.calls) == 1


@pytest.mark.parametrize(
    "seg_type,primary_key",
    [("text", "text"), ("markdown", "content")],
)
@pytest.mark.parametrize(
    "first_in_primary,blocked", [(True, True), (False, False)]
)
def test_content_review_typed_primary_extra_key_split_by_order(
    seg_type, primary_key, first_in_primary, blocked
):
    # text/markdown 的本分字段与剩余键同口径：提取序为本
    # 分字段在前、剩余键随后（卡片流），按该序拼出短语必
    # 拦；镜像放法按自身序判定、照常派发。
    primary, extra = (
        (_SPLIT_A, _SPLIT_B) if first_in_primary else (_SPLIT_B, _SPLIT_A)
    )
    fake = _gateway_fake()
    message = [
        {"type": seg_type, "data": {primary_key: primary, "title": extra}}
    ]
    result = _invoke_group(fake, message)
    if blocked:
        assert (result.outcome, result.gate) == (
            "rejected",
            "content_review_blocked",
        )
        assert fake.transport.calls == []
    else:
        assert result.outcome == "ok"
        assert len(fake.transport.calls) == 1


def test_content_review_split_benign_cross_field_dispatches():
    # 良性跨字段文本不新增误拦：普通跨字段短语（按渲染序
    # 不成违禁短语）照常恰一次派发。
    fake = _gateway_fake()
    message = [
        {
            "type": "node",
            "data": {
                "name": "小明",
                "uin": "1",
                "desc": "今天的",
                "content": [{"type": "text", "data": {"text": "天气不错"}}],
            },
        }
    ]
    result = _invoke_group(fake, message)
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        '{"desc":"%s","desc":"b","desc":"c"}',
        '{"desc":"a","desc":"%s","desc":"c"}',
        '{"desc":"a","desc":"b","desc":"%s"}',
    ],
)
def test_content_review_json_triple_duplicate_any_position_blocked(payload):
    # ≥3 重复键（复审 P3）：hook 的 append 分支（第三次及以
    # 后出现）必须同口径保值送审——任一位置藏词必拦。
    fake = _gateway_fake()
    result = _invoke_group(
        fake, [{"type": "json", "data": {"data": payload % BANNED}}]
    )
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_json_triple_duplicate_third_blocked_cq():
    fake = _gateway_fake()
    payload = '[CQ:json,data={"desc":"a","desc":"b","desc":"%s"}]' % BANNED
    result = _invoke_group(fake, payload)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_json_triple_duplicate_benign_dispatches_once():
    fake = _gateway_fake()
    payload = '{"desc":"a","desc":"b","desc":"c"}'
    result = _invoke_group(fake, [{"type": "json", "data": {"data": payload}}])
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


@pytest.mark.parametrize(
    "seg_type,data",
    [
        ("node", {"name": "n", "uin": "1", "DESC": BANNED,
                  "content": [{"type": "text", "data": {"text": "hi"}}]}),
        ("node", {"name": "n", "uin": "1", " Desc ": BANNED,
                  "content": [{"type": "text", "data": {"text": "hi"}}]}),
        ("forward", {"id": "5", "SUMMARY": BANNED}),
        ("forward", {"id": "5", " Summary ": BANNED}),
        ("text", {"text": "hi", "Title": BANNED}),
        ("markdown", {"content": "hi", "DESC": BANNED}),
    ],
)
def test_content_review_remaining_key_case_padding_variants_blocked(
    seg_type, data
):
    # 剩余键的大小写/填充变体（复审 P3）：经 typed 分支的剩
    # 余键回落路径同口径受审（归一在共享提取一处）。
    fake = _gateway_fake()
    result = _invoke_group(fake, [{"type": seg_type, "data": data}])
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


@pytest.mark.parametrize(
    "seg_type,data",
    [
        # list 值剩余键：可见键的值按全量收集受审。
        ("node", {"name": "n", "uin": "1", "desc": [BANNED],
                  "content": [{"type": "text", "data": {"text": "hi"}}]}),
        ("forward", {"id": "5", "desc": [BANNED]}),
        # 非可见容器键之下嵌套的可见键：回落递归到达。
        ("node", {"name": "n", "uin": "1", "extra": {"title": BANNED},
                  "content": [{"type": "text", "data": {"text": "hi"}}]}),
        ("forward", {"id": "5", "extra": [{"summary": BANNED}]}),
        ("text", {"text": "hi", "extra": {"desc": BANNED}}),
    ],
)
def test_content_review_remaining_key_container_values_blocked(seg_type, data):
    fake = _gateway_fake()
    result = _invoke_group(fake, [{"type": seg_type, "data": data}])
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []


def test_content_review_remaining_key_variant_benign_dispatches_once():
    fake = _gateway_fake()
    message = [
        {
            "type": "node",
            "data": {
                "name": "n",
                "uin": "1",
                "DESC": "你好呀",
                "extra": {"title": "你好呀"},
                "content": [{"type": "text", "data": {"text": "hi"}}],
            },
        }
    ]
    result = _invoke_group(fake, message)
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


# --- services 路径：与网关同矩阵（段对象形态 × 可见键全集） -----------
#
# CQ 形态在 services 层不存在（本层处理的是已建模段对象），
# 矩阵以段对象形态覆盖七类：RawSegment 的 json/xml/markdown/
# share + NodeSegment + ForwardSegment + FileSegment（收口
# 复审 P3 补：file 曾是矩阵盲区，其可见键在本层私规下脱
# 检）。可见键全集与网关矩阵共用 _MATRIX_VISIBLE_KEYS
# （本文件内同一份独立钉死清单）。

_SVC_MATRIX_TYPES = [
    "json",
    "xml",
    "markdown",
    "share",
    "node",
    "forward",
    "file",
]


def _svc_matrix_segment(seg_type, key, value):
    if seg_type == "json":
        return RawSegment(
            type="json", data={"data": '{"desc":"benign"}', key: value}
        )
    if seg_type == "xml":
        return RawSegment(
            type="xml",
            data={"data": "<msg><title>benign</title></msg>", key: value},
        )
    if seg_type == "markdown":
        return RawSegment(type="markdown", data={"content": "hi", key: value})
    if seg_type == "share":
        return RawSegment(
            type="share",
            data={"url": "https://example.com", "title": "t", key: value},
        )
    if seg_type == "node":
        if key == "content":
            return NodeSegment(
                data={"name": "n", "uin": "1"}, content=(TextSegment(value),)
            )
        return NodeSegment(
            data={"name": "n", "uin": "1", key: value},
            content=(TextSegment("hi"),),
        )
    if seg_type == "forward":
        if key == "content":
            return ForwardSegment(
                data={
                    "id": "5",
                    "content": [{"type": "text", "data": {"text": value}}],
                }
            )
        return ForwardSegment(data={"id": "5", key: value})
    if seg_type == "file":
        return FileSegment(
            data={"file": "f.pdf", "name": "f.pdf", key: value}
        )
    raise AssertionError(seg_type)


@pytest.mark.parametrize("seg_type", _SVC_MATRIX_TYPES)
@pytest.mark.parametrize("key", _MATRIX_VISIBLE_KEYS)
def test_services_matrix_banned_blocked(seg_type, key):
    allowed, _reason = review_outbound_segments(
        [_svc_matrix_segment(seg_type, key, BANNED)]
    )
    assert allowed is False


@pytest.mark.parametrize("seg_type", _SVC_MATRIX_TYPES)
@pytest.mark.parametrize("key", _MATRIX_VISIBLE_KEYS)
def test_services_matrix_benign_allowed(seg_type, key):
    allowed, _reason = review_outbound_segments(
        [_svc_matrix_segment(seg_type, key, _MATRIX_BENIGN)]
    )
    assert allowed is True


@pytest.mark.parametrize(
    "first_in_field,blocked", [(True, True), (False, False)]
)
def test_services_node_split_by_render_order(first_in_field, blocked):
    # services node 拆分与网关同口径按渲染序判定：字段在
    # 前拼出短语必拦；字段在后按自身序放行（双序并集撤销
    # 后的定稿，见网关同名钉的口径注）。
    field, body = (_SPLIT_A, _SPLIT_B) if first_in_field else (_SPLIT_B, _SPLIT_A)
    segment = NodeSegment(
        data={"name": "n", "desc": field}, content=(TextSegment(body),)
    )
    allowed, _reason = review_outbound_segments([segment])
    assert allowed is (not blocked)


@pytest.mark.parametrize(
    "first_in_field,blocked", [(True, True), (False, False)]
)
def test_services_forward_split_by_render_order(first_in_field, blocked):
    # services ForwardSegment 字段/正文拆分钉（收口复审
    # P4 补，此前只有网关侧同名钉）：summary 先于 content
    # 的渲染序下，字段在前必拦、字段在后按自身序放行；
    # 良性孪生两放法都放行（见下）。
    field, body = (_SPLIT_A, _SPLIT_B) if first_in_field else (_SPLIT_B, _SPLIT_A)
    segment = ForwardSegment(
        data={
            "id": "5",
            "summary": field,
            "content": [{"type": "text", "data": {"text": body}}],
        }
    )
    allowed, _reason = review_outbound_segments([segment])
    assert allowed is (not blocked)


@pytest.mark.parametrize("first_in_field", [True, False])
def test_services_forward_split_benign_allowed(first_in_field):
    field, body = ("今天的", "天气不错") if first_in_field else ("天气不错", "今天的")
    segment = ForwardSegment(
        data={
            "id": "5",
            "summary": field,
            "content": [{"type": "text", "data": {"text": body}}],
        }
    )
    allowed, _reason = review_outbound_segments([segment])
    assert allowed is True


@pytest.mark.parametrize(
    "segments,blocked",
    [
        ([TextSegment(_SPLIT_A), TextSegment(_SPLIT_B)], True),
        ([TextSegment(_SPLIT_B), TextSegment(_SPLIT_A)], False),
    ],
)
def test_services_segments_split_across_segments_by_order(segments, blocked):
    # 段间拆分（services 层的片段序 = 段序 = 线序）：按段
    # 序拼出短语必拦；逆段序的流按其自身顺序判定、放行
    # （双序并集撤销后的定稿口径）。
    allowed, _reason = review_outbound_segments(segments)
    assert allowed is (not blocked)


def test_services_deep_json_recursion_error_fails_closed(monkeypatch):
    # 深层解析异常的确定性钉（复审 P3）：共享解析抛
    # RecursionError 时 services 路径必须转不可审拦截，
    # 不许穿透到发送路径（与网关打桩钉同手法）。
    from types import SimpleNamespace

    from companion import content_extract as extract_module

    def _raising_loads(_text, **_kwargs):
        raise RecursionError("parser stack exhausted")

    monkeypatch.setattr(
        extract_module,
        "json",
        SimpleNamespace(loads=_raising_loads, dumps=json.dumps),
    )
    allowed, reason = review_outbound_segments(
        [RawSegment(type="json", data={"data": "{}"})]
    )
    assert allowed is False
    assert reason == "unreviewable_payload"


def test_services_deep_json_banned_blocked_no_exception():
    # 自然深层形态的不变量钉（收口复审 P4 后改原因感知断
    # 言）：藏词在 2000 层深处——解析器完整提取后按内容拦
    # （原因 identity/safety）、或抛深度异常转不可审拦
    # （unreviewable_payload），二者都是合法拦截形态，但
    # 绝不放行、绝不抛异常穿出；良性深层则只许「提取完整
    # 后放行」或「不可审拦截」两种结果——被按内容拦截
    # （allowed=False 且原因是内容裁决）即是回归、必红，
    # 旧断言 isinstance(bool) 对此空转、已废。
    deep_banned = '{"a":' * 2000 + '"%s"' % BANNED + '}' * 2000
    allowed, reason = review_outbound_segments(
        [RawSegment(type="json", data={"data": deep_banned})]
    )
    assert allowed is False
    assert reason in {"identity", "safety", "unreviewable_payload"}
    deep_benign = '{"a":' * 2000 + '"x"' + '}' * 2000
    allowed, reason = review_outbound_segments(
        [RawSegment(type="json", data={"data": deep_benign})]
    )
    assert allowed is True or reason == "unreviewable_payload"


def test_services_deep_dict_form_banned_blocked():
    # dict 形（已解析结构）深层同口径：旧 _collect_all_
    # strings 的递归爆栈面已由共享迭代收集关闭。
    node = cursor = {}
    for _ in range(2000):
        cursor["a"] = {}
        cursor = cursor["a"]
    cursor["leaf"] = BANNED
    allowed, _reason = review_outbound_segments(
        [RawSegment(type="json", data={"data": node})]
    )
    assert allowed is False


def test_services_json_triple_duplicate_positions_blocked():
    for payload in (
        '{"desc":"%s","desc":"b","desc":"c"}',
        '{"desc":"a","desc":"b","desc":"%s"}',
    ):
        allowed, _reason = review_outbound_segments(
            [RawSegment(type="json", data={"data": payload % BANNED})]
        )
        assert allowed is False


def test_services_json_duplicate_nested_collected_and_blocked():
    # services 层重复键的嵌套形态 + 复核级拦截（复审 P4 补）：
    # 不止于 visible_texts 收集，review_outbound_segments
    # 必须据此作出拦截判定；良性重复键照常放行。
    segment = RawSegment(
        type="json",
        data={"data": '{"card":{"desc":"benign","desc":"%s"}}' % BANNED},
    )
    texts = visible_texts([segment])
    assert any(BANNED in text for text in texts)
    allowed, _reason = review_outbound_segments([segment])
    assert allowed is False
    benign = RawSegment(
        type="json", data={"data": '{"desc":"one","desc":"two"}'}
    )
    allowed, _reason = review_outbound_segments([benign])
    assert allowed is True


# ---------------------------------------------------------------------------
# 纠正轮（基线 d9deb91）：撤销双序、FileSegment 共享提取、NodeSegment
# 变体键恒扫、双 caller 装配 fail-closed
# ---------------------------------------------------------------------------


def test_review_bubbles_benign_sequential_pair_passes():
    # 法官复现的良性误拦案例（收口复审 P3）：顺序确定的两
    # 气泡回复——逆序拼接凭空拼出「结束生命」，曾被双序并
    # 集误判 safety。只审真实顺序后必过；同对文本走
    # services 纯文本路径（review_outbound_segments）同判。
    bubbles = ["生命在于运动，要多锻炼", "今天聊到这里就结束"]
    assert review_bubbles(bubbles, enforce_size=False) == (True, "")
    allowed, _reason = review_outbound_segments(
        [TextSegment(text) for text in bubbles]
    )
    assert allowed is True
    # 对照：真实顺序下拼出短语的拆分仍必拦（纠正不回退）。
    allowed, _reason = review_outbound_segments(
        [TextSegment("今天聊到这里就结束"), TextSegment("你的生命")]
    )
    assert allowed is False


@pytest.mark.parametrize(
    "variant_key",
    ["Content", "CONTENT", " CONTENT ", "content ", "CoNtEnT"],
)
@pytest.mark.parametrize("typed", [True, False])
def test_services_node_content_variant_key_blocked(variant_key, typed):
    # NodeSegment content 变体键矩阵（收口复审 P3）：序列
    # 化器只剔精确 'content' 键，变体原样进报文——任何类
    # 型化状态下藏词都必须被复核拦下（旧口径只在类型化
    # content 为空时才扫原始 content，变体借非空逃审）。
    segment = NodeSegment(
        data={"name": "n", variant_key: BANNED},
        content=(TextSegment("hi"),) if typed else (),
    )
    allowed, _reason = review_outbound_segments([segment])
    assert allowed is False


def test_services_node_content_variant_list_value_blocked():
    # 变体键的值为原始段数组形态同口径必拦。
    segment = NodeSegment(
        data={
            "name": "n",
            "Content": [{"type": "text", "data": {"text": BANNED}}],
        },
        content=(TextSegment("hi"),),
    )
    allowed, _reason = review_outbound_segments([segment])
    assert allowed is False


def test_services_node_content_variant_benign_allowed():
    segment = NodeSegment(
        data={"name": "n", "Content": "你好呀"},
        content=(TextSegment("hi"),),
    )
    allowed, _reason = review_outbound_segments([segment])
    assert allowed is True


_UNREVIEWABLE_XML = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE msg [<!ENTITY x "y">]><msg><title>hi</title></msg>'
)


def test_delivery_service_unreviewable_payload_fails_closed():
    # 生产 caller 装配钉（收口复审 P3）：不可审载荷（XML
    # DTD）经 DeliveryService 真实调用链——回执 rejected、
    # 适配器零发送、无异常穿出；良性对照 accepted 且恰
    # 一次发送（路径本身通畅、拦的是载荷不是通道）。
    from companion.adapters import FakeAdapter
    from companion.adapters.base import PrivateTarget
    from companion.quota import SendQuota
    from companion.services.delivery import DeliveryService

    async def scenario():
        fake = FakeAdapter()
        delivery = DeliveryService(fake, SendQuota())
        receipt = await delivery.send_to(
            PrivateTarget("10001"),
            [RawSegment(type="xml", data={"data": _UNREVIEWABLE_XML})],
        )
        assert receipt.outcome == "rejected"
        assert fake.segment_sends == []
        receipt = await delivery.send_to(
            PrivateTarget("10001"), [TextSegment("你好")]
        )
        assert receipt.outcome == "accepted"
        assert len(fake.segment_sends) == 1

    run(scenario())


def test_onebot_send_private_unreviewable_payload_fails_closed():
    # 另一生产 caller（OneBotAdapter.send_private 直发）同
    # 钉：不可审载荷在到达客户端之前被复核拦下，客户端零
    # 调用；良性对照 accepted 且恰一次调用。
    from companion.adapters.onebot import OneBotAdapter

    class _RecordingClient:
        def __init__(self):
            self.calls = []

        async def send_private_msg(self, user_id, message, **_kwargs):
            self.calls.append((user_id, message))
            return {"status": "ok", "data": {"message_id": 4242}}

    async def scenario():
        client = _RecordingClient()
        adapter = OneBotAdapter(client)
        receipt = await adapter.send_private(
            "onebot:90001",
            [RawSegment(type="xml", data={"data": _UNREVIEWABLE_XML})],
        )
        assert receipt.outcome == "rejected"
        assert client.calls == []
        receipt = await adapter.send_private(
            "onebot:90001", [TextSegment("你好")]
        )
        assert receipt.outcome == "accepted"
        assert len(client.calls) == 1

    run(scenario())


# ---------------------------------------------------------------------------
# 序列化序轮（基线 bb550ce）：提取顺序逐字等于序列化顺序
# ---------------------------------------------------------------------------

_PERM_A = "结束你的"
_PERM_B = "生命"
_PERM_X = "嗯"
_PERM_KEYS = ("title", "name", "prompt")
_PERM_VALS = {"title": _PERM_A, "name": _PERM_B, "prompt": _PERM_X}


def _perm_blocked_in_stream(values) -> bool:
    return any(
        values[i] == _PERM_A and values[i + 1] == _PERM_B
        for i in range(len(values) - 1)
    )


def test_gateway_node_field_order_permutations():
    # 字段序排列抽测（收口复审 P2）：node 三字段全排列——
    # 提取流必须逐字等于 dict 插入序（序列化序），判定随
    # 之：短语两半在流中相邻（A 紧接 B）才拦，其余放行。
    # 规范序提取下 title/name 的相对序被重排、两排列必失
    # 判（复审双车道复现的 title 先于 name 即其一）。
    import itertools

    from companion.content_extract import payload_visible_texts

    for perm in itertools.permutations(_PERM_KEYS):
        data = {key: _PERM_VALS[key] for key in perm}
        data["content"] = [{"type": "text", "data": {"text": "hi"}}]
        message = [{"type": "node", "data": data}]
        stream = [_PERM_VALS[key] for key in perm] + ["hi"]
        assert payload_visible_texts(message) == stream
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        if _perm_blocked_in_stream(stream):
            assert (result.outcome, result.gate) == (
                "rejected",
                "content_review_blocked",
            )
            assert fake.transport.calls == []
        else:
            assert result.outcome == "ok"
            assert len(fake.transport.calls) == 1


def test_gateway_node_remaining_key_position_by_insertion():
    # 剩余键（desc）的位置同样由插入序决定：desc 先插入
    # 则排在 name 之前拼出短语必拦；name 先插入则按自身
    # 序放行——规范序/回落追加的旧结构下前一形态必逃。
    fake = _gateway_fake()
    message = [
        {
            "type": "node",
            "data": {
                "desc": _PERM_A,
                "name": _PERM_B,
                "content": [{"type": "text", "data": {"text": "hi"}}],
            },
        }
    ]
    result = _invoke_group(fake, message)
    assert (result.outcome, result.gate) == ("rejected", "content_review_blocked")
    assert fake.transport.calls == []
    fake = _gateway_fake()
    message[0]["data"] = {
        "name": _PERM_B,
        "desc": _PERM_A,
        "content": [{"type": "text", "data": {"text": "hi"}}],
    }
    result = _invoke_group(fake, message)
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def test_services_node_field_order_permutations():
    # services typed node 同抽测：data 插入序 = 序列化属
    # 性序，类型化 content 恒居末（此抽测 content 为良性
    # 单段、不参与接缝），判定以序列化序流为准。
    import itertools

    for perm in itertools.permutations(_PERM_KEYS):
        data = {key: _PERM_VALS[key] for key in perm}
        segment = NodeSegment(data=data, content=(TextSegment("hi"),))
        stream = [_PERM_VALS[key] for key in perm] + ["hi"]
        assert visible_texts([segment]) == stream
        allowed, _reason = review_outbound_segments([segment])
        assert allowed is (not _perm_blocked_in_stream(stream))


def test_services_file_field_order_permutations():
    # FileSegment 全排列：name 有效时 file 键不发声（文件
    # 名以 name 为准），流中只有 title/name 两值——判定只
    # 看 title 是否排在 name 之前（序列化序）；name 强制
    # 置前的旧结构下 title 先插入的形态必逃（复审复现）。
    import itertools

    for perm in itertools.permutations(("title", "name", "file")):
        values = {"title": _PERM_A, "name": _PERM_B, "file": "f.pdf"}
        data = {key: values[key] for key in perm}
        allowed, _reason = review_outbound_segments([FileSegment(data)])
        assert allowed is (perm.index("title") > perm.index("name"))


def test_typed_extraction_matches_serialization_order():
    # typed 对齐钉（父级口径 2）：提取流与序列化字段流逐
    # 字对照——node 序列化 = 属性插入序 + content 末尾，
    # 提取同序；file 的 basename 站在 file 键原位（name
    # 缺失时），name 在原位、file 键在 name 有效时不发声。
    from companion.adapters.onebot import segments_to_onebot_message

    segment = NodeSegment(
        data={"title": "T", "name": "N", "desc": "D"},
        content=(TextSegment("C"),),
    )
    serialized = segments_to_onebot_message([segment])
    assert list(serialized[0]["data"].keys()) == [
        "title",
        "name",
        "desc",
        "content",
    ]
    assert visible_texts([segment]) == ["T", "N", "D", "C"]

    segment = FileSegment({"file": "/x/报告.pdf", "title": "T"})
    serialized = segments_to_onebot_message([segment])
    assert list(serialized[0]["data"].keys()) == ["file", "title"]
    assert visible_texts([segment]) == ["报告.pdf", "T"]

    segment = FileSegment({"title": "T", "name": "N", "file": "f.pdf"})
    assert visible_texts([segment]) == ["T", "N"]


def test_services_forward_extraction_follows_insertion_order():
    # ForwardSegment 的 data 由共享 forward 分支按插入序
    # 提取：summary 先插入则先出、content 先插入则正文先
    # 出——顺序由 dict 构造决定，不再有「字段恒先于正文」
    # 的硬编码（与 dict 形态的序列化序同源）。
    segment = ForwardSegment(
        data={
            "summary": "S",
            "id": "5",
            "content": [{"type": "text", "data": {"text": "C"}}],
        }
    )
    assert visible_texts([segment]) == ["S", "C"]
    segment = ForwardSegment(
        data={
            "content": [{"type": "text", "data": {"text": "C"}}],
            "id": "5",
            "summary": "S",
        }
    )
    assert visible_texts([segment]) == ["C", "S"]


def test_services_node_content_variant_interleaved_by_position():
    # 变体键在提取流中的位置 = 其 data 插入位（收口复审
    # P2 同类缝）：不再被统一追加到全部字段之后——变体在
    # 前则先出、在 name 之后则居中，类型化 content 恒末。
    segment = NodeSegment(
        data={"Content": "V", "name": "N"}, content=(TextSegment("C"),)
    )
    assert visible_texts([segment]) == ["V", "N", "C"]
    segment = NodeSegment(
        data={"name": "N", "Content": "V"}, content=(TextSegment("C"),)
    )
    assert visible_texts([segment]) == ["N", "V", "C"]


def test_services_node_exact_content_key_exception_both_directions():
    # exact-'content' 例外双向钉（收口复审 P3）：类型化
    # content 非空时，精确 'content' 键被序列化器剔除并
    # 以类型化内容重建（不进报文）——其原始值不纳入复
    # 核，良性/藏词同判放行；类型化为空时原始 content
    # 是未成形节点的唯一内容载体，同形态必须拦。
    segment = NodeSegment(
        data={"name": "n", "content": BANNED},
        content=(TextSegment("hi"),),
    )
    allowed, _reason = review_outbound_segments([segment])
    assert allowed is True
    segment = NodeSegment(data={"name": "n", "content": BANNED}, content=())
    allowed, _reason = review_outbound_segments([segment])
    assert allowed is False


def test_services_node_variant_typed_split_pair():
    # 变体键与类型化 content 的拆分对（收口复审 P3）：变
    # 体值在数据流内、类型化 content 在其后——变体出前
    # 半、类型化出后半，拼成短语必拦；镜像放法按自身序
    # 放行。本对同时钉住变体循环的位置（移到类型化
    # content 之后即前一形态逃审、变异可杀）。
    segment = NodeSegment(
        data={"name": "n", "Content": "结束"},
        content=(TextSegment("你的生命"),),
    )
    allowed, _reason = review_outbound_segments([segment])
    assert allowed is False
    segment = NodeSegment(
        data={"name": "n", "Content": "你的生命"},
        content=(TextSegment("结束"),),
    )
    allowed, _reason = review_outbound_segments([segment])
    assert allowed is True


def test_json_duplicate_keys_document_order_preserved():
    # 重复键形态与派发同构（父级口径 3 查清后钉）：派发
    # 路径恒转发原始 JSON 字符串（网关不重序列化段数
    # 据），报文里双值并存、按文档序排列——提取以保对解
    # 析收集，双值同序进审查流（首值的位置即文档首现
    # 位），与报文逐字同构；dict 形态在构造时已单值化，
    # 无双值可言（既有重复键拦截钉覆盖判定面）。
    from companion.content_extract import payload_visible_texts

    message = [
        {"type": "json", "data": {"data": '{"desc":"a","desc":"b"}'}}
    ]
    assert payload_visible_texts(message) == ["a", "b"]


# ---------------------------------------------------------------------
# 语义角色精确键轮（serde 序复审 P2/P3/P4）：序列化序主体已
# 确认，本轮收「归一匹配驱动语义决策」的等价类——语义角色
# （取哪个值当文件名/正文、跳过什么）只认序列化端精确键，
# 归一只许扩大审查覆盖、永不许顶替或跳过另一个值的审查。
# services FileSegment 的 has_name 旧以 strip().lower() 归一
# 判定，'Name' 等变体键（序列化只作惰性附加属性透传）即可
# 把违禁 basename 顶出审查集。另补四分支兄弟键位置排列钉
# 与 node content 入排列集钉。
# ---------------------------------------------------------------------


def test_services_file_variant_name_never_suppresses_basename():
    # 复审 P2 复现形态全集：变体键（大小写/填充五形）× 精确
    # name 缺失/为空——basename 恒送审、必拦；变体值本身按
    # 普通可见值在本位入流。
    from companion.services.segments import (
        FileSegment,
        review_outbound_segments,
        visible_texts,
    )

    for variant in ("Name", "NAME", " name ", "Name ", "nAme"):
        for data in (
            {variant: "ok", "file": "/x/结束你的生命.pdf"},
            {"name": "", variant: "ok", "file": "/x/结束你的生命.pdf"},
        ):
            seg = FileSegment(data=data)
            assert visible_texts([seg]) == ["ok", "结束你的生命.pdf"], (
                variant,
                data,
            )
            assert review_outbound_segments([seg]) == (False, "safety"), (
                variant,
                data,
            )


def test_services_file_exact_name_semantics_unchanged():
    # 精确 'name' 既有行为零变化：非空 str 的精确 name 给出
    # 文件名显示 → basename 不入审查集、放行（基线语义）；
    # 非 str 的精确 name 不构成文件名 → 不顶替、basename
    # 照常送审必拦。
    from companion.services.segments import (
        FileSegment,
        review_outbound_segments,
        visible_texts,
    )

    seg = FileSegment(data={"name": "ok", "file": "/x/结束你的生命.pdf"})
    assert visible_texts([seg]) == ["ok"]
    assert review_outbound_segments([seg]) == (True, "")
    seg = FileSegment(
        data={"name": "ok", "Name": "fine", "file": "/x/结束你的生命.pdf"}
    )
    assert visible_texts([seg]) == ["ok", "fine"]
    assert review_outbound_segments([seg]) == (True, "")
    seg = FileSegment(data={"name": 123, "file": "/x/结束你的生命.pdf"})
    assert visible_texts([seg]) == ["结束你的生命.pdf"]
    assert review_outbound_segments([seg]) == (False, "safety")


def test_sibling_key_position_permutations_text_json_xml_markdown():
    # 复审 P3：text/json/xml/markdown 四分支的兄弟可见键位
    # 置排列钉——兄弟键在主键（text/content/data）两侧时提
    # 取流恒等于插入序（xml 的 ['X', 'X'] 为双流并集既有形
    # 态，基线同）。任一分支退回字段先行/回落追加序即红。
    from companion.content_extract import payload_visible_texts

    cases = [
        ([{"type": "text", "data": {"title": "T", "text": "B"}}], ["T", "B"]),
        ([{"type": "text", "data": {"text": "B", "title": "T"}}], ["B", "T"]),
        (
            [{"type": "markdown", "data": {"title": "T", "content": "C"}}],
            ["T", "C"],
        ),
        (
            [{"type": "markdown", "data": {"content": "C", "title": "T"}}],
            ["C", "T"],
        ),
        (
            [{"type": "json", "data": {"title": "T", "data": '{"desc": "D"}'}}],
            ["T", "D"],
        ),
        (
            [{"type": "json", "data": {"data": '{"desc": "D"}', "title": "T"}}],
            ["D", "T"],
        ),
        (
            [
                {
                    "type": "xml",
                    "data": {"title": "T", "data": "<msg><text>X</text></msg>"},
                }
            ],
            ["T", "X", "X"],
        ),
        (
            [
                {
                    "type": "xml",
                    "data": {"data": "<msg><text>X</text></msg>", "title": "T"},
                }
            ],
            ["X", "X", "T"],
        ),
    ]
    for payload, expected in cases:
        assert payload_visible_texts(payload) == expected, payload


def test_text_primary_not_first_split_outcome():
    # typed-primary 拆分钉补「主键非首位」形态（复审 P3）：
    # text 段兄弟键 title 在主键 text 之前拼出短语必拦；镜
    # 像放法按自身序列化序判定（两半逆序不构成短语）放行；
    # 良性对照恰一次派发。
    fake = _gateway_fake()
    result = _invoke_group(
        fake,
        [{"type": "text", "data": {"title": "结束你的", "text": "生命"}}],
    )
    assert (result.outcome, result.gate) == (
        "rejected",
        "content_review_blocked",
    )
    assert fake.transport.calls == []

    fake = _gateway_fake()
    result = _invoke_group(
        fake,
        [{"type": "text", "data": {"text": "生命", "title": "结束你的"}}],
    )
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1

    fake = _gateway_fake()
    result = _invoke_group(
        fake,
        [{"type": "text", "data": {"title": "标题", "text": "正文"}}],
    )
    assert result.outcome == "ok"
    assert len(fake.transport.calls) == 1


def test_gateway_node_content_key_inside_permutation():
    # 复审 P4：content 键进入排列集（首/中/末三位）——提取
    # 流逐字等于含 content 的全插入序，'hi' 居中即隔断短语
    # 相邻性、按其自身序放行；content 恒排末位的回归即红。
    import itertools

    from companion.content_extract import payload_visible_texts

    content_form = [{"type": "text", "data": {"text": "hi"}}]
    values = {"title": "结束你的", "name": "生命", "content": content_form}
    stream_of = {"title": "结束你的", "name": "生命", "content": "hi"}
    for perm in itertools.permutations(("title", "name", "content")):
        data = {key: values[key] for key in perm}
        message = [{"type": "node", "data": data}]
        stream = [stream_of[key] for key in perm]
        assert payload_visible_texts(message) == stream, perm
        fake = _gateway_fake()
        result = _invoke_group(fake, message)
        adjacent = any(
            stream[i] == "结束你的" and stream[i + 1] == "生命"
            for i in range(len(stream) - 1)
        )
        if adjacent:
            assert (result.outcome, result.gate) == (
                "rejected",
                "content_review_blocked",
            ), perm
            assert fake.transport.calls == []
        else:
            assert result.outcome == "ok", perm
            assert len(fake.transport.calls) == 1


def test_services_file_variant_file_key_dispatch_pinned():
    # 语义键复审 P3 补钉：'file' 键归一分发是变体 file 键值
    # 的唯一审查路径（共享回落对 {'File': 路径} 返回 []、
    # 不出 basename）——变体 file 键 × name 组合在此钉死：
    # 无精确 name 时 basename 恒在本位送审必拦（双插入
    # 序）；精确 name 非空时按基线语义放行（变体 file 键的
    # basename 不入集）。分发收窄为精确 'file' 的回归即红。
    from companion.services.segments import (
        FileSegment,
        review_outbound_segments,
        visible_texts,
    )

    banned = "/x/结束你的生命.pdf"
    for file_key in ("File", " file ", "FILE"):
        # 变体 name 在前、变体 file 键在后
        seg = FileSegment(data={"Name": "ok", file_key: banned})
        assert visible_texts([seg]) == ["ok", "结束你的生命.pdf"], file_key
        assert review_outbound_segments([seg]) == (False, "safety"), file_key
        # 变体 file 键在前、变体 name 在后（流随插入序）
        seg = FileSegment(data={file_key: banned, "Name": "ok"})
        assert visible_texts([seg]) == ["结束你的生命.pdf", "ok"], file_key
        assert review_outbound_segments([seg]) == (False, "safety"), file_key
        # 变体 file 键在前、精确 name 在后：精确 name 给出
        # 文件名显示 → basename 不入集、放行
        seg = FileSegment(data={file_key: banned, "name": "ok"})
        assert visible_texts([seg]) == ["ok"], file_key
        assert review_outbound_segments([seg]) == (True, ""), file_key
        # 精确 name 在前、变体 file 键在后：同上
        seg = FileSegment(data={"name": "ok", file_key: banned})
        assert visible_texts([seg]) == ["ok"], file_key
        assert review_outbound_segments([seg]) == (True, ""), file_key
