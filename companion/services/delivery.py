"""DeliveryService：投递契约独占（方案 §5.2）。

额度与并发的唯一记账方：预留经 SendQuota，传输经注入的 sender，
适配器/客户端不另行计费。本服务不记正文；发送失败只记异常类型名
（不记异常原文，防正文/凭证混入日志，与 ingress 旧口径一致）；
发送异常或结果不明一律 ambiguous，绝不猜 accepted、绝不重试。
"""

import asyncio
import inspect
import logging

from companion.adapters.base import GroupTarget, PrivateTarget, SendReceipt, TextSegment
from companion.adapters.transport import receipt_from_result
from companion.services.segments import (
    contains_media_segment,
    plan_segment_sends,
    review_outbound_segments,
)

logger = logging.getLogger("companion.delivery")


def _sender_accepts(sender, method: str, keyword: str) -> bool:
    """sender 的发送方法是否接受指定关键字（形参命中或带 **kwargs）。"""
    try:
        params = inspect.signature(getattr(sender, method)).parameters
    except (TypeError, ValueError, AttributeError):
        return False
    return keyword in params or any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
    )


async def _preflight_allows(preflight) -> bool:
    """求值 preflight（同步/异步均可）：异常按不通过（fail closed）。"""
    try:
        result = preflight()
        if inspect.isawaitable(result):
            result = await result
    except Exception:  # noqa: BLE001 — 复查故障绝不放行传输
        return False
    return bool(result)


class DeliveryService:
    """额度与并发的唯一记账方：预留经 SendQuota，传输经注入的 sender，适配器/客户端不另行计费。"""

    def __init__(
        self,
        sender,
        quota,
        *,
        group_delivery_enabled: bool = False,
        critical_segments_enabled: bool = False,
    ):
        self.sender = sender
        self._quota = quota
        # 群发送运行时门（方案 §6 N2/N4）：默认 False；GroupTarget
        # 在预留之前 rejected（与发送额度无关的结构性关闭）。N4 起
        # 接线层可随产品开关 tools.group_chat.reply_enabled 打开本门
        # （EventProcessor 据此装配），生产默认仍关。
        self._group_delivery_enabled = group_delivery_enabled
        # 段通道的 critical 能力开关（Codex Review N2 #4）：critical
        # 不是调用方可以自声明的优先级——段通道默认不接受 critical，
        # 危机告警走冻结的 send()/send_alert() 专用路径（其 critical
        # 由入站可信代码路径设置）；本开关默认关、生产接线不开启，
        # 仅供将来 SafetyService 接线时显式启用。
        self._critical_segments_enabled = critical_segments_enabled

    @property
    def quota(self):
        return self._quota

    @quota.setter
    def quota(self, value):
        self._quota = value

    def reserve(self, user_id, count=1, *, critical=False, allow_partial_single=False):
        """quota.reserve 的 fail-closed 包装：异常一律返回 None（记账不可信即不发送）。"""
        try:
            return self._quota.reserve(
                user_id,
                count,
                critical=critical,
                allow_partial_single=allow_partial_single,
            )
        except Exception:  # noqa: BLE001 — shared accounting must fail closed
            return None

    def reserve_for(self, target, count=1, *, critical=False, allow_partial_single=False):
        """按 SendTarget 预留（C8 两级额度）：私聊走 user 域、群走 group
        域（per-group 子桶 + 账号群发子预算 + 同一账号总池）；critical
        只认私聊目标，群目标的 critical 预留直接 None（不可自声明提级）。
        fail-closed 口径同 reserve。"""
        if critical and (
            not isinstance(target, PrivateTarget)
            or not self._critical_segments_enabled
        ):
            return None
        scope = "group" if isinstance(target, GroupTarget) else "user"
        try:
            return self._quota.reserve(
                target.identity,
                count,
                critical=critical,
                allow_partial_single=allow_partial_single,
                scope=scope,
            )
        except Exception:  # noqa: BLE001 — shared accounting must fail closed
            return None

    async def send(
        self, user_id, text, *, critical=False, reservation=None, preflight=None
    ) -> SendReceipt:
        """单条发送：reservation 为 None 时先 reserve(1)（失败→rejected 不发送）；
        token = reservation.consume()（None→rejected）；仅当 sender.quota is 本服务 quota 时
        才把 token 作为 quota_token 传给 sender（避免与网关双重计费，
        也兼容无 quota 的测试 sender）；
        priority = 'critical' if critical else 'normal'；异常→ambiguous（绝不猜 accepted、不重试）；
        结果映射经 transport.receipt_from_result（全仓唯一回执映射，与
        adapters/onebot.py 同口径）：None 或 status∈{ok, async, dry_run}→accepted
        （accepted 时回填平台 message_id）；status∈{rate_limited, disabled,
        unavailable, busy}→rejected；其余/异形→ambiguous。

        preflight（E 路修复 4）：可选的发送前复查回调。sender 接受
        preflight 关键字时原样透传，由其网关在取得并发槽后、发起
        传输前求值（槽上等待期间的状态变化也能拦住）；sender 不
        接受该关键字（无槽的测试替身等）时退化为本服务在传输前
        求值。任一路径求值为假/异常 → rejected，绝不执行传输。
        """
        if reservation is None:
            reservation = self.reserve(user_id, 1, critical=critical)
            if reservation is None:
                return SendReceipt("rejected")
        token = reservation.consume()
        if token is None:
            return SendReceipt("rejected")
        kwargs = {}
        if self._quota is not None and getattr(self.sender, "quota", None) is self._quota:
            kwargs["quota_token"] = token
        if preflight is not None:
            if _sender_accepts(self.sender, "send_private_msg", "preflight"):
                kwargs["preflight"] = preflight
            elif not await _preflight_allows(preflight):
                return SendReceipt("rejected")
        priority = "critical" if critical else "normal"
        try:
            result = await self.sender.send_private_msg(
                user_id, text, priority=priority, **kwargs
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 结果不明一律 ambiguous
            logger.error("delivery transport failed: %s", type(exc).__name__)
            return SendReceipt("ambiguous")
        return receipt_from_result(result)

    async def send_to(
        self,
        target,
        segments,
        *,
        critical=False,
        reservation=None,
        preflight=None,
        allow_media=False,
    ) -> SendReceipt:
        """段级发送（SendTarget + segments，方案 §3.2/C8/C9，N2）。

        门禁顺序（全部在预留之前，任一不过 rejected 且零派发）：
        critical 只许私聊目标（群目标自声明 critical 直接拒）→ 群发
        送运行时开关（默认硬关）→ 媒体门（非文本载体默认关，须
        allow_media 显式开启）→ 出站递归复核（可见文本与纯文本同
        一套规则）。其后按段级规划拆单元（私聊文本可拆、群不拆），
        整体预留 len(单元) 个额度（reservation 传入时逐单元消费），
        逐单元经 sender 发送：sender 有 send_segments 走段通道；私聊
        纯文本单元且 sender 只有 send_private_msg 时走旧字符串通道
        （与 send() 同口径）；sender 无可用通道的单元记 rejected。

        聚合回执：全 accepted → accepted（带首个平台 id）；全
        rejected → rejected；其余（部分成功/含 ambiguous）→
        ambiguous——整条消息的投递结果不干净时不谎报任一极端，
        子单元结果按各自回执逐个记账（额度已在预留时统一扣除）。
        """
        if critical and (
            not isinstance(target, PrivateTarget)
            or not self._critical_segments_enabled
        ):
            return SendReceipt("rejected")
        if isinstance(target, GroupTarget) and not self._group_delivery_enabled:
            return SendReceipt("rejected")
        segments = tuple(segments)
        if not segments:
            return SendReceipt("rejected")
        if not allow_media and contains_media_segment(segments):
            return SendReceipt("rejected")
        allowed, _reason = review_outbound_segments(segments)
        if not allowed:
            return SendReceipt("rejected")
        units = plan_segment_sends(
            segments, split_text=isinstance(target, PrivateTarget)
        )
        if not units:
            return SendReceipt("rejected")
        if reservation is None:
            reservation = self.reserve_for(target, len(units), critical=critical)
            if reservation is None:
                return SendReceipt("rejected")
        priority = "critical" if critical else "normal"
        receipts: list[SendReceipt] = []
        for unit in units:
            token = reservation.consume()
            if token is None:
                receipts.append(SendReceipt("rejected"))
                continue
            receipts.append(
                await self._send_unit(
                    target, unit, priority=priority, token=token, preflight=preflight
                )
            )
        outcomes = {receipt.outcome for receipt in receipts}
        if outcomes == {"accepted"}:
            platform_id = next(
                (r.platform_message_id for r in receipts if r.platform_message_id),
                None,
            )
            return SendReceipt("accepted", platform_id)
        if outcomes == {"rejected"}:
            return SendReceipt("rejected")
        return SendReceipt("ambiguous")

    async def _send_unit(self, target, unit, *, priority, token, preflight) -> SendReceipt:
        """发送单个规划单元：通道选择与 send() 同口径（token 只在
        sender.quota 为本服务 quota 时透传，避免双重计费）。"""
        kwargs = {}
        if self._quota is not None and getattr(self.sender, "quota", None) is self._quota:
            kwargs["quota_token"] = token
        if hasattr(self.sender, "send_segments"):
            method = "send_segments"
            args = (target, unit)
        elif isinstance(target, PrivateTarget) and all(
            isinstance(seg, TextSegment) for seg in unit
        ) and hasattr(self.sender, "send_private_msg"):
            joined = "".join(seg.text for seg in unit)
            if "[CQ:" in joined:
                # 旧字符串通道会把正文里的 [CQ: 解析成真段（注入，
                # Codex Review #1）：段通道不可用时宁可拒发，
                # 不借字符串通道绕过段级口径。
                return SendReceipt("rejected")
            method = "send_private_msg"
            args = (target.user_id, joined)
        else:
            return SendReceipt("rejected")
        if preflight is not None:
            if _sender_accepts(self.sender, method, "preflight"):
                kwargs["preflight"] = preflight
            elif not await _preflight_allows(preflight):
                return SendReceipt("rejected")
        try:
            result = await getattr(self.sender, method)(
                *args, priority=priority, **kwargs
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 结果不明一律 ambiguous
            logger.error("delivery transport failed: %s", type(exc).__name__)
            return SendReceipt("ambiguous")
        return receipt_from_result(result)

    async def send_alert(self, user_id, text) -> bool:
        """危机私聊告警（后果性通知）：sender.send_alert_private_msg，
        结果 dict status∈{ok, async}→True，其余/异常→False（保持现行口径）。"""
        try:
            result = await self.sender.send_alert_private_msg(user_id, text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 告警失败只报 False，细节由调用方记日志
            return False
        return isinstance(result, dict) and result.get("status") in {"ok", "async"}
