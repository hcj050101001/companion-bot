"""记忆与投递存储（P5 拆分）：Database / ConversationStore /
DeliveryLedger / SqliteClaimStore；N3 增 SqliteRequestStore（request
处置状态机）；长期记忆仍为 deferred 端口。
"""

from companion.memory.conversation import ConversationStore
from companion.memory.database import SCHEMA_VERSION, Database
from companion.memory.ledger import DeliveryLedger

__all__ = [
    "SCHEMA_VERSION",
    "AnniversaryStore",
    "AnniversaryStoreFull",
    "ConversationStore",
    "Database",
    "DeliveryLedger",
    "LongTermMemory",
    "RequestBegin",
    "RequestRecord",
    "SqliteClaimStore",
    "SqliteRequestStore",
]


def __getattr__(name: str):
    # AnniversaryStore 惰性导出：与 SqliteClaimStore 同理，避免包
    # 初始化时多打开一条存储导入链。
    if name in ("AnniversaryStore", "AnniversaryStoreFull"):
        from companion.memory import anniversaries as _anniversaries

        return getattr(_anniversaries, name)
    # SqliteClaimStore / SqliteRequestStore 惰性导出：claim 与 request
    # 纯定义已下沉 companion.domain（无循环依赖），此处按需加载只是
    # 避免包初始化时多打开一条存储导入链；`from companion.memory
    # import SqliteClaimStore` 的行为与急切导入一致。
    if name == "SqliteClaimStore":
        from companion.memory.claims import SqliteClaimStore

        return SqliteClaimStore
    if name == "SqliteRequestStore":
        from companion.memory.requests import SqliteRequestStore

        return SqliteRequestStore
    if name in ("RequestBegin", "RequestRecord"):
        from companion.domain import requests as _requests

        return getattr(_requests, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class LongTermMemory:
    """长期向量、画像和时间线接口（deferred 端口）。

    P5 只定接口、不实现：store 仍 raise NotImplementedError，
    实际的长期记忆实现留待后续阶段接入。
    """

    def store(self, *_args, **_kwargs):
        """存储长期记忆（P5 只定接口，未实现）。"""
        raise NotImplementedError("long-term memory is planned for W2")
