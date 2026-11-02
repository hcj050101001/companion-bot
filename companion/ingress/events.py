"""入站事件面子链（N3，方案 §2.3/§3.3/§4 C3、C5(a)、C6、C7、C14、C16）。

本模块只做「群聊子链 / 事件子链」的登记与观察：群消息只解析、分类、
有界观察（SafetyObserve 关键词登记），不生成、不读记忆、不调工具、
不告警、不回复；notice/meta 只登记落日志与指标（meta 不入队，由接
入边界直接调用）；request 事件经持久化处置状态机落库后由事件消费者
调用 :func:`handle_request`，默认策略为人工处置（处置动作不启用）。

N4 扩展（方案 §6 N4 / §4 C3、C4）：群回复通路 :func:`handle_group_message`
在观察之后追加——仅当接线层注入了 responder（产品开关开）时才可能
回复，且须 @ 触发、过双维主体分级、发送只经 DeliveryService 的群
通道（其群开关/额度/递归复核即群安全边界）；默认无 responder 时
行为与 N3 逐项一致（只观察）。request 处置策略增 allowlist 模式，
处置通道增授权上下文显式注入。

依赖纪律：本模块**不得** import ``companion.ingress`` 包的
``__init__``（避免循环导入）；处理器 ``proc`` 一律按不透明句柄使用
（只读 ``proc.metrics`` / ``proc.safety`` / ``proc.allowed``，只调
``proc._log``；群回复通路另只经 ``proc.delivery.send_to`` 发送）。
类型与常量一律取适配器层与领域层的契约定义，不复制枚举。
"""

import hashlib
import inspect
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from companion.adapters.base import (
    AtSegment,
    ForwardSegment,
    GroupTarget,
    InboundEvent,
    MetaEvent,
    NodeSegment,
    NoticeEvent,
    ReplySegment,
    RequestEvent,
    Segment,
    TextSegment,
)
from companion.adapters.gateway import (
    PRINCIPAL_OPS_CLI,
    PRINCIPAL_PRODUCT_COMMAND,
    PRINCIPAL_SYSTEM,
    AuthorizationContext,
)
from companion.adapters.onebot import event_type_summary
from companion.domain.events import (
    SCENE_EVENT_NOTICE,
    SCENE_EVENT_REQUEST,
    SCENE_GROUP_CHAT,
    SCENE_PRIVATE_CHAT,
    SCENE_SYSTEM,
)
from companion.domain.requests import (
    DECISION_APPROVED,
    DECISION_MANUAL,
    DECISION_REJECTED,
    REQUEST_STATUS_AMBIGUOUS,
    REQUEST_STATUS_DONE,
    REQUEST_STATUS_FAILED,
    RESULT_HANDLER_ERROR,
    RESULT_PLATFORM_REJECTED,
    RESULT_POLICY_MANUAL,
    request_key,
)

logger = logging.getLogger("companion.events")

# 场景子链的命名 Stage 序列（方案 §4 C3）：群聊子链是白名单形态，
# 只允许「解析 → 分类 → 有界观察」；事件子链止于 RequestHandle
# （默认策略关，见 RequestPolicy）。链的选择本身是契约，顺序固定。
GROUP_SUBCHAIN = ("GroupNormalize", "GroupClassify", "SafetyObserve")
EVENT_SUBCHAIN = ("EventNormalize", "EventClassify", "EventObserve", "RequestHandle")

# N4 群回复通路（方案 §6 N4）：产品开关开且接线层注入 responder 时，
# 群聊子链在观察之后由 handle_group_message 追加「双维准入 → 群回复」
# 行为（等效 GroupAdmission/GroupRespond 两级，但不冒充 Stage 装配——
# Review P3-10：曾有的 group_subchain() 纯装配函数不被运行时使用，
# 已删除，运行时事实以 handle_group_message 为准）。默认形态（开关
# 关）严格等于 GROUP_SUBCHAIN——生成（模型调用）、私聊记忆读写、
# 工具调用、自动动作不进群链；回复行为也不例外：只调注入的
# responder 并经 DeliveryService 群通道发送，框架在群链内不持有
# brain/记忆/工具。


__all__ = [
    "EVENT_SUBCHAIN",
    "GROUP_SUBCHAIN",
    "GatewayRequestChannel",
    "GroupReplyContext",
    "GroupSubject",
    "group_reply_subject_admitted",
    "NoticeCoalescer",
    "NoticeRateBudget",
    "RequestDeferred",
    "RequestPolicy",
    "group_subject",
    "handle_group_message",
    "handle_request",
    "is_group_trigger",
    "is_mergeable_notice",
    "notice_merge_key",
    "observe_group_message",
    "register_group_detached",
    "register_meta",
    "register_notice",
    "scene_for",
    "subchain_for",
]


def scene_for(classified) -> str:
    """分类结果 → EventContext.scene 取值（方案 §3.3）。

    只接受 classify 的判别联合产出；其余输入（含原始 dict）抛
    TypeError，不猜测场景。
    """
    if isinstance(classified, NoticeEvent):
        return SCENE_EVENT_NOTICE
    if isinstance(classified, RequestEvent):
        return SCENE_EVENT_REQUEST
    if isinstance(classified, MetaEvent):
        return SCENE_SYSTEM
    if isinstance(classified, InboundEvent):
        if classified.chat_type == "group":
            return SCENE_GROUP_CHAT
        return SCENE_PRIVATE_CHAT
    raise TypeError(
        f"scene_for expects a classified event, got {type(classified).__name__}"
    )


def subchain_for(classified) -> tuple:
    """分类结果 → 子链：群消息走群聊子链，notice/request 走事件子链。

    其余（私聊消息走既有私聊链、meta 只登记不入链）返回空 tuple；
    非分类输入抛 TypeError。
    """
    if isinstance(classified, InboundEvent):
        if classified.chat_type == "group":
            return GROUP_SUBCHAIN
        return ()
    if isinstance(classified, (NoticeEvent, RequestEvent)):
        return EVENT_SUBCHAIN
    if isinstance(classified, MetaEvent):
        return ()
    raise TypeError(
        f"subchain_for expects a classified event, got {type(classified).__name__}"
    )


# ---------------------------------------------------------------------------
# notice 合并（方案 §4 C7 入队前预算）：状态类 notice 同键合并只留最新。


def is_mergeable_notice(ev) -> bool:
    """可合并的 notice：group_card（群名片变更）与 input_status（输入状态）。

    这两类是状态类通知：同键在途时只保留最新载荷，旧载荷无独立价值。
    其余 notice 一律不可合并、逐条入队登记。
    """
    if getattr(ev, "notice_type", None) == "group_card":
        return True
    return (
        getattr(ev, "notice_type", None) == "notify"
        and getattr(ev, "sub_type", None) == "input_status"
    )


#: 兼容别名：可合并判定谓词。
mergeable_notice = is_mergeable_notice


def notice_merge_key(ev) -> tuple:
    """合并键：(self_id, notice_type, sub_type, group_id, user_id)。"""
    return (
        str(ev.self_id),
        str(ev.notice_type),
        ev.sub_type or "",
        ev.group_id or "",
        str(ev.user_id),
    )


class NoticeCoalescer:
    """notice 在途合并器：同键已有信封在队时，新载荷只替换不重复入队。

    队列中的信封由 webhook 侧持有引用：``admit`` 返回 "queue" 时把
    信封 dict 登记为该键的在途信封；返回 "coalesced" 时把在途信封的
    ``["classified"]`` 替换为最新事件并累计合并计数，传入的新信封
    不入队。消费者处理完在途信封后以 :meth:`release` 取回合并计数
    并解除在途标记；接入边界 shed 时同样以 release 回滚在途标记。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._inflight: dict[tuple, dict] = {}
        self._counts: dict[tuple, int] = {}

    def admit(self, ev, envelope) -> str:
        """判定一条 notice 的去向："queue"（入队）或 "coalesced"（合并）。"""
        if not is_mergeable_notice(ev):
            return "queue"
        key = notice_merge_key(ev)
        with self._lock:
            inflight = self._inflight.get(key)
            if inflight is not None:
                inflight["classified"] = ev
                self._counts[key] = self._counts.get(key, 0) + 1
                return "coalesced"
            self._inflight[key] = envelope
            self._counts.setdefault(key, 0)
            return "queue"

    def release(self, key) -> int:
        """解除某键的在途标记，返回并清除其累计合并计数。"""
        with self._lock:
            self._inflight.pop(key, None)
            return self._counts.pop(key, 0)


class NoticeRateBudget:
    """notice 入队前限流预算：滑动窗口按方案 C7 的
    (self_id, 群, 事件族) 三维键计数。

    写法与 ingress.RateLimiter 同源（单调钟 + deque 窗口），只是键为
    三维事件键而非用户；超限由接入边界按 shed 口径处理（204 + shed
    登记）。总量天花板由 notice 队列容量承担，本预算只做分群/分族
    的公平限流。事件族 family 为 notice_type，notify 再按 sub_type
    细分（不同 notify 子类互不占用额度）。
    """

    def __init__(self, limit, window_seconds: int = 60):
        self.limit, self.window = limit, window_seconds
        self.timestamps: dict[tuple[str, str, str], deque[float]] = defaultdict(deque)

    def allow(self, ev, now: float | None = None) -> bool:
        """当前窗口尚未达到限制时消费一个额度。"""
        current = time.monotonic() if now is None else now
        if ev.notice_type == "notify":
            family = f"notify:{ev.sub_type or ''}"
        else:
            family = str(ev.notice_type)
        key = (str(ev.self_id), ev.group_id or "", family)
        bucket = self.timestamps[key]
        while bucket and bucket[0] <= current - self.window:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(current)
        return True


# ---------------------------------------------------------------------------
# 群聊子链（方案 §4 C3 白名单形态 / C5(a)）：只观察，绝不触碰
# brain / conversation / delivery / alerter / 限流 / 记忆。


async def observe_group_message(proc, ev) -> str | None:
    """群聊子链：计数 + SafetyObserve 关键词登记 + 结构化日志。

    SafetyObserve 只做危机关键词命中登记（observer）：不 Interrupt、
    不告警、不回复——群里他人言论不进私聊危机链（方案 §4 C5(a)）。
    关键词检测异常按未命中处理，观察路径本身不得因检测故障升级。

    返回 reason_code（危机关键词命中时为 "crisis_keyword"，否则
    None），供 handle_group_message 的回复门消费：命中时跳过
    responder——危机不入群链（不进 LLM 自由回复）。
    """
    proc.metrics.record_event("group", "observed")
    reason_code = None
    try:
        hit = proc.safety.keyword_match(ev.text)
    except Exception:  # noqa: BLE001 — 观察路径 fail-open 为「未命中」
        hit = None
    if hit:
        proc.metrics.record_event("group", "safety_observe")
        reason_code = "crisis_keyword"
    proc._log(
        "group_message",
        len(ev.text),
        False,
        0.0,
        reason_code=reason_code,
        event_type_summary=event_type_summary(ev.raw),
    )
    return reason_code


def register_group_detached(proc, ev) -> None:
    """anonymous/notice 型群消息的边界登记：无稳定身份，只记元数据。"""
    proc._log(
        "group_message",
        len(ev.text),
        False,
        0.0,
        reason_code="registered_only",
        event_type_summary=event_type_summary(ev.raw),
    )
    proc.metrics.record_event("group", "registered")


# ---------------------------------------------------------------------------
# 群回复通路（N4，方案 §4 C3/C4 + §6 N4 群安全验收）：观察在先、回复在后；
# 通路的存在不改变观察口径，开关关（无 responder）时与 N3 逐项一致。


@dataclass(frozen=True)
class GroupSubject:
    """群场景双维主体（方案 §4 C4）：人是谁 × 他在群里是什么角色。

    person 沿用私聊 Admission 的取值口径（配置名单内 = owner、
    机器人自身 = system、其余 = stranger）；role 取平台 sender 的
    群内角色（owner/admin/member），平台未给或值域外记 unknown。
    两维独立判定：群管理员不因此成为 owner，名单内用户在群里也
    可能只是 member——权限判断须两维合取，不许单维放行。
    """

    person: str
    role: str


def group_subject(ev, *, allowed_ids=()) -> GroupSubject:
    """由事件计算双维主体：person 看配置名单，role 看 sender 角色。"""
    user_id = str(ev.user_id)
    if ev.self_id and user_id == str(ev.self_id):
        person = "system"
    elif user_id in {str(x) for x in allowed_ids}:
        person = "owner"
    else:
        person = "stranger"
    sender = ev.raw.get("sender") if isinstance(ev.raw, dict) else None
    role = sender.get("role") if isinstance(sender, dict) else None
    if role not in ("owner", "admin", "member"):
        role = "unknown"
    return GroupSubject(person=person, role=role)


def is_group_trigger(ev) -> bool:
    """群回复触发语义（群安全验收项）：仅 @ 机器人本人的消息可触发。

    普通群消息永不触发回复；引用回复不作触发（框架默认不引用、
    也不凭引用定位被回复者）。@ 全体（qq=all）不算 @ 本人。
    """
    self_id = str(ev.self_id)
    for segment in ev.segments:
        if isinstance(segment, AtSegment):
            qq = segment.data.get("qq") if isinstance(segment.data, dict) else None
            if qq is not None and str(qq) == self_id:
                return True
    return False


@dataclass(frozen=True)
class GroupReplyContext:
    """注入 responder 的群回复上下文：事件 + 双维主体 + 触发文本。

    只携带事件与分级结果，不携带任何私聊状态（会话记忆、用户偏好、
    关怀同意一律不进群链——C3/C17 上下文隔离）。
    """

    event: InboundEvent
    subject: GroupSubject
    text: str


def group_reply_subject_admitted(
    subject: GroupSubject,
    *,
    blocked_persons=("system",),
    allowed_persons=None,
    allowed_roles=None,
) -> bool:
    """群回复的框架级主体准入门（补审第二段 P3）。

    此前双维主体只分类、从不据此准入：responder 一旦接线，任何
    主体（含机器人自身回流的 system）@ 机器人都直达生成与群发送。
    本门是框架层的最小下限，与 responder 自身的判断叠加、互不
    替代：

    - ``blocked_persons``：person 命中即拒，默认 ``("system",)``
      ——机器人自身消息回流永不自触发回复（防自激循环）；
    - ``allowed_persons`` / ``allowed_roles``：非 None 时 person /
      role 须落在名单内。默认 None = 不按名单收窄——「群员 @ 即
      触发」是已拍板的产品口径，本门只补 system 下限，不收窄它。
      两维合取：任一维不合即拒（与 GroupSubject 的双维口径一致）。
    """
    if subject.person in blocked_persons:
        return False
    if allowed_persons is not None and subject.person not in allowed_persons:
        return False
    if allowed_roles is not None and subject.role not in allowed_roles:
        return False
    return True


def _group_reply_segments(produced) -> tuple:
    """responder 产出规范化 + 群回复产出过滤（Review P3-3）。

    接受 str / 单个 Segment / 段可迭代三种形态。过滤规则：剥
    ReplySegment（默认不引用他人消息）；剥 NodeSegment /
    ForwardSegment（转发结构不进群回复——其内容虽经投递层递归
    复核与媒体检测，但群回复只许平面段）；剥 @ 全体段
    （AtSegment qq="all"，responder 不得 @ 全体；@ 个人的段保留）。
    """
    if produced is None:
        return ()
    if isinstance(produced, str):
        items = [TextSegment(produced)] if produced else []
    elif isinstance(produced, Segment):
        items = [produced]
    else:
        items = list(produced)
    kept = []
    for seg in items:
        if isinstance(seg, (ReplySegment, NodeSegment, ForwardSegment)):
            continue
        if isinstance(seg, AtSegment):
            qq = seg.data.get("qq") if isinstance(seg.data, dict) else None
            if str(qq) == "all":
                continue
        kept.append(seg)
    return tuple(kept)


async def handle_group_message(
    proc,
    ev,
    *,
    responder=None,
    blocked_persons=("system",),
    allowed_persons=None,
    allowed_roles=None,
) -> None:
    """群消息处置：先观察（N3 口径不变），注入了 responder 才走回复通路。

    回复通路（产品开关开时由接线层注入 responder）的安全边界：
    仅 @ 触发；双维主体过框架级准入门（group_reply_subject_admitted：
    默认只拒 system 自触发，允许名单由接线层经本函数参数注入、
    缺省不收窄「群员 @ 即触发」的产品口径）；responder 只拿到
    GroupReplyContext（无私聊状态）；其产出经 _group_reply_segments
    过滤（默认不引用、无转发结构、不 @ 全体）再经
    DeliveryService.send_to 的群通道发送——群开关、媒体门、段级
    递归复核与两级额度都在该通道内，框架不另开旁路。
    回复段全程（生成、规范化、发送）异常只记日志（Review P3-2），
    观察结果与事件道消费不受影响。
    """
    reason_code = await observe_group_message(proc, ev)
    if responder is None:
        return
    if reason_code == "crisis_keyword":
        # 危机不入群链：命中危机关键词的群消息只观察登记，绝不进
        # LLM 自由回复——私聊危机链的固定话术/真人告警不适用于群
        # 场景（方案 §4 C5(a)），群里能做的是不接话。检测异常在
        # observe 内 fail-open 为未命中，此处不另设第二口径。
        proc.metrics.record_event("group", "reply_skipped")
        return
    if not is_group_trigger(ev):
        proc.metrics.record_event("group", "reply_skipped")
        return
    subject = group_subject(ev, allowed_ids=getattr(proc, "allowed", ()))
    if not group_reply_subject_admitted(
        subject,
        blocked_persons=blocked_persons,
        allowed_persons=allowed_persons,
        allowed_roles=allowed_roles,
    ):
        # 框架级准入门未过（默认即机器人自身回流）：只观察，不调
        # responder、不占群发送额度（补审第二段 P3）。
        proc.metrics.record_event("group", "reply_skipped")
        return
    context = GroupReplyContext(event=ev, subject=subject, text=ev.text)
    try:
        produced = responder(context)
        if inspect.isawaitable(produced):
            produced = await produced
        segments = _group_reply_segments(produced)
        if not segments:
            proc.metrics.record_event("group", "reply_skipped")
            return
        receipt = await proc.delivery.send_to(GroupTarget(str(ev.group_id)), segments)
    except Exception:  # noqa: BLE001 — 回复通路故障不影响观察与消费
        logger.exception("group reply pathway failed")
        proc.metrics.record_event("group", "reply_error")
        return
    outcome = getattr(receipt, "outcome", "rejected")
    proc._log(
        "group_reply",
        len(ev.text),
        outcome == "accepted",
        0.0,
        reason_code=outcome,
        event_type_summary=event_type_summary(ev.raw),
    )
    proc.metrics.record_event("group", "reply_sent" if outcome == "accepted" else "reply_rejected")


# ---------------------------------------------------------------------------
# notice / meta 登记（方案 §2.3 登记类处置 + 运维升级）。


def register_notice(proc, ev, *, coalesced: int = 0) -> None:
    """notice 登记：一条 notice 日志 + 计数；特定子类升级为运维告警。

    运维升级在 notice 记录之后再写一条 ops_alert：机器人被踢
    （group_decrease/kick_me）、机器人被禁言（group_ban 且对象为
    机器人自身）、机器人掉线（bot_offline）。``coalesced`` 为该事件
    在队期间被合并掉的同键更新次数，只补记指标、不写额外日志行。
    """
    summary = event_type_summary(ev.raw)
    proc._log("notice", 0, False, 0.0, event_type_summary=summary)
    proc.metrics.record_event("notice", "registered")
    ops_reason = None
    if ev.notice_type == "group_decrease" and ev.sub_type == "kick_me":
        ops_reason = "bot_kicked"
    elif (
        ev.notice_type == "group_ban"
        and ev.user_id
        and ev.user_id == ev.self_id
    ):
        ops_reason = "bot_muted"
    elif ev.notice_type == "bot_offline":
        ops_reason = "bot_offline"
    if ops_reason is not None:
        proc._log(
            "ops_alert", 0, False, 0.0,
            reason_code=ops_reason, event_type_summary=summary,
        )
        proc.metrics.record_event("notice", "ops_alert")
    for _ in range(max(int(coalesced), 0)):
        proc.metrics.record_event("notice", "coalesced")


def register_meta(proc, ev) -> None:
    """meta 事件登记（方案 §4 C14：不入队，接入边界直接登记）。

    C16 对账规则：连接健康以 get_status 探针为准，heartbeat 只作
    冗余信号——本函数只登记心跳的 online/good 观测值，不做任何对账
    动作、不改变健康判定。message_sent（自身消息回流）只入观测与
    投递关联路径：单独记 message_sent 日志，永不进对话 claim 键
    空间、永不触发 Brain（方案 §2.3 方向契约）。
    """
    summary = event_type_summary(ev.raw)
    if ev.meta_event_type == "message_sent":
        proc._log("message_sent", 0, False, 0.0, event_type_summary=summary)
        proc.metrics.record_event("meta", "registered")
        return
    if ev.meta_event_type == "heartbeat":
        proc._log(
            "meta_event",
            0,
            False,
            0.0,
            event_type_summary=summary,
            extra={
                "heartbeat_online": ev.status_online,
                "heartbeat_good": ev.status_good,
            },
        )
        proc.metrics.record_event("meta", "registered")
        proc.metrics.record_event("meta", "heartbeat")
        return
    proc._log("meta_event", 0, False, 0.0, event_type_summary=summary)
    proc.metrics.record_event("meta", "registered")


# ---------------------------------------------------------------------------
# request 处置（方案 §4 C6 持久化处置状态机 + §2.3 事件子链）。


class RequestPolicy:
    """request 处置策略（N3 落 manual，N4 增 allowlist 自动同意）。

    默认 ``manual``——只落库待人工，处置动作不启用（生产口径）。
    ``allowlist`` 模式下，仅当申请方落在对应名单内才自动同意：
    好友申请看申请人 user_id、入群申请/邀请看申请人 user_id（群
    维度名单可另给 approve_group_ids 限定目标群）；名单外一律回落
    manual 待人工——策略只会自动同意、永不自动拒绝（拒绝是关系
    处置，后果不对称，留给人工/显式决策）。

    分类型覆盖（friend_mode / group_mode）：非 None 时按事件的
    request_type 选用对应模式，未给的类型回落基础 ``mode``。
    ``approve_all`` 只经分类型覆盖进入（基础 mode 仍只接受
    manual/allowlist）：有效模式为 approve_all 时不看名单直接
    同意。配置层进一步限定 group 不开放 approve_all（见
    companion.config.RequestsConfig），策略层本身不重复该限定。
    """

    _OVERRIDE_MODES = ("manual", "allowlist", "approve_all")

    def __init__(
        self,
        mode: str = "manual",
        *,
        approve_user_ids=(),
        approve_group_ids=(),
        friend_mode: str | None = None,
        group_mode: str | None = None,
    ):
        if mode not in ("manual", "allowlist"):
            raise ValueError(f"unsupported request policy mode: {mode!r}")
        if friend_mode is not None and friend_mode not in self._OVERRIDE_MODES:
            raise ValueError(f"unsupported request policy mode: {friend_mode!r}")
        if group_mode is not None and group_mode not in self._OVERRIDE_MODES:
            raise ValueError(f"unsupported request policy mode: {group_mode!r}")
        self.mode = mode
        self.friend_mode = friend_mode
        self.group_mode = group_mode
        self.approve_user_ids = frozenset(str(x) for x in approve_user_ids)
        self.approve_group_ids = frozenset(str(x) for x in approve_group_ids)

    def _effective_mode(self, ev) -> str:
        if ev.request_type == "friend" and self.friend_mode is not None:
            return self.friend_mode
        if ev.request_type == "group" and self.group_mode is not None:
            return self.group_mode
        return self.mode

    def has_auto(self) -> bool:
        """任一有效模式非 manual 时 True（存在自动同意的可能）。"""
        modes = [self.mode]
        if self.friend_mode is not None:
            modes.append(self.friend_mode)
        if self.group_mode is not None:
            modes.append(self.group_mode)
        return any(mode != "manual" for mode in modes)

    def decide(self, ev) -> str:
        effective = self._effective_mode(ev)
        if effective == "manual":
            return "manual"
        if effective == "approve_all":
            return "approve"
        if str(ev.user_id) not in self.approve_user_ids:
            return "manual"
        if (
            ev.request_type == "group"
            and self.approve_group_ids
            and str(ev.group_id) not in self.approve_group_ids
        ):
            return "manual"
        return "approve"


class RequestDeferred(Exception):
    """处置未达平台、须留待后续（配置修正/人工）的信号（Review P2-3）。

    携带将写入状态机的 result_code；handle_request 收到后按
    ambiguous 收口（不释放、不重做、不误记平台拒绝），修复配置
    后可由显式决策重新处置。通道抛出的其他异常仍按 handler_error
    收口。
    """

    def __init__(self, result_code: str):
        super().__init__(result_code)
        self.result_code = result_code


class GatewayRequestChannel:
    """request 处置的平台通道：经 ActionGateway 调用 L2 处置动作。

    好友申请走 set_friend_add_request、入群申请/邀请走
    set_group_add_request；operation_id 由处置唯一键派生（同一申请
    的重复处置尝试同号，Gateway 账本据此防重放）。

    结果映射（Review P2-3）：ok 与「平台已应答的 rejected」
    （raw 非空）原样返回；本地门禁拒绝（raw 为空，如 authorization_
    scope/disabled/rate_limited）与 simulated（dry_run/待确认）
    根本没到平台，抛 RequestDeferred 携 gateway_gate:<gate> /
    gateway_simulated:<gate>——绝不误记成平台拒绝的终态。

    授权上下文（主体 + 确认 + N4 的逐资源授权 AuthorizationContext）
    必须由接线层显式注入，本类不自造；生产接线由 create_app 按
    requests 策略装配：策略含自动同意模式时构造本通道，全 manual
    时不装配（app.state.request_channel 为 None）。处置动作的参数只有 flag（平台不透明凭证），网关按
    account 目标校验授权——授权范围由接线层在构造上下文时按其
    产品口径圈定，通道不放大也不缩小。注意授权是通道级的
    （Review P3-4）：需要按事件最小范围授权的接线层应按事件构造
    本通道（或为每类事件各构造一条），不要用一条 account 全量
    通道覆盖所有事件。
    """

    #: 可担任处置主体的白名单（与 gateway 的 L2 主体口径一致）。
    _ALLOWED_PRINCIPALS = frozenset(
        {PRINCIPAL_SYSTEM, PRINCIPAL_PRODUCT_COMMAND, PRINCIPAL_OPS_CLI}
    )

    def __init__(self, gateway, *, principal, confirmed, authorization):
        if principal not in self._ALLOWED_PRINCIPALS:
            raise ValueError(f"unsupported principal: {principal!r}")
        if not isinstance(authorization, AuthorizationContext) or not authorization.authorizer:
            raise ValueError("authorization context must be injected explicitly")
        self.gateway = gateway
        self.principal = principal
        self.confirmed = confirmed
        self.authorization = authorization

    async def dispose(self, ev, *, approve: bool):
        key = request_key(ev.self_id, ev.request_type, ev.sub_type, ev.flag)
        operation_id = "req-" + hashlib.sha256(
            "|".join(key).encode("utf-8")
        ).hexdigest()[:24]
        if ev.request_type == "friend":
            action = "set_friend_add_request"
            params = {"flag": ev.flag, "approve": approve}
        elif ev.request_type == "group":
            action = "set_group_add_request"
            params = {
                "flag": ev.flag,
                "sub_type": ev.sub_type or "add",
                "approve": approve,
            }
        else:
            raise ValueError(f"unsupported request_type: {ev.request_type!r}")
        result = self.gateway.invoke(
            action,
            params,
            principal=self.principal,
            confirmed=self.confirmed,
            operation_id=operation_id,
            authorization=self.authorization,
            identity=operation_id,  # 逐请求限流桶（Review P3-4）
        )
        if inspect.isawaitable(result):
            result = await result
        outcome = getattr(result, "outcome", None)
        gate = getattr(result, "gate", None) or "unknown"
        if outcome == "simulated":
            raise RequestDeferred(f"gateway_simulated:{gate}")
        if outcome == "rejected" and getattr(result, "raw", None) is None:
            raise RequestDeferred(f"gateway_gate:{gate}")
        return result


async def handle_request(proc, ev, *, store, channel, policy) -> None:
    """事件子链的 RequestHandle：原子占用 → 策略决策 → 处置/落终态。

    占用失败（重复投递或已被其他消费者占用）只记 duplicate 并返回，
    同一申请永不处置两次。manual 策略（或无通道）直接落 done /
    policy_manual 待人工。平台处置三态映射：ok → done（决策
    approved/rejected）；平台已应答的 rejected → failed /
    platform_rejected；ambiguous 或通道抛异常 → ambiguous /
    handler_error（不释放、不重做，转人工/显式决策），其中通道的
    RequestDeferred 携细分 result_code（本地门禁/模拟未达平台，
    不误记平台拒绝，Review P2-3）。异常只记日志、绝不外抛。
    """
    key = request_key(ev.self_id, ev.request_type, ev.sub_type, ev.flag)
    # 收口留痕的定位摘要（复审 P4）：complete 未生效的 warning
    # 必须能对账到具体请求行——记键的 self_id / 类型 / 子类型
    # 与 flag 的 sha256 摘要。flag 是平台申请凭证，只记摘要
    # 不记原文；摘要原料仅键分量，不带任何用户载荷内容。
    key_summary = (
        f"{key[0]}/{key[1]}/{key[2]}"
        " flag_sha256="
        + hashlib.sha256(key[3].encode("utf-8")).hexdigest()[:16]
    )
    summary = event_type_summary(ev.raw)
    if not store.claim_processing(key):
        proc.metrics.record_event("request", "duplicate")
        proc._log(
            "request",
            0,
            False,
            0.0,
            reason_code="duplicate_delivery",
            event_type_summary=summary,
        )
        return

    def complete(**kwargs) -> None:
        """落终态并核对结果（补审第三段 P3）：complete 返回 False
        说明行状态已被其他路径改写、本次收口未生效；complete 抛
        异常（如瞬时 SQLite 错误）同理收口未生效（复审 P4）——
        两种情形都只记 warning 留痕，绝不外抛（处置链的异常口
        径不变）。"""
        try:
            applied = store.complete(key, **kwargs)
        except Exception as exc:  # noqa: BLE001 — 收口异常只留痕不外抛
            logger.warning(
                "request complete not applied: request=%s"
                " status=%s result_code=%s error=%s",
                key_summary,
                kwargs.get("status"),
                kwargs.get("result_code"),
                type(exc).__name__,
            )
            return
        if not applied:
            logger.warning(
                "request complete not applied: request=%s"
                " status=%s result_code=%s",
                key_summary,
                kwargs.get("status"),
                kwargs.get("result_code"),
            )

    decision = policy.decide(ev) if policy is not None else "manual"
    if inspect.isawaitable(decision):
        decision = await decision
    if channel is None or decision == "manual":
        complete(
            status=REQUEST_STATUS_DONE,
            decision=DECISION_MANUAL,
            result_code=RESULT_POLICY_MANUAL,
        )
        proc._log(
            "request",
            0,
            False,
            0.0,
            reason_code=RESULT_POLICY_MANUAL,
            event_type_summary=summary,
        )
        proc.metrics.record_event("request", "handled")
        return
    approve = decision == "approve"
    decision_code = DECISION_APPROVED if approve else DECISION_REJECTED
    try:
        result = await channel.dispose(ev, approve=approve)
    except Exception as exc:  # noqa: BLE001 — 已派发与否不明，按 ambiguous 收口
        logger.error("request dispose failed: %s", type(exc).__name__)
        # RequestDeferred 携通道给出的细分代码（gateway_gate:* /
        # gateway_simulated:*，Review P2-3）；其余异常维持 handler_error。
        deferred_code = (
            exc.result_code if isinstance(exc, RequestDeferred) else RESULT_HANDLER_ERROR
        )
        complete(
            status=REQUEST_STATUS_AMBIGUOUS,
            result_code=deferred_code,
        )
        proc._log(
            "request",
            0,
            False,
            0.0,
            reason_code=deferred_code,
            event_type_summary=summary,
        )
        proc.metrics.record_event("request", "handled")
        return
    outcome = getattr(result, "outcome", None)
    if outcome == "ok":
        complete(status=REQUEST_STATUS_DONE, decision=decision_code)
        log_code = decision_code
    elif outcome == "rejected":
        complete(
            status=REQUEST_STATUS_FAILED,
            result_code=RESULT_PLATFORM_REJECTED,
        )
        log_code = RESULT_PLATFORM_REJECTED
    else:
        # ambiguous（含未知 outcome）：结果不明，不释放、不重做。
        complete(
            status=REQUEST_STATUS_AMBIGUOUS,
            result_code=RESULT_HANDLER_ERROR,
        )
        log_code = RESULT_HANDLER_ERROR
    proc._log(
        "request",
        0,
        False,
        0.0,
        reason_code=log_code,
        event_type_summary=summary,
    )
    proc.metrics.record_event("request", "handled")
