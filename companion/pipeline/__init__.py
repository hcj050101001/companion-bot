"""消息流水线：命名 Stage 链、EventContext 与运行器（方案 §5.3）。"""

from companion.pipeline.context import EventContext
from companion.pipeline.observers import (
    DiagnosticsLogObserver,
    MetricsObserver,
    StageMetrics,
)
from companion.pipeline.runner import STAGE_ORDER, PipelineRunner, Stage
from companion.pipeline.stages import STAGE_CLASSES, build_runner

__all__ = [
    "DiagnosticsLogObserver",
    "EventContext",
    "MetricsObserver",
    "PipelineRunner",
    "STAGE_CLASSES",
    "STAGE_ORDER",
    "Stage",
    "StageMetrics",
    "build_runner",
]
