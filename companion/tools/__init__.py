"""OneBot QQ 动作接口；测试可注入 mock sender。"""

import inspect
import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass

from companion.adapters.actions import ActionsBundle
from companion.adapters.base import GroupTarget, PrivateTarget
from companion.adapters.gateway import (
    PRINCIPAL_SYSTEM,
    ActionGateway,
    ActionResult,
    AuthorizationContext,
)
from companion.adapters.onebot import segments_to_onebot_message
from companion.adapters.transport import OneBotTransport, TransportError
from companion.quota import NORMAL_PER_USER_LIMIT, QuotaToken, SendQuota

QUOTA_POLICIES = frozenset({"send_private_msg", "alert_private_msg"})


@dataclass(frozen=True)
class ToolPolicy:
    dry_run: bool = True
    rate_limit: int = 6
    window_seconds: int = 60


class ToolRateLimiter:
    def __init__(self):
        self.calls = defaultdict(deque)

    def allow(self, tool: str, limit: int, window: int, identity: str = "") -> bool:
        now = time.monotonic()
        bucket = self.calls[f"{tool}:{identity}"]
        while bucket and bucket[0] <= now - window:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


class ToolGateway:
    """所有动作工具统一经 dry-run、限流、配额和权限门禁。"""

    def __init__(self, policies: dict | None = None, limiter=None, quota: SendQuota | None = None):
        self.policies = policies or {}
        self.limiter = limiter or ToolRateLimiter()
        self.quota = quota if quota is not None else SendQuota()

    async def invoke(
        self,
        tool: str,
        action,
        *,
        dry_run: bool = True,
        rate_limit: int = 6,
        window_seconds: int = 60,
        requires_confirmation: bool = False,
        confirmed: bool = False,
        identity: str = "",
        priority: str = "normal",
        quota_token: QuotaToken | None = None,
        preflight=None,
    ):
        if priority not in {"normal", "critical"}:
            raise ValueError("unknown tool priority")
        policy = self.policies.get(tool, {})
        if tool in {"qzone", "friend_management"} and "enabled" not in policy:
            raise PermissionError(f"{tool} tool is not configured")
        enabled = bool(policy.get("enabled", False))
        if not enabled:
            if tool in {"qzone", "friend_management"}:
                raise PermissionError(f"{tool} tool is disabled")
            return {"status": "disabled"}
        rate_limit = int(policy.get("rate_limit", rate_limit))
        window_seconds = int(policy.get("window_seconds", window_seconds))
        if priority == "critical":
            logging.getLogger("companion.tools").info("critical tool invocation: %s", tool)
        elif not self.limiter.allow(tool, rate_limit, window_seconds, identity):
            logging.getLogger("companion.tools").warning("tool rate limited: %s", tool)
            return {"status": "rate_limited"}
        requires_confirmation = requires_confirmation or tool == "friend_management"
        if requires_confirmation and not confirmed:
            return {"status": "pending"}
        if dry_run or policy.get("dry_run", False):
            return {"status": "dry_run"}
        critical = priority == "critical"
        if self.quota is not None and tool in QUOTA_POLICIES:
            if quota_token is not None:
                # The reply path reserved these attempts atomically; never charge twice.
                if not self.quota.accept_token(
                    quota_token, critical=critical, identity=identity or "anonymous"
                ):
                    return {"status": "unavailable"}
            else:
                try:
                    reservation = self.quota.reserve(
                        identity or "anonymous", 1, critical=critical
                    )
                except Exception:  # noqa: BLE001 — accounting must fail closed
                    return {"status": "unavailable"}
                if reservation is None:
                    return {"status": "rate_limited"}
                if not self.quota.accept_token(
                    reservation.consume(), critical=critical, identity=identity or "anonymous"
                ):
                    return {"status": "unavailable"}
            if not await self.quota.acquire_slot(critical):
                return {"status": "busy"}
            try:
                # 槽后 preflight（E 路修复 4）：取得并发槽后、发起传输
                # 前最后一道复查——等待槽期间失效的授权（关怀同意被
                # 删除等）在此拦下，假/异常一律按 rejected 形态返回
                # （status=unavailable），action 绝不执行；槽由 finally
                # 正常释放。非额度工具无槽可言，不走此复查。
                if preflight is not None:
                    try:
                        verdict = preflight()
                        if inspect.isawaitable(verdict):
                            verdict = await verdict
                    except Exception:  # noqa: BLE001 — 复查故障 fail closed
                        verdict = False
                    if not verdict:
                        return {"status": "unavailable"}
                return await action()
            finally:
                self.quota.release_slot(critical)
        return await action()


class OneBotClient(ABC):
    """私聊消息发送抽象接口。"""

    @abstractmethod
    async def _send_private(
        self, policy_name: str, user_id: str, message: str, *, dry_run: bool, rate_limit: int,
        priority: str = "normal",
    ) -> dict:
        """调用 OneBot v11 发送私聊消息。"""


def _numeric_id(value):
    """数字型 ID 转 int（OneBot 惯例）；非数字值原样透传。

    收敛前 int() 转换位于网关 action 闭包内、门禁通过后才求值，
    dry_run/限流等门禁路径从不触碰它；参数在此处组装时若直接 int()
    会让非数字 ID 在门禁前先抛 ValueError，改变旧语义。故只对纯
    数字串/整数转换，其余值交给网关与传输层按原样处置。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return value


class HttpOneBotClient(OneBotClient):
    """调用本地 OneBot HTTP API（N1 起经 ActionGateway + OneBotTransport）。

    本类是适配器层网关之上的旧口径垫片：公共方法签名与旧 dict 返回
    语义逐项不变，HTTP 只经 OneBotTransport 出站、门控只经
    ActionGateway 收口（方案 §3.1/C15）。``gateway`` 入参兼容两种形态：
    ActionGateway 直接使用；ToolGateway（ReadOnlyRegistry 与既有测试的
    本地工具门控形态）则把其 policies/limiter/quota 对象共享进新建的
    ActionGateway——同一 dict、同一 limiter 对象，不拷贝，运行时改
    ``client.gateway.policies`` 与清 ``limiter.calls`` 的旧语义不变。
    """

    def __init__(self, base_url: str = "http://127.0.0.1:3000", policies=None, gateway=None,
                 quota: SendQuota | None = None, operation_ledger=None,
                 l3_unlocked=None, resource_roots=None, resource_base_dir=None):
        self.base_url = base_url.rstrip("/")
        if isinstance(gateway, ActionGateway):
            if l3_unlocked is not None:
                # 解锁名单是网关构造期快照：外部网关自带其名单，
                # 客户端不事后改写（换名单 = 重建网关/重启）。
                raise ValueError(
                    "l3_unlocked must be set on the provided gateway itself"
                )
            if resource_roots is not None or resource_base_dir is not None:
                # 资源根/基准目录同为网关构造期口径（补审第二段
                # P2）：外部网关自带其边界，客户端不事后改写。
                raise ValueError(
                    "resource_roots/resource_base_dir must be set on the "
                    "provided gateway itself"
                )
            self.action_gateway = gateway
            self.transport = gateway.transport
            if operation_ledger is not None:
                # 显式注入的持久账本覆盖网关自带账本（Review 修复 3）。
                self.action_gateway.operation_ledger = operation_ledger
        elif gateway is not None:
            self.transport = OneBotTransport(self.base_url)
            self.action_gateway = ActionGateway(
                self.transport,
                policies=gateway.policies,
                quota=gateway.quota,
                limiter=gateway.limiter,
                operation_ledger=operation_ledger,
                l3_unlocked=l3_unlocked,
                resource_roots=resource_roots or (),
                resource_base_dir=resource_base_dir,
            )
        else:
            self.transport = OneBotTransport(self.base_url)
            self.action_gateway = ActionGateway(
                self.transport,
                policies=(
                    policies
                    if policies is not None
                    else {"send_private_msg": {"enabled": True}}
                ),
                # 与旧 ToolGateway 等价：quota 未给时新建 SendQuota，
                # 发送额度记账不因收敛而消失。
                quota=quota if quota is not None else SendQuota(),
                operation_ledger=operation_ledger,
                l3_unlocked=l3_unlocked,
                resource_roots=resource_roots or (),
                resource_base_dir=resource_base_dir,
            )
        self.gateway = self.action_gateway
        self.quota = self.action_gateway.quota
        self.actions = ActionsBundle(self.action_gateway, PRINCIPAL_SYSTEM)

    @staticmethod
    def _translate(result: ActionResult, *, policy_key: str, action: str = "") -> dict:
        """ActionResult → 旧 dict 语义翻译（逐项对齐收敛前行为）。

        not_configured → PermissionError（仅 qzone/friend_management
        这类 legacy spec 会产生）；disabled 对 legacy 两键抛
        PermissionError、其余回 {"status": "disabled"}；rate_limited /
        quota_unavailable / slot_busy 分别回 rate_limited / unavailable
        / busy；simulated 的 dry_run / pending_confirmation 分别回
        dry_run / pending；ok 或带 raw 的 rejected 回平台响应原样；
        ambiguous 且无 raw 表示传输结果不明，抛 TransportError 交上层
        收敛；其余门禁一律 fail closed 回 unavailable，不打断发送链。
        """
        gate = result.gate
        if gate == "not_configured":
            raise PermissionError(f"{policy_key} tool is not configured")
        if gate == "disabled":
            if policy_key in {"qzone", "friend_management"}:
                raise PermissionError(f"{policy_key} tool is disabled")
            return {"status": "disabled"}
        if gate == "rate_limited":
            return {"status": "rate_limited"}
        if gate == "quota_unavailable":
            return {"status": "unavailable"}
        if gate == "slot_busy":
            return {"status": "busy"}
        if result.outcome == "simulated":
            if gate == "dry_run":
                return {"status": "dry_run"}
            if gate == "pending_confirmation":
                return {"status": "pending"}
        # raw 判空一律用 is not None（Review 修复 6）：平台回 {} 是
        # 合法空响应，须原样返回 {}，不得因 falsy 误判为 unavailable；
        # {} 后续经 adapter/delivery 映射为 ambiguous 的旧口径不变。
        if result.outcome == "ok" or (result.outcome == "rejected" and result.raw is not None):
            return result.raw
        if result.outcome == "ambiguous":
            if result.raw is None:
                raise TransportError(
                    action or policy_key, result.error_code or "AmbiguousResult"
                )
            return {"status": "unavailable"}
        return {"status": "unavailable"}

    async def _send_private(
        self,
        policy_name: str,
        user_id: str,
        message: str,
        *,
        dry_run: bool,
        rate_limit: int,
        priority: str = "normal",
        quota_token: QuotaToken | None = None,
        preflight=None,
    ) -> dict:
        result = await self.action_gateway.invoke(
            "send_private_msg",
            {"user_id": _numeric_id(user_id), "message": message},
            principal=PRINCIPAL_SYSTEM,
            policy_key=policy_name,
            dry_run=dry_run,
            rate_limit=rate_limit,
            identity=str(user_id),
            priority=priority,
            quota_token=quota_token,
            preflight=preflight,
        )
        return self._translate(result, policy_key=policy_name, action="send_private_msg")

    async def send_private_msg(
        self, user_id: str, message: str, *, dry_run: bool = False,
        rate_limit: int = NORMAL_PER_USER_LIMIT, priority: str = "normal",
        quota_token: QuotaToken | None = None, preflight=None,
    ) -> dict:
        return await self._send_private(
            "send_private_msg", user_id, message, dry_run=dry_run, rate_limit=rate_limit,
            priority=priority, quota_token=quota_token, preflight=preflight,
        )

    async def send_alert_private_msg(self, user_id: str, message: str) -> dict:
        policy = self.gateway.policies.get("alert_private_msg", {})
        return await self._send_private(
            "alert_private_msg",
            user_id,
            message,
            dry_run=bool(policy.get("dry_run", False)),
            rate_limit=int(policy.get("rate_limit", 2)),
            priority="critical",
        )

    async def send_segments(
        self, target, segments, *, priority: str = "normal",
        quota_token: QuotaToken | None = None, preflight=None,
    ) -> dict:
        """段级发送垫片（N2，DeliveryService.send_to 的生产 sender 通道）。

        段序列经 adapters.onebot 的编码分发（CQ 字符串或 segment
        数组）后走 ActionGateway：私聊 → send_private_msg（critical
        时 policy_key=alert_private_msg，与 send_alert_private_msg
        同口径），群 → send_group_msg（policy 来自 tools.group_chat，
        默认 enabled=false/dry_run=true，即运行时默认硬关）。报文
        不可序列化返回 {"status": "unavailable"}（由 DeliveryService
        映射为 rejected），不静默丢段。
        """
        message = segments_to_onebot_message(segments)
        if message is None:
            return {"status": "unavailable"}
        if isinstance(target, PrivateTarget):
            policy_key = (
                "alert_private_msg" if priority == "critical" else "send_private_msg"
            )
            result = await self.action_gateway.invoke(
                "send_private_msg",
                {"user_id": _numeric_id(target.user_id), "message": message},
                principal=PRINCIPAL_SYSTEM,
                policy_key=policy_key,
                identity=str(target.user_id),
                priority=priority,
                quota_token=quota_token,
                preflight=preflight,
            )
            return self._translate(
                result, policy_key=policy_key, action="send_private_msg"
            )
        if isinstance(target, GroupTarget):
            result = await self.action_gateway.invoke(
                "send_group_msg",
                {"group_id": _numeric_id(target.group_id), "message": message},
                principal=PRINCIPAL_SYSTEM,
                policy_key="send_group_msg",
                identity=target.identity,
                priority=priority,
                quota_token=quota_token,
                preflight=preflight,
            )
            return self._translate(
                result, policy_key="send_group_msg", action="send_group_msg"
            )
        return {"status": "unavailable"}

    async def send_group_msg(
        self, group_id: str, message: str, *, dry_run: bool = True, rate_limit: int = 6
    ):
        result = await self.action_gateway.invoke(
            "send_group_msg",
            {"group_id": _numeric_id(group_id), "message": message},
            principal=PRINCIPAL_SYSTEM,
            policy_key="send_group_msg",
            dry_run=dry_run,
            rate_limit=rate_limit,
            identity=str(group_id),
        )
        return self._translate(result, policy_key="send_group_msg", action="send_group_msg")

    async def send_reaction(
        self, user_id: str, reaction: str, *, dry_run: bool = True, rate_limit: int = 6
    ):
        raise PermissionError("reaction tool is disabled")

    async def qzone_post(self, content: str, *, dry_run: bool = True, rate_limit: int = 2):
        # N4 迁移：旧端点 publish_message 的 legacy 合成 spec 已下线，
        # 改调登记在册的 send_qzone_msg；policy_key 仍为 qzone，
        # enabled/dry_run/限流与 not_configured 的 PermissionError
        # 口径逐项不变（spec 的 allowed_policy_keys 含 qzone）。
        result = await self.action_gateway.invoke(
            "send_qzone_msg",
            {"message": content},
            principal=PRINCIPAL_SYSTEM,
            policy_key="qzone",
            dry_run=dry_run,
            rate_limit=rate_limit,
        )
        return self._translate(result, policy_key="qzone", action="send_qzone_msg")

    async def manage_friend(
        self,
        user_id: str,
        operation: str,
        *,
        dry_run: bool = True,
        rate_limit: int = 2,
        confirmed: bool = False,
    ):
        result = await self.action_gateway.invoke(
            "set_friend_add_request",
            # flag 为 OneBot 该动作的申请标识（已验证 schema 必填，网关
            # 参数校验要求）；user_id/operation 为收敛前的旧报文键，保留
            # 不动。flag 取同一 user_id 字符串，与旧调用指向同一申请。
            {"flag": str(user_id), "user_id": _numeric_id(user_id), "operation": operation},
            principal=PRINCIPAL_SYSTEM,
            policy_key="friend_management",
            dry_run=dry_run,
            rate_limit=rate_limit,
            confirmed=confirmed,
            identity=str(user_id),
            # legacy 路径的授权下限（网关 c2b，补审第二段 P3）：本
            # 垫片是 friend_management 例外的唯一接线点，在此显式
            # 注入 account 级授权上下文（目标派生为 account）——
            # 例外不再由 invoke 调用方自选 policy_key 即可开启，
            # 端到端行为（pending/真派发口径）不变。
            authorization=AuthorizationContext(
                authorizer="tools.friend_management", account=True
            ),
        )
        return self._translate(
            result, policy_key="friend_management", action="set_friend_add_request"
        )

    async def manage_friend_when_enabled(
        self, user_id: str, operation: str, *, enabled: bool = False
    ):
        if not enabled:
            raise PermissionError("friend management is disabled")
        return await self.manage_friend(user_id, operation)
