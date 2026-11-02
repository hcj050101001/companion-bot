"""断线补回的 D1 单一适配层：历史接口原始返回的唯一解读点。

设计见实例设计稿 §6（qqbot-reconnect-catchup）：全仓只有本模块解读
``get_recent_contact`` / ``get_friend_msg_history`` /
``get_group_msg_history`` 三个历史动作的原始返回——字段名、时间语义、
message_id 取值、at 段载体判定、bot self_id 解析全部收敛于此；业务层
（companion/ingress/catchup.py）只依赖本模块的归一化类型与
``HistorySource`` 协议，真机验证结论翻转时只改本模块一处。

D1 验证状态（2026-11-01）：``get_recent_contact`` 能捞回断线期消息、
array 段 / sender / epoch 时间齐全已真机证实；但 history 动作返回的
字段名细节、时间单位、容器形态与 OneBot ``message_id`` 保真度仍属
**推测**（D1-c/d/e/f 未验）。下文凡标注「推测」的取值链均为按 OneBot 11
与 NTQQ 惯例的推测实现，样本测试锁定其当前行为，不得在别处写成已验。

返回语义严格区分：``None`` = 不可用（动作失败 / 抛错 / 结构不可用，
业务层据此降级或跳过）；空元组 = 可用但无数据（不降级、正常收尾）。

本模块不落盘、不打正文日志（只有丢弃原因计数 ``drop_counts``）。
只依赖 adapters 层内部与标准库（导入边界 tests/test_import_boundaries_p0）。
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from companion.adapters.base import Ignore, InboundEvent
from companion.adapters.onebot import classify_onebot_event, normalize_onebot_event

#: 单次历史拉取的请求条数上限（设计 §6.3）。count 是否被平台 honored
#: 属 D1-e 未验项：窗口过滤与每会话截断都在本地做，count 只影响一次
#: 拉取的覆盖面；50 是覆盖 24h 窗口内单会话常态积压的保守值。
HISTORY_FETCH_COUNT = 50

# 容器形态的候选键（推测，D1-f）：data 为 dict 时按序取第一个为 list 的键。
_CONTAINER_KEYS = ("messages", "list", "data", "contacts")

# history 条目发送时刻的取值链（推测，D1-f）：OneBot 惯例首选 time。
_TIME_KEYS = ("time", "timestamp", "sent_time", "send_time")

# contacts 条目最后活动时刻的取值链（推测，D1-f；lastMsg 为嵌套形态）。
_CONTACT_TIME_KEYS = ("lastMsgTime", "last_time", "time")


@dataclass(frozen=True)
class NormalizedContact:
    """归一化会话线索：recent_contacts 的一条。"""

    kind: str  # "private" | "group"
    peer_id: str  # 私聊=user_id，群=group_id（均为 str）
    last_activity_at: float  # epoch 秒


@dataclass(frozen=True)
class NormalizedHistoryEntry:
    """归一化历史条目：业务层消费的唯一历史消息形态。

    ``message_id`` 以既有 normalize/classify 产出的 ``event.message_id``
    为准（设计 §10.1 的 R2 退路预留：D1-c 翻转时该字段可替换为本地键
    派生值，本期不实现退路逻辑）。
    """

    message_id: str  # 归一化后的 OneBot message_id（str）
    sender_id: str
    sent_at: float  # epoch 秒
    at_known: bool  # §6.4：段载体可判读（私聊恒 True）
    event: InboundEvent  # 经既有 normalize/classify 归一的事件


@runtime_checkable
class HistorySource(Protocol):
    """补回扫描消费的历史来源协议（业务层只依赖此协议）。"""

    async def recent_contacts(self) -> tuple[NormalizedContact, ...] | None: ...

    async def friend_history(
        self, user_id: str
    ) -> tuple[NormalizedHistoryEntry, ...] | None: ...

    async def group_history(
        self, group_id: str
    ) -> tuple[NormalizedHistoryEntry, ...] | None: ...


def _id_text(value) -> str | None:
    """标识归一：仅 str/int（不含 bool），去空白后为空即 None。

    与 onebot._id_text 的账号标识口径同源（不放行负数/浮点/容器）。
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, int) and value <= 0:
        return None
    text = str(value).strip()
    return text or None


def _numeric_id(value) -> str | None:
    """纯数字标识归一（peerUid 专用，推测其可能为 UID 字符串形态）。"""
    if isinstance(value, str) and value.isdigit():
        return value
    return _id_text(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _normalize_time(value) -> float | None:
    """时间归一（推测，D1-f，样本测试锁定）：

    数值 ≥1e12 视为毫秒除 1000；≥1e9 视为 epoch 秒原样；字符串先按
    数字试转（同二档）、再按 ISO 8601 试解；其余一律 None。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _epoch_from_number(float(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _epoch_from_number(float(text))
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _epoch_from_number(number: float) -> float | None:
    if number >= 1e12:
        return number / 1000.0
    if number >= 1e9:
        return number
    return None


def _extract_items(data) -> list | None:
    """容器形态归一（推测，D1-f）：list 即条目列表；dict 按
    ``_CONTAINER_KEYS`` 顺序取第一个为 list 的键；皆无 → None（不可用）。
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in _CONTAINER_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return None


def _first_time(item: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in item:
            normalized = _normalize_time(item.get(key))
            if normalized is not None:
                return normalized
    return None


class GatewayHistorySource:
    """生产实现：包裹 ActionsBundle 门面调用三个 L0 历史动作。

    门面纪律（actions.py 文件头）：只经门面方法出站——
    ``social.get_recent_contact()`` /
    ``media.get_friend_msg_history(user_id, count=...)`` /
    ``media.get_group_msg_history(group_id, count=...)`` /
    ``system.get_login_info()``；归一只发生在本模块。
    ``actions`` 为 None 时三方法恒返回 None（不可用），不抛。
    """

    def __init__(self, actions, *, self_id: str | None = None):
        self._actions = actions
        self._self_id = _id_text(self_id)
        self._login_self_id: str | None = None
        # 丢弃原因计数（只记类别与条数，绝不记正文/原始标识原文细节）。
        self.drop_counts: dict[str, int] = {}

    # ---------------------------------------------------------- 内部工具

    def _drop(self, reason: str) -> None:
        self.drop_counts[reason] = self.drop_counts.get(reason, 0) + 1

    async def _call_data(self, facade_call) -> tuple[bool, object]:
        """执行一次门面调用：(True, data) 或 (False, None)=不可用。

        outcome != "ok" / 抛错 / 结果异形一律不可用（None 语义在调用方）。
        """
        try:
            result = await facade_call()
        except Exception:  # noqa: BLE001 — 历史源不可用即降级，不上抛
            return False, None
        if result is None or getattr(result, "outcome", None) != "ok":
            return False, None
        return True, result.data

    async def _login_self_id_cached(self) -> str | None:
        """经 system.get_login_info() 解析 bot self_id 并缓存（设计 §6.5）。

        失败不使整源不可用：返回 None，降级为逐条自带 self_id 模式。
        """
        if self._login_self_id is not None:
            return self._login_self_id
        if self._actions is None:
            return None
        ok, data = await self._call_data(self._actions.system.get_login_info)
        if ok and isinstance(data, dict):
            resolved = _id_text(data.get("user_id"))
            if resolved is not None:
                self._login_self_id = resolved
        return self._login_self_id

    async def _fallback_self_id(self) -> str | None:
        """注入值优先，其次 get_login_info 缓存（条目自带在逐条处优先）。"""
        if self._self_id is not None:
            return self._self_id
        return await self._login_self_id_cached()

    # ---------------------------------------------------------- contacts

    async def recent_contacts(self) -> tuple[NormalizedContact, ...] | None:
        if self._actions is None:
            return None
        ok, data = await self._call_data(self._actions.social.get_recent_contact)
        if not ok:
            return None
        items = _extract_items(data)
        if items is None:
            return None
        contacts: list[NormalizedContact] = []
        for item in items:
            contact = self._contact_from(item)
            if contact is not None:
                contacts.append(contact)
        return tuple(contacts)

    def _contact_from(self, item) -> NormalizedContact | None:
        """contacts 条目归一（字段链为推测，D1-f，样本测试锁定）。"""
        if not isinstance(item, dict):
            self._drop("contact_unusable")
            return None
        group_id = _id_text(item.get("group_id")) or _id_text(item.get("groupId"))
        chat_type = item.get("chatType")
        # kind 判定（chatType 1/2 语义为 NTQQ 惯例推测）：
        # 含 group_id/groupId 或 chatType==2 → group；chatType==1 或含
        # user_id 类键 → private；皆无 → 丢弃。
        if group_id is not None or chat_type == 2:
            kind = "group"
        elif (
            chat_type == 1
            or "peerUin" in item
            or "uin" in item
            or "user_id" in item
            or "peerUid" in item
        ):
            kind = "private"
        else:
            self._drop("contact_unusable")
            return None
        # peer_id 取值链（推测）：peerUin → uin → user_id → peerUid
        # （仅纯数字）→ group_id（群）。
        peer_id = (
            _id_text(item.get("peerUin"))
            or _id_text(item.get("uin"))
            or _id_text(item.get("user_id"))
            or _numeric_id(item.get("peerUid"))
            or group_id
        )
        if kind == "group" and group_id is not None:
            peer_id = group_id
        if peer_id is None:
            self._drop("contact_unusable")
            return None
        last_activity_at = _first_time(item, _CONTACT_TIME_KEYS)
        if last_activity_at is None:
            nested = item.get("lastMsg")
            if isinstance(nested, dict):
                last_activity_at = _normalize_time(nested.get("time"))
        if last_activity_at is None:
            self._drop("contact_unusable")
            return None
        return NormalizedContact(
            kind=kind, peer_id=peer_id, last_activity_at=last_activity_at
        )

    # ---------------------------------------------------------- history

    async def friend_history(
        self, user_id: str
    ) -> tuple[NormalizedHistoryEntry, ...] | None:
        if self._actions is None:
            return None
        ok, data = await self._call_data(
            lambda: self._actions.media.get_friend_msg_history(
                user_id, count=HISTORY_FETCH_COUNT
            )
        )
        if not ok:
            return None
        return await self._entries_from(data, kind="private", peer_id=user_id)

    async def group_history(
        self, group_id: str
    ) -> tuple[NormalizedHistoryEntry, ...] | None:
        if self._actions is None:
            return None
        ok, data = await self._call_data(
            lambda: self._actions.media.get_group_msg_history(
                group_id, count=HISTORY_FETCH_COUNT
            )
        )
        if not ok:
            return None
        return await self._entries_from(data, kind="group", peer_id=group_id)

    async def _entries_from(
        self, data, *, kind: str, peer_id: str
    ) -> tuple[NormalizedHistoryEntry, ...] | None:
        items = _extract_items(data)
        if items is None:
            return None
        fallback_self_id = await self._fallback_self_id()
        entries: list[NormalizedHistoryEntry] = []
        for item in items:
            entry = self._entry_from(
                item, kind=kind, peer_id=peer_id, fallback_self_id=fallback_self_id
            )
            if entry is not None:
                entries.append(entry)
        # 按 sent_at 升序稳定排序（业务层不再排序）。
        entries.sort(key=lambda entry: entry.sent_at)
        return tuple(entries)

    def _entry_from(
        self, item, *, kind: str, peer_id: str, fallback_self_id: str | None
    ) -> NormalizedHistoryEntry | None:
        """单条历史条目归一：补齐四键后复用既有 normalize/classify，
        不手写段解析（段语义与实时通道同源）。
        """
        if not isinstance(item, dict):
            self._drop("not_message")
            return None
        # sent_at 从原始条目取值（event 不带时间）；无时间无法做窗口
        # 判定 → 丢弃（宁漏，设计 §6.3）。
        sent_at = _first_time(item, _TIME_KEYS)
        if sent_at is None:
            self._drop("no_time")
            return None
        raw = dict(item)
        raw["post_type"] = "message"
        raw["message_type"] = "private" if kind == "private" else "group"
        # user_id：条目自带优先（sender 子 dict 为真机已见形态）。
        if _id_text(raw.get("user_id")) is None:
            sender = item.get("sender")
            if isinstance(sender, dict):
                raw["user_id"] = sender.get("user_id")
        if _id_text(raw.get("user_id")) is None and kind == "private":
            # 方向不明即丢弃（审查 P3 修复）：顶层 user_id 与
            # sender.user_id 皆缺失时无法判定条目方向——它可能是机器人
            # 自发消息；以会话 peer_id 兜底会把自发条目伪装成对方条目、
            # 绕过业务层自发过滤使机器人回复自己。故与 no_self_id 同
            # 口径宁漏勿扰，计 no_sender 丢弃，不做 peer 兜底。
            self._drop("no_sender")
            return None
        if kind == "group" and _id_text(raw.get("group_id")) is None:
            raw["group_id"] = peer_id
        # self_id：条目自带逐条优先，缺失用注入/登录解析值注入；三者
        # 皆无 → 无法建 claim 键与自发判定，丢弃（设计 §6.5）。
        if _id_text(raw.get("self_id")) is None:
            if fallback_self_id is None:
                self._drop("no_self_id")
                return None
            raw["self_id"] = fallback_self_id
        if kind == "private":
            event = normalize_onebot_event(raw, require_keys=True)
        else:
            event = classify_onebot_event(raw, require_keys=True)
        # 非 InboundEvent（Ignore / notice / request / meta 等非消息类，
        # AC8c）与缺 message_id 一律在此收口丢弃。
        if isinstance(event, Ignore) or not isinstance(event, InboundEvent):
            self._drop("not_message")
            return None
        if event.message_id is None:
            self._drop("no_message_id")
            return None
        return NormalizedHistoryEntry(
            message_id=event.message_id,
            sender_id=event.user_id,
            sent_at=sent_at,
            at_known=self._at_known(item, kind=kind),
            event=event,
        )

    @staticmethod
    def _at_known(item: dict, *, kind: str) -> bool:
        """at 段还原可判读性（设计 §6.4，钉死口径）：

        私聊恒 True；群聊仅当原始条目含 list 形 ``message``，或含带
        CQ at 码的 ``raw_message`` / 字符串形 ``message`` 时为 True
        （段解析与实时同源）；仅有纯文本投影 → False（业务层整群跳过）。
        """
        if kind == "private":
            return True
        if isinstance(item.get("message"), list):
            return True
        for carrier in (item.get("raw_message"), item.get("message")):
            if isinstance(carrier, str) and "[CQ:at," in carrier:
                return True
        return False
