"""图片内容危机筛查（Review F7）真路径测试。

F7 口径：图片内容经视觉模型描述成文本后，在回复生成之前过
既有危机网关筛查（与用户文字同一 SafetyGateway、同一
_detect_with_budget）；命中即严格走既有危机协议（固定话术 +
告警，逐字不变），描述不可得则按视觉失败收敛——未筛查的图
片内容不进入回复生成。

覆盖三条图片路径：纯图、图文混发、接力接管；外加模型 judge
命中、judge 不可用（D1 疑似危机）、clear 放行与描述委托的单
元钉。全部经 tests.test_app.setup_app 的完整 ingress 管线
（processor.handle）驱动；视觉服务为服务级替身，describe 按
调用序弹出预置描述。
"""

import asyncio
import base64
import json

import pytest

from companion.safety import crisis_reply
from companion.vision import (
    IMAGE_DESCRIPTION_PROMPT,
    MEDIA_FIXED_REPLY,
    PreparedImages,
    VisionService,
)
from tests import test_app
from tests.fake_onebot import private_event

setup_vision_app = test_app.setup_app

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")

BENIGN_DESCRIPTION = "图片里是一只小猫，趴在窗台上晒太阳。"
# 含测试配置危机关键词「想自杀」的转写形态描述（纸条逐字转写）。
CRISIS_DESCRIPTION = "图片中有一张手写的纸条，上面写着：我想自杀，旁边放着一把刀。"
# 无任何危机关键词的描述：只能靠模型 judge 命中。
JUDGE_ONLY_DESCRIPTION = "图片里一个人站在高楼天台的边缘，低头望着楼下，神情麻木。"

VISION_REPLY = "看到啦，是一只猫"
TEXT_REPLY = "测试回复"


class ScreeningVisionService:
    """可控描述输出的视觉服务替身。

    ``descriptions`` 按 describe 调用序逐个弹出（弹空后回落良
    性描述）；条目为异常实例时 describe 抛该异常（钉描述抛错
    的 unavailable 收敛）。``reply_gate`` 非空时 generate 在其
    放行前停住，供接力用例把来源回合冻在回复生成中途；
    ``describe_gate`` 非空时首个 describe 调用在其放行前停住
    （弹出条目在入门控前完成，调用序即弹出序），供筛查窗口
    接管用例把来源回合冻在描述中途。``describe_gates`` 为
    {调用序: Event} 时逐调用独立门控（describe_gate 即调用
    序 1 的简写），供镜像交错用例分别冻结来源与接管回合的
    describe。generate_started 在回复生成进入时置位、
    describe_started 在首个描述进入时置位、
    describe_started_events[序] 在每次描述进入时置位。
    """

    def __init__(
        self,
        descriptions=(),
        *,
        reply=VISION_REPLY,
        reply_gate=None,
        describe_gate=None,
        describe_gates=None,
    ):
        self._descriptions = list(descriptions)
        self.reply = reply
        self.reply_gate = reply_gate
        self.describe_gate = describe_gate
        self.describe_gates = dict(describe_gates) if describe_gates else {}
        if describe_gate is not None:
            self.describe_gates.setdefault(1, describe_gate)
        self.generate_started = asyncio.Event()
        self.describe_started_events = {
            call_no: asyncio.Event() for call_no in range(1, 5)
        }
        self.describe_started = self.describe_started_events[1]
        self._describe_seen = 0
        self.prepare_calls = []
        self.describe_calls = []
        self.generate_calls = []

    async def prepare(self, refs):
        refs = tuple(refs)
        self.prepare_calls.append(refs)
        return PreparedImages(
            data_urls=tuple(DATA_URL for _ in refs),
            received=len(refs),
            skipped=0,
        )

    async def describe(self, data_urls):
        self.describe_calls.append(tuple(data_urls))
        self._describe_seen += 1
        item = self._descriptions.pop(0) if self._descriptions else BENIGN_DESCRIPTION
        started = self.describe_started_events.get(self._describe_seen)
        if started is not None:
            started.set()
        gate = self.describe_gates.get(self._describe_seen)
        if gate is not None:
            await gate.wait()
        if isinstance(item, BaseException):
            raise item
        return item

    async def generate(self, prompt, data_urls):
        self.generate_calls.append((prompt, tuple(data_urls)))
        self.generate_started.set()
        if self.reply_gate is not None:
            await self.reply_gate.wait()
        return self.reply


def _ready_processor(app, service):
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.vision_service = service
    return processor


def _image_event(text=None):
    segments = []
    if text:
        segments.append({"type": "text", "data": {"text": text}})
    segments.append(
        {
            "type": "image",
            "data": {
                "url": "https://images.example.test/cat0.png",
                "file": "cat0.png",
            },
        }
    )
    event = private_event("10001", text or "")
    event["message"] = segments
    event["raw_message"] = text or ""
    return event


def _messages(calls):
    return [call["message"] for call in calls]


def _vision_records(processor):
    path = processor.log_path
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _steps(processor, step):
    return [
        record
        for record in _vision_records(processor)
        if record.get("event_type") == "vision"
        and record.get("vision_step") == step
    ]


def _crisis_expected(settings):
    return crisis_reply(settings.safety["helplines"], False, settings.safety)


async def _wait_until(predicate, timeout=5.0):
    """轮询等待跨任务可观测状态（如危机话术已发到 fake 通道）。"""

    async def _poll():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout=timeout)


# ---------------------------------------------------------------------------
# 纯图路径：危机描述 → 危机协议（固定话术 + 告警记账 + 不生成回复）
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_pure_image_crisis_content_triggers_crisis_protocol(setup_vision_app):
    app, calls, _, settings = setup_vision_app
    service = ScreeningVisionService([CRISIS_DESCRIPTION])
    processor = _ready_processor(app, service)

    assert asyncio.run(processor.handle(_image_event()))
    # 发出的恰是配置固定危机话术（逐字），视觉回复从未生成。
    assert _messages(calls) == [_crisis_expected(settings)]
    assert service.generate_calls == []
    assert len(service.describe_calls) == 1
    # 危机协议记账：crisis_history 落账（告警去重锚点同文字路）。
    assert processor.crisis_history["10001"]["count"] == 1
    # 危机指标：MetricsObserver 只认 SafetyJudge 阶段，Respond
    # 命中靠 _respond_with_vision 自行 record_crisis——钉恰一次
    # （删掉手工入账此断言必红，复审 P3）。
    assert processor.metrics.crisis_count == 1
    # 危机处置后不留图：筛查前已登记的接力记录被协议清除。
    assert processor._vision_handoff == {}
    # 可观测：content_screen=crisis 一条、outcome=content_screened_crisis 一条。
    screens = _steps(processor, "content_screen")
    assert [(r["result"], r["via_handoff"]) for r in screens] == [("crisis", False)]
    outcomes = _steps(processor, "outcome")
    assert [(r["outcome"], r["via_handoff"]) for r in outcomes] == [
        ("content_screened_crisis", False)
    ]
    # events.jsonl 只记元数据：描述原文与其中关键词绝不落盘。
    raw = processor.log_path.read_text(encoding="utf-8")
    assert "手写的纸条" not in raw
    assert "我想自杀" not in raw


@pytest.mark.timeout(10)
def test_pure_image_crisis_leaves_no_handoff_for_later_text(setup_vision_app):
    app, calls, _, settings = setup_vision_app
    service = ScreeningVisionService([CRISIS_DESCRIPTION])
    processor = _ready_processor(app, service)

    assert asyncio.run(processor.handle(_image_event()))
    assert _messages(calls) == [_crisis_expected(settings)]
    # 其后普通文本不得接管任何图片（记录已被危机协议清除）：
    # 走普通文本路，视觉侧零新增调用。
    assert asyncio.run(processor.handle(private_event("10001", "刚才拍了什么")))
    assert _messages(calls) == [_crisis_expected(settings), TEXT_REPLY]
    assert len(service.describe_calls) == 1
    assert service.generate_calls == []


# ---------------------------------------------------------------------------
# 图文路径：随图文字无危机、图片内容有危机 → 同样走危机协议
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_mixed_image_crisis_content_triggers_crisis_protocol(setup_vision_app):
    app, calls, _, settings = setup_vision_app
    service = ScreeningVisionService([CRISIS_DESCRIPTION])
    processor = _ready_processor(app, service)

    assert asyncio.run(processor.handle(_image_event(text="看看这张图")))
    assert _messages(calls) == [_crisis_expected(settings)]
    assert service.generate_calls == []
    assert processor.crisis_history["10001"]["count"] == 1
    screens = _steps(processor, "content_screen")
    assert [r["result"] for r in screens] == ["crisis"]
    outcomes = _steps(processor, "outcome")
    assert [r["outcome"] for r in outcomes] == ["content_screened_crisis"]


# ---------------------------------------------------------------------------
# 模型 judge 路径：无关键词的描述靠 judge 命中；judge 不可用按 D1
# ---------------------------------------------------------------------------


class _JudgeStub:
    def __init__(self, *, result=None, error=None):
        self.result = result
        self.error = error

    async def judge(self, text):
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.timeout(10)
def test_crisis_content_detected_by_model_judge_without_keywords(setup_vision_app):
    app, calls, _, settings = setup_vision_app
    service = ScreeningVisionService([JUDGE_ONLY_DESCRIPTION])
    processor = _ready_processor(app, service)
    processor.safety.judge = _JudgeStub(result=True)

    assert asyncio.run(processor.handle(_image_event()))
    assert _messages(calls) == [_crisis_expected(settings)]
    assert service.generate_calls == []
    # 分类来自模型 judge（detect 的 "model" 口径），与文字路一致。
    assert processor.crisis_history["10001"]["category"] == "model"


@pytest.mark.timeout(10)
def test_judge_unavailable_during_screening_is_suspected_crisis(setup_vision_app):
    app, calls, _, settings = setup_vision_app
    service = ScreeningVisionService([JUDGE_ONLY_DESCRIPTION])
    processor = _ready_processor(app, service)
    processor.safety.judge = _JudgeStub(error=RuntimeError("stub judge down"))

    assert asyncio.run(processor.handle(_image_event()))
    # D1 保守降级：judge 抛错按疑似危机处置，绝不放行未判内容。
    assert _messages(calls) == [_crisis_expected(settings)]
    assert service.generate_calls == []
    assert processor.crisis_history["10001"]["category"] == "疑似危机"


# ---------------------------------------------------------------------------
# 接力路径：接管回合对接管图片独立筛查，命中即由接管回合走危机协议
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_handoff_takeover_screens_taken_over_images(setup_vision_app):
    app, calls, _, settings = setup_vision_app
    gate = asyncio.Event()
    service = ScreeningVisionService(
        [BENIGN_DESCRIPTION, CRISIS_DESCRIPTION], reply_gate=gate
    )
    processor = _ready_processor(app, service)

    async def run():
        origin = asyncio.create_task(processor.handle(_image_event()))
        # 来源回合已备图、登记接力、筛查 clear，冻在回复生成中。
        await asyncio.wait_for(service.generate_started.wait(), timeout=5.0)
        follow = asyncio.create_task(
            processor.handle(private_event("10001", "我好看吗"))
        )
        await asyncio.wait_for(follow, timeout=5.0)
        gate.set()
        await asyncio.wait_for(origin, timeout=5.0)

    asyncio.run(run())

    # 接管回合筛查命中：只发出危机固定话术；来源回合的视觉回复
    # 被接管收尾（handed_off）不投递，接管回合从未调回复生成。
    assert _messages(calls) == [_crisis_expected(settings)]
    assert len(service.generate_calls) == 1  # 只有来源回合那一次（未投递）
    assert len(service.describe_calls) == 2  # 两回合各筛查一次
    screens = _steps(processor, "content_screen")
    assert [(r["result"], r["via_handoff"]) for r in screens] == [
        ("clear", False),
        ("crisis", True),
    ]
    outcomes = {
        (r["outcome"], r["via_handoff"]) for r in _steps(processor, "outcome")
    }
    assert ("content_screened_crisis", True) in outcomes
    assert ("handed_off", False) in outcomes
    assert processor.crisis_history["10001"]["count"] == 1
    # 两回合只有接管回合执行协议并入账：指标恰一次（复审 P3）。
    assert processor.metrics.crisis_count == 1
    # 接力路径同样只记元数据（复审第 2 轮 P4）：危机描述原文
    # 与其中关键词绝不落 events.jsonl。
    raw = processor.log_path.read_text(encoding="utf-8")
    assert "手写的纸条" not in raw
    assert "我想自杀" not in raw


# ---------------------------------------------------------------------------
# 筛查窗口接力（复审 P2/P3）：来源回合冻在 describe 中途时被接管
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_screening_window_takeover_does_not_discard_origin_crisis(setup_vision_app):
    """复审 P2 回归：来源已判危机不得因接管被丢弃。

    来源回合冻在 describe（尚未返回）时补发文本接管图片；接管
    回合的二次 describe 返回 None（不可得）先行回落文本路。放
    行后来源描述返回危机文本——已判结论必须由来源回合执行危
    机协议：危机话术恰一条、crisis_history 与指标恰一次。
    """
    app, calls, _, settings = setup_vision_app
    gate = asyncio.Event()
    service = ScreeningVisionService(
        [CRISIS_DESCRIPTION, None], describe_gate=gate
    )
    processor = _ready_processor(app, service)

    async def run():
        origin = asyncio.create_task(processor.handle(_image_event()))
        # 来源回合已备图、登记接力，冻在首个 describe 内。
        await asyncio.wait_for(service.describe_started.wait(), timeout=5.0)
        follow = asyncio.create_task(
            processor.handle(private_event("10001", "我好看吗"))
        )
        # 接管回合先行走完：二次 describe 不可得 → 回落文本路。
        await asyncio.wait_for(follow, timeout=5.0)
        gate.set()
        await asyncio.wait_for(origin, timeout=5.0)

    asyncio.run(run())

    assert _messages(calls) == [TEXT_REPLY, _crisis_expected(settings)]
    assert _messages(calls).count(_crisis_expected(settings)) == 1
    assert service.generate_calls == []  # 两回合都没走到回复生成
    assert len(service.describe_calls) == 2
    assert processor.crisis_history["10001"]["count"] == 1
    assert processor.metrics.crisis_count == 1
    outcomes = {
        (r["outcome"], r["via_handoff"]) for r in _steps(processor, "outcome")
    }
    assert ("content_screened_crisis", False) in outcomes
    assert ("fallback_text", True) in outcomes


@pytest.mark.timeout(10)
def test_screening_window_both_crisis_runs_protocol_exactly_once(setup_vision_app):
    """筛查窗口内两回合都判危机：处置权抢占保证协议恰执行一次。

    来源冻在 describe 时接管回合先判完（二次描述亦危机）并执
    行协议；来源放行后同样判危机，但处置权已被抢占——让位静
    默收尾（handed_off），不双发危机话术、不重复计数。
    """
    app, calls, _, settings = setup_vision_app
    gate = asyncio.Event()
    service = ScreeningVisionService(
        [CRISIS_DESCRIPTION, CRISIS_DESCRIPTION], describe_gate=gate
    )
    processor = _ready_processor(app, service)

    async def run():
        origin = asyncio.create_task(processor.handle(_image_event()))
        await asyncio.wait_for(service.describe_started.wait(), timeout=5.0)
        follow = asyncio.create_task(
            processor.handle(private_event("10001", "我好看吗"))
        )
        await asyncio.wait_for(follow, timeout=5.0)
        gate.set()
        await asyncio.wait_for(origin, timeout=5.0)

    asyncio.run(run())

    assert _messages(calls) == [_crisis_expected(settings)]
    assert service.generate_calls == []
    assert processor.crisis_history["10001"]["count"] == 1
    assert processor.metrics.crisis_count == 1
    outcomes = {
        (r["outcome"], r["via_handoff"]) for r in _steps(processor, "outcome")
    }
    assert ("content_screened_crisis", True) in outcomes
    assert ("handed_off", False) in outcomes


@pytest.mark.timeout(10)
def test_screening_window_takeover_benign_origin_hands_off(setup_vision_app):
    """筛查窗口接管的非危机pin（复审 P3）：两回合描述皆良性时，
    接管回合正常生成投递，来源回合筛查 clear 后以 handed_off
    静默收尾——筛查窗口分支本身被走通且无危机记账。"""
    app, calls, _, _ = setup_vision_app
    gate = asyncio.Event()
    service = ScreeningVisionService(
        [BENIGN_DESCRIPTION, BENIGN_DESCRIPTION], describe_gate=gate
    )
    processor = _ready_processor(app, service)

    async def run():
        origin = asyncio.create_task(processor.handle(_image_event()))
        await asyncio.wait_for(service.describe_started.wait(), timeout=5.0)
        follow = asyncio.create_task(
            processor.handle(private_event("10001", "我好看吗"))
        )
        await asyncio.wait_for(follow, timeout=5.0)
        gate.set()
        await asyncio.wait_for(origin, timeout=5.0)

    asyncio.run(run())

    assert _messages(calls) == [VISION_REPLY]  # 只有接管回合的回复
    assert len(service.generate_calls) == 1
    assert len(service.describe_calls) == 2
    screens = _steps(processor, "content_screen")
    assert [(r["result"], r["via_handoff"]) for r in screens] == [
        ("clear", True),
        ("clear", False),
    ]
    outcomes = {
        (r["outcome"], r["via_handoff"]) for r in _steps(processor, "outcome")
    }
    assert ("delivered", True) in outcomes
    assert ("handed_off", False) in outcomes
    assert processor.crisis_history == {}
    assert processor.metrics.crisis_count == 0


# ---------------------------------------------------------------------------
# 筛查窗口接力·镜像交错（复审第 2 轮 P3）：接管回合冻在二次
# describe、来源回合先判完——与上节「来源冻结、接管先判完」镜像
# ---------------------------------------------------------------------------


def _mirror_service(takeover_item, gate_origin, gate_takeover):
    """镜像交错共用替身：来源 describe 返回危机描述，接管
    describe 返回 ``takeover_item``；两次 describe 各自门控。"""
    return ScreeningVisionService(
        [CRISIS_DESCRIPTION, takeover_item],
        describe_gates={1: gate_origin, 2: gate_takeover},
    )


async def _run_mirror_window(processor, service, calls, gate_origin, gate_takeover):
    """驱动镜像交错：来源冻在首个 describe → 接管接管图片并冻在
    二次 describe → 放行来源（判危机、抢占、执行协议）→ 来源
    危机话术发出后放行接管 → 两回合各自收尾。"""
    origin = asyncio.create_task(processor.handle(_image_event()))
    await asyncio.wait_for(service.describe_started_events[1].wait(), timeout=5.0)
    follow = asyncio.create_task(processor.handle(private_event("10001", "我好看吗")))
    # 接管回合已接管图片并冻在第二次 describe 内（记录已 consumed）。
    await asyncio.wait_for(service.describe_started_events[2].wait(), timeout=5.0)
    gate_origin.set()
    # 来源回合的危机话术真正发出，才放行接管的二次筛查。
    await _wait_until(lambda: len(calls) >= 1)
    gate_takeover.set()
    await asyncio.wait_for(follow, timeout=5.0)
    await asyncio.wait_for(origin, timeout=5.0)


@pytest.mark.timeout(10)
def test_screening_window_takeover_clear_yields_to_origin_crisis(setup_vision_app):
    """接管二次筛查 clear 不得推翻来源已判危机（ingress 的
    taken_record.crisis_disposed 让位分支）：接管回合不生成、
    不回落，静默收尾——危机话术恰一条、计数恰一次。"""
    app, calls, _, settings = setup_vision_app
    gate_origin = asyncio.Event()
    gate_takeover = asyncio.Event()
    service = _mirror_service(BENIGN_DESCRIPTION, gate_origin, gate_takeover)
    processor = _ready_processor(app, service)

    asyncio.run(
        _run_mirror_window(processor, service, calls, gate_origin, gate_takeover)
    )

    assert _messages(calls) == [_crisis_expected(settings)]
    assert service.generate_calls == []  # 两回合都没走到回复生成
    assert len(service.describe_calls) == 2
    assert processor.crisis_history["10001"]["count"] == 1
    assert processor.metrics.crisis_count == 1
    screens = _steps(processor, "content_screen")
    assert [(r["result"], r["via_handoff"]) for r in screens] == [
        ("crisis", False),
        ("clear", True),
    ]
    outcomes = {
        (r["outcome"], r["via_handoff"]) for r in _steps(processor, "outcome")
    }
    assert ("content_screened_crisis", False) in outcomes
    assert ("content_screened_crisis", True) in outcomes


@pytest.mark.timeout(10)
def test_screening_window_takeover_unavailable_yields_to_origin_crisis(
    setup_vision_app,
):
    """镜像交错·unavailable 变体：接管二次描述不可得时同样让位，
    且让位分支优先于 unavailable 回落分支——接管回合不得回落
    文本路补发普通回复（让位优先于回落的微妙优先级）。"""
    app, calls, _, settings = setup_vision_app
    gate_origin = asyncio.Event()
    gate_takeover = asyncio.Event()
    service = _mirror_service(None, gate_origin, gate_takeover)
    processor = _ready_processor(app, service)

    asyncio.run(
        _run_mirror_window(processor, service, calls, gate_origin, gate_takeover)
    )

    assert _messages(calls) == [_crisis_expected(settings)]
    assert TEXT_REPLY not in _messages(calls)  # 未回落文本路
    assert service.generate_calls == []
    assert len(service.describe_calls) == 2
    assert processor.crisis_history["10001"]["count"] == 1
    assert processor.metrics.crisis_count == 1
    screens = _steps(processor, "content_screen")
    assert [(r["result"], r["via_handoff"]) for r in screens] == [
        ("crisis", False),
        ("unavailable", True),
    ]
    outcomes = {
        (r["outcome"], r["via_handoff"]) for r in _steps(processor, "outcome")
    }
    assert ("content_screened_crisis", True) in outcomes
    assert ("fallback_text", True) not in outcomes
    assert ("fixed_reply", True) not in outcomes


@pytest.mark.timeout(10)
def test_screening_window_takeover_crisis_loses_claim_to_origin(setup_vision_app):
    """接管回合也判危机但抢占落败（ingress 抢占失败的
    own_handoff 为 None 臂）：让位记 content_screened_crisis，
    不重复执行危机协议、不重复计数、不清接力记录冒充 handed_off。"""
    app, calls, _, settings = setup_vision_app
    gate_origin = asyncio.Event()
    gate_takeover = asyncio.Event()
    service = _mirror_service(CRISIS_DESCRIPTION, gate_origin, gate_takeover)
    processor = _ready_processor(app, service)

    asyncio.run(
        _run_mirror_window(processor, service, calls, gate_origin, gate_takeover)
    )

    assert _messages(calls) == [_crisis_expected(settings)]
    assert _messages(calls).count(_crisis_expected(settings)) == 1
    assert service.generate_calls == []
    assert len(service.describe_calls) == 2
    assert processor.crisis_history["10001"]["count"] == 1
    # 抢占落败不得二次入账：指标恰一次（让位臂误加 record_crisis 此处必红）。
    assert processor.metrics.crisis_count == 1
    screens = _steps(processor, "content_screen")
    assert [(r["result"], r["via_handoff"]) for r in screens] == [
        ("crisis", False),
        ("crisis", True),
    ]
    outcomes = {
        (r["outcome"], r["via_handoff"]) for r in _steps(processor, "outcome")
    }
    assert ("content_screened_crisis", False) in outcomes
    assert ("content_screened_crisis", True) in outcomes
    # 让位臂与来源让位（handed_off）口径不同：接管回合不得记 handed_off。
    assert ("handed_off", True) not in outcomes


# ---------------------------------------------------------------------------
# 描述不可得：无从筛查 → 按视觉失败三态收敛
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_describe_unavailable_pure_image_falls_back_to_fixed_reply(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service = ScreeningVisionService([None])
    processor = _ready_processor(app, service)

    assert asyncio.run(processor.handle(_image_event()))
    assert _messages(calls) == [MEDIA_FIXED_REPLY]
    assert service.generate_calls == []
    screens = _steps(processor, "content_screen")
    assert [r["result"] for r in screens] == ["unavailable"]
    outcomes = _steps(processor, "outcome")
    assert [r["outcome"] for r in outcomes] == ["fixed_reply"]


@pytest.mark.timeout(10)
def test_describe_unavailable_mixed_falls_back_to_text_route(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service = ScreeningVisionService([None])
    processor = _ready_processor(app, service)

    assert asyncio.run(processor.handle(_image_event(text="这是什么动物")))
    # 图文回落本回合文本路（F3 口径）：普通回复、不用媒体文案顶替。
    assert _messages(calls) == [TEXT_REPLY]
    assert service.generate_calls == []
    screens = _steps(processor, "content_screen")
    assert [r["result"] for r in screens] == ["unavailable"]
    outcomes = _steps(processor, "outcome")
    assert [r["outcome"] for r in outcomes] == ["fallback_text"]


@pytest.mark.timeout(10)
def test_describe_raises_pure_image_falls_back_to_fixed_reply(
    setup_vision_app, caplog
):
    """describe 抛错与返回 None 同口径收敛（复审 P3）：异常被
    _screen_vision_content 吞成 unavailable，绝不穿透 Respond。"""
    app, calls, _, _ = setup_vision_app
    service = ScreeningVisionService([RuntimeError("vision backend boom")])
    processor = _ready_processor(app, service)

    with caplog.at_level("ERROR", logger="companion.events"):
        assert asyncio.run(processor.handle(_image_event()))
    assert _messages(calls) == [MEDIA_FIXED_REPLY]
    assert service.generate_calls == []
    screens = _steps(processor, "content_screen")
    assert [r["result"] for r in screens] == ["unavailable"]
    outcomes = _steps(processor, "outcome")
    assert [r["outcome"] for r in outcomes] == ["fixed_reply"]
    # 异常原文不进任何日志（复审第 2 轮 P4）：运行日志只记异常
    # 类型名，events.jsonl 只记结果码——两处都不得出现异常消息。
    assert "vision backend boom" not in caplog.text
    raw = processor.log_path.read_text(encoding="utf-8")
    assert "vision backend boom" not in raw


@pytest.mark.timeout(10)
def test_describe_raises_mixed_falls_back_to_text_route(setup_vision_app, caplog):
    app, calls, _, _ = setup_vision_app
    service = ScreeningVisionService([RuntimeError("vision backend boom")])
    processor = _ready_processor(app, service)

    with caplog.at_level("ERROR", logger="companion.events"):
        assert asyncio.run(processor.handle(_image_event(text="这是什么动物")))
    assert _messages(calls) == [TEXT_REPLY]
    assert service.generate_calls == []
    screens = _steps(processor, "content_screen")
    assert [r["result"] for r in screens] == ["unavailable"]
    outcomes = _steps(processor, "outcome")
    assert [r["outcome"] for r in outcomes] == ["fallback_text"]
    # 异常原文不进任何日志（复审第 2 轮 P4）：同纯图用例口径。
    assert "vision backend boom" not in caplog.text
    raw = processor.log_path.read_text(encoding="utf-8")
    assert "vision backend boom" not in raw


# ---------------------------------------------------------------------------
# clear 放行：良性描述 → 正常视觉回复，筛查事件留痕
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_clear_description_proceeds_to_vision_reply(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service = ScreeningVisionService([BENIGN_DESCRIPTION])
    processor = _ready_processor(app, service)

    assert asyncio.run(processor.handle(_image_event()))
    assert _messages(calls) == [VISION_REPLY]
    assert len(service.describe_calls) == 1
    assert len(service.generate_calls) == 1
    screens = _steps(processor, "content_screen")
    assert [(r["result"], r["via_handoff"]) for r in screens] == [("clear", False)]
    outcomes = _steps(processor, "outcome")
    assert [r["outcome"] for r in outcomes] == ["delivered"]
    assert "10001" not in processor.crisis_history


# ---------------------------------------------------------------------------
# 单元钉：VisionService.describe 以固定描述提示词委托 client
# ---------------------------------------------------------------------------


class _UnitClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def has_credentials(self):
        return True

    async def generate_with_images(self, prompt, data_urls):
        self.calls.append((prompt, tuple(data_urls)))
        return self.reply


def test_describe_delegates_with_description_prompt():
    client = _UnitClient("图片里是一只猫")
    service = VisionService(client=client, fetcher=None)
    result = asyncio.run(service.describe((DATA_URL,)))
    assert result == "图片里是一只猫"
    assert client.calls == [(IMAGE_DESCRIPTION_PROMPT, (DATA_URL,))]


def test_describe_failure_converges_to_none():
    client = _UnitClient(None)
    service = VisionService(client=client, fetcher=None)
    assert asyncio.run(service.describe((DATA_URL,))) is None
