"""claim 落库存储（P5）：与 ingress ClaimStore 完全相同的公开 API。

定义（ClaimOutcome / ClaimRecord / 常量 / 分区映射）一律从纯领域层
companion.domain.claims 导入，不复制（P5 边界裁决：两种存储共用领域
定义，memory 不再反向依赖 ingress）。持久化要点：
- 同 key 任意状态只接受一次，由 claims 表主键保证；
- try_claim 在单一 BEGIN IMMEDIATE 事务内完成判重→计数→淘汰→插入；
- 代次存 schema_meta 的 claim_generation：构造时读旧值 +1 写回，
  并把上一代次遗留的 processing 一律转 abandoned（不重放）；
- critical 豁免计数存 schema_meta 的 claim_exempted，跨代累计；
- EXEMPT 时仍在同一事务内落一条去重记录：partition 记独立标记
  ``exempt``（不计入 ordinary/reserved 的 counts 与容量判定）、状态
  直接置终态 done，使同 key 重投被判重 / ``get`` 前置检查命中，
  不再重复放行危机话术；其保留期淘汰与跨代语义同普通终态记录。
"""

import time
from collections.abc import Callable

from companion.domain.claims import (
    _LANE_PARTITION,
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
)
from companion.memory.database import Database

# EXEMPT 去重记录的独立 partition 标记：不是真实分区——不出现在
# partitions / counts 中，也不参与任一分区的容量判定，仅借 claims
# 表主键实现跨进程、跨重启的同 key 判重。
_PARTITION_EXEMPT = "exempt"

__all__ = [
    "LANE_CONTROL",
    "LANE_CRITICAL",
    "LANE_GROUP",
    "LANE_ORDINARY",
    "ClaimOutcome",
    "ClaimRecord",
    "SqliteClaimStore",
]



class SqliteClaimStore:
    """落库版 claim 表；公开 API 与语义与 ClaimStore 对齐。"""

    def __init__(
        self,
        db: Database,
        *,
        ordinary_capacity: int = 10000,
        reserved_capacity: int = 1000,
        group_capacity: int = 10000,
        retention_seconds: float = 24 * 3600,
        clock: Callable[[], float] = time.time,
    ):
        if ordinary_capacity < 0 or reserved_capacity < 0 or group_capacity < 0:
            raise ValueError("capacities must be >= 0")
        if retention_seconds < 0:
            raise ValueError("retention_seconds must be >= 0")
        self.db = db
        self.ordinary_capacity = ordinary_capacity
        self.reserved_capacity = reserved_capacity
        self.group_capacity = group_capacity
        self.retention_seconds = retention_seconds
        self.clock = clock
        now = self.clock()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='claim_generation'"
            ).fetchone()
            previous_generation = int(row[0]) if row else 0
            self.generation = previous_generation + 1
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES ('claim_generation', ?)",
                (str(self.generation),),
            )
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='claim_exempted'"
            ).fetchone()
            self._exempted_count = int(row[0]) if row else 0
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta VALUES ('claim_exempted', '0')"
            )
            # 上一代次遗留的 processing 不重放：一律转 abandoned。
            conn.execute(
                "UPDATE claims SET state=?, finished_at=? "
                "WHERE generation < ? AND state=?",
                (STATE_ABANDONED, now, self.generation, STATE_PROCESSING),
            )

    # -- 属性 ---------------------------------------------------------------

    @property
    def partitions(self) -> set[str]:
        """对外分区集合恒为 {"ordinary", "reserved"}：group 分区与
        exempt 一样同为辅助分区，不进 partitions / counts 输出。"""
        return {PARTITION_ORDINARY, PARTITION_RESERVED}

    @property
    def exempted_count(self) -> int:
        return self._exempted_count

    # -- 内部工具 -------------------------------------------------------------

    def _capacity(self, partition: str) -> int:
        if partition == PARTITION_ORDINARY:
            return self.ordinary_capacity
        if partition == PARTITION_GROUP:
            return self.group_capacity
        return self.reserved_capacity

    @staticmethod
    def _partition_size(conn, partition: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM claims WHERE partition_name=?", (partition,)
        ).fetchone()[0]

    def _evict_expired(self, conn, partition: str, now: float) -> None:
        """淘汰本分区内超过保留期的终态条目；processing 永不淘汰。"""
        rows = conn.execute(
            "SELECT platform, self_id, message_id, created_at, finished_at "
            "FROM claims WHERE partition_name=? AND state IN (?, ?)",
            (partition, *sorted(TERMINAL_STATES)),
        ).fetchall()
        for platform, self_id, message_id, created_at, finished_at in rows:
            reference = finished_at if finished_at is not None else created_at
            if now - reference > self.retention_seconds:
                conn.execute(
                    "DELETE FROM claims WHERE platform=? AND self_id=? AND message_id=?",
                    (platform, self_id, message_id),
                )

    # -- 公开 API -------------------------------------------------------------

    def try_claim(self, key, lane: str) -> ClaimOutcome:
        if lane not in _LANE_PARTITION:
            raise ValueError(f"unknown lane: {lane!r}")
        key = tuple(key)
        partition = _LANE_PARTITION[lane]
        now = self.clock()
        with self.db.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM claims WHERE platform=? AND self_id=? AND message_id=?",
                key,
            ).fetchone():
                return ClaimOutcome.DUPLICATE
            if self._partition_size(conn, partition) >= self._capacity(partition):
                self._evict_expired(conn, partition, now)
            if self._partition_size(conn, partition) >= self._capacity(partition):
                if lane == LANE_CRITICAL:
                    self._exempted_count += 1
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta VALUES ('claim_exempted', ?)",
                        (str(self._exempted_count),),
                    )
                    # 豁免不等于放弃去重：同事务落终态去重记录（独立
                    # partition 标记，不计入分区容量/counts），同 key
                    # 重投先命中上方判重、或被 Stage 0 的 get 前置检查
                    # 拦下，不再第二次 EXEMPT 放行。落新记录前先按
                    # 保留期淘汰已过期的 exempt 终态记录（与普通终态
                    # 记录同一淘汰口径），避免 exempt 行无界增长。
                    self._evict_expired(conn, _PARTITION_EXEMPT, now)
                    conn.execute(
                        "INSERT INTO claims"
                        "(platform, self_id, message_id, lane, partition_name, state,"
                        " generation, created_at, finished_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            *key,
                            lane,
                            _PARTITION_EXEMPT,
                            STATE_DONE,
                            self.generation,
                            now,
                            now,
                        ),
                    )
                    return ClaimOutcome.EXEMPT
                return ClaimOutcome.FULL
            conn.execute(
                "INSERT INTO claims"
                "(platform, self_id, message_id, lane, partition_name, state,"
                " generation, created_at, finished_at)"
                " VALUES (?,?,?,?,?,?,?, ?, NULL)",
                (*key, lane, partition, STATE_PROCESSING, self.generation, now),
            )
            return ClaimOutcome.CLAIMED

    def finish(self, key, state: str = "done") -> bool:
        if state not in TERMINAL_STATES:
            raise ValueError(f"finish state must be a terminal state, got {state!r}")
        key = tuple(key)
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT state FROM claims WHERE platform=? AND self_id=? AND message_id=?",
                key,
            ).fetchone()
            if row is None or row[0] != STATE_PROCESSING:
                return False
            conn.execute(
                "UPDATE claims SET state=?, finished_at=? "
                "WHERE platform=? AND self_id=? AND message_id=?",
                (state, self.clock(), *key),
            )
            return True

    def release(self, key) -> bool:
        key = tuple(key)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM claims WHERE platform=? AND self_id=? AND message_id=?",
                key,
            )
            return cursor.rowcount > 0

    def get(self, key) -> ClaimRecord | None:
        key = tuple(key)
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT lane, partition_name, state, generation, created_at, finished_at "
                "FROM claims WHERE platform=? AND self_id=? AND message_id=?",
                key,
            ).fetchone()
        if row is None:
            return None
        return ClaimRecord(
            key=key,
            lane=row[0],
            partition=row[1],
            state=row[2],
            generation=row[3],
            created_at=row[4],
            finished_at=row[5],
        )

    def counts(self) -> dict:
        """各分区条目数：恒只含 ordinary/reserved 两键（group 与
        exempt 同为辅助分区，不进本输出）。"""
        with self.db.transaction() as conn:
            return {
                PARTITION_ORDINARY: self._partition_size(conn, PARTITION_ORDINARY),
                PARTITION_RESERVED: self._partition_size(conn, PARTITION_RESERVED),
            }

    def startup_recovery(self, previous=None) -> int:
        """持久化实现无需跨实例迁移：恢复已在构造时完成（旧代次
        processing 转 abandoned、计数与代次均落库），故直接返回 0。
        保留此方法仅为与内存版 ClaimStore 的 duck-type 兼容。"""
        return 0
