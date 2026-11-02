"""平台适配器契约：统一入站事件、消息段与发送回执（方案 §5.1/§5.2）。

适配器只做协议翻译与传输：验签、事件解析、发送与探活都收在适配器内，
业务层只认本模块的类型。投递契约（额度、优先级、回执三态的记账）归
DeliveryService 独占，适配器不得自行计费或另设限流。
"""

from dataclasses import dataclass, field
from typing import Mapping, Protocol, runtime_checkable

# Ignore.reason_code 的固定枚举（方案 §5.2）。
REASON_NOT_PRIVATE = "not_private"
REASON_NO_MESSAGE_ID = "no_message_id"
REASON_NO_SELF_ID = "no_self_id"
REASON_MALFORMED = "malformed"
REASON_UNSUPPORTED_TYPE = "unsupported_type"
# N3 新增（全接口方案 §3.3）：与 malformed 区分——not_message 供仍走
# normalize 严格级的调用方标记「结构合法但不是消息」；unknown_event_type
# 供 classify 标记「类型字段是合法 token、但不在已知事件枚举内」。
REASON_NOT_MESSAGE = "not_message"
REASON_UNKNOWN_EVENT_TYPE = "unknown_event_type"


@dataclass(frozen=True)
class TextSegment:
    """纯文本消息段。"""

    text: str


@dataclass(frozen=True)
class ImageSegment:
    """图片消息段，data 为平台原始段数据。"""

    data: dict


@dataclass(frozen=True)
class AudioSegment:
    """语音消息段，data 为平台原始段数据。"""

    data: dict


@dataclass(frozen=True)
class AtSegment:
    """@ 消息段，data 为平台原始段数据。"""

    data: dict


@dataclass(frozen=True)
class RawSegment:
    """未建模的消息段，按平台原始 type 与 data 透传。"""

    type: str
    data: dict


@dataclass(frozen=True)
class ImageRef:
    """入站图片引用，仅引用与平台报告的大小提示，不含图片内容。

    ``ref`` 多为带签名的 CDN URL，``repr=False`` 防其随对象 repr
    进日志/异常上报（与 ResolvedResource.ref 同例）。本类型原在
    perception 层，2026-10-31 接缝修复时下沉到适配器契约层：图片
    引用是 normalize 的契约产出（见 InboundEvent.images），感知层
    只作转发（perception 仍 re-export 本类型，旧导入路径不变）。

    ``file_id`` 为平台段 data 的 ``file`` 标识（NapCat get_image
    取本地缓存文件的键），与 ``ref`` 独立保留：有 url 时 ref 仍
    优先 url，但 file_id 照常填充，供消费方走 NapCat 介导取图。
    """

    ref: str = field(repr=False)
    size_hint: int | None = None
    # repr=False 同 ref 口径：file_id 为平台不透明标识，不进日志/repr。
    file_id: str | None = field(default=None, repr=False)


def extract_image_refs(segments) -> tuple[ImageRef, ...]:
    """从消息段中提取图片引用：只取引用与大小提示，不取图片内容。

    url 优先、无 url 时以 file 标识兜底（取址由消费方决定，本层
    只如实保留平台给的引用）；file_size 仅在平台报告为非负整数
    时保留为大小提示（字符串报告不采，bool 是 int 子类须排除）。
    data 的 ``file`` 为非空字符串时同时填 ``file_id``（与 url
    是否存在无关），供 NapCat 介导取图；空串/非字符串不采。
    """
    refs: list[ImageRef] = []
    for segment in segments or ():
        if not isinstance(segment, ImageSegment):
            continue
        data = segment.data if isinstance(segment.data, dict) else {}
        ref = data.get("url")
        if not (isinstance(ref, str) and ref):
            ref = data.get("file")
        if not (isinstance(ref, str) and ref):
            continue
        size = data.get("file_size")
        size_hint = (
            size
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0
            else None
        )
        file_value = data.get("file")
        file_id = (
            file_value if isinstance(file_value, str) and file_value else None
        )
        refs.append(ImageRef(ref, size_hint, file_id))
    return tuple(refs)


# ---------------------------------------------------------------------------
# N2 段全集（方案 §2.2）：reply/face/mface/video/file/poke/dice/rps 提为
# 一等段，forward/node 为结构化段；json/xml/music/contact/location/
# markdown/miniapp 登记为已知段但仍以 RawSegment 承载（KNOWN_RAW_SEGMENT
# _TYPES）。flashtransfer/onlinefile 两段 N5 已按 bundle 证据定稿为
# receive-only Raw 段（见 RECEIVE_ONLY_SEGMENT_TYPES）：只入不出——
# 入站解析产出 RawSegment 并校验 data 关键字段，出站序列化明确拒绝。
# 注：在线文件段的平台字面量是 onlinefile（无下划线）；online_file
# 只是动作名/事件名，bundle 中无此段字面量（N5 正名）。


@dataclass(frozen=True)
class ReplySegment:
    """引用回复段，data 为平台原始段数据（id 必填）。"""

    data: dict


@dataclass(frozen=True)
class FaceSegment:
    """系统表情段，data 为平台原始段数据（id 必填）。"""

    data: dict


@dataclass(frozen=True)
class MFaceSegment:
    """商城表情段，data 为平台原始段数据（含 super-emoji 字段）。"""

    data: dict


@dataclass(frozen=True)
class VideoSegment:
    """视频段，data 为平台原始段数据（file 必填，可含 thumb）。"""

    data: dict


@dataclass(frozen=True)
class FileSegment:
    """文件段，data 为平台原始段数据（与文件域接口配套）。"""

    data: dict


@dataclass(frozen=True)
class PokeSegment:
    """戳一戳段，data 为平台原始段数据。"""

    data: dict


@dataclass(frozen=True)
class DiceSegment:
    """骰子段，data 为平台原始段数据。"""

    data: dict


@dataclass(frozen=True)
class RpsSegment:
    """猜拳段，data 为平台原始段数据。"""

    data: dict


@dataclass(frozen=True)
class ForwardSegment:
    """合并转发引用段（按转发 id 引用），data 为平台原始段数据。"""

    data: dict


@dataclass(frozen=True)
class NodeSegment:
    """合并转发节点段（结构化）：data 为节点属性（发送者标识/昵称等），
    content 为节点内的段元组（可嵌套 NodeSegment，序列化走 segment
    数组报文，不用 CQ 拼接表达嵌套结构，方案 §2.2/§3.1）。"""

    data: dict
    content: tuple = ()


#: 只入不出的段类型（N5 定稿）：NapCat 的 ob11→raw 转换器对这两类
#: 是空实现（bundle 原文注释「不需要支持发送」），平台不支持经消息
#: 段发送；入站可收（RawSegment + data 关键字段校验），出站明确拒绝。
RECEIVE_ONLY_SEGMENT_TYPES = frozenset({"flashtransfer", "onlinefile"})

#: 已登记但仍以 RawSegment 承载的段类型（方案 §2.2「已知段」行；
#: N5 追加 receive-only 两段 flashtransfer/onlinefile）。
KNOWN_RAW_SEGMENT_TYPES = frozenset(
    {"json", "xml", "music", "contact", "location", "markdown", "miniapp"}
) | RECEIVE_ONLY_SEGMENT_TYPES

#: 一等建模的段类型（与段类一一对应）。
FIRST_CLASS_SEGMENT_TYPES = frozenset(
    {
        "text",
        "at",
        "reply",
        "face",
        "mface",
        "image",
        "record",
        "video",
        "file",
        "poke",
        "dice",
        "rps",
        "forward",
        "node",
    }
)

#: 本实现可构造/可解析的全部段类型（段覆盖矩阵中除待验 2 类外）。
SUPPORTED_SEGMENT_TYPES = FIRST_CLASS_SEGMENT_TYPES | KNOWN_RAW_SEGMENT_TYPES


Segment = (
    TextSegment
    | ImageSegment
    | AudioSegment
    | AtSegment
    | RawSegment
    | ReplySegment
    | FaceSegment
    | MFaceSegment
    | VideoSegment
    | FileSegment
    | PokeSegment
    | DiceSegment
    | RpsSegment
    | ForwardSegment
    | NodeSegment
)


# ---------------------------------------------------------------------------
# SendTarget：结构化投递寻址（方案 §3.2 / C8 / C13），替代 session_id
# 字符串与 split(":") 反解。session_id 仅保留作会话键口径。


@dataclass(frozen=True)
class PrivateTarget:
    """私聊目标。"""

    user_id: str

    @property
    def kind(self) -> str:
        return "private"

    @property
    def identity(self) -> str:
        """额度/限流身份键（私聊按用户）。"""
        return str(self.user_id)

    @property
    def session_id(self) -> str:
        return f"onebot:{self.user_id}"


@dataclass(frozen=True)
class GroupTarget:
    """群目标。"""

    group_id: str

    @property
    def kind(self) -> str:
        return "group"

    @property
    def identity(self) -> str:
        """额度/限流身份键（群按 g: 前缀与用户身份分开命名空间）。"""
        return f"g:{self.group_id}"

    @property
    def session_id(self) -> str:
        return f"onebot:g{self.group_id}"


SendTarget = PrivateTarget | GroupTarget


@dataclass(frozen=True)
class InboundEvent:
    """适配器归一化后的统一入站消息事件（判别联合的消息型）。

    raw 为平台原文透传，仅适配器与排障可用，业务层禁读私有字段。
    N3 起末尾追加 v2 字段（全接口方案 §3.3），全部带默认值：既有构造
    点零改动继续工作，默认值即私聊入站口径（event_kind=message、
    chat_type=private、direction=inbound）。
    """

    platform: str
    self_id: str
    user_id: str
    session_id: str
    segments: tuple
    text: str
    has_media: bool
    message_id: str | None
    raw: dict
    event_kind: str = "message"
    chat_type: str = "private"
    group_id: str | None = None
    actor_id: str | None = None
    target_id: str | None = None
    sub_type: str | None = None
    direction: str = "inbound"
    # 图片引用是 normalize 的契约产出（2026-10-31 接缝修复）：由段
    # 提取（数组形与 CQ 字符串形同口径），不再靠消费方从 segments
    # 手抄重提取。签名 URL 只在本对象内存流转，禁入日志/台账/记忆。
    images: tuple[ImageRef, ...] = ()


@dataclass(frozen=True)
class NoticeEvent:
    """通知事件（判别联合的 notice 型）：无 user_id 必填项。

    user_id 为事件主体/对象（可为空串）；actor_id 为操作者
    （operator_id，如撤回/禁言的执行者），target_id 为事件对象
    （仅当 operator 存在时与 user_id 同值，主体与对象由此分开）。
    """

    platform: str
    self_id: str
    notice_type: str
    sub_type: str | None
    user_id: str
    group_id: str | None
    actor_id: str | None
    target_id: str | None
    message_id: str | None
    raw: dict


@dataclass(frozen=True)
class RequestEvent:
    """请求事件（判别联合的 request 型）：flag 为处置动作的回执凭据。"""

    platform: str
    self_id: str
    request_type: str
    sub_type: str | None
    user_id: str
    group_id: str | None
    flag: str
    raw: dict


@dataclass(frozen=True)
class MetaEvent:
    """元事件（判别联合的 meta 型）：lifecycle/heartbeat 与自身消息
    回流（message_sent，direction=self_echo）共用此型，只入观测路径。"""

    platform: str
    self_id: str
    meta_event_type: str
    sub_type: str | None
    direction: str = "inbound"
    message_id: str | None = None
    user_id: str = ""
    group_id: str | None = None
    status_online: bool | None = None
    status_good: bool | None = None
    raw: dict = field(default_factory=dict)


#: 入站判别联合：classify 的产出四型（方案 §3.3）。
ClassifiedEvent = InboundEvent | NoticeEvent | RequestEvent | MetaEvent


@dataclass(frozen=True)
class Ignore:
    """不进入流水线的事件：只记原因码与事件类型摘要，绝不记正文。"""

    reason_code: str
    event_type_summary: str


@dataclass(frozen=True)
class AdapterCapabilities:
    """适配器能力声明。"""

    supports_recall: bool = False
    multi_bubble_via_repeat_send: bool = True
    # N2：本实现可构造/可解析的段类型（与段覆盖矩阵同源口径，
    # tests 断言与段模型/序列化面一致）。
    segment_types: frozenset = frozenset()
    # N3：本实现 classify 可产出的 event_kind 集合。
    event_kinds: frozenset = frozenset()
    # N6（方案 §3.4）：action_name → 本实现是否提供，由 ActionSpec
    # 注册表生成（gateway.build_action_levels），不手写——登记表、
    # 能力声明、实现面三者同源，tests 断言与 ACTION_SPECS 一致。
    action_levels: Mapping[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterHealth:
    """适配器探活结果；good 的来源须如实标注（见各适配器 health 实现）。"""

    online: bool
    good: bool
    detail: str = ""


@dataclass(frozen=True)
class DeliveryHandle:
    """DeliveryService 随发送携带的投递句柄：优先级与额度预留 token。"""

    priority: str = "normal"
    reservation_token: object = None


@dataclass(frozen=True)
class SendReceipt:
    """发送回执三态：超时/结果不明一律 ambiguous，绝不猜 accepted。"""

    outcome: str  # accepted / rejected / ambiguous
    platform_message_id: str | None = None


@runtime_checkable
class PlatformAdapter(Protocol):
    """平台适配器协议：业务层只依赖此契约，不接触平台私有字段。"""

    name: str
    capabilities: AdapterCapabilities

    async def start(self) -> None:
        """启动适配器（无连接型适配器可为 no-op）。"""
        ...

    async def stop(self) -> None:
        """停止适配器（无连接型适配器可为 no-op）。"""
        ...

    async def health(self) -> AdapterHealth:
        """只读探活：只观察不动手，不发消息、不重放。"""
        ...

    def normalize(self, raw: dict, *, require_keys: bool = True) -> InboundEvent | Ignore:
        """把平台原始事件归一化；不合格事件返回带 reason_code 的 Ignore。"""
        ...

    def classify(self, raw, *, require_keys: bool = True) -> ClassifiedEvent | Ignore:
        """把平台原始事件分类为判别联合四型之一；不合格返回 Ignore。

        与 normalize 的分工：normalize 是消息类的严格级（既有 reason
        语义不变），classify 是全事件面的结构分类入口（消息分支委托
        normalize，事件三型走枚举白名单校验）。
        """
        ...

    async def send_private(
        self, session_id: str, segments, *, delivery: DeliveryHandle | None = None
    ) -> SendReceipt:
        """发送一条私聊；结果不明一律回 ambiguous，适配器从不重试。"""
        ...
