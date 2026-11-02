"""Send-time safety boundary for future opt-in reminders; no scheduling loop."""

import inspect
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from companion.memory.care import CarePreferences, _utc
from companion.quota import SendQuota
from companion.safety import SafetyGateway
from companion.services.delivery import DeliveryService

SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_WORK_AGE = timedelta(hours=24)
# Fixed, non-personal copy; this is a reminder, not a crisis response or monitoring.
REMINDER = "这是你选择开启的一条关怀提醒。想聊聊的话可以给我发消息。"


class CareSendGate:
    """Fail closed unless all current authorization and safety checks pass.

    The caller supplies current whitelist and policy checks, including whether a crisis
    is pending, and a revision that changes whenever safety policy changes. The same
    OneBot client/gateway as ordinary sends enforces its send rate.
    This gate never initiates sends on its own; the wired CareScheduler
    invokes it when a queued care item comes due. There is currently no
    production producer enqueueing items (trigger parameters are the
    separate D2 stage), so in production the gate stays idle.
    """

    def __init__(
        self,
        preferences: CarePreferences,
        delivery,
        *,
        whitelist,
        current_safety,
        config=None,
        clock=None,
    ):
        # 发送端解耦（方案 §5.2）：接受 DeliveryService，或任意带
        # send_private_msg 的 sender（内部包装为 DeliveryService，额度经
        # sender 自带 quota 或新建 SendQuota），不再硬依赖 HttpOneBotClient。
        # 注意：无 quota 的 sender 会得到隔离记账——生产接线（P6）必须
        # 传入与回复路径共享的 DeliveryService，不得以裸 sender 包装，
        # 否则关怀发送不占全账号共享额度（P5 Review #8）。
        if isinstance(delivery, DeliveryService):
            self.delivery = delivery
        else:
            self.delivery = DeliveryService(
                delivery, getattr(delivery, "quota", None) or SendQuota()
            )
        self.preferences = preferences
        self.sender = delivery
        self.whitelist = whitelist
        self.current_safety = current_safety
        self.config = config if config is not None else {}
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _enabled(self) -> bool:
        care = self.config.get("proactive_care") if isinstance(self.config, dict) else None
        return isinstance(care, dict) and care.get("enabled") is True

    async def _fence_allows(self, fence) -> bool:
        """求值 fence 回调（同步/异步均可）：无 fence 放行，异常或假值拒绝。"""
        if fence is None:
            return True
        try:
            result = fence()
            if inspect.isawaitable(result):
                result = await result
        except Exception:  # noqa: BLE001 — fence 故障按不通过（fail closed）
            return False
        return bool(result)

    async def send(
        self, user_id: str, work_id: str, created_at: datetime, *, fence=None
    ) -> bool:
        """发送一条关怀提醒；任何一道检查不过即返回 False（未发送形态）。

        fence（E 路修复 3）：可选回调，detect 返回后、reserve 之前
        求值，返回假即放弃本次发送——调用方（CareScheduler）借此在
        detect 的 await 窗口内发生的撤回/换代生效；同一 fence 还会
        经槽后 preflight（E 路修复 4）在传输前再求值一次。
        """
        if not self._enabled() or not isinstance(work_id, str) or not 1 <= len(work_id) <= 128:
            return False
        try:
            now = _utc(self.clock())
            created_at = _utc(created_at)
            local = now.astimezone(SHANGHAI)
            if (
                not created_at <= now < created_at + MAX_WORK_AGE
                or local.hour < 8
                or local.hour >= 23
            ):
                return False
            if not self.whitelist(str(user_id)):
                return False
            # Provider must supply the current gateway and an affirmative policy decision.
            gateway, allowed, revision = self.current_safety(str(user_id))
            if allowed is not True or not isinstance(gateway, SafetyGateway):
                return False
            crisis, _ = await gateway.detect(REMINDER)
            if crisis:
                return False
            # Detection may await: re-read every mutable condition immediately before reservation.
            now = _utc(self.clock())
            local = now.astimezone(SHANGHAI)
            gateway_now, allowed, revision_now = self.current_safety(str(user_id))
            if (
                not self._enabled()
                or not created_at <= now < created_at + MAX_WORK_AGE
                or local.hour < 8
                or local.hour >= 23
                or not self.whitelist(str(user_id))
                or gateway_now is not gateway
                or revision_now != revision
                or allowed is not True
            ):
                return False
            # fence 求值（E 路修复 3）：detect 的 await 期间调用方状态
            # 可能已变（待发项被撤回、投递代次被新消息推进）；reserve
            # 之前最后一道外部复核，假即放弃且不消费 reserve。
            if not await self._fence_allows(fence):
                return False
            if not self.preferences.reserve(
                str(user_id), work_id, local.date().isoformat(), now=now
            ):
                return False
            # The shared OneBot gateway rechecks its current enable/dry-run/rate policy.
            # 接受口径按 §5.2 回执三态与回复路径统一（dry_run 在网关层即视为
            # accepted，与 _send_once 现行口径一致）。
            # 槽后 preflight（E 路修复 4）：取得发送并发槽后、发起传输
            # 前由网关再求值——等待槽期间被删除的同意不得在槽释放后
            # 被旧关怀钻空发送。同意只查 consent_state、不重复 reserve
            # （本条的 reserve 已在上面完成，预占语义不变）。
            async def _preflight() -> bool:
                if not self._enabled():
                    return False
                try:
                    state = self.preferences.consent_state(
                        str(user_id), now=_utc(self.clock())
                    )
                except Exception:  # noqa: BLE001 — 同意查不到按未同意
                    return False
                if state != "active":
                    return False
                return await self._fence_allows(fence)

            receipt = await self.delivery.send(
                str(user_id), REMINDER, preflight=_preflight
            )
            return receipt.outcome == "accepted"
        except Exception:
            # Ambiguous sends retain the reservation; never retry automatically or log content.
            return False
