"""claim 的纯领域定义（方案 §5.3 Stage 0）：分道、分区、状态与记录。

本模块只有常量、枚举、dataclass 与纯函数 ``classify_lane``，无 IO、
无存储。进程内存储在 ``companion.ingress.claims.ClaimStore``，落库
存储在 ``companion.memory.claims.SqliteClaimStore``，两者共用本模块
的定义，避免任一存储层反向依赖另一层（P5 import 边界裁决）。
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

LANE_ORDINARY = "ordinary"
LANE_CONTROL = "control"
LANE_CRITICAL = "critical"
LANE_GROUP = "group"

PARTITION_ORDINARY = "ordinary"
PARTITION_RESERVED = "reserved"
PARTITION_GROUP = "group"

STATE_PROCESSING = "processing"
STATE_DONE = "done"
STATE_ABANDONED = "abandoned"

TERMINAL_STATES = frozenset({STATE_DONE, STATE_ABANDONED})

# 关怀控制命令短语（P6 接入）：此处是单一事实来源——预筛分道用
# CONTROL_PHRASES 做子串分道，ingress 的 Admission 精确匹配命令集
# 由下面三个常量构建，两边不得各写一份（P6 Review P4）。
CARE_OPT_IN_PHRASE = "开启关怀提醒"
CARE_PAUSE_PHRASE = "暂停关怀提醒"
CARE_STOP_PHRASES = ("停止关怀提醒", "关闭关怀提醒")
CARE_CONTROL_PHRASES = (CARE_OPT_IN_PHRASE, CARE_PAUSE_PHRASE, *CARE_STOP_PHRASES)

CONTROL_PHRASES = (
    "忘掉",
    "删除记忆",
    "忘掉我",
    "清除记忆",
    # 关怀控制命令同属 control 道（方案 §5.3 预筛分道），保证撤回/
    # 暂停类命令在普通道打满时仍经 control 道优先出队。
    *CARE_CONTROL_PHRASES,
)

_LANE_PARTITION = {
    LANE_ORDINARY: PARTITION_ORDINARY,
    LANE_CONTROL: PARTITION_RESERVED,
    LANE_CRITICAL: PARTITION_RESERVED,
    LANE_GROUP: PARTITION_GROUP,
}

__all__ = [
    "CARE_CONTROL_PHRASES",
    "CARE_OPT_IN_PHRASE",
    "CARE_PAUSE_PHRASE",
    "CARE_STOP_PHRASES",
    "CONTROL_PHRASES",
    "LANE_CONTROL",
    "LANE_CRITICAL",
    "LANE_GROUP",
    "LANE_ORDINARY",
    "PARTITION_GROUP",
    "PARTITION_ORDINARY",
    "PARTITION_RESERVED",
    "STATE_ABANDONED",
    "STATE_DONE",
    "STATE_PROCESSING",
    "TERMINAL_STATES",
    "ClaimOutcome",
    "ClaimRecord",
    "classify_lane",
]


class ClaimOutcome(Enum):
    """一次 try_claim 的结果。"""

    CLAIMED = "claimed"
    DUPLICATE = "duplicate"
    FULL = "full"
    EXEMPT = "exempt"


@dataclass
class ClaimRecord:
    """一条 claim 记录；finish 会原地更新 state 与 finished_at。"""

    key: tuple[str, str, str]
    lane: str = LANE_ORDINARY
    partition: str = PARTITION_ORDINARY
    state: str = STATE_PROCESSING
    generation: int = 1
    created_at: float = 0.0
    finished_at: float | None = None


def classify_lane(text: str, *, keyword_match: Callable[[str], object]) -> str:
    """纯函数分道（无 IO）：关键词危机命中 → critical，控制短语 → control，其余 ordinary。

    ``keyword_match`` 由 SafetyGateway.keyword_match 提供，本函数只调用：
    返回非 None 表示命中。keyword_match 抛异常时按未命中处理——预筛只是
    分道，正式判定在 SafetyJudge Stage。
    """
    try:
        hit = keyword_match(text)
    except Exception:  # noqa: BLE001 — 预筛失败降级为未命中，不阻断分道
        hit = None
    if hit is not None:
        return LANE_CRITICAL
    if any(phrase in text for phrase in CONTROL_PHRASES):
        return LANE_CONTROL
    return LANE_ORDINARY
