"""date_countdown 只读工具契约测试（PRD AC1–AC20、AC22）。

本文件覆盖的 AC：AC1–AC20 与 AC22（逐条见各节横幅）。
AC21（全量回归）不在本文件内设用例：其实质是全量 `scripts/test.sh`
运行放电，证据在实例 handoffs/04-dev-reports/T5.md 的全量计数记录
（及审查修复轮的 review-fix-1.md）。

全部进程内运行：冻结时钟（Asia/Shanghai today=2026-11-01，除非用例另注）、
fake 条目与合成身份，不访问网络、不打真实 QQ。期望文案逐字取 PRD §5.6/§5.7。
脚手架仿 tests/test_current_time.py 的 registry 构造与 instant 助手。
"""

import ast
import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import companion.tools.date_countdown as countdown_module
from companion.brain import CompanionBrain
from companion.config import (
    HOT_RELOADABLE_CONFIG_PATHS,
    AppConfig,
    ConfigValidationError,
    model_config_field_paths,
    validate_app_config,
)
from companion.ingress import EventProcessor
from companion.tools.date_countdown import (
    CLARIFY_REPLY,
    INVALID_DATE_REPLY,
    UNAVAILABLE_REPLY,
    UNCONFIGURED_REPLY,
    _observed,
    countdown_user_provided,
    format_date_countdown,
)
from companion.tools.plugins import DateCountdownPlugin, PluginRegistry, build_plugin_registry
from companion.tools.read_only import UNAVAILABLE, ReadOnlyRegistry

pytest_plugins = ["tests.test_app"]

ROOT = Path(__file__).resolve().parents[1]
FROZEN = "2026-11-01T04:00:00"  # UTC 04:00 = 上海 12:00，today=2026-11-01

ENTRIES = [
    {"name": "结婚纪念日", "month": 6, "day": 15, "year": 2020},
    {"name": "生日", "month": 12, "day": 24},
    {"name": "闰日纪念", "month": 2, "day": 29, "year": 2024},
    {"name": "春节", "date": "2027-02-17"},
]


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def clock_at(value, calls=None):
    def clock():
        if calls is not None:
            calls.append(1)
        return instant(value)

    return clock


def registry(entries=(), clock=None, *, enabled=True, policy=True, zone_factory=None):
    instance = ReadOnlyRegistry(
        enabled=enabled, policies={"date_countdown": {"enabled": policy}}
    )
    PluginRegistry(instance).register_plugin(
        # 列表主体门 fail-closed 后，本文件合成身份须显式登记为
        # 主人才可渲染列表（断言口径不变，仅装配补齐 owner_ids）。
        DateCountdownPlugin(
            entries, clock, zone_factory,
            owner_ids={"synthetic", "synthetic-2"},
        )
    )
    return instance


def frozen_registry(entries=ENTRIES, calls=None):
    return registry(entries, clock_at(FROZEN, calls))


def invoke(instance, args, message, identity="synthetic"):
    return asyncio.run(
        instance.invoke(
            "date_countdown",
            json.dumps(args, ensure_ascii=False),
            identity,
            user_message=message,
        )
    )


def render(instance, args, message, identity="synthetic"):
    result = invoke(instance, args, message, identity)
    disposition = instance.disposition("date_countdown", result)
    assert disposition.kind == "direct"
    return disposition.text


# ---------------------------------------------------------------- AC1–AC3


def test_ac1_explicit_future_date():
    assert render(frozen_registry(), {"target_date": "2027-02-17"}, "2027-02-17还有几天") == (
        "2027-02-17距今还有108天。"
    )


def test_ac2_explicit_today():
    assert render(frozen_registry(), {"target_date": "2026-11-01"}, "2026-11-01是哪天") == (
        "2026-11-01就是今天。"
    )


def test_ac3_explicit_past_never_rolls():
    text = render(frozen_registry(), {"target_date": "2026-10-01"}, "2026-10-01过去了吗")
    assert text == "2026-10-01已过31天。"
    assert "2027" not in text


# ------------------------------------------------------------- AC4–AC6/AC18


def test_ac4_recurring_rolls_with_anniversary():
    assert render(frozen_registry(), {"name": "结婚纪念日"}, "结婚纪念日还有几天") == (
        "结婚纪念日是2027-06-15，距今还有226天，是第7周年。"
    )


def test_ac5_anniversary_on_today():
    reg = registry(ENTRIES, clock_at("2027-06-15T04:00:00"))
    assert render(reg, {"name": "结婚纪念日"}, "结婚纪念日是今天吗") == (
        "结婚纪念日就是今天（2027-06-15），是第7周年。"
    )


def test_ac6_leap_day_observed_on_feb28_in_common_year():
    assert render(frozen_registry(), {"name": "闰日纪念"}, "闰日纪念还有几天") == (
        "闰日纪念是2027-02-28，距今还有119天，是第3周年。"
    )


def test_ac18_leap_day_valid_config_and_leap_year_observance():
    config = validate_app_config(_raw_with_entries([{"name": "闰日纪念", "month": 2, "day": 29}]))
    assert config.read_only_tools.policies.date_countdown.anniversaries[0].day == 29
    reg = registry(
        [{"name": "闰日纪念", "month": 2, "day": 29, "year": 2024}],
        clock_at("2028-01-01T04:00:00"),
    )
    text = render(reg, {"name": "闰日纪念"}, "闰日纪念还有几天")
    assert text == "闰日纪念是2028-02-29，距今还有59天，是第4周年。"
    assert "2028-02-29" in text and "还有59天" in text


def test_recurring_without_year_has_no_anniversary_suffix():
    assert render(frozen_registry(), {"name": "生日"}, "生日还有几天") == (
        "生日是2026-12-24，距今还有53天。"
    )


def test_form_b_future_by_name():
    assert render(frozen_registry(), {"name": "春节"}, "春节还有几天") == (
        "春节是2027-02-17，距今还有108天。"
    )


def test_future_origin_year_yields_no_anniversary_years():
    # 起源年（2099）晚于解析目标年（2027）：anniversary_years 置 null，
    # 渲染不带周年句、不得出现负数周年
    reg = registry(
        [{"name": "未来起源", "month": 6, "day": 15, "year": 2099}], clock_at(FROZEN)
    )
    result = invoke(reg, {"name": "未来起源"}, "未来起源还有几天")
    data = json.loads(json.loads(result)["data"])
    assert data["target_date"] == "2027-06-15"
    assert data["anniversary_years"] is None
    assert reg.disposition("date_countdown", result).text == (
        "未来起源是2027-06-15，距今还有226天。"
    )


# ------------------------------------------------------------ AC7–AC10


def test_ac7_unknown_name():
    reg = frozen_registry()
    result = invoke(reg, {"name": "元旦"}, "元旦还有几天")
    assert json.loads(result)["status"] == "ok"
    assert reg.disposition("date_countdown", result).text == (
        "没有找到名为「元旦」的纪念日，请确认名称或直接提供日期（YYYY-MM-DD）。"
    )


@pytest.mark.parametrize("bad", ["2026-13-01", "2027-02-29", "2026/11/01"])
def test_ac8_invalid_dates(bad):
    assert render(frozen_registry(), {"target_date": bad}, f"{bad}还有几天") == INVALID_DATE_REPLY


def test_ac9_unconfigured_empty_entries():
    reg = registry((), clock_at(FROZEN))
    assert render(reg, {"name": "生日"}, "生日还有几天") == UNCONFIGURED_REPLY
    assert render(reg, {}, "有哪些纪念日", identity="synthetic-2") == UNCONFIGURED_REPLY


def test_ac10_both_arguments_clarify():
    args = {"name": "结婚纪念日", "target_date": "2027-06-15"}
    assert render(frozen_registry(), args, "结婚纪念日 2027-06-15 帮我查") == CLARIFY_REPLY


# ------------------------------------------------------------ AC11/AC12


def test_ac11_list_mode_sorted_verbatim():
    expected = (
        "已配置的纪念日：\n"
        "- 生日：2026-12-24，还有53天\n"
        "- 春节：2027-02-17，还有108天\n"
        "- 闰日纪念：2027-02-28，还有119天，是第3周年\n"
        "- 结婚纪念日：2027-06-15，还有226天，是第7周年"
    )
    assert render(frozen_registry(), {}, "有哪些纪念日") == expected


def test_list_mode_mixed_past_and_today_verbatim():
    entries = [
        {"name": "已过纪念", "date": "2026-10-01"},
        {"name": "今日纪念", "date": "2026-11-01"},
        {"name": "未来纪念", "date": "2027-02-17"},
    ]
    expected = (
        "已配置的纪念日：\n"
        "- 已过纪念：2026-10-01，已过31天\n"
        "- 今日纪念：2026-11-01，就是今天\n"
        "- 未来纪念：2027-02-17，还有108天"
    )
    assert render(registry(entries, clock_at(FROZEN)), {}, "有哪些纪念日") == expected


def test_ac12_form_b_past_does_not_roll():
    reg = registry(ENTRIES, clock_at("2027-03-01T04:00:00"))
    assert render(reg, {"name": "春节"}, "春节是哪天") == "春节（2027-02-17）已过12天。"


# ------------------------------------------------------------------ AC13


def test_ac13_fabricated_argument_never_executes_action():
    calls = []
    reg = frozen_registry(calls=calls)
    result = invoke(reg, {"name": "生日"}, "帮我查一下")
    assert result == UNAVAILABLE
    assert calls == []  # user_input_check 在 action 之前拦截，时钟零调用


@pytest.mark.parametrize("args,message,expected", [
    ({}, "任意消息", True),
    ({"name": ""}, "任意消息", True),
    ({"name": "生日"}, "生日是哪天", True),
    ({"name": "生日"}, "帮我查一下", False),
    ({"target_date": "2027-02-17"}, "2027-02-17还有几天", True),
    ({"target_date": "２０２７-０２-１７"}, "２０２７-０２-１７还有几天", True),
    ({"target_date": "2027-02-17"}, "12027-02-17还有几天", False),
    ({"name": "生日", "target_date": "2027-02-17"}, "生日是哪天", False),
    ({"name": "生日", "target_date": "2027-02-17"}, "生日和2027-02-17", True),
    ({"name": "abc"}, "xabcx", False),
    ({"name": "abc"}, "abc def", True),
    # 首个出现边界失败、后续出现合法：遍历全部出现位置，不得误拒
    ({"target_date": "2027-02-17"}, "单号12027-02-17，后来2027-02-17还有几天", True),
    ({"name": "abc"}, "xabcx 里 abc 是哪天", True),
    # 名称分支与日期分支对齐：数字/下划线/连字符粘连同样拒绝
    ({"name": "abc"}, "1abc", False),
    ({"name": "abc"}, "abc1", False),
    ({"name": "abc"}, "_abc", False),
    ({"name": "abc"}, "abc-def", False),
    # 复审 N6：边界矩阵补前导连字符与尾随下划线组合
    ({"name": "abc"}, "-abc", False),
    ({"name": "abc"}, "abc_", False),
    # 复审 N1：数字边缘名称的 CJK 邻接不得误拒（紧邻检查只认
    # ASCII [A-Za-z0-9_-]，不用 Unicode isalnum）
    ({"name": "618"}, "618还有几天", True),
    ({"name": "双11"}, "双11还有几天", True),
    ({"name": "v2"}, "v2是哪天", True),
    # 复审 N2：名称边缘为非 ASCII 字母/'_' 时同样无条件受检
    ({"name": "café"}, "caféx 是哪天", False),
    ({"name": "café"}, "café 是哪天", True),
    ({"name": "_abc"}, "x_abc 是哪天", False),
    ({"name": "abc_"}, "abc_x 是哪天", False),
    # 复审 N3：日期分支对 ASCII 字母粘连与数字/'-' 同口径拒绝
    ({"target_date": "2027-02-17"}, "2027-02-17abc", False),
    ({"target_date": "2027-02-17"}, "x2027-02-17", False),
    # 复审第 3 轮：CJK 豁免收窄——豁免仅限 CJK 脚本邻接，
    # 非 CJK 字母/数字邻接仍算粘连（é/Ω/阿拉伯-印度数字）
    ({"name": "abc"}, "abcé是哪天", False),
    ({"name": "abc"}, "abcΩ是哪天", False),
    ({"name": "618"}, "٦618还有几天", False),
    ({"target_date": "2027-02-17"}, "٦2027-02-17还有几天", False),
    # 复审第 3 轮补：日期分支 '_'/'-' 邻接同样算粘连
    ({"target_date": "2027-02-17"}, "_2027-02-17还有几天", False),
    ({"target_date": "2027-02-17"}, "2027-02-17_还有几天", False),
    ({"target_date": "2027-02-17"}, "2027-02-17-还有几天", False),
    # 复审第 4 轮：豁免脚本逐项钉死 + 表意标记豁免 + 不可见字符算粘连
    # (1) 豁免脚本逐项正例（HIRAGANA/KATAKANA/HANGUL）
    ({"name": "abc"}, "abcあ是哪天", True),
    ({"name": "abc"}, "abcア是哪天", True),
    ({"name": "abc"}, "abc한是哪天", True),
    # (2) 表意标记豁免（IDEOGRAPHIC 前缀：々/〇 同属 CJK 文本字符）
    ({"name": "abc"}, "abc々是哪天", True),
    ({"name": "618"}, "〇618还有几天", True),
    # (3) 不可见字符算粘连（Cf/Mn/Me 不得充当 token 分隔符）
    ({"name": "abc"}, "abc\u200bdef是哪天", False),
    ({"name": "abc"}, "abc\u034fdef是哪天", False),
    ({"target_date": "2027-02-17"}, "2027-02-17\u200bx还有几天", False),
    # 复审第 5 轮：Cc 控制字符同算粘连 + Me/〆 钉死 + 前置侧不可见字符
    ({"name": "abc"}, "abc\x00def是哪天", False),
    ({"name": "abc"}, "abc\x7fdef是哪天", False),
    ({"name": "abc"}, "abc\u009fdef是哪天", False),
    ({"name": "abc"}, "abc\u20dddef是哪天", False),
    ({"name": "abc"}, "abc〆是哪天", True),
    ({"name": "abc"}, "x\u200babc是哪天", False),
    ({"target_date": "2027-02-17"}, "x\u200b2027-02-17还有几天", False),
])
def test_ac13_user_provided_truth_table(args, message, expected):
    assert countdown_user_provided(args, message) is expected


# ------------------------------------------------------------------ AC14


def _envelope(data):
    return json.dumps(
        {"status": "ok", "data": json.dumps(data, ensure_ascii=False, separators=(",", ":"))},
        ensure_ascii=False,
    )


def test_ac14_direct_policy():
    assert frozen_registry().result_policy("date_countdown") == "direct"


def test_ac14_render_rejects_corrupt_results():
    assert format_date_countdown("not json") == UNAVAILABLE_REPLY
    assert format_date_countdown(UNAVAILABLE) == UNAVAILABLE_REPLY
    assert format_date_countdown('{"status":"ok"}') == UNAVAILABLE_REPLY
    good = {
        "today": "2026-11-01", "target_date": "2027-02-17", "label": "2027-02-17",
        "days": 108, "anniversary_years": None, "kind": "countdown",
    }
    assert format_date_countdown(_envelope(good)) == "2027-02-17距今还有108天。"
    # days 与实际日差矛盾
    assert format_date_countdown(_envelope({**good, "days": 107})) == UNAVAILABLE_REPLY
    # 键序错位
    reordered = dict(reversed(list(good.items())))
    assert format_date_countdown(_envelope(reordered)) == UNAVAILABLE_REPLY
    # 字段缺失
    missing = {k: v for k, v in good.items() if k != "days"}
    assert format_date_countdown(_envelope(missing)) == UNAVAILABLE_REPLY
    # list 的 days 非递减被破坏（omitted 键须齐备且合法，信封才
    # 能到达排序守卫——fix1 复审 P3：夹具缺键会被键形检查先拒，
    # 目标守卫失去变异敏感性）
    bad_list = {
        "today": "2026-11-01", "kind": "list",
        "items": [
            {"label": "甲", "target_date": "2027-02-17", "days": 108, "anniversary_years": None},
            {"label": "乙", "target_date": "2026-12-24", "days": 53, "anniversary_years": None},
        ],
        "omitted": 0,
    }
    assert format_date_countdown(_envelope(bad_list)) == UNAVAILABLE_REPLY
    # 非 countdown 种类的自洽守卫：字段该为 null 而非 null 即拒绝
    not_found = {
        "today": "2026-11-01", "target_date": None, "label": "元旦",
        "days": 5, "anniversary_years": None, "kind": "not_found",
    }
    assert format_date_countdown(_envelope(not_found)) == UNAVAILABLE_REPLY
    clarify = {
        "today": "2026-11-01", "target_date": None, "label": "生日",
        "days": None, "anniversary_years": None, "kind": "clarify",
    }
    assert format_date_countdown(_envelope(clarify)) == UNAVAILABLE_REPLY
    unconfigured = {
        "today": "2026-11-01", "target_date": None, "label": 123,
        "days": None, "anniversary_years": None, "kind": "unconfigured",
    }
    assert format_date_countdown(_envelope(unconfigured)) == UNAVAILABLE_REPLY
    # days 为 bool 不是 int（_is_int 以 type() is int 排除 bool）
    assert format_date_countdown(_envelope({**good, "days": True})) == UNAVAILABLE_REPLY
    # 负 anniversary_years 与数据契约不一致，拒绝
    assert format_date_countdown(_envelope({**good, "anniversary_years": -72})) == (
        UNAVAILABLE_REPLY
    )
    # 复审 N5（T9）：清单模式负周年渲染守卫须变异敏感——构造除
    # anniversary_years<0 外完全自洽（已排序、日差一致）的清单
    # 信封，删去 _render_list 的负周年守卫即会渲染出文本而变红
    corrupt_list_negative_years = {
        "today": "2026-11-01", "kind": "list",
        "items": [
            {
                "label": "甲", "target_date": "2027-02-17", "days": 108,
                "anniversary_years": -1,
            },
        ],
        "omitted": 0,
    }
    assert format_date_countdown(_envelope(corrupt_list_negative_years)) == (
        UNAVAILABLE_REPLY
    )
    # omitted 校验本身（fix1 复审 P3）：除 omitted 外完全合法的
    # 清单信封正常渲染；omitted 为负数/bool/字符串即拒绝——删去
    # _render_list 的 omitted 校验则这些变体会被当作未截断渲染。
    good_list = {
        "today": "2026-11-01", "kind": "list",
        "items": [
            {
                "label": "甲", "target_date": "2027-02-17", "days": 108,
                "anniversary_years": None,
            },
        ],
        "omitted": 0,
    }
    assert format_date_countdown(_envelope(good_list)) == (
        "已配置的纪念日：\n- 甲：2027-02-17，还有108天"
    )
    assert format_date_countdown(_envelope({**good_list, "omitted": 2})) == (
        "已配置的纪念日：\n- 甲：2027-02-17，还有108天\n……还有 2 条未列出。"
    )
    for bad_omitted in (-1, True, "1", None):
        assert format_date_countdown(
            _envelope({**good_list, "omitted": bad_omitted})
        ) == UNAVAILABLE_REPLY


# ------------------------------------------------------------------ AC15


@pytest.mark.parametrize(
    "enabled,policy", [(False, False), (True, False), (False, True), (True, True)]
)
def test_ac15_schemas_follow_double_switch(enabled, policy):
    reg = registry(ENTRIES, clock_at(FROZEN), enabled=enabled, policy=policy)
    names = [s["function"]["name"] for s in reg.schemas()]
    assert ("date_countdown" in names) is (enabled and policy)


def test_ac15_build_plugin_registry_registers_four_tools_by_default():
    instance = ReadOnlyRegistry(
        enabled=True,
        policies={
            name: {"enabled": True}
            for name in ("current_time", "date_countdown", "weather_lookup", "web_search")
        },
    )
    build_plugin_registry(instance)  # 不传 countdown_anniversaries：向后兼容
    names = [s["function"]["name"] for s in instance.schemas()]
    assert sorted(names) == ["current_time", "date_countdown", "weather_lookup", "web_search"]


def test_ac15_existing_policy_defaults_unchanged():
    config = validate_app_config({"allowed_private_user_ids": ["10001"]})
    for name in ("current_time", "weather_lookup", "web_search"):
        policy = getattr(config.read_only_tools.policies, name)
        assert policy.enabled is False and policy.rate_limit == 6 and policy.window_seconds == 60
    policy = config.read_only_tools.policies.date_countdown
    assert policy.enabled is False and policy.rate_limit == 6
    assert policy.window_seconds == 60 and policy.anniversaries == ()


# ------------------------------------------------------------------ AC16


def test_ac16_new_leaf_paths_present():
    paths = model_config_field_paths(AppConfig)
    assert {
        "read_only_tools.policies.date_countdown.enabled",
        "read_only_tools.policies.date_countdown.rate_limit",
        "read_only_tools.policies.date_countdown.window_seconds",
        "read_only_tools.policies.date_countdown.anniversaries",
    } <= paths


def test_ac16_hot_reload_contract():
    # 仅 enabled 叶子可热更新；其余叶子变更须重启（docs/config.md 口径）
    assert "read_only_tools.policies.date_countdown.enabled" in HOT_RELOADABLE_CONFIG_PATHS
    assert "read_only_tools.policies.date_countdown.anniversaries" not in (
        HOT_RELOADABLE_CONFIG_PATHS
    )
    assert "read_only_tools.policies.date_countdown.rate_limit" not in (
        HOT_RELOADABLE_CONFIG_PATHS
    )
    assert "read_only_tools.policies.date_countdown.window_seconds" not in (
        HOT_RELOADABLE_CONFIG_PATHS
    )


# ------------------------------------------------------------------ AC17


def _raw_with_entries(entries):
    return {
        "allowed_private_user_ids": ["10001"],
        "read_only_tools": {"policies": {"date_countdown": {"anniversaries": entries}}},
    }


@pytest.mark.parametrize("entries,secret_name", [
    ([{"name": "ＡＢＣ", "month": 1, "day": 1}, {"name": "abc", "month": 2, "day": 2}], "ＡＢＣ"),
    ([{"name": "混给纪念", "date": "2027-01-01", "month": 1, "day": 1}], "混给纪念"),
    ([{"name": "混给年份", "date": "2027-01-01", "year": 2020}], "混给年份"),
    ([{"name": "二月三十", "month": 2, "day": 30}], "二月三十"),
    ([{"name": f"纪念{i}", "month": 1, "day": 1} for i in range(51)], "纪念50"),
    ([{"name": "未知键纪念", "month": 1, "day": 1, "bogus": 1}], "未知键纪念"),
    ([{"name": "年份过小", "month": 1, "day": 1, "year": 1899}], "年份过小"),
    ([{"name": "年份过大", "month": 1, "day": 1, "year": 2101}], "年份过大"),
    ([{"name": "   ", "month": 1, "day": 1}], "   "),
    ([{"name": "长" * 65, "month": 1, "day": 1}], "长" * 65),
    ([{"name": "零月纪念", "month": 0, "day": 1}], "零月纪念"),
    ([{"name": "十三月纪念", "month": 13, "day": 1}], "十三月纪念"),
    ([{"name": "零日纪念", "month": 1, "day": 0}], "零日纪念"),
    ([{"name": "只给月纪念", "month": 5}], "只给月纪念"),
    ([{"name": "不存在日期", "date": "2027-02-29"}], "不存在日期"),
])
def test_ac17_invalid_configs_fail_fast_without_echoing_values(entries, secret_name):
    with pytest.raises(ConfigValidationError) as excinfo:
        validate_app_config(_raw_with_entries(entries))
    assert secret_name not in str(excinfo.value)


# ------------------------------------------------------------------ AC19


def test_ac19_imports_within_allowlist():
    tree = ast.parse((ROOT / "companion/tools/date_countdown.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported <= {
        "json", "re", "unicodedata", "calendar", "datetime", "zoneinfo",
        "companion.config", "companion.tools.read_only",
    }


def test_ac19_no_network_during_invoke(monkeypatch):
    import socket

    def fail(*_args, **_kwargs):
        pytest.fail("network access")

    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket.socket, "connect_ex", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert render(frozen_registry(), {"name": "生日"}, "生日还有几天") == (
        "生日是2026-12-24，距今还有53天。"
    )


# ------------------------------------------------------------------ AC20


def test_ac20_clock_is_the_only_time_source():
    args = {"target_date": "2027-02-17"}
    first = render(registry((), clock_at(FROZEN)), args, "2027-02-17还有几天")
    second = render(
        registry((), clock_at("2027-02-17T04:00:00")), args, "2027-02-17还有几天",
    )
    assert first == "2027-02-17距今还有108天。"
    assert second == "2027-02-17就是今天。"


def test_ac20_no_direct_system_clock_calls_outside_default_factory():
    tree = ast.parse((ROOT / "companion/tools/date_countdown.py").read_text(encoding="utf-8"))
    now_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "now"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "datetime"
    ]
    today_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "today"
    ]
    assert len(now_calls) == 1  # 仅缺省 _system_clock 一处
    assert today_calls == []


# ------------------------------------------------------------------ AC22


@pytest.mark.parametrize("moment", ["2026-10-31T16:00:01", "2026-11-01T15:59:59"])
def test_ac22_same_calendar_day_same_output(moment):
    reg = registry(ENTRIES, clock_at(moment))
    assert render(reg, {"target_date": "2027-02-17"}, "2027-02-17还有几天") == (
        "2027-02-17距今还有108天。"
    )


# ------------------------------------------------------- 补充防回归（design §8）


@pytest.mark.parametrize("year,month,day,expected", [
    (2027, 2, 29, date(2027, 2, 28)),
    (2028, 2, 29, date(2028, 2, 29)),
    (2026, 6, 15, date(2026, 6, 15)),
    (2026, 12, 24, date(2026, 12, 24)),
])
def test_observed_table(year, month, day, expected):
    assert _observed(year, month, day) == expected


def test_list_envelope_budget_fitting_keeps_sorted_prefix():
    entries = [
        {"name": f"纪念日{i:02d}" + "长" * 50, "month": (i % 12) + 1, "day": 15}
        for i in range(50)
    ]
    reg = registry(entries, clock_at(FROZEN))
    result = invoke(reg, {}, "有哪些纪念日")
    envelope = json.dumps(
        {"status": "ok", "data": json.loads(result)["data"]},
        ensure_ascii=True, separators=(",", ":"),
    )
    assert len(envelope) <= 2000
    items = json.loads(json.loads(result)["data"])["items"]
    assert 0 < len(items) < 50
    # 期望顺序由硬编码日期规则在本用例内独立推导（每月 15 日：已过
    # 今年者滚动到明年），不 import 生产 _resolve 计算期望。
    today = date(2026, 11, 1)
    expected = []
    for position, raw in enumerate(entries):
        candidate = date(2026, raw["month"], 15)
        if candidate < today:
            candidate = date(2027, raw["month"], 15)
        expected.append(((candidate - today).days, position, raw["name"], candidate))
    expected.sort(key=lambda item: (item[0], item[1]))
    assert [item["label"] for item in items] == [
        label for _, _, label, _target in expected[: len(items)]
    ]
    # 渲染逐字断言：截断前缀的每一行都与独立推导的目标日期/天数一致，
    # 末行另须明示未列出条数（复审 P3：截断不许静默，计数由本用例
    # 的 50 条总量与实际列出数独立推导）。
    expected_lines = ["已配置的纪念日："] + [
        f"- {label}：{target.isoformat()}，还有{days}天"
        for days, _position, label, target in expected[: len(items)]
    ] + [f"……还有 {50 - len(items)} 条未列出。"]
    assert reg.disposition("date_countdown", result).text.split("\n") == expected_lines


# ------------------------------------------------------- T4 生产接线（ingress）


class _FakeSender:
    async def send_private_msg(self, user_id, message, *, priority="normal"):
        return {"status": "ok"}

    async def send_alert_private_msg(self, user_id, message):
        return {"status": "ok"}


class _FakeLLM:
    async def generate(self, _prompt):
        return "测试回复"

    async def generate_json(self, _prompt):
        return json.dumps({"bubbles": ["测试回复"]}, ensure_ascii=False)


def _processor(settings):
    return EventProcessor(
        settings,
        sender=_FakeSender(),
        brain=CompanionBrain(settings.persona, _FakeLLM()),
    )


def _wired_settings(base, entries, *, with_config=True):
    policies = {
        "date_countdown": {
            "enabled": True, "rate_limit": 6, "window_seconds": 60,
            "anniversaries": entries,
        }
    }
    app = {**base.app, "read_only_tools": {"enabled": True, "policies": policies}}
    raw = {"allowed_private_user_ids": ["10001"], "read_only_tools": app["read_only_tools"]}
    return replace(
        base, app=app, config=validate_app_config(raw) if with_config else None
    )


def _frozen_processor(settings, monkeypatch):
    # 处理器装配无时钟注入点：装配前把 date_countdown 模块的缺省系统
    # 时钟换成冻结时钟（插件在装配期以 clock=None 绑定缺省时钟），
    # 期望文案全部由冻结 today（2026-11-01）与目标日期推导，彻底消除
    # 旧写法「真实墙钟读 today + 期望写死还有10天」的跨午夜竞态。
    monkeypatch.setattr(countdown_module, "_system_clock", clock_at(FROZEN))
    return _processor(settings)


def test_t4_ingress_uses_validated_config_entries(setup_app, monkeypatch):
    _, _, _, base = setup_app
    settings = _wired_settings(base, [{"name": "动态纪念", "date": "2026-11-11"}])
    processor = _frozen_processor(settings, monkeypatch)
    reg = processor.read_only_registry
    assert "date_countdown" in [s["function"]["name"] for s in reg.schemas()]
    text = render(reg, {"name": "动态纪念"}, "动态纪念还有几天")
    assert text == "动态纪念是2026-11-11，距今还有10天。"


def test_t4_ingress_config_tree_form_a_entry(setup_app, monkeypatch):
    _, _, _, base = setup_app
    entries = [{"name": "动态周年", "month": 11, "day": 11, "year": 2020}]
    settings = _wired_settings(base, entries)
    processor = _frozen_processor(settings, monkeypatch)
    reg = processor.read_only_registry
    assert render(reg, {"name": "动态周年"}, "动态周年还有几天") == (
        "动态周年是2026-11-11，距今还有10天，是第6周年。"
    )


def test_t4_ingress_fallback_validates_raw_entries(setup_app, monkeypatch):
    _, _, _, base = setup_app
    settings = _wired_settings(
        base, [{"name": "动态纪念", "date": "2026-11-11"}], with_config=False
    )
    processor = _frozen_processor(settings, monkeypatch)
    reg = processor.read_only_registry
    assert "date_countdown" in [s["function"]["name"] for s in reg.schemas()]
    assert render(reg, {"name": "动态纪念"}, "动态纪念还有几天") == (
        "动态纪念是2026-11-11，距今还有10天。"
    )


def test_t4_ingress_fallback_form_a_entry(setup_app, monkeypatch):
    _, _, _, base = setup_app
    entries = [{"name": "动态周年", "month": 11, "day": 11, "year": 2020}]
    settings = _wired_settings(base, entries, with_config=False)
    processor = _frozen_processor(settings, monkeypatch)
    reg = processor.read_only_registry
    assert render(reg, {"name": "动态周年"}, "动态周年还有几天") == (
        "动态周年是2026-11-11，距今还有10天，是第6周年。"
    )


def test_t4_ingress_fallback_rejects_invalid_entries_fail_fast(setup_app):
    _, _, _, base = setup_app
    settings = _wired_settings(
        base, [{"name": "坏条目", "month": 2, "day": 30}], with_config=False
    )
    # 回退路径整表经 DateCountdownPolicyConfig 校验（复审 N4 起）：
    # 装配期 fail-fast 的异常改与配置树同口径——捕获 pydantic
    # ValidationError 后改抛 ConfigValidationError，只报键路径、
    # 不回显条目值（原 ValidationError 的类型断言随之更新）
    with pytest.raises(ConfigValidationError) as excinfo:
        _processor(settings)
    assert "坏条目" not in str(excinfo.value)
    # 复审第 3 轮（P3-2）正向断言：异常须真的携带键路径（不只是
    # 不回显）——非空、完整前缀、定位到出错条目叶子、无尾随点；
    # 实测 loc 形状为 ('anniversaries', 0)（条目级 model_validator）
    assert excinfo.value.paths
    assert all(
        path.startswith("read_only_tools.policies.date_countdown.anniversaries")
        for path in excinfo.value.paths
    )
    assert all(not path.endswith(".") for path in excinfo.value.paths)
    assert "anniversaries.0" in str(excinfo.value)


# --------------------------------- 集成复审（2026-11-01）修复轮补强


def _section_settings(base, section, *, with_config=True):
    """以任意 date_countdown 节形状构造 Settings（集成复审补强用）。"""
    app = {
        **base.app,
        "read_only_tools": {"enabled": True, "policies": {"date_countdown": section}},
    }
    raw = {"allowed_private_user_ids": ["10001"], "read_only_tools": app["read_only_tools"]}
    return replace(
        base, app=app, config=validate_app_config(raw) if with_config else None
    )


@pytest.mark.parametrize("section", [None, "oops", ["x"]])
def test_t4_ingress_fallback_rejects_non_mapping_section_fail_fast(setup_app, section):
    _, _, _, base = setup_app
    # 集成复审 P3：回退路径节为 None/标量/列表时不得让 .get 链抛
    # AttributeError——须与配置树同纪律以 ConfigValidationError
    # fail-fast，键路径恰为策略节点本身，消息不回显节值
    settings = _section_settings(base, section, with_config=False)
    with pytest.raises(ConfigValidationError) as excinfo:
        _processor(settings)
    assert excinfo.value.paths == ("read_only_tools.policies.date_countdown",)
    if isinstance(section, str):
        assert section not in str(excinfo.value)


@pytest.mark.parametrize(
    "section",
    [
        {"enabled": True, "rate_limit": 0, "window_seconds": 60, "anniversaries": []},
        {"enabled": True, "anniversaries": [], "bogus_key": 1},
    ],
)
def test_t4_ingress_fallback_rejects_invalid_sibling_fields_fail_fast(setup_app, section):
    _, _, _, base = setup_app
    # 集成复审 P4：回退路径整节校验——兄弟字段越界（rate_limit=0）
    # 与未知键须与配置树同口径 fail-fast，不得静默忽略
    settings = _section_settings(base, section, with_config=False)
    with pytest.raises(ConfigValidationError) as excinfo:
        _processor(settings)
    assert excinfo.value.paths
    assert all(
        path.startswith("read_only_tools.policies.date_countdown")
        for path in excinfo.value.paths
    )


def test_t4_ingress_config_tree_takes_precedence_over_raw_app(setup_app, monkeypatch):
    _, _, _, base = setup_app
    # 集成复审 P3：配置树与原始 app dict 分岔时以配置树为准——
    # _wired_settings 的双源同值夹具测不出优先级，此处显式分岔
    settings = _wired_settings(base, [{"name": "配置树纪念", "date": "2026-11-11"}])
    diverged_app = {
        **settings.app,
        "read_only_tools": {
            "enabled": True,
            "policies": {
                "date_countdown": {
                    "enabled": True,
                    "rate_limit": 6,
                    "window_seconds": 60,
                    "anniversaries": [{"name": "原始字典纪念", "date": "2026-12-25"}],
                }
            },
        },
    }
    processor = _frozen_processor(replace(settings, app=diverged_app), monkeypatch)
    reg = processor.read_only_registry
    assert render(reg, {"name": "配置树纪念"}, "配置树纪念还有几天") == (
        "配置树纪念是2026-11-11，距今还有10天。"
    )
    assert render(reg, {"name": "原始字典纪念"}, "原始字典纪念还有几天") == (
        "没有找到名为「原始字典纪念」的纪念日，"
        "请确认名称或直接提供日期（YYYY-MM-DD）。"
    )


def test_t4_ingress_config_tree_ignores_invalid_raw_app_entries(setup_app, monkeypatch):
    _, _, _, base = setup_app
    # 集成复审 P3 分岔变体：原始 app dict 含非法条目（2 月 30 日）
    # 时装配不得 fail-fast——回退校验只在无配置树时执行
    settings = _wired_settings(base, [{"name": "配置树纪念", "date": "2026-11-11"}])
    diverged_app = {
        **settings.app,
        "read_only_tools": {
            "enabled": True,
            "policies": {
                "date_countdown": {
                    "enabled": True,
                    "rate_limit": 6,
                    "window_seconds": 60,
                    "anniversaries": [{"name": "坏条目", "month": 2, "day": 30}],
                }
            },
        },
    }
    processor = _frozen_processor(replace(settings, app=diverged_app), monkeypatch)
    reg = processor.read_only_registry
    assert render(reg, {"name": "配置树纪念"}, "配置树纪念还有几天") == (
        "配置树纪念是2026-11-11，距今还有10天。"
    )


def test_t4_ingress_fallback_rejects_duplicate_names_fail_fast(setup_app):
    _, _, _, base = setup_app
    # 集成复审 P3：回退路径规范化重名（NFKC + casefold 后同名）
    # 须整表校验 fail-fast——此前只有配置树路径（AC17）钉住此规则
    entries = [
        {"name": "ＡＢＣ", "month": 1, "day": 1},
        {"name": "abc", "month": 2, "day": 2},
    ]
    settings = _wired_settings(base, entries, with_config=False)
    with pytest.raises(ConfigValidationError) as excinfo:
        _processor(settings)
    assert excinfo.value.paths == ("read_only_tools.policies.date_countdown",)
    assert "ＡＢＣ" not in str(excinfo.value)
    assert "abc" not in str(excinfo.value)


@pytest.mark.parametrize(
    "global_enabled,policy_enabled", [(True, False), (False, True), (False, False)]
)
def test_t4_ingress_double_switch_disabled_hides_tool(
    setup_app, monkeypatch, global_enabled, policy_enabled
):
    _, _, _, base = setup_app
    # 集成复审 P4：双开关关闭态须经生产装配（EventProcessor）断言，
    # 此前只有裸 registry 的 AC15 矩阵——生产默认（关闭）态无装配级钉
    section = {
        "enabled": policy_enabled,
        "rate_limit": 6,
        "window_seconds": 60,
        "anniversaries": [{"name": "动态纪念", "date": "2026-11-11"}],
    }
    app = {
        **base.app,
        "read_only_tools": {
            "enabled": global_enabled,
            "policies": {"date_countdown": section},
        },
    }
    raw = {"allowed_private_user_ids": ["10001"], "read_only_tools": app["read_only_tools"]}
    settings = replace(base, app=app, config=validate_app_config(raw))
    processor = _frozen_processor(settings, monkeypatch)
    names = [s["function"]["name"] for s in processor.read_only_registry.schemas()]
    assert "date_countdown" not in names


def test_t4_ingress_fallback_rejects_over_cap_entries_fail_fast(setup_app):
    _, _, _, base = setup_app
    entries = [{"name": f"纪念{i}", "month": 1, "day": 1} for i in range(51)]
    settings = _wired_settings(base, entries, with_config=False)
    # 50 条上限与配置树同口径：回退路径 51 条同样在装配期 fail-fast
    with pytest.raises(ConfigValidationError) as excinfo:
        _processor(settings)
    assert "纪念50" not in str(excinfo.value)
    # 复审第 3 轮（P3-2）正向断言：实测超上限为模型级错误、loc 为
    # ()，故完整路径的叶子就是策略节点本身（不带 anniversaries
    # 后缀）——断言非空、路径恰为完整前缀、无尾随点（P4-2 口径：
    # 空 loc 不拼尾随点）
    assert excinfo.value.paths
    assert excinfo.value.paths == ("read_only_tools.policies.date_countdown",)
    assert all(not path.endswith(".") for path in excinfo.value.paths)


def test_t4_ingress_fallback_validation_error_never_echoes_entry_values(setup_app):
    _, _, _, base = setup_app
    # 复审 N4 无回显断言：以可辨识的纪念名触发回退校验失败，异常
    # 信息（str 与结构化 paths）均不得出现条目值全文
    settings = _wired_settings(
        base,
        [{"name": "秘密纪念名XYZ", "date": "2027-01-01", "year": 2020}],
        with_config=False,
    )
    with pytest.raises(ConfigValidationError) as excinfo:
        _processor(settings)
    assert "秘密纪念名XYZ" not in str(excinfo.value)
    assert all("秘密纪念名XYZ" not in path for path in excinfo.value.paths)
    # 复审第 3 轮（P3-2）正向断言：同 invalid-entries——非空、完整
    # 前缀、定位到出错条目叶子、无尾随点；实测 loc 形状为
    # ('anniversaries', 0)（date 与 year 冲突的条目级校验）
    assert excinfo.value.paths
    assert all(
        path.startswith("read_only_tools.policies.date_countdown.anniversaries")
        for path in excinfo.value.paths
    )
    assert all(not path.endswith(".") for path in excinfo.value.paths)
    assert "anniversaries.0" in str(excinfo.value)
