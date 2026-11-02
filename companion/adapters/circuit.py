"""ActionCircuitBreaker：按动作的熔断器（方案 C18 补强，N5）。

供 ActionGateway 集成（本模块只交付类本身）：同一动作连续失败达
阈值即 open，冷却期内拒绝派发；冷却已过进入 half-open，只放行
一次试探——试探成功（``record_success``）关闭并清零，试探再失败
立即 reopen 并刷新冷却起点。

计数口径：只统计「派发后失败」。是否派发由调用方以 ``allow``
把关，本类不区分失败来源、不看异常内容。单事件循环假设：内部
用普通 dict、无锁（与 Gateway 账本的进程内实现同口径）。
"""

import time

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"


class _ActionRecord:
    __slots__ = ("failures", "opened_at", "probing", "probe_gen")

    def __init__(self):
        self.failures = 0
        self.opened_at: float | None = None
        self.probing = False
        #: 试探世代号：每次 allow() 发放试探自增。结果记账携带
        #: 世代号时，只许与当前世代匹配者结清试探——迟到结果（在
        #: 电路打开前派发、试探在飞时才返回）不得关/刷新他人的
        #: 试探（Review P3-4）。
        self.probe_gen = 0


class ActionCircuitBreaker:
    """按 action 名独立计数的熔断器。

    构造参数：``threshold`` 连续失败打开阈值（默认 3）；
    ``cooldown_seconds`` open 后冷却秒数（默认 300）；
    ``clock`` 时钟（默认 ``time.monotonic``，与 cooldown 同口径）。
    """

    def __init__(self, *, threshold=3, cooldown_seconds=300, clock=time.monotonic):
        if not isinstance(threshold, int) or threshold < 1:
            raise ValueError("threshold must be a positive int")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative")
        self.threshold = threshold
        self.cooldown_seconds = float(cooldown_seconds)
        self.clock = clock
        self._records: dict[str, _ActionRecord] = {}

    def _record(self, action) -> _ActionRecord:
        return self._records.setdefault(str(action), _ActionRecord())

    def _cooldown_elapsed(self, record: _ActionRecord) -> bool:
        return (
            record.opened_at is not None
            and (self.clock() - record.opened_at) >= self.cooldown_seconds
        )

    def state(self, action) -> str:
        """返回 "closed" / "open" / "half_open"。

        open 且冷却已过（或试探进行中）即 half_open——即使尚未经
        ``allow`` 发起试探，状态也按可试探报告。
        """
        record = self._records.get(str(action))
        if record is None or record.opened_at is None:
            return STATE_CLOSED
        if record.probing or self._cooldown_elapsed(record):
            return STATE_HALF_OPEN
        return STATE_OPEN

    def allow(self, action) -> bool:
        """是否放行一次派发。half-open 只放行一次试探。

        closed 恒放行；open 且冷却未过拒绝；open 且冷却已过放行
        一次试探并标记 probing，试探结论（success/failure）落账
        前后续 ``allow`` 一律拒绝。
        """
        record = self._record(action)
        if record.opened_at is None:
            return True
        if record.probing:
            return False
        if self._cooldown_elapsed(record):
            record.probing = True
            record.probe_gen += 1
            return True
        return False

    def probe_generation(self, action) -> int | None:
        """当前在飞试探的世代号；无试探返回 None（调用方记账用）。"""
        record = self._records.get(str(action))
        if record is None or not record.probing:
            return None
        return record.probe_gen

    def abandon_probe(self, action, *, probe_gen=None) -> None:
        """放弃在飞试探（派发被取消/超时、无结果可记账时调用）。

        试探按失败结清：清 probing 并刷新冷却起点——否则 probing
        永久悬挂、动作被熔断死锁（Review P1-1）。世代不匹配（试探
        已被他人结清）时不动。
        """
        record = self._record(action)
        if not record.probing:
            return
        if probe_gen is not None and probe_gen != record.probe_gen:
            return
        record.probing = False
        record.opened_at = self.clock()
        record.failures = self.threshold

    @staticmethod
    def _stale_probe(record: _ActionRecord, probe_gen) -> bool:
        """记账世代与在飞试探不匹配 → 该结果是迟到结果，不动试探。"""
        return (
            record.probing
            and probe_gen is not None
            and probe_gen != record.probe_gen
        )

    def record_failure(self, action, *, probe_gen=None) -> None:
        """记一次派发后失败：closed 累计达阈值打开；试探失败立即 reopen。

        已 open 且非试探中的失败不刷新冷却起点（调用方本不应在
        open 期派发，此类迟到失败若刷新 opened_at 会无限推迟恢复）。
        携带世代号且与在飞试探不匹配的迟到失败直接忽略（P3-4）。
        """
        record = self._record(action)
        if self._stale_probe(record, probe_gen):
            return
        if record.opened_at is not None:
            if record.probing:
                record.probing = False
                record.opened_at = self.clock()
                record.failures = self.threshold
            return
        record.failures += 1
        if record.failures >= self.threshold:
            record.opened_at = self.clock()

    def record_success(self, action, *, probe_gen=None) -> None:
        """记一次成功：清零连续失败并关闭（含试探成功）。

        携带世代号且与在飞试探不匹配的迟到成功忽略——不得在
        试探仍在飞时提前关闭电路、放出第二个「试探」（P3-4）。
        """
        record = self._record(action)
        if self._stale_probe(record, probe_gen):
            return
        record.failures = 0
        record.opened_at = None
        record.probing = False

    def reset(self, action) -> None:
        """手动复位一个动作（等同从未失败过）。"""
        self._records.pop(str(action), None)
