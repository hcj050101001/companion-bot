"""SQLite 存储核心：连接、WAL、schema 版本与 v0→v1 迁移（P5）。

本模块只负责打开数据库并保证打开后 schema 处于 v1，不包含任何
对话 / 投递 / claim 的业务语义（分别在 conversation / ledger / claims）。

v1 布局：
- ``messages`` 只存历史投影：assistant 行只在投递 partial/complete 时
  物化，其余回合的助手内容只存 ``delivery_turns.accepted_text``。
- ``delivery_turns`` 是投递台账，一回合一行。
- ``claims`` 是消息 claim 的落库表（跨重启去重）。
- ``anniversaries`` 是主人对话式维护的纪念日（倒计时增强），
  与 config 静态清单分工、合并口径见 memory.anniversaries。
- ``vision_observation_index`` 是识图观察行的图像同一性索引
  （turn_id → 图片内容哈希），供危机补偿删除跨上传定位同图行。
- ``schema_meta`` 存 schema_version 与 claim 的代次 / 豁免计数。
"""

import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

# v1 全部表（全新库直接按此建）。messages 保留 turn_id /
# delivery_state / unsent_count 列：迁移库本来就有，且快照回滚后
# 旧口径 reader 仍能按 v0 schema 读 messages。
_CREATE_V1 = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    turn_id TEXT,
    delivery_state TEXT DEFAULT 'legacy_unknown',
    unsent_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_user_id_id ON messages(user_id, id);
CREATE TABLE IF NOT EXISTS user_state (
    user_id TEXT PRIMARY KEY,
    onboarded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pack_preferences (
    user_id TEXT PRIMARY KEY,
    pack_id TEXT,
    generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS delivery_generations (
    user_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_turns (
    user_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    state TEXT NOT NULL,
    unsent_count INTEGER NOT NULL,
    accepted_text TEXT NOT NULL DEFAULT '',
    pack_generation INTEGER NOT NULL,
    delivery_generation INTEGER NOT NULL,
    PRIMARY KEY (user_id, turn_id)
);
CREATE TABLE IF NOT EXISTS claims (
    platform TEXT NOT NULL,
    self_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    lane TEXT NOT NULL,
    partition_name TEXT NOT NULL,
    state TEXT NOT NULL,
    generation INTEGER NOT NULL,
    created_at REAL NOT NULL,
    finished_at REAL,
    PRIMARY KEY (platform, self_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_claims_partition_state
    ON claims(partition_name, state, finished_at);
CREATE INDEX IF NOT EXISTS idx_delivery_turns_state
    ON delivery_turns(state);
CREATE TABLE IF NOT EXISTS anniversaries (
    normalized_name TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    month INTEGER,
    day INTEGER,
    year INTEGER,
    date TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS vision_observation_index (
    user_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    image_hash TEXT NOT NULL,
    PRIMARY KEY (user_id, turn_id)
);
CREATE INDEX IF NOT EXISTS idx_vision_observation_index_hash
    ON vision_observation_index(user_id, image_hash);
"""


class Database:
    """串行化的 SQLite 连接与事务边界。

    单连接 + 内部锁：所有事务经 :meth:`transaction` 以
    ``BEGIN IMMEDIATE`` 串行执行，避免多写者交错。
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fault_hook: Callable[[str], None] | None = None
        self._lock = threading.Lock()
        # isolation_level=None：手动控制 BEGIN/COMMIT/ROLLBACK。
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._initialise()

    # -- 事务 -------------------------------------------------------------

    @contextmanager
    def transaction(self):
        """BEGIN IMMEDIATE → yield connection → COMMIT；异常 ROLLBACK。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def call_fault_hook(self, point: str) -> None:
        """在事务内的指定点调用故障注入钩子（若设置）。"""
        if self.fault_hook is not None:
            self.fault_hook(point)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 初始化与迁移 -------------------------------------------------------

    @staticmethod
    def _exec_script(db, script: str) -> None:
        # 不用 executescript：它会隐式 COMMIT、打断外层手动事务。
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                db.execute(statement)

    def _initialise(self) -> None:
        with self.transaction() as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "schema_meta" not in tables and "messages" in tables:
                # 无 schema_meta 且已有旧表 → v0 库，迁移到 v1。
                self._migrate_v0_to_v1(db)
            else:
                # 全新库（或已是 v1）：按 v1 建全部表并补 meta 默认值。
                self._exec_script(db, _CREATE_V1)
                stored = self._stored_schema_version(db)
                if stored is not None and stored > SCHEMA_VERSION:
                    # 比本代码更新的库绝不静默按 v1 打开（P5 Review #3）。
                    raise RuntimeError(
                        f"database schema_version {stored} is newer than "
                        f"supported {SCHEMA_VERSION}"
                    )
                db.execute(
                    "INSERT OR REPLACE INTO schema_meta VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                db.execute(
                    "INSERT OR IGNORE INTO schema_meta VALUES ('claim_generation', '0')"
                )
                db.execute(
                    "INSERT OR IGNORE INTO schema_meta VALUES ('claim_exempted', '0')"
                )
                # 自愈扫描：若曾有旧代码在 v1 库上按 v0 口径写过
                # messages（仅换代码未恢复快照的回滚再升级），其回合
                # 不在台账内——按迁移同口径补填并清不可见行（对健康
                # v1 库为空操作）。
                self._backfill_turns(db)
            self._startup_recovery(db)

    @staticmethod
    def _stored_schema_version(db) -> int | None:
        try:
            row = db.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
        except Exception:  # noqa: BLE001 — 无表/无行都按未知处理
            return None
        if row is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    def _migrate_v0_to_v1(self, db) -> None:
        """v0 → v1：additive 建表 + 回填台账 + 删除不可见 assistant 行。

        不得改 messages 既有列；只按旧 ShortTermMemory 的方式补齐
        缺失的 turn_id / delivery_state / unsent_count 列。
        """
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        for name, definition in (
            ("turn_id", "TEXT"),
            ("delivery_state", "TEXT DEFAULT 'legacy_unknown'"),
            ("unsent_count", "INTEGER"),
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
        self._exec_script(db, _CREATE_V1)
        self._backfill_turns(db)
        db.execute(
            "INSERT OR REPLACE INTO schema_meta VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        db.execute(
            "INSERT OR IGNORE INTO schema_meta VALUES ('claim_generation', '0')"
        )
        db.execute(
            "INSERT OR IGNORE INTO schema_meta VALUES ('claim_exempted', '0')"
        )

    @staticmethod
    def _backfill_turns(db) -> None:
        """把 messages 中尚不在台账的回合按 v0 口径回填 delivery_turns。

        两个 generation 取回填时该用户当前的 pack_preferences /
        delivery_generations 值（无行按 0）——回填只为让在途回合在
        新口径下可判定，写实际值即可。INSERT OR IGNORE + 删除条件
        都幂等：迁移时全量执行，日常打开时只对缺失回合生效（自愈
        扫描，无缺失即空操作）。
        """
        groups = db.execute(
            "SELECT user_id, turn_id FROM messages WHERE turn_id IS NOT NULL "
            "GROUP BY user_id, turn_id"
        ).fetchall()
        for user_id, turn_id in groups:
            rows = db.execute(
                "SELECT role, content, delivery_state, unsent_count FROM messages "
                "WHERE user_id=? AND turn_id=? ORDER BY id",
                (user_id, turn_id),
            ).fetchall()
            assistant = next((r for r in rows if r[0] == "assistant"), None)
            primary = assistant if assistant is not None else rows[0]
            state = primary[2] if primary[2] is not None else "prepared"
            if state == "in_flight":
                # 与现行启动规则一致：在途一律转 ambiguous。
                state = "ambiguous"
            elif state not in {"prepared", "partial", "complete", "ambiguous", "failed"}:
                # 未知/旧默认状态（如 legacy_unknown 带上了 turn_id）：
                # 保守按 ambiguous 回填——前缀不进历史、不可再更新。
                state = "ambiguous"
            unsent = primary[3] if primary[3] is not None else 0
            accepted = assistant[1] if assistant is not None else ""
            pack_row = db.execute(
                "SELECT generation FROM pack_preferences WHERE user_id=?",
                (user_id,),
            ).fetchone()
            delivery_row = db.execute(
                "SELECT generation FROM delivery_generations WHERE user_id=?",
                (user_id,),
            ).fetchone()
            db.execute(
                "INSERT OR IGNORE INTO delivery_turns"
                "(user_id, turn_id, state, unsent_count, accepted_text,"
                " pack_generation, delivery_generation) VALUES (?,?,?,?,?,?,?)",
                (
                    user_id,
                    turn_id,
                    state,
                    unsent,
                    accepted,
                    pack_row[0] if pack_row else 0,
                    delivery_row[0] if delivery_row else 0,
                ),
            )

        # 这些回合的助手内容本就不进历史（旧 get_history 亦过滤），
        # 删除其 assistant 投影行；partial/complete 的行保留。user 行全留。
        db.execute(
            "DELETE FROM messages WHERE role='assistant' AND turn_id IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM delivery_turns "
            "WHERE delivery_turns.user_id = messages.user_id "
            "AND delivery_turns.turn_id = messages.turn_id "
            "AND delivery_turns.state IN ('prepared', 'ambiguous', 'failed'))"
        )

    @staticmethod
    def _startup_recovery(db) -> None:
        """启动规则：残留 in_flight 一律转 ambiguous（与旧 UPDATE 等价）。

        旧口径下 ambiguous 的 assistant 行被 get_history 过滤；新口径
        messages 只存投影，故转 ambiguous 时一并删除该回合的 assistant
        投影行（accepted_text 仍保留在台账中），保持历史可见性等价。
        """
        in_flight = db.execute(
            "SELECT user_id, turn_id FROM delivery_turns WHERE state='in_flight'"
        ).fetchall()
        if in_flight:
            db.execute(
                "UPDATE delivery_turns SET state='ambiguous' WHERE state='in_flight'"
            )
            for user_id, turn_id in in_flight:
                db.execute(
                    "DELETE FROM messages WHERE user_id=? AND turn_id=? "
                    "AND role='assistant'",
                    (user_id, turn_id),
                )
        # 台账保留口径（P5 Review #10）：终态回合若其历史行已被裁剪/
        # 删除（messages 中无任何一行），台账行不再有投影或围栏价值，
        # 打开时修剪，避免 delivery_turns 无界增长。仍被历史引用的
        # 终态回合保留（迟到写靠状态机拒绝，不依赖删除）。
        db.execute(
            "DELETE FROM delivery_turns WHERE state IN ('complete', 'failed', 'ambiguous') "
            "AND NOT EXISTS (SELECT 1 FROM messages "
            "WHERE messages.user_id = delivery_turns.user_id "
            "AND messages.turn_id = delivery_turns.turn_id)"
        )
        # 同一性索引对账（复审 r5 P3 的启动路径同口径核查）：
        # 索引不独立存在——无对应 vision 观察行的索引条目一律
        # 清掉。运行时裁剪/删除已在同事务内对账（conversation
        # 的 _trim_history_window 等），此处兜底历史遗留与异常
        # 中断留下的孤儿，保证重启后同一性判定基于真实行集。
        db.execute(
            "DELETE FROM vision_observation_index WHERE NOT EXISTS "
            "(SELECT 1 FROM messages "
            "WHERE messages.user_id = vision_observation_index.user_id "
            "AND messages.turn_id = vision_observation_index.turn_id "
            "AND messages.role='vision')"
        )
