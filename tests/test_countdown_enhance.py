"""倒计时体验增强契约测试（分支 ai/qqbot-countdown-enhance）。

覆盖：合并口径（config 为骨、对话存储同名覆盖）、provider 动态条目
（刚记下立刻可查）、month/day 查询形态与防伪造真值表、列表模式主
体门（关闭 R-C1；单查维持开放）、管理命令解析真值表，以及管线级
钉（主人对话增删查全链路、非主人拒绝零泄露、config 同名影子规则、
工具关闭时命令面不存在）。

全部进程内运行：冻结时钟（Asia/Shanghai today=2026-11-01）、合成
身份与假条目，不访问网络、不打真实 QQ。期望文案由冻结 today 与
目标日期独立验算。
"""

import asyncio
import json
from dataclasses import replace

import pytest

import companion.tools.date_countdown as countdown_module
from companion.brain import CompanionBrain, LLMClient
from companion.config import AnniversaryEntry
from companion.ingress import EventProcessor
from companion.safety import ModelLLMJudge
from companion.tool_loop import ProviderTurn, ToolCallRequest
from companion.tools.date_countdown import (
    CLARIFY_REPLY,
    INVALID_DATE_REPLY,
    UNAVAILABLE_REPLY,
    countdown_user_provided,
    date_countdown_tool,
    format_date_countdown,
    merge_anniversaries,
    parse_anniversary_command,
)
from companion.tools.plugins import DateCountdownPlugin, PluginRegistry
from companion.tools.read_only import UNAVAILABLE, ReadOnlyRegistry
from tests.fake_onebot import private_event
from tests.test_date_countdown import FROZEN, _wired_settings, clock_at

pytest_plugins = ["tests.test_app"]

OWNER = "10001"  # _wired_settings / setup_app 的准入名单
STRANGER = "10002"

ENTRIES = [
    {"name": "结婚纪念日", "month": 6, "day": 15, "year": 2020},
    {"name": "生日", "month": 12, "day": 24},
    {"name": "闰日纪念", "month": 2, "day": 29, "year": 2024},
    {"name": "春节", "date": "2027-02-17"},
]

OWNER_ONLY_REPLY = "纪念日的记录、查看和删除只能由主人本人来操作，我这边没法帮你处理。"
FORMAT_REPLY = (
    "格式是：记一下：纪念日名称 + 日期，比如「记一下：结婚纪念日 5月20日」，"
    "也可以写完整日期「记一下：春节 2027-02-17」。"
)


# ------------------------------------------------------------ 注册表脚手架


def _registry(entries=(), *, provider=None, owner_ids=frozenset({OWNER})):
    instance = ReadOnlyRegistry(
        enabled=True, policies={"date_countdown": {"enabled": True}}
    )
    PluginRegistry(instance).register_plugin(
        DateCountdownPlugin(
            entries, clock_at(FROZEN), None,
            anniversaries_provider=provider, owner_ids=owner_ids,
        )
    )
    return instance


def _invoke(instance, args, message, identity=OWNER):
    return asyncio.run(
        instance.invoke(
            "date_countdown",
            json.dumps(args, ensure_ascii=False),
            identity,
            user_message=message,
        )
    )


def _render(instance, args, message, identity=OWNER):
    result = _invoke(instance, args, message, identity)
    return instance.disposition("date_countdown", result).text


# ------------------------------------------------------------ 合并口径


def test_merge_stored_wins_in_place_and_appends():
    config = [
        {"name": "结婚纪念日", "month": 6, "day": 15, "year": 2020},
        {"name": "生日", "month": 12, "day": 24},
    ]
    stored = [
        AnniversaryEntry(name="结婚纪念日", month=5, day=20),
        AnniversaryEntry(name="春节", date="2027-02-17"),
    ]
    merged = merge_anniversaries(config, stored)
    assert [entry.name for entry in merged] == ["结婚纪念日", "生日", "春节"]
    # 同名以存储为准且保位（config 骨架序不乱）。
    assert (merged[0].month, merged[0].day, merged[0].year) == (5, 20, None)


def test_merge_config_duplicates_fail_fast():
    config = [
        {"name": "重复", "month": 1, "day": 1},
        {"name": "重复", "month": 2, "day": 2},
    ]
    with pytest.raises(ValueError):
        merge_anniversaries(config, ())


def test_merge_empty_inputs():
    assert merge_anniversaries((), ()) == ()
    assert merge_anniversaries(None, None) == ()


# ------------------------------------------------------------ provider 动态条目


def test_provider_entries_are_live_without_rebuild():
    stored = []

    async def provider():
        return tuple(stored)

    reg = _registry((), provider=provider)
    assert _render(reg, {"name": "结婚纪念日"}, "结婚纪念日还有几天") == (
        "尚未配置纪念日清单，暂时无法查询纪念日。"
    )
    stored.append(AnniversaryEntry(name="结婚纪念日", month=5, day=20))
    assert _render(reg, {"name": "结婚纪念日"}, "结婚纪念日还有几天") == (
        "结婚纪念日是2027-05-20，距今还有200天。"
    )


# -------------------------------------------- 可见性按来源分（复审 P2 修正）


def _stored_provider(stored):
    async def provider():
        return tuple(stored)

    return provider


def test_stored_entries_invisible_to_strangers_on_name_query():
    stored = [AnniversaryEntry(name="宝宝生日", month=5, day=20, year=2015)]
    reg = _registry(ENTRIES, provider=_stored_provider(stored), owner_ids={OWNER})
    # 主人查存储条目：正常命中（含起源周年）。
    assert _render(reg, {"name": "宝宝生日"}, "宝宝生日还有几天", identity=OWNER) == (
        "宝宝生日是2027-05-20，距今还有200天，是第12周年。"
    )
    # 陌生人猜中存储条目名：与查一个根本不存在的名字同结果，
    # 不泄露存在性、日期与周年。
    stranger_hit = _render(
        reg, {"name": "宝宝生日"}, "宝宝生日还有几天", identity=STRANGER
    )
    stranger_miss = _render(
        reg, {"name": "不存在的纪念日"}, "不存在的纪念日还有几天", identity=STRANGER
    )
    assert stranger_hit == (
        "没有找到名为「宝宝生日」的纪念日，请确认名称或直接提供日期（YYYY-MM-DD）。"
    )
    assert stranger_miss == (
        "没有找到名为「不存在的纪念日」的纪念日，请确认名称或直接提供日期（YYYY-MM-DD）。"
    )
    # 静态条目对陌生人维持现状可见。
    assert _render(reg, {"name": "结婚纪念日"}, "结婚纪念日还有几天", identity=STRANGER) == (
        "结婚纪念日是2027-06-15，距今还有226天，是第7周年。"
    )


def test_stranger_sees_static_version_of_shadowed_name():
    # 同名影子：存储覆盖只对主人成立；陌生人看到的是静态条目本身。
    stored = [AnniversaryEntry(name="结婚纪念日", month=5, day=20)]
    reg = _registry(ENTRIES, provider=_stored_provider(stored), owner_ids={OWNER})
    assert _render(reg, {"name": "结婚纪念日"}, "结婚纪念日还有几天", identity=OWNER) == (
        "结婚纪念日是2027-05-20，距今还有200天。"
    )
    assert _render(reg, {"name": "结婚纪念日"}, "结婚纪念日还有几天", identity=STRANGER) == (
        "结婚纪念日是2027-06-15，距今还有226天，是第7周年。"
    )


def test_bare_action_call_defaults_to_public_scope():
    # fix1 复审 P2：动作层 caller_scope 缺省必须是最小权限——
    # 裸直调（不传档位）与显式 public 同结果，存储条目不可见；
    # 只有显式 owner 才命中。
    stored = [AnniversaryEntry(name="宝宝生日", month=5, day=20, year=2015)]
    tool = date_countdown_tool(
        ENTRIES, clock_at(FROZEN), None,
        anniversaries_provider=_stored_provider(stored), owner_ids={OWNER},
    )

    async def run():
        bare = await tool.action(name="宝宝生日")
        public = await tool.action(name="宝宝生日", caller_scope="public")
        owner = await tool.action(name="宝宝生日", caller_scope="owner")
        return bare, public, owner

    def render(inner):
        return format_date_countdown(json.dumps({"status": "ok", "data": inner}))

    bare, public, owner = asyncio.run(run())
    not_found = "没有找到名为「宝宝生日」的纪念日，请确认名称或直接提供日期（YYYY-MM-DD）。"
    assert render(bare) == not_found
    assert render(public) == not_found
    assert render(owner) == "宝宝生日是2027-05-20，距今还有200天，是第12周年。"


def _format_inner(inner):
    return format_date_countdown(json.dumps({"status": "ok", "data": inner}))


def test_action_layer_list_mode_owner_only():
    # list（整表枚举）在每一层都仅主人（fix2 复审 P3）：裸直调与
    # public 档位在 action 层与「无清单」同表现，不含任何条目；
    # owner 档位全量。按名查静态条目在缺省档位维持命中（单查
    # 开放口径不动）。
    tool = date_countdown_tool(ENTRIES, clock_at(FROZEN), None, owner_ids={OWNER})

    async def run():
        return (
            await tool.action(),
            await tool.action(caller_scope="public"),
            await tool.action(caller_scope="owner"),
            await tool.action(name="结婚纪念日"),
        )

    bare, public, owner, bare_name = asyncio.run(run())
    unconfigured = "尚未配置纪念日清单，暂时无法查询纪念日。"
    assert _format_inner(bare) == unconfigured
    assert _format_inner(public) == unconfigured
    owner_text = _format_inner(owner)
    assert "结婚纪念日" in owner_text and "春节" in owner_text
    assert _format_inner(bare_name) == "结婚纪念日是2027-06-15，距今还有226天，是第7周年。"


def test_action_layer_list_guard_with_provider():
    # 带 provider 的构造同口径：public 档位 list 不枚举（连静态
    # 部分也不给），owner 档位见合并全量（含存储条目）。
    stored = [AnniversaryEntry(name="宝宝生日", month=5, day=20, year=2015)]
    tool = date_countdown_tool(
        ENTRIES, clock_at(FROZEN), None,
        anniversaries_provider=_stored_provider(stored), owner_ids={OWNER},
    )

    async def run():
        return (
            await tool.action(caller_scope="public"),
            await tool.action(caller_scope="owner"),
        )

    public, owner = asyncio.run(run())
    assert _format_inner(public) == "尚未配置纪念日清单，暂时无法查询纪念日。"
    owner_text = _format_inner(owner)
    assert "宝宝生日" in owner_text and "结婚纪念日" in owner_text


def test_action_layer_noncanonical_scopes_fail_closed():
    # fix3 复审 P4（部署预备轮补）：非规范 caller_scope 值——
    # None、大小写变体、带空格、任意字符串——一律非主人待遇：
    # list 与「无清单」同表现，存储条目单查与不存在同结果，
    # 且 provider 零调用（档位判定为精确等值 "owner"）。
    stored = [AnniversaryEntry(name="宝宝生日", month=5, day=20, year=2015)]
    provider_calls = []

    def provider():
        provider_calls.append(1)
        return list(stored)

    tool = date_countdown_tool(
        ENTRIES, clock_at(FROZEN), None,
        anniversaries_provider=provider, owner_ids={OWNER},
    )
    scopes = (None, "OWNER", "Owner", "owner ", "admin", "")

    async def run():
        out = {}
        for scope in scopes:
            out[scope] = (
                await tool.action(caller_scope=scope),
                await tool.action(name="宝宝生日", caller_scope=scope),
            )
        return out

    results = asyncio.run(run())
    for scope, (listed, single) in results.items():
        assert _format_inner(listed) == (
            "尚未配置纪念日清单，暂时无法查询纪念日。"
        ), scope
        assert _format_inner(single) == (
            "没有找到名为「宝宝生日」的纪念日，"
            "请确认名称或直接提供日期（YYYY-MM-DD）。"
        ), scope
    assert provider_calls == []


def test_provider_not_invoked_for_public_scope():
    calls = []

    async def counting_provider():
        calls.append(1)
        return (AnniversaryEntry(name="宝宝生日", month=5, day=20),)

    reg = _registry(ENTRIES, provider=counting_provider, owner_ids={OWNER})
    # 陌生人的三种单查面都不触存储 provider，静态结果照常返回。
    assert _render(reg, {"name": "结婚纪念日"}, "结婚纪念日还有几天", identity=STRANGER) == (
        "结婚纪念日是2027-06-15，距今还有226天，是第7周年。"
    )
    assert _render(reg, {"target_date": "2027-02-17"}, "2027-02-17还有几天", identity=STRANGER) == (
        "2027-02-17距今还有108天。"
    )
    assert _render(reg, {"month": 5, "day": 20}, "离5月20日还有几天", identity=STRANGER) == (
        "5月20日是2027-05-20，距今还有200天。"
    )
    assert calls == []
    _render(reg, {"name": "结婚纪念日"}, "结婚纪念日还有几天", identity=OWNER)
    assert calls


def test_raising_provider_isolated_to_owner_scope():
    async def raising_provider():
        raise RuntimeError("store down")

    reg = _registry(ENTRIES, provider=raising_provider, owner_ids={OWNER})
    # 存储故障不污染陌生人的静态查询面。
    assert _render(reg, {"name": "结婚纪念日"}, "结婚纪念日还有几天", identity=STRANGER) == (
        "结婚纪念日是2027-06-15，距今还有226天，是第7周年。"
    )
    # 主人侧查询收敛为不可用，绝不假造结果。
    assert _invoke(reg, {"name": "结婚纪念日"}, "结婚纪念日还有几天", identity=OWNER) == (
        UNAVAILABLE
    )


def test_stored_entries_invisible_when_owner_set_empty():
    stored = [AnniversaryEntry(name="宝宝生日", month=5, day=20, year=2015)]
    reg = _registry((), provider=_stored_provider(stored), owner_ids=set())
    # fail-closed：无人是主人时存储条目对所有人不可见（同不存在）。
    assert _render(reg, {"name": "宝宝生日"}, "宝宝生日还有几天", identity=OWNER) == (
        "尚未配置纪念日清单，暂时无法查询纪念日。"
    )


# ------------------------------------------------------------ 列表截断明示


def test_list_truncation_is_announced_with_exact_omitted_count():
    entries = [
        AnniversaryEntry(name=f"纪念{i}", month=1, day=1) for i in range(50)
    ]
    reg = _registry(entries, owner_ids={OWNER})
    text = _render(reg, {}, "有哪些纪念日", identity=OWNER)
    listed = sum(1 for line in text.splitlines() if line.startswith("- "))
    assert 0 < listed < 50
    assert text.splitlines()[-1] == f"……还有 {50 - listed} 条未列出。"


def test_list_without_truncation_has_no_marker():
    reg = _registry(ENTRIES, owner_ids={OWNER})
    text = _render(reg, {}, "有哪些纪念日", identity=OWNER)
    assert "未列出" not in text


# ------------------------------------------------------------ month/day 查询形态


def test_month_day_query_resolves_next_occurrence():
    reg = _registry(ENTRIES)
    assert _render(reg, {"month": 5, "day": 20}, "离5月20日还有几天") == (
        "5月20日是2027-05-20，距今还有200天。"
    )
    assert _render(reg, {"month": 12, "day": 25}, "12月25日还有几天") == (
        "12月25日是2026-12-25，距今还有54天。"
    )


def test_month_day_impossible_date_is_invalid():
    reg = _registry(ENTRIES)
    assert _render(reg, {"month": 2, "day": 30}, "2月30日还有几天") == INVALID_DATE_REPLY


def test_month_day_leap_day_resolves_to_feb28_observance():
    # 2月29日：闰年语境校验合法，非闰年按 2月28日 观察（与形态 A
    # 条目同一 _resolve 口径）。冻结 today=2026-11-01 → 2027-02-28。
    reg = _registry(ENTRIES)
    assert _render(reg, {"month": 2, "day": 29}, "2月29日还有几天") == (
        "2月29日是2027-02-28，距今还有119天。"
    )


def test_month_day_multi_source_clarifies():
    reg = _registry(ENTRIES)
    args = {"name": "结婚纪念日", "month": 6, "day": 15}
    assert _render(reg, args, "结婚纪念日，6月15日还有几天") == CLARIFY_REPLY


def test_month_day_lone_value_refused_by_fabrication_check():
    reg = _registry(ENTRIES)
    assert _invoke(reg, {"month": 5}, "5月20日还有几天") == UNAVAILABLE


@pytest.mark.parametrize(
    "args,message,expected",
    [
        ({"month": 5, "day": 20}, "离5月20日还有几天", True),
        ({"month": 2, "day": 29}, "2月29日还有几天", True),
        ({"month": 5, "day": 20}, "离05月20日还有几天", True),
        ({"month": 5, "day": 20}, "5月20号是哪天", True),
        ({"month": 5, "day": 20}, "2026年5月20日还有几天", True),
        ({"month": 5, "day": 20}, "15月20日还有几天", False),
        ({"month": 5, "day": 20}, "5月2日还有几天", False),
        ({"month": 5, "day": 20}, "随便聊聊", False),
        ({"month": 5}, "5月20日还有几天", False),
        ({"day": 20}, "5月20日还有几天", False),
    ],
)
def test_month_day_fabrication_truth_table(args, message, expected):
    assert countdown_user_provided(args, message) is expected


# ------------------------------------------------------------ 列表主体门（R-C1）


def test_list_gate_owner_only_stranger_gets_unavailable():
    reg = _registry(ENTRIES, owner_ids={OWNER})
    owner_text = _render(reg, {}, "有哪些纪念日", identity=OWNER)
    assert "结婚纪念日" in owner_text
    assert _invoke(reg, {}, "有哪些纪念日", identity=STRANGER) == UNAVAILABLE
    stranger_text = _render(reg, {}, "有哪些纪念日", identity=STRANGER)
    assert stranger_text == UNAVAILABLE_REPLY
    assert "结婚纪念日" not in stranger_text and "春节" not in stranger_text


def test_list_gate_keeps_single_queries_open_to_strangers():
    reg = _registry(ENTRIES, owner_ids={OWNER})
    assert _render(reg, {"name": "结婚纪念日"}, "结婚纪念日还有几天", identity=STRANGER) == (
        "结婚纪念日是2027-06-15，距今还有226天，是第7周年。"
    )
    assert _render(reg, {"month": 5, "day": 20}, "离5月20日还有几天", identity=STRANGER) == (
        "5月20日是2027-05-20，距今还有200天。"
    )
    # target_date 直查同为单查面，对陌生人维持开放（caller_check 透传）。
    assert _render(
        reg, {"target_date": "2027-02-17"}, "2027-02-17还有几天", identity=STRANGER
    ) == "2027-02-17距今还有108天。"


@pytest.mark.parametrize("owner_ids", [None, set()])
def test_owner_gate_fail_closed_without_owner_set(owner_ids):
    # owner_ids=None 或空集：fail-closed——任何身份都不是主人，
    # 列表对所有人关闭（包括口径上的主人 id 本人）。
    reg = _registry(ENTRIES, owner_ids=owner_ids)
    for identity in (OWNER, STRANGER):
        assert _invoke(reg, {}, "有哪些纪念日", identity=identity) == UNAVAILABLE


# ------------------------------------------------------------ 命令解析真值表


@pytest.mark.parametrize(
    "text,kind",
    [
        ("记一下：结婚纪念日 5月20日", "add"),
        ("记一下：春节 2027-02-17", "add"),
        ("记一下：结婚纪念日 2020年6月15日", "add"),
        ("记住：生日 12月24日", "add"),
        ("帮我记一下：生日 12月24日", "add"),
        ("记一下 结婚纪念日 5月20日", "add"),
        ("记一下：结婚纪念日 2月30日", "add_invalid"),
        ("记一下：随便聊聊", "add_invalid"),
        ("删掉结婚纪念日", "remove"),
        ("删掉纪念日结婚纪念日", "remove"),
        ("删除：结婚纪念日", "remove"),
        ("帮我删掉 结婚纪念日", "remove"),
        ("我记了哪些纪念日", "list"),
        ("我记了哪些纪念日？", "list"),
        ("纪念日清单", "list"),
        ("我的纪念日。", "list"),
    ],
)
def test_command_parse_hits(text, kind):
    command = parse_anniversary_command(text)
    assert command is not None and command.kind == kind


def test_command_parse_add_payload_forms():
    form_a = parse_anniversary_command("记一下：结婚纪念日 5月20日")
    assert (form_a.entry.name, form_a.entry.month, form_a.entry.day) == (
        "结婚纪念日", 5, 20,
    )
    assert form_a.entry.year is None and form_a.entry.date is None
    form_a_year = parse_anniversary_command("记一下：结婚纪念日 2020年6月15日")
    assert form_a_year.entry.year == 2020
    form_b = parse_anniversary_command("记一下：春节 2027-02-17")
    assert form_b.entry.date == "2027-02-17"
    remove = parse_anniversary_command("删掉结婚纪念日")
    assert remove.name == "结婚纪念日"


@pytest.mark.parametrize(
    "text",
    [
        "删掉结婚纪念日。",
        "删掉结婚纪念日！",
        "删掉结婚纪念日?",
        "删掉结婚纪念日.",
        "删掉结婚纪念日；",
    ],
)
def test_command_parse_remove_trailing_punctuation(text):
    # 尾随句读标点与 list 分支同口径容忍：剥净后命中 remove，
    # 标点不粘进目标名。
    command = parse_anniversary_command(text)
    assert command is not None and command.kind == "remove"
    assert command.name == "结婚纪念日"


def test_command_parse_feb29_origin_year_must_exist():
    # 带起源年：2023 年没有 2月29日 → 拒绝（colon 形态 add_invalid）；
    # 2024 年是闰年 → 正常 add。无年份的年度型 2月29日 不受此限。
    bad = parse_anniversary_command("记一下：闰日纪念 2023年2月29日")
    assert bad is not None and bad.kind == "add_invalid"
    good = parse_anniversary_command("记一下：闰日纪念 2024年2月29日")
    assert good is not None and good.kind == "add"
    assert (good.entry.month, good.entry.day, good.entry.year) == (2, 29, 2024)
    plain = parse_anniversary_command("记一下：闰日纪念 2月29日")
    assert plain is not None and plain.kind == "add" and plain.entry.year is None


@pytest.mark.parametrize(
    "text",
    [
        "今天天气怎么样",
        "记一下今天天气不错",
        "记住生日快乐",
        "记一下 随便聊聊",
        "删掉 他 昨天 说的话",
        "我们有哪些纪念日活动",
        "",
        "   ",
    ],
)
def test_command_parse_misses_fall_back_to_chat(text):
    assert parse_anniversary_command(text) is None
    assert parse_anniversary_command(None) is None


# ------------------------------------------------------------ 管线级脚手架


class _RecordingSender:
    def __init__(self):
        self.calls = []

    async def send_private_msg(self, user_id, message, *, priority="normal"):
        self.calls.append({"user_id": user_id, "message": message})
        return {"status": "ok"}

    async def send_alert_private_msg(self, user_id, message):
        return await self.send_private_msg(user_id, message)


class _SafeJudgeLLM:
    async def generate(self, _prompt):
        return "NO"


class _CountingBrain:
    """命令面用例的大脑桩：被调用即计数，管线断言其零调用。"""

    def __init__(self):
        self.calls = 0

    async def respond(self, *_args, **_kwargs):
        self.calls += 1
        return "不应被调用的生成回复"

    async def respond_multipart(self, *_args, **_kwargs):
        self.calls += 1
        return '{"bubbles": ["不应被调用的生成回复"]}'

    async def respond_with_tools(self, *_args, **_kwargs):
        self.calls += 1
        return "不应被调用的生成回复", False


def _tool_call(call_id, arguments):
    raw = {
        "id": call_id, "type": "function",
        "function": {"name": "date_countdown", "arguments": arguments},
    }
    return ToolCallRequest(
        id=call_id, name="date_countdown", arguments=arguments, raw=raw
    )


class _ScriptedProvider(LLMClient):
    supports_tools = True
    model_name = "scripted-countdown-test"

    def __init__(self, turns):
        self._turns = list(turns)

    async def generate(self, _prompt):
        return "普通回复"

    async def tool_turn(self, _messages, _schemas, *, tools_enabled):
        return self._turns.pop(0)


def _processor(settings, monkeypatch, brain, anniversary_store=None):
    monkeypatch.setattr(countdown_module, "_system_clock", clock_at(FROZEN))
    sender = _RecordingSender()
    processor = EventProcessor(
        settings, sender=sender, brain=brain, anniversary_store=anniversary_store
    )
    processor.safety.judge = ModelLLMJudge(_SafeJudgeLLM())
    for user in (OWNER, STRANGER):
        asyncio.run(processor.conversation.mark_onboarded(user))
    return processor, sender


def _say(processor, user, text):
    return asyncio.run(processor.handle(private_event(user_id=user, text=text)))


def _stored_names(processor):
    entries = asyncio.run(processor.anniversary_store.list_entries())
    return [entry.name for entry in entries]


# ------------------------------------------------------------ 管线：查询体验


def test_pipeline_owner_name_query_via_tool(setup_app, monkeypatch):
    _, _, _, base = setup_app
    settings = _wired_settings(base, ENTRIES)
    brain = CompanionBrain(
        settings.persona,
        _ScriptedProvider([
            ProviderTurn(calls=(_tool_call("1", '{"name": "结婚纪念日"}'),)),
        ]),
    )
    processor, sender = _processor(settings, monkeypatch, brain)
    assert _say(processor, OWNER, "结婚纪念日还有几天")
    assert sender.calls[-1]["message"] == "结婚纪念日是2027-06-15，距今还有226天，是第7周年。"


def test_pipeline_owner_month_day_query_via_tool(setup_app, monkeypatch):
    _, _, _, base = setup_app
    settings = _wired_settings(base, [])
    brain = CompanionBrain(
        settings.persona,
        _ScriptedProvider([
            ProviderTurn(calls=(_tool_call("1", '{"month": 5, "day": 20}'),)),
        ]),
    )
    processor, sender = _processor(settings, monkeypatch, brain)
    assert _say(processor, OWNER, "离5月20日还有几天")
    assert sender.calls[-1]["message"] == "5月20日是2027-05-20，距今还有200天。"


# ------------------------------------------------------------ 管线：主人管理全链路


def test_pipeline_owner_add_query_list_remove_flow(setup_app, monkeypatch):
    _, _, _, base = setup_app
    settings = _wired_settings(base, [])
    brain = CompanionBrain(
        settings.persona,
        _ScriptedProvider([
            ProviderTurn(calls=(_tool_call("1", '{"name": "结婚纪念日"}'),)),
        ]),
    )
    processor, sender = _processor(settings, monkeypatch, brain)

    assert _say(processor, OWNER, "记一下：结婚纪念日 5月20日")
    assert sender.calls[-1]["message"] == "记好了。结婚纪念日是2027-05-20，距今还有200天。"
    assert _stored_names(processor) == ["结婚纪念日"]

    # 刚记下立刻可经工具查（provider 合并视图，无需重启/重建）。
    assert _say(processor, OWNER, "结婚纪念日是哪天")
    assert sender.calls[-1]["message"] == "结婚纪念日是2027-05-20，距今还有200天。"

    assert _say(processor, OWNER, "我记了哪些纪念日")
    assert sender.calls[-1]["message"] == (
        "已配置的纪念日：\n- 结婚纪念日：2027-05-20，还有200天"
    )

    assert _say(processor, OWNER, "删掉结婚纪念日")
    assert sender.calls[-1]["message"] == "已删掉「结婚纪念日」。"
    assert _stored_names(processor) == []

    assert _say(processor, OWNER, "我记了哪些纪念日")
    assert sender.calls[-1]["message"] == (
        "你还没有记任何纪念日。可以对我说：记一下：结婚纪念日 5月20日。"
    )


def test_pipeline_add_survives_processor_rebuild(setup_app, monkeypatch):
    _, _, _, base = setup_app
    settings = _wired_settings(base, [])
    brain = _CountingBrain()
    processor, _sender = _processor(settings, monkeypatch, brain)
    assert _say(processor, OWNER, "记一下：结婚纪念日 5月20日")
    assert brain.calls == 0

    # 同一 settings（同一 DB 文件）重建处理器：存储条目仍在。
    brain2 = CompanionBrain(
        settings.persona,
        _ScriptedProvider([
            ProviderTurn(calls=(_tool_call("1", '{"name": "结婚纪念日"}'),)),
        ]),
    )
    processor2, sender2 = _processor(settings, monkeypatch, brain2)
    assert _stored_names(processor2) == ["结婚纪念日"]
    assert _say(processor2, OWNER, "结婚纪念日还有几天")
    assert sender2.calls[-1]["message"] == "结婚纪念日是2027-05-20，距今还有200天。"


def test_pipeline_owner_add_invalid_remove_config_only_and_missing(
    setup_app, monkeypatch
):
    _, _, _, base = setup_app
    settings = _wired_settings(base, ENTRIES)
    brain = _CountingBrain()
    processor, sender = _processor(settings, monkeypatch, brain)

    assert _say(processor, OWNER, "记一下：结婚纪念日 2月30日")
    assert sender.calls[-1]["message"] == FORMAT_REPLY

    assert _say(processor, OWNER, "删掉春节")
    assert sender.calls[-1]["message"] == (
        "「春节」是写在配置文件里的，我这边删不掉，需要改配置才行。"
    )

    assert _say(processor, OWNER, "删掉不存在的纪念日")
    assert sender.calls[-1]["message"] == "没有找到你记的「不存在的纪念日」。"
    assert brain.calls == 0


def test_pipeline_owner_store_full_reply(setup_app, monkeypatch):
    _, _, _, base = setup_app
    settings = _wired_settings(base, [])
    brain = _CountingBrain()
    processor, sender = _processor(settings, monkeypatch, brain)

    async def prefill():
        for index in range(50):
            await processor.anniversary_store.upsert(
                AnniversaryEntry(name=f"纪念{index}", month=1, day=1)
            )

    asyncio.run(prefill())
    assert _say(processor, OWNER, "记一下：新纪念日 5月20日")
    assert sender.calls[-1]["message"] == "记得太多了（最多 50 条），先删掉一些再记新的吧。"
    # 更新既有条目不受上限影响。
    assert _say(processor, OWNER, "记一下：纪念0 6月15日")
    assert sender.calls[-1]["message"].startswith("已更新。")
    # 50 条的命令面列表必然截断：须明示未列出条数，不许静默。
    assert _say(processor, OWNER, "我记了哪些纪念日")
    assert "条未列出。" in sender.calls[-1]["message"]


def test_pipeline_config_entry_shadowed_by_stored_then_restored(
    setup_app, monkeypatch
):
    _, _, _, base = setup_app
    settings = _wired_settings(
        base, [{"name": "结婚纪念日", "month": 6, "day": 15, "year": 2020}]
    )
    brain = CompanionBrain(
        settings.persona,
        _ScriptedProvider([
            ProviderTurn(calls=(_tool_call("1", '{"name": "结婚纪念日"}'),)),
            ProviderTurn(calls=(_tool_call("2", '{"name": "结婚纪念日"}'),)),
        ]),
    )
    processor, sender = _processor(settings, monkeypatch, brain)

    assert _say(processor, OWNER, "记一下：结婚纪念日 5月20日")
    assert sender.calls[-1]["message"] == "记好了。结婚纪念日是2027-05-20，距今还有200天。"

    assert _say(processor, OWNER, "结婚纪念日还有几天")
    assert sender.calls[-1]["message"] == "结婚纪念日是2027-05-20，距今还有200天。"

    assert _say(processor, OWNER, "删掉结婚纪念日")
    assert sender.calls[-1]["message"] == (
        "已删掉「结婚纪念日」。配置文件里还有一条同名纪念日，之后会按那条显示。"
    )

    # 存储条目删除后，同名 config 条目重新可见（合并是视图）。
    assert _say(processor, OWNER, "结婚纪念日还有几天")
    assert sender.calls[-1]["message"] == "结婚纪念日是2027-06-15，距今还有226天，是第7周年。"


# ------------------------------------------------------------ 管线：非主人


def test_pipeline_stranger_management_refused_without_leak(setup_app, monkeypatch):
    _, _, _, base = setup_app
    settings = _wired_settings(base, ENTRIES)
    brain = _CountingBrain()
    processor, sender = _processor(settings, monkeypatch, brain)

    for text in (
        "记一下：结婚纪念日 5月20日",
        "记一下：结婚纪念日 2月30日",  # add_invalid：主体门先于格式分发
        "记一下：随便聊聊",  # add_invalid 同口径拒绝，不给格式提示
        "我记了哪些纪念日",
        "删掉结婚纪念日",
    ):
        assert _say(processor, STRANGER, text)
        message = sender.calls[-1]["message"]
        assert message == OWNER_ONLY_REPLY
        assert "结婚" not in message and "春节" not in message
    # 拒绝路径不查不写存储。
    assert _stored_names(processor) == []
    assert brain.calls == 0


def test_pipeline_stranger_tool_list_blocked_single_query_open(
    setup_app, monkeypatch
):
    _, _, _, base = setup_app
    settings = _wired_settings(base, ENTRIES)
    brain = CompanionBrain(
        settings.persona,
        _ScriptedProvider([
            ProviderTurn(calls=(_tool_call("1", "{}"),)),
            ProviderTurn(calls=(_tool_call("2", '{"name": "结婚纪念日"}'),)),
        ]),
    )
    processor, sender = _processor(settings, monkeypatch, brain)

    # 问法避开命令面列表短语集（「有哪些纪念日」会被命令分支以
    # OWNER_ONLY 先行拒绝，见非主人管理用例），专钉工具面主体门。
    assert _say(processor, STRANGER, "纪念日都有哪些呀")
    message = sender.calls[-1]["message"]
    assert message == UNAVAILABLE_REPLY
    assert "结婚纪念日" not in message and "春节" not in message

    assert _say(processor, STRANGER, "结婚纪念日还有几天")
    assert sender.calls[-1]["message"] == "结婚纪念日是2027-06-15，距今还有226天，是第7周年。"


def test_pipeline_stored_entry_invisible_to_stranger_tool_query(
    setup_app, monkeypatch
):
    # P2 管线钉：主人对话存入的条目，陌生人经工具按名查与查不存在
    # 同结果；主人查正常命中；静态条目对陌生人维持可见（前一用例）。
    _, _, _, base = setup_app
    settings = _wired_settings(base, [{"name": "春节", "date": "2027-02-17"}])
    brain = CompanionBrain(
        settings.persona,
        _ScriptedProvider([
            ProviderTurn(calls=(_tool_call("1", '{"name": "宝宝生日"}'),)),
            ProviderTurn(calls=(_tool_call("2", '{"name": "不存在的纪念日"}'),)),
            ProviderTurn(calls=(_tool_call("3", '{"name": "宝宝生日"}'),)),
        ]),
    )
    processor, sender = _processor(settings, monkeypatch, brain)

    assert _say(processor, OWNER, "记一下：宝宝生日 2015年5月20日")
    assert sender.calls[-1]["message"] == (
        "记好了。宝宝生日是2027-05-20，距今还有200天，是第12周年。"
    )

    # 陌生人猜中存储名 → not_found；与查真不存在的名字同形态，
    # 仅名称回显不同（名称本就来自提问者自己），存在性零泄露。
    assert _say(processor, STRANGER, "宝宝生日还有几天")
    assert sender.calls[-1]["message"] == (
        "没有找到名为「宝宝生日」的纪念日，请确认名称或直接提供日期（YYYY-MM-DD）。"
    )
    assert _say(processor, STRANGER, "不存在的纪念日还有几天")
    assert sender.calls[-1]["message"] == (
        "没有找到名为「不存在的纪念日」的纪念日，请确认名称或直接提供日期（YYYY-MM-DD）。"
    )

    assert _say(processor, OWNER, "宝宝生日还有几天")
    assert sender.calls[-1]["message"] == (
        "宝宝生日是2027-05-20，距今还有200天，是第12周年。"
    )


def test_pipeline_owner_remove_with_trailing_punctuation(setup_app, monkeypatch):
    _, _, _, base = setup_app
    settings = _wired_settings(base, [])
    brain = _CountingBrain()
    processor, sender = _processor(settings, monkeypatch, brain)
    assert _say(processor, OWNER, "记一下：结婚纪念日 5月20日")
    assert _say(processor, OWNER, "删掉结婚纪念日。")
    assert sender.calls[-1]["message"] == "已删掉「结婚纪念日」。"
    assert _stored_names(processor) == []
    assert brain.calls == 0


# ------------------------------------------------ 管线：存储失败契约（P3）


class _FlakyStore:
    """AnniversaryStore 故障注入替身：按开关让指定方法抛错。"""

    def __init__(self, *, fail_upsert=False, fail_list=False, fail_remove=False):
        self.saved = {}
        self.fail_upsert = fail_upsert
        self.fail_list = fail_list
        self.fail_remove = fail_remove

    async def upsert(self, entry):
        if self.fail_upsert:
            raise RuntimeError("injected upsert failure")
        created = entry.name not in self.saved
        self.saved[entry.name] = entry
        return created

    async def list_entries(self):
        if self.fail_list:
            raise RuntimeError("injected list failure")
        return tuple(self.saved.values())

    async def remove(self, name):
        if self.fail_remove:
            raise RuntimeError("injected remove failure")
        return self.saved.pop(name, None) is not None

    async def get(self, name):
        return self.saved.get(name)


def test_pipeline_add_write_failure_reports_failed_and_stores_nothing(
    setup_app, monkeypatch
):
    _, _, _, base = setup_app
    settings = _wired_settings(base, [])
    store = _FlakyStore(fail_upsert=True)
    processor, sender = _processor(
        settings, monkeypatch, _CountingBrain(), anniversary_store=store
    )
    assert _say(processor, OWNER, "记一下：结婚纪念日 5月20日")
    assert sender.calls[-1]["message"] == "纪念日保存失败，请稍后再试。"
    assert store.saved == {}


def test_pipeline_add_post_write_failure_still_confirms_saved(
    setup_app, monkeypatch
):
    # 已落存、后段（合并视图读取）失败：绝不误报「保存失败」，
    # 回退为纯确认——条目实际已存（复审 P3 契约钉）。
    _, _, _, base = setup_app
    settings = _wired_settings(base, [])
    store = _FlakyStore(fail_list=True)
    processor, sender = _processor(
        settings, monkeypatch, _CountingBrain(), anniversary_store=store
    )
    assert _say(processor, OWNER, "记一下：结婚纪念日 5月20日")
    message = sender.calls[-1]["message"]
    assert message == "记好了。「结婚纪念日」已保存。"
    assert "保存失败" not in message
    assert list(store.saved) == ["结婚纪念日"]


def test_pipeline_anniversary_render_passes_owner_scope_explicitly(
    setup_app, monkeypatch
):
    # 变异敏感钉（fix2 复审 P3）：_anniversary_render 必须显式以
    # owner 档位调用工具动作——以记录型工厂替换 ingress 的
    # date_countdown_tool 并断言实参；删掉显式传参（退回缺省
    # public）本钉即红，不再依赖行为惰性。
    from dataclasses import replace as _dc_replace

    import companion.ingress as ingress_module

    seen = []
    real_factory = ingress_module.date_countdown_tool

    def recording_factory(*args, **kwargs):
        tool = real_factory(*args, **kwargs)
        real_action = tool.action

        async def recording_action(**call_kwargs):
            seen.append(call_kwargs.get("caller_scope"))
            return await real_action(**call_kwargs)

        return _dc_replace(tool, action=recording_action)

    monkeypatch.setattr(ingress_module, "date_countdown_tool", recording_factory)
    _, _, _, base = setup_app
    settings = _wired_settings(base, [])
    processor, sender = _processor(settings, monkeypatch, _CountingBrain())
    assert _say(processor, OWNER, "记一下：结婚纪念日 5月20日")
    assert sender.calls[-1]["message"].startswith("记好了。")
    assert seen and all(scope == "owner" for scope in seen)


def test_pipeline_add_post_write_failure_update_variant(setup_app, monkeypatch):
    # 已落存后段失败的更新变体：upsert 返回 False（更新）时回退
    # 文案须是「已更新。」而非「记好了。」，同样不许报保存失败。
    _, _, _, base = setup_app
    settings = _wired_settings(base, [])
    store = _FlakyStore(fail_list=True)
    store.saved["结婚纪念日"] = AnniversaryEntry(name="结婚纪念日", month=5, day=20)
    processor, sender = _processor(
        settings, monkeypatch, _CountingBrain(), anniversary_store=store
    )
    assert _say(processor, OWNER, "记一下：结婚纪念日 6月15日")
    message = sender.calls[-1]["message"]
    assert message == "已更新。「结婚纪念日」已保存。"
    assert "保存失败" not in message


def test_failure_reply_mapping_covers_all_command_kinds():
    from companion.ingress import (
        ANNIVERSARY_FAILED_REPLY,
        ANNIVERSARY_FAILURE_REPLIES,
        ANNIVERSARY_FORMAT_REPLY,
        ANNIVERSARY_LIST_FAILED_REPLY,
        ANNIVERSARY_REMOVE_FAILED_REPLY,
    )

    # 键与值都钉死：任一值被换（尤其 add_invalid 误指保存失败
    # 文案）本钉即红（fix2 复审 P4——只断言键集时变异不红）。
    assert ANNIVERSARY_FAILURE_REPLIES == {
        "add": ANNIVERSARY_FAILED_REPLY,
        "add_invalid": ANNIVERSARY_FORMAT_REPLY,
        "remove": ANNIVERSARY_REMOVE_FAILED_REPLY,
        "list": ANNIVERSARY_LIST_FAILED_REPLY,
    }
    assert ANNIVERSARY_FAILURE_REPLIES["add_invalid"] == FORMAT_REPLY


def test_pipeline_remove_and_list_failures_use_operation_texts(
    setup_app, monkeypatch
):
    _, _, _, base = setup_app
    settings = _wired_settings(base, [])
    store = _FlakyStore(fail_remove=True)
    processor, sender = _processor(
        settings, monkeypatch, _CountingBrain(), anniversary_store=store
    )
    assert _say(processor, OWNER, "删掉结婚纪念日")
    assert sender.calls[-1]["message"] == "删除纪念日失败，请稍后再试。"

    store2 = _FlakyStore(fail_list=True)
    processor2, sender2 = _processor(
        settings, monkeypatch, _CountingBrain(), anniversary_store=store2
    )
    assert _say(processor2, OWNER, "我记了哪些纪念日")
    assert sender2.calls[-1]["message"] == "纪念日清单读取失败，请稍后再试。"


# ------------------------------------------------------------ 管线：工具关闭


def test_pipeline_tool_disabled_command_surface_absent(setup_app, monkeypatch):
    _, _, _, base = setup_app
    wired = _wired_settings(base, [])
    app = {
        **wired.app,
        "read_only_tools": {
            "enabled": True,
            "policies": {
                "date_countdown": {
                    "enabled": False, "rate_limit": 6, "window_seconds": 60,
                    "anniversaries": [],
                }
            },
        },
    }
    settings = replace(wired, app=app)
    brain = CompanionBrain(settings.persona, _ScriptedProvider([]))
    processor, sender = _processor(settings, monkeypatch, brain)

    assert _say(processor, OWNER, "记一下：结婚纪念日 5月20日")
    message = sender.calls[-1]["message"]
    assert "记好了" not in message
    assert _stored_names(processor) == []
