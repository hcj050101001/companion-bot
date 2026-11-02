"""感知层：把 OneBot 事件解析为统一输入。

P2 起解析实现收归适配器层（companion.adapters.onebot），本模块只作
宽松入口转发：require_keys=False 时 message_id/self_id 可缺失，但
user_id 缺失会 Ignore 并返回 None（与旧行为的唯一差异，KD-C07 预期）。

2026-10-31 接缝修复：ImageRef 与 extract_image_refs 下沉到适配器
契约层（companion.adapters.base），图片引用成为 normalize 的契约
产出（InboundEvent.images）；本模块 re-export 两者以保持既有导入
路径不变，parse_event 直接取事件自带的 images、不再重提取。
"""

from dataclasses import dataclass

from companion.adapters.base import Ignore, ImageRef, extract_image_refs
from companion.adapters.onebot import normalize_onebot_event

__all__ = [
    "ImageRef",
    "UserMessage",
    "extract_image_refs",
    "parse_event",
    "parse_image",
    "parse_audio",
]


@dataclass(frozen=True)
class UserMessage:
    """已解析的私聊文本消息。"""

    user_id: str
    text: str
    message_id: str | None = None
    has_media: bool = False
    images: tuple[ImageRef, ...] = ()


def parse_event(event: dict) -> UserMessage | None:
    """解析 OneBot 字符串或消息段格式的私聊；Ignore 一律返回 None。"""
    result = normalize_onebot_event(event, require_keys=False)
    if isinstance(result, Ignore):
        return None
    return UserMessage(
        result.user_id,
        result.text,
        result.message_id,
        result.has_media,
        result.images,
    )


def parse_image(_event: dict):
    """图片感知接口（W2）。

    方案 §10 称存根在新结构中删除，但本阶段（P2）不删、避免超范围，
    删除留待后续立项时处理。
    """
    raise NotImplementedError("image perception is planned for W2")


def parse_audio(_event: dict):
    """语音感知接口（W2）。

    方案 §10 称存根在新结构中删除，但本阶段（P2）不删、避免超范围，
    删除留待后续立项时处理。
    """
    raise NotImplementedError("audio perception is planned for W2")
