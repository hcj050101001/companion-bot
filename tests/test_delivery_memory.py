"""Synthetic delivery-state and crash-recovery tests (P5 new API).

Behaviour assertions are equivalent to the pre-P5 ShortTermMemory
versions, migrated to Database + ConversationStore / DeliveryLedger.
"""

import asyncio
import sqlite3

import pytest

from companion.memory import ConversationStore, Database, DeliveryLedger


def make_stores(path):
    """Small helper: one Database, the (conversation, ledger) pair on it."""
    db = Database(path)
    return db, ConversationStore(db), DeliveryLedger(db)


def test_legacy_migration_and_ambiguous_recovery(tmp_path):
    path = tmp_path / "memory.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE messages (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL,
                   role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT)""")
        db.execute("INSERT INTO messages VALUES (1, 'u', 'assistant', 'legacy', NULL)")

    async def scenario():
        db, conversation, ledger = make_stores(path)
        assert await conversation.get_history("u") == [("assistant", "legacy")]
        generation = await ledger.advance_delivery_generation("u")
        assert await ledger.prepare_delivery("u", "turn", "question", 2, 0, generation)
        assert await ledger.update_delivery("u", "turn", "in_flight", 1, 0, generation)
        assert await ledger.update_delivery(
            "u", "turn", "partial", 1, 0, generation, "accepted"
        )
        assert await ledger.update_delivery("u", "turn", "in_flight", 0, 0, generation)
        db.close()

        restarted_db, restarted, _ = make_stores(path)
        assert ("assistant", "accepted") not in await restarted.get_history("u")
        restarted_db.close()
        with sqlite3.connect(path) as raw:
            turns = raw.execute(
                "SELECT turn_id, state, accepted_text FROM delivery_turns ORDER BY turn_id"
            ).fetchall()
            history_rows = raw.execute(
                "SELECT role, content FROM messages ORDER BY id"
            ).fetchall()
        # 在途回合重启后转 ambiguous，accepted_text 留在台账、历史投影被清。
        assert turns == [("turn", "ambiguous", "accepted")]
        assert ("assistant", "accepted") not in history_rows
        assert ("assistant", "legacy") in history_rows
        assert ("user", "question") in history_rows

    asyncio.run(scenario())


def test_generation_and_forget_guard_every_delivery_write(tmp_path):
    async def scenario():
        db, conversation, ledger = make_stores(tmp_path / "memory.db")
        generation = await ledger.advance_delivery_generation("u")
        assert await ledger.prepare_delivery("u", "turn", "question", 3, 0, generation)
        assert not await ledger.prepare_delivery("u", "turn", "question", 3, 0, generation)
        assert await ledger.update_delivery("u", "turn", "in_flight", 2, 0, generation)
        await conversation.forget("u")
        assert not await ledger.update_delivery(
            "u", "turn", "partial", 2, 0, generation, "accepted"
        )
        assert not await ledger.prepare_delivery("u", "stale", "question", 1, 0, generation)
        assert await conversation.get_history("u") == []
        db.close()

    asyncio.run(scenario())


def test_only_accepted_text_is_stored(tmp_path):
    async def scenario():
        db, conversation, ledger = make_stores(tmp_path / "memory.db")
        generation = await ledger.advance_delivery_generation("u")
        assert await ledger.prepare_delivery("u", "turn", "question", 2, 0, generation)
        with pytest.raises(ValueError):
            await ledger.update_delivery(
                "u", "turn", "in_flight", 1, 0, generation, "candidate"
            )
        assert await ledger.update_delivery("u", "turn", "in_flight", 1, 0, generation)
        assert await ledger.update_delivery(
            "u", "turn", "partial", 1, 0, generation, "first"
        )
        assert await conversation.get_history("u") == [
            ("user", "question"),
            ("assistant", "first"),
        ]
        assert await ledger.update_delivery("u", "turn", "in_flight", 0, 0, generation)
        assert await ledger.update_delivery(
            "u", "turn", "complete", 0, 0, generation, "second"
        )
        assert (await conversation.get_history("u"))[-1] == (
            "assistant",
            "first\nsecond",
        )
        db.close()

    asyncio.run(scenario())


def test_in_flight_withdraws_partial_projection_and_partial_restores_prefix(tmp_path):
    """多气泡：第二泡进入 in_flight 时，第一泡已物化的 assistant
    投影必须撤下（get_history 无 assistant 行）；第二泡被接受后，
    全前缀按序恢复（台账 accepted_text + INSERT 恢复路径）。"""

    async def scenario():
        db, conversation, ledger = make_stores(tmp_path / "memory.db")
        generation = await ledger.advance_delivery_generation("u")
        assert await ledger.prepare_delivery("u", "turn", "question", 3, 0, generation)
        # prepared → in_flight：无投影可删，应为安全 no-op。
        assert await ledger.update_delivery("u", "turn", "in_flight", 2, 0, generation)
        assert await conversation.get_history("u") == [("user", "question")]
        assert await ledger.update_delivery(
            "u", "turn", "partial", 2, 0, generation, "bubble-1"
        )
        assert await conversation.get_history("u") == [
            ("user", "question"),
            ("assistant", "bubble-1"),
        ]
        # 第二泡在途：第一泡前缀撤投影，处理期间不可见。
        assert await ledger.update_delivery("u", "turn", "in_flight", 1, 0, generation)
        assert await conversation.get_history("u") == [("user", "question")]
        # 第二泡被接受：前缀按序完整恢复。
        assert await ledger.update_delivery(
            "u", "turn", "partial", 1, 0, generation, "bubble-2"
        )
        assert await conversation.get_history("u") == [
            ("user", "question"),
            ("assistant", "bubble-1\nbubble-2"),
        ]
        db.close()

    asyncio.run(scenario())


def test_in_flight_fence_rejection_and_failed_close_leave_no_projection(tmp_path):
    """in_flight 中收尾被围栏拒绝、或转 failed 取消后，历史不残留
    已撤下的 assistant 投影。"""

    async def scenario():
        db, conversation, ledger = make_stores(tmp_path / "memory.db")
        generation = await ledger.advance_delivery_generation("u")
        assert await ledger.prepare_delivery("u", "turn", "question", 2, 0, generation)
        assert await ledger.update_delivery("u", "turn", "in_flight", 1, 0, generation)
        assert await ledger.update_delivery(
            "u", "turn", "partial", 1, 0, generation, "bubble-1"
        )
        assert await ledger.update_delivery("u", "turn", "in_flight", 0, 0, generation)
        assert await conversation.get_history("u") == [("user", "question")]
        # 围栏拒绝的迟到 complete 不得复活投影。
        assert not await ledger.update_delivery(
            "u", "turn", "complete", 0, 0, generation + 99, "bubble-2"
        )
        assert await conversation.get_history("u") == [("user", "question")]
        # in_flight 中转 failed 收尾：同样不残留。
        assert await ledger.update_delivery("u", "turn", "failed", 0, 0, generation)
        assert await conversation.get_history("u") == [("user", "question")]
        db.close()

    asyncio.run(scenario())


def test_new_event_and_pack_changes_invalidate_delivery(tmp_path):
    async def scenario():
        db, conversation, ledger = make_stores(tmp_path / "memory.db")
        old = await ledger.advance_delivery_generation("u")
        assert await ledger.prepare_delivery("u", "turn", "question", 2, 0, old)
        assert not await ledger.update_delivery("u", "turn", "in_flight", 0, 0, old)
        assert await ledger.update_delivery("u", "turn", "in_flight", 1, 0, old)
        current = await ledger.advance_delivery_generation("u")
        assert not await ledger.update_delivery(
            "u", "turn", "partial", 1, 0, old, "accepted"
        )
        await conversation.select_pack("u", "friend")
        assert not await ledger.prepare_delivery("u", "new", "question", 1, 0, current)
        db.close()

    asyncio.run(scenario())
