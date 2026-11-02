"""AnniversaryStore 契约测试（倒计时体验增强：对话式纪念日存储）。

进程内合成数据：临时 SQLite 库、假条目，不访问网络。覆盖 upsert /
list / get / remove、规范化同名去重、50 条上限（新名受限、更新不
受限）、坏行跳过、跨重开持久化，以及三方名称规范化口径一致
（config / tools / memory 各内置一份、靠测试锁定）。
"""

import asyncio

import pytest

from companion.config import AnniversaryEntry
from companion.config import _normalize_anniversary_name as config_normalize
from companion.memory import Database
from companion.memory.anniversaries import (
    AnniversaryStore,
    AnniversaryStoreFull,
)
from companion.memory.anniversaries import (
    _normalize_name as store_normalize,
)
from companion.tools.date_countdown import (
    normalize_anniversary_name as tool_normalize,
)


def _store(tmp_path, name="memory.db"):
    return AnniversaryStore(Database(tmp_path / name))


def test_upsert_list_get_remove_roundtrip(tmp_path):
    store = _store(tmp_path)

    async def run():
        assert await store.list_entries() == ()
        assert await store.upsert(
            AnniversaryEntry(name="结婚纪念日", month=5, day=20)
        ) is True
        assert await store.upsert(AnniversaryEntry(name="春节", date="2027-02-17")) is True
        entries = await store.list_entries()
        assert [entry.name for entry in entries] == ["结婚纪念日", "春节"]
        got = await store.get("结婚纪念日")
        assert got is not None and got.month == 5 and got.day == 20
        assert await store.get("不存在") is None
        assert await store.remove("结婚纪念日") is True
        assert await store.remove("结婚纪念日") is False
        assert [entry.name for entry in await store.list_entries()] == ["春节"]

    asyncio.run(run())


def test_upsert_normalized_duplicate_updates_in_place(tmp_path):
    store = _store(tmp_path)

    async def run():
        assert await store.upsert(
            AnniversaryEntry(name="Key Day", month=5, day=20)
        ) is True
        # 全角/大小写变体经规范化命中同一主键 → 更新而非新增。
        assert await store.upsert(
            AnniversaryEntry(name="ＫＥＹ ＤＡＹ", month=6, day=15)
        ) is False
        entries = await store.list_entries()
        assert len(entries) == 1
        assert entries[0].month == 6 and entries[0].day == 15

    asyncio.run(run())


def test_store_cap_blocks_new_names_but_not_updates(tmp_path):
    store = _store(tmp_path)

    async def run():
        for index in range(50):
            assert await store.upsert(
                AnniversaryEntry(name=f"纪念{index}", month=1, day=1)
            ) is True
        with pytest.raises(AnniversaryStoreFull):
            await store.upsert(AnniversaryEntry(name="第51条", month=1, day=1))
        # 更新既有条目不受上限约束。
        assert await store.upsert(
            AnniversaryEntry(name="纪念0", month=2, day=2)
        ) is False
        assert len(await store.list_entries()) == 50

    asyncio.run(run())


def test_bad_row_skipped_without_bricking_table(tmp_path):
    db = Database(tmp_path / "memory.db")
    store = AnniversaryStore(db)

    async def run():
        await store.upsert(AnniversaryEntry(name="好条目", month=5, day=20))
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO anniversaries(normalized_name, name, month, day)"
                " VALUES(?,?,?,?)",
                ("坏行", "坏行", 13, 40),
            )
        entries = await store.list_entries()
        assert [entry.name for entry in entries] == ["好条目"]
        assert await store.get("坏行") is None

    asyncio.run(run())


def test_entries_survive_database_reopen(tmp_path):
    async def write():
        store = _store(tmp_path)
        await store.upsert(AnniversaryEntry(name="结婚纪念日", month=5, day=20))

    asyncio.run(write())
    reopened = _store(tmp_path)
    entries = asyncio.run(reopened.list_entries())
    assert len(entries) == 1
    assert entries[0].name == "结婚纪念日"
    assert (entries[0].month, entries[0].day) == (5, 20)


@pytest.mark.parametrize(
    "raw",
    [
        "结婚纪念日",
        "  结婚   纪念日 ",
        "ＫＥＹ ＤＡＹ",
        "Key Day",
        "a\tb\nc",
        "Straße Fest",
    ],
)
def test_name_normalizers_agree_across_modules(raw):
    expected = config_normalize(raw)
    assert tool_normalize(raw) == expected
    assert store_normalize(raw) == expected
