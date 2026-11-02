"""request 处置的纯领域定义（方案 §4 C6）：键、状态、决策与结果码。

本模块只有常量、dataclass 与纯函数 ``request_key``，无 IO、无存储。
request 事件（好友申请 / 入群申请等）的处置是副作用动作：平台调用
一旦发出便不可撤回，故其去重与进度不能只靠进程内指纹，须落库为与
claim 同级的持久化处置状态机（存储在 ``companion.memory.requests``
的 ``SqliteRequestStore``），两者共用本模块定义。

状态机：pending → processing → done / ambiguous / failed（后三者为
终态）。ambiguous 表示已调用平台但结果不明：不释放、不重做，只留
待人工 / 显式决策，故与 done / failed 同列终态。
"""

from dataclasses import dataclass

REQUEST_STATUS_PENDING = "pending"
REQUEST_STATUS_PROCESSING = "processing"
REQUEST_STATUS_DONE = "done"
REQUEST_STATUS_AMBIGUOUS = "ambiguous"
REQUEST_STATUS_FAILED = "failed"

REQUEST_TERMINAL_STATES = frozenset(
    {REQUEST_STATUS_DONE, REQUEST_STATUS_AMBIGUOUS, REQUEST_STATUS_FAILED}
)

# 处置决策：谁 / 以何种口径拍板。
DECISION_MANUAL = "manual"
DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"

# 结果码：与状态正交，记处置最终为何如此收口。
RESULT_POLICY_MANUAL = "policy_manual"
RESULT_PLATFORM_REJECTED = "platform_rejected"
RESULT_PLATFORM_EXPIRED = "platform_expired"
RESULT_HANDLER_ERROR = "handler_error"

__all__ = [
    "DECISION_APPROVED",
    "DECISION_MANUAL",
    "DECISION_REJECTED",
    "REQUEST_STATUS_AMBIGUOUS",
    "REQUEST_STATUS_DONE",
    "REQUEST_STATUS_FAILED",
    "REQUEST_STATUS_PENDING",
    "REQUEST_STATUS_PROCESSING",
    "REQUEST_TERMINAL_STATES",
    "RESULT_HANDLER_ERROR",
    "RESULT_PLATFORM_EXPIRED",
    "RESULT_PLATFORM_REJECTED",
    "RESULT_POLICY_MANUAL",
    "RequestBegin",
    "RequestRecord",
    "request_key",
]


def request_key(self_id, request_type, sub_type, flag) -> tuple[str, str, str, str]:
    """归一处置唯一键 ``(self_id, request_type, sub_type, flag)``。

    ``sub_type`` 为 None 时记空串（部分 request 无子类型）；其余分量
    一律 ``str()`` 化。``flag`` 是平台侧的申请凭证，缺了它无从处置、
    也无从防重，故为空（含 None）直接抛 ValueError，不许占位键。
    """
    normalized_flag = "" if flag is None else str(flag)
    if not normalized_flag:
        raise ValueError("flag must not be empty")
    return (
        str(self_id),
        str(request_type),
        "" if sub_type is None else str(sub_type),
        normalized_flag,
    )


@dataclass(frozen=True)
class RequestRecord:
    """一条 request 处置记录（落库行的只读投影）。"""

    key: tuple[str, str, str, str]
    user_id: str
    group_id: str | None
    status: str
    decision: str | None
    result_code: str | None
    generation: int
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class RequestBegin:
    """一次 try_begin 的结果：是新建，还是撞上既有记录及其现状态。"""

    created: bool
    status: str
