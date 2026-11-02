"""P3 claim 存储与分道契约（方案 §5.3 Stage 0 单一接受边界，v4）。

只覆盖 companion/ingress/claims.py 的公开 API。所有标识均为合成值，
不含真实 QQ 号、密钥或用户数据；FakeClock 让保留期判定确定可测。
"""

import pytest

from companion.ingress.claims import (
    LANE_CONTROL,
    LANE_CRITICAL,
    LANE_ORDINARY,
    ClaimOutcome,
    ClaimStore,
    classify_lane,
)

RETENTION = 24 * 3600


class FakeClock:
    """可手动推进的时钟，注入 ClaimStore 的 clock 参数。"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def key(mid: str) -> tuple[str, str, str]:
    return ("onebot", "1000", mid)


def no_hit(text: str):
    return None


def hit(text: str):
    return "crisis-keyword"


# -- 三道分区归属 ----------------------------------------------------------


def test_lane_partition_mapping():
    store = ClaimStore()
    assert store.try_claim(key("a"), LANE_ORDINARY) is ClaimOutcome.CLAIMED
    assert store.try_claim(key("b"), LANE_CONTROL) is ClaimOutcome.CLAIMED
    assert store.try_claim(key("c"), LANE_CRITICAL) is ClaimOutcome.CLAIMED
    assert store.get(key("a")).partition == "ordinary"
    assert store.get(key("b")).partition == "reserved"
    assert store.get(key("c")).partition == "reserved"
    assert store.counts() == {"ordinary": 1, "reserved": 2}


def test_partitions_and_generation():
    store = ClaimStore(generation=7)
    assert store.partitions == {"ordinary", "reserved"}
    assert store.exempted_count == 0
    assert store.try_claim(key("a"), LANE_ORDINARY) is ClaimOutcome.CLAIMED
    record = store.get(key("a"))
    assert record.lane == LANE_ORDINARY
    assert record.state == "processing"
    assert record.generation == 7
    assert record.finished_at is None


# -- duplicate -------------------------------------------------------------


def test_duplicate_any_state():
    store = ClaimStore()
    assert store.try_claim(key("a"), LANE_ORDINARY) is ClaimOutcome.CLAIMED
    # 同 key 换道也算重复（processing）。
    assert store.try_claim(key("a"), LANE_CRITICAL) is ClaimOutcome.DUPLICATE
    assert store.finish(key("a")) is True
    # 终态 done 仍占 key，不可重 claim。
    assert store.try_claim(key("a"), LANE_ORDINARY) is ClaimOutcome.DUPLICATE
    assert store.release(key("a")) is True
    # 释放后 key 空出，才可再 claim。
    assert store.try_claim(key("a"), LANE_ORDINARY) is ClaimOutcome.CLAIMED


def test_duplicate_after_abandoned():
    store = ClaimStore()
    store.try_claim(key("a"), LANE_CRITICAL)
    assert store.finish(key("a"), "abandoned") is True
    assert store.try_claim(key("a"), LANE_CRITICAL) is ClaimOutcome.DUPLICATE


# -- 普通分区满 / 保留分区满 ------------------------------------------------


def test_ordinary_full_but_critical_still_claimed_in_reserved():
    store = ClaimStore(ordinary_capacity=2, reserved_capacity=2)
    assert store.try_claim(key("a"), LANE_ORDINARY) is ClaimOutcome.CLAIMED
    assert store.try_claim(key("b"), LANE_ORDINARY) is ClaimOutcome.CLAIMED
    # 普通分区满：普通道 FULL。
    assert store.try_claim(key("c"), LANE_ORDINARY) is ClaimOutcome.FULL
    assert store.get(key("c")) is None
    # 普通流量挤不占保留分区：critical 仍可从保留分区 CLAIMED。
    assert store.try_claim(key("c"), LANE_CRITICAL) is ClaimOutcome.CLAIMED
    assert store.get(key("c")).partition == "reserved"
    # control 同属保留分区，保留分区未满时亦可 CLAIMED。
    assert store.try_claim(key("d"), LANE_CONTROL) is ClaimOutcome.CLAIMED


def test_reserved_full_blocks_ordinary_only_via_reserved():
    store = ClaimStore(ordinary_capacity=2, reserved_capacity=1)
    assert store.try_claim(key("a"), LANE_CONTROL) is ClaimOutcome.CLAIMED
    # 保留分区满不影响普通分区。
    assert store.try_claim(key("b"), LANE_ORDINARY) is ClaimOutcome.CLAIMED


def test_reserved_full_control_full_critical_exempt():
    store = ClaimStore(ordinary_capacity=10, reserved_capacity=1)
    assert store.try_claim(key("a"), LANE_CONTROL) is ClaimOutcome.CLAIMED
    # 保留分区满：control FULL，不建 claim。
    assert store.try_claim(key("b"), LANE_CONTROL) is ClaimOutcome.FULL
    assert store.get(key("b")) is None
    assert store.exempted_count == 0
    # 保留分区满：critical EXEMPT，不建 claim、豁免计数 +1，放弃去重。
    assert store.try_claim(key("c"), LANE_CRITICAL) is ClaimOutcome.EXEMPT
    assert store.get(key("c")) is None
    assert store.exempted_count == 1
    assert store.counts() == {"ordinary": 0, "reserved": 1}
    # 同一 critical key 再次豁免也不建 claim，计数继续递增（放弃去重）。
    assert store.try_claim(key("c"), LANE_CRITICAL) is ClaimOutcome.EXEMPT
    assert store.exempted_count == 2
    # 保留分区满不影响普通分区。
    assert store.try_claim(key("d"), LANE_ORDINARY) is ClaimOutcome.CLAIMED


def test_ordinary_full_does_not_exempt_ordinary():
    store = ClaimStore(ordinary_capacity=1, reserved_capacity=0)
    store.try_claim(key("a"), LANE_ORDINARY)
    assert store.try_claim(key("b"), LANE_ORDINARY) is ClaimOutcome.FULL
    # 保留分区也满时 critical 才豁免；ordinary 满只 FULL，不计豁免。
    assert store.try_claim(key("c"), LANE_CRITICAL) is ClaimOutcome.EXEMPT
    assert store.exempted_count == 1


# -- 保留期淘汰 ------------------------------------------------------------


def test_terminal_expired_evicted_then_new_claim_succeeds():
    clock = FakeClock()
    store = ClaimStore(ordinary_capacity=1, retention_seconds=RETENTION, clock=clock)
    assert store.try_claim(key("a"), LANE_ORDINARY) is ClaimOutcome.CLAIMED
    assert store.finish(key("a")) is True
    # 保留期内：分区仍满，新 claim FULL。
    clock.advance(RETENTION - 1)
    assert store.try_claim(key("b"), LANE_ORDINARY) is ClaimOutcome.FULL
    # 超过保留期：淘汰终态条目后新 claim CLAIMED。
    clock.advance(2)
    assert store.try_claim(key("b"), LANE_ORDINARY) is ClaimOutcome.CLAIMED
    assert store.get(key("a")) is None
    # 被淘汰的 key 空出，可再 claim（需先 finish 占位的 processing 以空出容量）。
    assert store.finish(key("b")) is True
    clock.advance(RETENTION + 1)
    assert store.try_claim(key("a"), LANE_ORDINARY) is ClaimOutcome.CLAIMED


def test_processing_never_evicted_even_when_over_retention():
    """对照：processing 超保留期也不淘汰；终态未过期时分区仍 FULL。

    时间线：t=0 建 processing A；t=RETENTION-10 建 B 并立即 finish（终态
    新鲜）；t=RETENTION+5 时 A 已超保留期但仍为 processing，B 终态未过期，
    新 claim 必须 FULL，且 A、B 均未被淘汰。
    """
    clock = FakeClock()
    store = ClaimStore(ordinary_capacity=2, retention_seconds=RETENTION, clock=clock)
    assert store.try_claim(key("a"), LANE_ORDINARY) is ClaimOutcome.CLAIMED
    clock.advance(RETENTION - 10)
    assert store.try_claim(key("b"), LANE_ORDINARY) is ClaimOutcome.CLAIMED
    assert store.finish(key("b")) is True
    clock.advance(15)
    assert store.try_claim(key("c"), LANE_ORDINARY) is ClaimOutcome.FULL
    assert store.get(key("a")).state == "processing"
    assert store.get(key("b")).state == "done"


def test_expired_abandoned_also_evicted():
    clock = FakeClock()
    store = ClaimStore(ordinary_capacity=1, retention_seconds=RETENTION, clock=clock)
    store.try_claim(key("a"), LANE_ORDINARY)
    assert store.finish(key("a"), "abandoned") is True
    clock.advance(RETENTION + 1)
    assert store.try_claim(key("b"), LANE_ORDINARY) is ClaimOutcome.CLAIMED
    assert store.get(key("a")) is None


# -- finish / release / get / counts ---------------------------------------


def test_finish_release_get_counts():
    clock = FakeClock()
    store = ClaimStore(clock=clock)
    assert store.finish(key("missing")) is False
    assert store.get(key("missing")) is None
    assert store.release(key("missing")) is False

    store.try_claim(key("a"), LANE_ORDINARY)
    store.try_claim(key("b"), LANE_CRITICAL)
    assert store.counts() == {"ordinary": 1, "reserved": 1}

    clock.advance(5)
    assert store.finish(key("a")) is True
    record = store.get(key("a"))
    assert record.state == "done"
    assert record.finished_at == clock.now
    # 非 processing 再 finish 返回 False，状态不变。
    assert store.finish(key("a")) is False
    assert store.get(key("a")).state == "done"

    assert store.release(key("b")) is True
    assert store.get(key("b")) is None
    assert store.counts() == {"ordinary": 1, "reserved": 0}


def test_finish_abandoned_state():
    store = ClaimStore()
    store.try_claim(key("a"), LANE_ORDINARY)
    assert store.finish(key("a"), "abandoned") is True
    assert store.get(key("a")).state == "abandoned"
    assert store.get(key("a")).finished_at is not None


def test_finish_invalid_state_rejected():
    store = ClaimStore()
    store.try_claim(key("a"), LANE_ORDINARY)
    with pytest.raises(ValueError):
        store.finish(key("a"), "processing")
    # 非法 finish 未改变条目状态。
    assert store.get(key("a")).state == "processing"


# -- startup_recovery ------------------------------------------------------


def test_startup_recovery_converts_processing_to_abandoned_no_replay():
    clock = FakeClock()
    previous = ClaimStore(retention_seconds=RETENTION, generation=1, clock=clock)
    # t=0：一条终态（稍后会过期）与两条 processing。
    previous.try_claim(key("expired"), LANE_ORDINARY)
    assert previous.finish(key("expired")) is True
    previous.try_claim(key("p1"), LANE_ORDINARY)
    previous.try_claim(key("p2"), LANE_CRITICAL)
    # t=RETENTION+1：再建一条新鲜终态（未过期，应迁入）。
    clock.advance(RETENTION + 1)
    previous.try_claim(key("fresh"), LANE_ORDINARY)
    assert previous.finish(key("fresh")) is True

    current = ClaimStore(retention_seconds=RETENTION, generation=2, clock=clock)
    recovered = current.startup_recovery(previous)
    # 返回 processing 转 abandoned 的迁移数。
    assert recovered == 2
    for mid in ("p1", "p2"):
        record = current.get(key(mid))
        assert record is not None
        assert record.state == "abandoned"
        # 代次记上一代次，而非本代次。
        assert record.generation == 1
        assert record.finished_at == clock.now
        # 迁入后同 key try_claim 返回 DUPLICATE：不重放。
        assert current.try_claim(key(mid), LANE_ORDINARY) is ClaimOutcome.DUPLICATE
    # 未过期终态一并迁入并保留原状态与代次。
    fresh = current.get(key("fresh"))
    assert fresh is not None and fresh.state == "done" and fresh.generation == 1
    # 过期终态不迁入。
    assert current.get(key("expired")) is None
    assert current.counts() == {"ordinary": 2, "reserved": 1}


def test_startup_recovery_skips_keys_already_present():
    clock = FakeClock()
    previous = ClaimStore(generation=1, clock=clock)
    previous.try_claim(key("a"), LANE_ORDINARY)
    current = ClaimStore(generation=2, clock=clock)
    current.try_claim(key("a"), LANE_CRITICAL)
    # 本存储已有同 key 时不覆盖，迁移数不计入。
    assert current.startup_recovery(previous) == 0
    assert current.get(key("a")).state == "processing"
    assert current.get(key("a")).generation == 2


# -- classify_lane ---------------------------------------------------------


def test_classify_lane_critical_hit():
    assert classify_lane("我想结束一切", keyword_match=hit) == LANE_CRITICAL
    # 关键词命中优先于控制短语。
    assert classify_lane("忘掉我，我想结束一切", keyword_match=hit) == LANE_CRITICAL


@pytest.mark.parametrize(
    "text",
    ["忘掉这件事", "请删除记忆", "忘掉我吧", "清除记忆"],
)
def test_classify_lane_control_phrases(text):
    assert classify_lane(text, keyword_match=no_hit) == LANE_CONTROL


def test_classify_lane_ordinary():
    assert classify_lane("今天天气真好", keyword_match=no_hit) == LANE_ORDINARY
    assert classify_lane("", keyword_match=no_hit) == LANE_ORDINARY


def test_classify_lane_keyword_match_exception_degrades():
    def boom(text: str):
        raise RuntimeError("safety prescreen unavailable")

    # 异常按未命中处理：控制短语仍走 control，普通文本走 ordinary。
    assert classify_lane("忘掉这件事", keyword_match=boom) == LANE_CONTROL
    assert classify_lane("你好", keyword_match=boom) == LANE_ORDINARY


def test_classify_lane_keyword_match_none_vs_value():
    assert classify_lane("你好", keyword_match=lambda t: None) == LANE_ORDINARY
    assert classify_lane("你好", keyword_match=lambda t: object()) == LANE_CRITICAL
