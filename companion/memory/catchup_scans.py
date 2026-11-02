"""断线重连补回扫描标记存储：记录已开始/已完成的补回扫描窗口。

持久化要点（设计 §7.1）：

- ``catchup_scans`` 以 ``(window_start, window_end)`` 为主键，
  标记在扫描**开始**时落盘（``try_begin`` 占位），进程中途崩溃
  后该窗口行已存在，重启后不会重扫同一窗口（宁漏勿重）；
- ``latest_window_end`` 单调推进，任何两次扫描窗口不重叠；
- ``known_private_user_ids`` / ``message_turn_exists`` 是对既有
  ``messages`` 表的只读查询，供发现降级与已处理核查使用，不写
  任何既有表。

迁移 / 回滚说明：本表不进 Database 的 v1 schema（SCHEMA_VERSION
不变），由本 store 在构造事务内 ``CREATE TABLE IF NOT EXISTS``
自建（SqliteRequestStore 先例），对既有库纯 additive，与其他
store 同库共存、互不干扰。回滚 = ``DROP TABLE catchup_scans``，
不影响其他任何表。

本模块只依赖 ``companion.memory.database`` 与标准库（导入边界）。
"""

import time
from collections.abc import Callable

from companion.memory.database import Database

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS catchup_scans (
    window_start REAL NOT NULL,
    window_end   REAL NOT NULL,
    state        TEXT NOT NULL,
    outcome      TEXT,
    created_at   REAL NOT NULL,
    finished_at  REAL,
    PRIMARY KEY (window_start, window_end)
)
"""

__all__ = [
    "SqliteCatchupScanStore",
]


class SqliteCatchupScanStore:
    """落库版补回扫描标记；与既有 store 同库共存。"""

    def __init__(
        self,
        db: Database,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.db = db
        self.clock = clock
        with self.db.transaction() as conn:
            conn.execute(_CREATE_TABLE)

    def try_begin(self, window_start: float, window_end: float) -> bool:
        """占位一个扫描窗口；已存在则返回 False（本轮不得重扫）。"""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO catchup_scans"
                " (window_start, window_end, state, outcome, created_at, finished_at)"
                " VALUES (?, ?, 'started', NULL, ?, NULL)",
                (window_start, window_end, self.clock()),
            )
            return cursor.rowcount == 1

    def complete(self, window_start: float, window_end: float, outcome: str) -> bool:
        """把已开始的窗口标记为完成；未开始/已完成一律 False。"""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE catchup_scans SET state='completed', outcome=?, finished_at=? "
                "WHERE window_start=? AND window_end=? AND state='started'",
                (outcome, self.clock(), window_start, window_end),
            )
            return cursor.rowcount > 0

    def latest_window_end(self) -> float:
        """返回已记录窗口的最大终点；无行时返回 0.0。"""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT MAX(window_end) FROM catchup_scans").fetchone()
        if row is None or row[0] is None:
            return 0.0
        return float(row[0])

    def known_private_user_ids(self, limit: int) -> list[str]:
        """本地 messages 表有往来的私聊会话，按最近一行倒序。"""
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT user_id FROM messages GROUP BY user_id "
                "ORDER BY MAX(id) DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [row[0] for row in rows]

    def message_turn_exists(self, user_id: str, turn_id: str) -> bool:
        """messages 表中是否已有该 (user_id, turn_id) 的回合行。"""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM messages WHERE user_id=? AND turn_id=? LIMIT 1",
                (user_id, turn_id),
            ).fetchone()
        return row is not None
