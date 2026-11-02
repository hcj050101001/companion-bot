"""P5 存储核心：迁移守恒、故障注入、回滚演练与 claim 跨重启。

所有数据均为合成值、只落在 tmp_path，不含真实 QQ 号或用户数据。
"""

import asyncio
import shutil
import sqlite3

import pytest

from companion.memory import ConversationStore, Database, DeliveryLedger
from companion.memory.claims import (
    LANE_CONTROL,
    LANE_CRITICAL,
    LANE_ORDINARY,
    ClaimOutcome,
    SqliteClaimStore,
)

OLD_FILTER = (
    "SELECT role, content FROM messages WHERE user_id=? "
    "AND (role != 'assistant' OR delivery_state IS NULL OR "
    "delivery_state NOT IN ('prepared', 'in_flight', 'ambiguous', 'failed')) "
    "ORDER BY id DESC LIMIT 40"
)


def build_v0(path, *, with_turns=True):
    """手工建 v0 库：messages 带旧投递列，含 legacy 与各状态回合。"""
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
               created_at TEXT, turn_id TEXT,
               delivery_state TEXT DEFAULT 'legacy_unknown', unsent_count INTEGER)"""
        )
        db.execute(
            "INSERT INTO messages(user_id, role, content) VALUES ('u', 'assistant', 'legacy')"
        )
        if with_turns:
            rows = [
                ("u", "user", "q1", "t-complete", "complete", 0),
                ("u", "assistant", "a1", "t-complete", "complete", 0),
                ("u", "user", "q2", "t-partial", "partial", 1),
                ("u", "assistant", "part", "t-partial", "partial", 1),
                ("u", "user", "q3", "t-flight", "in_flight", 1),
                ("u", "assistant", "flight-part", "t-flight", "in_flight", 1),
                ("u", "user", "q4", "t-amb", "ambiguous", 1),
                ("u", "assistant", "amb-accepted", "t-amb", "ambiguous", 1),
            ]
            db.executemany(
                "INSERT INTO messages(user_id, role, content, turn_id,"
                " delivery_state, unsent_count) VALUES (?,?,?,?,?,?)",
                rows,
            )
        db.execute(
            "CREATE TABLE user_state (user_id TEXT PRIMARY KEY,"
            " onboarded INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute("INSERT INTO user_state VALUES ('u', 1)")


def test_migration_conservation(tmp_path):
    path = tmp_path / "memory.db"
    build_v0(path)
    with sqlite3.connect(path) as db:
        old_history = list(reversed(db.execute(OLD_FILTER, ("u",)).fetchall()))
        total_before = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        users_before = db.execute(
            "SELECT COUNT(*) FROM messages WHERE role='user'"
        ).fetchone()[0]

    async def scenario():
        db = Database(path)
        conversation = ConversationStore(db)
        history = await conversation.get_history("u")
        db.close()
        return history

    new_history = asyncio.run(scenario())
    # 迁移守恒：新口径 history 与迁移前旧过滤口径完全一致。
    assert new_history == old_history
    with sqlite3.connect(path) as db:
        turns = {
            row[0]: (row[1], row[2])
            for row in db.execute(
                "SELECT turn_id, state, accepted_text FROM delivery_turns"
            )
        }
        total_after = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        users_after = db.execute(
            "SELECT COUNT(*) FROM messages WHERE role='user'"
        ).fetchone()[0]
    assert turns == {
        "t-complete": ("complete", "a1"),
        "t-partial": ("partial", "part"),
        "t-flight": ("ambiguous", "flight-part"),  # in_flight → ambiguous
        "t-amb": ("ambiguous", "amb-accepted"),
    }
    # 被删的只有不可见 assistant 行（in_flight / ambiguous 两行）；
    # user 行一律保留，可见行数与旧 history 一致。
    assert users_after == users_before
    assert total_before - total_after == 2
    assert total_after == len(old_history)


def test_fault_injection_between_ledger_and_projection(tmp_path):
    path = tmp_path / "memory.db"

    async def scenario():
        db = Database(path)
        conversation, ledger = ConversationStore(db), DeliveryLedger(db)
        generation = await ledger.advance_delivery_generation("u")

        def boom(point):
            assert point == "after_ledger_write"
            raise RuntimeError("simulated crash between the two writes")

        # prepare 在两写间崩溃：整个事务回滚，两边都不落盘。
        db.fault_hook = boom
        with pytest.raises(RuntimeError):
            await ledger.prepare_delivery("u", "turn", "question", 2, 0, generation)
        with sqlite3.connect(path) as raw:
            assert raw.execute("SELECT COUNT(*) FROM delivery_turns").fetchone()[0] == 0
            assert raw.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0

        # 清 hook 后重试可成功。
        db.fault_hook = None
        assert await ledger.prepare_delivery("u", "turn", "question", 2, 0, generation)
        assert await ledger.update_delivery("u", "turn", "in_flight", 1, 0, generation)

        # update 到 partial 时同样崩溃：台账状态与历史投影都不变。
        db.fault_hook = boom
        with pytest.raises(RuntimeError):
            await ledger.update_delivery(
                "u", "turn", "partial", 1, 0, generation, "accepted"
            )
        with sqlite3.connect(path) as raw:
            row = raw.execute(
                "SELECT state, accepted_text FROM delivery_turns WHERE turn_id='turn'"
            ).fetchone()
        assert row == ("in_flight", "")
        assert await conversation.get_history("u") == [("user", "question")]

        db.fault_hook = None
        assert await ledger.update_delivery(
            "u", "turn", "partial", 1, 0, generation, "accepted"
        )
        assert await conversation.get_history("u") == [
            ("user", "question"),
            ("assistant", "accepted"),
        ]
        db.close()

    asyncio.run(scenario())


def test_forget_while_delivery_in_flight(tmp_path):
    path = tmp_path / "memory.db"

    async def scenario():
        db = Database(path)
        conversation, ledger = ConversationStore(db), DeliveryLedger(db)
        generation = await ledger.advance_delivery_generation("u")
        assert await ledger.prepare_delivery("u", "turn", "question", 2, 0, generation)
        assert await ledger.update_delivery("u", "turn", "in_flight", 1, 0, generation)
        await conversation.forget("u")
        # 投递在途时忘掉：迟到的 update 必须被围栏拦下。
        assert not await ledger.update_delivery(
            "u", "turn", "partial", 1, 0, generation, "accepted"
        )
        assert await conversation.get_history("u") == []
        db.close()

    asyncio.run(scenario())
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT COUNT(*) FROM delivery_turns").fetchone()[0] == 0


def test_snapshot_rollback_rehearsal(tmp_path):
    """快照回滚演练。

    已知的快照回滚损失面：快照之后（迁移后）新收的消息、以及
    forget 做出的删除，都会随快照恢复而回滚——即新消息丢失、
    已删除的数据复现。这是 WAL 快照恢复的既定取舍，此测试把
    损失面钉住：恢复后能且只能看到迁移前的数据。
    """
    path = tmp_path / "memory.db"
    snapshot = tmp_path / "snapshot.db"
    build_v0(path)
    with sqlite3.connect(path) as db:
        original_rows = db.execute(
            "SELECT user_id, role, content FROM messages ORDER BY id"
        ).fetchall()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with sqlite3.connect(snapshot) as dest:
            db.backup(dest)

    async def scenario():
        db = Database(path)  # 打开即迁移到 v1
        conversation = ConversationStore(db)
        await conversation.add_turn("u", "new-q", "new-a")
        await conversation.forget("u")
        assert await conversation.get_history("u") == []
        db.close()

    asyncio.run(scenario())

    # 用快照覆盖回原路径（连同 WAL 残留一并清掉）。
    for suffix in ("-wal", "-shm"):
        residual = tmp_path / f"memory.db{suffix}"
        if residual.exists():
            residual.unlink()
    shutil.copyfile(snapshot, path)

    # 以旧口径 reader（直接按 v0 schema 读）打开：迁移前数据完整，
    # 迁移后写入与 forget 的删除都不存在。
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT user_id, role, content FROM messages ORDER BY id"
        ).fetchall()
        onboarded = db.execute(
            "SELECT onboarded FROM user_state WHERE user_id='u'"
        ).fetchone()
    assert rows == original_rows
    assert ("u", "user", "new-q") not in rows
    assert onboarded == (1,)


def _key(mid: str) -> tuple[str, str, str]:
    return ("onebot", "1000", mid)


def test_claim_store_survives_restart(tmp_path):
    path = tmp_path / "memory.db"
    db1 = Database(path)
    first = SqliteClaimStore(db1)
    assert first.try_claim(_key("crit-1"), LANE_CRITICAL) is ClaimOutcome.CLAIMED
    assert first.finish(_key("crit-1")) is True
    assert first.try_claim(_key("proc-1"), LANE_ORDINARY) is ClaimOutcome.CLAIMED
    first_generation = first.generation
    db1.close()

    # 第二代进程：同 key 仍 DUPLICATE；上一代遗留 processing 转
    # abandoned 且不重放。
    db2 = Database(path)
    second = SqliteClaimStore(db2)
    assert second.generation == first_generation + 1
    assert second.try_claim(_key("crit-1"), LANE_CRITICAL) is ClaimOutcome.DUPLICATE
    record = second.get(_key("proc-1"))
    assert record is not None and record.state == "abandoned"
    assert record.generation == first_generation
    assert second.try_claim(_key("proc-1"), LANE_ORDINARY) is ClaimOutcome.DUPLICATE
    assert second.startup_recovery() == 0
    db2.close()

    # 分区容量与 EXEMPT 计数跨代保持（落库累计）。
    small_path = tmp_path / "small.db"
    db_a = Database(small_path)
    store_a = SqliteClaimStore(db_a, ordinary_capacity=10, reserved_capacity=1)
    assert store_a.try_claim(_key("r1"), LANE_CONTROL) is ClaimOutcome.CLAIMED
    assert store_a.try_claim(_key("r2"), LANE_CRITICAL) is ClaimOutcome.EXEMPT
    assert store_a.exempted_count == 1
    db_a.close()

    db_b = Database(small_path)
    store_b = SqliteClaimStore(db_b, ordinary_capacity=10, reserved_capacity=1)
    assert store_b.counts() == {"ordinary": 0, "reserved": 1}
    assert store_b.exempted_count == 1
    assert store_b.try_claim(_key("r3"), LANE_CRITICAL) is ClaimOutcome.EXEMPT
    assert store_b.exempted_count == 2
    db_b.close()

    db_c = Database(small_path)
    store_c = SqliteClaimStore(db_c, ordinary_capacity=10, reserved_capacity=1)
    assert store_c.exempted_count == 2
    db_c.close()


def test_exempt_claim_leaves_terminal_dedup_record(tmp_path):
    """reserved 满时 EXEMPT 仍落终态去重记录（独立 partition 标记）：
    同 key 同进程重投与跨实例（模拟重启）重投都被判重拦下，不再
    第二次 EXEMPT 放行；exempt 记录不计入任何分区容量/counts。"""
    path = tmp_path / "memory.db"
    db = Database(path)
    store = SqliteClaimStore(db, ordinary_capacity=2, reserved_capacity=1)
    assert store.try_claim(_key("r1"), LANE_CONTROL) is ClaimOutcome.CLAIMED
    crisis = _key("exempt-crisis")
    assert store.try_claim(crisis, LANE_CRITICAL) is ClaimOutcome.EXEMPT
    assert store.exempted_count == 1
    record = store.get(crisis)
    assert record is not None and record.state == "done"
    assert record.partition == "exempt"
    first_generation = store.generation
    assert record.generation == first_generation
    # exempt 记录不占分区容量：counts 与普通分区 claim 均不受影响。
    assert store.counts() == {"ordinary": 0, "reserved": 1}
    assert store.try_claim(_key("o1"), LANE_ORDINARY) is ClaimOutcome.CLAIMED
    assert store.counts() == {"ordinary": 1, "reserved": 1}
    # 同进程同 key 重投：DUPLICATE 而非再次 EXEMPT，豁免计数不重复累计。
    assert store.try_claim(crisis, LANE_CRITICAL) is ClaimOutcome.DUPLICATE
    assert store.exempted_count == 1
    db.close()

    # 跨实例（模拟重启）：去重记录仍在、终态不被代次恢复转 abandoned。
    db2 = Database(path)
    store2 = SqliteClaimStore(db2, ordinary_capacity=2, reserved_capacity=1)
    record2 = store2.get(crisis)
    assert record2 is not None and record2.state == "done"
    assert record2.generation == first_generation
    assert store2.try_claim(crisis, LANE_CRITICAL) is ClaimOutcome.DUPLICATE
    assert store2.exempted_count == 1
    assert store2.counts() == {"ordinary": 1, "reserved": 1}
    db2.close()


def test_exempt_dedup_record_expires_like_terminal_records(tmp_path):
    """exempt 终态记录按保留期淘汰（与普通终态一致）：下一次 EXEMPT
    落记录前，已过期的 exempt 记录被淘汰，不无界增长。"""
    path = tmp_path / "memory.db"
    now = [1000.0]
    db = Database(path)
    store = SqliteClaimStore(
        db,
        ordinary_capacity=10,
        reserved_capacity=1,
        retention_seconds=100,
        clock=lambda: now[0],
    )
    # reserved 被 processing 占满（processing 永不淘汰，分区持续满）。
    assert store.try_claim(_key("r1"), LANE_CONTROL) is ClaimOutcome.CLAIMED
    first = _key("exempt-1")
    assert store.try_claim(first, LANE_CRITICAL) is ClaimOutcome.EXEMPT
    assert store.get(first) is not None
    now[0] += 101
    second = _key("exempt-2")
    assert store.try_claim(second, LANE_CRITICAL) is ClaimOutcome.EXEMPT
    assert store.get(first) is None
    assert store.get(second) is not None
    assert store.counts() == {"ordinary": 0, "reserved": 1}
    db.close()


def test_wal_and_schema_version(tmp_path):
    path = tmp_path / "memory.db"
    db = Database(path)
    db.close()
    with sqlite3.connect(path) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        row = raw.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    assert row == ("1",)


def test_claim_store_capacity_release_and_eviction(tmp_path):
    """SqliteClaimStore 与进程内实现语义对齐（P5 Review #9）：分区满
    FULL、release 释放占位、终态超保留期可淘汰、processing 永不淘汰。
    注入假时钟，不依赖真实时间流逝。"""
    path = tmp_path / "memory.db"
    now = [1000.0]
    store = SqliteClaimStore(
        Database(path),
        ordinary_capacity=1,
        reserved_capacity=1,
        retention_seconds=100,
        clock=lambda: now[0],
    )
    assert store.try_claim(_key("o1"), LANE_ORDINARY) is ClaimOutcome.CLAIMED
    # 普通分区被 processing 占满：processing 永不淘汰 → FULL。
    assert store.try_claim(_key("o2"), LANE_ORDINARY) is ClaimOutcome.FULL
    # release 释放占位后可再 claim。
    assert store.release(_key("o1")) is True
    assert store.try_claim(_key("o2"), LANE_ORDINARY) is ClaimOutcome.CLAIMED
    # 终态在保留期内仍占位 → FULL；超过保留期被淘汰 → CLAIMED。
    assert store.finish(_key("o2")) is True
    assert store.try_claim(_key("o3"), LANE_ORDINARY) is ClaimOutcome.FULL
    now[0] += 101
    assert store.try_claim(_key("o3"), LANE_ORDINARY) is ClaimOutcome.CLAIMED
    # processing 即使远超保留期也永不淘汰。
    now[0] += 100000
    assert store.try_claim(_key("o4"), LANE_ORDINARY) is ClaimOutcome.FULL


def test_newer_schema_version_is_refused(tmp_path):
    """比本代码更新的库绝不静默按 v1 打开（P5 Review #3）。"""
    path = tmp_path / "memory.db"
    Database(path).close()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE schema_meta SET value='99' WHERE key='schema_version'")
    with pytest.raises(RuntimeError):
        Database(path)


def test_reupgrade_after_legacy_writes_self_heals(tmp_path):
    """仅换代码回滚（未按 §9 恢复快照）时，旧代码在 v1 库上按 v0
    口径写下的 in_flight 回合，再升级打开须自愈回填台账：已接受
    前缀不进历史、台账状态转 ambiguous（P5 Review #3）。"""
    path = tmp_path / "memory.db"
    Database(path).close()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO messages(user_id, role, content, turn_id,"
            " delivery_state, unsent_count) VALUES"
            " ('u', 'user', 'q', 't-legacy', 'in_flight', 0)"
        )
        db.execute(
            "INSERT INTO messages(user_id, role, content, turn_id,"
            " delivery_state, unsent_count) VALUES"
            " ('u', 'assistant', 'partial-prefix', 't-legacy', 'in_flight', 0)"
        )
    db = Database(path)
    history = asyncio.run(ConversationStore(db).get_history("u"))
    assert ("assistant", "partial-prefix") not in history
    assert ("user", "q") in history
    with sqlite3.connect(path) as raw:
        row = raw.execute(
            "SELECT state FROM delivery_turns WHERE user_id='u' AND turn_id='t-legacy'"
        ).fetchone()
    assert row == ("ambiguous",)
    db.close()
