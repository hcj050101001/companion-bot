"""固定 Stage 链的实现（方案 §5.3）：每个 Stage 一段既有处理语义。

重构原则是搬运而非改写：各 Stage 的实际处理仍在 EventProcessor 的
_stage_* 方法里（与原 _run_event 的代码段一一对应、顺序与日志/返回
语义逐项保留），Stage 类只负责命名、排序与中断语义。改序即契约变更。
"""

from companion.pipeline.runner import Stage


class TransportGateStage(Stage):
    """Stage 0：验签 + 资格 + 预筛分道 + 容量预留 + 原子 claim。

    本 Stage 在适配器/入口层（create_app 的 webhook 接受边界）先行
    执行完毕，处理器内只留命名占位，保证链的顺序口径完整；其行为
    由接入层契约测试与饱和测试直接验证。
    """

    name = "TransportGate"

    async def process(self, ctx) -> bool:
        return True


class NormalizeStage(Stage):
    """Stage 1：适配器规范化结果在此定型（Ignore 已在 dispatch 前置处理）。"""

    name = "Normalize"

    async def process(self, ctx) -> bool:
        ctx.scratch["normalized_user_id"] = ctx.parsed.user_id
        return True


class AdmissionStage(Stage):
    """Stage 2：主体分级一次判定 + onboarding + 忘掉/媒体前置分支。

    忘掉等 control 命令在本 Stage 前置分支处理（在 judge 与限流之前），
    保持现行先后；关怀控制命令同属本分支（P6 接入）。
    """

    name = "Admission"

    async def process(self, ctx) -> bool:
        return await ctx.proc._stage_admission(ctx)


class SafetyJudgeStage(Stage):
    """Stage 3：关键词 + judge 双路；危机 → critical 通道直发固定话术并终止。"""

    name = "SafetyJudge"

    async def process(self, ctx) -> bool:
        return await ctx.proc._stage_safety_judge(ctx)


class InboundRateLimitStage(Stage):
    """Stage 4：入站 6/min/user；超限静默丢弃（保持现行语义）。"""

    name = "InboundRateLimit"

    async def process(self, ctx) -> bool:
        return await ctx.proc._stage_inbound_rate_limit(ctx)


class CommandStage(Stage):
    """Stage 5：风格切换等精确匹配命令。"""

    name = "Command"

    async def process(self, ctx) -> bool:
        return await ctx.proc._stage_command(ctx)


class MemoryLoadStage(Stage):
    """Stage 6：读短期历史与本回合标识（工具轮次不写记忆的规则在此维护）。"""

    name = "MemoryLoad"

    async def process(self, ctx) -> bool:
        return await ctx.proc._stage_memory_load(ctx)


class PersonaStage(Stage):
    """Stage 7：解析人设快照（默认/风格包 + 安全包装）。"""

    name = "Persona"

    async def process(self, ctx) -> bool:
        return await ctx.proc._stage_persona(ctx)


class RespondStage(Stage):
    """Stage 8：生成回复计划（工具循环/确定性直达/多气泡生成）。"""

    name = "Respond"

    async def process(self, ctx) -> bool:
        return await ctx.proc._stage_respond(ctx)


class OutputGuardStage(Stage):
    """Stage 9：出站复核（身份披露/自伤鼓励/免责），不过回退固定单条。

    按路由分型：多气泡 guard 只管模型生成的聊天回复；工具直出与
    搜索合成走各自 guard（在 Respond 的计划函数内完成，置 guard_done）。
    """

    name = "OutputGuard"

    async def process(self, ctx) -> bool:
        return await ctx.proc._stage_output_guard(ctx)


class DeliverStage(Stage):
    """Stage 10：额度原子预留 → 节奏发送 → 投递台账记账。"""

    name = "Deliver"

    async def process(self, ctx) -> bool:
        return await ctx.proc._stage_deliver(ctx)


STAGE_CLASSES = {
    "TransportGate": TransportGateStage,
    "Normalize": NormalizeStage,
    "Admission": AdmissionStage,
    "SafetyJudge": SafetyJudgeStage,
    "InboundRateLimit": InboundRateLimitStage,
    "Command": CommandStage,
    "MemoryLoad": MemoryLoadStage,
    "Persona": PersonaStage,
    "Respond": RespondStage,
    "OutputGuard": OutputGuardStage,
    "Deliver": DeliverStage,
}


def build_runner(order, observers=()):
    """按给定顺序组装运行器；顺序来自 STAGE_ORDER（可被测试换序验证）。"""
    from companion.pipeline.runner import PipelineRunner

    return PipelineRunner([STAGE_CLASSES[name]() for name in order], observers)
