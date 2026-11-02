"""Stage 协议与流水线运行器（方案 §5.3）。

顺序即契约：STAGE_ORDER 是固定链的唯一口径，任何改序都是契约变更、
必须过顺序契约测试（改序即红）。观察者只读、永不阻断：observer 抛错
被吞掉并记日志，绝不影响处理链本身。
"""

import logging
import time

logger = logging.getLogger("companion.pipeline")

# 固定链（顺序即 §3.1 契约）：TransportGate 在适配器/入口层先行，
# 其余 Stage 在处理器内依次执行。
STAGE_ORDER = (
    "TransportGate",
    "Normalize",
    "Admission",
    "SafetyJudge",
    "InboundRateLimit",
    "Command",
    "MemoryLoad",
    "Persona",
    "Respond",
    "OutputGuard",
    "Deliver",
)


class Stage:
    """命名 Stage：process 返回 True 继续下一 Stage，False 中断链。"""

    name = "Stage"

    async def process(self, ctx) -> bool:  # pragma: no cover - 协议方法
        raise NotImplementedError


class PipelineRunner:
    """按给定 Stage 列表顺序执行，逐个通知观察者。"""

    def __init__(self, stages, observers=()):
        self.stages = list(stages)
        self.observers = list(observers)

    async def run(self, ctx) -> bool:
        for stage in self.stages:
            started = time.perf_counter()
            try:
                proceed = await stage.process(ctx)
            except Exception:
                for observer in self.observers:
                    self._notify(observer, stage, ctx, started, "error")
                raise
            ctx.stage_trace.append(stage.name)
            for observer in self.observers:
                self._notify(
                    observer, stage, ctx, started, "continue" if proceed else "interrupt"
                )
            if not proceed:
                break
        return ctx.result

    @staticmethod
    def _notify(observer, stage, ctx, started, outcome):
        try:
            observer(stage.name, ctx, (time.perf_counter() - started) * 1000, outcome)
        except Exception:  # noqa: BLE001 — 观察者永不阻断处理链
            logger.debug("pipeline observer failed", exc_info=True)
