"""OneBot/NapCat 适配器：首个且唯一产品实现（方案 §5.2）。

SHA1 验签、CQ 码解析、消息段翻译只许存在于本模块内；出站传输与回执
映射自 N1 起收敛到 OneBotTransport（全仓唯一实现，见 transport.py）。
消息解析语义与 perception 旧 parse_event 逐字等价，
资格判定顺序固定（TransportGate 步骤 2）：非对象 → 非私聊 → 键缺失
（message_id/self_id，仅 require_keys 时）→ user_id 缺失 → 内容解析。
"""

import dataclasses
import hashlib
import hmac
import re

from companion.adapters.base import (
    REASON_MALFORMED,
    REASON_NO_MESSAGE_ID,
    REASON_NO_SELF_ID,
    REASON_NOT_PRIVATE,
    REASON_UNKNOWN_EVENT_TYPE,
    REASON_UNSUPPORTED_TYPE,
    RECEIVE_ONLY_SEGMENT_TYPES,
    SUPPORTED_SEGMENT_TYPES,
    AdapterCapabilities,
    AdapterHealth,
    AtSegment,
    AudioSegment,
    ClassifiedEvent,
    DeliveryHandle,
    DiceSegment,
    FaceSegment,
    FileSegment,
    ForwardSegment,
    Ignore,
    ImageSegment,
    InboundEvent,
    MetaEvent,
    MFaceSegment,
    NodeSegment,
    NoticeEvent,
    PokeSegment,
    RawSegment,
    ReplySegment,
    RequestEvent,
    RpsSegment,
    Segment,
    SendReceipt,
    TextSegment,
    VideoSegment,
    extract_image_refs,
)
from companion.adapters.gateway import build_action_levels
from companion.adapters.transport import OneBotTransport, receipt_from_result

_CQ_PATTERN = r"\[CQ:[a-zA-Z0-9_]+(?:,|\])"
_CQ_STRIP = r"\[CQ:[a-zA-Z0-9_]+(?:,[^\]]*)?\]"
_CQ_IMAGE = re.compile(r"\[CQ:image(?:,([^\]]*))?\]")
_CQ_AT = re.compile(r"\[CQ:at(?:,([^\]]*))?\]")


def _cq_unescape(value: str) -> str:
    """CQ 参数值反转义（须在按字面逗号切分参数之后逐值执行）。"""
    return (
        value.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def _parse_cq_image_segments(raw_message: str) -> tuple:
    """从 CQ 字符串中提取 image 段（2026-10-31 接缝修复）。

    此前字符串形解析只置 has_media 并剥离 CQ 码，图片参数（file/
    url）全部丢失，字符串上报形态下视觉链路恒得空引用。现在与
    数组形同口径产出 ImageSegment（data 为 CQ 参数原样 dict），
    其余 CQ 码（face 等）维持只剥离、不建段的旧行为。

    已知降级：参数值若含未转义的 "]"（CQ 规范应写作 &#93;），
    匹配会在首个 "]" 处截断，该图退化为 file-only 引用，不崩溃、
    不误判文本；规范报文（NapCat 输出恒转义）不受影响。
    """
    segments: list[ImageSegment] = []
    for match in _CQ_IMAGE.finditer(raw_message):
        data: dict[str, str] = {}
        params = match.group(1)
        if params:
            for token in params.split(","):
                key, sep, value = token.partition("=")
                if sep and key:
                    data[key] = _cq_unescape(value)
        segments.append(ImageSegment(data))
    return tuple(segments)


def _parse_cq_at_segments(raw_message: str) -> tuple:
    """从 CQ 字符串中提取 at 段（2026-10-31 群回复接线 Review P3-1）。

    与 image 段同口径（见 _parse_cq_image_segments）：此前字符串形
    解析只剥离 CQ 码不建 AtSegment，群回复触发判定 is_group_trigger
    只认 AtSegment，字符串上报形态下 [CQ:at,qq=<self>] 恒不触发、
    群回复静默失效。现在产出 AtSegment（data 为 CQ 参数原样 dict，
    与数组形 {"qq": ...} 同形）；text/has_media 的产出不变。参数
    截断降级同 image：未转义 "]" 只会让该 at 段丢失，不误判文本。
    """
    segments: list[AtSegment] = []
    for match in _CQ_AT.finditer(raw_message):
        data: dict[str, str] = {}
        params = match.group(1)
        if params:
            for token in params.split(","):
                key, sep, value = token.partition("=")
                if sep and key:
                    data[key] = _cq_unescape(value)
        segments.append(AtSegment(data))
    return tuple(segments)


_TYPE_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,31}")
_CQ_NAME = re.compile(r"[a-zA-Z0-9_]+")

# N3 事件枚举白名单（全接口方案 §2.3 事件登记表，本机 bundle 核对口径）。
KNOWN_NOTICE_TYPES = frozenset(
    {
        "group_upload",
        "group_admin",
        "group_decrease",
        "group_increase",
        "group_ban",
        "friend_add",
        "group_recall",
        "friend_recall",
        "group_card",
        "group_msg_emoji_like",
        "essence",
        "online_file_receive",
        "online_file_send",
        "notify",
        "bot_offline",
    }
)

#: notify 的 sub_type 白名单。lucky_king/honor 为单源存疑（官方事件表
#: 在列、本机 bundle 无事件证据）：classify 接受、由下游标存疑，不得
#: 在分类层拒绝。
KNOWN_NOTIFY_SUB_TYPES = frozenset(
    {
        "poke",
        "lucky_king",
        "honor",
        "title",
        "profile_like",
        "input_status",
        "gray_tip",
        "group_name",
    }
)

KNOWN_META_TYPES = frozenset({"lifecycle", "heartbeat"})

KNOWN_REQUEST_TYPES = frozenset({"friend", "group"})
KNOWN_GROUP_REQUEST_SUB_TYPES = frozenset({"add", "invite"})

#: classify 可产出的 event_kind 集合（与 capabilities 声明同源）。
EVENT_KINDS = frozenset({"message", "notice", "request", "meta"})


def event_type_summary(raw: dict) -> str:
    """只记事件类型摘要（如 post_type=message/message_type=group），不含正文。

    类型值只接受受限字符集的字符串，其余（dict/list/超长/异形值）一律记
    固定标记 invalid_type——摘要会进日志，绝不插值未校验的载荷内容，
    否则任意构造的类型字段可把正文/标识写进 Ignore 日志（Codex P2 审）。
    N3 起摘要依次覆盖五个类型维度（post_type、message_type、
    notice_type、request_type、meta_event_type），同一校验口径。
    """
    parts = []
    if isinstance(raw, dict):
        for key in (
            "post_type",
            "message_type",
            "notice_type",
            "request_type",
            "meta_event_type",
        ):
            value = raw.get(key)
            if isinstance(value, str) and _TYPE_TOKEN.fullmatch(value):
                parts.append(f"{key}={value}")
            elif value is not None:
                parts.append(f"{key}=invalid_type")
    return "/".join(parts)


def _id_text(value, *, allow_negative: bool = False) -> str | None:
    """标识值单次归一化：仅接受 str/int（不含 bool），归一后为空即 None。

    容器/浮点/bool 等异形值一律判无效——str([]) 之类会绕过「非空」检查、
    却在构造事件时变成空用户，进而毒化 claim 与限流/记忆键（Codex P2 审）。
    校验与事件构造必须共用本函数的同一归一结果。账号类标识（user_id/
    self_id）恒为正数；message_id 按 OneBot 规范为有符号 int32，仅在
    allow_negative 时放行负数（Codex P2 复审），0 仍判无效。
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, int):
        if value == 0 or (value < 0 and not allow_negative):
            return None
        if allow_negative and not -(2**31) <= value <= 2**31 - 1:
            return None
    text = str(value).strip()
    return text or None


def _segment_data(segment: dict) -> dict:
    """消息段 data 归一化为 dict，非 dict 一律记空 dict。"""
    data = segment.get("data")
    return data if isinstance(data, dict) else {}


# 段 type → 一等段类（data 透传型）。text/node 有专门构造逻辑，
# json/xml/music/contact/location/markdown/miniapp 等已知段与未知段
# 一律 RawSegment（方案 §2.2）。
_TYPED_DATA_SEGMENTS = {
    "image": ImageSegment,
    "record": AudioSegment,
    "at": AtSegment,
    "reply": ReplySegment,
    "face": FaceSegment,
    "mface": MFaceSegment,
    "video": VideoSegment,
    "file": FileSegment,
    "poke": PokeSegment,
    "dice": DiceSegment,
    "rps": RpsSegment,
    "forward": ForwardSegment,
}


# receive-only 两段的 data 关键字段（bundle segment schema 定稿，
# docs/napcat-schema-evidence-n5.md §6）：flashtransfer 仅 fileSetId
# 必填；onlinefile 五字段全必填（msgId/elementId/fileName/fileSize
# 为 String、isDir 为 Boolean）。
_RECEIVE_ONLY_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "flashtransfer": ("fileSetId",),
    "onlinefile": ("msgId", "elementId", "fileName", "fileSize", "isDir"),
}


class _InvalidSegment(ValueError):
    """段结构不成立（如 receive-only 段缺关键字段）：消息判 malformed。"""


def _receive_only_data_valid(seg_type: str, data: dict) -> bool:
    required = _RECEIVE_ONLY_REQUIRED_FIELDS.get(seg_type, ())
    for field_name in required:
        value = data.get(field_name)
        if field_name == "isDir":
            if not isinstance(value, bool):
                return False
        elif not isinstance(value, str) or not value:
            return False
    return True


def _parse_segment(segment: dict) -> Segment:
    """单个原始段 dict → Segment（node 的 content 递归解析）。"""
    seg_type = segment.get("type")
    data = _segment_data(segment)
    if isinstance(seg_type, str) and seg_type in RECEIVE_ONLY_SEGMENT_TYPES:
        # receive-only 段：产出 RawSegment，但 data 关键字段不全
        # （或类型不对）即整条消息结构不成立，抛 _InvalidSegment
        # 由 normalize/classify 判 malformed（fail closed，不静默
        # 收下残缺的文件段）。
        if not _receive_only_data_valid(seg_type, data):
            raise _InvalidSegment(seg_type)
        return RawSegment(seg_type, data)
    if seg_type == "text":
        text = str(data.get("text", "")) if isinstance(segment.get("data"), dict) else ""
        return TextSegment(text)
    if seg_type == "node":
        raw_content = data.get("content")
        content: tuple = ()
        if isinstance(raw_content, list):
            content = tuple(
                _parse_segment(item) for item in raw_content if isinstance(item, dict)
            )
        return NodeSegment(data, content)
    cls = _TYPED_DATA_SEGMENTS.get(seg_type) if isinstance(seg_type, str) else None
    if cls is not None:
        return cls(data)
    return RawSegment(
        seg_type if isinstance(seg_type, str) else str(seg_type or ""), data
    )


def _parse_list_message(message: list) -> tuple[tuple, str, bool]:
    """消息段列表解析：与旧 parse_event 逐字等价，并同步建 Segment 元组。"""
    media = any(isinstance(s, dict) and s.get("type") != "text" for s in message)
    segments: list[Segment] = []
    texts: list[str] = []
    for segment in message:
        if not isinstance(segment, dict):
            continue
        parsed = _parse_segment(segment)
        if isinstance(parsed, TextSegment):
            texts.append(parsed.text)
        segments.append(parsed)
    return tuple(segments), "".join(texts), media


def _parse_string_message(raw_message: str) -> tuple[tuple, str, bool]:
    """字符串消息解析：CQ 码检测媒体并剥离，文本口径与旧版逐字等价。

    2026-10-31 接缝修复：image 型 CQ 码另行建 ImageSegment（见
    _parse_cq_image_segments），使字符串形的图片引用与数组形一样
    进入 InboundEvent.images；at 型 CQ 码同口径建 AtSegment（见
    _parse_cq_at_segments），使字符串形的群 @ 与数组形一样可被
    is_group_trigger 识别；text/has_media 的产出不变。
    """
    media = bool(re.search(_CQ_PATTERN, raw_message))
    text = re.sub(_CQ_STRIP, "", raw_message)
    segments = (TextSegment(text),) if text else ()
    segments = segments + _parse_cq_image_segments(raw_message)
    segments = segments + _parse_cq_at_segments(raw_message)
    return segments, text, media


def normalize_onebot_event(raw, *, require_keys: bool = True) -> InboundEvent | Ignore:
    """把 OneBot 原始事件归一化为 InboundEvent，不合格返回 Ignore。

    require_keys=False 供 perception 等宽松入口使用：message_id/self_id
    可缺失，但 user_id 缺失仍 Ignore(malformed)（KD-C07）。
    """
    if not isinstance(raw, dict):
        return Ignore(REASON_MALFORMED, "body:non_object")
    summary = event_type_summary(raw)
    if raw.get("message_type") != "private":
        return Ignore(REASON_NOT_PRIVATE, summary)
    message_id = _id_text(raw.get("message_id"), allow_negative=True)
    self_id = _id_text(raw.get("self_id"))
    user_id = _id_text(raw.get("user_id"))
    if require_keys and message_id is None:
        return Ignore(REASON_NO_MESSAGE_ID, summary)
    if require_keys and self_id is None:
        return Ignore(REASON_NO_SELF_ID, summary)
    if user_id is None:
        return Ignore(REASON_MALFORMED, summary)

    message = raw.get("message", "")
    raw_message = raw.get("raw_message", message)
    if isinstance(message, list):
        try:
            segments, text, media = _parse_list_message(message)
        except _InvalidSegment:
            return Ignore(REASON_MALFORMED, summary)
    elif isinstance(raw_message, str):
        segments, text, media = _parse_string_message(raw_message)
    else:
        return Ignore(REASON_UNSUPPORTED_TYPE, summary)

    return InboundEvent(
        platform="onebot",
        self_id=self_id or "",
        user_id=user_id,
        session_id=f"onebot:{user_id}",
        segments=segments,
        text=text,
        has_media=media,
        message_id=message_id,
        raw=raw,
        images=extract_image_refs(segments),
    )


def _sub_type_text(raw: dict) -> str | None:
    """sub_type 归一化：仅 str 保留，其余（缺失/异形）一律 None。"""
    value = raw.get("sub_type")
    return value if isinstance(value, str) else None


def _status_flags(raw: dict) -> tuple[bool | None, bool | None]:
    """status.online/good 提取：仅 dict 内的 bool 取值，其余一律 None。"""
    status = raw.get("status")
    if not isinstance(status, dict):
        return None, None
    online = status.get("online")
    good = status.get("good")
    return (
        online if isinstance(online, bool) else None,
        good if isinstance(good, bool) else None,
    )


def _classify_group_message(raw: dict, *, require_keys: bool) -> InboundEvent | Ignore:
    """群消息分支：键校验口径与私聊同源（_id_text + require_keys 两键），
    anonymous/notice 子类无稳定 user_id 与内容保证、解析宽容兜底，
    其余子类 user_id 与内容形态严判（方案 §2.3 群行）。"""
    summary = event_type_summary(raw)
    message_id = _id_text(raw.get("message_id"), allow_negative=True)
    self_id = _id_text(raw.get("self_id"))
    group_id = _id_text(raw.get("group_id"))
    user_id = _id_text(raw.get("user_id"))
    if require_keys and message_id is None:
        return Ignore(REASON_NO_MESSAGE_ID, summary)
    if require_keys and self_id is None:
        return Ignore(REASON_NO_SELF_ID, summary)
    if group_id is None:
        return Ignore(REASON_MALFORMED, summary)

    sub_type = _sub_type_text(raw)
    lenient = sub_type in ("anonymous", "notice")
    if user_id is None and not lenient:
        return Ignore(REASON_MALFORMED, summary)

    message = raw.get("message", "")
    raw_message = raw.get("raw_message", message)
    parsed: tuple[tuple, str, bool] | None = None
    if isinstance(message, list):
        try:
            parsed = _parse_list_message(message)
        except _InvalidSegment:
            if not lenient:
                return Ignore(REASON_MALFORMED, summary)
            parsed = ((), "", False)
    elif isinstance(raw_message, str):
        parsed = _parse_string_message(raw_message)
    if parsed is None:
        if not lenient:
            return Ignore(REASON_UNSUPPORTED_TYPE, summary)
        parsed = ((), "", False)
    segments, text, media = parsed

    return InboundEvent(
        platform="onebot",
        self_id=self_id or "",
        user_id=user_id or "",
        session_id=f"onebot:g{group_id}",
        segments=segments,
        text=text,
        has_media=media,
        message_id=message_id,
        raw=raw,
        chat_type="group",
        group_id=group_id,
        sub_type=sub_type or "normal",
        images=extract_image_refs(segments),
    )


def classify_onebot_event(raw, *, require_keys: bool = True) -> ClassifiedEvent | Ignore:
    """把 OneBot 原始事件分类为判别联合四型之一（方案 §3.3 / C1）。

    分工：消息类仍走 normalize 的严格级（reason 语义逐字不变）+ 群
    分支；notice/request/meta 走结构分类（类型枚举白名单校验），事件
    类不因缺 self_id 判 Ignore（黄金报文有缺 self_id 的 notice）。
    reason 分工：类型字段是合法 token 但不在已知枚举内 →
    unknown_event_type；结构本身不成立（非对象/缺必填键/类型字段
    非 str 或非 token 形）→ malformed
    （消息键缺失仍用 no_message_id/no_self_id 既有码）。
    方向先行：post_type=message_sent 的自身回流先于 message_type 分支
    定为 self_echo，永不进对话 claim 键空间（方案 §2.3 方向契约）。
    """
    if not isinstance(raw, dict):
        return Ignore(REASON_MALFORMED, "body:non_object")
    summary = event_type_summary(raw)
    post_type = raw.get("post_type")

    if post_type == "message_sent":
        online, good = _status_flags(raw)
        return MetaEvent(
            platform="onebot",
            self_id=_id_text(raw.get("self_id")) or "",
            meta_event_type="message_sent",
            sub_type=_sub_type_text(raw),
            direction="self_echo",
            message_id=_id_text(raw.get("message_id"), allow_negative=True),
            user_id=_id_text(raw.get("user_id")) or "",
            group_id=_id_text(raw.get("group_id")),
            status_online=online,
            status_good=good,
            raw=raw,
        )

    message_type = raw.get("message_type")
    if message_type is not None:
        if not isinstance(message_type, str) or not _TYPE_TOKEN.fullmatch(message_type):
            return Ignore(REASON_MALFORMED, summary)
        if message_type == "private":
            result = normalize_onebot_event(raw, require_keys=require_keys)
            if isinstance(result, Ignore):
                return result
            sub_type = _sub_type_text(raw)
            chat_type = (
                "group_temp"
                if sub_type == "group_temp" or "temp_source" in raw
                else "private"
            )
            return dataclasses.replace(
                result, chat_type=chat_type, sub_type=sub_type
            )
        if message_type == "group":
            return _classify_group_message(raw, require_keys=require_keys)
        return Ignore(REASON_UNKNOWN_EVENT_TYPE, summary)

    notice_type = raw.get("notice_type")
    if notice_type is not None or post_type == "notice":
        if not isinstance(notice_type, str) or not _TYPE_TOKEN.fullmatch(notice_type):
            return Ignore(REASON_MALFORMED, summary)
        if notice_type not in KNOWN_NOTICE_TYPES:
            return Ignore(REASON_UNKNOWN_EVENT_TYPE, summary)
        sub_type = _sub_type_text(raw)
        if notice_type == "notify" and raw.get("sub_type") is not None:
            if not isinstance(raw.get("sub_type"), str) or not _TYPE_TOKEN.fullmatch(
                raw.get("sub_type")
            ):
                return Ignore(REASON_MALFORMED, summary)
            if sub_type not in KNOWN_NOTIFY_SUB_TYPES:
                return Ignore(REASON_UNKNOWN_EVENT_TYPE, summary)
        user_id = _id_text(raw.get("user_id"))
        actor_id = _id_text(raw.get("operator_id"))
        return NoticeEvent(
            platform="onebot",
            self_id=_id_text(raw.get("self_id")) or "",
            notice_type=notice_type,
            sub_type=sub_type,
            user_id=user_id or "",
            group_id=_id_text(raw.get("group_id")),
            actor_id=actor_id,
            target_id=user_id if actor_id is not None else None,
            message_id=_id_text(raw.get("message_id"), allow_negative=True),
            raw=raw,
        )

    request_type = raw.get("request_type")
    if request_type is not None or post_type == "request":
        if not isinstance(request_type, str) or not _TYPE_TOKEN.fullmatch(request_type):
            return Ignore(REASON_MALFORMED, summary)
        if request_type not in KNOWN_REQUEST_TYPES:
            return Ignore(REASON_UNKNOWN_EVENT_TYPE, summary)
        flag = raw.get("flag")
        if not isinstance(flag, str) or not flag:
            return Ignore(REASON_MALFORMED, summary)
        user_id = _id_text(raw.get("user_id"))
        if user_id is None:
            return Ignore(REASON_MALFORMED, summary)
        if request_type == "group":
            if raw.get("sub_type") is not None and (
                not isinstance(raw.get("sub_type"), str)
                or not _TYPE_TOKEN.fullmatch(raw.get("sub_type"))
            ):
                return Ignore(REASON_MALFORMED, summary)
            sub_type = _sub_type_text(raw)
            if sub_type not in KNOWN_GROUP_REQUEST_SUB_TYPES:
                return Ignore(REASON_UNKNOWN_EVENT_TYPE, summary)
        else:
            sub_type = None
        return RequestEvent(
            platform="onebot",
            self_id=_id_text(raw.get("self_id")) or "",
            request_type=request_type,
            sub_type=sub_type,
            user_id=user_id,
            group_id=_id_text(raw.get("group_id")),
            flag=flag,
            raw=raw,
        )

    meta_event_type = raw.get("meta_event_type")
    if meta_event_type is not None or post_type == "meta_event":
        if not isinstance(meta_event_type, str) or not _TYPE_TOKEN.fullmatch(
            meta_event_type
        ):
            return Ignore(REASON_MALFORMED, summary)
        if meta_event_type not in KNOWN_META_TYPES:
            return Ignore(REASON_UNKNOWN_EVENT_TYPE, summary)
        online, good = _status_flags(raw)
        return MetaEvent(
            platform="onebot",
            self_id=_id_text(raw.get("self_id")) or "",
            meta_event_type=meta_event_type,
            sub_type=_sub_type_text(raw),
            message_id=_id_text(raw.get("message_id"), allow_negative=True),
            user_id=_id_text(raw.get("user_id")) or "",
            group_id=_id_text(raw.get("group_id")),
            status_online=online,
            status_good=good,
            raw=raw,
        )

    if isinstance(post_type, str) and _TYPE_TOKEN.fullmatch(post_type):
        return Ignore(REASON_UNKNOWN_EVENT_TYPE, summary)
    return Ignore(REASON_MALFORMED, summary)


def _cq_escape(value: str) -> str:
    """CQ 参数值转义（& 先行，再转 [ ] ,）。"""
    return (
        value.replace("&", "&amp;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace(",", "&#44;")
    )


def _cq_segment(seg_type: str, primary_key: str | None, data: dict) -> str | None:
    """把一段翻译为单个 CQ 码：类型名与参数名必须过令牌文法（防注入
    额外 CQ 段），参数全量序列化（主参数在前、其余按名排序），值只接受
    标量；任一不合规返回 None（Codex P2 复审）。"""
    if not _CQ_NAME.fullmatch(seg_type):
        return None
    keys = list(data)
    if primary_key is not None:
        if primary_key not in data or str(data[primary_key]) == "":
            return None
        keys = [primary_key] + sorted(key for key in keys if key != primary_key)
    rendered = []
    for key in keys:
        if not isinstance(key, str) or not _CQ_NAME.fullmatch(key):
            return None
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        rendered.append(f",{key}={_cq_escape(str(value))}")
    return f"[CQ:{seg_type}{''.join(rendered)}]"


def _segments_to_onebot_text(segments) -> str | None:
    """把 Segment 序列翻译为 OneBot 字符串报文（文本 + CQ 码）。

    任一段无法翻译（媒体段缺 file、At 段缺 qq、类型/参数名不合文法、
    参数值非标量）返回 None，由调用方在发送前 rejected，不静默丢段、
    不丢可保留的发送参数（如图片 type=flash、语音 magic）。
    N2 起 CQ 可表达的一等段扩到 reply/face/mface/video/dice/rps；
    file/forward/node 与带嵌套值的 Raw 段不在 CQ 面内（走数组报文，
    见 segments_to_onebot_message）。
    """
    parts: list[str] = []
    for seg in segments:
        if isinstance(seg, TextSegment):
            parts.append(seg.text)
            continue
        if isinstance(seg, ImageSegment):
            built = _cq_segment("image", "file", seg.data)
        elif isinstance(seg, AudioSegment):
            built = _cq_segment("record", "file", seg.data)
        elif isinstance(seg, AtSegment):
            built = _cq_segment("at", "qq", seg.data)
        elif isinstance(seg, ReplySegment):
            built = _cq_segment("reply", "id", seg.data)
        elif isinstance(seg, FaceSegment):
            built = _cq_segment("face", "id", seg.data)
        elif isinstance(seg, MFaceSegment):
            built = _cq_segment("mface", "id", seg.data)
        elif isinstance(seg, VideoSegment):
            built = _cq_segment("video", "file", seg.data)
        elif isinstance(seg, DiceSegment):
            built = _cq_segment("dice", None, seg.data)
        elif isinstance(seg, RpsSegment):
            built = _cq_segment("rps", None, seg.data)
        elif isinstance(seg, PokeSegment):
            # poke 的 CQ 形态以 qq 为主参数；只有 id 等其他形态时
            # 不可 CQ 化，交数组报文。
            built = _cq_segment("poke", "qq", seg.data) if "qq" in seg.data else None
        elif isinstance(seg, RawSegment):
            # receive-only 段（flashtransfer/onlinefile）明确拒绝：
            # 平台 ob11→raw 转换器为空实现，不可经消息段发送——
            # 与未知/不可序列化段同一口径返回 None（发送前 rejected）。
            if seg.type in RECEIVE_ONLY_SEGMENT_TYPES:
                return None
            built = _cq_segment(seg.type, None, seg.data)
        else:
            return None
        if built is None:
            return None
        parts.append(built)
    return "".join(parts)


# 一等段类 → 平台段 type 名（数组报文序列化用）。
_SEGMENT_TYPE_NAMES = {
    ImageSegment: "image",
    AudioSegment: "record",
    AtSegment: "at",
    ReplySegment: "reply",
    FaceSegment: "face",
    MFaceSegment: "mface",
    VideoSegment: "video",
    FileSegment: "file",
    PokeSegment: "poke",
    DiceSegment: "dice",
    RpsSegment: "rps",
    ForwardSegment: "forward",
}

# 数组报文同样强制主参数存在（与 CQ 路径同一口径）：缺主参数的段
# 两种形态都不可序列化，不许借数组形态绕过「缺 file 即拒」。
_ARRAY_PRIMARY_KEYS = {
    "image": "file",
    "record": "file",
    "at": "qq",
    "reply": "id",
    "face": "id",
    "mface": "id",
    "video": "file",
}


def _json_shaped(value) -> bool:
    """值是否为可 JSON 序列化的标量/容器（数组报文 data 的准入）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_json_shaped(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _json_shaped(item)
            for key, item in value.items()
        )
    return False


def _segment_data_for_array(seg_type: str, data, *, allow_nested: bool) -> dict | None:
    """数组报文的 data 校验：键名过令牌文法；值默认只许标量，
    allow_nested（Raw 段的 json 载荷等）时许 JSON 形容器。"""
    if not isinstance(data, dict):
        return None
    for key, value in data.items():
        if not isinstance(key, str) or not _CQ_NAME.fullmatch(key):
            return None
        if allow_nested:
            if not _json_shaped(value):
                return None
        elif isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
    return dict(data)


def _raw_contains_receive_only(value) -> bool:
    """递归扫描 Raw 段 data：任意层 dict 的 type 命中 receive-only
    段型即 True（Review P3-8 的嵌套夹带检查）。"""
    if isinstance(value, dict):
        seg_type = value.get("type")
        if isinstance(seg_type, str) and seg_type in RECEIVE_ONLY_SEGMENT_TYPES:
            return True
        return any(_raw_contains_receive_only(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_raw_contains_receive_only(item) for item in value)
    return False


def _segment_to_array_item(seg) -> dict | None:
    """单个 Segment → segment 数组报文的一项；不可序列化返回 None。"""
    if isinstance(seg, TextSegment):
        return {"type": "text", "data": {"text": seg.text}}
    if isinstance(seg, NodeSegment):
        # 节点属性（去掉原始 content 键）+ content 递归序列化。
        attrs = {k: v for k, v in seg.data.items() if k != "content"}
        checked = _segment_data_for_array("node", attrs, allow_nested=False)
        if checked is None:
            return None
        content_items = []
        for item in seg.content:
            built = _segment_to_array_item(item)
            if built is None:
                return None
            content_items.append(built)
        if content_items:
            checked["content"] = content_items
        return {"type": "node", "data": checked}
    if isinstance(seg, RawSegment):
        # receive-only 段出站拒绝（数组形态同样不可绕，见 CQ 路径注记）。
        if seg.type in RECEIVE_ONLY_SEGMENT_TYPES:
            return None
        # 手工构造的 Raw node/forward 不得借 content 嵌套夹带
        # receive-only 段绕过（Review P3-8）：NodeSegment 路径有
        # 递归拒绝，Raw 路径在此补齐同一不变量。
        if seg.type in {"node", "forward"} and _raw_contains_receive_only(seg.data):
            return None
        if not _CQ_NAME.fullmatch(seg.type):
            return None
        checked = _segment_data_for_array(seg.type, seg.data, allow_nested=True)
        if checked is None:
            return None
        return {"type": seg.type, "data": checked}
    seg_type = _SEGMENT_TYPE_NAMES.get(type(seg))
    if seg_type is None:
        return None
    checked = _segment_data_for_array(seg_type, seg.data, allow_nested=False)
    if checked is None:
        return None
    primary = _ARRAY_PRIMARY_KEYS.get(seg_type)
    if primary is not None and (
        primary not in checked or str(checked[primary]) == ""
    ):
        return None
    return {"type": seg_type, "data": checked}


def segments_to_onebot_message(segments) -> str | list | None:
    """Segment 序列 → OneBot 报文（方案 §3.1 编码选择是段结构的函数）。

    全部段可 CQ 表达时返回 CQ 字符串（与历史口径逐字一致）；否则
    返回 segment 数组报文（forward/node 等嵌套结构不用 CQ 拼接）；
    任一段两种形态都不可序列化返回 None（调用方发送前 rejected）。

    两条安全口径（Codex Review N2 #1/#8）：
    - 输入先物化为 tuple：迭代器只遍历一次，禁止 CQ 尝试消费后
      数组回退拿到空序列、静默丢段。
    - TextSegment 正文含 ``[CQ:`` 时强制数组报文：CQ 字符串形态下
      该文本会被平台解析成真正的段（媒体门与复核都按纯文本放行，
      等于注入）；数组形态的 text 段是纯数据，永不被 CQ 解析。
      不含该形态的文本口径与历史逐字一致。
    """
    segments = tuple(segments)
    if any(
        isinstance(seg, TextSegment) and "[CQ:" in seg.text for seg in segments
    ):
        items = []
        for seg in segments:
            item = _segment_to_array_item(seg)
            if item is None:
                return None
            items.append(item)
        return items
    text = _segments_to_onebot_text(segments)
    if text is not None:
        return text
    items = []
    for seg in segments:
        item = _segment_to_array_item(seg)
        if item is None:
            return None
        items.append(item)
    return items


def verify_onebot_signature(body: bytes, signature: str | None, secret: str | None) -> bool:
    """验证 OneBot X-Signature: sha1=<hex>，与 ingress.verify_signature 同逻辑。"""
    if not secret or not signature:
        return False
    expected = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(signature.strip(), expected)


class OneBotAdapter:
    """经 HTTP API 对接 NapCat 的 OneBot v11 适配器。"""

    name = "onebot"
    capabilities = AdapterCapabilities(
        segment_types=SUPPORTED_SEGMENT_TYPES,
        event_kinds=EVENT_KINDS,
        action_levels=build_action_levels(),
    )

    def __init__(self, client=None, base_url: str = "http://127.0.0.1:3000"):
        self.client = client
        self.base_url = base_url.rstrip("/")
        self._own_transport = None
        self._own_actions = None

    async def start(self) -> None:
        """无连接状态，no-op。"""

    async def stop(self) -> None:
        """无连接状态，no-op。"""

    def normalize(self, raw: dict, *, require_keys: bool = True) -> InboundEvent | Ignore:
        """委托 normalize_onebot_event。"""
        return normalize_onebot_event(raw, require_keys=require_keys)

    def classify(self, raw, *, require_keys: bool = True) -> ClassifiedEvent | Ignore:
        """委托 classify_onebot_event（判别联合全事件面分类）。"""
        return classify_onebot_event(raw, require_keys=require_keys)

    def _ensure_client(self):
        """client 未注入时懒构造 HttpOneBotClient（避免模块级循环依赖）。"""
        if self.client is None:
            from companion.tools import HttpOneBotClient

            self.client = HttpOneBotClient(self.base_url)
        return self.client

    def _transport(self):
        """出站传输：client 自带 transport 时共用之，否则按 base_url 自建。

        base 口径与收敛前 health 一致：优先 client.base_url、缺失时用
        适配器自身 base_url。自建 transport 缓存复用，不逐次新建。
        """
        transport = getattr(self.client, "transport", None)
        if transport is not None:
            return transport
        if self._own_transport is None:
            base = getattr(self.client, "base_url", None) or self.base_url
            self._own_transport = OneBotTransport(str(base).rstrip("/"))
        return self._own_transport

    @property
    def actions(self):
        """分组类型化门面（方案 §3.1）：client 具备时用 client 的
        ActionsBundle（与发送路径共用同一 ActionGateway）；否则用自身
        base_url 建 transport+gateway 后构造并缓存。"""
        client_actions = getattr(self.client, "actions", None)
        if client_actions is not None:
            return client_actions
        if self._own_actions is None:
            from companion.adapters.actions import ActionsBundle
            from companion.adapters.gateway import ActionGateway

            self._own_actions = ActionsBundle(ActionGateway(self._transport()))
        return self._own_actions

    async def send_private(
        self, session_id: str, segments, *, delivery: DeliveryHandle | None = None
    ) -> SendReceipt:
        """发送一条私聊；回执映射经 transport.receipt_from_result（全仓
        唯一实现，与 DeliveryService 同口径），从不重试。

        客户端抛异常 → ambiguous；结果 dict 的三态映射由
        receipt_from_result 独占。非文本段经 CQ 码翻译进报文，
        无法翻译的段在调用客户端前即 rejected，绝不静默丢弃后谎报
        accepted（Codex P2 审）。
        """
        user_id = session_id.split(":", 1)[-1]
        segments = tuple(segments)
        # 出站可见文本复核（N2，Codex Review #2）：适配器直发路径
        # 同样不得绕过内容规则——node/json 等富段的可见文本与纯
        # 文本同一套 review 口径。懒导入避免 adapters ↔ services
        # 的模块级循环（services 依赖 adapters.base）。
        from companion.services.segments import review_outbound_segments

        allowed, _reason = review_outbound_segments(segments)
        if not allowed:
            return SendReceipt("rejected")
        message = segments_to_onebot_message(segments)
        if message is None:
            return SendReceipt("rejected")
        priority = delivery.priority if delivery is not None else "normal"
        kwargs = {}
        if delivery is not None and delivery.reservation_token is not None:
            # token 只在 DeliveryService 已预留时出现，适配器不自行计费。
            kwargs["quota_token"] = delivery.reservation_token
        try:
            result = await self._ensure_client().send_private_msg(
                user_id, message, priority=priority, **kwargs
            )
        except Exception:  # noqa: BLE001 — 结果不明一律 ambiguous，绝不猜 accepted
            return SendReceipt("ambiguous")
        return receipt_from_result(result)

    async def health(self) -> AdapterHealth:
        """只读探针：经 transport 以 GET 动作 get_status 探活，解析
        data.online/good（顶层亦兼容）。

        good 直接取自 get_status 载荷、非硬编码（NapCat 的 good 本身为其
        上报值，盲区注记见方案 §3.6）；任何异常记为不在线、detail 只记
        异常类型名、不带载荷。
        """
        try:
            payload = await self._transport().get_action("get_status")
        except Exception as exc:  # noqa: BLE001 — 探针失败即不健康，不上抛
            return AdapterHealth(False, False, detail=type(exc).__name__)
        payload = payload if isinstance(payload, dict) else {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        online = data.get("online")
        if online is None:
            online = payload.get("online")
        good = data.get("good")
        if good is None:
            good = payload.get("good")
        return AdapterHealth(bool(online), bool(good))
