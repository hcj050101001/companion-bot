"""工具诊断统一观测出口（方案 §5.10）。

本模块只依赖标准库：Stage 的进程内聚合指标、把 Respond 收割到的
工具循环结构化诊断汇入指标的观察者，以及把同一批诊断经白名单投影
后交给日志 sink 的观察者。正文性字段（prompt / 工具参数 / 工具正文 /
用户消息）永不进入指标、日志记录或渲染文本；按用户聚合只以
HMAC-SHA256(进程级随机盐, user_id) 的十六进制前 16 位为键——进程
重启即换盐、键不可跨进程比对，原始 user_id 绝不保存。

白名单只过滤字段名还不够（Codex 补审 P1）：字段值同样来自不受信的
模型/工具链产出，未注册工具名、正文样字符串与嵌套容器都可能经白
名单字段透传进日志与 /metrics。故聚合与日志投影共用
``_normalize_diagnostic`` 做值规范化：状态类字段闭合枚举校验、
tool/model 长度与成员校验、数值字段类型与非负校验、容器值一律
丢弃。规范化规则的定稿见该函数 docstring。
"""

import hashlib
import hmac
import math
import secrets
import threading

# 诊断 dict 的白名单字段（§5.10）：聚合与日志投影共用同一份，其余
# 键一律忽略（两处口径合并为一个常量，防止后续加字段时漂移）。
_DIAGNOSTIC_FIELDS = (
    "step",
    "round",
    "model",
    "tool",
    "result_code",
    "disposition",
    "elapsed_ms",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
)
_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
# 状态类字段的闭合枚举（值集合与 tool_loop 现行产出核实一致）：
# step 只由 tool_loop 写 respond / tool_invoke；result_code 在
# respond 步为 error/malformed/content/tool_calls，在 tool_invoke 步
# 为 tool_result_status 的 ok/unavailable 与超预算的 skipped_budget；
# disposition 为 ToolDisposition.kind 的 feed/direct/terminal。
_STEP_VALUES = frozenset({"respond", "tool_invoke"})
_RESULT_CODE_VALUES = frozenset({
    "ok",
    "unavailable",
    "skipped_budget",
    "error",
    "malformed",
    "content",
    "tool_calls",
})
_DISPOSITION_VALUES = frozenset({"feed", "direct", "terminal"})
_OTHER_RESULT_CODE = "other"
_UNKNOWN_LABEL = "unknown"
_MAX_LABEL_CHARS = 64
# 标签基数上限（Review P3-2 与 Codex 补审 P2）：tool 名来自模型输出、
# 用户键来自开放私聊流量、model 与标签组键同样可被外部输入持续制造
# 新序列；三个聚合字典各设硬上限，超限后新键折叠到 "other" overflow
# 桶（tool/respond 桶保留已归一的 result_code 维度，其取值为有限
# 枚举，故溢出键数亦有界），保证进程内存与 /metrics 响应体有界。
_MAX_TOOL_KEYS = 128
_MAX_RESPOND_KEYS = 128
_MAX_MODEL_KEYS = 128
_MAX_USER_KEYS = 1024
_OVERFLOW_USER_KEY = "overflow"
_OTHER_TOOL = "other"
_OTHER_MODEL = "other"


def _prepare_known_tools(known_tools):
    """known_tools 构造参数的内部形态：None 表示未给集合，否则冻结。"""
    if known_tools is None:
        return None
    return frozenset(known_tools)


def _valid_number(value) -> bool:
    """数值字段口径：int/float（拒 bool）、有限、非负。"""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _normalize_diagnostic(diag, known_tools=None) -> dict:
    """聚合与日志投影共用的诊断值规范化（Codex 补审 P1 定稿）。

    只输出白名单字段，且每个字段都过值校验：

    - step / disposition：闭合枚举内的 str 才保留，未知值丢弃字段；
    - result_code：枚举内保留，字段存在但未知/异型一律归一 "other"；
    - tool：给了 known_tools 集合时不在其中一律记 "unknown"；未给
      集合时仅接受 str 并截断至 64 字符；非 str（含容器/None）丢弃
      字段（聚合侧缺失时回退 "unknown"，见 record_tool_diagnostics）；
    - model：仅接受 str 并截断至 64 字符，其余丢弃字段；
    - round / elapsed_ms / token 类：仅接受 int/float（拒 bool）、
      有限且非负，异型/越界丢弃字段；
    - dict/list/set 等容器值在任何字段上一律不透传（上述逐字段类型
      校验即其实现：没有任何字段接受容器类型）。
    """
    clean: dict = {}
    step = diag.get("step")
    if isinstance(step, str) and step in _STEP_VALUES:
        clean["step"] = step
    if "round" in diag and _valid_number(diag["round"]):
        clean["round"] = diag["round"]
    model = diag.get("model")
    if isinstance(model, str):
        clean["model"] = model[:_MAX_LABEL_CHARS]
    if "tool" in diag:
        tool = diag["tool"]
        if isinstance(tool, str):
            if known_tools is not None:
                clean["tool"] = tool if tool in known_tools else _UNKNOWN_LABEL
            else:
                clean["tool"] = tool[:_MAX_LABEL_CHARS]
    if "result_code" in diag:
        result_code = diag["result_code"]
        if isinstance(result_code, str) and result_code in _RESULT_CODE_VALUES:
            clean["result_code"] = result_code
        else:
            clean["result_code"] = _OTHER_RESULT_CODE
    disposition = diag.get("disposition")
    if isinstance(disposition, str) and disposition in _DISPOSITION_VALUES:
        clean["disposition"] = disposition
    for key in ("elapsed_ms",) + _TOKEN_FIELDS:
        if key in diag and _valid_number(diag[key]):
            clean[key] = diag[key]
    return clean


def _escape_label(value) -> str:
    """Prometheus label 值转义：只用文本格式合法的 \\、\"、\\n 三种。

    CR 不是合法转义序列（\\r 会让解析器报错），一律折叠为空格——
    含 CR 的工具名若原样带转义渲染，会把整个 /metrics 响应打成
    不可解析（Review P2-1）。
    """
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", " ")
    )


def _format_number(value) -> str:
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(round(value, 3))
    return str(value)


def _token_value(diag, key):
    # 与 _normalize_diagnostic 同一数值口径（int/float、拒 bool、
    # 有限、非负）；diag 在调用前已经过规范化，此处只作纵深复核。
    value = diag.get(key)
    if _valid_number(value):
        return value
    return None


def _empty_tokens() -> dict:
    return {key: 0 for key in _TOKEN_FIELDS}


class StageMetrics:
    """进程内聚合指标（方案 §5.10）：Stage 耗时、工具步、token 与危机。"""

    def __init__(self, known_tools=None):
        # known_tools（可选 Iterable[str]）：已注册工具名集合，透传给
        # _normalize_diagnostic；未给（None）时 tool 只做 str+截断校验。
        self._known_tools = _prepare_known_tools(known_tools)
        self._lock = threading.Lock()
        # 进程级随机盐：用户哈希键只在本进程内有意义、不可离线枚举
        # 比对（Review P3-4）；指标本身就是进程内聚合、重启清零，
        # 换盐不损失任何既有语义。
        self._user_salt = secrets.token_bytes(16)
        self.stage_counts: dict[str, int] = {}
        self.stage_total_ms: dict[str, float] = {}
        self.stage_max_ms: dict[str, float] = {}
        self.tool_counts: dict[tuple[str, str], int] = {}
        self.respond_counts: dict[tuple[str, str], int] = {}
        self.tool_invoke_total: int = 0
        self.tool_invoke_ok: int = 0
        self.tokens_by_model: dict[str, dict[str, int]] = {}
        self.tokens_by_user_hash: dict[str, dict[str, int]] = {}
        self.crisis_count: int = 0
        # N3 事件面计数（方案 §2.3/§4 C14）：(kind, disposition) → 次数，
        # kind ∈ {group, notice, request, meta}、disposition 为事件子链
        # 的固定处置词表；键总数设硬上限防基数膨胀（见 record_event）。
        self.event_counts: dict[tuple[str, str], int] = {}

    def user_hash(self, user_id) -> str:
        """按用户聚合的唯一键：HMAC-SHA256(进程盐, user_id) 前 16 位。"""
        return hmac.new(
            self._user_salt, str(user_id).encode("utf-8"), hashlib.sha256
        ).hexdigest()[:16]

    def record_stage(self, name, elapsed_ms) -> None:
        stage = str(name)
        try:
            elapsed = float(elapsed_ms)
        except (TypeError, ValueError):
            elapsed = 0.0
        with self._lock:
            self.stage_counts[stage] = self.stage_counts.get(stage, 0) + 1
            self.stage_total_ms[stage] = self.stage_total_ms.get(stage, 0.0) + elapsed
            if elapsed > self.stage_max_ms.get(stage, 0.0):
                self.stage_max_ms[stage] = elapsed

    def record_tool_diagnostics(self, user_id, diagnostics, known_tools=None) -> None:
        """汇入一批工具循环诊断：只读经规范化的白名单字段与值。

        known_tools 显式给出时覆盖本对象构造时的集合；为 None 时用
        构造时的集合（构造时也未给则只做 tool 的 str+截断校验）。
        """
        if not diagnostics:
            return
        known = (
            self._known_tools
            if known_tools is None
            else _prepare_known_tools(known_tools)
        )
        hashed = self.user_hash(user_id) if user_id is not None else None
        with self._lock:
            for diag in diagnostics:
                if not isinstance(diag, dict):
                    continue
                clean = _normalize_diagnostic(diag, known)
                step = clean.get("step")
                # 字段缺失（原诊断没给）保持空串旧口径；字段存在但
                # 未知已在规范化时归一为 "other"。
                result_code = clean.get("result_code", "")
                if step == "tool_invoke":
                    tool = clean.get("tool") or _UNKNOWN_LABEL
                    key = (tool, result_code)
                    if key not in self.tool_counts and (
                        len(self.tool_counts) >= _MAX_TOOL_KEYS
                    ):
                        # 基数上限：新标签组折叠到 other，已见键继续累计。
                        key = (_OTHER_TOOL, result_code)
                    self.tool_counts[key] = self.tool_counts.get(key, 0) + 1
                    self.tool_invoke_total += 1
                    if result_code == "ok":
                        self.tool_invoke_ok += 1
                elif step == "respond":
                    model_key = clean.get("model") or _UNKNOWN_LABEL
                    key = (model_key, result_code)
                    if key not in self.respond_counts and (
                        len(self.respond_counts) >= _MAX_RESPOND_KEYS
                    ):
                        # 基数上限：新标签组折叠到 other 桶计数。
                        key = (_OTHER_MODEL, result_code)
                    self.respond_counts[key] = self.respond_counts.get(key, 0) + 1
                tokens = {key: _token_value(clean, key) for key in _TOKEN_FIELDS}
                if any(value is not None for value in tokens.values()):
                    model_key = clean.get("model") or _UNKNOWN_LABEL
                    if model_key not in self.tokens_by_model and (
                        len(self.tokens_by_model) >= _MAX_MODEL_KEYS
                    ):
                        # 基数上限：新 model 折叠到 other 桶计数。
                        model_key = _OTHER_MODEL
                    by_model = self.tokens_by_model.setdefault(model_key, _empty_tokens())
                    for key, value in tokens.items():
                        if value is not None:
                            by_model[key] += value
                    if hashed is not None:
                        user_key = hashed
                        if user_key not in self.tokens_by_user_hash and (
                            len(self.tokens_by_user_hash) >= _MAX_USER_KEYS
                        ):
                            user_key = _OVERFLOW_USER_KEY
                        by_user = self.tokens_by_user_hash.setdefault(
                            user_key, _empty_tokens()
                        )
                        for key, value in tokens.items():
                            if value is not None:
                                by_user[key] += value

    def record_crisis(self) -> None:
        with self._lock:
            self.crisis_count += 1

    def record_event(self, kind, disposition) -> None:
        """记一笔入站事件处置计数（N3 事件面，线程安全）。

        新键总数上限 64：超限后新键折叠到 kind="other" 桶（保留
        disposition 维度），已见键继续累计，保证 /metrics 标签基数
        与进程内存有界（与 tool/respond 桶同一口径）。
        """
        key = (str(kind), str(disposition))
        with self._lock:
            if key not in self.event_counts and len(self.event_counts) >= 64:
                key = ("other", key[1])
            self.event_counts[key] = self.event_counts.get(key, 0) + 1

    @property
    def tool_success_ratio(self) -> float:
        with self._lock:
            if not self.tool_invoke_total:
                return 0.0
            return self.tool_invoke_ok / self.tool_invoke_total

    def render_text(self, quota_snapshot=None) -> str:
        """渲染 Prometheus 文本格式；quota_snapshot 为 None 时不输出额度行。"""
        with self._lock:
            stage_counts = dict(self.stage_counts)
            stage_total = dict(self.stage_total_ms)
            stage_max = dict(self.stage_max_ms)
            tool_counts = dict(self.tool_counts)
            respond_counts = dict(self.respond_counts)
            invoke_total = self.tool_invoke_total
            invoke_ok = self.tool_invoke_ok
            tokens_by_model = {
                key: dict(value) for key, value in self.tokens_by_model.items()
            }
            tokens_by_user = {
                key: dict(value) for key, value in self.tokens_by_user_hash.items()
            }
            crisis = self.crisis_count
            event_counts = dict(self.event_counts)
        lines: list[str] = []

        def family(name, help_text, type_name):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {type_name}")

        family(
            "companion_stage_calls_total", "Pipeline stage executions.", "counter"
        )
        for stage in sorted(stage_counts):
            lines.append(
                f'companion_stage_calls_total{{stage="{_escape_label(stage)}"}} '
                f"{stage_counts[stage]}"
            )
        family(
            "companion_stage_duration_ms_total",
            "Total pipeline stage elapsed milliseconds.",
            "counter",
        )
        for stage in sorted(stage_total):
            lines.append(
                f'companion_stage_duration_ms_total{{stage="{_escape_label(stage)}"}} '
                f"{_format_number(stage_total[stage])}"
            )
        family(
            "companion_stage_duration_ms_max",
            "Maximum pipeline stage elapsed milliseconds.",
            "gauge",
        )
        for stage in sorted(stage_max):
            lines.append(
                f'companion_stage_duration_ms_max{{stage="{_escape_label(stage)}"}} '
                f"{_format_number(stage_max[stage])}"
            )
        family("companion_tool_steps_total", "Tool invoke steps by tool and result.", "counter")
        for tool, result_code in sorted(tool_counts):
            lines.append(
                f'companion_tool_steps_total{{tool="{_escape_label(tool)}",'
                f'result_code="{_escape_label(result_code)}"}} '
                f"{tool_counts[(tool, result_code)]}"
            )
        family(
            "companion_respond_steps_total",
            "Tool-loop respond steps by model and result.",
            "counter",
        )
        for model, result_code in sorted(respond_counts):
            lines.append(
                f'companion_respond_steps_total{{model="{_escape_label(model)}",'
                f'result_code="{_escape_label(result_code)}"}} '
                f"{respond_counts[(model, result_code)]}"
            )
        family(
            "companion_tool_invocations_total", "Total tool invoke steps.", "counter"
        )
        lines.append(f"companion_tool_invocations_total {invoke_total}")
        family(
            "companion_tool_invocations_ok_total",
            "Tool invoke steps with result_code ok.",
            "counter",
        )
        lines.append(f"companion_tool_invocations_ok_total {invoke_ok}")
        family(
            "companion_tool_success_ratio",
            "Fraction of tool invoke steps with result_code ok.",
            "gauge",
        )
        ratio = invoke_ok / invoke_total if invoke_total else 0.0
        lines.append(f"companion_tool_success_ratio {_format_number(ratio)}")
        # 口径限定（Review P3-5）：token 只统计工具循环 respond 步的
        # usage，普通生成与 judge 的模型调用不在此口径内，指标名与
        # HELP 都必须明示 tool_loop，防被当作全量成本看板。
        family(
            "companion_tool_loop_tokens_by_model_total",
            "Tool-loop respond-step tokens by model and kind (tool loop only).",
            "counter",
        )
        for model in sorted(tokens_by_model):
            for kind in _TOKEN_FIELDS:
                lines.append(
                    f'companion_tool_loop_tokens_by_model_total{{model="{_escape_label(model)}",'
                    f'kind="{kind}"}} {tokens_by_model[model][kind]}'
                )
        family(
            "companion_tool_loop_tokens_by_user_total",
            "Tool-loop respond-step tokens by salted user hash and kind "
            "(tool loop only).",
            "counter",
        )
        for hashed in sorted(tokens_by_user):
            for kind in _TOKEN_FIELDS:
                lines.append(
                    f'companion_tool_loop_tokens_by_user_total{{user_hash="{_escape_label(hashed)}",'
                    f'kind="{kind}"}} {tokens_by_user[hashed][kind]}'
                )
        family("companion_crisis_total", "Crisis hits.", "counter")
        lines.append(f"companion_crisis_total {crisis}")
        family(
            "companion_events_total",
            "Inbound non-private events by kind and disposition.",
            "counter",
        )
        for kind, disposition in sorted(event_counts):
            lines.append(
                f'companion_events_total{{kind="{_escape_label(kind)}",'
                f'disposition="{_escape_label(disposition)}"}} '
                f"{event_counts[(kind, disposition)]}"
            )
        if quota_snapshot is not None:
            family(
                "companion_send_quota_normal_remaining",
                "Remaining account normal sends in the rolling window.",
                "gauge",
            )
            if "normal_remaining" in quota_snapshot:
                lines.append(
                    "companion_send_quota_normal_remaining "
                    f"{_format_number(quota_snapshot['normal_remaining'])}"
                )
            family(
                "companion_send_quota_critical_remaining",
                "Remaining account critical sends in the rolling window.",
                "gauge",
            )
            if "critical_remaining" in quota_snapshot:
                lines.append(
                    "companion_send_quota_critical_remaining "
                    f"{_format_number(quota_snapshot['critical_remaining'])}"
                )
        return "\n".join(lines) + "\n"


class MetricsObserver:
    """runner 观察者：Stage 耗时入聚合，Respond 汇入工具诊断，危机计数。"""

    def __init__(self, metrics: StageMetrics, known_tools=None):
        # known_tools（可选）：显式给出时覆盖 metrics 构造时的集合，
        # 透传给 record_tool_diagnostics 的规范化；接线由组装方协调。
        self.metrics = metrics
        self.known_tools = _prepare_known_tools(known_tools)

    def __call__(self, stage_name, ctx, elapsed_ms, outcome) -> None:
        self.metrics.record_stage(stage_name, elapsed_ms)
        scratch = getattr(ctx, "scratch", None)
        if (
            stage_name == "SafetyJudge"
            and isinstance(scratch, dict)
            and scratch.get("crisis_hit")
        ):
            self.metrics.record_crisis()
        if stage_name == "Respond":
            diagnostics = getattr(ctx, "tool_diagnostics", ()) or ()
            if diagnostics:
                parsed = getattr(ctx, "parsed", None)
                user_id = getattr(parsed, "user_id", None)
                self.metrics.record_tool_diagnostics(
                    user_id, diagnostics, known_tools=self.known_tools
                )


class DiagnosticsLogObserver:
    """runner 观察者：Respond 的工具诊断经规范化投影后逐条交给 sink。"""

    def __init__(self, sink, known_tools=None):
        # known_tools（可选 Iterable[str]）：已注册工具名集合；给出时
        # 不在集合内的 tool 一律记 "unknown"，与聚合出口同一口径。
        self.sink = sink
        self.known_tools = _prepare_known_tools(known_tools)

    def __call__(self, stage_name, ctx, elapsed_ms, outcome) -> None:
        if stage_name != "Respond":
            return
        diagnostics = getattr(ctx, "tool_diagnostics", ()) or ()
        if not diagnostics:
            return
        trace_id = getattr(ctx, "trace_id", None)
        for diag in diagnostics:
            if not isinstance(diag, dict):
                continue
            clean = _normalize_diagnostic(diag, self.known_tools)
            record: dict = {}
            if trace_id is not None:
                record["trace_id"] = trace_id
            record["stage"] = "Respond"
            record["event_type"] = "tool_step"
            for key in _DIAGNOSTIC_FIELDS:
                if key in clean:
                    record[key] = clean[key]
            self.sink(record)
