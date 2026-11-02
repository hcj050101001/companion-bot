"""EventContext：一条入站事件在 Stage 链中流动的全部状态（方案 §5.1）。

Stage 之间只通过本对象传递数据；正文只存于 event/工作字段中，
观察者与日志永不复制正文（保持现行脱敏纪律）。
"""

import uuid
from dataclasses import dataclass, field


@dataclass
class EventContext:
    """流水线上下文：由 dispatch 组装，各 Stage 依次填充。"""

    proc: object  # EventProcessor（避免与 ingress 循环导入，仅作不透明句柄）
    parsed: object  # perception.UserMessage（适配器规范化后的入站消息）
    delivery_generation: int
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    lane: str = "ordinary"  # 接入层预筛分道（只决定走哪条道，不改变 Stage 顺序）
    # 场景（方案 §3.3）：private_chat | group_chat | event_notice |
    # event_request | system，由分发处按事件来源写入
    scene: str = "private_chat"
    stage_trace: list = field(default_factory=list)  # 已执行的 Stage 名，按序
    result: bool = False  # 最终是否发送了回复
    # Admission 写入的主体分级与能力集（方案 §5.3 Stage 2，后续 Stage 只读）
    subject: str = "stranger"  # owner | consented_friend | stranger | system
    capabilities: frozenset = frozenset()
    # 处理中工作字段（由各 Stage 填充，语义与原 _run_event 局部变量一一对应）
    started: float = 0.0
    already_onboarded: bool = False
    selected: object = None
    pack_generation: int = 0
    pack: object = None
    persona: object = None
    history: list = field(default_factory=list)
    turn_id: str = ""
    safety_epoch: int = 0
    tools_used: bool = False
    repeat_token: object = None
    route: str = "chat"  # chat（多气泡）| direct（工具直出/合成后的单条计划）
    bubbles: list | None = None  # chat 路由待审气泡（Respond 产出、OutputGuard 审）
    plan: list | None = None  # 最终发送计划（Deliver 消费）
    reservation: object = None  # SendQuota 预留
    store: bool = True  # 本回合是否写短期记忆
    needs_reserve: bool = False  # chat 路由在 Deliver 阶段做额度原子预留
    guard_done: bool = False  # plan 已在 Respond 内完成 review_bubbles 的路径
    kind: str = "普通回复"
    # Respond Stage 从 provider 客户端收割的工具循环结构化诊断，只含
    # 元数据、绝不含正文（方案 §5.10 统一观测出口的汇入点）。
    tool_diagnostics: tuple = ()
    scratch: dict = field(default_factory=dict)
