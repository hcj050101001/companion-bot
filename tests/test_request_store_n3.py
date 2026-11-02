"""N3 request 处置持久化状态机（方案 §4 C6）：SqliteRequestStore 契约。

只覆盖 companion/domain/requests.py 的纯定义与
companion/memory/requests.py 的落库状态机。所有标识均为合成值，
只落在 tmp_path 的真实 sqlite Database，不含真实 QQ 号或用户数据。
"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from companion.domain.requests import (
    DECISION_APPROVED,
    DECISION_MANUAL,
    DECISION_REJECTED,
    REQUEST_STATUS_AMBIGUOUS,
    REQUEST_STATUS_DONE,
    REQUEST_STATUS_FAILED,
    REQUEST_STATUS_PENDING,
    REQUEST_STATUS_PROCESSING,
    REQUEST_TERMINAL_STATES,
    RESULT_HANDLER_ERROR,
    RESULT_PLATFORM_EXPIRED,
    RESULT_PLATFORM_REJECTED,
    RESULT_POLICY_MANUAL,
    RequestBegin,
    request_key,
)
from companion.memory import Database, SqliteRequestStore
from companion.memory.claims import SqliteClaimStore


class FakeClock:
    """可手动设定的时钟，注入 SqliteRequestStore 的 clock 参数。"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def key(flag: str, sub_type=""):
    return ("1000", "friend", sub_type, flag)


def make_store(tmp_path, clock=None):
    db = Database(tmp_path / "memory.db")
    kwargs = {} if clock is None else {"clock": clock}
    return db, SqliteRequestStore(db, **kwargs)


# -- 键归一 -----------------------------------------------------------------


def test_request_key_normalization():
    assert request_key("1000", "friend", None, "f1") == ("1000", "friend", "", "f1")
    assert request_key(1000, "friend", "", "f1") == ("1000", "friend", "", "f1")
    # sub_type None 与 "" 是同一个键。
    assert request_key("1000", "group", None, "f2") == request_key(
        "1000", "group", "", "f2"
    )


def test_request_key_empty_flag_rejected():
    with pytest.raises(ValueError):
        request_key("1000", "friend", "", "")
    with pytest.raises(ValueError):
        request_key("1000", "friend", None, None)


def test_store_methods_normalize_key(tmp_path):
    _, store = make_store(tmp_path)
    begin = store.try_begin(("1000", "group", None, "fx"), user_id="42")
    assert begin == RequestBegin(created=True, status=REQUEST_STATUS_PENDING)
    # "" 口径的同一键：判重命中，且记录可读。
    assert store.try_begin(("1000", "group", "", "fx")).created is False
    record = store.get(("1000", "group", None, "fx"))
    assert record is not None
    assert record.key == ("1000", "group", "", "fx")
    assert record.user_id == "42"
    with pytest.raises(ValueError):
        store.try_begin(("1000", "group", "", ""))


# -- begin 判重 / 不重置 -------------------------------------------------------


def test_try_begin_creates_pending(tmp_path):
    _, store = make_store(tmp_path)
    begin = store.try_begin(key("a"), user_id="42", group_id="777")
    assert begin.created is True
    assert begin.status == REQUEST_STATUS_PENDING
    record = store.get(key("a"))
    assert record.status == REQUEST_STATUS_PENDING
    assert record.user_id == "42"
    assert record.group_id == "777"
    assert record.decision is None
    assert record.result_code is None
    assert record.generation == store.generation
    assert record.created_at == record.updated_at


def test_try_begin_duplicate_returns_current_status(tmp_path):
    _, store = make_store(tmp_path)
    assert store.try_begin(key("a"), user_id="42").created is True
    again = store.try_begin(key("a"), user_id="99", group_id="1")
    assert again.created is False
    assert again.status == REQUEST_STATUS_PENDING
    # 不覆盖、不重置：首次登记的处置要素原样保留。
    record = store.get(key("a"))
    assert record.user_id == "42"
    assert record.group_id is None


def test_try_begin_after_complete_still_done(tmp_path):
    _, store = make_store(tmp_path)
    store.try_begin(key("a"))
    assert store.claim_processing(key("a")) is True
    assert store.complete(key("a"), status=REQUEST_STATUS_DONE) is True
    begin = store.try_begin(key("a"))
    assert begin.created is False
    assert begin.status == REQUEST_STATUS_DONE
    assert store.get(key("a")).status == REQUEST_STATUS_DONE


# -- claim / complete 状态机 ---------------------------------------------------


def test_claim_processing_only_once(tmp_path):
    _, store = make_store(tmp_path)
    assert store.claim_processing(key("missing")) is False
    store.try_begin(key("a"))
    assert store.claim_processing(key("a")) is True
    assert store.get(key("a")).status == REQUEST_STATUS_PROCESSING
    assert store.claim_processing(key("a")) is False


def test_complete_requires_processing(tmp_path):
    _, store = make_store(tmp_path)
    assert store.complete(key("missing"), status=REQUEST_STATUS_DONE) is False
    store.try_begin(key("a"))
    # 仍是 pending：不可直接落终态。
    assert store.complete(key("a"), status=REQUEST_STATUS_DONE) is False
    assert store.get(key("a")).status == REQUEST_STATUS_PENDING


@pytest.mark.parametrize(
    ("status", "decision", "result_code"),
    [
        (REQUEST_STATUS_DONE, DECISION_APPROVED, None),
        (REQUEST_STATUS_AMBIGUOUS, DECISION_MANUAL, RESULT_POLICY_MANUAL),
        (REQUEST_STATUS_FAILED, DECISION_REJECTED, RESULT_PLATFORM_REJECTED),
        (REQUEST_STATUS_FAILED, None, RESULT_PLATFORM_EXPIRED),
        (REQUEST_STATUS_FAILED, None, RESULT_HANDLER_ERROR),
    ],
)
def test_complete_terminal_states(tmp_path, status, decision, result_code):
    _, store = make_store(tmp_path)
    store.try_begin(key("a"))
    assert store.claim_processing(key("a")) is True
    assert (
        store.complete(
            key("a"), status=status, decision=decision, result_code=result_code
        )
        is True
    )
    record = store.get(key("a"))
    assert record.status == status
    assert record.status in REQUEST_TERMINAL_STATES
    assert record.decision == decision
    assert record.result_code == result_code
    # 终态不可再变：二次 complete / 再 claim 都被拒。
    assert store.complete(key("a"), status=REQUEST_STATUS_DONE) is False
    assert store.claim_processing(key("a")) is False


def test_complete_rejects_non_terminal_status(tmp_path):
    _, store = make_store(tmp_path)
    store.try_begin(key("a"))
    store.claim_processing(key("a"))
    for bad in (REQUEST_STATUS_PENDING, REQUEST_STATUS_PROCESSING, "bogus"):
        with pytest.raises(ValueError):
            store.complete(key("a"), status=bad)
    assert store.get(key("a")).status == REQUEST_STATUS_PROCESSING


# -- 并发原子占用 ---------------------------------------------------------------


def test_concurrent_claim_processing_exactly_one(tmp_path):
    db = Database(tmp_path / "memory.db")
    store_a = SqliteRequestStore(db)
    store_b = SqliteRequestStore(db)
    assert store_a.try_begin(key("a")).created is True
    stores = [store_a, store_b] * 4
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda s: s.claim_processing(key("a")), stores))
    assert results.count(True) == 1
    assert results.count(False) == len(stores) - 1
    assert store_a.get(key("a")).status == REQUEST_STATUS_PROCESSING


# -- 崩溃恢复（代次） -----------------------------------------------------------


def test_crash_recovery_processing_becomes_ambiguous(tmp_path, monkeypatch):
    db, store1 = make_store(tmp_path)
    store1.try_begin(key("dispatched"))
    store1.try_begin(key("still-pending"))
    assert store1.claim_processing(key("dispatched")) is True

    # 进程崩溃后重启：换 boot 身份 + 心跳阈值归零模拟「前任 boot
    # 已失联」（复审 P3 的心跳语义：失联才清扫），新 store 代次 +1。
    monkeypatch.setattr(
        "companion.memory.requests._PROCESS_BOOT_ID", "another-boot"
    )
    monkeypatch.setattr("companion.memory.requests._BOOT_STALE_SECONDS", 0.0)
    store2 = SqliteRequestStore(db)
    assert store2.generation == store1.generation + 1

    crashed = store2.get(key("dispatched"))
    assert crashed.status == REQUEST_STATUS_AMBIGUOUS
    # ambiguous 不释放、不重做：占用与完成都拒。
    assert store2.claim_processing(key("dispatched")) is False
    assert store2.complete(key("dispatched"), status=REQUEST_STATUS_DONE) is False
    assert store2.try_begin(key("dispatched")).status == REQUEST_STATUS_AMBIGUOUS

    # pending 行不受代次影响：仍 pending、可清扫、可续驱到终态。
    pending = store2.get(key("still-pending"))
    assert pending.status == REQUEST_STATUS_PENDING
    assert [r.key for r in store2.list_pending()] == [key("still-pending")]
    assert store2.claim_processing(key("still-pending")) is True
    assert (
        store2.complete(
            key("still-pending"),
            status=REQUEST_STATUS_DONE,
            decision=DECISION_APPROVED,
        )
        is True
    )


def test_done_records_survive_restart(tmp_path):
    db, store1 = make_store(tmp_path)
    store1.try_begin(key("a"))
    store1.claim_processing(key("a"))
    store1.complete(
        key("a"), status=REQUEST_STATUS_DONE, decision=DECISION_APPROVED
    )
    store2 = SqliteRequestStore(db)
    record = store2.get(key("a"))
    assert record.status == REQUEST_STATUS_DONE
    assert record.decision == DECISION_APPROVED
    assert store2.try_begin(key("a")).created is False


# -- counts / list_pending -------------------------------------------------------


def test_counts_and_list_pending_order(tmp_path):
    clock = FakeClock()
    _, store = make_store(tmp_path, clock=clock)
    clock.now = 300.0
    store.try_begin(key("c"))
    clock.now = 100.0
    store.try_begin(key("a"))
    clock.now = 200.0
    store.try_begin(key("b"))
    store.try_begin(key("done-1"))
    store.claim_processing(key("done-1"))
    store.complete(key("done-1"), status=REQUEST_STATUS_DONE)

    # list_pending 按 created_at 升序，与插入顺序无关。
    assert [r.key for r in store.list_pending()] == [
        key("a"),
        key("b"),
        key("c"),
    ]
    assert [r.key for r in store.list_pending(limit=2)] == [key("a"), key("b")]
    assert store.counts() == {
        REQUEST_STATUS_PENDING: 3,
        REQUEST_STATUS_DONE: 1,
    }


def test_get_missing_and_empty_counts(tmp_path):
    _, store = make_store(tmp_path)
    assert store.get(key("nope")) is None
    assert store.list_pending() == []
    assert store.counts() == {}


# -- 与 claim 同库共存 ------------------------------------------------------------


def test_coexists_with_claim_store_same_database(tmp_path):
    db = Database(tmp_path / "memory.db")
    claims = SqliteClaimStore(db)
    requests = SqliteRequestStore(db)
    assert claims.generation >= 1
    assert requests.generation == 1
    assert requests.try_begin(key("a")).created is True
    assert requests.counts() == {REQUEST_STATUS_PENDING: 1}
    # claim 侧代次键独立；同进程再建 request store 复用当前代次
    # （不算重启、不推进代次，补审第三段 P3 的 boot 身份口径）。
    requests2 = SqliteRequestStore(db)
    assert requests2.generation == 1
    assert requests2.get(key("a")).status == REQUEST_STATUS_PENDING
