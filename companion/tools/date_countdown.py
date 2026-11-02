"""Deterministic countdown / anniversary lookup from configured dates.

Read-only tool: given the anniversary list and an injectable clock,
answer "how many days until X" questions with pure calendar-date
arithmetic in Asia/Shanghai. No network, no sending.

体验增强（ai/qqbot-countdown-enhance）：
- 条目来源可经 anniversaries_provider 动态提供（装配层注入主人
  对话存储条目，工具内与 config 静态清单合并，见
  merge_anniversaries）；缺省静态元组口径与原实现逐项一致。
- 可见性按来源分（复审 P2 修正口径）：对话存储条目是主人私密
  数据，仅主人档位（caller_scope="owner"）可见——列表、按名
  单查、合并视图全表面一致，非主人查存储独有名称与查不存在
  同结果（不泄露存在性）；config 静态条目维持既有可见性（单
  查开放）。列表（整表枚举）在每一层都仅主人：注册表路径经
  caller_check 主体门拒绝，action 层按 caller_scope 设防，
  非主人档位与「无清单」同表现。owner_ids 为 None/空集时
  fail-closed，无人是主人。
- 新增 month/day 整数参数：用户只给月日（如「5月20日」）时由
  模型成对提取、工具确定性解析到下一次该月日；防编造 check 要求
  「M月D日/号」字面逐字出现在用户消息中（口径同 name/target_date）。
- 列表在信封上限截断时以 omitted 计数明示未列出条数，不静默。
- parse_anniversary_command 是私聊命令面的确定性解析（主人对话
  式增删查），与工具分离：命令在 ingress 的 Command Stage 消费，
  写入走 AnniversaryStore，不经本工具。
"""

import calendar
import json
import re
import unicodedata
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from companion.config import AnniversaryEntry
from companion.tools.read_only import ReadOnlyTool, _unique_object

ZONE = "Asia/Shanghai"
UNAVAILABLE_REPLY = "倒计时查询暂不可用，请稍后再试。"
UNCONFIGURED_REPLY = "尚未配置纪念日清单，暂时无法查询纪念日。"
INVALID_DATE_REPLY = "日期格式不正确，请使用 YYYY-MM-DD 格式并确认日期真实存在（例如 2027-02-17）。"
CLARIFY_REPLY = "请只提供纪念日名称或具体日期中的一项再查询。"
MAX_OUTPUT_CHARS = 2000
KEYS = ("today", "target_date", "label", "days", "anniversary_years", "kind")
LIST_KEYS = ("today", "kind", "items", "omitted")
ITEM_KEYS = ("label", "target_date", "days", "anniversary_years")
SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 0, "maxLength": 64},
        "target_date": {"type": "string", "minLength": 0, "maxLength": 10},
        "month": {"type": "integer", "minimum": 1, "maximum": 12},
        "day": {"type": "integer", "minimum": 1, "maximum": 31},
    },
    "required": [],
    "additionalProperties": False,
}
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)


def _normalize_name(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _normalize_text(value):
    return unicodedata.normalize("NFKC", value).casefold()


def _system_clock():
    return datetime.now(timezone.utc)


def _today(clock, zone_factory):
    instant = clock()
    if not isinstance(instant, datetime) or instant.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("invalid clock")
    zone = zone_factory(ZONE)
    if not isinstance(zone, ZoneInfo) or zone.key != ZONE:
        raise ValueError("invalid timezone")
    return instant.astimezone(zone).date()


def _observed(year, month, day):
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, month, day)


def _parse_date(value):
    """Strict YYYY-MM-DD parse; returns a date or None."""
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _resolve(entry, today):
    """Resolve an entry to (target_date, anniversary_years|None) for today."""
    if entry.date is not None:
        return date.fromisoformat(entry.date), None
    target = _observed(today.year, entry.month, entry.day)
    if target < today:
        target = _observed(today.year + 1, entry.month, entry.day)
    years = target.year - entry.year if entry.year is not None else None
    if years is not None and target.year < entry.year:
        # 起源年晚于解析出的目标年：周年数无意义，按未配置起源年处理，
        # 不得产出负数（渲染层亦拒绝负 anniversary_years）。
        years = None
    return target, years


_CJK_SCRIPT_PREFIXES = ("CJK", "HIRAGANA", "KATAKANA", "HANGUL", "IDEOGRAPHIC")


def _is_token_char(char):
    # 紧邻字符算 token 粘连（出现被视为嵌在更长 token 中）当且仅当：
    # 它是 ASCII [A-Za-z0-9_-]，或它是非 ASCII 的字母/数字
    # （isalnum() 为真）且不属于 CJK 脚本（汉字 Han、平假名
    # Hiragana、片假名 Katakana、谚文 Hangul，经 unicodedata.name
    # 前缀判定）。CJK 邻接（如「618还有几天」）不算粘连；而 é、Ω、
    # ٦ 等非 CJK 字母/数字邻接同样构成粘连，必须算 glue。非 ASCII
    # 的非字母数字字符（标点等）是安全边界，不算粘连。
    # IDEOGRAPHIC 前缀的表意迭代标记/数字零等（如 々 U+3005、〇
    # U+3007、〆等）同属 CJK 文本字符，故一并纳入 CJK 豁免前缀。
    # 不可见字符（unicodedata.category 为 Cc/Cf/Mn/Me，
    # 如控制字符、零宽字符、组合附加符号）恒算 glue：不可见字符
    # 不得充当 token 分隔符，否则可见字符间插入不可见字符即可
    # 绕过防编造门；此检查先于 ASCII 分支与 CJK 豁免，独立生效
    # （空白类控制字符已在消息归一化的 split-join 阶段被折叠，
    # 到达此处的 Cc 均为非空白控制字符，恒算 glue 不误伤合法
    # 空格分隔）。
    if unicodedata.category(char) in ("Cc", "Cf", "Mn", "Me"):
        return True
    if char.isascii():
        return char.isalnum() or char in "_-"
    if not char.isalnum():
        return False
    return not unicodedata.name(char, "").startswith(_CJK_SCRIPT_PREFIXES)


def _occurrence_passes(normalized_value, normalized_message, index, *, is_date):
    # 名称与日期分支统一口径：对每处出现无条件检查紧邻字符，不看
    # 值自身的边缘字符类型（café/_abc/abc- 等边缘同样受检）。
    end = index + len(normalized_value)
    if index and _is_token_char(normalized_message[index - 1]):
        return False
    if end < len(normalized_message) and _is_token_char(normalized_message[end]):
        return False
    return True


def _found_in_message(normalized_value, normalized_message, *, is_date):
    # 遍历全部出现位置：任一出现通过边界检查即命中，首个出现边界
    # 失败（如粘连在单号/长 token 中）不得误拒后续的合法出现。
    start = 0
    while True:
        index = normalized_message.find(normalized_value, start)
        if index < 0:
            return False
        if _occurrence_passes(normalized_value, normalized_message, index, is_date=is_date):
            return True
        start = index + 1


def _month_day_literals(month, day):
    """「M月D日/号」的全部书写变体（前导零有无 × 日/号）。

    用户口语书写不统一（5月20日 / 05月20日），模型提取出的整数
    无法区分原文写法，故 check 对全部变体逐一按边界口径核验，
    任一逐字出现即算有据；粘连边界仍由 _found_in_message 把守
    （如「15月20日」不得为 month=5 背书）。
    """
    return {
        f"{month}月{day}日",
        f"{month}月{day}号",
        f"{month:02d}月{day:02d}日",
        f"{month:02d}月{day:02d}号",
        f"{month:02d}月{day}日",
        f"{month:02d}月{day}号",
        f"{month}月{day:02d}日",
        f"{month}月{day:02d}号",
    }


def countdown_user_provided(args, message):
    """Anti-fabrication check: every non-empty argument must occur verbatim
    (after NFKC + casefold normalization) in the user's current message.
    List mode (all empty) is exempt. month/day must come as an int pair
    whose 「M月D日/号」literal occurs in the message (any zero-padding
    variant); a lone month or day is never trusted."""
    if not isinstance(args, dict):
        return False
    name = args.get("name")
    target_date = args.get("target_date")
    name = name.strip() if isinstance(name, str) else ""
    target_date = target_date.strip() if isinstance(target_date, str) else ""
    month, day = args.get("month"), args.get("day")
    has_month = type(month) is int
    has_day = type(day) is int
    if not name and not target_date and not has_month and not has_day:
        return True
    if not isinstance(message, str):
        return False
    text = " ".join(_normalize_text(message).split())
    if name and not _found_in_message(_normalize_name(name), text, is_date=False):
        return False
    if target_date and not _found_in_message(_normalize_text(target_date), text, is_date=True):
        return False
    if has_month != has_day:
        return False
    if has_month and not any(
        _found_in_message(_normalize_text(literal), text, is_date=True)
        for literal in _month_day_literals(month, day)
    ):
        return False
    return True


def _single(today, target, label, days, years, kind):
    return {
        "today": today.isoformat(),
        "target_date": target.isoformat() if target is not None else None,
        "label": label,
        "days": days,
        "anniversary_years": years,
        "kind": kind,
    }


def _dump(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _envelope_length(data_str):
    payload = {"status": "ok", "data": data_str}
    return len(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))


def normalize_anniversary_name(value):
    """名称规范化公开入口（与 _normalize_name 同一实现）。

    供装配/命令层在工具外比对条目名时复用同一口径，避免各处再
    内置一份（config 与 memory 侧的同口径内置是叶子依赖所迫，
    tools 内部则统一走本函数）。
    """
    return _normalize_name(value)


def merge_anniversaries(config_entries, stored_entries):
    """合并 config 静态清单与主人对话存储清单（合并口径，钉死）。

    - 规范化同名时以存储条目为准：主人对话里的显式维护覆盖部署
      期静态配置（否则对话「记一下」对同名条目静默无效）。
    - 顺序以 config 为骨架：被覆盖者在原位替换；存储独有条目按
      存储序追加在末尾。删掉存储条目后，同名 config 条目重新
      可见（合并是视图，不是搬运）。
    - config 侧规范化重名仍 fail-fast（与装配校验同口径）；存储
      侧以规范化名为 PK、天然不重。
    """
    merged = []
    positions = {}
    for raw in config_entries or ():
        entry = AnniversaryEntry.model_validate(raw)
        key = _normalize_name(entry.name)
        if key in positions:
            raise ValueError("duplicate anniversary name")
        positions[key] = len(merged)
        merged.append(entry)
    for raw in stored_entries or ():
        entry = AnniversaryEntry.model_validate(raw)
        key = _normalize_name(entry.name)
        if key in positions:
            merged[positions[key]] = entry
        else:
            positions[key] = len(merged)
            merged.append(entry)
    return tuple(merged)


def _build_entries(raw_entries):
    """条目序列 → (entries 元组, 规范化名索引)；非法/重名 fail-fast。"""
    entries = []
    index = {}
    for raw in raw_entries or ():
        try:
            entry = AnniversaryEntry.model_validate(raw)
        except Exception as exc:
            raise ValueError("invalid anniversary entry") from exc
        key = _normalize_name(entry.name)
        if key in index:
            raise ValueError("duplicate anniversary name")
        index[key] = entry
        entries.append(entry)
    return tuple(entries), index


def _countdown_caller_check(owner_ids):
    """列表模式主体门（关闭 R-C1）：仅主人身份可枚举全清单。

    由宿主在 invoke 内执行（caller_check 挂钩），动作本身仍拿不到
    身份。name/target_date/month/day 任一单查在此放行——单查的
    可见性按来源在动作内以 caller_scope 档位收口（存储条目仅
    主人档位可见，非主人查存储独有名称与查不存在同结果）。
    owner_ids 为 None 或空集时无人是主人（fail-closed）。
    """
    owners = frozenset(str(item) for item in owner_ids) if owner_ids else frozenset()

    def check(args, identity):
        name = args.get("name")
        target_date = args.get("target_date")
        if isinstance(name, str) and name.strip():
            return True
        if isinstance(target_date, str) and target_date.strip():
            return True
        if _is_int(args.get("month")) or _is_int(args.get("day")):
            return True
        return isinstance(identity, str) and identity in owners

    return check


def date_countdown_tool(anniversaries=(), clock=None, zone_factory=None, *,
                        anniversaries_provider=None, owner_ids=None):
    """构建 date_countdown 只读工具。

    anniversaries_provider（可选）：无参 async 可调用，每次调用时
    返回主人对话存储的条目序列（仅存储部分）；主人档位调用时与
    静态 anniversaries 按 merge_anniversaries 实时合并（刚记下
    立刻可查），非主人档位只见静态部分——可见性按来源分：对话
    存储是主人私密数据，全表面仅主人可见；config 静态条目维持
    既有可见性。owner_ids：主人身份集合，None 与空集同口径
    fail-closed（无人是主人：列表全关、存储条目全不可见）。
    caller_scope 由宿主在 invoke 内按身份算出档位标签后传入
    （"owner"/"public"）；缺省为最小权限 "public"——任何未显式
    传档位的直调一律非主人待遇（fail-closed，fix1 复审 P2）；
    宿主内部直调（命令面渲染，已在 Stage 侧过主人门）须显式传
    caller_scope="owner"，不许依赖默认值。
    """
    clock = clock or _system_clock
    zone_factory = zone_factory or ZoneInfo
    entries, index = _build_entries(anniversaries)
    owners = frozenset(str(item) for item in owner_ids) if owner_ids else frozenset()

    def _caller_scope(identity):
        return "owner" if isinstance(identity, str) and identity in owners else "public"

    async def action(name=None, target_date=None, month=None, day=None,
                     caller_scope="public"):
        today = _today(clock, zone_factory)
        current_entries, current_index = entries, index
        if anniversaries_provider is not None and caller_scope == "owner":
            current_entries, current_index = _build_entries(
                merge_anniversaries(anniversaries, await anniversaries_provider())
            )
        name_val = name.strip() if isinstance(name, str) else ""
        date_val = target_date.strip() if isinstance(target_date, str) else ""
        has_month = _is_int(month)
        has_day = _is_int(day)
        sources = bool(name_val) + bool(date_val) + (1 if has_month or has_day else 0)
        if sources > 1 or has_month != has_day:
            return _dump(_single(today, None, None, None, None, "clarify"))
        if not name_val and not date_val and not has_month:
            if caller_scope != "owner":
                # 列表（整表枚举）在每一层都仅主人（关闭 R-C1 的
                # 完整口径）：注册表路径有 caller_check 先行拒绝，
                # action 层同样设防——非主人档位与「无清单」同
                # 表现（unconfigured），不枚举、不泄露任何条目。
                return _dump(_single(today, None, None, None, None, "unconfigured"))
            if not current_entries:
                return _dump(_single(today, None, None, None, None, "unconfigured"))
            resolved = []
            for position, entry in enumerate(current_entries):
                target, years = _resolve(entry, today)
                resolved.append(((target - today).days, position, entry.name, target, years))
            resolved.sort(key=lambda item: (item[0], item[1]))
            total = len(resolved)
            items = []
            for days, _position, label, target, years in resolved:
                item = {
                    "label": label,
                    "target_date": target.isoformat(),
                    "days": days,
                    "anniversary_years": years,
                }
                candidate = {
                    "today": today.isoformat(), "kind": "list",
                    "items": items + [item], "omitted": total - len(items) - 1,
                }
                if _envelope_length(_dump(candidate)) > MAX_OUTPUT_CHARS:
                    break
                items.append(item)
            return _dump({
                "today": today.isoformat(), "kind": "list", "items": items,
                "omitted": total - len(items),
            })
        if date_val:
            target = _parse_date(date_val)
            if target is None:
                return _dump(_single(today, None, date_val, None, None, "invalid_date"))
            return _dump(
                _single(today, target, target.isoformat(), (target - today).days, None, "countdown")
            )
        if has_month:
            label = f"{month}月{day}日"
            try:
                # 闰年语境校验真实性：2 月 29 日合法、2 月 30 日拒绝，
                # 与形态 A 条目的闰年口径一致。
                date(2024, month, day)
            except ValueError:
                return _dump(_single(today, None, label, None, None, "invalid_date"))
            target, _years = _resolve(
                AnniversaryEntry(name=label, month=month, day=day), today
            )
            return _dump(
                _single(today, target, label, (target - today).days, None, "countdown")
            )
        if not current_entries:
            return _dump(_single(today, None, name_val, None, None, "unconfigured"))
        entry = current_index.get(_normalize_name(name_val))
        if entry is None:
            return _dump(_single(today, None, name_val, None, None, "not_found"))
        target, years = _resolve(entry, today)
        return _dump(
            _single(today, target, entry.name, (target - today).days, years, "countdown")
        )

    description = (
        "查询纪念日或具体日期距今天还有多少天（确定性计算）。结果同时含日期，"
        "用户问「XX 是哪天」时同样用本工具查询。"
        "name 为纪念日名称（配置清单或主人对话记录中的名称），必须逐字来自用户当前消息"
        "并与记录名称精确匹配；"
        "target_date 为具体公历日期，必须逐字来自用户当前消息且格式为 YYYY-MM-DD；"
        "month 与 day 为整数月、日：用户只给出月日（如「5月20日」）而没有名称和年份时"
        "成对提供，二者必须同给且数字逐字来自用户当前消息；"
        "name、target_date、month/day 三种来源只提供一种；"
        "用户询问有哪些纪念日时不要提供任何参数。"
    )
    return ReadOnlyTool(
        "date_countdown", description, SCHEMA, action,
        user_input_check=countdown_user_provided,
        caller_check=_countdown_caller_check(owners),
        caller_scope=_caller_scope,
    )


def _valid_date_field(value):
    return isinstance(value, str) and _parse_date(value) is not None


def _is_int(value):
    return type(value) is int


def _anniversary_suffix(years):
    return years is not None and years >= 1


def _render_countdown(label, date_str, days, years):
    explicit = label == date_str
    if days > 0:
        if explicit:
            text = f"{date_str}距今还有{days}天。"
        else:
            text = f"{label}是{date_str}，距今还有{days}天。"
        if _anniversary_suffix(years):
            text = text[:-1] + f"，是第{years}周年。"
        return text
    if days == 0:
        if explicit:
            text = f"{date_str}就是今天。"
        else:
            text = f"{label}就是今天（{date_str}）。"
        if _anniversary_suffix(years):
            text = text[:-1] + f"，是第{years}周年。"
        return text
    n = abs(days)
    if explicit:
        text = f"{date_str}已过{n}天。"
    else:
        text = f"{label}（{date_str}）已过{n}天。"
    if _anniversary_suffix(years):
        text = text[:-1] + f"，是第{years}周年。"
    return text


def _render_list(data):
    if tuple(data) != LIST_KEYS:
        return UNAVAILABLE_REPLY
    if not _valid_date_field(data["today"]):
        return UNAVAILABLE_REPLY
    items = data["items"]
    if not isinstance(items, list) or not items:
        return UNAVAILABLE_REPLY
    omitted = data["omitted"]
    if not _is_int(omitted) or omitted < 0:
        return UNAVAILABLE_REPLY
    today = date.fromisoformat(data["today"])
    lines = ["已配置的纪念日："]
    previous_days = None
    for item in items:
        if not isinstance(item, dict) or tuple(item) != ITEM_KEYS:
            return UNAVAILABLE_REPLY
        label, target_str, days, years = (
            item["label"], item["target_date"], item["days"], item["anniversary_years"],
        )
        if not isinstance(label, str) or not label:
            return UNAVAILABLE_REPLY
        if not _valid_date_field(target_str) or not _is_int(days):
            return UNAVAILABLE_REPLY
        if years is not None and (not _is_int(years) or years < 0):
            return UNAVAILABLE_REPLY
        if (date.fromisoformat(target_str) - today).days != days:
            return UNAVAILABLE_REPLY
        if previous_days is not None and days < previous_days:
            return UNAVAILABLE_REPLY
        previous_days = days
        if days > 0:
            line = f"- {label}：{target_str}，还有{days}天"
        elif days == 0:
            line = f"- {label}：{target_str}，就是今天"
        else:
            line = f"- {label}：{target_str}，已过{abs(days)}天"
        if _anniversary_suffix(years):
            line += f"，是第{years}周年"
        lines.append(line)
    if omitted > 0:
        # 截断不许静默：明示未列出条数（主人据此知道清单未看全）。
        lines.append(f"……还有 {omitted} 条未列出。")
    return "\n".join(lines)


def format_date_countdown(result):
    """Render only a complete, internally consistent gateway result."""
    try:
        wrapper = json.loads(result, object_pairs_hook=_unique_object)
        if not isinstance(wrapper, dict) or set(wrapper) != {"status", "data"}:
            return UNAVAILABLE_REPLY
        if wrapper["status"] != "ok" or not isinstance(wrapper["data"], str):
            return UNAVAILABLE_REPLY
        data = json.loads(wrapper["data"], object_pairs_hook=_unique_object)
        if not isinstance(data, dict):
            return UNAVAILABLE_REPLY
        kind = data.get("kind")
        if kind == "list":
            return _render_list(data)
        if tuple(data) != KEYS:
            return UNAVAILABLE_REPLY
        if not _valid_date_field(data["today"]):
            return UNAVAILABLE_REPLY
        today = date.fromisoformat(data["today"])
        target_str, label, days, years = (
            data["target_date"], data["label"], data["days"], data["anniversary_years"],
        )
        if kind == "countdown":
            if not _valid_date_field(target_str):
                return UNAVAILABLE_REPLY
            if not isinstance(label, str) or not label:
                return UNAVAILABLE_REPLY
            if not _is_int(days) or (years is not None and (not _is_int(years) or years < 0)):
                return UNAVAILABLE_REPLY
            if (date.fromisoformat(target_str) - today).days != days:
                return UNAVAILABLE_REPLY
            return _render_countdown(label, target_str, days, years)
        if target_str is not None or days is not None or years is not None:
            return UNAVAILABLE_REPLY
        if kind == "not_found":
            if not isinstance(label, str) or not label:
                return UNAVAILABLE_REPLY
            return f"没有找到名为「{label}」的纪念日，请确认名称或直接提供日期（YYYY-MM-DD）。"
        if kind == "invalid_date":
            if not isinstance(label, str):
                return UNAVAILABLE_REPLY
            return INVALID_DATE_REPLY
        if kind == "clarify":
            if label is not None:
                return UNAVAILABLE_REPLY
            return CLARIFY_REPLY
        if kind == "unconfigured":
            if label is not None and not isinstance(label, str):
                return UNAVAILABLE_REPLY
            return UNCONFIGURED_REPLY
        return UNAVAILABLE_REPLY
    except (ValueError, TypeError, KeyError, RecursionError):
        return UNAVAILABLE_REPLY


# ---------------------------------------------------------------------------
# 私聊命令面：纪念日管理命令的确定性解析（倒计时体验增强）
#
# 与工具分离：工具是模型驱动的只读查询，管理（增/删/列表）是主人
# 的确定性命令，由 ingress 的 Command Stage 消费本解析结果、经
# AnniversaryStore 落库。解析只认形态、不判主体（主体门在 Stage
# 侧沿用 Admission 分级）；命中从严，避免劫持普通聊天：
# - add：「记一下」须带冒号，或其余部分能完整解析出日期规格；
#   仅「记住」须带冒号。带冒号但规格解析失败 → add_invalid
#   （命令意图明确，Stage 侧给主人格式提示、给非主人拒绝）。
# - remove：「删掉/删除」+ 可选「纪念日」或冒号；裸名称须为无句
#   读标点的短串（整条消息就是命令的形态）。
# - list：固定问法集合（尾部句读标点可有可无）。


class AnniversaryCommand:
    """一条纪念日管理命令的解析结果（kind + 载荷）。"""

    __slots__ = ("kind", "entry", "name")

    def __init__(self, kind, entry=None, name=None):
        self.kind = kind  # "add" | "add_invalid" | "remove" | "list"
        self.entry = entry  # kind == "add" 时的 AnniversaryEntry
        self.name = name  # kind == "remove" 时的目标名称（原文）


_LIST_PHRASES = frozenset({
    "我记了哪些纪念日", "我有哪些纪念日", "有哪些纪念日",
    "纪念日清单", "纪念日列表", "查看纪念日", "我的纪念日",
})
_ADD_COLON_RE = re.compile(r"^(?:帮我)?(?:记一下|记住)[:：]\s*(?P<rest>.+)$")
_ADD_BARE_RE = re.compile(r"^(?:帮我)?记一下\s+(?P<rest>.+)$")
_REMOVE_RE = re.compile(
    r"^(?:帮我)?(?:删掉|删除)(?P<anniv>纪念日)?\s*[:：]?\s*(?P<name>.+)$"
)
_SPEC_B_RE = re.compile(
    r"^(?P<name>.+?)[\s，,、：:]*(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})$",
    re.ASCII,
)
_SPEC_A_YEAR_RE = re.compile(
    r"^(?P<name>.+?)[\s，,、：:]*(?P<year>\d{4})年\s*(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})[日号]$",
    re.ASCII,
)
_SPEC_A_RE = re.compile(
    r"^(?P<name>.+?)[\s，,、：:]*(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})[日号]$",
    re.ASCII,
)
_NAME_SEPARATORS = " \t，,、：:"


def _parse_add_rest(rest):
    """把「名称 + 日期规格」解析为条目；任一形态不合返回 None。

    日期规格与 config 两种条目形态同口径：YYYY-M-D（形态 B，
    补零为严格 YYYY-MM-DD）、YYYY年M月D日（形态 A 带起源年）、
    M月D日/号（形态 A）。真实性与范围经 AnniversaryEntry 校验
    （2 月 30 日、13 月等在此被拒）。
    """
    rest = rest.strip()
    match = _SPEC_B_RE.match(rest)
    if match:
        name = match.group("name").strip(_NAME_SEPARATORS)
        date_str = (
            f"{int(match.group('year')):04d}-{int(match.group('month')):02d}"
            f"-{int(match.group('day')):02d}"
        )
        try:
            return AnniversaryEntry(name=name, date=date_str)
        except Exception:  # noqa: BLE001 — 解析失败即未命中该形态
            return None
    match = _SPEC_A_YEAR_RE.match(rest)
    if match:
        name = match.group("name").strip(_NAME_SEPARATORS)
        year = int(match.group("year"))
        month = int(match.group("month"))
        day = int(match.group("day"))
        try:
            # 带起源年的形态须是真实存在过的日期：2 月 29 日只许
            # 闰年起源（如 2023年2月29日 从未存在，按既有 fail-fast
            # 口径拒绝）；无年份的年度型才走闰年语境宽容。
            date(year, month, day)
        except ValueError:
            return None
        try:
            return AnniversaryEntry(name=name, month=month, day=day, year=year)
        except Exception:  # noqa: BLE001
            return None
    match = _SPEC_A_RE.match(rest)
    if match:
        name = match.group("name").strip(_NAME_SEPARATORS)
        try:
            return AnniversaryEntry(
                name=name,
                month=int(match.group("month")),
                day=int(match.group("day")),
            )
        except Exception:  # noqa: BLE001
            return None
    return None


def parse_anniversary_command(text):
    """解析一条私聊文本是否为纪念日管理命令；未命中返回 None。"""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.rstrip("。？?！!") in _LIST_PHRASES:
        return AnniversaryCommand("list")
    match = _ADD_COLON_RE.match(stripped)
    if match:
        entry = _parse_add_rest(match.group("rest"))
        if entry is not None:
            return AnniversaryCommand("add", entry=entry)
        return AnniversaryCommand("add_invalid")
    match = _ADD_BARE_RE.match(stripped)
    if match:
        entry = _parse_add_rest(match.group("rest"))
        if entry is not None:
            return AnniversaryCommand("add", entry=entry)
        return None
    match = _REMOVE_RE.match(stripped)
    if match:
        # 尾随句读标点与 list 分支同口径容忍：先剥净再过守卫，
        # 不让「删掉结婚纪念日。」落空、也不把 ?/. 粘进目标名。
        name = match.group("name").strip().rstrip("。？?！!；;，,.、").strip()
        marked = (
            match.group("anniv") is not None
            or "：" in stripped
            or ":" in stripped
        )
        if (
            name
            and len(name) <= 64
            and not any(char in name for char in "，。！？；")
            and (marked or " " not in name)
        ):
            return AnniversaryCommand("remove", name=name)
    return None
