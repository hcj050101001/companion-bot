"""FastAPI OneBot v11 入口、验签、事件队列和 W1 处理链路。"""

import asyncio
import hashlib
import json
import logging
import os
import random
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from companion.adapters import Ignore, OneBotAdapter, RequestEvent
from companion.adapters.base import (
    REASON_NOT_MESSAGE,
    InboundEvent,
    MetaEvent,
    NoticeEvent,
    TextSegment,
)
from companion.adapters.gateway import (
    PRINCIPAL_SYSTEM,
    AuthorizationContext,
    SqliteOperationLedger,
    make_gateway_role_resolver,
)
from companion.adapters.onebot import event_type_summary, verify_onebot_signature
from companion.brain import FALLBACK_REPLY, CompanionBrain, DeepSeekVisionClient, create_llm_client
from companion.config import (
    ConfigValidationError,
    DateCountdownPolicyConfig,
    Settings,
    load_settings,
)
from companion.domain.claims import (
    CARE_OPT_IN_PHRASE,
    CARE_PAUSE_PHRASE,
    CARE_STOP_PHRASES,
)
from companion.domain.requests import REQUEST_TERMINAL_STATES, request_key
from companion.ingress.claims import LANE_GROUP, ClaimOutcome, classify_lane
from companion.memory import ConversationStore, Database, DeliveryLedger, SqliteClaimStore
from companion.memory.anniversaries import AnniversaryStore, AnniversaryStoreFull
from companion.memory.care import MAX_CONSENT, CarePreferences
from companion.memory.requests import SqliteRequestStore
from companion.multipart import (
    MAX_BUBBLES,
    identity_disclosure,
    is_identity_question,
    join_bubbles,
    parse_bubbles,
    requests_one_by_one,
    review_bubbles,
    split_plain_text,
)
from companion.ops.connection_health import ConnectionHealth
from companion.perception import UserMessage
from companion.pipeline import (
    STAGE_ORDER,
    DiagnosticsLogObserver,
    EventContext,
    MetricsObserver,
    StageMetrics,
    build_runner,
)
from companion.prompt_packs import PackRegistry, external_persona
from companion.quota import NORMAL_PER_USER_LIMIT, QuotaReservation, SendQuota
from companion.repetition import (
    TEASE_LINE,
    RepetitionSessions,
    is_light_tone,
    requests_stop_teasing,
)
from companion.safety import EmailAlerter, JsonlAlerter, ModelLLMJudge, SafetyGateway, crisis_reply
from companion.safety.care import CareSendGate
from companion.scheduler import CareScheduler, InProcessScheduler
from companion.services.delivery import DeliveryService
from companion.tool_loop import ToolLoopLimits
from companion.tools import HttpOneBotClient, OneBotClient
from companion.tools.current_time import (
    CLARIFY_REPLY,
    format_current_time,
    is_ambiguous_time_request,
    is_current_time_request,
)
from companion.tools.date_countdown import (
    UNAVAILABLE_REPLY as COUNTDOWN_UNAVAILABLE_REPLY,
)
from companion.tools.date_countdown import (
    date_countdown_tool,
    format_date_countdown,
    merge_anniversaries,
    normalize_anniversary_name,
    parse_anniversary_command,
)
from companion.tools.plugins import build_plugin_registry
from companion.tools.read_only import ReadOnlyRegistry
from companion.vision import (
    MEDIA_FIXED_REPLY,
    ImageFetcher,
    NapCatImageFetcher,
    PreparedImages,
    VisionService,
)

from . import events as ingress_events

MAX_BODY = 1024 * 1024

# 关怀控制命令（P6 接入，方案 §5.3 Admission 前置分支）：strip 后精确
# 匹配，在 judge 与入站限流之前处理，顺序固定为「先持久化撤回 → 使
# 待发关怀失效 → 再回复确认」。短语文案以 domain.claims 为单一事实
# 来源（其 CONTROL_PHRASES 负责 Stage 0 预筛分道，子串命中不构成
# 命令；此处只做精确匹配）。
CARE_OPT_IN_COMMAND = CARE_OPT_IN_PHRASE
CARE_PAUSE_COMMAND = CARE_PAUSE_PHRASE
CARE_STOP_COMMANDS = frozenset(CARE_STOP_PHRASES)
CARE_COMMANDS = frozenset({CARE_OPT_IN_COMMAND, CARE_PAUSE_COMMAND}) | CARE_STOP_COMMANDS
_CARE_CONSENT_TTL = MAX_CONSENT  # 单一事实来源：CarePreferences 的同意上限
_CARE_PAUSE_SPAN = timedelta(days=7)

# 纪念日管理命令（倒计时体验增强，Command Stage 前置分支）：触发与
# 解析在 companion.tools.date_countdown.parse_anniversary_command
# （确定性纯函数）；此处只定固定文案。非主人命中管理形态时回
# OWNER_ONLY 文案——不查存储、不确认任何条目存在性；存储失败回
# FAILED 文案，绝不假确认（与关怀命令同纪律）。
ANNIVERSARY_OWNER_ONLY_REPLY = (
    "纪念日的记录、查看和删除只能由主人本人来操作，我这边没法帮你处理。"
)
ANNIVERSARY_FORMAT_REPLY = (
    "格式是：记一下：纪念日名称 + 日期，比如「记一下：结婚纪念日 5月20日」，"
    "也可以写完整日期「记一下：春节 2027-02-17」。"
)
ANNIVERSARY_FULL_REPLY = "记得太多了（最多 50 条），先删掉一些再记新的吧。"
ANNIVERSARY_FAILED_REPLY = "纪念日保存失败，请稍后再试。"
ANNIVERSARY_REMOVE_FAILED_REPLY = "删除纪念日失败，请稍后再试。"
ANNIVERSARY_LIST_FAILED_REPLY = "纪念日清单读取失败，请稍后再试。"
# 命令失败文案映射：对 AnniversaryCommand 的全部 kind 全覆盖
# （add_invalid 的失败回退即格式口径本身）；消费侧再经 .get 兜底，
# except 块自身永不 KeyError（fix1 复审 P4）。
ANNIVERSARY_FAILURE_REPLIES = {
    "add": ANNIVERSARY_FAILED_REPLY,
    "add_invalid": ANNIVERSARY_FORMAT_REPLY,
    "remove": ANNIVERSARY_REMOVE_FAILED_REPLY,
    "list": ANNIVERSARY_LIST_FAILED_REPLY,
}
ANNIVERSARY_EMPTY_REPLY = (
    "你还没有记任何纪念日。可以对我说：记一下：结婚纪念日 5月20日。"
)


def _tool_limits_from_settings(settings) -> ToolLoopLimits:
    """工具循环上限的唯一读法：正式模型树优先（P1 校验与默认值都在
    那里）；手搓 Settings（config=None）时才回退到 app 字典中显式
    注入的 llm.tool_loop，非整数值一律回默认。"""
    config = getattr(settings, "config", None)
    if config is not None:
        loop = config.llm.tool_loop
        return ToolLoopLimits(
            max_tool_rounds=loop.max_tool_rounds,
            max_tool_calls=loop.max_tool_calls,
        )
    loop_settings = (settings.app.get("llm", {}) or {}).get("tool_loop", {}) or {}

    def _limit(key, default, ceiling):
        value = loop_settings.get(key, default)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= ceiling:
            return value
        return default

    return ToolLoopLimits(
        max_tool_rounds=_limit("max_tool_rounds", 2, 8),
        max_tool_calls=_limit("max_tool_calls", 3, 32),
    )


def _discover_napcat_image_roots(settings) -> tuple[Path, ...]:
    """视觉 NapCat 取图的本地根集合：配置根在前、自动发现根在后，去重保序。

    配置根取 media.vision.image_local_roots，相对路径相对
    settings.root 解析为绝对路径（不校验存在性，消费方逐文件
    判定）；自动发现根为部署根同级 napcat-v4 的 QQ 数据目录下
    ``nt_qq_*/nt_data`` 的现存目录（NapCat get_image 返回的本地
    缓存文件即落在此下）。glob 只在本函数内出现，装配与测试共用
    同一发现口径。
    """
    vision_cfg = (settings.app.get("media", {}) or {}).get("vision", {}) or {}
    roots: list[Path] = []
    for raw in vision_cfg.get("image_local_roots", ()) or ():
        if not str(raw).strip():
            # 空串经 Path 解析会落到 settings.root 本身（整个仓库
            # 成根、过宽）：直接跳过（确认复审 P4-4）。
            continue
        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute():
            candidate = settings.root / candidate
        if not candidate.exists():
            # 配置根不存在时仍保留在集合内（消费方逐文件判定），
            # 但留一行告警便于运维发现配错/未挂载（确认复审 P4-4）。
            logger.warning(
                "vision image root not found: code=root_missing path=%s", candidate
            )
        if candidate not in roots:
            roots.append(candidate)
    qq_dir = settings.root.parent / "napcat-v4" / "home" / ".config" / "QQ"
    if qq_dir.is_dir():
        for candidate in sorted(qq_dir.glob("nt_qq_*/nt_data")):
            if candidate.is_dir() and candidate not in roots:
                roots.append(candidate)
    return tuple(roots)


def _wire_vision_napcat_fetcher(vision_service, sender, settings) -> bool:
    """为视觉服务接线 NapCat 介导取图（主路径）；接成返回 True。

    只在 vision_service 存在且尚未接线、sender 的 actions.media
    暴露 callable 的 get_image、且本地根非空时赋值；任一条件不
    满足都保持 napcat_fetcher 为 None（直连取图兜底口径不变）。
    vision_cfg 在此以与 VisionService 装配点相同的表达式从
    settings.app 重读。
    """
    if vision_service is None or getattr(vision_service, "napcat_fetcher", None) is not None:
        return False
    get_image = getattr(
        getattr(getattr(sender, "actions", None), "media", None), "get_image", None
    )
    if not callable(get_image):
        # 视觉在、主路径却接不上：留一行结果码，避免主路径静默
        # 不生效只能靠真机发现（确认复审 P4-3）。
        logger.info("vision napcat fetch not wired: code=no_get_image")
        return False
    roots = _discover_napcat_image_roots(settings)
    if not roots:
        logger.warning("vision napcat fetch not wired: code=no_roots")
        return False
    vision_cfg = (settings.app.get("media", {}) or {}).get("vision", {}) or {}
    vision_service.napcat_fetcher = NapCatImageFetcher(
        get_image,
        roots=roots,
        timeout=float(vision_cfg.get("napcat_timeout_seconds", 8)),
    )
    return True


# Pacing: the existing initial delay before the first bubble, then 0.8-1.5s between
# bubbles. The total pacing budget across one reply is capped so a stream cannot hang.
BUBBLE_DELAY_MIN = 0.8
BUBBLE_DELAY_MAX = 1.5
MAX_TOTAL_PACING_SECONDS = 10.0
# Bounded ordinary stream concurrency; crisis and special routes use the priority lane.
MAX_ACTIVE_STREAMS = 64
STREAM_CAPACITY_WAIT_SECONDS = 2.0

logger = logging.getLogger("companion.events")


class BodyLimitMiddleware:
    """限制请求体大小为 1 MiB，避免完整读取超限 body。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = next(
            (v.decode() for k, v in scope.get("headers", []) if k.lower() == b"content-length"), ""
        )
        if length.isdigit() and int(length) > MAX_BODY:
            await JSONResponse({"detail": "request body too large"}, status_code=413)(
                scope, receive, send
            )
            return
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY:
                await JSONResponse({"detail": "request body too large"}, status_code=413)(
                    scope, receive, send
                )
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class EventBus:
    """三道有界入站队列（方案 §5.3 并发与过载预算）+ N3 事件三道。

    ``queue`` 是普通道（保留旧属性名，兼容既有调用与测试注入）；
    control 道优先于普通道出队，critical 道由专职执行池处理。
    容量在接受边界以 put_nowait 预留，满即 503，永不阻塞发布者。
    N3 起新增 group/notice/request 三道事件队列（方案 §4 C7/C14）：
    与消息三道容量隔离、由 consume_event_lane 独立消费，事件风暴
    不得挤占私聊/危机通道；request 道另有落库 503 口径（见 webhook）。
    """

    def __init__(
        self,
        ordinary_capacity: int = 200,
        control_capacity: int = 50,
        critical_capacity: int = 20,
        group_capacity: int = 200,
        notice_capacity: int = 200,
        request_capacity: int = 50,
    ):
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=ordinary_capacity)
        self.control_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=control_capacity)
        self.critical_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=critical_capacity)
        self.group_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=group_capacity)
        self.notice_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=notice_capacity)
        self.request_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=request_capacity)

    def lane_queue(self, lane: str) -> asyncio.Queue:
        """按道取队列；普通道动态读 self.queue（测试可注入替换）。"""
        if lane == "critical":
            return self.critical_queue
        if lane == "control":
            return self.control_queue
        if lane == "group":
            return self.group_queue
        if lane == "notice":
            return self.notice_queue
        if lane == "request":
            return self.request_queue
        return self.queue

    def lane_full(self, lane: str) -> bool:
        return self.lane_queue(lane).full()

    def try_publish(self, lane: str, event: dict) -> bool:
        """非阻塞入队（容量预留）：满则返回 False，不等待、不丢弃已排队事件。"""
        try:
            self.lane_queue(lane).put_nowait(event)
            return True
        except asyncio.QueueFull:
            return False

    async def publish(self, event: dict) -> None:
        """异步发布事件（旧接口：普通道阻塞式入队）。"""
        await self.queue.put(event)

    async def consume(self) -> tuple[str, dict]:
        """普通/control 两道的优先出队：control > ordinary。

        critical 道由独立消费循环 consume_critical 处理（专职池调度
        隔离，普通池占满不得挡住危机）。用短间隔轮询而非多路
        Queue.get 竞速：取消一个已唤醒的 getter 存在丢事件竞态，
        轮询无此风险，5ms 延迟对本服务可忽略。
        """
        while True:
            for lane in ("control", "ordinary"):
                queue = self.lane_queue(lane)
                if not queue.empty():
                    return lane, queue.get_nowait()
            await asyncio.sleep(0.005)

    async def consume_critical(self) -> dict:
        """critical 道出队（独立循环专用）。"""
        return await self.critical_queue.get()

    async def consume_event_lane(self) -> tuple[str, dict]:
        """N3 事件三道的优先出队：request > group > notice。

        request 是副作用处置、时效最强，优先于纯登记类事件；group
        观察次之；notice 登记量大且可合并，居末。与 consume 同样用
        5ms 轮询而非多路 Queue.get 竞速（避免取消唤醒 getter 丢事件）。
        """
        while True:
            for lane in ("request", "group", "notice"):
                queue = self.lane_queue(lane)
                if not queue.empty():
                    return lane, queue.get_nowait()
            await asyncio.sleep(0.005)

    def task_done(self, lane: str) -> None:
        self.lane_queue(lane).task_done()

    async def join(self) -> None:
        """等待全部队列出队完毕（消费者每出队一个事件即 task_done）。"""
        await asyncio.gather(
            self.queue.join(),
            self.control_queue.join(),
            self.critical_queue.join(),
            self.group_queue.join(),
            self.notice_queue.join(),
            self.request_queue.join(),
        )


def verify_signature(body: bytes, signature: str | None, secret: str | None) -> bool:
    """验证 OneBot X-Signature（实现归适配器层，此处仅保留兼容入口）。"""
    return verify_onebot_signature(body, signature, secret)


class RateLimiter:
    """滑动窗口限流器，按用户计数。"""

    def __init__(self, limit: int = 6, window_seconds: int = 60):
        self.limit, self.window = limit, window_seconds
        self.timestamps: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, user_id: str, now: float | None = None) -> bool:
        """当前窗口尚未达到限制时消费一个额度。"""
        current = time.monotonic() if now is None else now
        bucket = self.timestamps[str(user_id)]
        while bucket and bucket[0] <= current - self.window:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(current)
        return True


@dataclass(frozen=True)
class SendOutcome:
    """One outbound attempt: sent、结果不明（uncertain），或回执被拒（blocked）。

    blocked 专指 DeliveryService 回执 rejected（额度/状态类明确
    拒绝），与 uncertain（传输异常/结果不明、平台可能已接受）
    严格区分——_deliver 据此分流 drop_reason rejected / transport。
    """

    sent: bool
    uncertain: bool = False
    blocked: bool = False


#: 视觉上下文接力的存活窗口（秒，monotonic 口径）：图片回合的
#: 视觉生成常慢于用户补发文本的速度，补发回合在窗口内可接管
#: 已备好的图片；窗口外图片语境已陈旧，不再接力。
VISION_HANDOFF_TTL_SECONDS = 120.0


@dataclass
class _VisionHandoff:
    """每用户至多一条的视觉接力记录（进程内）。

    来源回合在备好图片后登记；后续回合接管时先置 ``consumed``
    再立即 pop 字典条目（接管即记录终态，确认复审 R4），来源
    回合凭自己持有的同一对象引用判读该标志——字典条目已不在，
    来源回合仍能正确判定自己已被接力、不得再投递自己的回复。

    ``delivery_generation`` 是登记回合的投递代次：接管只允许
    发生在代次严格更大的后续回合（代次顺序守卫），后到的图片
    不得被先到回合的迟到 Respond 误接管。``data_urls`` 是完整
    base64 载荷，``repr=False`` 防其随对象 repr 进日志/异常上报
    （与 PreparedImages 的口径一致）。

    ``crisis_disposed`` 是内容筛查（F7）的危机处置协调标记：
    来源回合与接管回合对同一图片各筛查一次，任一回合判定危机
    时经 _claim_vision_crisis 在本记录上抢占处置权（检查与置
    位无 await、事件循环内原子），先到者执行危机协议、后到者
    让位——已判出的危机结论不因接管被丢弃，也不双发危机话术。

    ``observation_turn_ids`` 是该图已（或正在）落存观察行的回
    合 id 集（复审 r3 P3）：落存方写库前把自己的 turn_id 登记
    进来，迟到的危机认领据此定位并补偿删除已落存行；落存方
    写库后复查 ``crisis_disposed`` 并自删兜底，覆盖认领删除
    先于写库执行而扑空的竞态——两方向合起来保证危机图描述
    不因认领迟到而残留于对话记忆。

    ``image_hash`` 是图片内容同一性哈希（data_urls 载荷的
    sha256，登记时计算，复审 r4 P3）：同一张图每次上传都是
    独立记录、observation_turn_ids 互不可见，认领方据哈希经
    store 的同一性索引跨上传定位同图全部落存行；哈希为空
    （无图片载荷）时同一性不可用，补偿删除走契约优先的全删
    兜底口径（见 ConversationStore 的认领删除）。
    """

    data_urls: tuple = field(repr=False)
    received: int
    created_at: float
    pack_generation: int
    safety_epoch: int
    delivery_generation: int
    fetch_codes: tuple = ()
    consumed: bool = False
    crisis_disposed: bool = False
    image_hash: str = ""
    observation_turn_ids: set = field(default_factory=set)


class EventProcessor:
    """串联私聊、安全检测、brain、记忆、配额、节奏、打断和发送。"""

    # sender / send_quota / delivery 三者保持同步：运行期替换 sender 或
    # send_quota（既有测试注入方式）时，DeliveryService 随之指向新对象，
    # 不继续走构造时的旧发送端或旧记账。
    @property
    def sender(self):
        return self._sender

    @sender.setter
    def sender(self, value):
        self._sender = value
        delivery = getattr(self, "_delivery", None)
        if delivery is not None:
            delivery.sender = value

    @property
    def send_quota(self):
        return self._send_quota

    @send_quota.setter
    def send_quota(self, value):
        old = getattr(self, "_send_quota", None)
        self._send_quota = value
        # 真实客户端的网关在构造时绑定了旧 quota：仍指向旧对象时一并
        # 换绑，否则 DeliveryService 的预留与网关记账会落到两个账本
        # （P5 Review #5）。无 gateway 的测试 sender 不受影响。
        sender = getattr(self, "_sender", None)
        gateway = getattr(sender, "gateway", None)
        if gateway is not None and getattr(gateway, "quota", None) is old:
            gateway.quota = value
            if getattr(sender, "quota", None) is old:
                sender.quota = value
        delivery = getattr(self, "_delivery", None)
        if delivery is not None:
            delivery.quota = value

    @property
    def delivery(self):
        return self._delivery

    @delivery.setter
    def delivery(self, value):
        self._delivery = value

    def __init__(
        self,
        settings: Settings,
        database: Database | None = None,
        conversation: ConversationStore | None = None,
        ledger: DeliveryLedger | None = None,
        delivery_service: DeliveryService | None = None,
        brain: CompanionBrain | None = None,
        sender: OneBotClient | None = None,
        alerter=None,
        limiter: RateLimiter | None = None,
        sleep=asyncio.sleep,
        random_uniform=random.uniform,
        read_only_registry: ReadOnlyRegistry | None = None,
        send_quota: SendQuota | None = None,
        care_preferences: CarePreferences | None = None,
        care_clock=None,
        vision_service=None,
        group_responder=None,
        max_active_streams: int = MAX_ACTIVE_STREAMS,
        stream_wait_seconds: float = STREAM_CAPACITY_WAIT_SECONDS,
        anniversary_store: AnniversaryStore | None = None,
    ):
        self.settings = settings
        self.database = database or Database(
            settings.root / settings.app.get("sqlite_path", "data/memory.db")
        )
        self.conversation = conversation or ConversationStore(self.database)
        # 纪念日对话存储（倒计时体验增强）：与 conversation 同库，
        # 主人经 Command Stage 的管理命令维护，查询经工具 provider
        # 合并视图读取。注入优先（测试可换独立库）。
        self.anniversary_store = anniversary_store or AnniversaryStore(self.database)
        self.ledger = ledger or DeliveryLedger(self.database)
        llm_settings = settings.app.get("llm", {})
        tool_limits = _tool_limits_from_settings(settings)
        llm = create_llm_client(
            str(llm_settings.get("provider", "deepseek")),
            tool_limits=tool_limits,
            secret_provider=settings.secret_provider,
        )
        self.brain = brain or CompanionBrain(settings.persona, llm)
        # 注入 brain 的上限对齐（E 路修复 1）：新建 llm 已由
        # create_llm_client(tool_limits=...) 注入 settings 口径；外部
        # 注入的 brain 若其 client 带 tool_limits 属性，统一以同一口径
        # 覆盖，避免静默沿用客户端默认上限。无该属性的 client（如
        # EchoStub）不强加，保持其原有形态。
        _brain_client = getattr(self.brain, "client", None)
        if _brain_client is not None and hasattr(_brain_client, "tool_limits"):
            _brain_client.tool_limits = tool_limits
        # 视觉服务与 brain/sender 同为可注入依赖；关或非 deepseek provider
        # 时不存在、媒体行为与旧版逐字一致。
        if vision_service is not None:
            self.vision_service = vision_service
        else:
            vision_cfg = (settings.app.get("media", {}) or {}).get("vision", {}) or {}
            if vision_cfg.get("enabled", True) and str(
                llm_settings.get("provider", "deepseek")
            ) == "deepseek":
                self.vision_service = VisionService(
                    client=DeepSeekVisionClient(
                        model=str(vision_cfg.get("model", "deepseek-flash")),
                        timeout=float(vision_cfg.get("timeout_seconds", 30)),
                        secret_provider=settings.secret_provider,
                    ),
                    fetcher=ImageFetcher(),
                    max_images=int(vision_cfg.get("max_images", 3)),
                    max_bytes=int(vision_cfg.get("max_image_bytes", 10485760)),
                )
            else:
                self.vision_service = None
        tool_settings = settings.app.get("read_only_tools", {})
        self.read_only_registry = read_only_registry or ReadOnlyRegistry(
            enabled=tool_settings.get("enabled", False), policies=tool_settings.get("policies", {})
        )
        if read_only_registry is None:
            # 工具一律以插件形态登记（§5.5）：政策与渲染随插件声明，
            # 门控仍由 ReadOnlyRegistry/ToolGateway 统一执行。搜索
            # 插件的密钥经 settings.secret_provider 注入（P5 接线），
            # 与直注册路径时 web_search_tool 的取密钥口径一致。
            # 倒计时纪念日清单优先取已校验的配置树（load_settings
            # 恒先 validate_app_config，条目已是 AnniversaryEntry）；
            # 无 config 树的回退路径把原始 dict 的 date_countdown
            # 整节经 DateCountdownPolicyConfig 校验，与配置树同一
            # 口径（单条形态、50 条上限、规范化重名、兄弟字段类型/
            # 边界与未知键都在此 fail-fast），失败在装配期上抛，
            # 不吞、不降级为空清单。节本身为 None 或非映射时同样
            # 以 ConfigValidationError fail-fast（不让 .get 链抛
            # 无键路径的 AttributeError）。回退路径校验失败时与
            # 配置树同纪律：只报键路径、不回显条目值——pydantic
            # ValidationError 会内嵌 input/input_value，故捕获后
            # 改抛 ConfigValidationError（include_input=False）。
            if settings.config is not None:
                countdown_anniversaries = (
                    settings.config.read_only_tools.policies.date_countdown.anniversaries
                )
            else:
                policies_node = tool_settings.get("policies", {})
                section_node = (
                    policies_node.get("date_countdown", {})
                    if isinstance(policies_node, dict)
                    else None
                )
                if not isinstance(section_node, dict):
                    raise ConfigValidationError(
                        "Invalid configuration: "
                        "read_only_tools.policies.date_countdown: "
                        "section must be a mapping",
                        paths=("read_only_tools.policies.date_countdown",),
                    )
                try:
                    countdown_anniversaries = DateCountdownPolicyConfig.model_validate(
                        section_node
                    ).anniversaries
                except ValidationError as exc:
                    errors = exc.errors(include_input=False)
                    prefix = "read_only_tools.policies.date_countdown"
                    paths = tuple(
                        prefix + "." + ".".join(str(part) for part in error["loc"])
                        if error["loc"]
                        else prefix
                        for error in errors
                    )
                    details = "; ".join(
                        f"{path}: {error['msg']}"
                        for path, error in zip(paths, errors)
                    )
                    raise ConfigValidationError(
                        "Invalid configuration: " + details, paths=paths
                    ) from None
            self._countdown_config_entries = tuple(countdown_anniversaries)
            anniversary_store = self.anniversary_store

            async def _countdown_entries_provider():
                # 只供存储部分：与静态清单的合并及按来源可见性
                # （存储条目仅主人档位可见）在工具内完成，查询时
                # 实时读取（主人刚记下的条目立刻可查）。
                return await anniversary_store.list_entries()

            self.plugin_registry = build_plugin_registry(
                self.read_only_registry,
                web_search_config={
                    "content_extraction_enabled": tool_settings.get("policies", {}).get(
                        "web_search", {}
                    ).get("content_extraction_enabled", False)
                },
                secret_provider=settings.secret_provider,
                countdown_anniversaries=countdown_anniversaries,
                countdown_anniversaries_provider=_countdown_entries_provider,
                countdown_owner_ids=frozenset(
                    str(x) for x in settings.app.get("allowed_private_user_ids", [])
                ),
            )
        else:
            # 注入契约：外部注入的 registry 须自行经 PluginRegistry
            # 登记工具，插件政策（direct 渲染/终结回复/待合成标记）
            # 才生效；裸 register 的工具只有 invoke 级门控，结果一律
            # 按普通数据回喂（disposition=feed）。注入时插件生命
            # 周期（on_load/on_unload）由注入方自行托管，app lifespan
            # 只托管自建路径的 plugin_registry。
            self.plugin_registry = None
            # 注入路径无 provider 注入，命令面合并视图只取 config
            # 静态清单（存储条目仍经 anniversary_store 增删查）；
            # 无正式配置树时为空清单。
            self._countdown_config_entries = (
                tuple(
                    settings.config.read_only_tools.policies.date_countdown.anniversaries
                )
                if settings.config is not None
                else ()
            )
        self.send_quota = send_quota or SendQuota()
        sender_policies = {
            "send_private_msg": {
                "enabled": True,
                # Shared normal policy: 18 attempts per user per rolling 60s.
                "rate_limit": NORMAL_PER_USER_LIMIT,
                **settings.app.get("tools", {}).get("send_private_msg", {}),
            },
            "alert_private_msg": {
                **settings.app.get("tools", {}).get("alert_private_msg", {}),
                "enabled": bool(os.getenv("ALERT_QQ", "").strip())
                and settings.app.get("tools", {})
                .get("alert_private_msg", {})
                .get("enabled", True),
            },
            "send_group_msg": settings.app.get("tools", {}).get("group_chat", {}),
            "qzone": settings.app.get("tools", {}).get("qzone", {}),
            "friend_management": settings.app.get("tools", {}).get("friend_management", {}),
            "get_image": {
                # 视觉 NapCat 介导取图（本地缓存查询、非外网下载）：
                # 网关默认 6 次/60 秒是给外发动作的保守值，而视觉
                # 单回合尝试上限即 5 次，两条多图消息就打满同一
                # identity 桶、主路径静默回落直连（确认复审 P2-1）
                # ——显式放宽到与入站限流同量级。
                "enabled": True,
                "rate_limit": 60,
                "window_seconds": 60,
            },
        }
        # actions 覆盖（方案 C11）：config 的 actions.overrides 逐条把
        # 非 None 的 enabled/dry_run 并入同一 policies dict（键为动作
        # 名），在既有各键之后合入——覆盖同名键时以覆盖值为准。
        action_overrides = (settings.app.get("actions", {}) or {}).get("overrides", {}) or {}
        for action_name, override in action_overrides.items():
            if not isinstance(override, dict):
                continue
            merged_policy = dict(sender_policies.get(action_name, {}))
            for field_name in ("enabled", "dry_run"):
                if override.get(field_name) is not None:
                    merged_policy[field_name] = override[field_name]
            sender_policies[action_name] = merged_policy
        # L3 受控解锁名单（N6，方案 §5）：config 的 actions.l3_unlock
        # 在进程启动构造网关时快照注入（默认空 = 全硬禁）；变更须
        # 重启，不进热更新白名单。运行时服务内无 L3 调用点，名单在
        # 生产配置中保持空——此处接线只保证运维显式配置时口径贯通。
        # 值路径的校验在 load_settings 的 validate_app_config（非法
        # 名单启动即失败）；此处读兼容 dict 与全仓其他设置同口径，
        # 且残余的未校验注入也 fail-closed（网关构造对非 L3 名
        # ValueError、名单经 frozenset 去重）——N6 Review P4-1 裁决。
        l3_unlock = (settings.app.get("actions", {}) or {}).get("l3_unlock", []) or []
        # 文件域资源边界（补审第二段 P2）：网关步骤 d 的路径语义
        # 校验须有 roots 与相对路径基准——生产装配在此显式注入
        # 专用目录 data/files（出站本地文件的唯一合法来源；数据库
        # 与生产配置不在其内）。文件域动作目前默认全关，本注入只
        # 先行钉死边界：日后启用任一文件动作时，相对/绝对路径都
        # 只能落在此目录内，../ 穿越在网关即被拒。
        file_resource_root = settings.root / "data" / "files"
        self.sender = sender or HttpOneBotClient(
            str(settings.app.get("napcat_base_url", "http://127.0.0.1:3000")),
            policies=sender_policies,
            quota=self.send_quota,
            l3_unlocked=frozenset(l3_unlock),
            resource_roots=(str(file_resource_root),),
            resource_base_dir=str(file_resource_root),
            # operation 账本落库（方案 §3.2）：与 claim 同库不同表，
            # 重启后 done 记录仍可查、防重放不丢。
            operation_ledger=SqliteOperationLedger(
                str(settings.root / settings.app.get("sqlite_path", "data/memory.db"))
            ),
        )
        # 群回复产品开关（N4，方案 §6 N4）：tools.group_chat.reply_enabled，
        # 默认关。开时事件道群分支走 handle_group_message 的回复通路
        # （@ 触发 + 双维主体 + DeliveryService 群通道），且默认自建
        # 的 DeliveryService 群门同步打开；注入的 delivery_service
        # 由注入方自负其群门口径。responder 装配口径（2026-10-31
        # 产品接线）：外部注入优先（装配层显式行为，不受开关影响）；
        # 未注入且开关开时构造默认生产 responder（见下方闭包的
        # 上下文隔离口径）；未注入且开关关时恒 None。事件道消费
        # responder 时另以 group_reply_enabled 门控（见 create_app
        # 的 event_consumer），故开关关时注入也不会被消费，群分支
        # 只观察、与接线前逐字一致。
        group_chat_settings = settings.app.get("tools", {}).get("group_chat", {}) or {}
        self.group_reply_enabled = bool(group_chat_settings.get("reply_enabled", False))
        # 群回复框架级准入门配置（补审第二段 P3）：默认只拒
        # person=system 自触发；allowed_persons / allowed_roles
        # 缺省 None = 不按名单收窄（「群员 @ 即触发」为已拍板的
        # 产品口径，本门只补框架下限）。三键均只在启动时读取，
        # 由事件消费者逐事件透传给 handle_group_message。
        # 下限兜底（复审 P4）：此处直读 app dict（不经
        # GroupChatToolConfig 校验），有效名单恒并入 "system"——
        # 配置给出空名单/漏 system 时自触发拦截不失效（模型层
        # validator 另在 load_settings 路径上 fail fast）。
        self.group_reply_blocked_persons = tuple(
            dict.fromkeys(
                (
                    "system",
                    *(
                        str(x)
                        for x in group_chat_settings.get(
                            "reply_blocked_persons", ("system",)
                        )
                    ),
                )
            )
        )
        _reply_allowed_persons = group_chat_settings.get("reply_allowed_persons")
        self.group_reply_allowed_persons = (
            frozenset(str(x) for x in _reply_allowed_persons)
            if _reply_allowed_persons is not None
            else None
        )
        _reply_allowed_roles = group_chat_settings.get("reply_allowed_roles")
        self.group_reply_allowed_roles = (
            frozenset(str(x) for x in _reply_allowed_roles)
            if _reply_allowed_roles is not None
            else None
        )
        if group_responder is not None:
            self.group_responder = group_responder
        elif self.group_reply_enabled:
            self.group_responder = self._default_group_responder()
        else:
            self.group_responder = None
        # L2 角色复核接线（N4）：自建 sender 的网关挂生产角色解析器
        # （经其自身 L0 查询复核机器人群内角色）；注入 sender 时不
        # 代为改装其网关，角色门保持 fail closed。
        if sender is None:
            self.sender.action_gateway.role_resolver = make_gateway_role_resolver(
                self.sender.action_gateway
            )
            # 视觉 NapCat 取图接线：自建 sender 的 actions.media
            # .get_image 为视觉服务挂 NapCat 介导取图（主路径，直连
            # 兜底不变）；注入 sender 时与 role_resolver 同口径，不
            # 代为改装其视觉服务，接线由注入方自行负责。
            _wire_vision_napcat_fetcher(self.vision_service, self.sender, settings)
        # 投递契约独占（方案 §5.2）：额度预留与发送回执统一经 DeliveryService。
        self.delivery = delivery_service or DeliveryService(
            self.sender, self.send_quota, group_delivery_enabled=self.group_reply_enabled
        )
        # 平台边界：本处理器的 OneBot 适配器（规范化/验签经此，发送迁移属 P5）。
        self.adapter = OneBotAdapter(client=self.sender)
        jsonl_alerter = JsonlAlerter(
            settings.root / settings.app.get("logs_dir", "logs") / "alerts.jsonl"
        )
        self.alerter = alerter or EmailAlerter(
            jsonl_alerter, secret_provider=settings.secret_provider
        )
        self.safety = SafetyGateway(
            settings.safety.get("crisis_keywords", []),
            ModelLLMJudge(llm),
            settings.safety.get("categories", []),
        )
        # 关怀控制面（P6 接入）：偏好存储未注入时关怀命令面不存在、
        # 忘掉不扩围，行为与接线前完全一致。care_gate / care_scheduler
        # 由 create_app 在 DeliveryService 就绪后接线（发送门必须与回复
        # 路径共享同一 DeliveryService 与额度账本，见 P5 注记）；测试
        # 亦可直接赋值这三个属性，Admission 每事件实时读取。
        self.care_preferences = care_preferences
        self.care_clock = care_clock or (lambda: datetime.now(timezone.utc))
        self.care_gate = None
        self.care_scheduler = None
        self.limiter = limiter or RateLimiter(int(settings.app.get("requests_per_minute", 6)))
        delay = settings.app.get("response_delay_seconds", {})
        self.delay_min, self.delay_max = float(delay.get("min", 1)), float(delay.get("max", 4))
        self.sleep, self.random_uniform = sleep, random_uniform
        self.allowed = {str(x) for x in settings.app.get("allowed_private_user_ids", [])}
        self.alert_qq = os.getenv("ALERT_QQ", "").strip()
        self.crisis_history = {}
        self.crisis_window = 3600
        # 关怀阻塞时钟：每用户最近一次危机事件的时刻（进程内，重启
        # 重置——已登记于 docs/state-ownership.md）。与 crisis_history
        # 的告警去重锚点分开，互不污染。
        self._last_crisis_at: dict[str, float] = {}
        self.log_path = settings.root / settings.app.get("logs_dir", "logs") / "events.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.seen_message_ids: set[str] = set()
        self.pending_message_ids: set[str] = set()
        self.onboarding_copy = settings.root / "config/onboarding.md"
        self.pack_registry = PackRegistry()
        self.repetition = RepetitionSessions()
        self._state_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._send_slots: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(1)
        )
        self._tasks: set[asyncio.Task] = set()
        self._ordinary_streams: dict[str, asyncio.Task] = {}
        self._safety_epoch: dict[str, int] = defaultdict(int)
        # 视觉上下文接力：每用户至多一条（见 _VisionHandoff）。
        self._vision_handoff: dict[str, _VisionHandoff] = {}
        # 视觉投递中标记（每用户计数，确认复审 R1）：视觉回合在
        # _stage_deliver 内投递未落定期间置位，_take_vision_handoff
        # 见置位拒绝接管，防来源投递收尾未定即被抢接管造成双回复；
        # 计数以容同用户并发视觉回合各自进出不互相误清。
        self._vision_delivering: dict[str, int] = {}
        # 视觉已服务代次水位（每用户已服务图片的最大投递代次，
        # 确认复审第二轮 P4-5）：接管成功或来源回合投递收尾确认
        # 有发出而清除自有接力记录时以该记录代次抬升；登记时
        # 代次不大于水位的迟到旧图片一律拒绝——已服务过的图片
        # 不被更旧图片补回复。登记于 docs/state-ownership.md。
        self._vision_served_generation: dict[str, int] = {}
        self._onboarding_claims: set[str] = set()
        self._stream_capacity = asyncio.Semaphore(max_active_streams)
        self._stream_wait_seconds = stream_wait_seconds
        profile = settings.app.get("persona_profile", {})
        name = profile.get("display_name") or profile.get("name") or "阿年"
        llm_settings = settings.app.get("llm", {})
        model = llm_settings.get("display_name") or llm_settings.get("provider") or "deepseek"
        self.identity_name = str(name)
        self.identity_model = str(model)
        self.identity_fallback = identity_disclosure(name, model)
        # 流水线（方案 §5.3）：Stage 链在构造时按模块级 STAGE_ORDER 组装，
        # 顺序即契约；judge 并发与等待预算在此集中登记（信号量 2 + 等待
        # 截止默认 5s，超时按 JudgeUnavailable 的 D1 保守口径处理）。
        pipeline_settings = settings.app.get("pipeline", {}) or {}
        self._judge_semaphore = asyncio.Semaphore(
            int(pipeline_settings.get("judge_concurrency", 2))
        )
        self._judge_wait_seconds = float(pipeline_settings.get("judge_wait_seconds", 5.0))
        # 统一观测出口（方案 §5.10）：进程内聚合指标 + 工具诊断日志观察者。
        self.metrics = StageMetrics()
        # 已知工具名集合（D 路 observers 的 known_tools 契约）：自建路径
        # 取插件注册表登记名；外部注入 registry 时取其已登记工具名。
        # 未知工具名在指标/诊断出口会被折叠为 unknown，防模型自造
        # 工具名打爆标签基数。
        if self.plugin_registry is not None:
            known_tools = frozenset(self.plugin_registry.names())
        else:
            known_tools = frozenset(getattr(self.read_only_registry, "_tools", {}) or {})
        self._runner = build_runner(
            STAGE_ORDER,
            observers=[
                self._observe_stage,
                MetricsObserver(self.metrics, known_tools=known_tools),
                DiagnosticsLogObserver(self._emit_tool_diagnostic, known_tools=known_tools),
            ],
        )

    def _default_group_responder(self):
        """默认生产群 responder（开关开且未注入时装配）。

        群链上下文隔离口径（方案 §4 C3/C17）：只把触发文本交给
        brain 的基础人设生成——空 history（不带任何私聊会话记忆）、
        不传 persona（不带任何用户级 prompt pack）、不调工具。文本
        取事件 TextSegment 拼接：AtSegment 的 @ 码不在其中，天然不
        混入 prompt；拼接为空（纯 @ 无正文）时回落 ctx.text 原样。
        生成异常不在此吞：上抛由 handle_group_message 的既有 try
        兜底记 reply_error，绝不造假回复。
        """

        async def _respond(ctx) -> str:
            text = "".join(
                segment.text
                for segment in ctx.event.segments
                if isinstance(segment, TextSegment)
            ).strip()
            if not text:
                text = ctx.text
            return await self.brain.respond(text, [])

        return _respond

    def care_whitelist(self, user_id: str) -> bool:
        """关怀门白名单回调：配置名单（care_known）即白名单（§3.1）。"""
        return str(user_id) in self.allowed

    def current_care_safety(self, user_id: str):
        """关怀门发送前一刻的实时安全回调（不信任 Admission 时刻快照）。

        返回 (gateway, allowed, revision)：allowed 表示该用户当前无进
        行中的危机处置（crisis_window 内有危机记录即 False）；revision
        取 safety epoch——危机与忘掉都会推进它，门在 detect 前后两次
        读取不一致即放弃本次发送。
        """
        user_id = str(user_id)
        # 阻塞判据用独立的关怀阻塞时钟（每次危机事件都推进，含告警
        # await 窗口内），不读 crisis_history 的告警去重锚点——锚点
        # 只在 first/upgrade/窗口过期时重置，拿它判阻塞会在持续危机
        # 时提前放行（P6 Review 第 2 轮）。
        last_crisis = self._last_crisis_at.get(user_id)
        allowed = last_crisis is None or time.time() - last_crisis >= self.crisis_window
        return self.safety, allowed, self._safety_epoch[user_id]

    def _invalidate_care(self, user_id: str) -> None:
        """使该用户的待发关怀失效（调度器未接线时为空操作）。"""
        scheduler = getattr(self, "care_scheduler", None)
        if scheduler is not None:
            scheduler.invalidate(str(user_id))

    def _apply_care_command(self, user_id: str, command: str) -> str:
        """关怀控制命令的持久化半段：先落库、再失效待发，返回确认文案。

        只做持久化与待发失效、不发送；确认回复由 Admission 在本方法
        返回后发出，顺序即「持久化撤回 → 待发失效 → 回复确认」（方案
        §5.3 对撞第 2 轮修正）。持久化抛错时异常上抛，由 Admission
        换成固定失败文案，绝不假确认。
        """
        prefs = self.care_preferences
        now = self.care_clock()
        if command == CARE_OPT_IN_COMMAND:
            prefs.opt_in(user_id, now=now, expires=now + _CARE_CONSENT_TTL)
            return "好的，已为你开启关怀提醒，30 天内有效。想暂停或停止，随时告诉我。"
        state = prefs.consent_state(user_id, now=now)
        if command == CARE_PAUSE_COMMAND:
            if state not in ("active", "paused"):
                return "你还没有开启关怀提醒。"
            prefs.pause(user_id, until=now + _CARE_PAUSE_SPAN, now=now)
            self._invalidate_care(user_id)
            # 文案不写死天数：同意剩余有效期不足暂停跨度时，先到期
            # 的是同意本身，写「7 天」会与实际恢复时点不符（P6
            # Review P4）。暂停跨度见 _CARE_PAUSE_SPAN。
            return "好的，关怀提醒已暂停。"
        # 停止 / 关闭：有同意记录（含已过期未修剪）即删除并确认。
        if state == "none":
            return "你还没有开启关怀提醒。"
        prefs.stop(user_id)
        self._invalidate_care(user_id)
        return "好的，已停止关怀提醒，你的关怀设置已删除。"

    def _observe_stage(self, stage_name, ctx, elapsed_ms, outcome):
        """流水线观察者（只读、永不阻断）：逐 Stage 记脱敏结构化日志。"""
        logger.debug(
            "stage=%s lane=%s outcome=%s elapsed_ms=%.2f",
            stage_name,
            ctx.lane,
            outcome,
            elapsed_ms,
        )

    async def _detect_with_budget(self, text: str):
        """带等待预算的安全检测：judge 信号量等待超时按疑似危机（D1）。

        关键词路径先行且不受信号量影响（Codex P3 Review #3）：关键词
        命中直接返回其分类，不等待 judge 许可、不降级为「疑似危机」；
        只有 judge 调用占许可并适用等待截止。等待任务计入 worker 池
        上限，不存在无界等待（方案 §5.3）。
        """
        keyword = self.safety.keyword_match(text)
        if keyword is not None:
            return True, keyword
        try:
            await asyncio.wait_for(
                self._judge_semaphore.acquire(), timeout=self._judge_wait_seconds
            )
        except asyncio.TimeoutError:
            return True, "疑似危机"
        try:
            return await self.safety.detect(text)
        finally:
            self._judge_semaphore.release()

    async def send_emergency_crisis(self, user_id: str) -> None:
        """critical 道亦满时的同步最小危机响应（方案 §5.3 最终兜底）。

        关键词固定话术直发、有界超时、不等通知回执、用未通知模板
        变体；失败只记日志，绝不向上抛。发送前与 SafetyJudge 危机分支
        同口径失效旧普通回复（推进 safety_epoch、取消在途普通流、重置
        重复追问），危机后普通尾泡不得继续发出（Codex P3 Review #2）。
        """
        self._safety_epoch[user_id] += 1
        # 本路径不走 runner、MetricsObserver 看不到它，危机计数在此同步
        # 入账（方案 §5.10），与 SafetyJudge 命中同口径。
        self.metrics.record_crisis()
        # 只推进关怀阻塞时钟（独立于告警去重锚点，见 _stage_safety_judge
        # 的注记）：本路径是兜底直发、不走告警段，不得污染 crisis_history
        # 的锚点与计数（P6 Review 第 2 轮）。
        self._last_crisis_at[user_id] = time.time()
        await self._advance_delivery_generation(user_id)
        self._cancel_ordinary_stream(user_id)
        self.repetition.reset(user_id)
        reply = crisis_reply(
            self.settings.safety.get("helplines", []), False, self.settings.safety
        )
        try:
            await asyncio.wait_for(
                self._send_reply(user_id, reply, "危机话术", critical=True),
                timeout=10.0,
            )
        except Exception as exc:  # noqa: BLE001 — 兜底路径失败不得影响接受边界
            logger.error("emergency crisis send failed: %s", type(exc).__name__)

    def _log(
        self,
        event_type: str,
        length: int,
        allowed: bool,
        latency_ms: float,
        *,
        reason_code: str | None = None,
        event_type_summary: str | None = None,
        extra: dict | None = None,
    ):
        """追加只含脱敏元数据的结构化日志，绝不记录消息原文或用户 ID。

        ``extra``（N3）为可选的附加元数据字段，合并进 record；既有
        调用不传即零影响。调用方只许放元数据（如 heartbeat 的
        online/good 布尔观测值），绝不放正文或标识原文。
        """
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "message_length": length,
            "whitelist_hit": allowed,
            "processing_latency_ms": round(latency_ms, 2),
        }
        if reason_code is not None:
            record["reason_code"] = reason_code
        if event_type_summary is not None:
            record["event_type_summary"] = event_type_summary
        if extra:
            record.update(extra)
        self._append_log_record(record)

    def _append_log_record(self, record) -> None:
        """向 events.jsonl 追加一行 JSON 记录（_log 与工具诊断共用）。"""
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _emit_tool_diagnostic(self, record) -> None:
        """工具诊断日志 sink：补 UTC 时间后与 _log 同文件追加一行。

        record 已由 DiagnosticsLogObserver 经白名单投影，只含元数据；
        本方法不增删任何其他字段，绝不写入正文。
        """
        enriched = {"time": datetime.now(timezone.utc).isoformat(), **record}
        self._append_log_record(enriched)

    def _log_ignore(self, ignore: Ignore) -> None:
        """Ignore 落日志：带 reason_code 与事件类型摘要（方案 §5.2），不记正文。"""
        self._log(
            "ignored",
            0,
            False,
            0.0,
            reason_code=ignore.reason_code,
            event_type_summary=ignore.event_type_summary,
        )

    async def _send_once(
        self,
        user_id: str,
        message: str,
        kind: str,
        *,
        critical: bool = False,
        reservation: QuotaReservation | None = None,
    ) -> SendOutcome:
        """Reserve shared attempts, then send one message; never retries here.

        预留、额度 token 与回执三态映射由 DeliveryService 独占（方案
        §5.2）；本方法只保留普通发送的 per-user 槽与失败日志职责。
        """
        if critical:
            receipt = await self.delivery.send(
                user_id, message, critical=True, reservation=reservation
            )
        else:
            # At most one ordinary send may be in flight per user.
            async with self._send_slots[user_id]:
                receipt = await self.delivery.send(
                    user_id, message, critical=False, reservation=reservation
                )
        if receipt.outcome == "accepted":
            return SendOutcome(True)
        logger.error("%s 发送失败: %s", kind, receipt.outcome)
        if receipt.outcome == "ambiguous":
            return SendOutcome(False, uncertain=True)
        return SendOutcome(False, blocked=True)

    async def _send_reply(self, user_id: str, message: str, kind: str, *, critical=False) -> bool:
        """发送单条回复并报告是否被接受；日志不包含消息或用户 ID。"""
        outcome = await self._send_once(user_id, message, kind, critical=critical)
        return outcome.sent

    def _cancel_ordinary_stream(self, user_id: str) -> None:
        """Cancel unsent ordinary bubbles for a user; never cancel priority routes."""
        task = self._ordinary_streams.pop(str(user_id), None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _register_ordinary_stream(self, user_id: str) -> None:
        task = asyncio.current_task()
        if task is None:
            return
        user_id = str(user_id)
        self._ordinary_streams[user_id] = task

        def clear(done, key=user_id):
            if self._ordinary_streams.get(key) is done:
                self._ordinary_streams.pop(key, None)

        task.add_done_callback(clear)

    async def _advance_delivery_generation(self, user_id: str) -> int:
        """推进投递代次，与 _deliver 的已接受记账段同锁串行（E 路修复 2）。

        _deliver 在创建 shielded 记账写任务时持有该用户的
        _state_locks、写任务落定（done 回调）才释放；代次推进必须排
        在未结记账写之后，否则推进先落、记账写后到时台账围栏会因
        代次不符拒写，已被平台接受的气泡永久丢失（台账停在
        in_flight、历史缺该回复）。本通道只做排序、不放行任何写：
        forget 删除后迟到的写到达台账时，仍按代次/回合存在性被
        删除围栏拒绝，绝不复活。
        """
        async with self._state_locks[str(user_id)]:
            return await self.ledger.advance_delivery_generation(user_id)

    async def dispatch(self, event: dict) -> asyncio.Task | None:
        """Accept one event without blocking; priority routes run on their own task."""
        # N3 前置分流（方案 §3.3/§4 C1）：先经 classify 收窄类型——
        # 事件三型与群消息永不进私聊链，在此以 not_message 落日志后
        # 拒收（它们在 webhook 路径由事件道/登记路径处理，直调
        # dispatch 时则到此为止）；classify 返回 Ignore 时不处理、
        # 落回下方既有 normalize 路径原样执行，保证 reason 口径零
        # 变化；私聊 InboundEvent 同样落回既有路径（保留 normalize
        # 调用，行为零变化）。
        classify = getattr(self.adapter, "classify", None)
        if classify is not None:
            classified = classify(event, require_keys=False)
            if isinstance(
                classified, (NoticeEvent, RequestEvent, MetaEvent)
            ) or (
                isinstance(classified, InboundEvent)
                and classified.chat_type == "group"
            ):
                self._log_ignore(
                    Ignore(REASON_NOT_MESSAGE, event_type_summary(event))
                )
                return None
        # 内部派发走适配器宽松规范化：直调本方法的既有调用方（含测试夹具）
        # 不带 message_id/self_id 仍可处理，但缺 user_id 等结构性畸形必须
        # 在此被 Ignore 挡下（KD-C07）；严格键资格在 webhook 接受边界。
        normalized = self.adapter.normalize(event, require_keys=False)
        if isinstance(normalized, Ignore):
            self._log_ignore(normalized)
            return None
        parsed = UserMessage(
            normalized.user_id,
            normalized.text,
            normalized.message_id,
            normalized.has_media,
            normalized.images,
        )
        return await self.dispatch_parsed(parsed)

    async def dispatch_parsed(
        self, parsed: UserMessage, *, catchup_prefix: str | None = None
    ) -> asyncio.Task | None:
        """normalize 之后的统一派发尾段：实时 dispatch 与补回合成回合共用。

        catchup_prefix 只写入本回合 EventContext.scratch（见 _run_event），
        供 Deliver 阶段拼前缀；实时路径恒传 None，行为逐字不变。
        """
        try:
            delivery_generation = await self._advance_delivery_generation(parsed.user_id)
        except Exception as exc:  # noqa: BLE001 — fail closed without touching the stream
            logger.error("delivery generation unavailable: %s", type(exc).__name__)
            return None
        # A freshly accepted parsed message immediately invalidates older ordinary bubbles.
        self._cancel_ordinary_stream(parsed.user_id)
        task = asyncio.create_task(
            self._run_event_safe(
                parsed, delivery_generation, catchup_prefix=catchup_prefix
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _run_event_safe(
        self, parsed, delivery_generation: int, *, catchup_prefix: str | None = None
    ) -> bool:
        """Keep one detached event failure from becoming an unretrieved task exception."""
        try:
            return await self._run_event(
                parsed, delivery_generation, catchup_prefix=catchup_prefix
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — one event must not kill the consumer
            logger.exception("event processing failed: %s", type(exc).__name__)
            return False

    async def handle(self, event: dict) -> bool:
        """处理单个事件并等待它完成；返回是否已发送回复。"""
        task = await self.dispatch(event)
        if task is None:
            return False
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return False
        except Exception as exc:  # noqa: BLE001 — one event must not kill the consumer
            logger.exception("event processing failed: %s", type(exc).__name__)
            return False

    async def wait_idle(self) -> None:
        """Wait until every dispatched event task has finished."""
        while True:
            tasks = [task for task in tuple(self._tasks) if not task.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._ordinary_streams.values()):
            task.cancel()
        self._ordinary_streams.clear()
        self.repetition.close()

    def _onboarding_text(self) -> str:
        try:
            copy = self.onboarding_copy.read_text(encoding="utf-8").strip()
        except OSError:
            copy = (
                "你好呀，我是阿年，一个 AI 陪伴助手。"
                "我能记住咱们最近聊过的事；如果你不想留着，直接跟我说「忘掉这件事」或「忘掉我」就行。"
                "想聊什么都可以，我在。"
            )
        # 首次告知以 onboarding.md 原文为准；仅当文件含 <!-- third-party --> 且其后有文案时，
        # 在第三方 provider 下追加（长江短版首次语不含模型/危机长说明）。
        if "<!-- third-party -->" in copy:
            parts = copy.split("<!-- third-party -->", 1)
            intro = parts[0].strip()
            provider = str(self.settings.app.get("llm", {}).get("provider", "deepseek"))
            extra = parts[1].strip() if len(parts) > 1 else ""
            if extra and provider in {"deepseek", "openai_compatible"}:
                intro += "\n" + extra
            return intro
        return copy

    async def _ensure_onboarding(self, user_id: str) -> None:
        """Claim onboarding so concurrent events cannot send it twice."""
        if await self.conversation.is_onboarded(user_id):
            return
        async with self._state_locks[user_id]:
            if await self.conversation.is_onboarded(user_id) or user_id in self._onboarding_claims:
                return
            self._onboarding_claims.add(user_id)
        try:
            if await self._send_reply(user_id, self._onboarding_text(), "首次告知"):
                await self.conversation.mark_onboarded(user_id)
        finally:
            self._onboarding_claims.discard(user_id)

    async def _still_current(
        self, user_id: str, delivery_generation: int, pack_generation: int, safety_epoch: int
    ) -> bool:
        """Recheck the cancellation marker, pack policy, and safety state before a send."""
        try:
            return (
                await self.ledger.delivery_generation(user_id) == delivery_generation
                and (await self.conversation.pack_snapshot(user_id))[1] == pack_generation
                and self._safety_epoch[user_id] == safety_epoch
            )
        except Exception:  # noqa: BLE001 — fail closed
            return False

    async def _paced_sleep(self, delay: float, spent: float) -> float:
        """Sleep within the total pacing cap; returns the pacing budget spent."""
        remaining = MAX_TOTAL_PACING_SECONDS - spent
        if remaining <= 0:
            return spent
        actual = min(max(float(delay), 0.0), remaining)
        # Always yield to the injected sleep, including a zero delay, so callers keep
        # the same webhook/response-delay semantics as before pacing existed.
        await self.sleep(actual)
        return spent + actual

    async def _acquire_stream_capacity(self) -> bool:
        try:
            await asyncio.wait_for(
                self._stream_capacity.acquire(), timeout=self._stream_wait_seconds
            )
            return True
        except asyncio.TimeoutError:
            return False

    def _review(self, bubbles, user_text: str = "", *, enforce_size: bool = True):
        """出站复核统一入口：带入本处理器配置的身份名与模型显示名（KD-001）。

        enforce_size=False 供自有字数口径的路由（工具合成 ≤5000 字，
        §5.5）使用：只做内容复核，不套 multipart 的 600 字总上限。
        """
        return review_bubbles(
            bubbles,
            user_text=user_text,
            identity_name=self.identity_name,
            identity_model=self.identity_model,
            enforce_size=enforce_size,
        )

    def _fallback_bubbles(self, reason: str, user_text: str = "") -> list[str]:
        if reason == "identity" or is_identity_question(user_text):
            return [self.identity_fallback]
        return [FALLBACK_REPLY]

    def _plan_plain_text_reply(
        self, user_id: str, user_text: str, raw: str
    ) -> tuple[list[str] | None, QuotaReservation | None]:
        """Plan an ordinary plain-text reply: split clear segments, else one bubble."""
        if not isinstance(raw, str) or not raw.strip():
            return self._fallback_bubbles("empty", user_text), None
        segments = split_plain_text(raw, one_by_one=requests_one_by_one(user_text))
        if segments is not None:
            allowed, reason = self._review(segments, user_text)
            if allowed:
                return self._reserve_multipart(user_id, segments, user_text)
            return self._fallback_bubbles(reason, user_text), None
        allowed, reason = self._review([raw], user_text)
        if not allowed:
            return self._fallback_bubbles(reason, user_text), None
        return [raw], None

    def _reserve_multipart(
        self, user_id: str, bubbles: list[str], user_text: str
    ) -> tuple[list[str] | None, QuotaReservation | None]:
        """Atomically reserve every bubble slot; fall back to one joined message."""
        # DeliveryService.reserve 已是 fail-closed 包装：记账异常一律 None。
        reservation = self.delivery.reserve(
            user_id, len(bubbles), allow_partial_single=True
        )
        if reservation is None:
            return None, None
        if reservation.remaining < len(bubbles):
            joined = join_bubbles(bubbles)
            if joined is not None:
                allowed, _ = self._review([joined], user_text)
                plan = [joined] if allowed else self._fallback_bubbles("safety", user_text)
            else:
                plan = self._fallback_bubbles("safety", user_text)
            return plan, reservation
        return bubbles, reservation

    async def _deliver(
        self,
        user_id: str,
        user_text: str,
        plan: list[str],
        *,
        store: bool,
        delivery_generation: int,
        pack_generation: int,
        safety_epoch: int,
        repeat_token,
        turn_id: str,
        kind: str = "普通回复",
        reservation: QuotaReservation | None = None,
        critical: bool = False,
    ) -> tuple[bool, bool, str | None]:
        """Pace and send a validated plan; only accepted prefixes reach memory.

        返回三元组 ``(sent, blocked, drop_reason)``：``drop_reason``
        只在未完整发出时说明丢弃归因——``"fence"`` 为代次/台账围栏
        在发送前拒绝（prepare/in_flight 被拒或发送前时效复核不过，
        该气泡未发出）、``"record_fence"`` 为气泡已被平台接受后其
        已接受记账写被围栏拒（气泡确已送达、只是台账未落，绝不按
        零发出 fence 归类，确认复审 R4）、``"blocked"`` 为额度预留
        耗尽未及发送、``"rejected"`` 为 _send_once 回执被拒
        （outcome.blocked，平台/额度层明确拒绝）、``"transport"``
        为 _send_once 结果不明（uncertain，含传输异常收敛的
        ambiguous 回执）；完整发出时为 None。本方法是内部契约，
        唯一生产调用方是 _stage_deliver（其据 drop_reason 区分
        视觉 outcome 码）。
        """
        prepared = False
        in_flight = False
        sends_ok = 0
        unsent = len(plan)
        blocked_any = False
        spent = 0.0
        # prepare 与「已接受气泡」的记录写以独立 task 发起并 shield
        # 等待：投递被取消（新消息打断是常态）时，await 普通 task 会把
        # 取消传播进被等者，必须 shield 才能让底层写独立存活；收尾
        # 逻辑再等它落盘后判定状态，避免收尾写与记录写并发乱序把
        # 已送达气泡弄丢（P5 Review #4；P5 前这两处是同步写、
        # 不存在该窗口）。
        prepare_task: asyncio.Task | None = None
        record_task: asyncio.Task | None = None
        try:
            if store:
                prepare_task = asyncio.ensure_future(
                    self.ledger.prepare_delivery(
                        user_id, turn_id, user_text, unsent,
                        pack_generation, delivery_generation,
                    )
                )
                if not await asyncio.shield(prepare_task):
                    self.repetition.finish(user_id, repeat_token, False)
                    return False, False, "fence"
                prepare_task = None
                prepared = True
            for index, bubble in enumerate(plan):
                delay = (
                    self.random_uniform(self.delay_min, self.delay_max)
                    if index == 0
                    else self.random_uniform(BUBBLE_DELAY_MIN, BUBBLE_DELAY_MAX)
                )
                spent = await self._paced_sleep(delay, spent)
                if not await self._still_current(
                    user_id, delivery_generation, pack_generation, safety_epoch
                ):
                    if prepared:
                        await self.ledger.update_delivery(
                            user_id, turn_id, "failed", unsent,
                            pack_generation, delivery_generation,
                        )
                    self.repetition.finish(user_id, repeat_token, False)
                    return False, blocked_any, "fence"
                if reservation is not None and reservation.remaining <= 0:
                    self.repetition.finish(user_id, repeat_token, False)
                    return False, True, "blocked"
                if prepared:
                    if not await self.ledger.update_delivery(
                        user_id, turn_id, "in_flight", unsent - 1,
                        pack_generation, delivery_generation,
                    ):
                        self.repetition.finish(user_id, repeat_token, False)
                        return False, blocked_any, "fence"
                    unsent -= 1
                in_flight = True
                outcome = await self._send_once(
                    user_id, bubble, kind, critical=critical, reservation=reservation
                )
                in_flight = False
                blocked_any = blocked_any or outcome.blocked
                if not outcome.sent:
                    if prepared:
                        await self.ledger.update_delivery(
                            user_id, turn_id,
                            "ambiguous" if outcome.uncertain else "failed",
                            unsent, pack_generation, delivery_generation,
                        )
                    self.repetition.finish(user_id, repeat_token, False)
                    # 回执被拒（blocked=rejected）与传输失败/结果不明
                    # 分流（确认复审 R3）：前者是明确拒绝、未送达；
                    # 后者（uncertain）平台可能已接受，归因不可混。
                    return (
                        False,
                        blocked_any,
                        "rejected" if outcome.blocked else "transport",
                    )
                if prepared:
                    state = "complete" if unsent == 0 else "partial"
                    # 已接受记账段与代次推进同锁串行（E 路修复 2）：建写
                    # 任务前持 _state_locks，锁由写任务的 done 回调在真正
                    # 落定后释放——即使本投递在 shield 等待中被取消，锁也
                    # 等底层写完成才放行新事件的代次推进；写本身仍经
                    # 台账围栏校验，forget 之后到达的写照常被拒。
                    record_lock = self._state_locks[user_id]
                    await record_lock.acquire()
                    try:
                        record_task = asyncio.ensure_future(
                            self.ledger.update_delivery(
                                user_id, turn_id, state, unsent,
                                pack_generation, delivery_generation,
                                accepted_bubble=bubble,
                            )
                        )
                    except BaseException:
                        record_lock.release()
                        raise
                    record_task.add_done_callback(
                        lambda _done, _lock=record_lock: (
                            _lock.release() if _lock.locked() else None
                        )
                    )
                    if not await asyncio.shield(record_task):
                        # 气泡已被平台接受、只是已接受记账写被台账
                        # 围栏拒：确已送达，不得归类为零发出 fence
                        # （确认复审 R4）——以 record_fence 单独归因，
                        # _stage_deliver 据此按有发出清接力记录并记
                        # delivered；台账/历史缺该前缀是围栏的既定
                        # 语义（迟到写不复活），不在此强行补写。
                        self.repetition.finish(user_id, repeat_token, False)
                        return False, blocked_any, "record_fence"
                    record_task = None
                sends_ok += 1
            complete = sends_ok == len(plan)
            accepted = complete and plan not in ([FALLBACK_REPLY], [self.identity_fallback])
            self.repetition.finish(user_id, repeat_token, accepted)
            return complete, blocked_any, None
        except asyncio.CancelledError:
            # 先结清在途的台账写再收尾（见 prepare_task 处注释）：等其
            # 落盘并把结果折算进 prepared/sends_ok，收尾写随后执行，
            # 与记录写严格串行。
            pending = [t for t in (prepare_task, record_task) if t is not None]
            if pending:
                await asyncio.wait(pending)
                if (
                    prepare_task is not None
                    and prepare_task.done()
                    and not prepare_task.cancelled()
                    and prepare_task.exception() is None
                    and prepare_task.result()
                ):
                    prepared = True
                if (
                    record_task is not None
                    and record_task.done()
                    and not record_task.cancelled()
                    and record_task.exception() is None
                    and record_task.result()
                ):
                    sends_ok += 1
            if prepared:
                if in_flight:
                    state = "ambiguous"
                elif sends_ok == 0:
                    state = "failed"
                else:
                    state = "partial"
                await self.ledger.update_delivery(
                    user_id, turn_id, state, unsent, pack_generation, delivery_generation
                )
            self.repetition.finish(user_id, repeat_token, False)
            raise

    async def _run_event(
        self, parsed, delivery_generation: int, *, catchup_prefix: str | None = None
    ) -> bool:
        """组装 EventContext 并跑固定 Stage 链（方案 §5.3）。

        原 _run_event 的处理段已按 Stage 边界拆为 _stage_* 方法，
        顺序、日志与返回语义逐项保留；顺序由 STAGE_ORDER 决定，
        改序即契约变更（顺序契约测试实测证明）。
        """
        ctx = EventContext(
            proc=self,
            parsed=parsed,
            delivery_generation=delivery_generation,
            started=time.monotonic(),
        )
        if catchup_prefix is not None:
            ctx.scratch["catchup_prefix"] = catchup_prefix
        ctx.already_onboarded = await self.conversation.is_onboarded(parsed.user_id)
        return await self._runner.run(ctx)

    # -- 视觉可观测性（events.jsonl，只记元数据） -------------------
    # 三步记录统一经 _log 写 event_type="vision"：extra 只含计数、
    # 结果码与模型名，绝不含 URL/路径/file 标识/正文/用户 ID。

    def _vision_model_name(self) -> str:
        """视觉模型名的脱敏读法：服务/其 client 的模型属性，缺失回退配置口径。"""
        service = getattr(self, "vision_service", None)
        for obj in (service, getattr(service, "client", None)):
            for attr in ("model_name", "model"):
                value = getattr(obj, attr, None)
                if isinstance(value, str) and value:
                    return value
        vision_cfg = (self.settings.app.get("media", {}) or {}).get("vision", {}) or {}
        return str(vision_cfg.get("model", "deepseek-flash"))

    def _log_vision_prepare(self, ctx, prepared, *, via_handoff: bool = False) -> None:
        """prepare 步：走到了 prepare 才写；prepared 为 None 时按全跳过记账。

        prepared 为 None 时 extra 带 ``"prepared": False`` 与
        ``"error"``（异常类型名，或服务不可用/无凭证时的
        ``"unavailable"``；绝不放异常消息正文）。接力形态
        （``via_handoff=True``）下本回合无自有图片引用，refs /
        received / attached / skipped / fetch_codes 全部取自接力
        记录（经 prepared_override 传入），并标 ``via_handoff``。
        遥测失败绝不打断回合：全函数包 try/except 吞掉。
        """
        try:
            refs = len(getattr(ctx.parsed, "images", None) or ())
            if prepared is None:
                extra = {
                    "vision_step": "prepare",
                    "refs": refs,
                    "received": refs,
                    "attached": 0,
                    "skipped": refs,
                    "fetch_codes": [],
                    "prepared": False,
                    "error": str(
                        ctx.scratch.get("vision_prepare_error") or "unavailable"
                    ),
                }
            else:
                extra = {
                    "vision_step": "prepare",
                    "refs": int(prepared.received) if via_handoff else refs,
                    "received": int(prepared.received),
                    "attached": len(prepared.data_urls),
                    "skipped": int(prepared.skipped),
                    "fetch_codes": list(getattr(prepared, "fetch_codes", ()) or ()),
                }
                if via_handoff:
                    extra["via_handoff"] = True
            self._log(
                "vision", len(ctx.parsed.text), True,
                (time.monotonic() - ctx.started) * 1000, extra=extra,
            )
        except Exception:  # noqa: BLE001 — 遥测失败绝不打断回合
            pass

    def _log_vision_generate(self, ctx, ok: bool) -> None:
        try:
            self._log(
                "vision", len(ctx.parsed.text), True,
                (time.monotonic() - ctx.started) * 1000,
                extra={
                    "vision_step": "generate",
                    "ok": bool(ok),
                    "model": self._vision_model_name(),
                },
            )
        except Exception:  # noqa: BLE001 — 遥测失败绝不打断回合
            pass

    def _log_vision_content_screen(self, ctx, result: str) -> None:
        """content_screen 步：图片内容筛查结局，每个实际筛查的视觉回合一条。

        ``result`` 只取结果码：``clear``（描述过网关未命中）、
        ``crisis``（命中危机、回合转危机协议）、``unavailable``
        （描述不可得、无从筛查）。只记结果码与接力标记，绝不记
        描述原文、分类名或图片内容。遥测失败绝不打断回合。
        """
        try:
            self._log(
                "vision", len(ctx.parsed.text), True,
                (time.monotonic() - ctx.started) * 1000,
                extra={
                    "vision_step": "content_screen",
                    "result": result,
                    "via_handoff": bool(ctx.scratch.get("vision_via_handoff")),
                },
            )
        except Exception:  # noqa: BLE001 — 遥测失败绝不打断回合
            pass

    def _log_vision_outcome(self, ctx, outcome: str) -> None:
        """outcome 步：每个进入视觉路由/接力的回合收尾恰一条（once 守卫）。

        码集（与各收尾点的归因一一对应，不得混用）：
        ``delivered`` 实际发出（含围栏前已有气泡发出）；
        ``fixed_reply`` 视觉失败走固定媒体文案；``fallback_text``
        视觉失败回落本回合文本路由；``handed_off`` 来源回合的图片
        已被后续回合接管；``rate_limited`` 覆盖两种来源——回执
        被拒（rejected，含部分投递后被拒）与额度拦截（blocked：
        Deliver 的 blocked，或 Respond 内计划预留失败）；
        ``delivery_failed`` 发送传输失败或结果不明；
        ``superseded_drop`` returned 模式下代次围栏零发出，或
        cancelled 模式下零发出收尾（两模式同码、成因不同，不再
        限于代次围栏一种）。
        ``content_screened_crisis`` 图片内容筛查命中危机、回合
        已由危机协议接管（Review F7）：危机固定话术的发送记账
        在协议内（event_type="crisis"），本码只标视觉回合的收
        尾归因，与 fixed_reply / fallback_text 严格区分。接力
        场景下同一图片的危机处置权只有一个执行者（见
        _claim_vision_crisis）：让位的接管回合（自己二次筛查
        未命中或命中但处置权已被来源回合抢占）亦记此码，危
        机话术的实际发送恰一次、以协议侧记录为准。

        收尾分类 precedence（_settle_vision_delivery 的 returned
        模式）：先判 delivered——``sent`` 完整发出，或
        drop_reason="record_fence"（与 had 无关），或
        drop_reason="fence" 且 had is True（围栏前已有发出；
        fence 是否记 delivered 取决于 had，不得无条件归
        delivered）；其后依次为 drop_reason="rejected" →
        rate_limited、drop_reason="transport" →
        delivery_failed、blocked（参数 blocked 或
        drop_reason="blocked"）→ rate_limited；以上皆不命中时
        才落台账读回兜底——had is None（读回不明）→
        delivery_failed，其余 → superseded_drop。returned 模式
        只用 had 三态、不使用原始 state；原始 state 仅用于
        cancelled / failed 分支的分类。遥测失败绝不打断回合。
        """
        try:
            if ctx.scratch.get("vision_outcome_logged"):
                return
            ctx.scratch["vision_outcome_logged"] = True
            self._log(
                "vision", len(ctx.parsed.text), True,
                (time.monotonic() - ctx.started) * 1000,
                extra={
                    "vision_step": "outcome",
                    "outcome": outcome,
                    "via_handoff": bool(ctx.scratch.get("vision_via_handoff")),
                },
            )
        except Exception:  # noqa: BLE001 — 遥测失败绝不打断回合
            pass

    # -- 视觉上下文接力 ------------------------------------------------

    def _raise_vision_served_generation(self, user_id, generation: int) -> None:
        """抬升每用户已服务代次水位（只升不降，P4-5）。"""
        key = str(user_id)
        if generation > self._vision_served_generation.get(key, 0):
            self._vision_served_generation[key] = generation

    def _register_vision_handoff(self, ctx, prepared) -> _VisionHandoff:
        """登记本回合备好的图片为接力记录（每用户至多一条，新替旧）。

        代次顺序守卫：已存在未过期记录且其 delivery_generation
        不小于本回合时，本回合是迟到的旧回合——不得覆盖更新回合
        的记录，保留新记录；本回合的记录不入字典（返回未登记的
        对象，其 consumed 永不被置位，本回合自身投递仍受其代次
        围栏约束）。已过期记录在登记时顺手丢弃后可直接替换。

        已服务水位守卫（P4-5，顺序口径）：本回合代次不大于该
        用户已服务水位（_vision_served_generation）时同样不登记
        （返回未登记对象、语义与拒覆盖一致）——已服务过的图片
        不被更旧图片补回复，迟到的旧回合登记不得复活已服务
        图片的接力机会。
        """
        digest = hashlib.sha256()
        for url in prepared.data_urls:
            digest.update(str(url).encode("utf-8"))
            digest.update(b"\x00")
        record = _VisionHandoff(
            data_urls=tuple(prepared.data_urls),
            received=int(prepared.received),
            created_at=time.monotonic(),
            pack_generation=ctx.pack_generation,
            safety_epoch=ctx.safety_epoch,
            delivery_generation=ctx.delivery_generation,
            fetch_codes=tuple(getattr(prepared, "fetch_codes", ()) or ()),
            image_hash=digest.hexdigest(),
        )
        key = str(ctx.parsed.user_id)
        served = self._vision_served_generation.get(key)
        if served is not None and record.delivery_generation <= served:
            return record
        existing = self._vision_handoff.get(key)
        if existing is not None:
            if time.monotonic() - existing.created_at > VISION_HANDOFF_TTL_SECONDS:
                self._clear_vision_handoff(key, existing)
            elif existing.delivery_generation >= record.delivery_generation:
                return record
        self._vision_handoff[key] = record
        return record

    def _take_vision_handoff(self, ctx) -> _VisionHandoff | None:
        """接管判定：未过期/未消费且风格与安全代次未变才置 consumed 接管。

        检查与置 consumed 之间无 await，单线程事件循环内原子，
        两条并发补发回合不会双双接管同一条记录。代次顺序守卫：
        记录的 delivery_generation 必须严格小于接管回合的——
        更早回合登记的图片才可被接管；后到的图片不得被先到
        回合的迟到 Respond 误接管（不消费、不清除，留待其后的
        合格回合）。

        清理与拒绝口径（确认复审 R1/R4）：TTL 检查先于 consumed
        判定，过期记录一律丢弃（即使已被消费）；命中接管即终态
        ——置 consumed 后立即 pop 字典条目，并以记录代次抬升已
        服务水位（P4-5），来源回合凭对象引用仍
        能判读 consumed，不依赖字典条目存续；该用户的视觉回合
        正在 _stage_deliver 投递未落定（_vision_delivering 置位）
        时拒绝接管（不消费、不清除，记录去留归来源回合的投递
        收尾判定），防投递未定即被抢接管造成双回复。
        """
        record = self._vision_handoff.get(str(ctx.parsed.user_id))
        if record is None:
            return None
        if time.monotonic() - record.created_at > VISION_HANDOFF_TTL_SECONDS:
            self._clear_vision_handoff(ctx.parsed.user_id, record)
            return None
        if record.consumed:
            return None
        if self._vision_delivering.get(str(ctx.parsed.user_id), 0):
            return None
        if record.delivery_generation >= ctx.delivery_generation:
            return None
        if (
            record.pack_generation != ctx.pack_generation
            or record.safety_epoch != ctx.safety_epoch
        ):
            return None
        record.consumed = True
        self._clear_vision_handoff(ctx.parsed.user_id, record)
        # 接管成功即该图片已被服务（P4-5）：以其代次抬升已服务
        # 水位，此后代次不大于它的迟到登记一律拒绝。
        self._raise_vision_served_generation(
            ctx.parsed.user_id, record.delivery_generation
        )
        return record

    def _mark_vision_delivering(self, user_id) -> None:
        key = str(user_id)
        self._vision_delivering[key] = self._vision_delivering.get(key, 0) + 1

    def _unmark_vision_delivering(self, user_id) -> None:
        key = str(user_id)
        count = self._vision_delivering.get(key, 0)
        if count <= 1:
            self._vision_delivering.pop(key, None)
        else:
            self._vision_delivering[key] = count - 1

    def _clear_vision_handoff(self, user_id, record) -> None:
        """仅当字典里仍是同一条记录时清除（不误删后登记的新记录）。"""
        if record is not None and self._vision_handoff.get(str(user_id)) is record:
            del self._vision_handoff[str(user_id)]

    def _drop_vision_handoff(self, user_id) -> None:
        """无条件清除该用户的接力记录（「忘掉」/危机命中的清理面）。

        隐私与安全语义：忘掉须连图片一并清、危机处置后不留图，
        故不做同一性校验，该用户当下在册的记录一律丢弃。
        """
        self._vision_handoff.pop(str(user_id), None)

    async def _prepare_vision_images(self, parsed, ctx=None):
        """准备本回合图片：无视觉服务或无图片引用时为 None；失败只记类型名。

        失败归因（异常类型名，或服务不可用/prepare 返回 None 时的
        ``"unavailable"``）写入 ctx.scratch 供 prepare 步日志的
        ``error`` 字段消费；绝不记异常消息正文。
        """
        service = self.vision_service
        if service is None or not parsed.images:
            if ctx is not None:
                ctx.scratch["vision_prepare_error"] = "unavailable"
            return None
        try:
            prepared = await service.prepare(parsed.images)
        except Exception as exc:  # noqa: BLE001 — 准备失败回落原固定回复
            logger.error("vision prepare failed: %s", type(exc).__name__)
            if ctx is not None:
                ctx.scratch["vision_prepare_error"] = type(exc).__name__
            return None
        if prepared is None and ctx is not None:
            ctx.scratch["vision_prepare_error"] = "unavailable"
        return prepared

    @staticmethod
    def _image_context_text(count: int) -> str:
        """纯图片回合的缺省语境文本：替代空文本进入记忆与回复拼装。

        计数用 received（用户实际发来的张数，与 prompt 内的超量
        注记同口径，Review F9-2），模型实际看到的张数另由注记说明。
        """
        if count <= 1:
            return "用户发来了一张图片"
        return f"用户发来了 {count} 张图片"

    async def _send_media_fixed_reply(self, ctx) -> bool:
        """视觉失败时回原固定文案，恒返回 False（本 Stage 就此终止）。

        发送前过投递/人格/安全代次围栏（Review F4）：等待视觉期间
        若有新消息到达、风格切换或危机处置推进了代次，本条回退已
        陈旧，静默丢弃不发，避免在新回复或危机话术之后补发一句
        不合时宜的媒体文案。
        """
        parsed = ctx.parsed
        user_id = parsed.user_id
        self.repetition.reset(user_id)  # 与旧两条固定文案路径同口径
        if not await self._still_current(
            user_id, ctx.delivery_generation, ctx.pack_generation, ctx.safety_epoch
        ):
            # 陈旧丢弃与真实发送在事件流里分开记账，不冒充已回复。
            self._log(
                "media_stale", len(parsed.text), True,
                (time.monotonic() - ctx.started) * 1000,
            )
            ctx.result = False
            return False
        sent = await self._send_reply(user_id, MEDIA_FIXED_REPLY, "媒体回复")
        self._log(
            "media", len(parsed.text), True, (time.monotonic() - ctx.started) * 1000
        )
        ctx.result = sent
        return False

    @staticmethod
    def _claim_vision_crisis(record) -> bool:
        """抢占一条接力记录的危机处置权：先到者执行危机协议。

        检查与置位之间无 await，单线程事件循环内原子。record
        为 None（无接力场景的自有回合）时恒可执行；返回 False
        表示对方回合已抢先处置同一图片，本回合不得重复执行
        危机协议（防双发危机话术与重复计数）。
        """
        if record is None:
            return True
        if record.crisis_disposed:
            return False
        record.crisis_disposed = True
        return True

    async def _delete_vision_observations_for_record(self, ctx, record) -> None:
        """危机认领的补偿删除（复审 r3 P3，r4 P3 扩至跨上传）。

        认领成功方调用：删除范围由 store 的认领删除在一个事务
        内三层定位——本记录登记的落存回合（兄弟回合在本回合
        认领前已按当时口径合法落存的副本）、图像同一性哈希命
        中的行（同图早前独立上传落存的副本）、以及同一性拿不
        准时的契约优先全删兜底。删除失败只记异常类型名，绝不
        挡调用方的危机协议；与落存方写库后复查的自删兜底互为
        冗余（删除幂等）：认领落在落存写库窗口内时认领方可能
        扑空，由落存方自删收口。
        """
        if record is None:
            return
        try:
            await self.conversation.delete_vision_observations_for_claim(
                str(ctx.parsed.user_id),
                tuple(record.observation_turn_ids),
                record.image_hash or None,
            )
        except Exception as exc:  # noqa: BLE001 — 补偿删除不挡危机协议
            logger.warning(
                "vision observation compensating delete failed: %s",
                type(exc).__name__,
            )

    async def _screen_vision_content(self, ctx, prepared) -> tuple[str, str]:
        """图片内容危机筛查（Review F7）：描述 → 同一网关判定。

        筛查对象是视觉模型对图片内容的客观描述文本（见
        VisionService.describe）：图片是用户发来的内容，其文本
        化形态与用户文字同属「待判输入」，正对 SafetyGateway 的
        判定口径（关键词 + 模型 judge，judge 不可用按 D1 保守降
        级为疑似危机，经 _detect_with_budget 与文字路完全同路）；
        不筛回复草稿——草稿是机器人自己的话，且筛查必须发生在
        人设回复生成消费图片之前。返回 (结果, 危机分类)：结果为
        ``clear`` / ``crisis`` / ``unavailable``（描述生成失败或
        抛错，无从筛查）；分类只供危机协议的告警分级，绝不进日志。
        描述原文不落日志、不进 events：clear 时经 ctx.scratch
        携带，待本回合回复投递成功后由投递收尾以观察条目形态
        写入对话记忆（2026-11-01 用户拍板：识图描述持久保留，
        见 _persist_vision_observation）；crisis / unavailable
        不携带——走危机协议或筛查失败的图片，其描述永不落存。
        """
        try:
            description = await self.vision_service.describe(prepared.data_urls)
        except Exception as exc:  # noqa: BLE001 — 描述失败按无法筛查收敛
            logger.error("vision describe failed: %s", type(exc).__name__)
            description = None
        if not description:
            self._log_vision_content_screen(ctx, "unavailable")
            return "unavailable", ""
        crisis, reason = await self._detect_with_budget(description)
        if crisis:
            self._log_vision_content_screen(ctx, "crisis")
            return "crisis", reason
        self._log_vision_content_screen(ctx, "clear")
        # 仅 clear 携带描述：供投递收尾在确认投递后落存观察条
        # 目；本回合若最终未投递视觉回复（被接管/回落/限流），
        # 携带值随 ctx 丢弃或由回落分支显式摘除，绝不落存。
        ctx.scratch["vision_description"] = description
        return "clear", ""

    async def _respond_with_vision(
        self, ctx, *, prepared_override=None, via_handoff: bool = False
    ) -> bool | None:
        """Respond 的视觉路由：准备图片 → 视觉生成 → 既有复核/投递口径。

        返回 True 表示回复已规划（route=direct，与工具路未调工具时
        同路：_plan_plain_text_reply 复核 + Deliver 额度/台账/记忆）；
        返回 False 表示已终止（纯图片失败，经围栏回原固定文案）；
        返回 None 表示未接管——图文消息在视觉不可用/失败时回落
        既有文本路由（Review F3：用户文字已有完整处理路，不得用
        媒体文案顶替其回答，更不得因此绕过身份披露等文本口径）。
        视觉回合不走工具循环/多气泡 JSON 路，此为有意取舍。

        接力形态（``prepared_override`` 非空）：图片来自先行图片
        回合登记的接力记录、本回合不再 prepare；语义与图文回合
        一致（失败回落本回合文本路、成功按 direct 路规划），且
        生成仍过 _plan_plain_text_reply 出站复核，不绕安全口径。

        内容筛查（Review F7）：两形态在图片就绪后、回复生成前
        都先经 _screen_vision_content——描述文本过危机网关，命
        中即转 _run_crisis_protocol 并终止（False）；描述不可得
        按视觉失败三态收敛。筛查跟随视觉服务存在性：media.vision
        关闭时本方法根本不被调用，无独立开关。
        """
        parsed = ctx.parsed
        user_id = parsed.user_id
        pure_image = not parsed.text.strip()
        # 视觉标记：Deliver 据此为本回合补 outcome（delivered /
        # rate_limited / delivery_failed / superseded_drop 按归因
        # 分流），回落/固定文案等收尾则在本方法内记。
        ctx.scratch["vision_active"] = True
        ctx.scratch["vision_via_handoff"] = bool(via_handoff)
        own_handoff = None
        if prepared_override is not None:
            prepared = prepared_override
            # 接力回合在走视觉生成前补发一条 prepare 步事件：计数与
            # fetch_codes 全部取自接力记录（经 prepared_override
            # 传入，fetch_codes 至此被消费、不再是死数据）。
            self._log_vision_prepare(ctx, prepared, via_handoff=True)
        else:
            prepared = await self._prepare_vision_images(parsed, ctx)
            self._log_vision_prepare(ctx, prepared)
            if prepared is not None and prepared.data_urls:
                # 接力登记：备好图片即登记，生成等待期间到达的
                # 补发文本回合可接管（见 _take_vision_handoff）。
                own_handoff = self._register_vision_handoff(ctx, prepared)
                ctx.scratch["vision_handoff_record"] = own_handoff
        if prepared is None or not prepared.data_urls:
            if pure_image:
                result = await self._send_media_fixed_reply(ctx)
                self._log_vision_outcome(ctx, "fixed_reply")
                return result
            self._log_vision_outcome(ctx, "fallback_text")
            return None
        # 图片内容筛查（Review F7）：图片备好后、回复生成之前，
        # 描述文本先过危机网关。自有图片与接力图片（prepared_
        # override）共用此门——接管回合对接管图片独立筛查一遍，
        # 不依赖来源回合的筛查结论，接力不构成绕过。
        screen, screen_reason = await self._screen_vision_content(ctx, prepared)
        # 危机处置协调（复审 P2）：筛查窗口内图片可能已被补发回
        # 合接管，两回合各持同一条接力记录（来源为 own_handoff、
        # 接管为 scratch 的 taken 记录）。已判出的危机结论绝不
        # 丢弃：任一回合判 crisis 即经记录上的 crisis_disposed
        # 抢占处置权（_claim_vision_crisis），先到者执行危机协
        # 议，后到者让位静默收尾；接管回合若先判完且结论非危机、
        # 却发现处置权已被来源回合抢占（来源结论后到且为危机），
        # 同样让位——二次描述的 clear/unavailable 不得推翻已判
        # 危机。反之结论非危机且未被抢占时，才按原口径处理接管
        # 与描述不可得。
        taken_record = ctx.scratch.get("vision_handoff_taken")
        if screen == "crisis":
            record = own_handoff if own_handoff is not None else taken_record
            if not self._claim_vision_crisis(record):
                # 对方回合已抢先处置同一图片的危机（其危机话术
                # 正在/已经发出）：本回合让位，不重复走协议。
                if own_handoff is not None:
                    self._clear_vision_handoff(user_id, own_handoff)
                    self._log_vision_outcome(ctx, "handed_off")
                else:
                    self._log_vision_outcome(ctx, "content_screened_crisis")
                ctx.result = False
                return False
            # 危机计数：MetricsObserver 只在 SafetyJudge 阶段结束
            # 时按 crisis_hit 入账，本路在 Respond 命中，自行入账
            # （与 send_emergency_crisis 兜底路径同口径）。抢占成
            # 功才入账，保证两回合合计恰一次。
            self.metrics.record_crisis()
            # 补偿删除（复审 r3 P3）：兄弟回合可能已先行落存同
            # 一图片的观察行（其落存时本回合尚未判危机，复查无
            # 从拦截）——认领成功即在协议执行前按记录上登记的
            # 落存回合 id 删净这些行，使「危机图描述永不落存」
            # 在迟到认领方向同样成立。删除失败只记异常类型名，
            # 绝不挡危机协议的发送。
            await self._delete_vision_observations_for_record(ctx, record)
            await self._run_crisis_protocol(ctx, screen_reason)
            self._log_vision_outcome(ctx, "content_screened_crisis")
            return False
        if taken_record is not None and taken_record.crisis_disposed:
            # 接管回合的二次筛查未命中，但来源回合已判危机并抢
            # 占处置（见上方协调说明）：本回合不生成、不回落，
            # 静默收尾，危机话术由来源回合发出。
            self._log_vision_outcome(ctx, "content_screened_crisis")
            ctx.result = False
            return False
        if own_handoff is not None and own_handoff.consumed:
            # 筛查期间图片已被补发回合接管且无人判危机：接管回
            # 合会对同一图片自行筛查并处置，本回合与「生成后被
            # 接管」同口径静默收尾——不补发固定文案、不回落文本路。
            self._clear_vision_handoff(user_id, own_handoff)
            self._log_vision_outcome(ctx, "handed_off")
            ctx.result = False
            return False
        if screen == "unavailable":
            # 描述不可得即无从筛查：未筛查的图片内容不得进入回复
            # 生成，按视觉失败三态收敛（纯图固定文案、图文回落
            # 本回合文本路），与 generate 失败口径一致。
            if pure_image:
                result = await self._send_media_fixed_reply(ctx)
                self._log_vision_outcome(ctx, "fixed_reply")
                return result
            self._log_vision_outcome(ctx, "fallback_text")
            return None
        self.repetition.reset(user_id)
        message = (
            self._image_context_text(prepared.received) if pure_image else parsed.text
        )
        if prepared.received > len(prepared.data_urls):
            message += (f"\n[系统注记：用户共发送了 {prepared.received} 张图片，"
                        f"本条仅附上其中 {len(prepared.data_urls)} 张，"
                        "请在回复中自然地向用户说明只看到了其中几张。]")
        try:
            # prompt 拼装与生成同置一 try（Review F10）：注入 brain
            # 缺 build_prompt 或拼装异常时与生成失败同口径收敛。
            prompt = self.brain.build_prompt(
                message, ctx.history, persona=ctx.persona
            )
            reply = await self.vision_service.generate(prompt, prepared.data_urls)
        except Exception as exc:  # noqa: BLE001 — 视觉失败按上方三态收敛，不重试
            logger.error("vision generation failed: %s", type(exc).__name__)
            reply = None
        self._log_vision_generate(ctx, bool(reply))
        if own_handoff is not None and own_handoff.consumed:
            # 生成期间图片已被补发回合接管：本回合不再投递自己
            # 的回复（其语境与回复都已被接管回合承接），静默收尾；
            # 接管发生在生成成败判定之前——即使本回合生成失败，
            # 也不得再补发固定文案/回落文本与接管回复打架。
            self._clear_vision_handoff(user_id, own_handoff)
            self._log_vision_outcome(ctx, "handed_off")
            ctx.result = False
            return False
        if taken_record is not None and taken_record.crisis_disposed:
            # 生成窗口复查（复审 r2 P3）：来源回合在接管回合生
            # 成期间才判危机并抢占处置——生成前复查（上方）看
            # 不到这次迟到的抢占。与生成前让位同口径静默收尾：
            # 不投递、不回落、不落存，危机话术由来源回合的危
            # 机协议发出；复查置于生成成败判定之前，生成失败
            # 也不得补发普通回复与危机处置打架。
            self._log_vision_outcome(ctx, "content_screened_crisis")
            ctx.result = False
            return False
        if not reply:
            # 视觉生成失败：本回合不会投递视觉回复，筛查携带的
            # 描述必须摘除——图文回落的文字回复仍会经 Deliver
            # 收尾，若不摘除，收尾会把从未成功生成的图片观察误
            # 落存（识图失败不落存口径）。
            ctx.scratch.pop("vision_description", None)
            if pure_image:
                result = await self._send_media_fixed_reply(ctx)
                self._log_vision_outcome(ctx, "fixed_reply")
                return result
            self._log_vision_outcome(ctx, "fallback_text")
            return None
        if pure_image:
            # 生成成功后才把缺省语境写入本回合文本：Deliver 据此把
            # 语境存入短期记忆；失败回退路径上 parsed 保持原样，
            # 与旧媒体回合「不存历史」的口径不冲突（回退不经 Deliver）。
            ctx.parsed = replace(
                parsed, text=self._image_context_text(prepared.received)
            )
        plan, reservation = self._plan_plain_text_reply(
            user_id, ctx.parsed.text, reply
        )
        if plan is None:
            self.repetition.finish(user_id, ctx.repeat_token, False)
            self._log("rate_limited", len(ctx.parsed.text), True,
                      (time.monotonic() - ctx.started) * 1000)
            # 计划为 None 是额度预留失败（_plan_plain_text_reply 的
            # 预留口径），归因 rate_limited 而非代次围栏丢弃。
            self._log_vision_outcome(ctx, "rate_limited")
            ctx.result = False
            return False
        ctx.plan, ctx.reservation = plan, reservation
        ctx.route, ctx.guard_done = "direct", True
        return True

    async def _stage_admission(self, ctx) -> bool:
        """Admission：主体分级 + onboarding + 忘掉/媒体前置分支。"""
        parsed = ctx.parsed
        user_id = parsed.user_id
        started = ctx.started
        # 主体分级一次判定（方案 §5.3 Stage 2）：后续 Stage 与网关只读
        # ctx 中的主体与能力集，不再散落手写检查。开放私聊下所有私聊
        # 用户都有 chat 能力；配置名单内用户另标 care_known（关怀门
        # 契约依赖该名单，§3.1）。
        if user_id in self.allowed:
            ctx.subject = "owner"
            ctx.capabilities = frozenset({"chat", "care_known"})
        else:
            ctx.subject = "stranger"
            ctx.capabilities = frozenset({"chat"})
        await self._ensure_onboarding(user_id)
        if requests_stop_teasing(parsed.text):
            self.repetition.block_teasing(user_id)
        if any(word in parsed.text for word in ("忘掉", "删除记忆", "忘掉我", "清除记忆")):
            async with self._state_locks[user_id]:
                await self.conversation.forget(user_id)
                self.repetition.forget(user_id)
                # 忘掉须连图片一并清：该用户的视觉接力记录（进程内
                # base64 图片）同在此临界区内丢弃，不留残图。
                self._drop_vision_handoff(user_id)
                if self.care_preferences is not None:
                    # KD-S03（P6 关闭）：「忘掉」统一覆盖关怀数据——在
                    # 同一临界区内清关怀同意/偏好并失效待发关怀，确认
                    # 回复在全部持久化完成后才发出。关怀存储是仓外独立
                    # SQLite、与会话库无法同事务，此为既定口径；清理的
                    # 原子性由本临界区串行保证。
                    self.care_preferences.forget(user_id)
                    self._invalidate_care(user_id)
                self._safety_epoch[user_id] += 1
            sent = await self._send_reply(
                user_id, "好的，我已删除保存的短期记忆。", "忘掉确认", critical=True
            )
            self._log("forget", len(parsed.text), True, (time.monotonic() - started) * 1000)
            ctx.result = sent
            return False
        if self.care_preferences is not None and user_id in self.allowed:
            # 关怀控制命令（P6）：Admission 前置分支，在 judge 与限流
            # 之前——撤回权不因 judge 故障（D1）或入站额度耗尽而落空。
            # 仅名单内（care_known）用户有此命令面；精确匹配才构成命令。
            command = parsed.text.strip()
            if command in CARE_COMMANDS:
                try:
                    # 与忘掉分支共用同一把状态锁线性化（P6 Review
                    # P3-2）：否则忘掉在 conversation.forget 的 await
                    # 处让出时，后到的 opt-in 会被先开始的忘掉吞掉，
                    # 「已开启」确认成为假确认。
                    async with self._state_locks[user_id]:
                        reply = self._apply_care_command(user_id, command)
                except Exception as exc:  # noqa: BLE001 — 撤回失败绝不假确认
                    logger.error("care command failed: %s", type(exc).__name__)
                    reply = "关怀设置处理失败，请稍后再试。"
                sent = await self._send_reply(user_id, reply, "关怀命令", critical=True)
                self._log(
                    "care_command", len(parsed.text), True,
                    (time.monotonic() - started) * 1000,
                )
                ctx.result = sent
                return False
        if parsed.has_media and not parsed.text.strip():
            # 带可取图片引用的纯图片不在此拒绝：放行到 Respond 的
            # 视觉路由，使图片下载与生成发生在 SafetyJudge 与入站
            # 限流之后——危机话术不被网络 I/O 延迟、图片洪泛先被
            # 限流拦下（Review F2）。延期标记供 SafetyJudge 识别
            # 「无文字可判」的回合。可取口径与 prepare 的双路径
            # 一致：http/https 引用可直连取图；只有 file 引用的
            # 图片在 NapCat 介导取图已接线时也可取（确认复审
            # P2-2）。其余纯媒体（语音/视频/无可用取图路径的
            # file-only 图片）仍在此回原固定文案，口径与接入前
            # 逐字一致。
            images = getattr(parsed, "images", None)
            napcat_wired = (
                self.vision_service is not None
                and getattr(self.vision_service, "napcat_fetcher", None) is not None
            )
            if self.vision_service is not None and images and any(
                ref.ref.lower().startswith(("http://", "https://"))
                or (napcat_wired and getattr(ref, "file_id", None))
                for ref in images
            ):
                ctx.scratch["vision_deferred"] = True
                return True
            self.repetition.reset(user_id)
            sent = await self._send_reply(
                user_id, MEDIA_FIXED_REPLY, "媒体回复"
            )
            self._log("media", 0, True, (time.monotonic() - started) * 1000)
            ctx.result = sent
            return False
        return True

    async def _stage_safety_judge(self, ctx) -> bool:
        """SafetyJudge：双路检测；危机 → critical 通道直发固定话术并终止。"""
        parsed = ctx.parsed
        # 延期的纯图片回合没有用户文字可判（Admission 标记）：judge
        # 的对象是文字，空文本既不会命中关键词，也不应为此调用模型
        # judge——否则 judge 不可用时 D1 保守口径会把一张普通图片
        # 误判成疑似危机。本 Stage 只判用户文字；图片内容由 Respond
        # 视觉路在生成前以描述文本过同一网关筛查（Review F7，见
        # _screen_vision_content）；有随图文字的回合不带此标记、
        # 文字判定口径逐字不变。
        if ctx.scratch.get("vision_deferred") and not parsed.text.strip():
            return True
        crisis, reason = await self._detect_with_budget(parsed.text)
        if not crisis:
            return True
        return await self._run_crisis_protocol(ctx, reason)

    async def _run_crisis_protocol(self, ctx, reason: str) -> bool:
        """危机处置协议：文字命中与视觉内容筛查命中（F7）共用同一实现。

        固定话术经 critical 通道直发、告警去重/升级判定、关怀阻塞
        时钟、安全代次推进与接力记录清理逐项沿用原 SafetyJudge
        内联实现，一个字不改；抽成独立方法只为让 Respond 视觉路
        的内容筛查命中走同一条处置路，不另起平行话术。恒返回
        False（本回合就此终止，ctx.result 已置为发送结果）。
        危机计数口径：本路径由 MetricsObserver 按 crisis_hit 标
        记在 SafetyJudge 阶段结束后入账；Respond 路径命中时观察
        者不认，调用方须自行 record_crisis（见 _respond_with_vision）。
        """
        parsed = ctx.parsed
        user_id = parsed.user_id
        started = ctx.started
        # 危机命中标记供 MetricsObserver 在本 Stage 结束后计数（§5.10）。
        ctx.scratch["crisis_hit"] = True
        # 危机处置后不留图：清该用户的视觉接力记录（与下方安全
        # 代次推进双保险——即使代次口径日后变化，图片也不残留）。
        self._drop_vision_handoff(user_id)
        # Safety state advances before anything ordinary can consider itself current.
        self._safety_epoch[user_id] += 1
        self.repetition.reset(user_id)
        notify = bool(
            self.alert_qq
            and self.settings.app.get("tools", {})
            .get("alert_private_msg", {})
            .get("enabled", False)
        )
        notified = False
        now = time.time()
        previous = self.crisis_history.get(user_id)
        severity = {
            x["name"]: int(x["severity"]) for x in self.settings.safety.get("categories", [])
        }
        old_level = severity.get(previous["category"], 0) if previous else 0
        new_level = severity.get(reason, 0)
        first_or_upgrade = (
            previous is None
            or now - previous["time"] >= self.crisis_window
            or new_level > old_level
        )
        # 关怀阻塞时钟在任何 await 之前同步推进（P6 Review 第 2 轮
        # 修正）：告警发送是 await，关怀门在该窗口内经
        # current_care_safety 实时重查时必须已能看到「危机处置中」。
        # 注意与 crisis_history 分开记账——后者的 time 是告警去重锚点
        # （只在 first/upgrade/窗口过期时重置，基线语义），曾把占位
        # 记录写进 crisis_history 导致锚点被每条危机消息刷新、持续
        # 危机时窗口后不再重复告警（第 2 轮 P2），故拆为独立时钟。
        self._last_crisis_at[user_id] = now
        if first_or_upgrade:
            try:
                await self.alerter.alert(user_id, reason, "危机提醒")
            except Exception as exc:
                logger.error("危机告警失败: %s", type(exc).__name__)
            if notify:
                try:
                    notified = await self.delivery.send_alert(
                        self.alert_qq,
                        f"时间：{datetime.now(timezone.utc).isoformat()} "
                        f"类别：{reason} user_id：{user_id}",
                    )
                    if not notified:
                        logger.error("危机私聊告警发送失败")
                except Exception as exc:
                    logger.error("危机私聊告警失败: %s", type(exc).__name__)
            self.crisis_history[user_id] = {"time": now, "category": reason, "count": 1}
        else:
            previous["count"] += 1
            previous["category"] = reason
        reply = crisis_reply(
            self.settings.safety.get("helplines", []), notified, self.settings.safety
        )
        # Fixed complete crisis copy: no ordinary pacing and no model rephrasing.
        sent = await self._send_reply(user_id, reply, "危机话术", critical=True)
        self._log("crisis", len(parsed.text), True, (time.monotonic() - started) * 1000)
        ctx.result = sent
        return False

    async def _stage_inbound_rate_limit(self, ctx) -> bool:
        """InboundRateLimit：入站滑动窗口；超限静默丢弃。"""
        if not self.limiter.allow(ctx.parsed.user_id):
            self.repetition.reset(ctx.parsed.user_id)
            self._log(
                "rate_limited", len(ctx.parsed.text), True,
                (time.monotonic() - ctx.started) * 1000,
            )
            ctx.result = False
            return False
        return True

    async def _anniversary_merged_entries(self):
        """命令面合并视图：config 静态清单为骨、对话存储同名覆盖。

        合并口径与查询面 provider 同一函数（merge_anniversaries），
        两面永不分岔。
        """
        stored = await self.anniversary_store.list_entries()
        return merge_anniversaries(self._countdown_config_entries, stored)

    async def _anniversary_render(self, entries, arguments) -> str:
        """以合并视图现构工具并渲染（单查/列表与查询面同一渲染器）。

        工具动作为内层数据串，须包回网关信封再交 format 渲染；
        任一不一致由渲染器收敛为不可用文案，调用方据此回退。
        """
        tool = date_countdown_tool(entries)
        # 本渲染面已在 Command Stage 过主人门：显式传主人档位，
        # 不依赖动作层默认值（其缺省为最小权限 fail-closed）。
        inner = await tool.action(**arguments, caller_scope="owner")
        return format_date_countdown(json.dumps({"status": "ok", "data": inner}))

    async def _anniversary_add(self, entry) -> str:
        # 失败契约分段：upsert 本身失败（含落存异常）向上抛、由
        # _anniversary_command 按 add 口径回 FAILED（未落存如实报
        # 失败）；upsert 已成功后，合并视图读取/渲染失败绝不再报
        # 「保存失败」——条目已经落存，回退为纯确认文案（如实回报
        # 已保存，与渲染不可用时的既有回退同口径）。
        try:
            created = await self.anniversary_store.upsert(entry)
        except AnniversaryStoreFull:
            return ANNIVERSARY_FULL_REPLY
        head = "记好了。" if created else "已更新。"
        try:
            entries = await self._anniversary_merged_entries()
            rendered = await self._anniversary_render(entries, {"name": entry.name})
        except Exception:  # noqa: BLE001 — 已落存，后段失败走确认回退
            rendered = COUNTDOWN_UNAVAILABLE_REPLY
        if rendered == COUNTDOWN_UNAVAILABLE_REPLY:
            return f"{head}「{entry.name}」已保存。"
        return f"{head}{rendered}"

    async def _anniversary_remove(self, name) -> str:
        removed = await self.anniversary_store.remove(name)
        key = normalize_anniversary_name(name)
        in_config = any(
            normalize_anniversary_name(item.name) == key
            for item in self._countdown_config_entries
        )
        if removed:
            reply = f"已删掉「{name}」。"
            if in_config:
                reply += "配置文件里还有一条同名纪念日，之后会按那条显示。"
            return reply
        if in_config:
            return f"「{name}」是写在配置文件里的，我这边删不掉，需要改配置才行。"
        return f"没有找到你记的「{name}」。"

    async def _anniversary_list(self) -> str:
        entries = await self._anniversary_merged_entries()
        if not entries:
            return ANNIVERSARY_EMPTY_REPLY
        return await self._anniversary_render(entries, {})

    async def _anniversary_command(self, ctx) -> bool:
        """纪念日管理命令（Command Stage 前置分支）。

        工具政策关闭时整面不存在（与查询面同一开关口径）；触发与
        解析为确定性纯函数，非命令文本直接放行。主体门在查存储
        之前：非主人命中管理形态一律固定拒绝文案——不查存储、
        不确认任何条目是否存在（关闭 R-C1 的命令面）。存储异常
        回 FAILED 文案，绝不假确认。
        """
        if not self.read_only_registry.is_enabled("date_countdown"):
            return False
        command = parse_anniversary_command(ctx.parsed.text)
        if command is None:
            return False
        parsed = ctx.parsed
        user_id = parsed.user_id
        self.repetition.reset(user_id)
        if ctx.subject != "owner":
            ctx.result = await self._send_reply(
                user_id, ANNIVERSARY_OWNER_ONLY_REPLY, "纪念日命令"
            )
            self._log(
                "anniversary_command", len(parsed.text), True,
                (time.monotonic() - ctx.started) * 1000,
                reason_code="not_owner",
            )
            return True
        try:
            if command.kind == "add":
                reply = await self._anniversary_add(command.entry)
            elif command.kind == "add_invalid":
                reply = ANNIVERSARY_FORMAT_REPLY
            elif command.kind == "remove":
                reply = await self._anniversary_remove(command.name)
            else:  # list
                reply = await self._anniversary_list()
        except Exception:  # noqa: BLE001 — 失败文案按操作分口径，不混用
            reply = ANNIVERSARY_FAILURE_REPLIES.get(
                command.kind, ANNIVERSARY_FAILED_REPLY
            )
        ctx.result = await self._send_reply(user_id, reply, "纪念日命令")
        self._log(
            "anniversary_command", len(parsed.text), True,
            (time.monotonic() - ctx.started) * 1000,
            reason_code=command.kind,
        )
        return True

    async def _stage_command(self, ctx) -> bool:
        """Command：纪念日命令前置分支 + 风格命令精确匹配。"""
        if await self._anniversary_command(ctx):
            return False
        parsed = ctx.parsed
        user_id = parsed.user_id
        command = parsed.text.strip()
        if command == "聊天风格":
            self.repetition.reset(user_id)
            ctx.result = await self._send_reply(user_id, self.pack_registry.menu(), "风格列表")
            return False
        if command == "恢复默认风格" or command.startswith("切换风格 "):
            self.repetition.reset(user_id)
            requested = "default" if command == "恢复默认风格" else command[5:].strip()
            pack = self.pack_registry.resolve(requested)
            async with self._state_locks[user_id]:
                selected_id = None if pack.id == "default" else pack.id
                await self.conversation.select_pack(user_id, selected_id)
            reply = ("已切换到默认风格。" if pack.id == "default" else
                     f"已切换到{pack.style}。")
            if requested not in self.pack_registry.packs:
                reply = "这个风格不可用，已恢复默认风格。"
            ctx.result = await self._send_reply(user_id, reply, "风格切换")
            return False
        return True

    async def _stage_memory_load(self, ctx) -> bool:
        """MemoryLoad：读短期历史、本回合 id 与安全代次快照。"""
        parsed = ctx.parsed
        ctx.history = await self.conversation.get_history(parsed.user_id)
        ctx.turn_id = parsed.message_id or uuid.uuid4().hex
        ctx.safety_epoch = self._safety_epoch[parsed.user_id]
        return True

    async def _stage_persona(self, ctx) -> bool:
        """Persona：解析人设快照（默认/风格包 + 安全包装）。"""
        selected, pack_generation = await self.conversation.pack_snapshot(ctx.parsed.user_id)
        ctx.selected = selected
        ctx.pack_generation = pack_generation
        pack = self.pack_registry.resolve(selected)
        ctx.pack = pack
        ctx.persona = external_persona(pack, self.settings) if pack.id != "default" else None
        return True

    async def _stage_respond(self, ctx) -> bool:
        """Respond：按路由生成计划（时间直达/工具路由/多气泡生成）。"""
        parsed = ctx.parsed
        user_id = parsed.user_id
        started = ctx.started
        already_onboarded = ctx.already_onboarded
        # 视觉路由（图片准备与生成都在本 Stage，即 SafetyJudge 与
        # 入站限流之后，Review F2）：处理结果三态——True 已规划回复、
        # False 已终止（纯图片失败的固定文案回退）、None 未接管
        # （图文消息视觉失败，回落下方既有文本路由，与视觉关闭时
        # 一致，Review F3）。
        if self.vision_service is not None and parsed.has_media and getattr(
            parsed, "images", None
        ):
            vision_outcome = await self._respond_with_vision(ctx)
            if vision_outcome is not None:
                return vision_outcome
        if (already_onboarded and self.read_only_registry.is_enabled("current_time")
                and is_current_time_request(parsed.text)):
            self.repetition.reset(user_id)
            result = await self.read_only_registry.invoke(
                "current_time", "{}", user_id, user_message=parsed.text
            )
            ctx.plan, ctx.tools_used = [format_current_time(result)], True
            ctx.route, ctx.guard_done = "direct", True
            return True
        if (already_onboarded and self.read_only_registry.is_enabled("current_time")
                and is_ambiguous_time_request(parsed.text)):
            self.repetition.reset(user_id)
            # KD-S02 定版：含糊时间澄清与直达路同口径，tools_used=True
            # 使本回合不写短期记忆（两路一致）。
            ctx.plan, ctx.tools_used = [CLARIFY_REPLY], True
            ctx.route, ctx.guard_done = "direct", True
            return True
        # 视觉接力（确定性路由优先）：本回合无图、但先行图片回合
        # 留有未过期未消费的接力记录，且风格/安全代次与登记时一致
        # 时，本回合以自己的文本 + 接力图片走视觉生成。判定点在
        # current_time 两条确定性路由之后、工具路由之前：命中问时
        # /澄清的文本不接管（记录留给后续回合）；本回合自带图片的
        # 视觉路由在上方、位置不变。接力只发生在此处——本回合已
        # 过自己的 SafetyJudge（危机回合在 SafetyJudge 即终止、永
        # 不到达这里），风格切换回合在 Command 即终止且其后的
        # pack_generation 已变化，接管判定同样过不去。
        if self.vision_service is not None and not getattr(parsed, "images", None):
            handoff = self._take_vision_handoff(ctx)
            if handoff is not None:
                ctx.scratch["vision_handoff_taken"] = handoff
                prepared_override = PreparedImages(
                    data_urls=tuple(handoff.data_urls),
                    received=handoff.received,
                    skipped=handoff.received - len(handoff.data_urls),
                    fetch_codes=tuple(handoff.fetch_codes),
                )
                vision_outcome = await self._respond_with_vision(
                    ctx, prepared_override=prepared_override, via_handoff=True
                )
                if vision_outcome is not None:
                    return vision_outcome
                # None = 接力视觉失败回落本回合既有文本路由（F3 口径），
                # 继续下方普通路由；outcome 已在回落点记 fallback_text。
        if already_onboarded and self.read_only_registry.schemas():
            self.repetition.reset(user_id)
            args = (parsed.text, ctx.history, self.read_only_registry, user_id)
            reply, tools_used = await (self.brain.respond_with_tools(*args, persona=ctx.persona)
                                       if ctx.persona is not None else
                                       self.brain.respond_with_tools(*args))
            # 统一观测出口（§5.10）：从 provider 客户端收割工具循环
            # 结构化诊断（只含元数据），供 Respond 的观察者汇入指标
            # 与诊断日志。本行在 Stage 内、不在 observer 保护伞下：
            # provider 是公开扩展点，属性缺失、为 None 或非序列值都
            # 必须收敛为空，绝不让收割本身打断回复（Review P3-1）。
            client = getattr(self.brain, "client", None)
            raw_diagnostics = getattr(client, "last_tool_diagnostics", ())
            ctx.tool_diagnostics = (
                tuple(raw_diagnostics)
                if isinstance(raw_diagnostics, (list, tuple))
                else ()
            )
            ctx.tools_used = tools_used
            # Tools were offered but not used: this is still an ordinary reply and
            # must get the same deterministic multipart treatment as the no-tool path.
            if tools_used:
                # 实际调用工具后的模型合成回复仍是模型生成内容：过单条
                # 身份/安全复核（KD-001 工具路，Codex P3 Review #4），
                # 不过换固定回退单条；不套 multipart 的条数/字数上限，
                # 搜索合成的单条形态与字数口径保持（§5.5 synthesize）。
                allowed, reason = self._review([reply], parsed.text, enforce_size=False)
                ctx.plan = [reply] if allowed else self._fallback_bubbles(
                    reason, parsed.text
                )
                ctx.route, ctx.guard_done = "direct", True
            else:
                plan, reservation = self._plan_plain_text_reply(user_id, parsed.text, reply)
                if plan is None:
                    self.repetition.finish(user_id, ctx.repeat_token, False)
                    self._log("rate_limited", len(parsed.text), True,
                              (time.monotonic() - started) * 1000)
                    ctx.result = False
                    return False
                ctx.plan, ctx.reservation = plan, reservation
                ctx.route, ctx.guard_done = "direct", True
            return True
        # 普通多气泡路由：生成与解析在本 Stage，复核归 OutputGuard，
        # 预留与发送归 Deliver（与原代码的 review→tease→reserve→deliver
        # 顺序逐项对应）。
        repeat_level, repeat_token = (self.repetition.begin(user_id, parsed.text)
                                      if already_onboarded else (0, None))
        ctx.repeat_token = repeat_token
        ctx.scratch["repeat_level"] = repeat_level
        self._register_ordinary_stream(user_id)
        if not await self._acquire_stream_capacity():
            self.repetition.finish(user_id, repeat_token, False)
            self._log("rate_limited", len(parsed.text), True,
                      (time.monotonic() - started) * 1000)
            ctx.result = False
            return False
        try:
            raw = await self.brain.respond_multipart(
                parsed.text, ctx.history, persona=ctx.persona, repeat_level=repeat_level
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — fallback below, no retry
            logger.error("multipart generation failed: %s", type(exc).__name__)
            raw = ""
        finally:
            self._stream_capacity.release()
        bubbles, reason = parse_bubbles(raw)
        if bubbles is None:
            # The model may ignore the JSON contract and answer in plain text.
            # Split only clear complete segments; otherwise keep the fixed single
            # fallback so malformed or fragmented output is never sent.
            segments = split_plain_text(
                raw, one_by_one=requests_one_by_one(parsed.text)
            )
            if segments is not None:
                ctx.bubbles = segments
                ctx.scratch["bubbles_unreviewed"] = True
            else:
                ctx.bubbles = self._fallback_bubbles(reason, parsed.text)
        else:
            ctx.bubbles = bubbles
            ctx.scratch["bubbles_unreviewed"] = True
        ctx.route = "chat"
        return True

    async def _stage_output_guard(self, ctx) -> bool:
        """OutputGuard：多气泡出站复核 + 重复追问轻调侃（原顺序保留）。

        只管 chat 路由的模型生成回复；direct 路由的计划已在各自
        guard 内完成复核（guard_done），本 Stage 直接放行。
        """
        if ctx.route != "chat" or ctx.bubbles is None:
            return True
        parsed = ctx.parsed
        user_id = parsed.user_id
        bubbles = ctx.bubbles
        if ctx.scratch.get("bubbles_unreviewed"):
            allowed, reason = self._review(bubbles, parsed.text)
            if not allowed:
                bubbles = self._fallback_bubbles(reason, parsed.text)
        repeat_level = ctx.scratch.get("repeat_level", 0)
        if (
            repeat_level >= 2
            and bubbles not in ([FALLBACK_REPLY], [self.identity_fallback])
            and len(bubbles) < MAX_BUBBLES
            and self.repetition.allow_teasing(user_id)
            and is_light_tone(parsed.text, ctx.history)
        ):
            bubbles = [*bubbles, TEASE_LINE]
        ctx.bubbles = bubbles
        ctx.plan = bubbles
        ctx.needs_reserve = True
        return True

    async def _persist_vision_observation(self, ctx) -> None:
        """识图观察落存：投递收尾判定 delivered 后写对话记忆。

        落存对象是内容筛查 clear 的图片描述（_screen_vision_
        content 经 ctx.scratch 携带至此）；走危机协议、筛查不
        可得或视觉生成失败的回合不携带描述（生成失败路径已显
        式摘除），天然不落存（fail-closed）。恰一次由
        ConversationStore.add_vision_observation 按
        (user_id, turn_id) 去重保证——收尾在 cancelled/failed
        模式下按台账事实重判 delivered 时同口径落存，重复收尾
        不双写。落存另受 store 侧 §5.6 删除围栏约束：用户在结
        算落定前执行 forget（台账回合行已删）时落存 no-op，
        已删除的图片描述不被迟到的结算复活。描述只进对话
        DB：不落日志、不进 events；写入失败只记异常类型名，
        绝不影响投递收尾的既定结局。

        危机防线（复审 r2 P3 + r3 P3）：落存前复查覆盖两条记
        录——接管记录与本回合自有记录（同一图片的共享对象）：
        任一已被抢占危机处置即跳过落存（来源回合在接管回合的
        投递窗口内才判危机时回复已送达不可撤回，但描述在此不
        得入记忆；自有记录方向同理：本回合投递后、结算前被兄
        弟回合的迟到认领标記者，跳过）。写库前把本回合
        turn_id 登记进两条记录的 observation_turn_ids，供认领
        方在更晚的迟到认领时补偿删除本行；写库后复查一次：
        认领落在写库窗口内时其补偿删除可能先于本行写入而扑
        空，此处自删兜底（删除幂等，与认领方重复删无碍）。
        """
        records = (
            ctx.scratch.get("vision_handoff_taken"),
            ctx.scratch.get("vision_handoff_record"),
        )
        if any(
            record is not None and record.crisis_disposed
            for record in records
        ):
            # 落存前防御复查（复审 r2 P3，r3 P3 扩至自有记录）：
            # 任一记录已被抢占危机处置——危机图的描述绝不落存
            # （_screen_vision_content 契约），落存是最后一道门，
            # 在此跳过。
            return
        description = ctx.scratch.get("vision_description")
        if not description:
            return
        for record in records:
            if record is not None:
                record.observation_turn_ids.add(ctx.turn_id)
        # 图像同一性（复审 r4 P3）：两条记录指向同一张图，取任
        # 一非空哈希随落存登记进同一性索引，供日后同图再上传
        # 被判危机时跨上传补偿删除。
        image_hash = next(
            (
                record.image_hash
                for record in records
                if record is not None and record.image_hash
            ),
            None,
        )
        try:
            await self.conversation.add_vision_observation(
                str(ctx.parsed.user_id),
                ctx.turn_id,
                description,
                image_hash=image_hash,
            )
        except Exception as exc:  # noqa: BLE001 — 落存失败不掩盖投递结局
            logger.warning(
                "vision observation persist failed: %s", type(exc).__name__
            )
            return
        if any(
            record is not None and record.crisis_disposed
            for record in records
        ):
            # 写库后复查（复审 r3 P3）：认领落在写库窗口内时，
            # 认领方的补偿删除先于本行写入执行而扑空——此处自
            # 删兜底，保证危机图描述不因该竞态残留。
            try:
                await self.conversation.delete_vision_observation(
                    str(ctx.parsed.user_id), ctx.turn_id
                )
            except Exception as exc:  # noqa: BLE001 — 自删失败只留痕
                logger.warning(
                    "vision observation post-persist delete failed: %s",
                    type(exc).__name__,
                )

    async def _settle_vision_delivery(
        self, ctx, *, mode: str, sent=False, blocked=False, drop_reason=None
    ) -> None:
        """视觉回合投递收尾：台账读回 → 接力记录清/留 → 观察落存 → outcome 记账。

        单一收尾实现，三种进入模式（确认复审 R1/R2/R5）：

        - ``returned``：_deliver 正常返回。部分投递判定以台账读回
          为准（sent 只代表整计划完整发出）；有发出、读回不明
          （had=None，按可能已送达保守处理）或 record_fence（气泡
          已被接受、记账写被拒）→ 清接力记录，零发出 → 保留等
          TTL 内接力。outcome 按 drop_reason 分流：完整发出 /
          record_fence / 围栏前已有发出记 delivered；rejected
          （回执被拒）与 blocked（额度预留耗尽）记 rate_limited；
          transport（结果不明）与读回不明记 delivery_failed；
          围栏零发出记 superseded_drop。
        - ``cancelled`` / ``failed``：投递被取消 / _deliver 抛
          非取消异常。两者共用同一段台账 state 分类（第二轮确认
          复审 P3-1，两模式不得再分叉漂移），仅零发出时的
          outcome 码不同：先按台账原始 state 分类——complete/
          partial → 清记录、delivered；ambiguous → 清记录（按
          可能已送达保守处理，防双回复）、delivery_failed；
          failed/无行（had=False）→ 保留记录，cancelled 记
          superseded_drop、failed 记 delivery_failed；读回失败
          （had=None）→ 清记录、delivery_failed（保守口径同
          R2）。state 仍停 in_flight 时先经台账
          mark_turn_ambiguous 收敛为 ambiguous 再按该分支判定
          ——发送在途未落定即结果不明，而 _deliver 自身的收尾
          写常被新消息推进的代次围栏拒绝、行会停在 in_flight，
          不收敛则会被误判零发出保留记录。

        读回只用台账公开方法 turn_had_send_with_state（三态
        had + 原始 state）。清理均走同一性校验的
        _clear_vision_handoff，不误删后登记的新记录；接管记录
        在接管命中时已 pop，此处再清为 no-op 兜底。来源回合
        自有记录因有发出被清除时，以其代次抬升已服务水位
        （P4-5）。本方法自身的异常由 shielded 包装层吞记，
        绝不掩盖投递的原始结局。
        """
        user_id = ctx.parsed.user_id
        had, state = await self.ledger.turn_had_send_with_state(
            str(user_id), ctx.turn_id
        )
        if mode in ("cancelled", "failed"):
            # cancelled 与 failed 共用同一段 state 分类（P3-1）：
            # 先把仍停 in_flight 的行收敛为 ambiguous 再分类，
            # 两模式仅零发出的 outcome 码不同（见 else 分支）。
            if state == "in_flight":
                # 发送在途未落定、结果不明：先把台账行从
                # in_flight 收敛为 ambiguous（收敛失败不阻断
                # 收尾，按读回原值继续分类），其后按 ambiguous
                # 分支判定清/留与 outcome。
                try:
                    if await self.ledger.mark_turn_ambiguous(
                        str(user_id), ctx.turn_id
                    ):
                        state = "ambiguous"
                except Exception as exc:  # noqa: BLE001 — 收敛失败只记类型名
                    logger.warning(
                        "vision ambiguous convergence failed: %s",
                        type(exc).__name__,
                    )
            if state in ("complete", "partial"):
                clear, outcome = True, "delivered"
            elif state == "ambiguous":
                clear, outcome = True, "delivery_failed"
            elif had is None:
                clear, outcome = True, "delivery_failed"
            elif had:
                clear, outcome = True, "delivered"
            else:
                clear = False
                outcome = (
                    "superseded_drop" if mode == "cancelled" else "delivery_failed"
                )
        else:  # returned
            clear = bool(sent or had is not False or drop_reason == "record_fence")
            if sent or drop_reason == "record_fence" or (
                drop_reason == "fence" and had is True
            ):
                outcome = "delivered"
            elif drop_reason == "rejected":
                outcome = "rate_limited"
            elif drop_reason == "transport":
                outcome = "delivery_failed"
            elif blocked or drop_reason == "blocked":
                outcome = "rate_limited"
            elif had is None:
                outcome = "delivery_failed"
            else:
                outcome = "superseded_drop"
        if clear:
            # 有发出/可能有发出 → 清除接力记录：来源回合清自己登记
            # 的那条，接管回合一并清理已被消费的记录条目。
            own_record = ctx.scratch.get("vision_handoff_record")
            own_cleared = (
                own_record is not None
                and self._vision_handoff.get(str(user_id)) is own_record
            )
            self._clear_vision_handoff(user_id, own_record)
            self._clear_vision_handoff(
                user_id, ctx.scratch.get("vision_handoff_taken")
            )
            if own_cleared:
                # 来源回合自有记录因有发出被清除（P4-5）：该图片
                # 已服务，以其代次抬升水位，防更旧图片迟到登记
                # 后被补回复。
                self._raise_vision_served_generation(
                    user_id, own_record.delivery_generation
                )
        if outcome == "delivered":
            # 视觉回复确已投递（台账事实判定，含 cancelled/
            # failed 模式下读回 complete/partial 的重判）：把筛
            # 查 clear 的图片描述落存为对话记忆观察条目，后续
            # 文字回合可引用（2026-11-01 用户拍板持久保留）。
            await self._persist_vision_observation(ctx)
        self._log_vision_outcome(ctx, outcome)

    async def _run_vision_settlement_shielded(self, ctx, *, mode: str, **kwargs) -> bool:
        """以 shield 运行投递收尾直至落定，返回等待期间是否收到取消。

        收尾（台账读回判定 + 清/留记录 + outcome）整体在一个独立
        task 内经 asyncio.shield 等待（确认复审 R1）：等待被取消
        时不丢收尾——记下取消、继续等收尾 task 落定再返回，由
        调用方在收尾完成后按原语义处置取消/异常。收尾自身异常
        只记类型名，绝不掩盖投递原始结局。
        """
        task = asyncio.ensure_future(
            self._settle_vision_delivery(ctx, mode=mode, **kwargs)
        )
        saw_cancel = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                saw_cancel = True
            except Exception:  # noqa: BLE001 — 收尾异常在下方统一吞记
                break
        if task.done() and not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning(
                    "vision delivery settlement failed: %s", type(exc).__name__
                )
        return saw_cancel

    async def _stage_deliver(self, ctx) -> bool:
        """Deliver：额度原子预留 → 节奏发送 → 台账记账与结果日志。"""
        parsed = ctx.parsed
        user_id = parsed.user_id
        started = ctx.started
        vision_active = bool(ctx.scratch.get("vision_active"))
        if vision_active:
            # 视觉投递中标记（确认复审 R1）：投递未落定期间拒绝
            # 接力接管；退出（含异常/取消）在 finally 中于收尾
            # 全部完成后清位——收尾在各 except/正常段内先行落定，
            # finally 只清标记，不与收尾竞速。
            self._mark_vision_delivering(user_id)
        try:
            if ctx.route == "direct":
                # 原尾段路径在发送前登记普通流（新消息可取消未发尾泡）。
                self._register_ordinary_stream(user_id)
            prefix = ctx.scratch.get("catchup_prefix")
            if prefix and ctx.plan:
                ctx.plan = [prefix + ctx.plan[0], *ctx.plan[1:]]
            if ctx.needs_reserve:
                plan, reservation = self._reserve_multipart(user_id, ctx.plan, parsed.text)
                if plan is None or reservation is None:
                    self.repetition.finish(user_id, ctx.repeat_token, False)
                    self._log("rate_limited", len(parsed.text), True,
                              (time.monotonic() - started) * 1000)
                    ctx.result = False
                    return False
                ctx.plan, ctx.reservation = plan, reservation
            try:
                sent, blocked, drop_reason = await self._deliver(
                    user_id, parsed.text, ctx.plan,
                    store=not ctx.tools_used if ctx.route == "direct" else True,
                    delivery_generation=ctx.delivery_generation,
                    pack_generation=ctx.pack_generation,
                    safety_epoch=ctx.safety_epoch,
                    repeat_token=ctx.repeat_token,
                    turn_id=ctx.turn_id,
                    reservation=ctx.reservation,
                )
            except asyncio.CancelledError:
                # 视觉回合投递被取消（确认复审 R1）：不再只记一条
                # superseded_drop 了事——先经 shielded 收尾按台账
                # 事实判定清/留接力记录与 outcome（读回本身被取消
                # 也等它落定），收尾完成后再原样上抛取消（取消
                # 语义不变）。非视觉回合口径不变、直接上抛。
                if vision_active:
                    await self._run_vision_settlement_shielded(ctx, mode="cancelled")
                raise
            except Exception:
                # _deliver 非取消异常的最小收敛（确认复审 R5）：
                # 视觉回合经 failed 模式收尾——与 cancelled 共用
                # 台账 state 分类判定清/留记录与 outcome，然后
                # 原样上抛（异常传播语义不变）。
                if vision_active:
                    await self._run_vision_settlement_shielded(ctx, mode="failed")
                raise
            if vision_active:
                # _deliver 返回后的收尾整体 shielded（确认复审
                # R1）：had_send 判定 + 清记录 + outcome 不得因
                # 此窗口内到达的取消被跳过；收尾落定后若等待期间
                # 收到过取消，按取消语义上抛。
                saw_cancel = await self._run_vision_settlement_shielded(
                    ctx,
                    mode="returned",
                    sent=sent,
                    blocked=blocked,
                    drop_reason=drop_reason,
                )
                if saw_cancel:
                    raise asyncio.CancelledError()
            self._log(
                "rate_limited" if blocked and not sent else "message",
                len(parsed.text), True, (time.monotonic() - started) * 1000,
            )
            ctx.result = sent
            return False
        finally:
            if vision_active:
                self._unmark_vision_delivering(user_id)


class LaneExecutor:
    """按道执行池（方案 §5.3 并发预算）：池在 dispatch 前取得许可，

    事件任务完成才释放——总并发封顶由此在传输层强制，而非靠各处理
    路内部自觉。critical 专职池与普通/control 共享池彼此独立。
    """

    def __init__(self, pool_size: int):
        self.pool_size = pool_size
        self.semaphore = asyncio.Semaphore(pool_size)


# 普通/control 共享 worker 池与 critical 专职池的默认规模（方案 §5.3）。
ORDINARY_POOL_SIZE = 8
CRITICAL_POOL_SIZE = 2


def create_app(settings: Settings | None = None, **processor_kwargs) -> FastAPI:
    """创建可注入依赖的 FastAPI 应用，生产入口默认读取环境配置。"""
    settings = settings or load_settings()
    pipeline_cfg = settings.app.get("pipeline", {}) or {}
    health_enabled = settings.app.get("connection_health", {}).get("enabled") is True
    bus = EventBus(
        ordinary_capacity=int(pipeline_cfg.get("ordinary_queue", 200)),
        control_capacity=int(pipeline_cfg.get("control_queue", 50)),
        critical_capacity=int(pipeline_cfg.get("critical_queue", 20)),
        group_capacity=int(pipeline_cfg.get("group_queue", 200)),
        notice_capacity=int(pipeline_cfg.get("event_queue", 200)),
        request_capacity=int(pipeline_cfg.get("request_queue", 50)),
    )
    # N3 事件面入队前预算（方案 §4 C7）：notice 滑动窗口预算与同键
    # 合并器；request 处置状态机落库（与 claim 同库不同表）。
    notice_budget = ingress_events.NoticeRateBudget(
        int(pipeline_cfg.get("event_notice_rate", 600))
    )
    processor = EventProcessor(settings, **processor_kwargs)
    # Stage 0 的 claim 存储（方案 §5.3）：分区容量 + 进程代次 + 终态淘汰
    # 语义与进程内 ClaimStore 完全一致，P5 起落库持久化（KD-C08 关闭）：
    # 与 processor 共用同一 Database，重启后已接受事件仍可判重。
    claim_store = SqliteClaimStore(
        processor.database,
        ordinary_capacity=int(pipeline_cfg.get("ordinary_claim_capacity", 10000)),
        reserved_capacity=int(pipeline_cfg.get("reserved_claim_capacity", 1000)),
        group_capacity=int(pipeline_cfg.get("group_claim_capacity", 10000)),
    )
    # 事件标识（webhook 解析出的 message_id）→ claim 键，供消费者在
    # 事件终结时 finish claim；claim_store 本身只认完整三元组键。
    claim_keys: dict[tuple[str, str], tuple[str, str, str]] = {}
    # N3 request 处置面（方案 §4 C6/C7）：状态机与 processor 共用同
    # 一 Database；策略对象可在运行期经 app.state 替换。处置策略由
    # 配置 requests 段装配（2026-10-31 产品接线）：friend/group 分
    # 类型模式 + 自动同意名单，缺省 manual/空与接线前逐字一致。
    # 策略存在自动同意可能（has_auto）时才装配平台处置通道；
    # 全 manual 时通道恒 None（不启用平台处置动作，事件只落库
    # 待人工），与接线前行为一致。
    request_store = SqliteRequestStore(processor.database)
    requests_cfg = settings.app.get("requests", {}) or {}
    if str(requests_cfg.get("group") or "manual") == "approve_all":
        # 纵深防御（Review P3-2）：group 的 approve_all 已被 Pydantic
        # 配置层禁止（RequestsConfig.group 不含该值），生产加载路径
        # 不可达；但本组合根消费的是原始 dict 视图，手工构造的
        # Settings 仍可注入——在此显式拒绝，保证「全自动同意只限
        # 好友申请」的产品边界不依赖单一校验层。
        raise RuntimeError(
            "requests.group does not support approve_all; "
            "auto-approve-all is limited to friend requests"
        )
    request_policy = ingress_events.RequestPolicy(
        friend_mode=str(requests_cfg.get("friend") or "manual"),
        group_mode=str(requests_cfg.get("group") or "manual"),
        approve_user_ids=requests_cfg.get("approve_user_ids", ()) or (),
        approve_group_ids=requests_cfg.get("approve_group_ids", ()) or (),
    )
    request_channel = None
    if request_policy.has_auto():
        gateway = getattr(processor.sender, "action_gateway", None)
        if gateway is None:
            # fail fast：自动处置已配置但发送端无网关，拒绝静默
            # 降级为 manual——配置与装配不一致必须在启动期暴露，
            # 不能让「已开启全自动同意」在运行期无声失效。
            raise RuntimeError(
                "requests auto-approve is configured but the sender has no "
                "action_gateway; refusing to silently degrade to manual"
            )
        request_channel = ingress_events.GatewayRequestChannel(
            gateway,
            principal=PRINCIPAL_SYSTEM,
            # 确认语义由产品配置层的常开决策承担：全自动同意是用户
            # 2026-10-31 拍板的产品决策，requests 段即其落点；通道
            # 契约要求接线层显式注入 confirmed，此处据实注入 True。
            confirmed=True,
            # set_friend_add_request / set_group_add_request 在网关的
            # 目标派生均为 account 级（gateway._TARGET_KIND_BY_ACTION），
            # 故授权上下文按 account 圈定，不放大到其他资源维度。
            authorization=AuthorizationContext(
                authorizer="requests-auto-policy", account=True
            ),
        )
    notice_coalescer = ingress_events.NoticeCoalescer()
    # 关怀控制面接线（P6）：偏好存储或由配置 proactive_care.store_path
    # 给出仓外路径、或由调用方注入 processor（care_preferences 参数）；
    # 两者皆无时不接线（命令面不存在、忘掉不扩围），与生产现状一致。
    # 发送门与调度器共享 processor.delivery（同一 DeliveryService、
    # 同一额度账本——P5 注记的共享记账要求）；调度器以投递代次戳记
    # 待发项，用户新消息推进代次即自动失效未发关怀。主动关怀开关仍
    # 由配置 proactive_care.enabled 决定，缺省 false 时门恒关、零发送，
    # 接线本身不产生任何发送（触发节奏参数属 D2 另立阶段）。
    care_cfg = settings.app.get("proactive_care", {}) or {}
    store_path = care_cfg.get("store_path")
    if isinstance(store_path, str) and not store_path.strip():
        # 空字符串视为误配置：告警并按未配置处理，不静默装配半成品。
        logger.warning("proactive_care.store_path is empty; care surface not assembled")
        store_path = None
    if store_path and processor.care_preferences is None:
        # 相对路径以 settings.root 为基准解析，不依赖进程 CWD（P6
        # Review P4）；解析后若落在仓库内，CarePreferences 构造会
        # 直接抛错 fail-fast，不会把同意数据写进仓库。
        care_path = Path(str(store_path)).expanduser()
        if not care_path.is_absolute():
            care_path = settings.root / care_path
        processor.care_preferences = CarePreferences(care_path)
    care_gate = None
    care_scheduler = None
    if processor.care_preferences is not None:
        care_gate = CareSendGate(
            processor.care_preferences,
            processor.delivery,
            whitelist=processor.care_whitelist,
            current_safety=processor.current_care_safety,
            config=settings.app,
            clock=processor.care_clock,
        )
        care_scheduler = CareScheduler(
            care_gate,
            clock=processor.care_clock,
            generation_of=processor.ledger.delivery_generation,
        )
        processor.care_gate = care_gate
        processor.care_scheduler = care_scheduler
    # 进程内调度（§5.8）：托管连接健康与关怀触发两类任务的生命周期，
    # 由 lifespan 启停；关停时先停调度（在途发送按 ambiguous 口径收尾），
    # 再走既有 drain（上限 10s，见 lifespan）。
    scheduler = InProcessScheduler()
    # 任务注册在组合根完成（与 app 同生命周期，P6 Review P3-4）：
    # lifespan 可重复进入（TestClient 复用/进程内重启），注册若放在
    # lifespan 内，第二次进入会因重名 ValueError 直接失败。
    # 连接健康实例则延迟到首次 lifespan 进入时构造：其构造会校验
    # napcat_base_url 必须是 loopback 地址，属运行时环境断言——既有
    # 夹具以假 URL 建 app 但从不进入 lifespan，构造时机须与 P6 前
    # （lifespan 内构造）保持一致，不得在组合期提前触发。
    health_box: dict = {}

    def _get_health():
        if "health" not in health_box:
            health_box["health"] = (
                ConnectionHealth(
                    settings.app.get("napcat_base_url", "http://127.0.0.1:3000")
                )
                if health_enabled
                else None
            )
        return health_box["health"]

    if health_enabled:
        # 连接健康迁为调度任务：其 run() 自带 tick + 退避循环（语义
        # 不变），调度器只托管启停与状态；tick 仍可独立单测。
        scheduler.register("connection_health", lambda: _get_health().run())
    # 断线重连补回接线（设计 §8.3）：设置读法与 care 同口径——正式
    # 配置树优先（§7.2 末段），手搓 Settings（config=None）时回退
    # app 字典并以同一默认值补齐，最终投影为 CatchupSettings 快照。
    # T1/T2/T4 模块一律函数内局部 import：本文件顶层导入不依赖未
    # 合流模块；store 在模块可用时无条件构造（仅建空表，与
    # request_store 同例）并挂 state，模块未合流（本单独立运行）时
    # 降级为不装配，使既有全量在本分支可独立通过——集成后模块恒
    # 存在，此降级分支不可达。仅 enabled 时才构造历史源/服务/观察
    # 者并注册调度任务，claim_store 复用组合根既有变量，不新造。
    _catchup_defaults = {
        "enabled": False,
        "prefix_enabled": True,
        "prefix_text": "（刚信号不好，才看到你之前的消息）",
    }
    _catchup_tree = (
        getattr(settings.config, "reconnect_catchup", None)
        if settings.config is not None
        else None
    )
    if _catchup_tree is not None:
        _catchup_values = {
            "enabled": bool(_catchup_tree.enabled),
            "prefix_enabled": bool(_catchup_tree.prefix_enabled),
            "prefix_text": str(_catchup_tree.prefix_text),
        }
    else:
        _catchup_dict = settings.app.get("reconnect_catchup", {}) or {}
        _catchup_values = {
            "enabled": _catchup_dict.get("enabled", _catchup_defaults["enabled"])
            is True,
            "prefix_enabled": _catchup_dict.get(
                "prefix_enabled", _catchup_defaults["prefix_enabled"]
            )
            is not False,
            "prefix_text": str(
                _catchup_dict.get("prefix_text")
                or _catchup_defaults["prefix_text"]
            ),
        }
    catchup_store = None
    catchup_watcher = None
    try:
        from companion.memory.catchup_scans import SqliteCatchupScanStore
    except ImportError:
        SqliteCatchupScanStore = None  # noqa: N806 — 未合流时降级不装配
    if SqliteCatchupScanStore is not None:
        catchup_store = SqliteCatchupScanStore(processor.database)
    if _catchup_values["enabled"]:
        from companion.adapters.history import GatewayHistorySource
        from companion.ingress.catchup import (
            CatchupService,
            CatchupSettings,
            RecoveryWatcher,
        )

        catchup_cfg = CatchupSettings(**_catchup_values)
        history_source = GatewayHistorySource(
            getattr(processor.sender, "actions", None)
        )
        catchup_service = CatchupService(
            processor,
            history=history_source,
            claim_store=claim_store,
            scan_store=catchup_store,
            ledger=processor.ledger,
            config=catchup_cfg,
        )
        catchup_watcher = RecoveryWatcher(lambda: _get_health(), catchup_service)
        scheduler.register("reconnect_catchup", catchup_watcher.run)
        if not health_enabled:
            logger.warning(
                "reconnect_catchup enabled but connection_health disabled; "
                "watcher inert"
            )
    if care_scheduler is not None:
        # 触发间隔以正式配置树为准（单一事实来源，P6 Review P4）；
        # 手搓 Settings（config=None）时才回退 raw 字典。
        if settings.config is not None:
            care_interval = float(
                settings.config.proactive_care.check_interval_seconds
            )
        else:
            care_interval = float(care_cfg.get("check_interval_seconds", 900))
        scheduler.register("care", lambda: care_scheduler.tick(care_interval))
    ordinary_executor = LaneExecutor(
        int(pipeline_cfg.get("ordinary_pool", ORDINARY_POOL_SIZE))
    )
    critical_executor = LaneExecutor(
        int(pipeline_cfg.get("critical_pool", CRITICAL_POOL_SIZE))
    )

    def finish_event(
        message_id: str,
        self_id: str = "",
        state: str = "done",
        claim_key: tuple[str, str, str] | None = None,
    ) -> None:
        if not message_id:
            return
        processor.pending_message_ids.discard(message_id)
        processor.seen_message_ids.add(message_id)
        if len(processor.seen_message_ids) > 10000:
            processor.seen_message_ids.clear()
        # claim 收尾优先用随事件传递的规范化完整键（适配器去空白后的
        # ID；原始报文 ID 可能带空白、与建 claim 时不一致，Codex P3
        # Review 第 2 轮 #5）；无随队键时回退 (message_id, self_id)
        # 复合映射反查（同 message_id 不同 self_id 互不覆盖）。
        mapped = claim_keys.pop((message_id, self_id), None)
        key = claim_key or mapped
        if key is not None:
            # 映射按规范化 ID 建键，而本函数的入参是原始报文 ID：再按
            # 规范化键清一次，防原始≠规范化时条目泄漏在映射里（P5
            # Review #11，EXEMPT 事件无随队键时同样靠这一步清理）。
            claim_keys.pop((key[2], key[1]), None)
            # 动态读 claim_store：运行期替换（如测试注入）同样生效。
            app.state.claim_store.finish(key, state)

    async def _dispatch_pooled(lane: str, event: dict, executor) -> None:
        """池许可先于 dispatch 取得：排队事件不占执行并发。"""
        message_id = str(event.get("message_id", ""))
        self_id = str(event.get("self_id", ""))
        claim_key = event.get("_claim_key")
        if message_id:
            processor.pending_message_ids.add(message_id)
        try:
            await executor.semaphore.acquire()
        except asyncio.CancelledError:
            # 关停时在许可等待处取消：事件未分发，claim 终结为 abandoned，
            # pending 与映射一并清理，不留不可淘汰占位（Review #8）。
            finish_event(message_id, self_id, "abandoned", claim_key)
            raise
        task = None
        try:
            # Do not await ordinary work here: safety and crisis routes run promptly.
            task = await processor.dispatch(event)
        except Exception as exc:
            logger.exception("event dispatch failed: %s", type(exc).__name__)
        if task is None:
            executor.semaphore.release()
            finish_event(message_id, self_id, claim_key=claim_key)
        else:
            def _release(_done, ex=executor, mid=message_id, sid=self_id, ck=claim_key):
                ex.semaphore.release()
                finish_event(mid, sid, claim_key=ck)

            task.add_done_callback(_release)

    async def consume_events():
        """普通/control 共享池消费循环（control 优先出队）。"""
        while True:
            lane, event = await bus.consume()
            try:
                await _dispatch_pooled(lane, event, ordinary_executor)
            finally:
                bus.task_done(lane)

    async def consume_critical_events():
        """critical 专职池独立消费循环：普通池占满不得挡住危机事件。"""
        while True:
            event = await bus.consume_critical()
            try:
                await _dispatch_pooled("critical", event, critical_executor)
            finally:
                bus.task_done("critical")

    async def consume_event_lane_loop():
        """N3 事件道消费循环：只跑事件/群聊子链，永不进私聊 Stage 链。

        group → 群聊子链观察（处理后 finish 其 claim；抛错时 claim
        以 abandoned 终结并记 exception 日志）；notice → 先 release
        合并器取回合并计数、再登记；request → 处置状态机子链。
        store/channel/policy/coalescer 一律动态读 app.state，运行期
        替换（如接线处置通道）即时生效。
        """
        # 启动清扫（有界，只做一次）：把上一进程遗留的 pending
        # request 重新入队续驱。清扫只收 pending——processing 遗留
        # 已由 store 代次恢复转 ambiguous、永不自动重驱；重复入队
        # 由 claim_processing 原子占用兜底，同一申请不会处置两次。
        # 入队失败的条目留待下次清扫/重投，不报错、不阻断启动。
        try:
            pending_records = app.state.request_store.list_pending(limit=200)
        except Exception:  # noqa: BLE001 — 清扫失败不阻断消费者启动
            logger.exception("request pending sweep failed")
            pending_records = []
        # 同进程队列中已有同键信封的 pending 不重复入队：那条信封
        # 本身会被本消费者驱动，不属于「无人再驱动」的遗留；重复
        # 入队虽有 claim_processing 兜底，也会多出一条 duplicate
        # 登记。队列外 pending（上一进程遗留 / 入队失败孤儿）照常清扫。
        queued_keys = set()
        for queued in list(getattr(bus.request_queue, "_queue", ())):
            queued_ev = queued.get("classified") if isinstance(queued, dict) else None
            if isinstance(queued_ev, RequestEvent):
                queued_keys.add(
                    request_key(
                        queued_ev.self_id,
                        queued_ev.request_type,
                        queued_ev.sub_type,
                        queued_ev.flag,
                    )
                )
        for record in pending_records:
            key = record.key
            if key in queued_keys:
                continue
            recovered = RequestEvent(
                platform="onebot",
                self_id=key[0],
                request_type=key[1],
                sub_type=key[2] or None,
                user_id=record.user_id,
                group_id=record.group_id,
                flag=key[3],
                raw={
                    "post_type": "request",
                    "request_type": key[1],
                    "sub_type": key[2] or None,
                    "recovered": True,
                },
            )
            bus.try_publish("request", {"classified": recovered})
        while True:
            kind, item = await bus.consume_event_lane()
            try:
                if kind == "group":
                    claim_key = item.get("claim_key")
                    try:
                        # N4：观察与（开关开时的）群回复合并为一条通路；
                        # 开关关或未注入 responder 时 handle_group_message
                        # 只观察，与 N3 行为逐项一致。
                        await ingress_events.handle_group_message(
                            processor,
                            item["classified"],
                            responder=(
                                processor.group_responder
                                if processor.group_reply_enabled
                                else None
                            ),
                            blocked_persons=processor.group_reply_blocked_persons,
                            allowed_persons=processor.group_reply_allowed_persons,
                            allowed_roles=processor.group_reply_allowed_roles,
                        )
                    except Exception:
                        logger.exception("group observe failed")
                        if claim_key is not None:
                            app.state.claim_store.finish(claim_key, "abandoned")
                    else:
                        if claim_key is not None:
                            app.state.claim_store.finish(claim_key)
                elif kind == "notice":
                    count = 0
                    merge_key = item.get("merge_key")
                    if merge_key is not None:
                        count = app.state.notice_coalescer.release(merge_key)
                    ingress_events.register_notice(
                        processor, item["classified"], coalesced=count
                    )
                elif kind == "request":
                    await ingress_events.handle_request(
                        processor,
                        item["classified"],
                        store=app.state.request_store,
                        channel=app.state.request_channel,
                        policy=app.state.request_policy,
                    )
            except Exception:  # noqa: BLE001 — 单项异常不得终止整个事件道循环
                logger.exception("event lane item failed: kind=%s", kind)
                if kind == "group":
                    # group 既有 claim finish/abandoned 语义之外再
                    # best-effort 收尾一次：已 finish 的返回 False、
                    # 无副作用，未 finish 的在此转 abandoned。
                    try:
                        claim_key = (
                            item.get("claim_key") if isinstance(item, dict) else None
                        )
                        if claim_key is not None:
                            app.state.claim_store.finish(claim_key, "abandoned")
                    except Exception:  # noqa: BLE001 — best-effort，不再上抛
                        logger.exception("group claim abandoned finish failed")
            finally:
                bus.task_done(kind)

    async def event_consumer():
        """完整消费入口：普通/control 循环与 critical 独立循环并发运行。

        两条循环各自独立（专职池调度隔离），合并为一个可取消的入口，
        保持 app.state.event_consumer 这一既有调用面。N3 起事件道
        消费循环一并纳入同一入口（三道彼此隔离、互不阻塞）。
        """
        await asyncio.gather(
            consume_events(), consume_critical_events(), consume_event_lane_loop()
        )

    async def start_consumer():
        task = asyncio.create_task(event_consumer())
        app.state.critical_consumer_task = task
        await asyncio.sleep(0)
        return task

    async def _drain() -> None:
        """关停排空：消费者仍在运行——先等三道队列出队完毕，再等在途任务终结。"""
        await bus.join()
        await processor.wait_idle()

    @asynccontextmanager
    async def lifespan(_app):
        # 调度注册在组合根完成（见 create_app）；lifespan 只负责启停，
        # 故可重复进入。健康实例仍在此处最先构造（与 P6 前的时机一
        # 致）：其 loopback 校验若失败，必须在插件装载/消费者启动等
        # 任何副作用之前抛出，不留泄漏的任务（P6 Review 第 2 轮 P3）。
        health = _get_health()
        if processor.plugin_registry is not None:
            # 插件生命周期托管（§5.5）：装载在消费者启动前完成，
            # 卸载在关停收尾时执行（dispose 语义）。装载中途失败时
            # lifespan 的 finally 尚未生效，须先尽力卸载已装载插件
            # 再让启动失败上抛，避免持资源插件泄漏。
            try:
                await processor.plugin_registry.load_all()
            except Exception:
                await processor.plugin_registry.unload_all()
                raise
        task = await start_consumer()
        _app.state.consumer_task = task
        if health is not None:
            _app.state.connection_health = health
        await scheduler.start()
        try:
            yield
        finally:
            # 关停先停调度：健康/关怀任务不再发起新动作；在途关怀发送被
            # 取消时按其 ambiguous 口径收尾（预占不退、永不重试）。停调度
            # 与下面的 drain 各自受 10s 上限约束且串行执行，最坏情况
            # 关停总时长可接近 20s（两段预算独立，P6 Review P4 注记）。
            await scheduler.stop(timeout=10.0)
            # 关停 drain（方案 §5.3）：先给队列与在途任务最多 10s 排空，
            # 超时才取消消费者与在途任务（在途发送按台账 ambiguous 语义
            # 由 wait_idle/close 的既有路径收尾），不再无条件立即取消。
            critical_task = getattr(_app.state, "critical_consumer_task", None)
            try:
                await asyncio.wait_for(_drain(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.error("shutdown drain timed out after 10s; cancelling in-flight work")
            task.cancel()
            if critical_task is not None:
                critical_task.cancel()
            await asyncio.gather(
                *(t for t in (task, critical_task) if t),
                return_exceptions=True,
            )
            processor.close()
            await processor.wait_idle()
            if processor.plugin_registry is not None:
                await processor.plugin_registry.unload_all()

    app = FastAPI(title="阿年 QQ Companion", lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware)
    app.state.event_bus = bus
    app.state.processor = processor
    app.state.metrics = processor.metrics
    app.state.scheduler = scheduler
    app.state.care_gate = care_gate
    app.state.care_scheduler = care_scheduler
    app.state.start_consumer = start_consumer
    app.state.event_consumer = event_consumer
    app.state.claim_store = claim_store
    app.state.critical_executor = critical_executor
    app.state.ordinary_executor = ordinary_executor
    # N3 事件面状态（见上方组合根注记）：request 处置通道仅在策略
    # 含自动同意时装配（见组合根），全 manual 时为 None。
    app.state.request_store = request_store
    app.state.request_channel = request_channel
    app.state.request_policy = request_policy
    app.state.notice_coalescer = notice_coalescer
    app.state.notice_budget = notice_budget
    # 断线重连补回（设计 §8.3）：扫描标记 store 恒挂载；watcher 仅在
    # 开关启用时构造并注册，未启用时为 None（与基线零装配口径一致）。
    app.state.catchup_scan_store = catchup_store
    app.state.catchup_watcher = catchup_watcher

    if health_enabled:
        @app.get("/health/ready")
        async def health_ready():
            task = getattr(app.state, "consumer_task", None)
            ready = task is not None and not task.done()
            return JSONResponse({"ready": ready}, status_code=200 if ready else 503)

    # 统一观测出口（§5.10）：恒注册，不依赖 health_enabled；部署口径
    # 为仅回环可达（deploy 的 companion-launch 绑定 127.0.0.1），端点
    # 只出聚合值、用户键为进程盐 HMAC。额度快照异常时降级为不带
    # 额度行并记 warning，渲染异常返回最小文本，端点不得 500。
    @app.get("/metrics")
    async def metrics_endpoint():
        try:
            quota_snapshot = processor.send_quota.snapshot()
        except Exception as exc:  # noqa: BLE001 — 额度不可用只降级额度行
            logger.warning(
                "metrics quota snapshot failed: %s", type(exc).__name__
            )
            quota_snapshot = None
        try:
            text = processor.metrics.render_text(quota_snapshot=quota_snapshot)
            # 渲染成败必须在抓取面可见（E 路修复 6）：成功追加 success 1，
            # 抓取方据此区分「指标为空」与「渲染挂了」。
            if not text.endswith("\n"):
                text += "\n"
            text += "companion_metrics_render_success 1\n"
        except Exception as exc:  # noqa: BLE001 — 渲染失败不得 500
            logger.error("metrics render failed: %s", type(exc).__name__)
            text = "# metrics unavailable\ncompanion_metrics_render_success 0\n"
        return PlainTextResponse(text)

    @app.post("/onebot/event")
    async def onebot_event(request: Request):
        # Stage 0 单一接受边界（方案 §5.3 v4，固定顺序）：
        # (1) 验签 → (2) 资格与键提取 → (3) 预筛分道 → (4) 按道容量
        # 预留 → (5) 原子 claim → (6) 204。普通/control 容量不足在
        # claim 之前 503；重复 claim 释放预留并回 204（已接受语义）；
        # critical 队列满则在接入任务内同步发送最小危机响应兜底。
        body = await request.body()
        if not settings.dev and not verify_signature(
            body, request.headers.get("X-Signature"), settings.onebot_secret
        ):
            return Response(status_code=401)
        try:
            event = json.loads(body)
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
        except (json.JSONDecodeError, ValueError):
            # 畸形报文走 Ignore(malformed) 路径而非 400（KD-C06）。
            processor._log_ignore(Ignore("malformed", "body:unparseable"))
            return Response(status_code=204)
        # N3 分类分流（方案 §3.3/§4 C1/C6/C7/C14）：先经 classify 收窄
        # 为判别联合四型，按型分发；私聊（含 group_temp）落回下方既有
        # Stage 0 路径逐字不变（classified 对私聊委托同一 normalize，
        # Ignore 已在此处理）。
        classified = processor.adapter.classify(event)
        if isinstance(classified, Ignore):
            # 不合格事件只落 reason_code 日志，不建 claim、不用占位键
            # （KD-C01/C02/C04/C05/C07）。
            processor._log_ignore(classified)
            return Response(status_code=204)
        if isinstance(classified, MetaEvent):
            # meta 只登记不入队（C14）：接入边界直接落 observer。
            ingress_events.register_meta(processor, classified)
            return Response(status_code=204)
        if isinstance(classified, NoticeEvent):
            summary = event_type_summary(event)
            mergeable = ingress_events.is_mergeable_notice(classified)
            merge_key = (
                ingress_events.notice_merge_key(classified) if mergeable else None
            )
            envelope = {"classified": classified, "merge_key": merge_key}
            if mergeable:
                # 先合并判定：在途同键只替换载荷、直接 204，不再过
                # rate/queue（C7 入队前合并）。
                decision = app.state.notice_coalescer.admit(classified, envelope)
                if decision == "coalesced":
                    return Response(status_code=204)
            # queue 路径：rate 预算或入队任一失败都是 shed（notice
            # 永不 503，上报方不靠 503 重投）；shed 时回滚合并器在途
            # 标记，防后续同键更新被误判为合并。
            shed = False
            if not notice_budget.allow(classified):
                shed = True
            elif not bus.try_publish("notice", envelope):
                shed = True
            if shed:
                if merge_key is not None:
                    app.state.notice_coalescer.release(merge_key)
                processor.metrics.record_event("notice", "shed")
                processor._log(
                    "notice", 0, False, 0.0,
                    reason_code="shed", event_type_summary=summary,
                )
            return Response(status_code=204)
        if isinstance(classified, RequestEvent):
            summary = event_type_summary(event)
            key = request_key(
                classified.self_id,
                classified.request_type,
                classified.sub_type,
                classified.flag,
            )
            try:
                begin = app.state.request_store.try_begin(
                    key,
                    user_id=classified.user_id,
                    group_id=classified.group_id,
                )
            except Exception:
                # C7：处置状态机落库失败回 503——这是 503 在事件面
                # 的唯一新增用途（宁可让上报方感知失败，也不收下后
                # 漏处置）。
                return Response(status_code=503)
            if begin.status in REQUEST_TERMINAL_STATES:
                # 已处置终态的重复投递：不重复入队，只记 duplicate。
                processor._log(
                    "ignored", 0, False, 0.0,
                    reason_code="duplicate_delivery",
                    event_type_summary=summary,
                )
                processor.metrics.record_event("request", "duplicate")
                return Response(status_code=204)
            if not bus.try_publish("request", {"classified": classified}):
                # request 保留容量不足同样回 503（C7），不静默丢弃。
                return Response(status_code=503)
            return Response(status_code=204)
        if isinstance(classified, InboundEvent) and classified.chat_type == "group":
            summary = event_type_summary(event)
            if classified.sub_type in ("anonymous", "notice"):
                # 匿名/系统型群消息无稳定身份：只登记，不进子链（§2.3）。
                ingress_events.register_group_detached(processor, classified)
                return Response(status_code=204)
            # 群 normal 消息进 claim + group 道（C3/C6）：claim 键三元
            # 组形态与 store 不变，仅 message_id 分量加 "g:" 场景前缀
            # ——键维度不变、防误杀优先：同一 message_id 的群/私聊不
            # 得互相误杀（P0 黄金报文 group_message_id_poisons_private
            # 钉死），故不按方案 C6 的「键不变」字面执行，docstring
            # 在此注明与 C6 的关系。
            claim_key = (
                classified.platform,
                classified.self_id,
                f"g:{classified.message_id}",
            )
            store = app.state.claim_store
            if store.get(claim_key) is not None:
                return Response(status_code=204)
            if bus.lane_full("group"):
                processor._log(
                    "group_message", len(classified.text), False, 0.0,
                    reason_code="shed", event_type_summary=summary,
                )
                processor.metrics.record_event("group", "shed")
                return Response(status_code=204)
            outcome = store.try_claim(claim_key, LANE_GROUP)
            if outcome is ClaimOutcome.DUPLICATE:
                return Response(status_code=204)
            if outcome is ClaimOutcome.FULL:
                processor._log(
                    "group_message", len(classified.text), False, 0.0,
                    reason_code="shed", event_type_summary=summary,
                )
                processor.metrics.record_event("group", "shed")
                return Response(status_code=204)
            # CLAIMED：入队失败释放 claim 并按 shed 登记（群流量永不
            # 回 503、永不挤占私聊/危机通道）。
            if not bus.try_publish(
                "group", {"classified": classified, "claim_key": claim_key}
            ):
                store.release(claim_key)
                processor._log(
                    "group_message", len(classified.text), False, 0.0,
                    reason_code="shed", event_type_summary=summary,
                )
                processor.metrics.record_event("group", "shed")
            return Response(status_code=204)
        # 其余（私聊含 group_temp）：classified 即同一 normalize 的产出。
        normalized = classified
        assert isinstance(normalized, InboundEvent)
        lane = classify_lane(
            normalized.text,
            keyword_match=lambda text: processor.safety.keyword_match(text),
        )
        store = app.state.claim_store
        claim_key = (normalized.platform, normalized.self_id, normalized.message_id)
        if store.get(claim_key) is not None:
            # 已接受事件的重投优先于容量判定：重复 claim 回 204（已接受
            # 语义），不因队列已满误报 503（Codex P3 Review #9）。本段与
            # try_claim 之间无 await，检查与建 claim 无竞态。
            return Response(status_code=204)
        if lane in ("ordinary", "control") and bus.lane_full(lane):
            # 队列容量不足：在 claim 之前拒绝，上游可重试（不留 claim）。
            return Response(status_code=503)
        outcome = store.try_claim(claim_key, lane)
        if outcome is ClaimOutcome.DUPLICATE:
            return Response(status_code=204)
        if outcome is ClaimOutcome.FULL:
            return Response(status_code=503)
        if outcome is ClaimOutcome.EXEMPT:
            # 保留分区亦满：关键词危机豁免 claim 直接进 critical 道，
            # 放弃本次去重并落审计（方案 §5.3 最终保底口径）。
            logger.warning(
                "claim exempted for keyword crisis: reason_code=claim_exempted"
            )
        claim_keys[(normalized.message_id, normalized.self_id)] = claim_key
        # 规范化 claim 键随队传递：消费者收尾直接用它终结 claim，不再
        # 从原始报文反查（原始 ID 可能带空白）。EXEMPT 不附：落库版虽在
        # EXEMPT 时留一条终态去重记录（补审修复），但无在途 claim 需收尾。
        queued_event = (
            event
            if outcome is ClaimOutcome.EXEMPT
            else {**event, "_claim_key": claim_key}
        )
        if not bus.try_publish(lane, queued_event):
            # 入队失败回滚：普通/control 释放 claim 后 503；critical 道
            # 满则同步最小危机响应兜底（不等通知回执、未通知变体）。
            if lane == "critical":
                logger.warning(
                    "critical lane overflow: reason_code=critical_lane_overflow"
                )
                claim_keys.pop((normalized.message_id, normalized.self_id), None)
                store.finish(claim_key)
                await processor.send_emergency_crisis(normalized.user_id)
                return Response(status_code=204)
            claim_keys.pop((normalized.message_id, normalized.self_id), None)
            store.release(claim_key)
            return Response(status_code=503)
        return Response(status_code=204)

    return app
