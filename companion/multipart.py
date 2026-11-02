"""Bounded structured multipart replies: schema, validation, and safety review.

The model may return at most three complete, independently understandable bubbles.
Plain-text output from an ordinary reply is split deterministically only when it
contains clear complete segments. Anything unsafe, fragmented, or too large falls
back to a reviewed fixed single answer in the reply path; this module never retries.
"""

import json
import re
import unicodedata

MAX_BUBBLES = 3
# An explicit one-by-one request is an instruction to deliver line by line,
# so the plain-text splitter keeps each complete line as its own bubble
# there instead of packing the tail into the last of MAX_BUBBLES bubbles.
# The cap stays well inside the per-user send quota (18 attempts per
# rolling 60s) and the total-size bound below still applies; longer
# one-by-one answers fall back to the historical grouping.
# (Ported from production round 7, commit 09b621c, at framework cutover.)
MAX_ONE_BY_ONE_BUBBLES = 10
MAX_BUBBLE_CHARS = 240
MAX_TOTAL_CHARS = 600
MAX_SINGLE_MESSAGE_CHARS = 600
# Casual chat lines often omit the terminal punctuation. Under an explicit
# one-by-one request only short, self-contained lines may stand without it.
MAX_SHORT_SEGMENT_CHARS = 60

MULTIPART_INSTRUCTION = (
    "请把这次回复组织成 JSON 对象，格式严格为 "
    '{"bubbles": ["第一条完整气泡", "第二条完整气泡"]}。\n'
    "要求：只输出 JSON，不要代码围栏、解释或额外文字；bubbles 是 1 到 3 条字符串；"
    "每条气泡都必须是完整、可独立理解的短句；"
    "不要在一条气泡中间拆开人名、链接、数字、免责声明或限定条件；"
    "不要为了凑条数而拆分；通常一条即可，只有自然的“先回应再回答”才用两条；"
    "身份说明、安全相关内容以及其他必须完整披露的内容只能放在同一条气泡里。"
)

_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_TRAILING_FRAGMENTS = (
    "，", ",", "、", "：", ":", "；", ";", "—", "-", "…", "（", "(", "【", "[",
    "《", "<", "“", '"', "‘", "'", "和", "与", "及", "或", "但", "而", "因为",
    "所以", "比如", "例如", "详见", "链接", "地址", "网址", "http", "https", "www",
    "是", "叫", "名叫", "叫做", "叫作", "姓", "名字", "姓名为", "名为",
)
# A segment that ends with an English name lead-in leaves the name dangling in
# the next segment, exactly like the CJK lead-ins above.
_NAME_LEAD_END = re.compile(
    r"(?:\b(?:his|her|my|their)\s+name\s*(?:is|['’]s)"
    r"|\bname\s*(?:is|['’]s)"
    r"|\b(?:he|she)\s*(?:is|['’]s)\s+called"
    r"|\bis\s+called|\bknown\s+as)\s*$",
    re.IGNORECASE,
)
# A URL anywhere inside a segment must stay attached to its context; checking
# only segment starts and trailing fragments misses mid-segment links.
_URL_ANYWHERE = re.compile(r"https?://|www\.", re.IGNORECASE)
_PAIRS = {"（": "）", "(": ")", "【": "】", "[": "]", "《": "》", "「": "」", "“": "”"}
# Deterministic plain-text splitting. Blank lines always delimit segments; single
# line breaks do so only when the user explicitly asked for one-by-one delivery.
_ONE_BY_ONE_REQUEST = re.compile(
    r"一条(?:一)?条|逐条|分条|一句(?:一)?句|分段(?:发|说|回)|"
    r"分开(?:发|说|回|送)|拆开(?:发|说|回|送)|不要(?:一次性|一起|一次)(?:发|说)|"
    r"多(?:发|分)几(?:条|段)|one by one|separate messages",
    re.IGNORECASE,
)
_TERMINAL_PUNCTUATION = frozenset("。！？!?…～~")
_CLOSING_MARKS = frozenset("”’」』】）)]》\"'")
_CONTINUATION_PREFIXES = (
    "http://", "https://", "www.", "详见", "链接", "地址", "网址", "来源",
)
_CONTINUATION_STARTS = frozenset("，。、；：！？,.;:!?）)]】》」”’")
# Identity, crisis, and other fixed disclosures must never be split apart.
_CRITICAL_DISCLOSURE = re.compile(
    # The hotline number needs digit boundaries: as a bare substring it also
    # matches inside unrelated identifiers such as an order number.
    r"自杀|自残|自伤|伤害自己|轻生|求助热线|心理援助|危机热线|(?<!\d)12356(?!\d)",
    re.IGNORECASE,
)
# A disclaimer or qualification on its own line must stay with the answer it limits.
_DISCLAIMER = re.compile(
    r"仅供参考|不构成|不能替代|请咨询|请以.{0,8}为准|免责声明|"
    r"专业(?:医生|人士|机构)|务必",
    re.IGNORECASE,
)
_IDENTITY_DENIAL = re.compile(
    r"我是(?:一个|一名|个)?(?:真人|人类|真实的人|真人小[姐哥])"
    r"|我不是(?:一个|一名|个)?(?:AI|人工智能|机器人|虚拟人物|模型|程序)"
    r"|没有(?:使用|用|经过|通过)(?:AI|人工智能|模型|机器人)"
    r"|\bI(?:'m| am) (?:a |an |the )?(?:real |actual )?human\b"
    r"|\bI(?:'m| am) not (?:an? )?(?:AI|artificial intelligence|bot|robot|model)\b",
    re.IGNORECASE,
)
_IDENTITY_QUESTION = re.compile(
    r"你是谁|你是什么|你是(?:不是)?(?:真人|AI|人工智能|机器人|模型|人类|程序)"
    r"|真人吗|是否真人|是不是人|who are you|what are you"
    r"|are you (?:a )?(?:human|ai|bot|robot|real|person)"
    # Model-identity questions, anchored on 你/您 as the subject of using /
    # being based on a model, so non-identity questions about a model in
    # general ("这个模型怎么做") never trigger the disclosure contract.
    r"|[你您](?:背后)?(?:用|使用|基于|运行)(?:的)?(?:是)?(?:什么|哪个|哪种|啥)模型"
    r"|[你您](?:用|使用|基于)的模型(?:是)?(?:什么|哪个|哪种)"
    r"|[你您]是(?:什么|哪个|哪种)模型"
    r"|[你您]背后的?(?:模型|系统)"
    # Name questions are identity questions (§3.3 身份问题须答全三项).
    r"|[你您]叫(?:什么|啥)|[你您]的名字(?:是|叫)(?:什么|啥)"
    r"|[你您]的名字\s*(?:[?？。！!]|$)"
    # Asking where the conversation goes is asking about third-party
    # processing, which the disclosure contract also covers.
    r"|(?:对话|聊天|消息|内容).{0,6}(?:发给|传给|发送给|给别人|给第三方|第三方)"
    r"|(?:what|which) model (?:do you use|are you (?:using|based on|running on|built on))"
    r"|(?:what|which) model are you\s*[?？.!。]*$",
    re.IGNORECASE,
)
# Evasive refusals to an identity question are never a disclosure, even if
# the text happens to contain other identity markers.
_IDENTITY_EVASIVE = re.compile(
    r"这是(?:个)?秘密|保密|无可奉告|不能告诉你|不告诉你|不能透露|无法透露|不方便透露|不能说"
    r"|it'?s a secret|i can'?t tell you|i cannot tell you|confidential|no comment",
    re.IGNORECASE,
)
_IDENTITY_DISCLOSURE = re.compile(
    # \bAI\b misses CJK-adjacent "AI" (CJK characters are word characters, so
    # there is no boundary in "是AI助手"); require non-ASCII-alphanumeric
    # neighbours instead so disclosure is recognised wherever it appears.
    r"(?<![A-Za-z0-9])AI(?![A-Za-z0-9])|人工智能|机器人|模型|虚拟|不是真人|不是人类",
    re.IGNORECASE,
)
_SELF_HARM_ENCOURAGEMENT = re.compile(
    r"去死|赶紧死|死了算了|自杀吧|结束(?:你的)?生命|伤害自己吧|"
    r"割腕|跳楼|吃安眠药|上吊|"
    r"\b(?:go )?kill yourself\b|\byou should die\b",
    re.IGNORECASE,
)


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _has_control_characters(text: str) -> bool:
    return any(unicodedata.category(char) in _CONTROL_CATEGORIES for char in text)


def _balanced(text: str) -> bool:
    stack = []
    for char in text:
        if char in _PAIRS:
            stack.append(_PAIRS[char])
        elif char in _PAIRS.values():
            if not stack or stack.pop() != char:
                return False
    return not stack


def looks_complete(text: str) -> bool:
    """Reject obvious mid-sentence splits; completeness is also a prompt constraint."""
    stripped = text.rstrip()
    if not stripped or stripped.endswith(_TRAILING_FRAGMENTS):
        return False
    return _balanced(stripped)


def parse_bubbles(raw: str) -> tuple[list[str] | None, str]:
    """Parse the closed schema; return (bubbles, "") or (None, reason)."""
    if not isinstance(raw, str) or not raw.strip():
        return None, "empty"
    try:
        data = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except (TypeError, ValueError):
        return None, "invalid_json"
    if not isinstance(data, dict) or set(data) != {"bubbles"}:
        return None, "schema"
    values = data["bubbles"]
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_BUBBLES:
        return None, "count"
    bubbles: list[str] = []
    for value in values:
        if not isinstance(value, str):
            return None, "type"
        bubble = value.strip()
        if not bubble or _has_control_characters(bubble):
            return None, "content"
        if len(bubble) > MAX_BUBBLE_CHARS or not looks_complete(bubble):
            return None, "size_or_fragment"
        bubbles.append(bubble)
    if len(set(bubbles)) != len(bubbles):
        return None, "duplicate"
    if len("\n".join(bubbles)) > MAX_TOTAL_CHARS:
        return None, "total_size"
    return bubbles, ""


# Self-referential AI identity evidence (KD-001): the AI marker must
# describe the speaker itself — «DeepSeek 是一款 AI» describes a third
# party and does not count (Codex P3 Review round 2, item 6).
_IDENTITY_SELF_AI = re.compile(
    r"我(?:是|就是)(?:一个|一名|个)?[^。！？\n]{0,8}?(?:AI|人工智能|机器人|虚拟|语言模型)"
    r"|(?:AI|人工智能)(?:陪伴)?助手"
    r"|作为(?:一个|一名|个)?[^。！？\n]{0,6}?(?:AI|人工智能|语言模型)"
    r"|不是真人|不是人类|虚拟助手|智能助手",
    re.IGNORECASE,
)


def review_bubbles(
    bubbles: list[str],
    *,
    user_text: str = "",
    identity_name: str = "阿年",
    identity_model: str = "DeepSeek",
    enforce_size: bool = True,
) -> tuple[bool, str]:
    """Review the joined answer and every bubble for identity and safety.

    When the user asked an identity question, the answer must disclose all
    four items (KD-001 / §3.3): an AI identity marker, the assistant name
    (``identity_name``) or the role 「AI 陪伴助手」, the model display name
    (``identity_model``, case-insensitive), and the words 「第三方模型」.
    Anything less — including evasive refusals — is rejected as "identity"
    so the caller falls back to :func:`identity_disclosure`.
    """
    if not bubbles:
        return False, "empty"
    joined = "\n".join(bubbles)
    compact = "".join(bubbles)
    # enforce_size=False is for routes with their own length regime
    # (tool synthesis, §5.5): content checks still apply, the multipart
    # 600-char total cap does not (Codex P3 Review round 2, item 4).
    if enforce_size and len(joined) > MAX_TOTAL_CHARS:
        return False, "too_large"
    # Check the joined text, a separator-free concatenation, a space-joined
    # variant (English protected phrases need their literal spaces, which the
    # newline join and the concatenation both destroy), a fully
    # whitespace-stripped variant (for CJK phrases with stray spaces), and
    # every bubble on its own.
    spaced = " ".join(bubbles)
    squashed = "".join(joined.split())
    # 只按片段的真实顺序送审（收口复审 P3 纠正）：曾试过对
    # 安全模式另加逆序拼接变体（双序并集），被复现证明误拦
    # 顺序确定的良性回复——如 ['生命在于运动，要多锻炼',
    # '今天聊到这里就结束'] 的逆序拼接凭空拼出「结束生命」。
    # 顺序问题的正解在提取层：companion.content_extract 与
    # services 适配按序列化顺序产出片段（dict 字段取插入
    # 序、typed 段取序列化字段序，见 content_extract 模块
    # docstring），使拼接流与出站字段流逐字同序；本函数只
    # 审这一序，镜像放法各自按其自身序列化序判定，不用多
    # 序并集掩盖提取顺序未对齐。
    for text in (joined, compact, spaced, squashed, *bubbles):
        if _IDENTITY_DENIAL.search(text):
            return False, "identity"
        if _SELF_HARM_ENCOURAGEMENT.search(text):
            return False, "safety"
        # Crisis wording must stay in one complete message; checking the
        # separator-free concatenation catches a phrase split across bubbles.
        if _CRITICAL_DISCLOSURE.search(text):
            return False, "safety"
    if _IDENTITY_QUESTION.search(user_text):
        # An evasive refusal is never sent as-is, even if it happens to
        # contain some disclosure markers.
        if any(
            _IDENTITY_EVASIVE.search(text)
            for text in (joined, compact, spaced, squashed)
        ):
            return False, "identity"
        name = " ".join(str(identity_name).split()) or "阿年"
        model = " ".join(str(identity_model).split()) or "DeepSeek"
        # The AI item must be a self-referential disclosure, independent
        # of the third-party item (Codex P3 Review #6 + round 2 item 6):
        # a generic 「模型」/「AI」 mention — including one describing the
        # third-party model itself — never satisfies it.
        has_ai_marker = _IDENTITY_SELF_AI.search(compact) is not None
        has_name_or_role = (
            name in compact
            or name in squashed
            or "AI 陪伴助手" in compact
            or "AI陪伴助手" in squashed
        )
        has_model = model.casefold() in compact.casefold() or (
            model.casefold() in squashed.casefold()
        )
        has_third_party = "第三方模型" in compact or "第三方模型" in squashed
        if not (has_ai_marker and has_name_or_role and has_model and has_third_party):
            return False, "identity"
    return True, ""


def is_identity_question(text: str) -> bool:
    """Whether the user explicitly asked about identity or AI/human status."""
    return _IDENTITY_QUESTION.search(text) is not None


def join_bubbles(bubbles: list[str]) -> str | None:
    """Join validated bubbles for the single-message fallback when slots are scarce."""
    joined = "\n".join(bubbles)
    return joined if len(joined) <= MAX_SINGLE_MESSAGE_CHARS else None


def requests_one_by_one(text: str) -> bool:
    """Whether the user explicitly asked for replies delivered one by one."""
    return isinstance(text, str) and _ONE_BY_ONE_REQUEST.search(text) is not None


def _ends_complete_sentence(text: str) -> bool:
    tail = text.rstrip()
    while tail and tail[-1] in _CLOSING_MARKS:
        tail = tail[:-1].rstrip()
    if not tail:
        return False
    if tail[-1] in _TERMINAL_PUNCTUATION:
        return True
    return tail[-1] == "." and (len(tail) == 1 or not tail[-2].isdigit())


def _starts_continuation(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped or stripped[0] in _CONTINUATION_STARTS:
        return True
    return stripped.startswith(_CONTINUATION_PREFIXES)


def _has_letter(text: str) -> bool:
    """Whether the text contains a real letter.

    str.isalpha() is used instead of a regex class: the old negated-word-char
    class also matches Unicode number forms such as superscripts and Roman
    numerals, which are not letters and must never make a bare-number
    segment look complete.
    """
    return any(char.isalpha() for char in text)


def _is_complete_segment(text: str, *, one_by_one: bool) -> bool:
    """Whether a segment can stand alone as a bubble.

    Terminal punctuation is required by default. When the user explicitly asked
    for one-by-one delivery, a short self-contained chat line may omit it; longer
    unpunctuated prose can still be a mid-sentence split and stays unsplit.
    """
    if _ends_complete_sentence(text):
        return True
    if not one_by_one:
        return False
    stripped = text.strip()
    return len(stripped) <= MAX_SHORT_SEGMENT_CHARS and _has_letter(stripped)


def _numeric_boundary_split(left: str, right: str) -> bool:
    """Whether splitting between two segments would cut a number in half.

    A left segment ending in a digit (or "digit+.") is a fragment only when
    the right segment actually continues the number — with a digit, a decimal
    point followed by a digit, or the CJK decimal marker followed by a digit.
    A complete short line that merely ends in a digit ("我选方案2", or the
    intact "3.14" at the end of a line) is not a fragment. Detection runs on
    an NFKC copy so fullwidth digits and decimal points are recognised; the
    original text is never modified.
    """
    tail = unicodedata.normalize("NFKC", left).rstrip()
    # Closing marks and terminal punctuation do not complete a number: in
    # "预算为3。" the quantity is still dangling when the next segment starts
    # with more digits. Strip them before looking at the last real character.
    while tail and (
        tail[-1] in _CLOSING_MARKS or tail[-1] in _TERMINAL_PUNCTUATION or tail[-1] == "."
    ):
        tail = tail[:-1].rstrip()
    if not tail:
        return False
    head = unicodedata.normalize("NFKC", right).lstrip()
    continues = bool(
        head
        and (
            head[0].isdigit()
            or (head[0] == "." and len(head) >= 2 and head[1].isdigit())
            or (head[0] == "点" and len(head) >= 2 and head[1].isdigit())
        )
    )
    if not continues:
        return False
    return tail[-1].isdigit() or (
        tail[-1] == "." and len(tail) >= 2 and tail[-2].isdigit()
    )


def _group_segments(segments: list[str]) -> list[str] | None:
    """Pack ordered segments into at most MAX_BUBBLES bubbles without losing text.

    The historical tail grouping is kept when it fits. When the grouped tail would
    exceed one bubble, consecutive segments are repacked greedily instead of
    rejecting the whole split; refusing only happens when three bubbles cannot
    hold the content.
    """
    if len(segments) <= MAX_BUBBLES:
        return segments
    tail = "\n".join(segments[MAX_BUBBLES - 1:])
    if len(tail) <= MAX_BUBBLE_CHARS:
        return [*segments[:MAX_BUBBLES - 1], tail]
    groups: list[str] = []
    current = ""
    for segment in segments:
        candidate = f"{current}\n{segment}" if current else segment
        if current and len(candidate) > MAX_BUBBLE_CHARS:
            groups.append(current)
            current = segment
        else:
            current = candidate
    if current:
        groups.append(current)
    return groups if len(groups) <= MAX_BUBBLES else None


def split_plain_text(raw: str, *, one_by_one: bool = False) -> list[str] | None:
    """Split clear, complete plain-text segments into bubbles.

    Blank-line-separated paragraphs always qualify and are packed into at most
    MAX_BUBBLES bubbles. Single line breaks qualify only when the user explicitly
    asked for one-by-one delivery; in that case a short, self-contained line may
    omit terminal punctuation, while longer unpunctuated prose is still treated
    as a possible mid-sentence split, and up to MAX_ONE_BY_ONE_BUBBLES complete
    lines each stay their own bubble instead of being packed. Each segment must not
    look like a dangling link, number, name, or qualification, so a split can never
    detach one from what it modifies. Returns None to keep the answer as a single
    message.
    """
    if not isinstance(raw, str):
        return None
    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or "```" in text:
        return None
    pattern = r"\n\s*" if one_by_one else r"\n[ \t]*\n+"
    segments = [" ".join(part.split()) for part in re.split(pattern, text)]
    segments = [segment for segment in segments if segment]
    if len(segments) < 2:
        return None
    # A protected phrase or a URL can be split across segments so that no
    # single segment matches ("伤害" + "自己", "https:/" + "/…"); re-check the
    # full text first in three joins: separator-free (CJK phrases and split
    # URLs), space-joined (English phrases keep their literal spaces), and
    # whitespace-stripped (CJK phrases with stray spaces inside).
    compact = "".join(segments)
    spaced = " ".join(segments)
    squashed = "".join(compact.split())
    if any(
        _CRITICAL_DISCLOSURE.search(text)
        or _DISCLAIMER.search(text)
        or _IDENTITY_DISCLOSURE.search(text)
        or _IDENTITY_DENIAL.search(text)
        or _SELF_HARM_ENCOURAGEMENT.search(text)
        or _URL_ANYWHERE.search(text)
        for text in (compact, spaced, squashed)
    ):
        return None
    for index, segment in enumerate(segments):
        if (
            len(segment) > MAX_BUBBLE_CHARS
            or _has_control_characters(segment)
            or not looks_complete(segment)
            or not _is_complete_segment(segment, one_by_one=one_by_one)
            or _starts_continuation(segment)
            or _URL_ANYWHERE.search(segment)
            or _NAME_LEAD_END.search(segment)
            or (
                index + 1 < len(segments)
                and _numeric_boundary_split(segment, segments[index + 1])
            )
            or _IDENTITY_DISCLOSURE.search(segment)
            or _SELF_HARM_ENCOURAGEMENT.search(segment)
            or _CRITICAL_DISCLOSURE.search(segment)
            or _DISCLAIMER.search(segment)
        ):
            return None
    if len(set(segments)) != len(segments):
        return None
    if not (one_by_one and len(segments) <= MAX_ONE_BY_ONE_BUBBLES):
        grouped = _group_segments(segments)
        if grouped is None:
            return None
        segments = grouped
    if len("\n".join(segments)) > MAX_TOTAL_CHARS:
        return None
    return segments


def identity_disclosure(name: str, model: str) -> str:
    """Reviewed fixed single answer for identity-disclosure failures."""
    safe_name = " ".join(str(name).split()) or "阿年"
    safe_model = " ".join(str(model).split()) or "第三方模型"
    return (
        f"我是{safe_name}，一个 AI 陪伴助手，不是真人；"
        f"当前回复由第三方模型 {safe_model} 生成。"
    )
