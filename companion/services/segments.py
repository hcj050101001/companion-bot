"""出站段级发送规划与递归复核（方案 C9 / C5(b)，N2 落地）。

规划（C9）：出站计划由「文本气泡 + 非文本段」组成——多气泡拆分策略
只作用于纯文本运行段，非文本段原子、不可拆、保持相对顺序；群发送
不拆文本（split_text=False，群消息形态由调用方负责，方案 §3.2）。

复核（C5(b) 出站侧）：「框架构造」不等于内容安全——复核递归遍历
全部可见文本（TextSegment 正文、forward/node 节点树、json 卡片的
文本字段、文件标题/名），与纯文本同一套 review_bubbles 规则；
非文本载体（图/语音/视频/文件/转发/Raw 段）只放行两类来源——框架
确定性生成或经登记的已审资产——在投递面以 allow_media 显式开启
表达，默认关闭（模型输出的媒体/Raw/转发段默认不可达）。

提取实现与网关共用 companion.content_extract（复审 P3）：本层
旧版自维护的 8 键可见集与递归收集已整体删除——私发独审路径
（send_private_msg / onebot 直发）与网关从此同一口径、同一
渲染顺序、同一不可审信号，禁止再在本层维护第二份键集/提取。
"""

import os

from companion.adapters.base import (
    AtSegment,
    AudioSegment,
    FileSegment,
    ForwardSegment,
    ImageSegment,
    NodeSegment,
    RawSegment,
    ReplySegment,
    TextSegment,
    VideoSegment,
)
from companion.content_extract import (
    PayloadUnreviewable,
    fallback_visible_texts,
    payload_visible_texts,
)
from companion.multipart import review_bubbles, split_plain_text

#: 前缀段：reply / at 是紧随其后那条消息的元数据，不独立成条
#: （Codex Review N2 #7）。
_PREFIX_SEGMENT_TYPES = (ReplySegment, AtSegment)

#: 受媒体门管控的段类：本体为非文本载体（Raw 段按 C5(b) 一律在内，
#: 含 json 卡片——其文本过复核，但段本身默认关闭、须显式开启）。
MEDIA_SEGMENT_TYPES = (
    ImageSegment,
    AudioSegment,
    VideoSegment,
    FileSegment,
    ForwardSegment,
    RawSegment,
)

def contains_media_segment(segments) -> bool:
    """段序列（递归含 node 内容）是否含受媒体门管控的段。"""
    for seg in segments:
        if isinstance(seg, MEDIA_SEGMENT_TYPES):
            return True
        if isinstance(seg, NodeSegment) and contains_media_segment(seg.content):
            return True
    return False


def visible_texts(segments) -> list[str]:
    """提取段序列的全部可见文本（复核口径，顺序保持）。

    提取实现与网关共用 companion.content_extract（复审 P3）：
    可见键全集、大小写/填充归一、重复键保对、XML 真解析、
    深度界与不可审信号全在一处，本函数只做段对象 → 原始段
    dict 的形态适配。顺序口径与共享层一致（收口复审 P2）：
    提取顺序逐字等于序列化顺序——dict 形态按 data 插入序
    单遍扫描；typed 形态按各类型序列化字段序：TextSegment
    取正文；NodeSegment 的 data 按插入序逐键（序列化器对
    属性按插入序透传、仅剔精确 'content'）、类型化 content
    最后（序列化器把 content 追加在属性之后）；FileSegment
    同样逐键单遍、basename 在 file 键原位；RawSegment/
    ForwardSegment 的 data 原样交共享提取按 type 分发；其
    余带 data 的段走共享回落提取。

    提取不完时共享层抛 PayloadUnreviewable，本函数不吞信
    号（由 review_outbound_segments 转 fail-closed 拦截）。
    """
    out: list[str] = []
    for seg in segments:
        if isinstance(seg, TextSegment):
            if seg.text:
                out.append(seg.text)
        elif isinstance(seg, NodeSegment):
            # 单遍扫描 seg.data（序列化序，收口复审 P2）：序
            # 列化器（onebot._segment_to_array_item）对 node
            # 属性按 data 插入序透传、仅剔精确 'content' 键，
            # 类型化 content 序列化时追加在全部属性之后——
            # 提取同序：普通字段逐键在本位交共享 node 口径，
            # content 变体键在本位纳入，类型化 content 最后。
            for key, value in seg.data.items():
                if isinstance(key, str) and key.strip().lower() == "content":
                    # content 键（含大小写/填充变体）恒纳入
                    # 复核（收口复审 P3）：序列化器只剔精确
                    # 'content'，'Content'/' CONTENT ' 等变体
                    # 原样进报文，旧口径只在类型化 content 为
                    # 空时才扫、在非空时整段脱检。唯一例外是
                    # 精确 'content' 键且类型化 content 非空：
                    # 序列化器剔除它并以类型化内容重建（不进
                    # 报文），不重复纳入。
                    if key == "content" and seg.content:
                        continue
                    out.extend(payload_visible_texts(value))
                else:
                    out.extend(
                        payload_visible_texts(
                            [{"type": "node", "data": {key: value}}]
                        )
                    )
            out.extend(visible_texts(seg.content))
        elif isinstance(seg, FileSegment):
            # 单遍扫描 seg.data（序列化序，收口复审 P3）：
            # 序列化器对 file 段 data 按插入序透传——旧实现
            # 把 name 强制置于全部字段之前，title 在 name 之
            # 前插入时拆分短语被反序拼装逃审。逐键在本位提
            # 取：普通键交共享回落（type='file' 口径）；file
            # 键在本位出 basename（本层文件名语义：精确
            # 'name' 键有效时文件名以 name 为准、file 串值
            # 本身不入审查集，与共享回落对非可见键串值的
            # 口径一致）。
            # 语义角色只认序列化语义（serde 序复审 P2）：
            # 「文件名已由 name 给出」是语义判定，序列化器
            # 与平台只认精确键 'name'——'Name'/' name ' 等
            # 变体只是原样透传的惰性附加属性，不替代由
            # 'file' 派生的文件名。归一匹配只许扩大审查覆
            # 盖（变体键值照常按普通可见值送审），永不许
            # 用来跳过另一个值的审查：旧实现以归一判定
            # has_name，变体键即可把违禁 basename 顶出审
            # 查集。故 has_name 只认精确键 + 非空 str。
            has_name = isinstance(seg.data.get("name"), str) and bool(
                seg.data["name"]
            )
            for key, value in seg.data.items():
                # 'file' 键的归一分发只决定本键自身值的审查
                # 形态（出 basename），不跳过任何其它值——属
                # 「归一扩覆盖」，与上方 has_name 的语义判定
                # 严格区分（serde 序复审 P2 清查裁决）。
                if (
                    isinstance(key, str)
                    and key.strip().lower() == "file"
                    and isinstance(value, str)
                ):
                    if not has_name and "://" not in value:
                        base = os.path.basename(value)
                        if base:
                            out.append(base)
                    continue
                out.extend(
                    payload_visible_texts(
                        [{"type": "file", "data": {key: value}}]
                    )
                )
        elif isinstance(seg, RawSegment):
            out.extend(
                payload_visible_texts([{"type": seg.type, "data": seg.data}])
            )
        elif isinstance(seg, ForwardSegment):
            out.extend(
                payload_visible_texts([{"type": "forward", "data": seg.data}])
            )
        else:
            data = getattr(seg, "data", None)
            if isinstance(data, dict):
                out.extend(fallback_visible_texts(data))
    return out


def review_outbound_segments(
    segments,
    *,
    user_text: str = "",
    identity_name: str = "阿年",
    identity_model: str = "DeepSeek",
) -> tuple[bool, str]:
    """出站段级递归复核：可见文本与纯文本同一套 review 规则。

    无可见文本时通过（纯媒体段由媒体门另行管控）；有文本时以
    enforce_size=False 走 review_bubbles（段发送有自己的长度口径，
    内容规则——身份否认/自伤鼓励/危机词组——逐项同纯文本）。

    提取不可审时 fail-closed 拦截（复审 P3）：共享提取抛
    PayloadUnreviewable（超深/DTD 等）、或深层结构让收集抛
    RecursionError，都不许穿透到发送路径——两个调用点
    （DeliveryService / onebot 直发）均未对复核异常设防，
    异常穿出既是可用性洞、也绕过「审不过即拦」的门面。
    """
    try:
        texts = visible_texts(segments)
    except (PayloadUnreviewable, RecursionError):
        return False, "unreviewable_payload"
    if not texts:
        return True, ""
    return review_bubbles(
        texts,
        user_text=user_text,
        identity_name=identity_name,
        identity_model=identity_model,
        enforce_size=False,
    )


def plan_segment_sends(segments, *, split_text: bool) -> list[tuple]:
    """段级发送规划：段序列 → 发送单元列表（每单元一个段元组）。

    连续 TextSegment 合并为文本运行段：split_text 时按 split_plain_text
    的确定性规则拆分（拆不开则整段一条），否则整段一条；非文本段各自
    成单元、原子不可拆；全部单元保持原始相对顺序。
    """
    units: list[tuple] = []
    buffer: list[str] = []
    prefix: list = []

    def flush() -> None:
        if not buffer:
            return
        text = "".join(buffer)
        buffer.clear()
        if not text:
            return
        if split_text:
            parts = split_plain_text(text) or [text]
        else:
            parts = [text]
        # 前缀段只绑第一单元：引用/@ 指向整条回复的开头，
        # 后续分泡是同一条消息的续段。
        for index, part in enumerate(parts):
            if index == 0 and prefix:
                units.append((*prefix, TextSegment(part)))
                prefix.clear()
            else:
                units.append((TextSegment(part),))

    for seg in segments:
        if isinstance(seg, TextSegment):
            buffer.append(seg.text)
        elif isinstance(seg, _PREFIX_SEGMENT_TYPES):
            flush()
            prefix.append(seg)
        else:
            flush()
            if prefix:
                units.append((*prefix, seg))
                prefix.clear()
            else:
                units.append((seg,))
    flush()
    # 尾随悬空前缀（补审第三段 P3）：reply/at 前缀只能依附其后的
    # 发送单元，序列末尾无后续内容时直接丢弃，绝不单独成单元——
    # 否则投递层会为无内容报文多占一个额度槽并派发一条空消息、
    # 污染聚合回执。前缀-only 输入由此产出零单元，由投递层按
    # 空消息拒绝。
    return units
