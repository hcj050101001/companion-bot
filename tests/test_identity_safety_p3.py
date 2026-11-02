"""P3 安全契约单元测试：KD-001 身份披露四项齐备 + KD-S01/D1 judge 保守降级。

覆盖 companion/multipart.py 的 review_bubbles 身份契约升级（四项齐备、
evasive 拒绝、身份问法扩围）与 companion/safety/__init__.py 的
SafetyGateway.keyword_match 抽出及 detect 对任意 judge 异常的保守口径。
仅使用合成文本与假 judge，不触达真实 QQ / 模型。
"""

import asyncio
from pathlib import Path

import pytest
import yaml

from companion.multipart import (
    identity_disclosure,
    is_identity_question,
    review_bubbles,
)
from companion.safety import JudgeUnavailable, SafetyGateway, UnknownLLMJudge

ROOT = Path(__file__).resolve().parents[1]
SAFETY = yaml.safe_load((ROOT / "config/safety.yaml").read_text(encoding="utf-8"))

FULL_DISCLOSURE = identity_disclosure("阿年", "DeepSeek")
IDENTITY_QUESTIONS = ["你是谁", "你是谁？", "你背后用的是什么模型"]


# ---------------------------------------------------------------------------
# KD-001：四项齐备才放行
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user_text", IDENTITY_QUESTIONS)
def test_complete_disclosure_passes(user_text):
    assert review_bubbles([FULL_DISCLOSURE], user_text=user_text) == (True, "")


def test_role_alone_satisfies_name_item():
    # 无助手名、仅角色「AI 陪伴助手」（含无空格写法）亦满足名字/角色项。
    spaced = "我是一个 AI 陪伴助手，当前回复由第三方模型 DeepSeek 生成。"
    tight = "我是AI陪伴助手，当前回复由第三方模型 DeepSeek 生成。"
    assert review_bubbles([spaced], user_text="你是谁") == (True, "")
    assert review_bubbles([tight], user_text="你是谁") == (True, "")


def test_disclosure_split_across_bubbles_is_checked_joined():
    bubbles = ["我是阿年，一个 AI 陪伴助手。", "当前回复由第三方模型 DeepSeek 生成。"]
    assert review_bubbles(bubbles, user_text="你是谁") == (True, "")


@pytest.mark.parametrize(
    "reply",
    [
        # 缺 AI 身份标记（同时必然缺「第三方模型」：标记词「模型」是其子串，
        # 两项在构造上不可完全分离，此例以缺标记为主断言）。
        "我是阿年，当前回复由 DeepSeek 生成。",
        # 缺名字/角色：只有泛化「AI 助手」，无「阿年」亦无「AI 陪伴助手」。
        "我是 AI 助手，当前回复由第三方模型 DeepSeek 生成。",
        # 缺模型显示名。
        "我是阿年，一个 AI 陪伴助手，当前回复由第三方模型生成。",
        # 缺「第三方模型」字样。
        "我是阿年，一个 AI 陪伴助手，当前回复由 DeepSeek 生成。",
    ],
)
def test_missing_any_disclosure_item_is_rejected(reply):
    assert review_bubbles([reply], user_text="你是谁") == (False, "identity")


def test_weak_ai_assistant_reply_is_rejected():
    assert review_bubbles(["我是 AI 助手"], user_text="你是谁") == (False, "identity")
    assert review_bubbles(["我是 AI 助手。"], user_text="你是谁？") == (False, "identity")


def test_custom_identity_name_and_model_are_used():
    line = identity_disclosure("小伴", "Qwen")
    assert review_bubbles(
        [line], user_text="你是谁", identity_name="小伴", identity_model="Qwen"
    ) == (True, "")
    # 默认参数下该文案缺默认模型名，应被拒。
    assert review_bubbles([line], user_text="你是谁") == (False, "identity")
    # 模型名大小写不敏感。
    assert review_bubbles(
        [FULL_DISCLOSURE], user_text="你是谁", identity_model="deepseek"
    ) == (True, "")


# ---------------------------------------------------------------------------
# KD-001：evasive 拒绝措辞不放行
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply,user_text",
    [
        ("这是秘密", "你背后用的是什么模型"),
        ("这是个秘密。", "你用的什么模型"),
        ("保密。", "你是什么模型"),
        ("无可奉告。", "你是谁"),
        ("不能告诉你。", "你是谁"),
        # 凑巧含齐四项标记也不能原样发出：evasive 优先判拒。
        ("我是阿年，一个 AI 陪伴助手，当前回复由第三方模型 DeepSeek 生成，但细节保密。", "你是谁"),
    ],
)
def test_evasive_identity_reply_is_rejected(reply, user_text):
    assert review_bubbles([reply], user_text=user_text) == (False, "identity")


def test_evasive_phrase_split_across_bubbles_is_rejected():
    assert review_bubbles(["这是", "秘密。"], user_text="你是谁") == (False, "identity")


# ---------------------------------------------------------------------------
# KD-001：身份问法扩围与误触发防护
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "你背后用的是什么模型",
        "你背后用的什么模型？",
        "你用的什么模型",
        "你用的是什么模型",
        "您用的什么模型",
        "你是什么模型",
        "你基于哪个模型",
        "你基于什么模型",
        "what model are you",
        "which model are you",
        "What model are you based on?",
        "Which model do you use?",
    ],
)
def test_new_identity_question_forms_are_recognised(text):
    assert is_identity_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "这个模型怎么做",
        "这个模型怎么训练的",
        "你觉得这个模型怎么样",
        "你知道这个模型怎么做吗",
        "什么模型好用",
        "今天天气怎么样",
    ],
)
def test_non_identity_questions_do_not_trigger(text):
    assert not is_identity_question(text)


def test_non_identity_question_keeps_ordinary_reply_path():
    # 非身份提问不触发披露契约：普通回复行为不变，弱身份措辞亦不被拦。
    assert review_bubbles(["我是 AI 助手"], user_text="今天天气怎么样") == (True, "")
    assert review_bubbles(
        ["这个模型需要分步骤训练。"], user_text="这个模型怎么做"
    ) == (True, "")
    assert review_bubbles(["你好呀。"]) == (True, "")


def test_new_question_form_with_weak_reply_falls_back():
    # 新问法命中后，「这是秘密」与「我是 AI 助手」都必须走 identity 回退。
    assert review_bubbles(["这是秘密"], user_text="你背后用的是什么模型") == (
        False,
        "identity",
    )
    assert review_bubbles(["我是 AI 助手"], user_text="你用的什么模型") == (
        False,
        "identity",
    )
    # 而固定披露文案本身四项齐备，在新问法下可直接放行。
    assert review_bubbles([FULL_DISCLOSURE], user_text="你背后用的是什么模型") == (
        True,
        "",
    )


# ---------------------------------------------------------------------------
# KD-S01 / D1：judge 任意异常按疑似危机；keyword_match 与 detect 一致
# ---------------------------------------------------------------------------


class ExplodingJudge:
    """抛普通异常（非 JudgeUnavailable）的 judge。"""

    async def judge(self, _text):
        raise RuntimeError("synthetic judge failure")


class TimeoutJudge:
    async def judge(self, _text):
        raise TimeoutError("synthetic judge timeout")


class UnavailableJudge:
    async def judge(self, _text):
        raise JudgeUnavailable("synthetic unavailable")


class FixedJudge:
    def __init__(self, result):
        self.result = result

    async def judge(self, _text):
        return self.result


def _gateway(judge=None):
    return SafetyGateway(
        SAFETY["crisis_keywords"], judge or UnknownLLMJudge(), SAFETY["categories"]
    )


@pytest.mark.parametrize("judge", [ExplodingJudge(), TimeoutJudge(), UnavailableJudge()])
def test_any_judge_exception_is_suspected_crisis(judge):
    gateway = _gateway(judge)
    assert asyncio.run(gateway.detect("今天心情一般")) == (True, "疑似危机")


def test_judge_verdicts_still_pass_through():
    assert asyncio.run(_gateway(FixedJudge(True)).detect("今天心情一般")) == (
        True,
        "model",
    )
    assert asyncio.run(_gateway(FixedJudge(False)).detect("今天心情一般")) == (
        False,
        "",
    )
    assert asyncio.run(_gateway(FixedJudge(None)).detect("今天心情一般")) == (
        False,
        "",
    )


def test_keyword_hit_short_circuits_exploding_judge():
    # 关键词路径先行命中时不调用 judge，judge 异常不影响分类结果。
    gateway = _gateway(ExplodingJudge())
    assert asyncio.run(gateway.detect("我想自杀")) == (True, "明确自伤计划")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("我想自杀", "明确自伤计划"),  # 命中分类目
        ("活着没意思", "低落"),  # 命中低落分类（未被该语境掩码）
        ("我想消失", "危机"),  # 命中关键词但无分类
        ("笑死我了", None),  # 无害短语掩码
        ("这游戏想死", None),  # 无害短语掩码
        ("准备好了明天出门", None),  # 无害短语掩码
        ("今天很开心", None),  # 无命中
    ],
)
def test_keyword_match_categories_masks_and_misses(text, expected):
    assert _gateway().keyword_match(text) == expected


def test_keyword_match_mask_does_not_hide_other_crisis():
    gateway = _gateway()
    assert gateway.keyword_match("这游戏想死，但我不想活了") == "危机"


@pytest.mark.parametrize(
    "text",
    [
        "我想自杀",
        "活着没意思",
        "我想消失",
        "笑死我了",
        "这游戏想死",
        "准备好了明天出门",
        "今天很开心",
        "这游戏想死，但我不想活了",
        "我不想死",
    ],
)
def test_keyword_match_consistent_with_detect_keyword_path(text):
    gateway = _gateway()
    matched = gateway.keyword_match(text)
    detected = asyncio.run(gateway.detect(text))
    if matched is None:
        assert detected == (False, "")
    else:
        assert detected == (True, matched)
