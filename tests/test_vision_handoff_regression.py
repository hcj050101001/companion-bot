"""视觉上下文接力回归（2026-10-31 真机分叉）。

真机分叉不在报文形状，而在交互模式：用户先发纯图、约 3 秒后
补发文本；图片回合的视觉生成成功，却被后到文本推进的投递代次
围栏静默丢弃。本文件以脱敏真实报文 fixture
（``tests/fixtures/real_inbound_image_20261031.json``，结构取自
2026-10-31 23:47 的 NapCat get_friend_msg_history 记录、值全为
合成）从 HTTP 入站口 ``POST /onebot/event`` 驱动完整管线，钉住
接力契约：

- 单发图片：视觉回复实际发出，且 events.jsonl 出现
  event_type="vision" 的 prepare / generate / outcome=delivered
  三步记录；prepare 的 fetch_codes 精确等于 FakeFetcher 走 HTTP
  直连路的 ``["http_ok"]``（本文件未接 NapCat 介导取图）；
- 纯图后补文本：恰有一条视觉回复发出、其生成入参同时含该
  文本与图片（接力生效），普通 brain 文本回复与媒体固定文案
  都不发；接管回合补发 via_handoff=true 的 prepare 步事件，
  outcomes 完整多重集恰为 {handed_off×1, delivered(via)×1}；
- 连续两张图（第二张自带图片）：只有第二张的回复发出，第一
  张 outcome=superseded_drop（自带图回合不被接力误吞）；
- 图片后补危机文本：危机固定话术发出，接力记录随危机推进
  失效，其后普通文本走普通 brain 回复、不再接力；
- 反向顺序：文本回合先行被 brain 门控拖住期间，图片回合完整
  走完并发出自己的视觉回复；文本回合不接管后到的图片（接力
  只许更早回合的图片被后续回合接管）；
- 接力 TTL 过期、风格切换、确定性时间问句都不接管；时间问句
  直答后不消耗接力记录，其后普通文本仍可接管；
- 先行文本被安全 judge 门控拖住（Respond 接管判定之前）期间，
  图片回合发出自己的视觉回复，先行文本放行后不得反向接管
  （代次接管守卫）；迟到旧图片回合的登记不得覆盖更新回合的
  记录（登记守卫），接管命中的是更新回合的图片；
- 视觉回复回执被拒记 rate_limited 且零发出保留接力记录，
  其后代次更大的新文本仍可接管（k）；先行文本在来源回合已
  收尾、投递中标记已清后放行，仍被代次接管守卫单独挡下、
  其后新文本接管成功（i2，守卫敏感性经删行自检）；新图被
  接管服务后抬升的已服务水位拒绝迟到旧图登记，其后文本不
  接管旧图（w）；回执被拒（rate_limited）记 rate_limited；
  视觉回复投递中被新消息取消时按台账 state 收尾
  （ambiguous → delivery_failed 且清记录、其后文本不接管）；
  气泡已被发送接受但已接受记账写被故障注入引爆（fault
  hook）时，failed 收尾与 cancelled 同源分类（in_flight 先
  收敛 ambiguous、清记录、记 delivery_failed，其后文本不
  接管，o）；投递中标记置位时 _take 拒绝且不消费的语义另有
  一条处理器级单元钉（u，非真路径）；视觉服务无凭据时
  prepare 事件带 prepared=false 与 error、纯图回固定文案记
  fixed_reply；较新图自行投递成功（无接管）后其 settle 抬升
  的已服务水位拒绝被门控拖住的更旧图迟到登记，其后文本不
  接管旧图（x，与 w 的区别在水位抬升来源是来源回合自身投递
  收尾而非被接管）；图片回合发送遇未知 status 经 transport
  映射为 ambiguous（drop_reason=transport）时记
  delivery_failed 且按可能已送达清记录，其后文本不接管
  （y，钉 returned 的 transport 分支）。

时序同步口径（P3-8 加固）：a–d、f–h 一律用 Fake 的
asyncio.Event 同步点（视觉生成进入点 ``generate_started`` 与
放行门 ``generate_gate``、brain 进入点）与条件等待确定先后，
不用 asyncio.sleep 抢时序；唯一保留的 sleep 是 f 中等待 TTL
真实过期的语义等待（已在用例内注明）。

全部离线：取图为 FakeFetcher（按引用返回预制 PNG/JPEG 字节）、
视觉生成为 FakeVisionClient（Event 门控、按 prompt/data URL
返回罐装回复）、发送为 tests.test_app.setup_app 的 FakeSender；
真实 VisionService 负责 prepare（fetch_codes 的真实产出路径
不被替身绕过）。不连真实 QQ、不真调视觉 API。
"""

import asyncio
import copy
import json
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from companion.brain import CompanionBrain
from companion.prompt_packs import PackRegistry
from companion.safety import crisis_reply
from companion.vision import IMAGE_DESCRIPTION_PROMPT, MEDIA_FIXED_REPLY, VisionService
from tests import test_app
from tests.fake_onebot import private_event
from tests.test_current_time import instant
from tests.test_current_time import registry as time_registry
from tests.test_prompt_packs import make_registry

setup_vision_app = test_app.setup_app

pytestmark = pytest.mark.timeout(10)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "real_inbound_image_20261031.json"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16

FIRST_REPLY = "视觉回复：第一张图"
SECOND_REPLY = "视觉回复：第二张图"
HANDOFF_REPLY = "视觉接力回复：你很好看"
HANDOFF_TEXT = "我好看吗"
BRAIN_REPLY = "测试回复"  # setup_app 的 FakeLLM 普通文本回复

SECOND_FILE = "fedcba9876543210fedcba9876543210.png"
SECOND_URL = (
    "https://multimedia.nt.qq.com.cn/download"
    "?appid=1406&fileid=SYNTHETIC2&rkey=SYNTHETIC"
)

# test_current_time 夹具时钟（2026-10-30T04:34:56Z）的直答文案，
# 与 tests/test_current_time.py 的既有断言逐字一致。
TIME_REPLY = "2026-10-30 Friday 12:34:56 (afternoon, Asia/Shanghai, UTC+08:00)"
# test_prompt_packs.make_registry 合成人设包的切换确认文案。
PACK_SWITCH_REPLY = "已切换到示例风格。"


# ---------------------------------------------------------------------------
# fixture 装载与事件变体
# ---------------------------------------------------------------------------


def _load_image_event(message_id, *, second=False):
    """读 fixture 并换上本用例唯一的 message_id（claim 去重需要）。

    second=True 时派生第二张图：换 file / url / raw_message 为另
    一组合成值，使 FakeFetcher 能按引用区分两张图。
    """
    event = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    event = copy.deepcopy(event)
    event["message_id"] = message_id
    if second:
        data = event["message"][0]["data"]
        data["file"] = SECOND_FILE
        data["url"] = SECOND_URL
        event["raw_message"] = f"[CQ:image,file={SECOND_FILE},url={SECOND_URL}]"
    return event


def _text_event(message_id, text):
    event = private_event("10001", text)
    event["message_id"] = message_id
    return event


# ---------------------------------------------------------------------------
# Fake 取图 / Fake 视觉生成（真实 VisionService.prepare 居中）
# ---------------------------------------------------------------------------


class FakeFetcher:
    """按引用返回预制字节：第二张图（SYNTHETIC2）为 JPEG，其余 PNG。

    prepare 的嗅探据此产出不同 MIME 的 data URL，视觉生成替身
    可据 data URL 区分「哪一张图」——两张纯图的 prompt 文本相同，
    只能靠图片载荷本身区分回复归属。本替身只实现 HTTP 直连路的
    ``fetch(ref)`` 接口、未接 NapCat 介导取图（VisionService 的
    napcat_fetcher 恒为 None），故 prepare 汇总的每条成功码恒为
    ``http_ok``（companion/vision.py prepare 的双路汇总口径：
    NapCat 成功记 napcat_ok、HTTP 成功记 http_ok）。
    """

    def __init__(self):
        self.calls = []

    async def fetch(self, ref, *, max_bytes):
        self.calls.append(ref)
        if "SYNTHETIC2" in ref:
            return JPEG_BYTES
        return PNG_BYTES


#: 内容筛查描述调用的罐装描述（F7）：无危机关键词，过测试网关。
DESCRIPTION_TEXT = "图片描述：一只小猫在草地上玩耍"


class FakeVisionClient:
    """视觉生成替身：记录 (prompt, data_urls)、Event 门控、罐装回复。

    同步钩子（P3-8）：``generate_started`` 在每次生成进入时置位，
    测试据此确定「图片回合确已 prepare 并登记接力、正在生成」，
    再投递后续消息；``gated=True`` 时生成在 ``generate_gate``
    放行前一直停住，测试在目标时点 set() 放行。``delay`` 为旧
    固定延迟口径，仅在未启用门控时生效（新用例不用）。

    回复归属规则：prompt 含接力文本 → 接力回复；data URL 含
    JPEG → 第二张图回复；其余 → 第一张图回复。model_name/model
    双备，供 generate 步日志取模型名（只断言非空字符串）。

    内容筛查的描述调用（Review F7，prompt 为
    IMAGE_DESCRIPTION_PROMPT）单独建模：记入 ``describe_calls``、
    即时返回罐装良性描述——本文件的门控与 ``generate_calls``
    计数语义只针对回复生成，描述不占用其名额、不受
    gate/delay 影响；``generate_started`` 照常置位（描述发生在
    登记接力之后，同步点语义不变）。
    """

    def __init__(self, *, delay=0.0, gated=False):
        self.delay = delay
        self.model_name = "deepseek-flash"
        self.model = "deepseek-flash"
        self.generate_calls = []
        self.describe_calls = []
        self.generate_started = asyncio.Event()
        self.generate_gate = asyncio.Event() if gated else None

    def has_credentials(self):
        return True

    async def generate_with_images(self, prompt, data_urls):
        if prompt == IMAGE_DESCRIPTION_PROMPT:
            self.describe_calls.append((prompt, tuple(data_urls)))
            self.generate_started.set()
            return DESCRIPTION_TEXT
        self.generate_calls.append((prompt, tuple(data_urls)))
        self.generate_started.set()
        if self.generate_gate is not None:
            await self.generate_gate.wait()
        elif self.delay:
            await asyncio.sleep(self.delay)
        if HANDOFF_TEXT in prompt:
            return HANDOFF_REPLY
        if any(str(url).startswith("data:image/jpeg") for url in data_urls):
            return SECOND_REPLY
        return FIRST_REPLY


def _make_service(*, delay=0.0, gated=False):
    fetcher = FakeFetcher()
    client = FakeVisionClient(delay=delay, gated=gated)
    service = VisionService(client=client, fetcher=fetcher)
    return service, fetcher, client


def _ready_processor(app, service):
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.vision_service = service
    return processor


# ---------------------------------------------------------------------------
# webhook 驱动、条件等待与 events.jsonl 读取
# ---------------------------------------------------------------------------


async def _post(client, event):
    body = json.dumps(event, ensure_ascii=False).encode()
    response = await client.post(
        "/onebot/event",
        content=body,
        headers={"X-Signature": test_app.signed(body)},
    )
    assert response.status_code == 204


@asynccontextmanager
async def _webhook_session(app):
    consumer = asyncio.create_task(app.state.event_consumer())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


async def _wait_until(predicate, description, *, timeout=5.0):
    """条件等待：轮询谓词直到成立（确定性同步，不是固定时序抢跑）。

    每 10ms 让出一次事件循环；超时即断言失败并带上等待目标，
    避免用例在错误时序下静默通过或挂死到 pytest timeout。
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"等待超时：{description}")
        await asyncio.sleep(0.01)


async def _wait_generate_started(client):
    await asyncio.wait_for(client.generate_started.wait(), timeout=5.0)


def _messages(calls):
    return [call["message"] for call in calls]


def _vision_records(app):
    path = app.state.processor.log_path
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("event_type") == "vision":
            records.append(record)
    return records


def _vision_steps(app, step):
    return [r for r in _vision_records(app) if r.get("vision_step") == step]


def _outcomes(app):
    return _vision_steps(app, "outcome")


def _outcome_multiset(app):
    """outcome 完整多重集：(outcome 码, via_handoff) 组合 → 条数。"""
    return Counter(
        (record.get("outcome"), bool(record.get("via_handoff")))
        for record in _outcomes(app)
    )


def _log_records(app):
    """events.jsonl 全部记录（不限 event_type），供通用事件断言。"""
    path = app.state.processor.log_path
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _HookedSender:
    """包住夹具 FakeSender 的发送替身（i/i2/k/l/m/o 的脚本化发送面）。

    ``hook(user_id, message, real)`` 先行：``real`` 是真正走夹具
    FakeSender 的无参协程（调用即把消息记入 calls、返回 ok 回执）；
    hook 返回非 None 时以其为本次发送结果且不再真发，返回 None
    时由本替身代为执行 ``real()``。hook 内可先 ``await real()``
    再阻塞（已发出未收尾窗口）、可直接阻塞（发送在途）、可直接
    返回失败回执（明确被拒），三种姿势互不污染 calls 语义。
    """

    def __init__(self, inner, hook):
        self._inner = inner
        self._hook = hook

    async def send_private_msg(self, user_id, message, *, priority="normal"):
        async def real():
            return await self._inner.send_private_msg(
                user_id, message, priority=priority
            )

        result = await self._hook(user_id, message, real)
        return await real() if result is None else result

    async def send_alert_private_msg(self, user_id, message):
        return await self._inner.send_alert_private_msg(user_id, message)


# ---------------------------------------------------------------------------
# a. 单发 fixture 图片：视觉回复发出 + vision 三步可观测记录
# ---------------------------------------------------------------------------


def test_single_fixture_image_delivers_and_logs_vision_steps(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service, fetcher, client = _make_service()
    _ready_processor(app, service)
    event = _load_image_event("handoff-a-1")
    fixture_url = event["message"][0]["data"]["url"]
    # fixture 形态自检：file_size 必须是字符串（真实形态），且
    # 发件人只留 user_id、URL 参数全为合成值。
    assert isinstance(event["message"][0]["data"]["file_size"], str)
    assert set(event["sender"]) == {"user_id"}

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, event)
            await test_app.drain(app)

    asyncio.run(run())

    assert _messages(calls) == [FIRST_REPLY]
    assert MEDIA_FIXED_REPLY not in _messages(calls)
    assert fetcher.calls == [fixture_url]  # 真实 prepare 经引用取图恰一次
    assert len(client.generate_calls) == 1
    prompt, data_urls = client.generate_calls[0]
    assert data_urls and data_urls[0].startswith("data:image/png;base64,")

    prepares = _vision_steps(app, "prepare")
    assert len(prepares) == 1, f"prepare 应恰一条: {prepares}"
    prepare = prepares[0]
    for key in ("refs", "received", "attached", "skipped", "fetch_codes"):
        assert key in prepare, f"prepare 记录缺字段 {key}"
    assert prepare["received"] == 1
    assert prepare["attached"] == 1
    assert prepare["skipped"] == 0
    # fetch_codes 精确断言：本文件 FakeFetcher 只走 HTTP 直连路、
    # NapCat 介导取图未接线（napcat_fetcher=None），prepare 双路
    # 汇总口径下唯一应得码值即 ["http_ok"]（companion/vision.py）。
    assert prepare["fetch_codes"] == ["http_ok"]

    generates = _vision_steps(app, "generate")
    assert generates, "events.jsonl 缺 vision generate 记录"
    assert any(record.get("ok") is True for record in generates)
    assert all(
        isinstance(record.get("model"), str) and record["model"]
        for record in generates
    )

    # outcome 完整多重集：恰一条 delivered、非接力。
    assert _outcome_multiset(app) == Counter({("delivered", False): 1})


# ---------------------------------------------------------------------------
# b. 纯图（生成门控）后补文本：接力恰一次，来源回合 handed_off
# ---------------------------------------------------------------------------


def test_delayed_pure_image_hands_off_to_following_text(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service, fetcher, client = _make_service(gated=True)
    _ready_processor(app, service)

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _load_image_event("handoff-b-image"))
            # Event 同步点：图片回合确已 prepare、登记接力并进入
            # 生成（被门控停住），此时才补文本——不靠 sleep 抢时序。
            await _wait_generate_started(client)
            await _post(http, _text_event("handoff-b-text", HANDOFF_TEXT))
            # 接管回合的视觉生成同样被门控停住：等第二次生成确实
            # 进入后统一放行，两回合的收尾顺序由契约本身决定。
            await _wait_until(
                lambda: len(client.generate_calls) == 2,
                "接管回合进入视觉生成",
            )
            client.generate_gate.set()
            await test_app.drain(app)

    asyncio.run(run())

    # 恰有一条回复发出，且是接力视觉回复：来源回合的纯图回复、
    # 普通 brain 文本回复、媒体固定文案都不许出现。
    assert _messages(calls) == [HANDOFF_REPLY]
    assert BRAIN_REPLY not in _messages(calls)
    assert MEDIA_FIXED_REPLY not in _messages(calls)
    assert FIRST_REPLY not in _messages(calls)

    # 接力生成的入参同时含补发文本与图片载荷。
    assert any(
        HANDOFF_TEXT in prompt
        and data_urls
        and any(str(url).startswith("data:image/png") for url in data_urls)
        for prompt, data_urls in client.generate_calls
    ), f"无含接力文本+图片的生成调用: {client.generate_calls}"

    # outcome 完整多重集：来源 handed_off（非接力标记）与接管
    # delivered（via_handoff=true）各恰一条、无其他 outcome。
    assert _outcome_multiset(app) == Counter(
        {("handed_off", False): 1, ("delivered", True): 1}
    )

    # 接管回合补发 prepare 步事件：两条 prepare 中恰一条标
    # via_handoff=true，且其 fetch_codes 沿用来源记录的 ["http_ok"]。
    prepares = _vision_steps(app, "prepare")
    assert len(prepares) == 2, f"prepare 应为来源+接管共两条: {prepares}"
    assert Counter(bool(r.get("via_handoff")) for r in prepares) == Counter(
        {False: 1, True: 1}
    )
    handoff_prepare = next(r for r in prepares if r.get("via_handoff") is True)
    assert handoff_prepare["fetch_codes"] == ["http_ok"]
    assert all(r["fetch_codes"] == ["http_ok"] for r in prepares)


# ---------------------------------------------------------------------------
# c. 连续两张图：第二张自带图不走接力，第一张 superseded_drop
# ---------------------------------------------------------------------------


def test_second_image_with_own_picture_supersedes_first(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service, fetcher, client = _make_service(gated=True)
    _ready_processor(app, service)

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _load_image_event("handoff-c-first"))
            # Event 同步点：第一张确已登记接力并进入生成（门控
            # 停住）后，第二张才到达——先后关系确定，不靠 sleep。
            await _wait_generate_started(client)
            await _post(
                http, _load_image_event("handoff-c-second", second=True)
            )
            await _wait_until(
                lambda: len(client.generate_calls) == 2,
                "第二张图进入视觉生成",
            )
            client.generate_gate.set()
            await test_app.drain(app)

    asyncio.run(run())

    assert _messages(calls) == [SECOND_REPLY]
    assert len(client.generate_calls) == 2  # 两张都生成、只有第二张投递
    assert len(fetcher.calls) == 2

    # outcome 完整多重集：第一张 superseded_drop、第二张
    # delivered，各恰一条且都不带 via_handoff（自带图回合不是
    # 接力，不得被接力误吞或误标）。
    assert _outcome_multiset(app) == Counter(
        {("superseded_drop", False): 1, ("delivered", False): 1}
    )


# ---------------------------------------------------------------------------
# d. 图片后补危机文本：危机话术发出、接力失效、其后普通文本不接力
# ---------------------------------------------------------------------------


def test_crisis_after_image_invalidates_handoff(setup_vision_app):
    app, calls, _, settings = setup_vision_app
    service, fetcher, client = _make_service(gated=True)
    _ready_processor(app, service)
    expected_crisis = crisis_reply(
        settings.safety["helplines"], False, settings.safety
    )

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _load_image_event("handoff-d-image"))
            # Event 同步点：图片回合确已登记接力并进入生成（门控
            # 停住）后，危机文本才到达——危机推进必在登记之后。
            await _wait_generate_started(client)
            await _post(http, _text_event("handoff-d-crisis", "想自杀"))
            # 条件等待危机固定话术实际发出（危机推进已发生）再发
            # 普通文本，避免与危机处置竞速——断言的是推进后的
            # 失效语义。
            await _wait_until(
                lambda: expected_crisis in _messages(calls),
                "危机固定话术发出",
            )
            await _post(http, _text_event("handoff-d-after", HANDOFF_TEXT))
            await _wait_until(
                lambda: BRAIN_REPLY in _messages(calls),
                "危机后普通文本走 brain 回复",
            )
            client.generate_gate.set()
            await test_app.drain(app)

    asyncio.run(run())

    # 危机话术先行、其后普通文本走普通 brain 回复；图片视觉回复
    # 与接力回复都不许发出。
    assert _messages(calls) == [expected_crisis, BRAIN_REPLY]
    assert FIRST_REPLY not in _messages(calls)
    assert HANDOFF_REPLY not in _messages(calls)
    # 视觉生成只发生过来源图片回合那一次：其后普通文本未接管
    # 已失效的接力记录再生成。
    assert len(client.generate_calls) == 1
    assert HANDOFF_TEXT not in client.generate_calls[0][0]
    # outcome 完整多重集：来源图片回合的回复最终被投递围栏丢弃
    # 恰一条 superseded_drop；无任何 via_handoff outcome。
    assert _outcome_multiset(app) == Counter({("superseded_drop", False): 1})


# ---------------------------------------------------------------------------
# e. 反向顺序（钉 P1）：文本回合先行被 brain 门控，图片后到并走完
# ---------------------------------------------------------------------------


def test_prior_text_turn_does_not_take_later_image(setup_vision_app):
    app, calls, _, settings = setup_vision_app
    service, fetcher, client = _make_service()
    processor = _ready_processor(app, service)
    brain_entered = asyncio.Event()
    brain_gate = asyncio.Event()

    class GatedLLM:
        """普通 brain 替身：多气泡生成被 Event 门控拖住。"""

        async def generate(self, _prompt):
            return BRAIN_REPLY

        async def generate_json(self, _prompt):
            brain_entered.set()
            await brain_gate.wait()
            return json.dumps({"bubbles": [BRAIN_REPLY]}, ensure_ascii=False)

    processor.brain = CompanionBrain(settings.persona, GatedLLM())
    prior_text = "先聊聊别的"

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _text_event("handoff-e-text", prior_text))
            # Event 同步点：文本回合确已进入 brain 生成（其
            # Respond 入口的接管判定此时早已过、当时无任何接力
            # 记录）并被门控拖住，图片回合才到达。
            await asyncio.wait_for(brain_entered.wait(), timeout=5.0)
            await _post(http, _load_image_event("handoff-e-image"))
            # 图片回合完整走完：自己的视觉回复实际发出。
            await _wait_until(
                lambda: FIRST_REPLY in _messages(calls),
                "图片回合发出自己的视觉回复",
            )
            brain_gate.set()
            await test_app.drain(app)
            # 来源回合投递成功后接力记录已被清除：其后再发普通
            # 文本只能走普通 brain 回复，不得复用已投递图片接管。
            await _post(http, _text_event("handoff-e-after", HANDOFF_TEXT))
            await test_app.drain(app)

    asyncio.run(run())

    # 图片回复恰一条；被门控的先行文本回合按既有普通流语义被
    # 后到消息取消（其 brain 回复不发出），放行后紧随的普通文本
    # 回复为普通 brain 回复；接力回复从未出现。
    assert _messages(calls) == [FIRST_REPLY, BRAIN_REPLY]
    assert HANDOFF_REPLY not in _messages(calls)
    # 视觉生成恰一次且是图片回合自己的纯图生成：入参不含先行
    # 文本（先行回合没有、也无法反向接管后到的图片）。
    assert len(client.generate_calls) == 1
    assert prior_text not in client.generate_calls[0][0]
    assert HANDOFF_TEXT not in client.generate_calls[0][0]
    # outcome 完整多重集：只有图片回合自己的 delivered（非接力）
    # 一条；全程无 via_handoff=true 的 outcome。
    assert _outcome_multiset(app) == Counter({("delivered", False): 1})


# ---------------------------------------------------------------------------
# f. 接力 TTL 过期：登记后过期再发普通文本，不接管
# ---------------------------------------------------------------------------


def test_expired_handoff_is_not_taken(setup_vision_app, monkeypatch):
    app, calls, _, _ = setup_vision_app
    service, fetcher, client = _make_service(gated=True)
    _ready_processor(app, service)
    # TTL 常量定位：companion/ingress/__init__.py 模块级
    # VISION_HANDOFF_TTL_SECONDS（_take_vision_handoff 按模块全局
    # 名查找），monkeypatch 模块属性到极小值，不改生产文件。
    monkeypatch.setattr(
        "companion.ingress.VISION_HANDOFF_TTL_SECONDS", 0.05
    )

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _load_image_event("handoff-f-image"))
            # Event 同步点：接力确已登记、生成被门控停住。
            await _wait_generate_started(client)
            # 语义性等待（保留 sleep 并注明）：TTL 是真实时间
            # 窗口，必须等窗口真实过期，等的正是语义本身而非
            # 任务调度时序；0.2s 为 0.05s TTL 的四倍余量。
            await asyncio.sleep(0.2)
            await _post(http, _text_event("handoff-f-text", HANDOFF_TEXT))
            await _wait_until(
                lambda: BRAIN_REPLY in _messages(calls),
                "TTL 过期后普通文本走 brain 回复",
            )
            client.generate_gate.set()
            await test_app.drain(app)

    asyncio.run(run())

    # 未接管：普通文本走普通 brain 回复，接力回复不发出；图片
    # 回合自己的回复被后到回合的投递围栏丢弃。
    assert _messages(calls) == [BRAIN_REPLY]
    assert HANDOFF_REPLY not in _messages(calls)
    assert FIRST_REPLY not in _messages(calls)
    # 视觉生成恰一次（来源回合）：过期记录未被再生成接管。
    assert len(client.generate_calls) == 1
    assert HANDOFF_TEXT not in client.generate_calls[0][0]
    # outcome 完整多重集：恰一条 superseded_drop、无 via_handoff。
    assert _outcome_multiset(app) == Counter({("superseded_drop", False): 1})


# ---------------------------------------------------------------------------
# g. 风格切换失效：登记后切换人设包，其后普通文本不接管
# ---------------------------------------------------------------------------


def test_pack_switch_invalidates_handoff(setup_vision_app, tmp_path):
    app, calls, _, _ = setup_vision_app
    service, fetcher, client = _make_service(gated=True)
    processor = _ready_processor(app, service)
    # 夹具姿势与 tests/test_prompt_packs.py 一致：合成 registry
    # 落 tmp_path 后替换 processor.pack_registry，命令文本用既有
    # 真实命令「切换风格 friend」。
    make_registry(tmp_path)
    processor.pack_registry = PackRegistry(tmp_path)

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _load_image_event("handoff-g-image"))
            # Event 同步点：接力确已登记、生成被门控停住。
            await _wait_generate_started(client)
            await _post(http, _text_event("handoff-g-switch", "切换风格 friend"))
            await _wait_until(
                lambda: PACK_SWITCH_REPLY in _messages(calls),
                "风格切换确认发出",
            )
            await _post(http, _text_event("handoff-g-text", HANDOFF_TEXT))
            await _wait_until(
                lambda: BRAIN_REPLY in _messages(calls),
                "切换后普通文本走 brain 回复",
            )
            client.generate_gate.set()
            await test_app.drain(app)

    asyncio.run(run())

    # 切换确实生效（pack 代次已推进）且其后普通文本未接管：
    # 走普通 brain 回复，接力回复不发出。
    assert asyncio.run(processor.conversation.pack_snapshot("10001"))[0] == "friend"
    assert _messages(calls) == [PACK_SWITCH_REPLY, BRAIN_REPLY]
    assert HANDOFF_REPLY not in _messages(calls)
    assert FIRST_REPLY not in _messages(calls)
    assert len(client.generate_calls) == 1
    assert HANDOFF_TEXT not in client.generate_calls[0][0]
    # outcome 完整多重集：来源回合回复被围栏丢弃恰一条
    # superseded_drop、无 via_handoff。
    assert _outcome_multiset(app) == Counter({("superseded_drop", False): 1})


# ---------------------------------------------------------------------------
# h. 时间问句不接管：走确定性直答且不消耗接力记录
# ---------------------------------------------------------------------------


def test_current_time_question_does_not_consume_handoff(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service, fetcher, client = _make_service(gated=True)
    processor = _ready_processor(app, service)
    # current_time 夹具姿势与 tests/test_current_time.py 一致：
    # 固定时钟的 ReadOnlyRegistry 替换默认 registry。
    processor.read_only_registry = time_registry(
        lambda: instant("2026-10-30T04:34:56")
    )

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _load_image_event("handoff-h-image"))
            # Event 同步点：接力确已登记、生成被门控停住。
            await _wait_generate_started(client)
            await _post(http, _text_event("handoff-h-time", "现在几点"))
            # 时间问句走确定性直答（接管判定在该路由之后）：
            # 直答文案实际发出、未触发任何接力生成。
            await _wait_until(
                lambda: TIME_REPLY in _messages(calls),
                "时间问句确定性直答发出",
            )
            assert len(client.generate_calls) == 1
            # 接力记录未被时间问句消耗：其后普通文本仍可接管。
            await _post(http, _text_event("handoff-h-text", HANDOFF_TEXT))
            await _wait_until(
                lambda: len(client.generate_calls) == 2,
                "普通文本接管回合进入视觉生成",
            )
            client.generate_gate.set()
            await test_app.drain(app)

    asyncio.run(run())

    assert _messages(calls) == [TIME_REPLY, HANDOFF_REPLY]
    assert FIRST_REPLY not in _messages(calls)
    assert BRAIN_REPLY not in _messages(calls)
    # outcome 完整多重集：来源 handed_off 与接管 delivered(via)
    # 各恰一条；时间回合本身不产生任何 vision outcome。
    assert _outcome_multiset(app) == Counter(
        {("handed_off", False): 1, ("delivered", True): 1}
    )


# ---------------------------------------------------------------------------
# i. 先行文本被 judge 门控（Respond 之前）拖住，图片先行发出（钉 F1 接管守卫）
# ---------------------------------------------------------------------------


def test_prior_text_gated_before_respond_does_not_take_sent_image(setup_vision_app):
    app, calls, _, settings = setup_vision_app
    service, fetcher, client = _make_service()
    processor = _ready_processor(app, service)
    judge_entered = asyncio.Event()
    judge_gate = asyncio.Event()
    send_entered = asyncio.Event()
    send_gate = asyncio.Event()
    brain_entered = asyncio.Event()

    # 门控点选在 SafetyJudge 的 detect（Stage 3，dispatch 之后、
    # Respond 的 _take_vision_handoff 判定之前）。纯图片回合在
    # SafetyJudge 不经 detect（vision_deferred），但其内容筛查
    # （F7）在 Respond 内以描述文本同路调用 detect——替身按入参
    # 只拖先行文本回合，筛查调用不入门控。
    original_detect = processor.safety.detect

    async def gated_detect(text):
        # 只门控先行文本回合的 detect：图片回合的内容筛查（F7）
        # 在 Respond 内以描述文本同路调用 detect，不在本用例的
        # 门控范围内（按入参区分，先行文本为 HANDOFF_TEXT）。
        if text == HANDOFF_TEXT:
            judge_entered.set()
            await judge_gate.wait()
        return await original_detect(text)

    processor.safety.detect = gated_detect

    # 图片回复的发送在 FakeSender 内被记录（回复已发出）后、回执
    # 返回前被门控住：来源回合停在「已发出未收尾」窗口——此时
    # 接力记录仍在册，先行文本的迟到 Respond 只能被接管守卫
    # （投递中标记 + 代次顺序）挡下，不得反向接管已发出的图片。
    async def hook(_user_id, message, real):
        if message != FIRST_REPLY:
            return None
        result = await real()
        send_entered.set()
        await send_gate.wait()
        return result

    processor.sender = _HookedSender(processor.sender, hook)

    class RecordingLLM:
        """普通 brain 替身：只记录进入并回罐装气泡（不门控）。"""

        async def generate(self, _prompt):
            return BRAIN_REPLY

        async def generate_json(self, _prompt):
            brain_entered.set()
            return json.dumps({"bubbles": [BRAIN_REPLY]}, ensure_ascii=False)

    processor.brain = CompanionBrain(settings.persona, RecordingLLM())

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _text_event("handoff-i-text", HANDOFF_TEXT))
            # 先行文本确已进入 SafetyJudge 并被门控拖住（其
            # dispatch 已推进代次、Respond 尚未开始）。
            await asyncio.wait_for(judge_entered.wait(), timeout=5.0)
            await _post(http, _load_image_event("handoff-i-image"))
            # 图片回合完整走完生成与发送：视觉回复已由 FakeSender
            # 记下（恰一条），来源回合停在已发出未收尾窗口。
            await asyncio.wait_for(send_entered.wait(), timeout=5.0)
            assert _messages(calls) == [FIRST_REPLY]
            judge_gate.set()
            # 放行后的先行文本只能走普通 brain 路（brain 进入即
            # 证明其已过 _take 判定且未接管）；若守卫被删，它会
            # 改走视觉生成（generate_calls 增至 2）——两种结局都
            # 以条件等待收敛，不靠时序抢跑。
            await _wait_until(
                lambda: brain_entered.is_set() or len(client.generate_calls) >= 2,
                "先行文本放行后进入 brain 或（守卫缺失时）视觉生成",
            )
            send_gate.set()
            await test_app.drain(app)

    asyncio.run(run())

    # 图片回复恰一条；接力回复从未出现；先行文本的 brain 回复按
    # 既有普通流语义被后到代次围栏丢弃（与 e 的先行回合口径一致）。
    assert _messages(calls) == [FIRST_REPLY]
    assert HANDOFF_REPLY not in _messages(calls)
    assert brain_entered.is_set()
    # 视觉生成恰一次、是图片回合自己的纯图生成，入参不含先行文本。
    assert len(client.generate_calls) == 1
    assert HANDOFF_TEXT not in client.generate_calls[0][0]
    # outcome 完整多重集：只有图片回合自己的 delivered（非接力）
    # 一条；全程无 via_handoff=true 的 outcome。
    assert _outcome_multiset(app) == Counter({("delivered", False): 1})


# ---------------------------------------------------------------------------
# i2. 代次接管守卫单独钉（钉 P3-2）：来源已收尾、标记已清后仍拒绝先行文本
# ---------------------------------------------------------------------------


def test_generation_guard_alone_blocks_prior_text_after_source_settled(
    setup_vision_app,
):
    app, calls, _, settings = setup_vision_app
    service, fetcher, client = _make_service()
    processor = _ready_processor(app, service)
    judge_entered = asyncio.Event()
    judge_gate = asyncio.Event()
    brain_entered = asyncio.Event()

    # 与 i 同源的 judge 门控：先行文本停在 SafetyJudge（Respond
    # 的 _take_vision_handoff 判定之前），其投递代次记为 G1。
    original_detect = processor.safety.detect

    async def gated_detect(text):
        # 只门控先行文本回合的 detect：图片回合的内容筛查（F7）
        # 在 Respond 内以描述文本同路调用 detect，不在本用例的
        # 门控范围内（按入参区分，先行文本为 HANDOFF_TEXT）。
        if text == HANDOFF_TEXT:
            judge_entered.set()
            await judge_gate.wait()
        return await original_detect(text)

    processor.safety.detect = gated_detect

    # 图片回合（G2）的发送回执被拒（与 k/l 同源姿势）：来源以
    # rate_limited 收尾、零发出保留接力记录。与 i 的关键区别：
    # 放行先行文本之前来源已完整收尾、投递中标记已清——此时
    # 能拒绝先行文本（G1 < G2）接管的只剩 _take_vision_handoff
    # 的代次顺序守卫一条（守卫敏感性：临时删除该代次比较一行
    # 本用例必转红，见本轮汇报的自检证据）。
    async def hook(_user_id, message, _real):
        if message == FIRST_REPLY:
            return {"status": "rate_limited"}
        return None

    processor.sender = _HookedSender(processor.sender, hook)

    class RecordingLLM:
        """普通 brain 替身：只记录进入并回罐装气泡（不门控）。"""

        async def generate(self, _prompt):
            return BRAIN_REPLY

        async def generate_json(self, _prompt):
            brain_entered.set()
            return json.dumps({"bubbles": [BRAIN_REPLY]}, ensure_ascii=False)

    processor.brain = CompanionBrain(settings.persona, RecordingLLM())

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _text_event("handoff-i2-text", HANDOFF_TEXT))
            # 先行文本确已进入 SafetyJudge 并被门控拖住。
            await asyncio.wait_for(judge_entered.wait(), timeout=5.0)
            await _post(http, _load_image_event("handoff-i2-image"))
            # 来源收尾落定：vision outcome=rate_limited 已记且
            # 投递中标记已清（直接查处理器标记表，排除「标记
            # 守卫仍在位」这一替代解释——本用例要单独钉代次
            # 守卫，不许两条守卫混杂）。
            await _wait_until(
                lambda: any(
                    record.get("outcome") == "rate_limited"
                    for record in _outcomes(app)
                )
                and not processor._vision_delivering,
                "图片回合以 rate_limited 收尾且投递中标记已清",
            )
            judge_gate.set()
            # 先行文本放行：走普通 brain 路（brain 进入即证明其
            # 已过 _take 判定且未接管）；若代次守卫缺失，它会
            # 改走视觉生成（generate_calls 增至 2）——两种结局
            # 都以条件等待收敛，不靠时序抢跑。
            await _wait_until(
                lambda: brain_entered.is_set() or len(client.generate_calls) >= 2,
                "先行文本放行后进入 brain 或（守卫缺失时）视觉生成",
            )
            await test_app.drain(app)
            # 记录未被先行文本消费、也未失效：再投一条新文本
            # （G3，代次严格更大）应接管成功——证明挡住先行
            # 文本的是代次守卫而非记录已坏。
            await _post(http, _text_event("handoff-i2-after", HANDOFF_TEXT))
            await test_app.drain(app)

    asyncio.run(run())

    # 先行文本确走普通 brain 路（非接管），且全程无 via_handoff
    # outcome（见下方多重集）；其 brain 回复按代次围栏丢弃。
    assert brain_entered.is_set()
    # 发送集合：只有 G3 接管发出的接力回复；被拒的第一张视觉
    # 回复与先行文本的 brain 回复都未发出。
    assert _messages(calls) == [HANDOFF_REPLY]
    assert FIRST_REPLY not in _messages(calls)
    assert BRAIN_REPLY not in _messages(calls)
    # 视觉生成恰 2 次：图片回合自己 + G3 接管回合；先行文本未
    # 触发任何生成——只有图片回合自己与合格的后续回合生成过。
    assert len(client.generate_calls) == 2
    prompt, data_urls = client.generate_calls[1]
    assert HANDOFF_TEXT in prompt
    assert data_urls and data_urls[0].startswith("data:image/png")
    # outcome 完整多重集：来源 rate_limited 与 G3 接管
    # delivered(via_handoff) 各恰一条；先行文本不产生任何
    # vision outcome，更无 via_handoff=true 的条目。
    assert _outcome_multiset(app) == Counter(
        {("rate_limited", False): 1, ("delivered", True): 1}
    )


# ---------------------------------------------------------------------------
# j. 迟到旧图片回合的登记不覆盖新记录（钉 F1 登记守卫）
# ---------------------------------------------------------------------------


def test_late_first_image_registration_does_not_overwrite_second(setup_vision_app):
    app, calls, _, _ = setup_vision_app

    class FirstFetchGatedFetcher(FakeFetcher):
        """按引用门控：第一张图（非 SYNTHETIC2 引用）的 fetch 被拖住。"""

        def __init__(self):
            super().__init__()
            self.first_fetch_entered = asyncio.Event()
            self.first_fetch_gate = asyncio.Event()

        async def fetch(self, ref, *, max_bytes):
            if "SYNTHETIC2" not in ref:
                self.first_fetch_entered.set()
                await self.first_fetch_gate.wait()
            return await super().fetch(ref, max_bytes=max_bytes)

    fetcher = FirstFetchGatedFetcher()
    client = FakeVisionClient(gated=True)
    service = VisionService(client=client, fetcher=fetcher)
    _ready_processor(app, service)

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _load_image_event("handoff-j-first"))
            # 第一张确已进入 prepare 取图并被门控拖住（尚未登记）。
            await asyncio.wait_for(fetcher.first_fetch_entered.wait(), timeout=5.0)
            await _post(http, _load_image_event("handoff-j-second", second=True))
            # 第二张先行备好：登记接力并进入生成（被生成门控停住）
            # ——此时在册记录是第二张的（JPEG）。
            await _wait_generate_started(client)
            fetcher.first_fetch_gate.set()
            # 第一张迟到备好（PNG）：其登记被代次登记守卫拒绝后
            # 进入自己的生成（同被门控停住），在册记录仍是第二张。
            await _wait_until(
                lambda: len(client.generate_calls) == 2,
                "第一张迟到图进入视觉生成",
            )
            await _post(http, _text_event("handoff-j-text", HANDOFF_TEXT))
            await _wait_until(
                lambda: len(client.generate_calls) == 3,
                "接管回合进入视觉生成",
            )
            # 接管命中的是第二张图：生成入参同时含补发文本与
            # JPEG data URL（第一张是 PNG，据此区分归属）。
            prompt, data_urls = client.generate_calls[2]
            assert HANDOFF_TEXT in prompt
            assert data_urls and data_urls[0].startswith("data:image/jpeg")
            client.generate_gate.set()
            await test_app.drain(app)

    asyncio.run(run())

    assert _messages(calls) == [HANDOFF_REPLY]
    assert FIRST_REPLY not in _messages(calls)
    assert SECOND_REPLY not in _messages(calls)
    assert len(fetcher.calls) == 2
    assert len(client.generate_calls) == 3
    # outcome 完整多重集：第一张迟到回合的回复被围栏丢弃
    # superseded_drop、第二张被接管 handed_off、接管回合
    # delivered(via_handoff) 各恰一条。
    assert _outcome_multiset(app) == Counter(
        {
            ("superseded_drop", False): 1,
            ("handed_off", False): 1,
            ("delivered", True): 1,
        }
    )


# ---------------------------------------------------------------------------
# w. 已服务水位（钉 X4）：新图已被接管服务后，迟到旧图不得再登记
# ---------------------------------------------------------------------------


def test_late_image_registration_rejected_after_newer_image_served(
    setup_vision_app,
):
    app, calls, _, _ = setup_vision_app

    class FirstFetchGatedFetcher(FakeFetcher):
        """按引用门控（与 j 同源手法）：第一张图（非 SYNTHETIC2
        引用）的 fetch 被拖住，使其 prepare/登记整体迟到。"""

        def __init__(self):
            super().__init__()
            self.first_fetch_entered = asyncio.Event()
            self.first_fetch_gate = asyncio.Event()

        async def fetch(self, ref, *, max_bytes):
            if "SYNTHETIC2" not in ref:
                self.first_fetch_entered.set()
                await self.first_fetch_gate.wait()
            return await super().fetch(ref, max_bytes=max_bytes)

    fetcher = FirstFetchGatedFetcher()
    client = FakeVisionClient(gated=True)
    service = VisionService(client=client, fetcher=fetcher)
    _ready_processor(app, service)

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _load_image_event("handoff-w-old"))
            # 旧图（第一张，PNG）确已进入 prepare 取图并被门控
            # 拖住（尚未登记），其投递代次最小。
            await asyncio.wait_for(fetcher.first_fetch_entered.wait(), timeout=5.0)
            await _post(http, _load_image_event("handoff-w-new", second=True))
            # 新图（第二张，JPEG）先行备好：登记接力并进入生成
            # （被生成门控停住）。
            await _wait_generate_started(client)
            await _post(http, _text_event("handoff-w-text", HANDOFF_TEXT))
            # 接管命中新图：第二次生成入参同时含补发文本与 JPEG
            # data URL——接管成功这一刻已按契约以新图代次抬升
            # 已服务水位。
            await _wait_until(
                lambda: len(client.generate_calls) == 2,
                "接管回合进入视觉生成",
            )
            prompt, data_urls = client.generate_calls[1]
            assert HANDOFF_TEXT in prompt
            assert data_urls and data_urls[0].startswith("data:image/jpeg")
            # 放行旧图取图：旧图迟到备好后试图登记——此时在册
            # 记录已被接管 pop（字典为空，登记守卫的「已有记录」
            # 比较无从触发），能拒绝这次登记的只有已服务水位
            # （旧图代次 ≤ 水位）；旧图仍走完自己的生成（同被
            # 门控停住）。
            fetcher.first_fetch_gate.set()
            await _wait_until(
                lambda: len(client.generate_calls) == 3,
                "迟到旧图进入视觉生成",
            )
            client.generate_gate.set()
            await test_app.drain(app)
            # 其后普通文本：旧图未登记 → 不接管，走普通 brain
            # 回复；接管对象只能是未被服务的图片，此处无接管。
            await _post(http, _text_event("handoff-w-after", HANDOFF_TEXT))
            await _wait_until(
                lambda: BRAIN_REPLY in _messages(calls),
                "旧图未登记时其后普通文本走 brain 回复",
            )
            await test_app.drain(app)

    asyncio.run(run())

    # 发送集合：接管回复（新图）+ 其后普通文本的 brain 回复，
    # 恰各一条；两张图自己的视觉回复都未发出。
    assert _messages(calls) == [HANDOFF_REPLY, BRAIN_REPLY]
    assert FIRST_REPLY not in _messages(calls)
    assert SECOND_REPLY not in _messages(calls)
    # 视觉生成恰 3 次（新图自己、接管、旧图自己）：其后文本未
    # 接管旧图再生成；旧图自己的生成入参为其 PNG 且不含补发
    # 文本（图片归属据 data URL 区分）。
    assert len(client.generate_calls) == 3
    prompt, data_urls = client.generate_calls[2]
    assert HANDOFF_TEXT not in prompt
    assert data_urls and data_urls[0].startswith("data:image/png")
    assert len(fetcher.calls) == 2
    # outcome 完整多重集：新图来源 handed_off、接管
    # delivered(via_handoff)、迟到旧图 superseded_drop 各恰
    # 一条；无旧图的 via_handoff outcome。
    assert _outcome_multiset(app) == Counter(
        {
            ("handed_off", False): 1,
            ("delivered", True): 1,
            ("superseded_drop", False): 1,
        }
    )


# ---------------------------------------------------------------------------
# k. 视觉回复回执被拒：rate_limited + 零发出保留记录，其后新文本接管
# ---------------------------------------------------------------------------


def test_vision_receipt_rejected_keeps_handoff_for_newer_text(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service, fetcher, client = _make_service()
    processor = _ready_processor(app, service)

    # 生产可达姿势（钉 P3-3）：与 l 同源——网关回执被拒（hook
    # 返回 {"status": "rate_limited"}）。_send_once 的三态映射
    # （companion/ingress/__init__.py _send_once）：回执
    # accepted → SendOutcome(True)；ambiguous →
    # SendOutcome(False, uncertain=True)；其余（rejected）→
    # SendOutcome(False, blocked=True)。plain SendOutcome(False)
    # （sent/uncertain/blocked 全假）不映射任何生产回执，旧 k 在
    # _send_once 接缝手工构造该组合属生产不可达，已删除；本
    # 用例改走回执被拒路：_deliver 以 drop_reason="rejected"
    # 收尾，来源记 rate_limited、零发出且台账 state=failed
    # （had=False）→ 接力记录保留，待其后文本接管。
    async def hook(_user_id, message, _real):
        if message == FIRST_REPLY:
            return {"status": "rate_limited"}
        return None

    processor.sender = _HookedSender(processor.sender, hook)

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _load_image_event("handoff-k-image"))
            await test_app.drain(app)
            # 来源回合已以 rate_limited 收尾（零发出、记录保留）。
            assert _outcome_multiset(app) == Counter({("rate_limited", False): 1})
            assert _messages(calls) == []
            # 一条新的普通文本（投递代次严格更大）接管同一图片。
            await _post(http, _text_event("handoff-k-text", HANDOFF_TEXT))
            await test_app.drain(app)

    asyncio.run(run())

    # 被拒的视觉回复未发出；其后新文本接管同一图片并发出接力
    # 回复（入参仍为第一张的 PNG data URL）。
    assert _messages(calls) == [HANDOFF_REPLY]
    assert FIRST_REPLY not in _messages(calls)
    assert len(client.generate_calls) == 2
    prompt, data_urls = client.generate_calls[1]
    assert HANDOFF_TEXT in prompt
    assert data_urls and data_urls[0].startswith("data:image/png")
    # outcome 完整多重集：来源 rate_limited 与接管
    # delivered(via_handoff) 各恰一条（来源回合早已收尾，不再
    # 补记 handed_off）。
    assert _outcome_multiset(app) == Counter(
        {("rate_limited", False): 1, ("delivered", True): 1}
    )


# ---------------------------------------------------------------------------
# l. 视觉回复回执被拒：vision outcome=rate_limited（与通用事件并存）
# ---------------------------------------------------------------------------


def test_vision_receipt_rejected_logs_rate_limited_outcome(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service, fetcher, client = _make_service()
    processor = _ready_processor(app, service)

    # 网关回执被拒姿势：FakeSender 对视觉回复返回 rate_limited
    # 类回执（receipt_from_result 映射 rejected → _send_once
    # blocked → _deliver drop_reason="rejected"）。
    async def hook(_user_id, message, _real):
        if message == FIRST_REPLY:
            return {"status": "rate_limited"}
        return None

    processor.sender = _HookedSender(processor.sender, hook)

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _load_image_event("handoff-l-image"))
            await test_app.drain(app)

    asyncio.run(run())

    # 被拒即未发出：calls 无任何消息；视觉生成确已发生一次。
    assert _messages(calls) == []
    assert len(client.generate_calls) == 1
    # vision outcome 完整多重集：恰一条 rate_limited（非接力）——
    # 不得被旧口径误记为 delivery_failed / superseded_drop。
    assert _outcome_multiset(app) == Counter({("rate_limited", False): 1})
    # 通用事件流同回合另有一条 event_type="rate_limited" 记录
    # （Deliver 的通用记账），与 vision outcome 并存不冲突。
    assert any(
        record.get("event_type") == "rate_limited" for record in _log_records(app)
    )


# ---------------------------------------------------------------------------
# m. 视觉回复投递中被新消息取消：按台账 state（ambiguous）收尾
# ---------------------------------------------------------------------------


def test_vision_delivery_cancelled_in_flight_settles_by_ledger_state(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service, fetcher, client = _make_service()
    processor = _ready_processor(app, service)
    send_entered = asyncio.Event()
    send_gate = asyncio.Event()

    # 视觉回复的发送在 FakeSender 内被 Event 阻塞（未及真发）：
    # 来源回合停在 _deliver 的 in_flight 段，新消息 dispatch 的
    # 取消落在此窗口，台账收尾态即 ambiguous（结果不明）。
    async def hook(_user_id, message, _real):
        if message == FIRST_REPLY:
            send_entered.set()
            await send_gate.wait()
        return None

    processor.sender = _HookedSender(processor.sender, hook)

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _load_image_event("handoff-m-image"))
            await asyncio.wait_for(send_entered.wait(), timeout=5.0)
            # 投普通文本触发取消/顶替：来源投递未落定期间不得
            # 接管（投递中标记），文本走普通 brain 回复。
            await _post(http, _text_event("handoff-m-text", HANDOFF_TEXT))
            await _wait_until(
                lambda: BRAIN_REPLY in _messages(calls),
                "取消后普通文本走 brain 回复",
            )
            # 来源收尾落定：台账 ambiguous → outcome delivery_failed。
            await _wait_until(
                lambda: any(
                    record.get("outcome") == "delivery_failed"
                    for record in _outcomes(app)
                ),
                "来源回合按 ambiguous 收尾记 delivery_failed",
            )
            send_gate.set()
            await test_app.drain(app)
            # 记录已清：再投普通文本仍走 brain、不接管。
            await _post(http, _text_event("handoff-m-after", HANDOFF_TEXT))
            await test_app.drain(app)

    asyncio.run(run())

    # 台账事实核对：来源回合 state=ambiguous 且按可能已送达判
    # had_send=True——与 outcome delivery_failed + 清记录同源。
    had, state = asyncio.run(
        processor.ledger.turn_had_send_with_state("10001", "handoff-m-image")
    )
    assert (had, state) == (True, "ambiguous")
    # 视觉回复从未真发；两条普通文本都是 brain 回复、无接力回复。
    assert _messages(calls) == [BRAIN_REPLY, BRAIN_REPLY]
    assert FIRST_REPLY not in _messages(calls)
    assert HANDOFF_REPLY not in _messages(calls)
    # 视觉生成恰一次（来源回合自己的纯图生成），无人接管再生成。
    assert len(client.generate_calls) == 1
    # outcome 完整多重集：恰一条 delivery_failed（非接力）。
    assert _outcome_multiset(app) == Counter({("delivery_failed", False): 1})


# ---------------------------------------------------------------------------
# n. prepare 失败分支：无凭据 → prepare 事件 prepared=false + fixed_reply
# ---------------------------------------------------------------------------


def test_prepare_without_credentials_logs_failed_prepare_and_fixed_reply(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service, fetcher, client = _make_service()
    _ready_processor(app, service)
    # 无凭据姿势：真实 VisionService.prepare 在取图前直接返回
    # None（has_credentials=False），不触网、不调生成。
    client.has_credentials = lambda: False

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _load_image_event("handoff-n-image"))
            await test_app.drain(app)

    asyncio.run(run())

    # 纯图回原固定文案恰一条；未取图、未生成。
    assert _messages(calls) == [MEDIA_FIXED_REPLY]
    assert fetcher.calls == []
    assert client.generate_calls == []
    # prepare 步事件恰一条且带失败形态：prepared=false 与 error
    # 字段（无凭据口径为 "unavailable"，不含任何正文/凭证）。
    prepares = _vision_steps(app, "prepare")
    assert len(prepares) == 1, f"prepare 应恰一条: {prepares}"
    assert prepares[0]["prepared"] is False
    assert prepares[0]["error"] == "unavailable"
    assert prepares[0]["attached"] == 0
    # outcome 完整多重集：恰一条 fixed_reply（非接力），无 generate 步。
    assert _vision_steps(app, "generate") == []
    assert _outcome_multiset(app) == Counter({("fixed_reply", False): 1})


# ---------------------------------------------------------------------------
# u. 处理器级单元钉（钉 P4-3 可确定部分）：投递中标记置位时 _take 拒绝
# ---------------------------------------------------------------------------


def test_unit_take_vision_handoff_blocked_while_delivering_mark_set(
    setup_vision_app,
):
    """单元级钉（非真路径，文件内注明）：脱离 HTTP 入站口，
    直接构造 processor 并操作其内部方法。HTTP 路径下「投递中
    标记置位窗口」只能靠发送 hook 卡时序（见 i/m），此处以
    单元级确定性钉住标记本身的语义：置位时 _take 返回 None
    且记录未被消费、仍在册；清标记后同一记录可被接管。方法
    名以生产实现为准（_register_vision_handoff /
    _mark_vision_delivering / _unmark_vision_delivering /
    _take_vision_handoff，companion/ingress/__init__.py）。
    """
    app, calls, _, _ = setup_vision_app
    processor = app.state.processor

    prepared = SimpleNamespace(
        data_urls=("data:image/png;base64,AAAA",),
        received=1,
        fetch_codes=("http_ok",),
    )

    def ctx_for(generation):
        return SimpleNamespace(
            parsed=SimpleNamespace(user_id="10001"),
            pack_generation=0,
            safety_epoch=0,
            delivery_generation=generation,
        )

    record = processor._register_vision_handoff(ctx_for(1), prepared)
    assert processor._vision_handoff.get("10001") is record

    processor._mark_vision_delivering("10001")
    try:
        # 标记置位：即使接管回合代次严格更大，_take 也拒绝，
        # 且不消费、不清除记录。
        assert processor._take_vision_handoff(ctx_for(2)) is None
        assert record.consumed is False
        assert processor._vision_handoff.get("10001") is record
    finally:
        processor._unmark_vision_delivering("10001")

    # 清标记后同一记录可被接管：置 consumed 并 pop 在册条目。
    taken = processor._take_vision_handoff(ctx_for(2))
    assert taken is record
    assert record.consumed is True
    assert processor._vision_handoff.get("10001") is None


# ---------------------------------------------------------------------------
# o. 气泡已接受但记账写故障：failed 收尾与 cancelled 同源分类（钉 P3-1）
# ---------------------------------------------------------------------------


def test_accepted_bubble_ledger_write_fault_settles_failed_like_cancelled(
    setup_vision_app,
):
    app, calls, _, _ = setup_vision_app
    service, fetcher, client = _make_service()
    processor = _ready_processor(app, service)
    db = processor.ledger.db

    # 故障注入姿势与 tests/test_memory_p5.py 同源：台账写在
    # 「台账行已写、历史投影未写」点调用 db.fault_hook
    # （"after_ledger_write"），钩子抛异常则整事务回滚。在
    # 视觉回复已被 FakeSender 接受之后才武装钩子，故下一个被
    # 引爆的写恰是「已接受记账写」（update_delivery complete）：
    # 气泡确已送达、记账未落，_deliver 抛非取消异常 →
    # _stage_deliver 以 failed 模式收尾。钩子一次性自清，不
    # 污染其后回合的台账写。
    def boom(point):
        assert point == "after_ledger_write"
        db.fault_hook = None
        raise RuntimeError("simulated crash between ledger write and projection")

    async def hook(_user_id, message, real):
        if message != FIRST_REPLY:
            return None
        result = await real()
        db.fault_hook = boom
        return result

    processor.sender = _HookedSender(processor.sender, hook)

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _load_image_event("handoff-o-image"))
            await test_app.drain(app)
            # failed 收尾已清记录：其后普通文本不接管同一图片。
            await _post(http, _text_event("handoff-o-after", HANDOFF_TEXT))
            await test_app.drain(app)

    asyncio.run(run())

    # 气泡确已发出（FakeSender 已接受）；其后文本未接管同一
    # 图片，走普通 brain 回复；视觉生成恰一次。
    assert _messages(calls) == [FIRST_REPLY, BRAIN_REPLY]
    assert HANDOFF_REPLY not in _messages(calls)
    assert len(client.generate_calls) == 1
    # 台账事实核对：记账写回滚后行停 in_flight，failed 收尾与
    # cancelled 同源——先收敛 ambiguous、按可能已送达判
    # had=True（与 m 的 cancelled 收尾口径逐字一致）。
    had, state = asyncio.run(
        processor.ledger.turn_had_send_with_state("10001", "handoff-o-image")
    )
    assert (had, state) == (True, "ambiguous")
    # outcome 完整多重集：恰一条 delivery_failed（非接力）——
    # 台账已收敛 ambiguous 仍归因 delivery_failed 而非
    # delivered/superseded_drop，且记录已清（上方文本不接管
    # 已证）。
    assert _outcome_multiset(app) == Counter({("delivery_failed", False): 1})


# ---------------------------------------------------------------------------
# x. 已服务水位·settle 抬升点（钉 P4-2）：较新图自行投递成功抬升水位
# ---------------------------------------------------------------------------


def test_late_image_registration_rejected_after_newer_image_self_delivered(
    setup_vision_app,
):
    app, calls, _, _ = setup_vision_app

    class FirstFetchGatedFetcher(FakeFetcher):
        """按引用门控（与 j/w 同源手法）：更旧的第一张图（非
        SYNTHETIC2 引用）的 fetch 被拖住，使其 prepare/登记
        整体迟到到较新图已服务之后。"""

        def __init__(self):
            super().__init__()
            self.first_fetch_entered = asyncio.Event()
            self.first_fetch_gate = asyncio.Event()

        async def fetch(self, ref, *, max_bytes):
            if "SYNTHETIC2" not in ref:
                self.first_fetch_entered.set()
                await self.first_fetch_gate.wait()
            return await super().fetch(ref, max_bytes=max_bytes)

    fetcher = FirstFetchGatedFetcher()
    client = FakeVisionClient(gated=True)
    service = VisionService(client=client, fetcher=fetcher)
    _ready_processor(app, service)

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _load_image_event("handoff-x-old"))
            # 旧图（第一张，PNG）确已进入 prepare 取图并被门控
            # 拖住（尚未登记），其投递代次最小。
            await asyncio.wait_for(fetcher.first_fetch_entered.wait(), timeout=5.0)
            await _post(http, _load_image_event("handoff-x-new", second=True))
            # 新图（第二张，JPEG）先行备好：登记接力并进入生成
            # （被生成门控停住）。
            await _wait_generate_started(client)
            client.generate_gate.set()
            # 与 w 的关键区别：新图的服务不来自任何接管——本
            # 回合无后续文本介入，新图靠自身投递成功被服务。
            # settle 落定点以其 outcome=delivered 出现为准：
            # _settle_vision_delivery 在记 outcome 之前已清自有
            # 记录并以其代次抬升已服务水位（settle 抬升点），
            # 故 outcome 可见即水位已抬升、不是仅凭消息已发
            # （calls 出现）推断收尾完成。
            await _wait_until(
                lambda: any(
                    record.get("outcome") == "delivered"
                    for record in _outcomes(app)
                ),
                "较新图自行投递成功并 settle 落定（水位已抬升）",
            )
            assert _messages(calls) == [SECOND_REPLY]
            # 放行旧图取图：旧图迟到备好后试图登记——此时在册
            # 记录已被新图 settle 清除（字典为空，登记守卫的
            # 「已有记录」比较无从触发），能拒绝这次登记的只有
            # settle 抬升的已服务水位（旧图代次 ≤ 水位）；旧图
            # 仍走完自己的生成（生成门已放行、直通）。
            fetcher.first_fetch_gate.set()
            await _wait_until(
                lambda: len(client.generate_calls) == 2,
                "迟到旧图进入视觉生成",
            )
            await test_app.drain(app)
            # 其后普通文本：旧图未登记 → 不接管，走普通 brain
            # 回复（文本内容用接力文本也一样不接管）。
            await _post(http, _text_event("handoff-x-after", HANDOFF_TEXT))
            await _wait_until(
                lambda: BRAIN_REPLY in _messages(calls),
                "旧图未登记时其后普通文本走 brain 回复",
            )
            await test_app.drain(app)

    asyncio.run(run())

    # 发送集合：新图自己的视觉回复 + 其后普通文本的 brain
    # 回复，恰各一条；旧图自己的视觉回复与接力回复都未发出。
    assert _messages(calls) == [SECOND_REPLY, BRAIN_REPLY]
    assert FIRST_REPLY not in _messages(calls)
    assert HANDOFF_REPLY not in _messages(calls)
    # 视觉生成恰 2 次（新图自己、旧图自己）：其后文本未接管
    # 旧图再生成。新图生成入参为 JPEG、旧图生成入参为 PNG，
    # 且两者都不含补发文本（无任何接管生成）。
    assert len(client.generate_calls) == 2
    _, new_data_urls = client.generate_calls[0]
    assert new_data_urls and new_data_urls[0].startswith("data:image/jpeg")
    prompt, data_urls = client.generate_calls[1]
    assert HANDOFF_TEXT not in prompt
    assert data_urls and data_urls[0].startswith("data:image/png")
    assert len(fetcher.calls) == 2
    # outcome 完整多重集：新图自行投递 delivered、迟到旧图
    # superseded_drop 各恰一条，均非接力；无 via_handoff 条目。
    assert _outcome_multiset(app) == Counter(
        {("delivered", False): 1, ("superseded_drop", False): 1}
    )


# ---------------------------------------------------------------------------
# y. returned 的 transport 分支（钉 P4-3）：未知 status → ambiguous 清记录
# ---------------------------------------------------------------------------


def test_vision_unknown_status_settles_transport_delivery_failed(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service, fetcher, client = _make_service()
    processor = _ready_processor(app, service)

    # 映射链（以实现为准，构造前已核对）：
    # companion/adapters/transport.py 的 receipt_from_result——
    # status 既不在 accepted 集 {ok, async, dry_run}、也不在
    # rejected 集 {rate_limited, disabled, unavailable, busy}
    # 时一律 ambiguous；未知 status "error" 即落此支。
    # companion/ingress/__init__.py 的 _send_once——回执
    # ambiguous → SendOutcome(False, uncertain=True)；
    # _deliver 据 uncertain 把台账行写为 ambiguous 并返回
    # drop_reason="transport"；_settle_vision_delivery 的
    # returned 模式据 transport 记 delivery_failed，且
    # ambiguous 按可能已送达（had=True）清接力记录。
    async def hook(_user_id, message, _real):
        if message == FIRST_REPLY:
            return {"status": "error"}
        return None

    processor.sender = _HookedSender(processor.sender, hook)

    async def run():
        async with _webhook_session(app) as http:
            await _post(http, _load_image_event("handoff-y-image"))
            await test_app.drain(app)
            # 记录已清：其后普通文本不接管同一图片，走普通
            # brain 回复。
            await _post(http, _text_event("handoff-y-after", HANDOFF_TEXT))
            await test_app.drain(app)

    asyncio.run(run())

    # 台账事实核对：发送结果不明落 ambiguous 且按可能已送达
    # 判 had_send=True——与 outcome delivery_failed + 清记录
    # 同源（returned 的 transport 分支，不是 m 的 cancelled
    # 在途取消收尾）。
    had, state = asyncio.run(
        processor.ledger.turn_had_send_with_state("10001", "handoff-y-image")
    )
    assert (had, state) == (True, "ambiguous")
    # 未知 status 的发送未被 FakeSender 真发（hook 直接返回）；
    # 其后文本是 brain 回复，接力回复与被顶替的视觉回复都
    # 不许出现。
    assert _messages(calls) == [BRAIN_REPLY]
    assert FIRST_REPLY not in _messages(calls)
    assert HANDOFF_REPLY not in _messages(calls)
    # 视觉生成恰一次（来源回合自己的纯图生成），无人接管再生成。
    assert len(client.generate_calls) == 1
    assert HANDOFF_TEXT not in client.generate_calls[0][0]
    # outcome 完整多重集：恰一条 delivery_failed（非接力）——
    # 不得误记为 rate_limited / superseded_drop / delivered。
    assert _outcome_multiset(app) == Counter({("delivery_failed", False): 1})
