"""Stage 0 单一接受边界的进程内 claim 存储（方案 §5.3，P3）。

claim 键为 ``(platform, self_id, message_id)``。claim 表按道分区：普通道走
普通分区（硬上限），critical / control 共用保留分区，普通流量挤不占保留
分区；保留分区也满时，关键词危机走豁免 claim（EXEMPT，放弃去重、落审计
计数）。条目状态为 processing / done / abandoned；done 等终态保留
``retention_seconds``（默认 24h）后可被淘汰，淘汰只限终态，本代次的
processing 永不淘汰。条目带进程代次号；启动恢复时上一代次遗留的
processing 一律转 abandoned 落入新代次存储（不重放）。

分道/分区/状态/记录等纯定义在 ``companion.domain.claims``（P5 下沉，
进程内与落库两种存储共用），本模块只保留进程内存储实现，并转发
导出全部纯定义，既有导入路径不变。生产接受边界自 P5 起使用落库
实现 ``companion.memory.claims.SqliteClaimStore``（同 API）；本类保留
为纯内存实现与契约参照。
"""

import time
from collections.abc import Callable

from companion.domain.claims import (
    _LANE_PARTITION,
    CONTROL_PHRASES,
    LANE_CONTROL,
    LANE_CRITICAL,
    LANE_GROUP,
    LANE_ORDINARY,
    PARTITION_GROUP,
    PARTITION_ORDINARY,
    PARTITION_RESERVED,
    STATE_ABANDONED,
    STATE_DONE,
    STATE_PROCESSING,
    TERMINAL_STATES,
    ClaimOutcome,
    ClaimRecord,
    classify_lane,
)

__all__ = [
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
    "ClaimStore",
    "classify_lane",
]


class ClaimStore:
    """按道分区的有界 claim 表，同一 key 任意状态下只接受一次。"""

    def __init__(
        self,
        *,
        ordinary_capacity: int = 10000,
        reserved_capacity: int = 1000,
        group_capacity: int = 10000,
        retention_seconds: float = 24 * 3600,
        generation: int = 1,
        clock: Callable[[], float] = time.time,
    ):
        if ordinary_capacity < 0 or reserved_capacity < 0 or group_capacity < 0:
            raise ValueError("capacities must be >= 0")
        if retention_seconds < 0:
            raise ValueError("retention_seconds must be >= 0")
        self.ordinary_capacity = ordinary_capacity
        self.reserved_capacity = reserved_capacity
        self.group_capacity = group_capacity
        self.retention_seconds = retention_seconds
        self.generation = generation
        self.clock = clock
        self._records: dict[tuple[str, str, str], ClaimRecord] = {}
        self._exempted_count = 0

    @property
    def partitions(self) -> set[str]:
        """分区集合恒为 {"ordinary", "reserved"}（KD-C09 探针读此属性）。"""
        return {PARTITION_ORDINARY, PARTITION_RESERVED}

    @property
    def exempted_count(self) -> int:
        """保留分区满时 critical 豁免的累计次数，供审计与测试。"""
        return self._exempted_count

    # -- 内部工具 ---------------------------------------------------------

    def _capacity(self, partition: str) -> int:
        if partition == PARTITION_ORDINARY:
            return self.ordinary_capacity
        if partition == PARTITION_GROUP:
            return self.group_capacity
        return self.reserved_capacity

    def _partition_size(self, partition: str) -> int:
        return sum(1 for r in self._records.values() if r.partition == partition)

    def _is_expired(self, record: ClaimRecord, now: float) -> bool:
        """终态条目是否已超过保留期；processing 永不过期、永不淘汰。"""
        if record.state not in TERMINAL_STATES:
            return False
        reference = (
            record.finished_at if record.finished_at is not None else record.created_at
        )
        return now - reference > self.retention_seconds

    def _evict_expired(self, partition: str, now: float) -> int:
        """淘汰某分区内已超过保留期的终态条目，返回淘汰数。"""
        doomed = [
            key
            for key, record in self._records.items()
            if record.partition == partition and self._is_expired(record, now)
        ]
        for key in doomed:
            del self._records[key]
        return len(doomed)

    # -- 公开 API ---------------------------------------------------------

    def try_claim(self, key, lane: str) -> ClaimOutcome:
        """尝试为 key 建 processing claim。

        已有同 key（任意状态）→ DUPLICATE；分区满时先淘汰本分区内超过
        保留期的终态条目再判，仍满时 ordinary / control → FULL、
        critical → EXEMPT（不建 claim、豁免计数 +1）；成功 → CLAIMED。
        """
        if lane not in _LANE_PARTITION:
            raise ValueError(f"unknown lane: {lane!r}")
        key = tuple(key)
        if key in self._records:
            return ClaimOutcome.DUPLICATE
        partition = _LANE_PARTITION[lane]
        now = self.clock()
        if self._partition_size(partition) >= self._capacity(partition):
            self._evict_expired(partition, now)
        if self._partition_size(partition) >= self._capacity(partition):
            if lane == LANE_CRITICAL:
                self._exempted_count += 1
                return ClaimOutcome.EXEMPT
            return ClaimOutcome.FULL
        self._records[key] = ClaimRecord(
            key=key,
            lane=lane,
            partition=partition,
            state=STATE_PROCESSING,
            generation=self.generation,
            created_at=now,
            finished_at=None,
        )
        return ClaimOutcome.CLAIMED

    def finish(self, key, state: str = STATE_DONE) -> bool:
        """把 processing 条目转终态并记 finished_at；不存在或非 processing 返回 False。"""
        if state not in TERMINAL_STATES:
            raise ValueError(f"finish state must be a terminal state, got {state!r}")
        record = self._records.get(tuple(key))
        if record is None or record.state != STATE_PROCESSING:
            return False
        record.state = state
        record.finished_at = self.clock()
        return True

    def release(self, key) -> bool:
        """删除条目（入队失败回滚用）；不存在返回 False。"""
        return self._records.pop(tuple(key), None) is not None

    def get(self, key) -> ClaimRecord | None:
        """按 key 取记录，不存在返回 None。"""
        return self._records.get(tuple(key))

    def counts(self) -> dict:
        """当前各分区条目数：{"ordinary": n, "reserved": n}。"""
        return {
            PARTITION_ORDINARY: self._partition_size(PARTITION_ORDINARY),
            PARTITION_RESERVED: self._partition_size(PARTITION_RESERVED),
        }

    def startup_recovery(self, previous: "ClaimStore") -> int:
        """把上一代次存储迁入本存储，返回其中 processing 转 abandoned 的迁移数。

        previous 中 processing 的条目以 abandoned 终态迁入（generation 记
        previous.generation，finished_at 记恢复时刻），迁入后同 key 再
        try_claim 返回 DUPLICATE，即不重放；previous 的终态条目按本存储
        的保留期规则一并迁入，未过期才迁、过期不迁。本存储已有同 key 时
        跳过，不覆盖。
        """
        now = self.clock()
        recovered = 0
        for record in list(previous._records.values()):
            if record.key in self._records:
                continue
            if record.state == STATE_PROCESSING:
                self._records[record.key] = ClaimRecord(
                    key=record.key,
                    lane=record.lane,
                    partition=record.partition,
                    state=STATE_ABANDONED,
                    generation=previous.generation,
                    created_at=record.created_at,
                    finished_at=now,
                )
                recovered += 1
            elif record.state in TERMINAL_STATES:
                if self._is_expired(record, now):
                    continue
                self._records[record.key] = ClaimRecord(
                    key=record.key,
                    lane=record.lane,
                    partition=record.partition,
                    state=record.state,
                    generation=record.generation,
                    created_at=record.created_at,
                    finished_at=record.finished_at,
                )
        return recovered
