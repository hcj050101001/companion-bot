"""投递台账（P5）：delivery_turns 是投递状态的唯一事实源。

状态机与围栏规则逐条照搬旧 ShortTermMemory.update_delivery；
accepted_text 始终只存已接受前缀，未接受文本永不入库。
prepare / update 均为单一事务：台账行写完、历史投影未写时调用
Database.fault_hook("after_ledger_write")，钩子抛异常则整事务回滚。
全部方法 async，内部用 asyncio.to_thread 调同步事务核心。
"""

import asyncio
import logging

from companion.memory.database import Database
from companion.multipart import MAX_BUBBLES, MAX_ONE_BY_ONE_BUBBLES

logger = logging.getLogger("companion.ledger")

# A delivery turn must hold the largest plan any planner can emit: the
# structured path caps at MAX_BUBBLES bubbles, while an explicit one-by-one
# plain-text answer may keep up to MAX_ONE_BY_ONE_BUBBLES separate lines.
# Derived from the splitter's constants so the two caps can never drift
# apart; max() keeps the invariant structural even if either constant
# changes (ported from production round 7, commit 09b621c, at cutover).
MAX_TURN_BUBBLES = max(MAX_BUBBLES, MAX_ONE_BY_ONE_BUBBLES)

_VALID_STATES = {"in_flight", "partial", "complete", "ambiguous", "failed"}
_ALLOWED = {
    "prepared": {"in_flight", "failed"},
    "in_flight": {"partial", "complete", "ambiguous", "failed"},
    "partial": {"in_flight"},
}


class DeliveryLedger:
    def __init__(self, db: Database):
        self.db = db

    # -- 同步核心 ---------------------------------------------------------

    @staticmethod
    def _delivery_current(db, user_id: str, pack_generation: int, delivery_generation: int) -> bool:
        pack = db.execute(
            "SELECT generation FROM pack_preferences WHERE user_id=?", (str(user_id),)
        ).fetchone()
        delivery = db.execute(
            "SELECT generation FROM delivery_generations WHERE user_id=?", (str(user_id),)
        ).fetchone()
        return (pack[0] if pack else 0) == pack_generation and (
            delivery[0] if delivery else 0
        ) == delivery_generation

    def _delivery_generation_sync(self, user_id: str) -> int:
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT generation FROM delivery_generations WHERE user_id=?",
                (str(user_id),),
            ).fetchone()
        return row[0] if row else 0

    def _advance_sync(self, user_id: str) -> int:
        with self.db.transaction() as db:
            db.execute(
                """INSERT INTO delivery_generations(user_id, generation)
                VALUES (?, 1) ON CONFLICT(user_id) DO UPDATE SET
                generation=generation+1""",
                (str(user_id),),
            )
            return db.execute(
                "SELECT generation FROM delivery_generations WHERE user_id=?",
                (str(user_id),),
            ).fetchone()[0]

    def _prepare_sync(
        self,
        user_id: str,
        turn_id: str,
        user_text: str,
        count: int,
        pack_generation: int,
        delivery_generation: int,
    ) -> bool:
        if not turn_id or not 1 <= count <= MAX_TURN_BUBBLES:
            raise ValueError("invalid turn or bubble count")
        with self.db.transaction() as db:
            if not self._delivery_current(db, user_id, pack_generation, delivery_generation):
                return False
            if db.execute(
                "SELECT 1 FROM delivery_turns WHERE user_id=? AND turn_id=?",
                (str(user_id), turn_id),
            ).fetchone():
                return False
            db.execute(
                "INSERT INTO delivery_turns"
                "(user_id, turn_id, state, unsent_count, accepted_text,"
                " pack_generation, delivery_generation) VALUES (?,?, 'prepared', ?, '', ?, ?)",
                (str(user_id), turn_id, count, pack_generation, delivery_generation),
            )
            # 台账行已写、历史投影（user 行）未写：故障注入点。
            self.db.call_fault_hook("after_ledger_write")
            db.execute(
                "INSERT INTO messages"
                "(user_id, role, content, turn_id, delivery_state, unsent_count)"
                " VALUES (?, 'user', ?, ?, 'prepared', ?)",
                (str(user_id), user_text, turn_id, count),
            )
        return True

    def _update_sync(
        self,
        user_id: str,
        turn_id: str,
        state: str,
        unsent_count: int,
        pack_generation: int,
        delivery_generation: int,
        accepted_bubble: str | None = None,
    ) -> bool:
        if state not in _VALID_STATES:
            raise ValueError("invalid delivery state")
        if not 0 <= unsent_count <= MAX_TURN_BUBBLES:
            raise ValueError("invalid unsent count")
        if accepted_bubble is not None and (
            state not in {"partial", "complete"} or not accepted_bubble.strip()
        ):
            raise ValueError("only accepted text may be stored")
        with self.db.transaction() as db:
            if not self._delivery_current(db, user_id, pack_generation, delivery_generation):
                return False
            row = db.execute(
                "SELECT state, unsent_count, accepted_text FROM delivery_turns "
                "WHERE user_id=? AND turn_id=?",
                (str(user_id), turn_id),
            ).fetchone()
            if not row:
                return False
            current_state, current_unsent, accepted_text = row
            if state not in _ALLOWED.get(current_state, set()):
                return False
            if state == "in_flight":
                if accepted_bubble is not None or unsent_count != current_unsent - 1:
                    return False
            elif state in {"partial", "complete"}:
                if (
                    accepted_bubble is None
                    or unsent_count != current_unsent
                    or (state == "complete") != (unsent_count == 0)
                ):
                    return False
            elif accepted_bubble is not None or unsent_count != current_unsent:
                return False
            new_accepted = accepted_text
            if accepted_bubble is not None:
                new_accepted = "\n".join(filter(None, (accepted_text, accepted_bubble)))
            db.execute(
                "UPDATE delivery_turns SET state=?, unsent_count=?, accepted_text=? "
                "WHERE user_id=? AND turn_id=?",
                (state, unsent_count, new_accepted, str(user_id), turn_id),
            )
            # 台账行已写、历史投影未写：故障注入点。
            self.db.call_fault_hook("after_ledger_write")
            # 历史投影：只物化已接受前缀。
            if state in {"partial", "complete"} and accepted_bubble is not None:
                existing = db.execute(
                    "SELECT id FROM messages WHERE user_id=? AND turn_id=? "
                    "AND role='assistant'",
                    (str(user_id), turn_id),
                ).fetchone()
                if existing is None:
                    db.execute(
                        "INSERT INTO messages"
                        "(user_id, role, content, turn_id, delivery_state, unsent_count)"
                        " VALUES (?, 'assistant', ?, ?, ?, ?)",
                        (str(user_id), new_accepted, turn_id, state, unsent_count),
                    )
                else:
                    db.execute(
                        "UPDATE messages SET content=?, delivery_state=?, unsent_count=? "
                        "WHERE id=?",
                        (new_accepted, state, unsent_count, existing[0]),
                    )
            elif state in {"in_flight", "ambiguous", "failed"}:
                # in_flight 同样撤投影：多气泡下一泡在途期间，上一泡已
                # 物化的前缀不得继续可见（与启动恢复口径一致——残留
                # in_flight 转 ambiguous 时一并删投影）。prepared →
                # in_flight 时本无 assistant 行，删除为安全 no-op；
                # accepted_text 仍留在台账，随后 partial/complete 走
                # 上方 existing 为 None 的 INSERT 路径按序恢复全前缀。
                db.execute(
                    "DELETE FROM messages WHERE user_id=? AND turn_id=? "
                    "AND role='assistant'",
                    (str(user_id), turn_id),
                )
        return True

    def _turn_read_sync(self, user_id: str, turn_id: str):
        with self.db.transaction() as db:
            return db.execute(
                "SELECT state, accepted_text FROM delivery_turns "
                "WHERE user_id=? AND turn_id=?",
                (str(user_id), turn_id),
            ).fetchone()

    # -- 异步公开 API -------------------------------------------------------

    async def turn_had_send_with_state(
        self, user_id: str, turn_id: str
    ) -> tuple[bool | None, str | None]:
        """读回某回合的投递事实与原始 state（单一读回、一次落定）。

        返回 ``(had_send, state)``：``state`` 为台账原始状态，无行
        或读异常时为 None；``had_send`` 三态——state 为
        complete/partial/ambiguous 或已有 accepted_text 时 True
        （ambiguous 表示平台可能已接受、按可能已送达保守判定）；
        failed/无行等其余情形 False；读异常时 None（无从判定，
        调用方须按「可能有发出」保守处理）。读异常只记异常类型名，
        不记正文/标识。
        """
        try:
            row = await asyncio.to_thread(self._turn_read_sync, user_id, turn_id)
        except Exception as exc:  # noqa: BLE001 — 读异常收敛为 None 三态
            logger.warning("delivery readback failed: %s", type(exc).__name__)
            return None, None
        if row is None:
            return False, None
        state, accepted_text = row
        had = state in ("complete", "partial", "ambiguous") or bool(accepted_text)
        return had, state

    def _mark_ambiguous_sync(self, user_id: str, turn_id: str) -> bool:
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT state FROM delivery_turns WHERE user_id=? AND turn_id=?",
                (str(user_id), turn_id),
            ).fetchone()
            if not row or row[0] != "in_flight":
                return False
            db.execute(
                "UPDATE delivery_turns SET state='ambiguous' "
                "WHERE user_id=? AND turn_id=?",
                (str(user_id), turn_id),
            )
            # 与 _update_sync 的 ambiguous 口径一致：assistant 投影
            # 不物化（in_flight 时本已删除，此处为兜底 no-op）。
            db.execute(
                "DELETE FROM messages WHERE user_id=? AND turn_id=? "
                "AND role='assistant'",
                (str(user_id), turn_id),
            )
        return True

    async def mark_turn_ambiguous(self, user_id: str, turn_id: str) -> bool:
        """把仍停 in_flight 的回合收敛为 ambiguous（投递取消收尾专用）。

        发送在途被取消时结果不明，本应由 _deliver 的取消收尾写
        ambiguous，但该写常被新消息推进的投递代次围栏拒绝、行
        停在 in_flight。本收敛与启动恢复的 in_flight→ambiguous
        同口径：不写 accepted_text、不复活任何内容，只把未定
        状态落为不明，故不走代次围栏；行不存在（忘掉已删）或
        已非 in_flight 时为 no-op 返回 False。
        """
        return await asyncio.to_thread(self._mark_ambiguous_sync, user_id, turn_id)

    async def delivery_generation(self, user_id: str) -> int:
        return await asyncio.to_thread(self._delivery_generation_sync, user_id)

    async def advance_delivery_generation(self, user_id: str) -> int:
        return await asyncio.to_thread(self._advance_sync, user_id)

    async def prepare_delivery(
        self,
        user_id: str,
        turn_id: str,
        user_text: str,
        count: int,
        pack_generation: int,
        delivery_generation: int,
    ) -> bool:
        return await asyncio.to_thread(
            self._prepare_sync,
            user_id,
            turn_id,
            user_text,
            count,
            pack_generation,
            delivery_generation,
        )

    async def update_delivery(
        self,
        user_id: str,
        turn_id: str,
        state: str,
        unsent_count: int,
        pack_generation: int,
        delivery_generation: int,
        accepted_bubble: str | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._update_sync,
            user_id,
            turn_id,
            state,
            unsent_count,
            pack_generation,
            delivery_generation,
            accepted_bubble,
        )
