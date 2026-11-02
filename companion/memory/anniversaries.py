"""对话式纪念日存储（倒计时体验增强）：主人经私聊命令维护的纪念日。

与 config 静态清单分工：config 条目是部署期配置（变更须重启），
本表是主人运行时经对话增删的条目，落既有共享库（与 messages /
claims 同库不同表，生产即 data/memory.db），绝不写回配置文件。
合并口径（companion.tools.date_countdown.merge_anniversaries）：
规范化同名时以本表条目为准——主人对话里的显式维护覆盖部署期
静态配置；删掉本表条目后，同名 config 条目重新可见。

全部方法 async，内部用 asyncio.to_thread 调同步事务核心（与
ConversationStore 同模式）。
"""

import asyncio
import logging
import unicodedata

from companion.config import AnniversaryEntry
from companion.memory.database import Database

logger = logging.getLogger(__name__)

#: 存储上限与 config 清单的 50 条上限同口径。
MAX_STORED_ANNIVERSARIES = 50


def _normalize_name(value: str) -> str:
    """名称规范化：NFKC + casefold + 空白折叠。

    与 companion.config._normalize_anniversary_name 及
    companion.tools.date_countdown._normalize_name 同口径（各模块
    内置一份、靠测试锁定一致——config 是叶子模块、memory 不得
    反向依赖 tools，既定分工）。
    """
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


class AnniversaryStoreFull(Exception):
    """存储已达上限且本次是新名（更新既有条目不受上限约束）。"""


class AnniversaryStore:
    def __init__(self, db: Database):
        self.db = db

    # -- 同步核心 ---------------------------------------------------------

    @staticmethod
    def _row_to_entry(row) -> AnniversaryEntry | None:
        name, month, day, year, date_value = row
        try:
            return AnniversaryEntry(
                name=name, month=month, day=day, year=year, date=date_value
            )
        except Exception:  # noqa: BLE001 — 坏行跳过（只记固定文案），不砖化整表
            logger.error("anniversary row skipped: stored entry failed validation")
            return None

    def _list_entries_sync(self) -> tuple[AnniversaryEntry, ...]:
        with self.db.transaction() as db:
            rows = db.execute(
                "SELECT name, month, day, year, date FROM anniversaries "
                "ORDER BY created_at, rowid"
            ).fetchall()
        entries = []
        for row in rows:
            entry = self._row_to_entry(row)
            if entry is not None:
                entries.append(entry)
        return tuple(entries)

    def _get_sync(self, normalized_name: str) -> AnniversaryEntry | None:
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT name, month, day, year, date FROM anniversaries "
                "WHERE normalized_name=?",
                (normalized_name,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_entry(row)

    def _upsert_sync(self, entry: AnniversaryEntry) -> bool:
        """写入一条（规范化同名覆盖）；True = 新建，False = 更新。"""
        key = _normalize_name(entry.name)
        with self.db.transaction() as db:
            exists = db.execute(
                "SELECT 1 FROM anniversaries WHERE normalized_name=?", (key,)
            ).fetchone()
            if exists is None:
                count = db.execute("SELECT COUNT(*) FROM anniversaries").fetchone()[0]
                if count >= MAX_STORED_ANNIVERSARIES:
                    raise AnniversaryStoreFull("anniversary store is full")
            db.execute(
                """INSERT INTO anniversaries(normalized_name, name, month, day, year, date)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(normalized_name) DO UPDATE SET
                name=excluded.name, month=excluded.month, day=excluded.day,
                year=excluded.year, date=excluded.date,
                updated_at=CURRENT_TIMESTAMP""",
                (key, entry.name, entry.month, entry.day, entry.year, entry.date),
            )
        return exists is None

    def _remove_sync(self, normalized_name: str) -> bool:
        with self.db.transaction() as db:
            cursor = db.execute(
                "DELETE FROM anniversaries WHERE normalized_name=?",
                (normalized_name,),
            )
            removed = cursor.rowcount > 0
        return removed

    # -- 异步公开 API（name 入参均为未规范化的显示名，内部归一） --------

    async def list_entries(self) -> tuple[AnniversaryEntry, ...]:
        return await asyncio.to_thread(self._list_entries_sync)

    async def get(self, name: str) -> AnniversaryEntry | None:
        return await asyncio.to_thread(self._get_sync, _normalize_name(name))

    async def upsert(self, entry: AnniversaryEntry) -> bool:
        return await asyncio.to_thread(self._upsert_sync, entry)

    async def remove(self, name: str) -> bool:
        return await asyncio.to_thread(self._remove_sync, _normalize_name(name))
