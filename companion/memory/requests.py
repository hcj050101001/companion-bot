"""request 处置落库存储（N3，方案 §4 C6）：与 claim 同级的持久化状态机。

定义（状态 / 决策 / 结果码常量、RequestRecord / RequestBegin、
``request_key``）一律从纯领域层 companion.domain.requests 导入，
不复制。持久化要点：

- 唯一键 ``(self_id, request_type, sub_type, flag)`` 即表主键，
  同一申请任意状态只登记一次，重复投递由 try_begin 判重；
- try_begin 在单一 BEGIN IMMEDIATE 事务内 INSERT OR IGNORE：
  新建才落 pending，已存在一律返回其现状态，不覆盖、不重置；
- claim_processing 是 pending → processing 的原子占用：UPDATE
  带 ``WHERE status='pending'`` 并看 rowcount，并发 / 重复调用
  只有一个能成功（Database 自带锁串行事务，双重保证）；
- complete 只接受终态（done / ambiguous / failed），且仅当现状态
  为 processing 时才落（rowcount 判定），其余一律 False；
- 代次存 schema_meta 的 request_generation：新进程构造时读旧
  值 +1 写回，只把「已失联 boot」名下代次遗留的 processing 转
  ambiguous——已派发给平台的处置在崩溃后结果不明，不释放、不
  重做；pending 行保持 pending（处置要素都在键内，可由
  list_pending 清扫后 claim_processing 续驱）。boot 语义：每
  代次归属记 request_gen_boot:<代次>，每 boot 的心跳记
  request_boot_seen:<boot> 并在每次写操作时刷新；同进程第二
  实例（二次 create_app / 嵌入式装配）构造复用当前代次、不
  清扫；并发共库的另一进程只要心跳新鲜也不清扫其行；只有
  心跳缺失/过期（_BOOT_STALE_SECONDS）才认定该 boot 已死
  （补审第三段 P3、复审 P3）；清扫在构造时跑一次，并在
  try_begin / list_pending 时惰性重跑——刚崩溃的 boot 心跳
  尚新鲜时不清扫，过期后由后继的下一次读写接管，恢复是
  延迟而非丢失（复审 P2）；claim_processing 落 processing
  时同步打上当前代次。request_boot_id 为最近一次构造的 boot
  身份（兼容留存）。

  拓扑边界（复审 P3，负责人拍板以文档接受、不改清扫行为）：
  心跳只随写操作刷新，没有与写解耦的后台心跳、也没有 claim
  租约；活着的 boot 在 claim 后若写静默达到（>=）
  _BOOT_STALE_SECONDS（例如处置长时间等待平台应答），与崩
  溃 boot 不可区分，其 processing 行会被共库对端扫成
  ambiguous、其后 complete 落空。现网为单进程单库拓扑，
  「双活进程共库且写静默 ≥120 秒」属超出拓扑的情形；且该
  误判方向恒为 fail-closed——在飞处置只被搁浅待人工对账，
  绝不释放重做，不产生重复派发或误批准。

迁移 / 回滚说明：本表不进 Database 的 v1 schema（SCHEMA_VERSION
不变），由本 store 在构造事务内 ``CREATE TABLE IF NOT EXISTS``
自建（N1 SqliteOperationLedger 先例），对既有库纯 additive，与
SqliteClaimStore 同库共存、互不干扰。回滚 = ``DROP TABLE
request_dispositions`` 并删除 schema_meta 中的 ``request_generation``
键，不影响其他任何表。
"""

import time
import uuid
from collections.abc import Callable

from companion.domain.requests import (
    REQUEST_STATUS_AMBIGUOUS,
    REQUEST_STATUS_PENDING,
    REQUEST_STATUS_PROCESSING,
    REQUEST_TERMINAL_STATES,
    RequestBegin,
    RequestRecord,
    request_key,
)
from companion.memory.database import Database

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS request_dispositions (
    self_id TEXT,
    request_type TEXT,
    sub_type TEXT,
    flag TEXT,
    user_id TEXT,
    group_id TEXT,
    status TEXT,
    decision TEXT,
    result_code TEXT,
    generation INTEGER,
    created_at REAL,
    updated_at REAL,
    PRIMARY KEY (self_id, request_type, sub_type, flag)
)
"""

_COLUMNS = (
    "self_id, request_type, sub_type, flag, user_id, group_id, status,"
    " decision, result_code, generation, created_at, updated_at"
)
_KEY_WHERE = "self_id=? AND request_type=? AND sub_type=? AND flag=?"

#: 本进程的启动身份（模块加载时生成一次）：崩溃恢复清扫只应
#: 清扫「已失联 boot」的遗留行——同进程第二实例不清扫（代次
#: 归属即本 boot）；另一进程并发共库时，其代次的心跳新鲜也不
#: 清扫；只有心跳缺失/过期（见 _BOOT_STALE_SECONDS）才认定该
#: boot 已死、其 processing 行是崩溃遗留。归属以 schema_meta
#: 的 request_gen_boot:<代次> 记录，心跳以
#: request_boot_seen:<boot> 在每次写操作时刷新。
_PROCESS_BOOT_ID = uuid.uuid4().hex

#: boot 心跳过期阈值（秒）：无写操作达到此时长（>=）的 boot 视为已死。
_BOOT_STALE_SECONDS = 120.0

__all__ = [
    "RequestBegin",
    "RequestRecord",
    "SqliteRequestStore",
]


def _meta_int(row) -> int:
    """schema_meta 整数值的防御解析：行缺失或值损坏一律回落 0。

    与心跳的容错口径一致（复审 P4）：request_sweep_watermark
    损坏时裸 int() 会把每次 try_begin / list_pending 炸成
    ValueError、整条请求管线砖化；回落 0 对水位线的语义是
    「从头全量重扫」，恰是水位线未知时的安全缺省。本函数
    只负责不抛错的读取口径；request_generation 损坏时新代
    次的起点由构造期按库内数据推导（见 _generation_floor
    与 __init__），不按无历史从 1 重启。
    """
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _generation_floor(conn) -> int:
    """库内数据推导的代次下限：处置行与 gen_boot 键的最大代次。

    request_generation 元数据损坏或滞后于实存数据时（复审
    P3），计数器若按无历史回落 0 或按滞后值起算、新代次不高
    于既存最大代次，高于新计数器的旧 processing
    行落在主扫区间（水位线+1 .. 当前代次）与补扫（仅 ≤ 水位
    线）之外，永不收敛、永久滞留。以下限起计数则新代次必
    高于一切既存行的代次，清扫覆盖面完整——与水位线损坏回
    落全量重扫同为「向数据要真相」的容错口径。无任何历史
    数据时下限为 0，与新库从 1 起一致。
    """
    floor = 0
    row = conn.execute(
        "SELECT MAX(generation) FROM request_dispositions"
    ).fetchone()
    if row is not None and isinstance(row[0], int):
        floor = max(floor, row[0])
    for (key,) in conn.execute(
        "SELECT key FROM schema_meta WHERE key LIKE 'request_gen_boot:%'"
    ):
        try:
            floor = max(floor, int(key.rsplit(":", 1)[1]))
        except ValueError:
            continue
    return floor


class SqliteRequestStore:
    """落库版 request 处置状态机；与 SqliteClaimStore 同库共存。"""

    def __init__(
        self,
        db: Database,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.db = db
        self.clock = clock
        now = self.clock()
        with self.db.transaction() as conn:
            conn.execute(_CREATE_TABLE)
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='request_generation'"
            ).fetchone()
            # 代次起点恒以库内数据推导下限兜底（复审 P3）：
            # request_generation 损坏时 _meta_int 回落 0、滞后
            # （可解析但小于实存最大代次，如部分恢复/手工修复
            # 元数据/旧备份元数据配新处置行）时原样沿用——两
            # 种情形此前只有损坏分支按下限起计数，滞后分支新
            # boot 直接复用死 boot 的旧代次并覆盖其归属行，
            # 该代次的 processing 行被当作活 boot 自有行永不
            # 清扫、永久滞留。下限恒为 max(已存计数器, 数据
            # 推导最大值)（来源：处置行与 gen_boot 键，见
            # _generation_floor），新代次必高于一切既存行。
            previous_generation = max(
                _meta_int(row), _generation_floor(conn)
            )
            owner_row = conn.execute(
                "SELECT value FROM schema_meta WHERE key=?",
                (f"request_gen_boot:{previous_generation}",),
            ).fetchone()
            if row is not None and owner_row is not None and owner_row[0] == _PROCESS_BOOT_ID:
                # 同进程第二实例（二次 create_app / 嵌入式装配）：
                # 当前代次正由本进程持有，复用代次、不清扫——
                # 另一活实例正在处置的 processing 行不是崩溃
                # 遗留，扫成 ambiguous 会把合法处置静默搁浅
                # （补审第三段 P3）。
                self.generation = previous_generation
                self._touch_boot_seen(conn, now)
                return
            self.generation = previous_generation + 1
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES ('request_generation', ?)",
                (str(self.generation),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES ('request_boot_id', ?)",
                (_PROCESS_BOOT_ID,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES (?, ?)",
                (f"request_gen_boot:{self.generation}", _PROCESS_BOOT_ID),
            )
            self._touch_boot_seen(conn, now)
            self._sweep_stale_locked(conn, now)

    # -- 内部工具 -----------------------------------------------------------

    @staticmethod
    def _touch_boot_seen(conn, now: float) -> None:
        """刷新本 boot 的心跳（写操作顺带做，成本一行 upsert）。"""
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta VALUES (?, ?)",
            (f"request_boot_seen:{_PROCESS_BOOT_ID}", repr(now)),
        )

    @staticmethod
    def _sweep_stale_locked(conn, now: float) -> None:
        """已失联 boot 名下代次的遗留 processing 一律转 ambiguous。

        已派发、结果不明，不释放、不重做（留待人工/显式决策）。
        失联判据：该代次所属 boot 无心跳或心跳过期；代次归属
        未知（本机制上线前的老库）按失联论，保住老库的崩溃
        恢复语义。构造时调用一次，try_begin / list_pending 时
        惰性重跑：刚崩溃的 boot 心跳尚新鲜、构造期不清扫，其
        心跳过期后由后继实例的下一次读写接管——恢复是延迟
        （≤ 心跳阈值 + 下一次操作），不是丢失（复审 P2）。
        心跳新鲜的 boot 不被扫，本 boot 刚在本事务刷新过心
        跳、天然豁免；但心跳只随写操作刷新，活 boot 写静默
        达到阈值（>=）同样会被扫——该情形的拓扑边界与 fail-closed
        口径见模块 docstring（负责人拍板文档接受，不以心跳
        线程/租约改变此行为）。

        成本口径（复审 P3）：心跳整表批量读入内存判定，不再
        逐代次一条 SELECT；已清扫代次的 boot 元数据剪除并以
        水位线 request_sweep_watermark 记「连续已决前缀」——
        元数据的扫描与剪除只从水位线之后起，热路径成本只随
        未决代次走、不随历史总量线性增长。活 boot 的代次构成
        前缀的天然屏障、永不被剪。

        水位线只管元数据、永不管清扫的覆盖面（复审 P3）：
        剪除的旧前提「归属 boot 已失联即不会再以本代次产生
        新行」对被误判失联的活 boot 不成立——它写静默被扫、
        代次被剪后苏醒，仍以旧代次 claim，其行落在水位线之
        前，若清扫只扫水位线之后，它真死后这些行将永久停在
        processing（比文档接受的 ambiguous 待对账结局更坏）。
        故清扫 UPDATE 除扫描区间内的失联代次外，还补扫水位
        线之前仍持有 processing 行的代次，逐个按归属失联判
        定：归属行尚存且心跳新鲜者不扫（防误扫活对端的行）；
        归属行确不存在（已剪除且从未苏醒 claim、或老库）才
        按归属未知（失联）论，与老库口径一致。苏醒 boot 的
        归属行由其 claim 时重注册恢复（见 claim_processing）：
        补扫对已剪代次同样按心跳判失联，120 秒窗口对苏醒行
        恢复效力——否则归属恒缺失、恒判失联，心跳新鲜的行
        也会在 1 秒后被扫（复审 P3）。取舍：重注册只恢复本
        代次的归属行（代次号与归属 boot 均不变），不是换新
        代次——「claim 前改用新代次」会让单个 store 的代次
        身份运行中途漂移、归属与审计口径随之含混，仍不取。
        元数据值（request_generation / 水位线）
        一律经 _meta_int 防御解析：损坏回落 0——水位线回落
        0 即全量重扫，不抛错砖化管线（复审 P4）。当前代次
        的读取另以库内数据推导下限兜底（_generation_floor）：
        元数据损坏或落后于实有数据时，扫描区间仍覆盖全部
        既存代次，高代次行不因计数器回落而滞留（复审 P3）。
        取舍：不用「代次变化才重扫全量」——失联是时间驱动的
        （心跳过期与代次增减无关），按代次门控会漏掉刚崩溃
        boot 的延迟接管（复审 P2 钉死的惰性恢复语义）。
        """
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='request_generation'"
        ).fetchone()
        # 清扫覆盖面不得小于库内实有数据（复审 P3）：元数据
        # 损坏/落后于数据时以数据推导下限兜底，高于当前代次
        # 的行同样进扫描区间按归属失联判定，不永久滞留。
        current_generation = max(_meta_int(row), _generation_floor(conn))
        watermark_row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='request_sweep_watermark'"
        ).fetchone()
        watermark = _meta_int(watermark_row)
        owners = {
            key.rsplit(":", 1)[1]: value
            for key, value in conn.execute(
                "SELECT key, value FROM schema_meta"
                " WHERE key LIKE 'request_gen_boot:%'"
            )
        }
        # 心跳整表批量读入（复审 P3）：行缺失与值损坏（float
        # 解析失败）在内存判定里同口径归 None——无法证明存活
        # 即按失联论，与逐条 SELECT 时代的分支语义一致。
        heartbeats: dict[str, float | None] = {}
        for key, value in conn.execute(
            "SELECT key, value FROM schema_meta"
            " WHERE key LIKE 'request_boot_seen:%'"
        ):
            try:
                heartbeats[key.rsplit(":", 1)[1]] = float(value)
            except (TypeError, ValueError):
                heartbeats[key.rsplit(":", 1)[1]] = None

        def is_stale(owner: str | None) -> bool:
            if owner is None:
                # 代次归属未知（老库）按失联论。
                return True
            seen_at = heartbeats.get(owner)
            return seen_at is None or now - seen_at >= _BOOT_STALE_SECONDS

        stale_generations = []
        resolved_through = watermark
        prefix_open = True
        for generation in range(watermark + 1, current_generation + 1):
            owner = owners.get(str(generation))
            if not is_stale(owner):
                # 活 boot 的代次挡住连续已决前缀：它之后的失联
                # 代次照常清扫，但剪除/水位线不越过它。
                prefix_open = False
                continue
            stale_generations.append(generation)
            if prefix_open:
                resolved_through = generation
        if watermark > 0:
            # 补扫水位线之前仍持有 processing 行的代次（见
            # docstring「水位线只管元数据」段）：被误判失联的
            # 活 boot 以已剪代次 claim 的行只能经此收敛。持有
            # 行的代次以一条 DISTINCT 查出（在飞请求量级，与
            # 历史代次总量无关），逐个按归属失联判定。
            stranded = conn.execute(
                "SELECT DISTINCT generation FROM request_dispositions"
                " WHERE status=? AND generation<=?",
                (REQUEST_STATUS_PROCESSING, watermark),
            ).fetchall()
            stale_generations.extend(
                generation
                for (generation,) in stranded
                if is_stale(owners.get(str(generation)))
            )
        if stale_generations:
            placeholders = ",".join("?" for _ in stale_generations)
            conn.execute(
                "UPDATE request_dispositions SET status=?, updated_at=? "
                f"WHERE generation IN ({placeholders}) AND status=?",
                (
                    REQUEST_STATUS_AMBIGUOUS,
                    now,
                    *stale_generations,
                    REQUEST_STATUS_PROCESSING,
                ),
            )
        if resolved_through > watermark:
            # 剪除连续已决前缀的 gen_boot 键并推进水位线；前缀
            # 内失联 boot 的心跳键在其代次归属已全部剪除后一并
            # 删除，防 schema_meta 随重启次数单调增长。本 boot
            # 与仍有代次归属的 boot 的心跳键不动。
            for generation in range(watermark + 1, resolved_through + 1):
                conn.execute(
                    "DELETE FROM schema_meta WHERE key=?",
                    (f"request_gen_boot:{generation}",),
                )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES"
                " ('request_sweep_watermark', ?)",
                (str(resolved_through),),
            )
            referenced_owners = {
                owners[str(generation)]
                for generation in range(
                    resolved_through + 1, current_generation + 1
                )
                if str(generation) in owners
            }
            for owner in heartbeats:
                if owner == _PROCESS_BOOT_ID or owner in referenced_owners:
                    continue
                if is_stale(owner):
                    conn.execute(
                        "DELETE FROM schema_meta WHERE key=?",
                        (f"request_boot_seen:{owner}",),
                    )

    @staticmethod
    def _normalize(key) -> tuple[str, str, str, str]:
        return request_key(*key)

    @staticmethod
    def _record(row) -> RequestRecord:
        return RequestRecord(
            key=(row[0], row[1], row[2], row[3]),
            user_id=row[4],
            group_id=row[5],
            status=row[6],
            decision=row[7],
            result_code=row[8],
            generation=row[9],
            created_at=row[10],
            updated_at=row[11],
        )

    # -- 公开 API -------------------------------------------------------------

    def try_begin(self, key, *, user_id: str = "", group_id=None) -> RequestBegin:
        """登记一条处置（pending）；已存在则返回其现状态，绝不重置。"""
        key = self._normalize(key)
        now = self.clock()
        with self.db.transaction() as conn:
            self._touch_boot_seen(conn, now)
            self._sweep_stale_locked(conn, now)
            cursor = conn.execute(
                "INSERT OR IGNORE INTO request_dispositions"
                f"({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    *key,
                    str(user_id),
                    None if group_id is None else str(group_id),
                    REQUEST_STATUS_PENDING,
                    None,
                    None,
                    self.generation,
                    now,
                    now,
                ),
            )
            if cursor.rowcount > 0:
                return RequestBegin(created=True, status=REQUEST_STATUS_PENDING)
            row = conn.execute(
                f"SELECT status FROM request_dispositions WHERE {_KEY_WHERE}",
                key,
            ).fetchone()
            return RequestBegin(created=False, status=row[0])

    def claim_processing(self, key) -> bool:
        """原子占用：仅 pending 可转 processing，并发只有一个 True。

        占用时同步把行代次打成本实例代次、并刷新本 boot 心跳：
        行归属本 boot 后，只要本 boot 心跳新鲜，后继构造的失联
        清扫不会误及活行；若本 boot 写静默达到失联阈值而被误判
        已死，其 processing 行仍可能被扫成 ambiguous——方向恒
        为 fail-closed（搁浅待对账、不释放重做），拓扑边界见
        模块 docstring（复审 P4：与现行清扫口径对齐，旧「代次
        小于当前代次」谓词已被 boot 失联判定取代）。

        若本代次已被剪除（归属行随已决前缀删除、水位线已越
        过本代次），占用时同步恢复本代次的归属行：苏醒 boot
        仍以旧代次 claim 的行，其归属若不恢复，补扫只能按
        「归属未知」恒判失联——心跳再新鲜（刚 claim）也会在
        下一次对端清扫时被扫成 ambiguous，120 秒失联窗口对
        苏醒行完全失效（复审 P3）。重注册的只是归属行本身，
        代次号与归属 boot 都不变（取舍见 _sweep_stale_locked）。
        """
        key = self._normalize(key)
        with self.db.transaction() as conn:
            self._touch_boot_seen(conn, self.clock())
            watermark_row = conn.execute(
                "SELECT value FROM schema_meta"
                " WHERE key='request_sweep_watermark'"
            ).fetchone()
            if self.generation <= _meta_int(watermark_row):
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta VALUES (?, ?)",
                    (f"request_gen_boot:{self.generation}", _PROCESS_BOOT_ID),
                )
            cursor = conn.execute(
                "UPDATE request_dispositions SET status=?, updated_at=?, generation=? "
                f"WHERE {_KEY_WHERE} AND status=?",
                (
                    REQUEST_STATUS_PROCESSING,
                    self.clock(),
                    self.generation,
                    *key,
                    REQUEST_STATUS_PENDING,
                ),
            )
            return cursor.rowcount > 0

    def complete(self, key, *, status, decision=None, result_code=None) -> bool:
        """落终态：仅 processing 可完成，其余状态 / 不存在一律 False。"""
        if status not in REQUEST_TERMINAL_STATES:
            raise ValueError(f"complete status must be a terminal state, got {status!r}")
        key = self._normalize(key)
        with self.db.transaction() as conn:
            self._touch_boot_seen(conn, self.clock())
            cursor = conn.execute(
                "UPDATE request_dispositions"
                " SET status=?, decision=?, result_code=?, updated_at=? "
                f"WHERE {_KEY_WHERE} AND status=?",
                (
                    status,
                    decision,
                    result_code,
                    self.clock(),
                    *key,
                    REQUEST_STATUS_PROCESSING,
                ),
            )
            return cursor.rowcount > 0

    def get(self, key) -> RequestRecord | None:
        key = self._normalize(key)
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM request_dispositions WHERE {_KEY_WHERE}",
                key,
            ).fetchone()
        if row is None:
            return None
        return self._record(row)

    def list_pending(self, limit: int = 100) -> list[RequestRecord]:
        """pending 清扫面：按 created_at 升序（先到先处置）。

        入口顺带刷新本 boot 心跳并惰性重跑失联清扫（启动清扫
        正是经此路径：崩溃 boot 的遗留 processing 在其心跳
        过期后于此转 ambiguous，复审 P2）。
        """
        with self.db.transaction() as conn:
            now = self.clock()
            self._touch_boot_seen(conn, now)
            self._sweep_stale_locked(conn, now)
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM request_dispositions"
                " WHERE status=? ORDER BY created_at ASC LIMIT ?",
                (REQUEST_STATUS_PENDING, limit),
            ).fetchall()
        return [self._record(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self.db.transaction() as conn:
            return {
                status: count
                for status, count in conn.execute(
                    "SELECT status, COUNT(*) FROM request_dispositions"
                    " GROUP BY status"
                ).fetchall()
            }
