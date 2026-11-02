"""出站内容审查的共享提取层（网关与 services 投递层共用）。

历史上两个审查层各维护一份可见键集与提取实现，口径漂移出
等价形态绕过（复审 P3：services 私发独审路径丢弃网关已覆盖
的可见键、键集 8 与 15 不一致）。本模块是唯一的提取实现：
原始载荷（str / CQ 串 / segment dict / 任意 JSON 形结构）
→ 可见文本片段列表；提取不完（超深、DTD、裸串 data 等）
抛 PayloadUnreviewable，由调用层 fail-closed 拦截。判定
规则本身在 companion.multipart.review_bubbles（只按片段的
真实顺序审一次）。

顺序口径（收口复审 P2 定稿）：**提取顺序逐字等于序列化
顺序**。dict 形态段的字段排放一律取 payload 字典自身的
插入序——序列化端（onebot 序列化器对 data 按插入序透
传、网关对原始载荷原样转发）与提取端自此同源同序；可见
键的成员资格仍由归一键集判定，顺序不再有任何硬编码规范
序或本分字段置前。typed 形态段（dataclass）的字段序由
services 适配按各类型的序列化字段序对齐（NodeSegment：
data 插入序 + 类型化 content 末尾，与序列化器把 content
追加在属性之后一致）。曾用「规范序提取 + review 双序送
审兜底」的组合已废除：双序被复现证明误拦顺序确定的良性
回复，规范序则被复现证明与序列化序不一致、拆分短语按
序列化序拼装时整段逃审——顺序问题只能在提取层与序列
化逐字对齐，不能靠多审一遍掩盖。
"""

import html
import json
import re
from xml.etree import ElementTree as ET

_XML_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
#: 畸形 XML 回落送审前的标签标记剥除（复审 P3）：只剥 <...> 形
#: 标记本身，字符数据原样保留并恢复连续（见 _xml_visible_texts）。
_XML_TAG_STRIP_RE = re.compile(r"<[^>]+>")
#: 无类型/未知类型段回退提取的可见键集。审计口径（复审 P3）：
#: 以 OneBot/NapCat 标准段的渲染文本字段为准逐段核对——share
#: 的 title/content、music（custom）的 title/content、location
#: 的 title/content、at/poke 的 name、image/record/video/file
#: 的 name、image/mface 的 summary 均已在册；本轮补入漏网的
#: music singer、location address、share 等卡片的 description
#: （投递层旧可见键口径本有此键、网关侧缺失），以
#: 及 onlinefile 的 fileName（键名小写比对命中 filename）。
#: url/audio/image/id/lat/lon 等链接与数值字段不是渲染文本，
#: 不入册。
_PAYLOAD_VISIBLE_KEYS = frozenset(
    {
        "text", "content", "name", "nickname", "title", "prompt", "desc",
        "description", "summary", "app", "template", "uin",
        "address", "singer", "filename",
    }
)


class _PayloadUnreviewable(Exception):
    """载荷提取不完整、无法完整送审：网关据此 fail-closed 拦截。

    提取层三条原理（复审原理收口轮）：全文本（任何形态里的可
    见文本都进审查）、超限即拦（提取不完不静默丢弃，抛本信号
    由网关判 content_review_blocked）、有界遍历（任何输入不许
    让 invoke 抛异常穿出——深度以 _PAYLOAD_MAX_DEPTH 为界，
    在解释器爆栈之前先触发本信号）。
    """


#: 段结构嵌套提取的深度上限（node/forward 子内容层数）：合法
#: 段结构只有个位数层；每超一层递归约 3–4 帧，64 层约 256
#: 帧，连同 invoke 自身栈深仍远低于解释器默认上限——超限必
#: 先触发 _PayloadUnreviewable、不会先 RecursionError。
_PAYLOAD_MAX_DEPTH = 64


def _payload_all_strings(value) -> list[str]:
    """收集任意 JSON 形结构里的全部字符串（json 卡片载荷用）。

    本层即两审查层共用的全量收集实现：全量收集、无深度
    上限（复审 P3：旧版 depth>8 静默返回 [] 是
    fail-open，深层字符串整段逃审）。实现差异（有意分叉、
    检出口径一致）：本函数用迭代遍历而非递归，任意深度不占
    调用栈；并以容器 id 防环——自引用结构不无限遍历，同一
    子结构重复出现只收一次（复核只需检出存在性）。
    """
    out: list[str] = []
    seen: set[int] = set()
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            if current:
                out.append(current)
            continue
        if isinstance(current, (dict, list, tuple)):
            marker = id(current)
            if marker in seen:
                continue
            seen.add(marker)
            children = (
                current.values() if isinstance(current, dict) else current
            )
            # 逆序入栈，使出栈顺序与原递归 DFS 一致。
            stack.extend(reversed(list(children)))
    return out


def _xml_character_data(element) -> str:
    """元素子树的字符数据（text/tail）按文档顺序拼成的整串。

    递归口径：元素自身文本在前，其后逐个子元素「子树字符
    数据 + 该子元素的尾文本」——与渲染时的字符流顺序一致。
    XML 嵌套深度由解析器与调用栈共同有界（invoke 对
    RecursionError 有 fail-closed 兜底），不另设深度界。
    """
    parts = [element.text or ""]
    for child in element:
        parts.append(_xml_character_data(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _xml_visible_texts(raw: str) -> list[str]:
    """xml 段的可见文本（复审 P2）：真解析、全文本提取。

    弃用正则剥标签——属性值（卡片摘要 brief 等）与 CDATA 内
    容都会被平台渲染为可见文本，正则剥标签却把它们连同标签
    一起丢弃，违禁文本借此整段逃审。ElementTree 真解析：元
    素文本、尾文本与全部属性值并入提取；CDATA 由解析器解包
    为字面文本、实体引用由解析器解码，不留编码藏词的口子。

    带 DTD 的输入判不可审（复审 P3）：含 DOCTYPE/ENTITY 声
    明时，违禁短语可拆进多条内部实体定义、靠平台渲染时才拼
    装成形——本层不展开 DTD 内部实体（自行展开既复杂又引入
    实体扩展风险），原文回落送审则拼不出连续短语、等于漏
    审。故此类输入直接抛 _PayloadUnreviewable 由网关拦截
    （fail-closed）：合法 QQ xml 卡片不需要 DTD，为它留面
    的收益为零。

    非 DTD 的解析失败回落「原文整体送审」：格式错误的 XML
    平台侧也无法按卡片渲染，明文字面内容整体过审即可（此
    回落只对无 DTD 的字面内容成立，不再声称覆盖实体拼装形
    态）。不取「判不可审拦截」：格式错误不等于内容违禁，
    整体送审已保证无字面内容漏审。回落前先剥除标签标记
    （复审 P3）：多根等畸形 XML 里被标签隔断的拆字短语
    （<t>自</t><t>杀</t>）在带标签的原文里拼不成连续文本、
    等于漏审；剥标签后字符数据恢复连续，与良构路径「元素
    文本拼接送审」同口径。处理顺序为 CDATA 解包 → 剥标签
    → 实体解码：解码置于剥除之后，&lt; 等转义尖括号按字面
    文本保留送审，不被误当标签剥掉。

    良构路径的产出顺序（复审 P2，顺序/连续性收口）：判定对
    象是按渲染顺序拼成的文本流，不是无序片段袋——
    (1) 字符数据流整体在前：全部元素文本与尾文本按文档顺
    序拼成一条整串。字符数据在渲染时本就连续，结构属性值
    （layout 等不渲染的标记）若按文档位置插进片段之间，
    拆字短语会在拼接审查里被隔断、整段逃审（属性无、同样
    内容必拦的对照已钉死）。
    (2) 文档序交错流随后：属性值归属其元素位置，与该元素
    的文本、子元素、尾文本按文档顺序交错产出——属性文本
    与正文文本在同一元素的相邻位置渲染，跨「文本↔属性」
    接缝的拆分短语在此流里恢复连续、双向同口径受审；属性
    值本身的全文审查不依赖此流（逐属性原样在列）。
    两流并集送审、口径只增不减：同一文本在两流中重复出现
    只影响审查的存在性判定，不改变良性载荷的派发口径。
    """
    lowered = raw.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise _PayloadUnreviewable(
            "xml payload carries a DTD (DOCTYPE/ENTITY); internal"
            " entities are not expanded here, payload unreviewable"
        )
    try:
        root = ET.fromstring(raw)
    except Exception:  # noqa: BLE001 — 解析器任何异常都回落原文送审
        root = None
    if root is not None:
        out: list[str] = []
        character_data = _xml_character_data(root)
        if character_data:
            out.append(character_data)
        for element in root.iter():
            out.extend(element.attrib.values())
            if element.text:
                out.append(element.text)
            if element.tail:
                out.append(element.tail)
        return out
    text = _XML_CDATA_RE.sub(lambda m: m.group(1), raw)
    text = _XML_TAG_STRIP_RE.sub("", text)
    text = html.unescape(text)
    out = [text] if text.strip() else []
    # 并集送审（复审 P2，封版残余 R-S3-1 在此关闭）：剥标签流
    # 把标签连同其属性值一并丢弃，属性承载的可见文本（卡片
    # brief 等，良构路径刻意全量提取）就此整段逃审——畸形
    # dict/CQ 双形实证。故回落产出再加一条「原文流」：原文
    # 先经同一实体解码（属性内的字符引用同口径解码），属性
    # 值在引号内字面连续、审查可命中。两流按替代关系各自
    # 受审、任一命中即拦，**不拼接**——拼接会把两流接缝处
    # 的文本回绕拼成不存在的短语误拦良性载荷（良构双流的
    # 教训，见上文双流约定）；剥标签流负责跨标签拆分短语的
    # 连续性，原文流负责属性值的连续性，分工互补。
    raw_text = html.unescape(raw)
    if raw_text.strip() and raw_text not in out:
        out.append(raw_text)
    return out


def _lower_key_values(mapping) -> dict[str, list]:
    """小写键 -> 全部同名变体值（保持原始顺序）：碰撞不丢值。

    合法 JSON 对象可同时带 'Text' 与 'text' 两键；单值小写视图
    后写遮蔽先写，被遮蔽值整段逃过内容复核（复审 P3）。提取层
    一律按多值视图并集送审——每个大小写变体值都进提取，复核
    口径只增不减；派发语义不在本层，本层只负责「审全」。
    """
    out: dict[str, list] = {}
    for key, value in mapping.items():
        if isinstance(key, str):
            # 归一口径 strip().lower()（复审 P3）：只 lower 不
            # strip 时填充键（' Data ' / ' Text '）与查找键永
            # 不相等、整段提取为空绕审；与同层 CQ 参数名、资
            # 源键的 strip 口径对齐，在此单一归一点统一处理。
            out.setdefault(key.strip().lower(), []).append(value)
    return out


def _payload_fallback_texts(value, depth: int = 0) -> list[str]:
    """无类型/未知类型回落提取：递归找可见键（复审 P3）。

    可见键的值
    全量收集（字符串在任意深度都进审查），非可见键的容器值
    继续向下递归——可见字段嵌在普通容器键之下也必须到达，
    不许只看顶层一层就整段丢弃（旧回落三处均为平铺实现，
    share 段 data.extra.title 一类嵌套藏词整段逃审）。
    可见键的值按字段形态等价类走 _payload_all_strings
    全量收集（list 值等非串形态同口径）——投递层旧实现仅
    取 str 值后递归余下结构，本共享口径只增不减。

    ``depth`` 与段结构提取共用 _PAYLOAD_MAX_DEPTH 预算：容
    器每深一层 +1，超限抛 _PayloadUnreviewable（提取不完即
    拦）——自引用等环结构由此收敛为超限拦截，不挂死、不
    爆栈（回落路径与 typed 路径同一不可审口径）。
    """
    if depth > _PAYLOAD_MAX_DEPTH:
        raise _PayloadUnreviewable(
            f"payload nesting exceeds {_PAYLOAD_MAX_DEPTH} levels"
        )
    out: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.strip().lower() in _PAYLOAD_VISIBLE_KEYS:
                out.extend(_payload_all_strings(item))
            elif isinstance(item, (dict, list, tuple)):
                out.extend(_payload_fallback_texts(item, depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (dict, list, tuple)):
                out.extend(_payload_fallback_texts(item, depth + 1))
    return out


def _payload_segment_texts(item, depth: int = 0) -> list[str]:
    """单个原始 segment dict 的可见文本（两审查层共用口径）。

    ``depth`` 是段结构嵌套层数（node/forward 子内容逐层 +1）：
    在 _payload_visible_texts 入口统一以 _PAYLOAD_MAX_DEPTH
    判定，超限抛 _PayloadUnreviewable（有界遍历）。
    """
    if not isinstance(item, dict):
        return []
    # type/data 键本身与 data 内键名一律大小写不敏感（复审 P3）：
    # 入口一次性构建小写键多值视图供各分支共用——typed 分支此前
    # 以精确键名查找，Text / Data / Content 等变体提取为空、整段
    # 绕过内容复核，与 CQ 参数名、无类型回退的归一口径不一致。
    # 视图为多值（_lower_key_values）：type 变体与 data 变体全
    # 部组合并集提取，同名大小写碰撞不丢值（复审 P3）。
    item_view = _lower_key_values(item)
    data_values = item_view.get("data", [])
    if any(isinstance(value, str) for value in data_values):
        # data 为裸字符串（复审 P4）：段 schema 中 data 恒为
        # dict，裸串无从按类型路由提取；此前静默返回 [] 等于
        # 整段丢弃，违「提取不完即拦」原理——判不可审，由
        # 网关 fail-closed 拦截。
        raise _PayloadUnreviewable(
            "segment data is a bare string, not an object"
        )
    data_dicts = [value for value in data_values if isinstance(value, dict)]
    if not data_dicts:
        if any(value is not None for value in data_values):
            # data 为 list/标量（复审 P4）：与裸串同口径——data
            # 恒为 dict 的 schema 之外的形态无从按类型路由提
            # 取，静默返回 [] 等于整段丢弃、违「提取不完即
            # 拦」原理，判不可审。data 缺失或为 None 视为无
            # 载荷，不在此列。
            raise _PayloadUnreviewable(
                "segment data is not an object"
            )
        return []
    seg_types = [
        # 类型值同键名归一口径 strip().lower()（复审 P3）：
        # 'json ' / ' xml' 等填充类型只 lower 时与 typed 分
        # 支永不相等、整段提取为空绕审。
        value.strip().lower()
        for value in item_view.get("type", [])
        if isinstance(value, str)
    ] or [None]
    out: list[str] = []
    for data in data_dicts:
        data_view = _lower_key_values(data)
        for seg_type in seg_types:
            out.extend(_segment_texts_by_type(seg_type, data, data_view, depth))
    return out


def _remaining_keys_fallback_texts(data, handled_keys, depth: int = 0) -> list[str]:
    """typed 分支已处理键之外的同级键子集回落提取（复审 P3）。

    typed 分支只取本分字段，data 里其余可见键（node 的
    desc、forward 的 summary、text 段混入的 title 等）旧实
    现整段丢弃、绕过内容复核——本辅助把已处理键（按
    strip().lower() 归一判定，含大小写/填充变体）之外的键
    子集交 _payload_fallback_texts 递归提取并集送审。注：
    各 typed 分支现已改单遍逐键扫描（顺序 = 插入序，见
    _segment_texts_by_type），逐键以单键 dict 调
    _payload_fallback_texts 达成同一集合；本辅助保留为子
    集形态，供整体子集场景与公开名兼容。
    """
    remaining = {
        key: value
        for key, value in data.items()
        if not (isinstance(key, str) and key.strip().lower() in handled_keys)
    }
    if not remaining:
        return []
    return _payload_fallback_texts(remaining, depth)


def _json_object_pairs(pairs) -> dict:
    """json.loads 的 object_pairs_hook：重复键全部值保序保留（复审 P3）。

    明文 json.loads 对重复键后写遮蔽先写：卡片字符串以重
    复键首值藏词、末值良性，首值在解析成功的一刻被静默丢
    弃、整段绕审。本 hook 只用于审查提取路径的解析——同
    名键首值保留、再现时转为列表依次追加，
    _payload_all_strings 全量收集即全部值受审。派发路径不
    经本解析（网关派发的恒是原始载荷），发送语义不变。
    """
    out: dict = {}
    for key, value in pairs:
        if key in out:
            existing = out[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                out[key] = [existing, value]
        else:
            out[key] = value
    return out


def _scalar_field_texts(value) -> list[str]:
    """typed 分支本分标量字段的值提取：串原样、非串全量收集。

    可见字段为非串值（复审 P3）：list 等容器形态走全量收
    集，不许静默丢弃——与 json 分支同口径（提取不完即拦的
    前提是先提取得完；name 为 list 形态旧实现曾静默丢弃）。
    """
    if isinstance(value, str):
        return [value] if value else []
    return _payload_all_strings(value)


def _segment_texts_by_type(seg_type, data, data_view, depth: int = 0) -> list[str]:
    """按（已小写的）类型从一个 data dict 提取可见文本。

    全部 typed 分支单遍扫描 ``data`` 原 dict（收口复审 P2
    定稿）：逐键按归一键名分发——本分字段在本位取标量口
    径、content 在本位递归、同级其余键在本位走回落提取，
    排放顺序 = 字典插入序 = 序列化序（见模块 docstring）。
    ``data_view``（小写多值视图）只余个别分支的历史用途，
    顺序不再由它决定；无类型/未知类型的回退分支按原 dict
    逐项遍历（键名逐项小写判定，同名变体天然全部进提取）。
    """
    if seg_type == "text":
        out = []
        for key, value in data.items():
            if isinstance(key, str) and key.strip().lower() == "text":
                out.extend(_scalar_field_texts(value))
            else:
                # 本分字段之外的同级键在本位并集送审（复审
                # P3）：text 段 data 混入 title/desc 等可见键
                # 旧实现整段丢弃，与 json/xml/回落口径不一
                # 致——逐键走回落提取（可见键全量收集、非可
                # 见容器递归），位置即其序列化位置。
                out.extend(_payload_fallback_texts({key: value}, depth))
        return out
    if seg_type == "node":
        out: list[str] = []
        for key, value in data.items():
            normalized = key.strip().lower() if isinstance(key, str) else None
            if normalized == "content":
                # 子内容走统一入口（复审 P3）：列表内的裸字
                # 符串子、无 type/data 的 dict 子此前经
                # _payload_segment_texts 提取为空、整段绕
                # 审，而同形态在顶层与 forward 内容里都受
                # 审；单 dict 形同理（复审 P4）。
                # _payload_visible_texts 对 str / typed dict
                # / 无类型 dict / list 全形态同口径。
                out.extend(_payload_visible_texts(value, depth + 1))
            elif normalized in {"name", "nickname", "uin", "title", "prompt"}:
                out.extend(_scalar_field_texts(value))
            else:
                # 五键之外的同级键（desc/summary/text 等同在
                # 可见键集内）在本位并集送审（复审 P3）。
                out.extend(_payload_fallback_texts({key: value}, depth))
        return out
    if seg_type == "json":
        out = []
        for key, value in data.items():
            if isinstance(key, str) and key.strip().lower() == "data":
                payload = value
                if isinstance(payload, str):
                    try:
                        # 保对解析（复审 P3）：重复键全部值都
                        # 进送审集，明文 loads 的后写遮蔽会让
                        # 首值藏词整段绕审（见
                        # _json_object_pairs；派发路径恒转发
                        # 原始字符串、双值并存于报文，与保对
                        # 收集同构）。
                        payload = json.loads(
                            payload, object_pairs_hook=_json_object_pairs
                        )
                    except ValueError:
                        out.append(payload)
                        continue
                    except RecursionError:
                        # 深嵌套 JSON 串超出解释器解析栈：提取
                        # 不完，按超限即拦抛信号（有界遍历，
                        # 不许穿出）。
                        raise _PayloadUnreviewable(
                            "json payload nesting exceeds parser limits"
                        ) from None
                out.extend(_payload_all_strings(payload))
            else:
                # data 之外的同级键在本位并集送审（复审
                # P3）：CQ json/xml 卡片 data= 之前的前缀参
                # 数（brief/title 等）经 _cq_code_segments 以
                # 同级键形态进本分支，只取 data 本体等于整
                # 段丢弃前缀参数。
                out.extend(_payload_all_strings(value))
        return out
    if seg_type == "xml":
        out = []
        for key, value in data.items():
            if isinstance(key, str) and key.strip().lower() == "data":
                if isinstance(value, str):
                    out.extend(_xml_visible_texts(value))
                elif isinstance(value, (list, tuple)):
                    # data 为列表形态（复审 P3）：元素逐个按
                    # xml 本体口径提取，非串元素全量收集，
                    # 不静默丢弃。
                    for element in value:
                        if isinstance(element, str):
                            out.extend(_xml_visible_texts(element))
                        else:
                            out.extend(_payload_all_strings(element))
                else:
                    out.extend(_payload_all_strings(value))
            else:
                # 同级键在本位并集送审：同 json 分支口径。
                out.extend(_payload_all_strings(value))
        return out
    if seg_type == "markdown":
        out = []
        for key, value in data.items():
            normalized = key.strip().lower() if isinstance(key, str) else None
            if normalized in {"content", "text"}:
                out.extend(_scalar_field_texts(value))
            else:
                # 本分字段之外的同级可见键在本位并集送审：
                # 同 text 分支口径（复审 P3）。
                out.extend(_payload_fallback_texts({key: value}, depth))
        return out
    if seg_type == "forward":
        out = []
        for key, value in data.items():
            if isinstance(key, str) and key.strip().lower() == "content":
                if value is not None:
                    out.extend(_payload_visible_texts(value, depth + 1))
            else:
                # content 之外的 data 层可见键（summary/
                # title/text/desc 等）在本位并集送审（复审
                # P3）：旧实现整段丢弃。
                out.extend(_payload_fallback_texts({key: value}, depth))
        return out
    # 回退分支走递归回落提取（复审 P3）：键名逐项小写判定
    # 可见键（同名大小写变体全部进提取，碰撞并集），非可见
    # 键的容器继续向下递归找可见字段，不再只看顶层一层。
    return _payload_fallback_texts(data, depth)


_CQ_CODE_RE = re.compile(r"\[CQ:([A-Za-z0-9_]+)((?:,[^\]]*)?)\]")
#: json/xml 单值卡片的 data 参数标记：逗号后与等号前后的空白
#: 一律容忍（复审 P4）——'[CQ:json, data=...]' 的空格形在部分
#: 平台 CQ 解析下同样成卡，提取层不得因空格整段漏审。
_CQ_DATA_MARKER_RE = re.compile(r",\s*data\s*=", re.IGNORECASE)


def _cq_unescape(value: str) -> str:
    """CQ 参数值去转义（数字实体先行、&amp; 最后，防二次去转义）。"""
    return (
        value.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def _cq_param_segments(cq_type: str, raw_params: str) -> list[dict]:
    """CQ 参数串（以逗号起头）→ 按出现轮次分段的等价 segment 列表。

    碰撞保值（复审 P3）：同名参数的大小写变体与精确重复都是
    平台解析器可能区别对待的独立出现，单 dict 覆写 last-wins
    会让被遮蔽值整段逃过内容复核——dict 段形态已按多值视图
    并集送审（_lower_key_values），CQ 串形态同罪、同口径。
    每个参数的第 n 次出现进第 n 个等价 segment，提取层逐段
    并集；无参数时恰一个空 data segment，与旧口径一致。
    """
    occurrences: dict[str, list[str]] = {}
    if raw_params.startswith(","):
        for piece in raw_params[1:].split(","):
            key, sep, value = piece.partition("=")
            # 参数名两侧空白剥除后再小写归一（复审 P4）：空格形
            # 的键（' data'）此前与查找键永不相等、整段漏审。
            key = key.strip().lower()
            if sep and key:
                occurrences.setdefault(key, []).append(_cq_unescape(value))
    rounds = max((len(values) for values in occurrences.values()), default=0)
    if rounds == 0:
        return [{"type": cq_type, "data": {}}]
    segments = []
    for index in range(rounds):
        data = {
            key: values[index]
            for key, values in occurrences.items()
            if index < len(values)
        }
        segments.append({"type": cq_type, "data": data})
    return segments


def _cq_code_segments(match) -> list[dict]:
    """一条 CQ 码 → 等价 segment dict 列表（参数值已去转义）。"""
    cq_type = match.group(1)
    raw_params = match.group(2)
    if cq_type.lower() in {"json", "xml"}:
        # 单值卡片：data 是载荷本体，值内未转义的逗号是内容的一
        # 部分而非参数分隔符——取 data= 之后到码尾的全部内容，
        # 不按逗号切分（复审 P3：多字段卡片后段文本不得丢弃）。
        # 参数名大小写变体同口径（复审 P4）；逗号后/等号前后的
        # 空白同样容忍（复审 P4：'[CQ:json, data=...]' 空格形）。
        marker = _CQ_DATA_MARKER_RE.search(raw_params)
        if marker is not None:
            # data= 之前的前缀参数（brief/title 等）并集送审
            # （复审 P3）：此前特例一命中 data 标记即返回，前
            # 缀参数整段丢弃。前缀按通用出现轮次分段、与本体
            # segment 同类型并列返回，提取时由 json/xml 分支
            # 的同级键收集兜住（见 _segment_texts_by_type）。
            segments = [
                segment
                for segment in _cq_param_segments(
                    cq_type, raw_params[: marker.start()]
                )
                if segment["data"]
            ]
            value = raw_params[marker.end():]
            segments.append(
                {"type": cq_type, "data": {"data": _cq_unescape(value)}}
            )
            return segments
    return _cq_param_segments(cq_type, raw_params)


def _payload_string_texts(message: str, depth: int = 0) -> list[str]:
    """str 载荷的可见文本：CQ 码外剩余文本 + 逐条 CQ 码等价段提取。

    剥离与解析共用同一套文法（复审 P2）：剩余文本只剥除被
    _CQ_CODE_RE 实际解析消费的码段跨度，未被消费的 [CQ: 样
    文本（连字符类型、逗号前空格等畸形形态）整段留在剩余文
    本里按明文送审。旧实现先用宽口径正则剥除一切 [CQ:...]
    再用窄口径解析，畸形括号的内容在两步之间整段消失、未
    审出境；畸形括号在平台侧或按明文展示、或被宽容解析成
    卡片，两种情形其字面内容都已在本函数的送审文本内。

    产出顺序按原始位置交错（复审 P2，顺序/连续性收口）：
    逐个匹配按出现顺序扫描——匹配前的明文块、该匹配的段文
    本、下一块明文……顺次产出，尾块收尾。旧实现把全部余
    文拼成一条排在所有 CQ 段文本之前，卡片在前、明文在后
    的拆分短语被反序拼装（『杀我想自』）、整段逃审；交错
    后拼接顺序即原文顺序，跨「余文↔CQ 段」接缝的拆分双
    向同口径受审。明文块逐块 strip 后产出（与旧实现整体
    strip 的口径一致：块间空白不进审查文本）。
    """
    out: list[str] = []
    cursor = 0
    for match in _CQ_CODE_RE.finditer(message):
        chunk = message[cursor : match.start()].strip()
        if chunk:
            out.append(chunk)
        for segment in _cq_code_segments(match):
            out.extend(_payload_segment_texts(segment, depth))
        cursor = match.end()
    tail = message[cursor:].strip()
    if tail:
        out.append(tail)
    return out


def _has_type_key(mapping) -> bool:
    """dict 是否带 segment 的 type 键（键名大小写不敏感，复审 P3）。

    归一口径与提取层统一 strip().lower()（复审 P3）：' Type '
    填充键只 lower 时判定为无类型、整段走回落而非 typed 分发。
    """
    return any(
        isinstance(key, str) and key.strip().lower() == "type" for key in mapping
    )


def _payload_visible_texts(message, depth: int = 0) -> list[str]:
    """message/messages/text 原始载荷的可见文本（网关内容复核用）。

    全部载荷形态同口径提取（补审第三段 P2）：str 载荷取 CQ 码外
    的剩余文本，并逐条把 CQ 码按等价 segment 提取——json/xml
    卡片内的文本不再被整段剥除；list 载荷逐段提取，裸字符串
    元素按 str 载荷同路处理（复审 P4）；单个 segment dict 按一
    元素列表处理，不再整段跳过复核；forward 的 messages 列表
    条目同样是 segment dict，同一路处理。

    ``depth`` 是段结构嵌套层数：node/forward 子内容与列表套
    列表逐层 +1，超过 _PAYLOAD_MAX_DEPTH 抛 _PayloadUnreviewable
    ——提取不完即 fail-closed（超限即拦），且有界遍历保证
    RecursionError 不会先于本信号发生（复审 P3）。
    """
    if depth > _PAYLOAD_MAX_DEPTH:
        raise _PayloadUnreviewable(
            f"payload nesting exceeds {_PAYLOAD_MAX_DEPTH} levels"
        )
    if isinstance(message, str):
        return _payload_string_texts(message, depth)
    if isinstance(message, dict):
        if _has_type_key(message):
            return _payload_segment_texts(message, depth)
        # 无类型 dict 与未知类型段回落同路（复审 P3）：递归
        # 找可见键，非可见容器键之下的嵌套可见字段同口径受审。
        return _payload_fallback_texts(message, depth)
    if isinstance(message, (list, tuple)):
        out = []
        for item in message:
            if isinstance(item, str):
                out.extend(_payload_string_texts(item, depth))
            elif isinstance(item, (list, tuple)):
                # 列表套列表（复审 P3）：段列表再包一层此前被
                # 静默跳过、整段逃审；按 depth+1 递归同路提取，
                # 与深度界共用预算（自引用列表也由此收敛为超
                # 限拦截，不挂死、不爆栈）。
                out.extend(_payload_visible_texts(item, depth + 1))
            elif isinstance(item, dict) and _has_type_key(item):
                out.extend(_payload_segment_texts(item, depth))
            elif isinstance(item, dict):
                # 无类型 dict 元素同顶层无类型口径（复审 P3）：
                # 递归回落提取，不再平铺只看一层。
                out.extend(_payload_fallback_texts(item, depth))
        return out
    return []

# ---------------------------------------------------------------------------
# 公开名（跨层共用口径）：内部实现沿用历史下划线名，跨模块一律
# 用下列公开名引用，禁止在调用层再维护第二份键集/提取逻辑。
# ---------------------------------------------------------------------------
VISIBLE_KEYS = _PAYLOAD_VISIBLE_KEYS
MAX_DEPTH = _PAYLOAD_MAX_DEPTH
PayloadUnreviewable = _PayloadUnreviewable
collect_all_strings = _payload_all_strings
fallback_visible_texts = _payload_fallback_texts
lower_key_values = _lower_key_values
xml_visible_texts = _xml_visible_texts
json_object_pairs = _json_object_pairs
remaining_keys_fallback_texts = _remaining_keys_fallback_texts
payload_segment_texts = _payload_segment_texts
payload_visible_texts = _payload_visible_texts
