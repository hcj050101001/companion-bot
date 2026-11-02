"""入站事件分类常量（方案 §3.3）：event_kind 与 scene 的固定取值。

本模块只放常量：纯领域层纪律不变（无 IO、无 companion 内部依赖）。
event_kind 由适配器 classify 写入判别联合的对应类型；scene 由分发处
按事件来源写入 EventContext，Stage 子链按 scene 选择。
"""

EVENT_KIND_MESSAGE = "message"
EVENT_KIND_NOTICE = "notice"
EVENT_KIND_REQUEST = "request"
EVENT_KIND_META = "meta"

SCENE_PRIVATE_CHAT = "private_chat"
SCENE_GROUP_CHAT = "group_chat"
SCENE_EVENT_NOTICE = "event_notice"
SCENE_EVENT_REQUEST = "event_request"
SCENE_SYSTEM = "system"
