"""P8 observability tests (§5.10): tool diagnostics unified export.

Covers the whitelist projection of DiagnosticsLogObserver, StageMetrics
aggregation (tokens by model / by hashed user, tool success counts,
Prometheus rendering), SendQuota.snapshot, and an end-to-end pass through
EventProcessor plus the /metrics endpoint. No raw user_id or message body
may ever appear in metrics, diagnostic records, or rendered text.
"""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
import yaml

from companion.brain import CompanionBrain
from companion.config import load_settings
from companion.ingress import create_app
from companion.pipeline import (
    DiagnosticsLogObserver,
    MetricsObserver,
    PipelineRunner,
    Stage,
    StageMetrics,
)
from companion.quota import SendQuota
from companion.safety import ModelLLMJudge
from tests.fake_onebot import private_event
from tests.test_app import SECRET

USER_ID = "10001"
POISON_TEXT = "DIAG_POISON_TEXT"
POISON_PROMPT = "DIAG_POISON_PROMPT"
POISON_ARGUMENTS = "DIAG_POISON_ARGUMENTS"
POISON_MESSAGE = "USER_MESSAGE_POISON_12345"


def _poisoned_diagnostics():
    return (
        {
            "step": "respond",
            "round": 0,
            "model": "stub-model",
            "tools_enabled": True,
            "result_code": "tool_calls",
            "elapsed_ms": 12,
            "prompt_tokens": 11,
            "completion_tokens": 4,
            "total_tokens": 15,
            "text": POISON_TEXT,
            "prompt": POISON_PROMPT,
            "arguments": POISON_ARGUMENTS,
            "user_id": USER_ID,
        },
        {
            "step": "tool_invoke",
            "tool": "lookup",
            "result_code": "ok",
            "disposition": "feed",
            "elapsed_ms": 3,
            "text": POISON_TEXT,
            "prompt": POISON_PROMPT,
            "arguments": POISON_ARGUMENTS,
            "user_id": USER_ID,
        },
    )


@pytest.mark.timeout(10)
def test_diagnostics_log_observer_whitelist_projection():
    records = []
    observer = DiagnosticsLogObserver(records.append)
    ctx = SimpleNamespace(
        trace_id="trace-abc",
        tool_diagnostics=_poisoned_diagnostics(),
        parsed=SimpleNamespace(user_id=USER_ID),
        scratch={},
    )
    observer("Respond", ctx, 5.0, "continue")
    assert len(records) == 2
    first, second = records
    # Envelope and §5.10 field口径.
    assert first["trace_id"] == "trace-abc"
    assert first["stage"] == "Respond"
    assert first["event_type"] == "tool_step"
    assert first["step"] == "respond"
    assert first["round"] == 0
    assert first["model"] == "stub-model"
    assert first["result_code"] == "tool_calls"
    assert first["elapsed_ms"] == 12
    assert first["prompt_tokens"] == 11
    assert first["completion_tokens"] == 4
    assert first["total_tokens"] == 15
    # Missing fields are omitted, not None placeholders.
    assert "tool" not in first
    assert "disposition" not in first
    assert second["tool"] == "lookup"
    assert second["disposition"] == "feed"
    assert "model" not in second
    assert "prompt_tokens" not in second
    # Poison keys and values never survive the projection.
    for record in records:
        for key in ("text", "prompt", "arguments", "user_id", "tools_enabled"):
            assert key not in record
        blob = json.dumps(record, ensure_ascii=False)
        assert POISON_TEXT not in blob
        assert POISON_PROMPT not in blob
        assert POISON_ARGUMENTS not in blob
        assert USER_ID not in blob


@pytest.mark.timeout(10)
def test_diagnostics_log_observer_only_fires_for_respond_with_diagnostics():
    records = []
    observer = DiagnosticsLogObserver(records.append)
    base = dict(
        trace_id="t",
        parsed=SimpleNamespace(user_id=USER_ID),
        scratch={},
    )
    observer("Deliver", SimpleNamespace(tool_diagnostics=_poisoned_diagnostics(), **base),
             1.0, "continue")
    observer("Respond", SimpleNamespace(tool_diagnostics=(), **base), 1.0, "continue")
    assert records == []


@pytest.mark.timeout(10)
def test_stage_metrics_aggregation_and_render():
    metrics = StageMetrics()
    user_hash = metrics.user_hash(USER_ID)
    metrics.record_stage("Respond", 10.0)
    metrics.record_stage("Respond", 30.0)
    metrics.record_stage("Deliver", 5.0)
    metrics.record_tool_diagnostics(
        USER_ID,
        [
            {
                "step": "respond", "round": 0, "model": "m-test",
                "result_code": "tool_calls", "elapsed_ms": 10,
                "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
                "text": POISON_TEXT, "user_id": USER_ID,
            },
            {
                "step": "tool_invoke", "tool": "lookup", "result_code": "ok",
                "disposition": "feed", "elapsed_ms": 3,
            },
            {
                "step": "tool_invoke", "tool": "lookup",
                "result_code": "unavailable", "disposition": "feed",
                "elapsed_ms": 4, "prompt": POISON_PROMPT,
            },
            {
                "step": "respond", "round": 1, "model": "m-test",
                "result_code": "content", "elapsed_ms": 8,
                "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
            },
        ],
    )
    metrics.record_crisis()

    assert metrics.stage_counts == {"Respond": 2, "Deliver": 1}
    assert metrics.stage_total_ms["Respond"] == 40.0
    assert metrics.stage_max_ms["Respond"] == 30.0
    assert metrics.tokens_by_model == {
        "m-test": {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22}
    }
    assert metrics.tokens_by_user_hash == {
        user_hash: {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22}
    }
    assert metrics.tool_counts == {("lookup", "ok"): 1, ("lookup", "unavailable"): 1}
    assert metrics.tool_invoke_total == 2
    assert metrics.tool_invoke_ok == 1
    assert metrics.tool_success_ratio == 0.5
    assert metrics.respond_counts == {
        ("m-test", "tool_calls"): 1,
        ("m-test", "content"): 1,
    }
    assert metrics.crisis_count == 1
    # The raw user_id must not be stored anywhere in the aggregate state.
    assert USER_ID not in repr(metrics.__dict__)

    text = metrics.render_text(
        quota_snapshot={"normal_remaining": 42, "critical_remaining": 9}
    )
    assert 'companion_stage_calls_total{stage="Respond"} 2' in text
    assert 'companion_stage_duration_ms_total{stage="Respond"} 40' in text
    assert 'companion_stage_duration_ms_max{stage="Respond"} 30' in text
    assert (
        'companion_tool_steps_total{tool="lookup",result_code="ok"} 1' in text
    )
    assert "companion_tool_invocations_total 2" in text
    assert "companion_tool_invocations_ok_total 1" in text
    assert "companion_tool_success_ratio 0.5" in text
    assert (
        'companion_tool_loop_tokens_by_model_total{model="m-test",'
        'kind="total_tokens"} 22' in text
    )
    assert (
        f'companion_tool_loop_tokens_by_user_total{{user_hash="{user_hash}",'
        'kind="total_tokens"} 22' in text
    )
    assert "companion_crisis_total 1" in text
    assert "companion_send_quota_normal_remaining 42" in text
    assert "companion_send_quota_critical_remaining 9" in text
    assert USER_ID not in text
    assert POISON_TEXT not in text and POISON_PROMPT not in text

    without_quota = metrics.render_text()
    assert "companion_send_quota" not in without_quota
    assert "companion_crisis_total 1" in without_quota


@pytest.mark.timeout(10)
def test_stage_metrics_render_escapes_label_values():
    metrics = StageMetrics()
    metrics.record_tool_diagnostics(
        USER_ID,
        [{"step": "respond", "model": 'a"b\\c', "result_code": "content",
          "prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}],
    )
    text = metrics.render_text()
    assert 'model="a\\"b\\\\c"' in text


@pytest.mark.timeout(10)
def test_stage_metrics_render_never_emits_illegal_escapes():
    # Review P2-1: \r is not a legal Prometheus escape; a tool name with
    # CR/LF must not make every later /metrics scrape unparseable.
    metrics = StageMetrics()
    metrics.record_tool_diagnostics(
        USER_ID,
        [{"step": "tool_invoke", "tool": "evil\r\nname", "result_code": "ok",
          "disposition": "feed", "elapsed_ms": 1}],
    )
    text = metrics.render_text()
    assert "\r" not in text
    assert "\\r" not in text
    assert 'tool="evil \\nname"' in text
    # Every backslash in the output starts one of the three legal escapes.
    import re

    assert not re.search(r"\\(?![\\\"n])", text)


@pytest.mark.timeout(10)
def test_metrics_observer_crisis_and_respond_harvest():
    metrics = StageMetrics()
    observer = MetricsObserver(metrics)
    crisis_ctx = SimpleNamespace(
        scratch={"crisis_hit": True},
        tool_diagnostics=(),
        parsed=SimpleNamespace(user_id=USER_ID),
    )
    observer("SafetyJudge", crisis_ctx, 2.0, "interrupt")
    assert metrics.crisis_count == 1
    calm_ctx = SimpleNamespace(
        scratch={},
        tool_diagnostics=(),
        parsed=SimpleNamespace(user_id=USER_ID),
    )
    observer("SafetyJudge", calm_ctx, 2.0, "continue")
    assert metrics.crisis_count == 1
    respond_ctx = SimpleNamespace(
        scratch={},
        tool_diagnostics=_poisoned_diagnostics(),
        parsed=SimpleNamespace(user_id=USER_ID),
    )
    observer("Respond", respond_ctx, 12.0, "continue")
    assert metrics.stage_counts["Respond"] == 1
    assert metrics.tokens_by_model["stub-model"]["total_tokens"] == 15
    assert metrics.tool_invoke_ok == 1


@pytest.mark.timeout(10)
def test_send_quota_snapshot_is_read_only_and_prunes():
    now = [0.0]
    quota = SendQuota(
        clock=lambda: now[0],
        normal_per_user=5,
        account_normal=7,
        account_critical=3,
        window_seconds=60,
    )
    assert quota.snapshot() == {
        "normal_remaining": 7,
        "critical_remaining": 3,
        "per_user_normal_limit": 5,
    }
    assert quota.reserve(USER_ID, 2) is not None
    assert quota.snapshot()["normal_remaining"] == 5
    # Repeated snapshots do not consume anything.
    assert quota.snapshot()["normal_remaining"] == 5
    assert quota.reserve(USER_ID, 1, critical=True) is not None
    assert quota.snapshot()["critical_remaining"] == 2
    assert quota.snapshot()["normal_remaining"] == 5
    # After the window rolls, pruned buckets restore the full remaining.
    now[0] = 61.0
    assert quota.snapshot() == {
        "normal_remaining": 7,
        "critical_remaining": 3,
        "per_user_normal_limit": 5,
    }


@pytest.fixture
def obs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEBOT_SECRET", SECRET)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ALERT_QQ", raising=False)
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"allowed_private_user_ids": ["10001"]}), encoding="utf-8"
    )
    base = load_settings()
    settings = replace(
        base,
        onebot_secret=SECRET,
        dev=False,
        root=tmp_path,
        app={
            **base.app,
            "allowed_private_user_ids": ["10001"],
            "sqlite_path": "memory.db",
            "logs_dir": "logs",
            "response_delay_seconds": {"min": 0, "max": 0},
            "requests_per_minute": 6,
            "llm": {"provider": "deepseek", "display_name": "DeepSeek"},
            "read_only_tools": {
                "enabled": True,
                "policies": {
                    "current_time": {"enabled": True, "rate_limit": 50},
                    "weather_lookup": {"enabled": True, "rate_limit": 50},
                    "web_search": {
                        "enabled": True,
                        "rate_limit": 50,
                        "content_extraction_enabled": True,
                    },
                },
            },
            "connection_health": {"enabled": True},
            "proactive_care": {"enabled": False},
        },
    )
    calls = []

    class FakeSender:
        async def send_private_msg(self, user_id, message, *, priority="normal"):
            calls.append({"user_id": int(user_id), "message": message})
            return {"status": "ok"}

        async def send_alert_private_msg(self, user_id, message):
            return await self.send_private_msg(user_id, message)

    class FakeAlerter:
        def __init__(self):
            self.items = []

        async def alert(self, user_id, reason, summary="sensitive text omitted"):
            self.items.append((user_id, reason, summary))

    class FakeLLM:
        async def generate(self, _prompt):
            return "测试回复"

        async def generate_json(self, _prompt):
            return json.dumps({"bubbles": ["测试回复"]}, ensure_ascii=False)

    class SafeJudgeLLM:
        async def generate(self, _prompt):
            return "NO"

    async def no_sleep(_delay):
        pass

    alerter = FakeAlerter()
    app = create_app(
        settings,
        sender=FakeSender(),
        alerter=alerter,
        sleep=no_sleep,
        random_uniform=lambda _a, _b: 0,
        brain=CompanionBrain(settings.persona, FakeLLM()),
    )
    app.state.processor.safety.judge = ModelLLMJudge(SafeJudgeLLM())
    return SimpleNamespace(
        app=app, calls=calls, alerter=alerter, settings=settings,
        processor=app.state.processor,
    )


class _StubClient:
    def __init__(self, diagnostics):
        self.last_tool_diagnostics = diagnostics


class _StubBrain:
    """Tool-route brain stub whose client carries structured diagnostics."""

    def __init__(self, diagnostics):
        self.client = _StubClient(diagnostics)
        self.calls = 0

    async def respond_with_tools(self, *_args, **_kwargs):
        self.calls += 1
        return "工具合成回复", True


def _install_stub_brain(env):
    env.processor.brain = _StubBrain(_poisoned_diagnostics())


@pytest.mark.timeout(10)
def test_processor_tool_diagnostics_end_to_end(obs_env):
    env = obs_env
    processor = env.processor
    # The tool route requires a non-empty schema set (production profile).
    assert processor.read_only_registry.schemas()
    _install_stub_brain(env)
    asyncio.run(processor.conversation.mark_onboarded(USER_ID))

    assert asyncio.run(
        processor.handle(private_event(USER_ID, f"请帮我查一下 {POISON_MESSAGE}"))
    )
    assert [call["message"] for call in env.calls] == ["工具合成回复"]

    log_text = processor.log_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in log_text.splitlines() if line.strip()]
    tool_steps = [r for r in records if r.get("event_type") == "tool_step"]
    assert len(tool_steps) == 2
    for record in tool_steps:
        assert record["trace_id"]
        assert record["stage"] == "Respond"
        assert record["result_code"] in {"tool_calls", "ok"}
        assert "elapsed_ms" in record
        assert "time" in record
    assert len({r["trace_id"] for r in tool_steps}) == 1
    # Neither diagnostic poison nor the user's message body reaches the log.
    assert POISON_TEXT not in log_text
    assert POISON_PROMPT not in log_text
    assert POISON_ARGUMENTS not in log_text
    assert POISON_MESSAGE not in log_text
    assert USER_ID not in json.dumps(tool_steps, ensure_ascii=False)

    metrics = processor.metrics
    assert metrics.tokens_by_model == {
        "stub-model": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}
    }
    assert metrics.tokens_by_user_hash[metrics.user_hash(USER_ID)]["total_tokens"] == 15
    # E 路修复 6 接线后：ingress 把插件注册表工具名集合作为
    # known_tools 传给 observers；替身诊断里的 "lookup" 不是任何
    # 已注册工具名，按 D 路归一化口径折叠为 "unknown"（已注册名
    # 原样通过的口径见 test_diagnostics_normalization_known_tools_*）。
    assert metrics.tool_counts == {("unknown", "ok"): 1}
    assert metrics.crisis_count == 0
    assert env.app.state.metrics is metrics

    # A keyword-crisis message increments the crisis counter by exactly one.
    assert asyncio.run(processor.handle(private_event(USER_ID, "想自杀")))
    assert metrics.crisis_count == 1


@pytest.mark.timeout(10)
def test_metrics_endpoint(obs_env):
    env = obs_env
    processor = env.processor
    _install_stub_brain(env)
    asyncio.run(processor.conversation.mark_onboarded(USER_ID))
    assert asyncio.run(
        processor.handle(private_event(USER_ID, f"请帮我查一下 {POISON_MESSAGE}"))
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=env.app), base_url="http://test"
        ) as client:
            return await client.get("/metrics")

    response = asyncio.run(run())
    assert response.status_code == 200
    text = response.text
    # E 路修复 6：正常渲染必须以 render_success 1 收尾，抓取方据此
    # 确认本次抓取的渲染是完整的。
    assert text.endswith("companion_metrics_render_success 1\n")
    # Concrete sample lines with values, not just the always-present
    # HELP/TYPE headers (Review P4-5).
    assert 'companion_stage_calls_total{stage="Respond"} 1' in text
    assert (
        'companion_tool_loop_tokens_by_model_total{model="stub-model",'
        'kind="total_tokens"} 15' in text
    )
    assert "companion_crisis_total 0" in text
    assert "companion_send_quota_normal_remaining" in text
    assert "companion_send_quota_critical_remaining" in text
    assert processor.metrics.user_hash(USER_ID) in text
    # No raw user_id, no message body, no diagnostic poison in the output.
    assert USER_ID not in text
    assert POISON_MESSAGE not in text
    assert POISON_TEXT not in text
    assert POISON_PROMPT not in text


@pytest.mark.timeout(10)
def test_non_iterable_diagnostics_do_not_break_reply(obs_env):
    # Review P3-1: a provider whose last_tool_diagnostics is a non-sequence
    # value must not take the reply down with it (the harvest line runs
    # inside the Respond stage, outside the observer safety net).
    env = obs_env
    processor = env.processor
    assert processor.read_only_registry.schemas()
    env.processor.brain = _StubBrain(123)
    asyncio.run(processor.conversation.mark_onboarded(USER_ID))
    assert asyncio.run(
        processor.handle(private_event(USER_ID, "请帮我查一下天气"))
    )
    assert [call["message"] for call in env.calls] == ["工具合成回复"]
    assert processor.metrics.tokens_by_model == {}


@pytest.mark.timeout(10)
def test_emergency_crisis_counts_once(obs_env):
    # Review P3-6: the synchronous emergency path counts crisis hits
    # itself (it bypasses the runner); exactly +1, no double count.
    env = obs_env
    processor = env.processor
    asyncio.run(processor.conversation.mark_onboarded(USER_ID))
    before = processor.metrics.crisis_count
    asyncio.run(processor.send_emergency_crisis(USER_ID))
    assert processor.metrics.crisis_count == before + 1
    assert env.calls  # the fixed crisis reply was actually sent


@pytest.mark.timeout(10)
def test_metrics_registered_when_health_disabled(obs_env):
    # /metrics is registered unconditionally; /health/ready stays gated
    # by connection_health.enabled (Review P3-6).
    env = obs_env
    settings = replace(
        env.settings,
        app={**env.settings.app, "connection_health": {"enabled": False}},
    )

    class _Sender:
        async def send_private_msg(self, user_id, message, *, priority="normal"):
            return {"status": "ok"}

        async def send_alert_private_msg(self, user_id, message):
            return {"status": "ok"}

    class _Alerter:
        async def alert(self, user_id, reason, summary="sensitive text omitted"):
            pass

    async def no_sleep(_delay):
        pass

    app = create_app(
        settings,
        sender=_Sender(),
        alerter=_Alerter(),
        sleep=no_sleep,
        random_uniform=lambda _a, _b: 0,
        brain=env.processor.brain,
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            metrics = await client.get("/metrics")
            ready = await client.get("/health/ready")
            return metrics, ready

    metrics_response, ready_response = asyncio.run(run())
    assert metrics_response.status_code == 200
    assert "companion_crisis_total" in metrics_response.text
    assert metrics_response.text.endswith("companion_metrics_render_success 1\n")
    assert ready_response.status_code == 404


@pytest.mark.timeout(10)
def test_runner_observer_failure_never_blocks_pipeline():
    # The runner's guarantee: a raising observer only loses its own
    # notification; the stage chain still completes (Review P4-5).
    class _Stage(Stage):
        name = "Only"

        async def process(self, ctx) -> bool:
            return True

    def _boom(*_args):
        raise RuntimeError("observer blew up")

    ctx = SimpleNamespace(stage_trace=[], result=True)
    runner = PipelineRunner([_Stage()], observers=[_boom])
    assert asyncio.run(runner.run(ctx)) is True
    assert ctx.stage_trace == ["Only"]


@pytest.mark.timeout(10)
def test_metrics_cardinality_caps_fold(monkeypatch):
    # Review round 2: the label-cardinality caps must actually fold —
    # the (cap+1)-th distinct tool becomes "other", the (cap+1)-th
    # distinct user becomes "overflow", and both maps stay bounded.
    import companion.pipeline.observers as observers

    monkeypatch.setattr(observers, "_MAX_TOOL_KEYS", 3)
    monkeypatch.setattr(observers, "_MAX_USER_KEYS", 2)
    metrics = StageMetrics()
    for index in range(5):
        metrics.record_tool_diagnostics(
            f"user-{index}",
            [
                {"step": "tool_invoke", "tool": f"tool-{index}",
                 "result_code": "ok", "disposition": "feed", "elapsed_ms": 1},
                {"step": "respond", "model": "m", "result_code": "content",
                 "prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
            ],
        )
    tool_names = {tool for tool, _code in metrics.tool_counts}
    assert tool_names == {"tool-0", "tool-1", "tool-2", "other"}
    assert metrics.tool_counts[("other", "ok")] == 2
    assert metrics.tool_invoke_total == 5
    assert len(metrics.tokens_by_user_hash) == 3  # 2 real + overflow
    assert metrics.tokens_by_user_hash["overflow"]["total_tokens"] == 3


@pytest.mark.timeout(10)
def test_metrics_endpoint_degrades_instead_of_500(obs_env, monkeypatch):
    # Review round 2: both degradation branches of the endpoint —
    # quota snapshot failure drops only the quota lines; render
    # failure returns the minimal text. Neither may produce a 500.
    env = obs_env
    processor = env.processor

    def _boom_snapshot():
        raise RuntimeError("quota down")

    monkeypatch.setattr(processor.send_quota, "snapshot", _boom_snapshot)

    async def get_metrics():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=env.app), base_url="http://test"
        ) as client:
            return await client.get("/metrics")

    response = asyncio.run(get_metrics())
    assert response.status_code == 200
    assert "companion_crisis_total" in response.text
    assert "companion_send_quota" not in response.text

    def _boom_render(quota_snapshot=None):
        raise RuntimeError("render down")

    monkeypatch.setattr(processor.metrics, "render_text", _boom_render)
    response = asyncio.run(get_metrics())
    assert response.status_code == 200
    # E 路修复 6：兜底文本须带 render_success 0，抓取方才能区分
    # 「渲染失败」与正常空指标；仍保持 200 与最小文本口径。
    assert response.text == "# metrics unavailable\ncompanion_metrics_render_success 0\n"


@pytest.mark.timeout(10)
def test_diagnostics_normalization_known_tools_marks_unknown():
    # Codex 补审 P1: with a known_tools set, an unregistered (body-like)
    # tool name must be recorded as "unknown" in both exports, never
    # verbatim; the registered name passes through unchanged.
    body_like_tool = "这是一段正文样的工具名不该进日志"
    diagnostics = (
        {"step": "tool_invoke", "tool": body_like_tool, "result_code": "ok",
         "disposition": "feed", "elapsed_ms": 1},
        {"step": "tool_invoke", "tool": "lookup", "result_code": "ok",
         "disposition": "feed", "elapsed_ms": 1},
    )
    metrics = StageMetrics(known_tools={"lookup"})
    metrics.record_tool_diagnostics(USER_ID, diagnostics)
    assert metrics.tool_counts == {("unknown", "ok"): 1, ("lookup", "ok"): 1}
    assert body_like_tool not in metrics.render_text()

    records = []
    observer = DiagnosticsLogObserver(records.append, known_tools={"lookup"})
    ctx = SimpleNamespace(
        trace_id="t",
        tool_diagnostics=diagnostics,
        parsed=SimpleNamespace(user_id=USER_ID),
        scratch={},
    )
    observer("Respond", ctx, 1.0, "continue")
    assert records[0]["tool"] == "unknown"
    assert records[1]["tool"] == "lookup"
    assert body_like_tool not in json.dumps(records, ensure_ascii=False)

    # The MetricsObserver-level parameter reaches normalization as well.
    metrics2 = StageMetrics()
    MetricsObserver(metrics2, known_tools={"lookup"})(
        "Respond", ctx, 1.0, "continue"
    )
    assert metrics2.tool_counts == metrics.tool_counts

    # Without a known set: tool is still forced to str truncated at 64.
    long_tool = "t" * 100
    long_diag = (
        {"step": "tool_invoke", "tool": long_tool, "result_code": "ok",
         "disposition": "feed", "elapsed_ms": 1},
    )
    metrics3 = StageMetrics()
    metrics3.record_tool_diagnostics(USER_ID, long_diag)
    assert metrics3.tool_counts == {("t" * 64, "ok"): 1}
    records3 = []
    DiagnosticsLogObserver(records3.append)(
        "Respond",
        SimpleNamespace(
            trace_id="t", tool_diagnostics=long_diag,
            parsed=SimpleNamespace(user_id=USER_ID), scratch={},
        ),
        1.0, "continue",
    )
    assert records3[0]["tool"] == "t" * 64


@pytest.mark.timeout(10)
def test_diagnostics_normalization_drops_containers_and_bad_values():
    # Codex 补审 P1: nested containers must not pass through the
    # whitelist projection, unknown status values are dropped (step /
    # disposition) or normalized (result_code -> "other"), and
    # mistyped / negative / bool numerics are discarded.
    nested = {"body": POISON_TEXT, "inner": [POISON_PROMPT]}
    diagnostics = (
        {"step": "respond", "round": -1, "model": nested,
         "result_code": "content", "elapsed_ms": [12],
         "prompt_tokens": -5, "completion_tokens": True,
         "total_tokens": {"tokens": 3}},
        {"step": "tool_invoke", "tool": nested, "result_code": "正文样结果码",
         "disposition": "正文样处置", "elapsed_ms": "3"},
        {"step": "正文样步", "model": ["m"], "result_code": "ok"},
    )
    records = []
    DiagnosticsLogObserver(records.append)(
        "Respond",
        SimpleNamespace(
            trace_id="t", tool_diagnostics=diagnostics,
            parsed=SimpleNamespace(user_id=USER_ID), scratch={},
        ),
        1.0, "continue",
    )
    assert len(records) == 3
    first, second, third = records
    for key in (
        "round", "model", "elapsed_ms",
        "prompt_tokens", "completion_tokens", "total_tokens",
    ):
        assert key not in first
    assert first["result_code"] == "content"
    assert "tool" not in second
    assert "disposition" not in second
    assert "elapsed_ms" not in second
    assert second["result_code"] == "other"
    assert "step" not in third
    assert "model" not in third
    blob = json.dumps(records, ensure_ascii=False)
    assert POISON_TEXT not in blob
    assert POISON_PROMPT not in blob

    metrics = StageMetrics()
    metrics.record_tool_diagnostics(USER_ID, diagnostics)
    assert metrics.respond_counts == {("unknown", "content"): 1}
    assert metrics.tool_counts == {("unknown", "other"): 1}
    assert metrics.tool_invoke_total == 1
    assert metrics.tool_invoke_ok == 0
    # Every token value above was mistyped/negative/bool/container.
    assert metrics.tokens_by_model == {}
    text = metrics.render_text()
    assert POISON_TEXT not in text
    assert POISON_PROMPT not in text
    assert 'result_code="other"' in text


@pytest.mark.timeout(10)
def test_metrics_cardinality_caps_cover_respond_and_tokens(monkeypatch):
    # Codex 补审 P2: the caps cover all three aggregate dicts, not just
    # tool_counts. Raw result_codes are all unknown and normalize to the
    # single "other" value, so tool/model alone drive cardinality;
    # overflow folds into the "other" bucket of each dict.
    import companion.pipeline.observers as observers

    monkeypatch.setattr(observers, "_MAX_TOOL_KEYS", 3)
    monkeypatch.setattr(observers, "_MAX_RESPOND_KEYS", 3)
    monkeypatch.setattr(observers, "_MAX_MODEL_KEYS", 3)
    metrics = StageMetrics()
    diagnostics = []
    for index in range(8):
        diagnostics.append(
            {"step": "tool_invoke", "tool": f"tool-{index}",
             "result_code": f"raw-code-{index}", "disposition": "feed",
             "elapsed_ms": 1}
        )
        diagnostics.append(
            {"step": "respond", "model": f"model-{index}",
             "result_code": f"raw-code-{index}",
             "prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}
        )
    metrics.record_tool_diagnostics(USER_ID, diagnostics)
    assert len(metrics.tool_counts) <= 3 + 1
    assert metrics.tool_counts[("other", "other")] == 5
    assert len(metrics.respond_counts) <= 3 + 1
    assert metrics.respond_counts[("other", "other")] == 5
    assert len(metrics.tokens_by_model) <= 3 + 1
    assert metrics.tokens_by_model["other"]["total_tokens"] == 5
    assert metrics.tool_invoke_total == 8
