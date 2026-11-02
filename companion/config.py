"""集中加载应用配置；密钥只来自进程环境变量。

P1 起，应用 YAML 在进入旧有 ``Settings.app`` 字典前先经过 Pydantic
模型校验：未知键、错类型和不合法的组合都在启动期失败，并在错误中
指出键路径（不回显配置值）。``Settings.app`` 暂时保留为兼容视图，
供尚未迁移的模块按原方式读取；正式、已补齐默认值的配置树保存在
``Settings.config``。
"""

import json
import logging
import os
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

import yaml
from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

ROOT = Path(__file__).resolve().parent.parent

SECRET_ENV_NAMES = frozenset(
    {
        "DEEPSEEK_API_KEY",
        "GMAIL_APP_PASSWORD",
        "ONEBOT_SECRET",
        "OPENAI_COMPATIBLE_API_KEY",
        "TAVILY_API_KEY",
    }
)


class ConfigValidationError(RuntimeError):
    """配置不合法；消息只包含键路径与原因，绝不包含配置值。

    ``paths`` 保留结构化的出错键路径（或 YAML 位置），供热更新
    审计在不记录任何配置值的前提下写 rejected 行。
    """

    def __init__(self, message: str, *, paths: tuple[str, ...] = ()):
        super().__init__(message)
        self.paths = tuple(paths)


class SecretProvider(Protocol):
    """密钥读取协议；实现只能按名取值，不得批量导出密钥值。

    服务范围包括 ``load_settings`` 的启动检查（ONEBOT_SECRET 必备、
    GMAIL_APP_PASSWORD 是否已配置）与运行期消费者（DeepSeek /
    OpenAI-compatible / Tavily / Gmail 发送），均经本协议按名取值。
    实现只能按名取值、不得批量导出，``repr`` 不得暴露密钥值。
    """

    def get(self, name: str) -> str | None:
        """返回指定密钥；未知密钥名应直接拒绝。"""
        ...


class EnvironmentSecretProvider:
    """从进程环境读取密钥。repr 只暴露可读的密钥名，不暴露值。"""

    def __init__(self, environ: Mapping[str, str] | None = None):
        self._environ = os.environ if environ is None else environ

    def get(self, name: str) -> str | None:
        if name not in SECRET_ENV_NAMES:
            raise ValueError(f"unsupported secret name: {name}")
        return self._environ.get(name)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(names={sorted(SECRET_ENV_NAMES)!r})"


class _StrictModel(BaseModel):
    # 配置树不可变：变更只能整树重校验后替换，防止 Settings.config
    # 与已校验状态悄悄漂移（Settings.app 兼容视图仍可按旧方式改）。
    model_config = ConfigDict(extra="forbid", validate_default=True, frozen=True)


def _normalize_user_ids(value: object) -> list[str]:
    """把 QQ 号统一为字符串；布尔值不是合法号码。"""
    if not isinstance(value, (list, tuple)):
        raise ValueError("must be a list of user IDs")
    normalized: list[str] = []
    for item in value:
        if type(item) is int:
            normalized.append(str(item))
        elif isinstance(item, str):
            normalized.append(item.strip())
        else:
            raise ValueError("user IDs must be strings or integers")
    return normalized


class ResponseDelayConfig(_StrictModel):
    min: float = 1.0
    max: float = 4.0

    @field_validator("min", "max", mode="before")
    @classmethod
    def _coerce_delay(cls, value: object) -> object:
        # YAML 的整数秒（如 1）应被接受，但字符串/布尔值必须报错。
        if type(value) in (int, float):
            return float(value)
        raise ValueError("response delay must be a number")

    @field_validator("min", "max")
    @classmethod
    def _validate_delay(cls, value: float) -> float:
        if value < 0 or value != value or value in (float("inf"), float("-inf")):
            raise ValueError("response delay must be a finite, non-negative number")
        return value

    @model_validator(mode="after")
    def _validate_order(self):
        if self.min > self.max:
            raise ValueError("response_delay_seconds.min must not exceed max")
        return self


class ToolLoopConfig(_StrictModel):
    """工具循环上限（方案 §5.4）：工具轮次与实际调用数，状态机据此封顶。"""

    max_tool_rounds: StrictInt = Field(default=2, ge=0, le=8)
    max_tool_calls: StrictInt = Field(default=3, ge=0, le=32)


class LLMConfig(_StrictModel):
    provider: Literal["deepseek", "openai_compatible", "codex"] = "deepseek"
    display_name: StrictStr | None = Field(default=None, min_length=1)
    tool_loop: ToolLoopConfig = Field(default_factory=ToolLoopConfig)


class ActionToolConfig(_StrictModel):
    enabled: StrictBool
    dry_run: StrictBool
    rate_limit: StrictInt = Field(gt=0)
    window_seconds: StrictInt = Field(gt=0)


class SendPrivateToolConfig(ActionToolConfig):
    enabled: StrictBool = True
    dry_run: StrictBool = False
    rate_limit: StrictInt = Field(default=18, gt=0)
    window_seconds: StrictInt = Field(default=60, gt=0)


class AlertPrivateToolConfig(ActionToolConfig):
    # 与旧消费端的有效默认值对齐：ingress 构造策略时 enabled 缺省为
    # True，再与 ALERT_QQ 是否设置做与门（未配 ALERT_QQ 时实际仍为
    # 关闭）；ToolGateway 对 dry_run 的缺省是 False。此前模型写成
    # False/True 与旧语义正好相反，省略该节时正式树会把告警描述成
    # “关闭+模拟”，而运行期实际是“开启+真发”。
    enabled: StrictBool = True
    dry_run: StrictBool = False
    rate_limit: StrictInt = Field(default=2, gt=0)
    window_seconds: StrictInt = Field(default=60, gt=0)


class GroupChatToolConfig(ActionToolConfig):
    enabled: StrictBool = False
    dry_run: StrictBool = True
    rate_limit: StrictInt = Field(default=6, gt=0)
    window_seconds: StrictInt = Field(default=60, gt=0)
    # 群回复产品开关（N4）：与能力开关分离的第二层（方案 §5 产品/
    # 能力开关分离）——enabled/dry_run 管 send_group_msg 能力面，
    # reply_enabled 管群聊子链是否装配回复通路；两者都开才可能
    # 回复。默认关，变更须重启（不进热更新白名单，同 actions 口径）。
    reply_enabled: StrictBool = False
    # 群回复框架级准入门（补审第二段 P3）：reply_blocked_persons
    # 命中即不触发回复，默认 ("system",) ——机器人自身消息回流永不
    # 自触发；reply_allowed_persons / reply_allowed_roles 为 None
    # 时不按名单收窄（「群员 @ 即触发」为已拍板的产品口径，本门
    # 只补框架下限），给出名单时两维合取准入。变更须重启（同
    # reply_enabled 口径，不进热更新白名单）。
    #
    # 名单值域（复审 P4 明示）：*_persons 填的是 person 类别、
    # 不是 QQ 号——发送者相对机器人的身份类别，只取 "system"
    # （机器人自身）/ "owner"（配置名单内用户）/ "stranger"
    # （其余群员）；*_roles 填发送者的群内角色，只取 "owner" /
    # "admin" / "member" / "unknown"（平台未给角色时）。把 QQ
    # 号填进 *_persons 永不命中：填进 allowed 名单会让群回复
    # 静默永不触发，填进 blocked 名单则除恒含的 "system" 外
    # 毫无效果。
    reply_blocked_persons: tuple[StrictStr, ...] = ("system",)
    reply_allowed_persons: tuple[StrictStr, ...] | None = None
    reply_allowed_roles: tuple[StrictStr, ...] | None = None

    @field_validator("reply_blocked_persons")
    @classmethod
    def _reply_blocked_persons_keep_system(cls, value):
        # 框架下限（复审 P4）：名单可扩展，但 "system" 不可移除、
        # 名单不可清空——配错即启动失败，绝不静默放行自触发
        # （EventProcessor 侧另有并集下限兜底，防 dict 直读绕过）。
        if "system" not in value:
            raise ValueError("reply_blocked_persons must include 'system'")
        return value


class QzoneToolConfig(ActionToolConfig):
    enabled: StrictBool = False
    dry_run: StrictBool = True
    rate_limit: StrictInt = Field(default=2, gt=0)
    window_seconds: StrictInt = Field(default=60, gt=0)


class FriendManagementToolConfig(ActionToolConfig):
    enabled: StrictBool = False
    dry_run: StrictBool = True
    rate_limit: StrictInt = Field(default=2, gt=0)
    window_seconds: StrictInt = Field(default=60, gt=0)


class ToolsConfig(_StrictModel):
    send_private_msg: SendPrivateToolConfig = Field(default_factory=SendPrivateToolConfig)
    alert_private_msg: AlertPrivateToolConfig = Field(default_factory=AlertPrivateToolConfig)
    group_chat: GroupChatToolConfig = Field(default_factory=GroupChatToolConfig)
    qzone: QzoneToolConfig = Field(default_factory=QzoneToolConfig)
    friend_management: FriendManagementToolConfig = Field(
        default_factory=FriendManagementToolConfig
    )


class ReadOnlyPolicyConfig(_StrictModel):
    enabled: StrictBool = False
    rate_limit: StrictInt = Field(default=6, gt=0)
    window_seconds: StrictInt = Field(default=60, gt=0)


class WebSearchPolicyConfig(ReadOnlyPolicyConfig):
    content_extraction_enabled: StrictBool = False


def _normalize_anniversary_name(value: str) -> str:
    """纪念日名称规范化：NFKC + casefold + 空白折叠。

    与 date_countdown 查询侧共用同一口径（config 为叶子模块，不能
    反向 import tools，故在此内置一份同口径实现，靠测试锁定一致）。
    """
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


#: 闰年语境下各月最大日（2 月允许 29 日，见设计 §5.1 / PRD §5.3）。
_LEAP_MONTH_MAX_DAY = {
    1: 31,
    2: 29,
    3: 31,
    4: 30,
    5: 31,
    6: 30,
    7: 31,
    8: 31,
    9: 30,
    10: 31,
    11: 30,
    12: 31,
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AnniversaryEntry(_StrictModel):
    """纪念日条目：形态 A（name+month+day，可加 year）与形态 B
    （name+date）互斥，未知键 fail-fast（_StrictModel extra=forbid）。"""

    name: StrictStr = Field(min_length=1, max_length=64)
    month: StrictInt | None = Field(default=None, ge=1, le=12)
    day: StrictInt | None = Field(default=None, ge=1, le=31)
    year: StrictInt | None = Field(default=None, ge=1900, le=2100)
    date: StrictStr | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("anniversary name must not be blank")
        return value

    @field_validator("date")
    @classmethod
    def _validate_date(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not _DATE_RE.match(value):
            raise ValueError("anniversary date must be in YYYY-MM-DD format")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("anniversary date must be a real calendar date") from None
        return value

    @model_validator(mode="after")
    def _validate_form(self):
        has_date = self.date is not None
        has_month = self.month is not None
        has_day = self.day is not None
        if has_date:
            # 形态 B：不得混给 month/day，也不得混给 year（OD-4 按
            # fail-fast 方向落地：year 对一次性条目无意义，静默忽略
            # 会造成配置漂移，故与 month/day 同口径在加载期拒绝）
            if has_month or has_day:
                raise ValueError(
                    "anniversary entry must not combine date with month/day"
                )
            if self.year is not None:
                raise ValueError(
                    "anniversary entry must not combine date with year"
                )
        else:
            # 形态 A：month 与 day 必须同给
            if not (has_month and has_day):
                raise ValueError(
                    "anniversary entry requires either date or both month and day"
                )
            # 闰年语境合法性（2/29 放行，2/30 拒绝）
            assert self.month is not None and self.day is not None
            if self.day > _LEAP_MONTH_MAX_DAY[self.month]:
                raise ValueError("anniversary day is out of range for month")
        return self


class DateCountdownPolicyConfig(ReadOnlyPolicyConfig):
    anniversaries: tuple[AnniversaryEntry, ...] = ()

    @model_validator(mode="after")
    def _validate_anniversaries(self):
        if len(self.anniversaries) > 50:
            raise ValueError("anniversaries must not exceed 50 entries")
        names = [_normalize_anniversary_name(e.name) for e in self.anniversaries]
        if len(set(names)) != len(names):
            raise ValueError("anniversary names must be unique after normalization")
        return self


class ReadOnlyPoliciesConfig(_StrictModel):
    current_time: ReadOnlyPolicyConfig = Field(default_factory=ReadOnlyPolicyConfig)
    weather_lookup: ReadOnlyPolicyConfig = Field(default_factory=ReadOnlyPolicyConfig)
    web_search: WebSearchPolicyConfig = Field(default_factory=WebSearchPolicyConfig)
    date_countdown: DateCountdownPolicyConfig = Field(
        default_factory=DateCountdownPolicyConfig
    )


class ReadOnlyToolsConfig(_StrictModel):
    enabled: StrictBool = False
    policies: ReadOnlyPoliciesConfig = Field(default_factory=ReadOnlyPoliciesConfig)


class FeatureConfig(_StrictModel):
    enabled: StrictBool = False


class ProactiveCareConfig(_StrictModel):
    """主动关怀（P6 接线）：opt-in/默认关口径不变（D2）。

    store_path 为仓外偏好存储路径（CarePreferences 在构造时再校验
    仓外与 0600 纪律）；未配置时关怀命令面不装配。两键均只在启动时
    读取，不进热更新白名单（白名单只有 proactive_care.enabled）。
    """

    enabled: StrictBool = False
    store_path: StrictStr | None = None
    check_interval_seconds: StrictInt = Field(default=900, gt=0)


class ActionOverrideConfig(_StrictModel):
    """单个动作的开关覆盖（方案 C11）：None 表示不覆盖、沿用注册表与
    既有 policies 的口径；只有非 None 的键才在接线时并入 policies。"""

    enabled: StrictBool | None = None
    dry_run: StrictBool | None = None


#: L3 账号安全敏感动作全集（全接口方案 §5）：config 侧的字面名单，
#: 唯一事实源是 adapters/action_registry 中 level == "L3" 且
#: disposition == "registered" 的条目集；本字面量与登记表的相等性
#: 由 tests/test_l3_unlock_n6.py 的防漂移断言钉死（config 不反向
#: import adapters，避免配置层依赖适配器包）。
L3_SENSITIVE_ACTION_NAMES = frozenset(
    {
        "get_clientkey",
        "get_cookies",
        "get_credentials",
        "get_csrf_token",
        "get_rkey",
        "get_rkey_server",
        "nc_get_rkey",
        "send_packet",
    }
)


class ActionsConfig(_StrictModel):
    """actions 配置段：按动作名覆盖 enabled/dry_run。

    overrides 是 dict 字段，防漂移契约中模型与 example 两侧都记为
    单一叶子路径 ``actions.overrides``。动作开关变更须重启生效，
    不进 HOT_RELOADABLE_CONFIG_PATHS（与 L3「运维配置+重启」口径一致）。

    l3_unlock（N6，全接口方案 §5/§6 N6）：L3 受控解锁的运维名单——
    逐动作显式列名（不支持通配），默认空 = 全部 L3 在 Gateway 硬禁。
    名单只在进程启动构造 Gateway 时快照生效，同样不进热更新白名单；
    解锁不等于放行：调用仍须 ops_cli 主体 + 授权上下文 + 全量审计
    （见 adapters/gateway.py 的 L3 解锁段）。
    """

    overrides: dict[StrictStr, ActionOverrideConfig] = Field(default_factory=dict)
    l3_unlock: tuple[StrictStr, ...] = ()

    @field_validator("l3_unlock")
    @classmethod
    def _validate_l3_unlock(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("l3_unlock entries must be unique")
        unknown = [name for name in value if name not in L3_SENSITIVE_ACTION_NAMES]
        if unknown:
            raise ValueError(
                "l3_unlock entries must be registered L3 sensitive actions"
            )
        return value


class VisionConfig(_StrictModel):
    """私聊图片理解（deepseek-flash 视觉）：默认开。

    enabled 关时行为与旧固定回复逐字一致（不下载、不调用视觉
    模型）；enabled 开但无 DEEPSEEK_API_KEY 时同样回落旧行为，
    由运行期 has_credentials 判定。全部键只在启动时读取、变更
    须重启，不进热更新白名单（与 actions 口径一致）。
    """

    enabled: StrictBool = True
    model: StrictStr = Field(default="deepseek-flash", min_length=1)
    max_images: StrictInt = Field(default=3, ge=1, le=10)
    max_image_bytes: StrictInt = Field(default=10485760, gt=0, le=52428800)
    timeout_seconds: StrictInt = Field(default=30, gt=0, le=120)
    napcat_timeout_seconds: StrictInt = Field(default=8, gt=0, le=60)
    image_local_roots: tuple[StrictStr, ...] = ()


class RequestsConfig(_StrictModel):
    """request 处置策略配置段（好友申请 / 入群申请与邀请）。

    friend 可取 ``approve_all``（全自动同意所有好友申请）；group
    在配置层就不开放 ``approve_all``——2026-10-31 用户决策的范围
    是全自动同意只限好友申请，群申请最多到 allowlist（名单内
    自动同意、名单外人工）。全部键只在启动时读取、变更须重启，
    不进热更新白名单（与 actions 口径一致）。
    """

    friend: Literal["manual", "allowlist", "approve_all"] = "manual"
    group: Literal["manual", "allowlist"] = "manual"
    approve_user_ids: tuple[StrictStr, ...] = ()
    approve_group_ids: tuple[StrictStr, ...] = ()


class ReconnectCatchupConfig(_StrictModel):
    """断线重连补回（默认关；启用与默认文案定稿均为实例外决策）。

    全部键只在启动时读取、变更须重启，不进热更新白名单（与
    media.vision / requests 口径一致）。
    """

    enabled: StrictBool = False
    prefix_enabled: StrictBool = True
    prefix_text: StrictStr = Field(
        default="（刚信号不好，才看到你之前的消息）", min_length=1
    )


class MediaConfig(_StrictModel):
    """媒体处理配置段：目前只有图片理解（vision）。"""

    vision: VisionConfig = Field(default_factory=VisionConfig)


class AppConfig(_StrictModel):
    """应用 YAML 的正式模型树；字段集与 config.example.yaml 一一对应。"""

    # 用 tuple 而非 list：frozen=True 只挡属性赋值，挡不住对 list 的
    # 原地 clear()/append()，那会让已校验的名单在运行期被悄悄改成
    # 非法状态且绕过重校验与热更新审计。消费端需要 list 时用
    # list(...) 拷贝（care_known_user_ids 属性即如此）。
    allowed_private_user_ids: tuple[StrictStr, ...] = Field(min_length=1)
    requests_per_minute: StrictInt = Field(default=6, gt=0)
    response_delay_seconds: ResponseDelayConfig = Field(default_factory=ResponseDelayConfig)
    sqlite_path: StrictStr = Field(default="data/memory.db", min_length=1)
    logs_dir: StrictStr = Field(default="logs", min_length=1)
    persona_file: StrictStr = Field(default="config/persona.md", min_length=1)
    persona_profile_file: StrictStr = Field(default="config/persona.yaml", min_length=1)
    safety_file: StrictStr = Field(default="config/safety.yaml", min_length=1)
    napcat_base_url: StrictStr = Field(default="http://127.0.0.1:3000", min_length=1)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    read_only_tools: ReadOnlyToolsConfig = Field(default_factory=ReadOnlyToolsConfig)
    connection_health: FeatureConfig = Field(default_factory=FeatureConfig)
    proactive_care: ProactiveCareConfig = Field(default_factory=ProactiveCareConfig)
    actions: ActionsConfig = Field(default_factory=ActionsConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    requests: RequestsConfig = Field(default_factory=RequestsConfig)
    reconnect_catchup: ReconnectCatchupConfig = Field(
        default_factory=ReconnectCatchupConfig
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_care_known_user_ids(cls, value: object) -> object:
        """兼容读 care_known_user_ids；与旧键并存时两者必须一致。"""
        if not isinstance(value, dict) or "care_known_user_ids" not in value:
            return value
        copied = dict(value)
        care = copied.pop("care_known_user_ids")
        if "allowed_private_user_ids" not in copied:
            copied["allowed_private_user_ids"] = care
            return copied
        try:
            same = _normalize_user_ids(care) == _normalize_user_ids(
                copied["allowed_private_user_ids"]
            )
        except ValueError:
            same = False
        if not same:
            raise ValueError(
                "care_known_user_ids must match allowed_private_user_ids when both are set"
            )
        return copied

    @field_validator("allowed_private_user_ids", mode="before")
    @classmethod
    def _normalize_allowed_ids(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return _normalize_user_ids(value)
        return value

    @field_validator("allowed_private_user_ids")
    @classmethod
    def _validate_allowed_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            raise ValueError("user IDs must not be empty")
        return value

    @property
    def care_known_user_ids(self) -> list[str]:
        """新语义名称；与旧键指向同一份已校验名单。"""
        return list(self.allowed_private_user_ids)


def _format_validation_error(exc: ValidationError) -> str:
    details = []
    for error in exc.errors(include_input=False):
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        details.append(f"{location}: {error['msg']}")
    return "; ".join(details)


def validate_app_config(raw: object) -> AppConfig:
    """校验原始 YAML 映射；失败时只报键路径，不回显任何配置值。"""
    if not isinstance(raw, dict):
        raise ConfigValidationError(
            "Invalid configuration: (root): must be a mapping", paths=("(root)",)
        )
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        paths = tuple(
            ".".join(str(part) for part in error["loc"]) or "(root)"
            for error in exc.errors(include_input=False)
        )
        raise ConfigValidationError(
            "Invalid configuration: " + _format_validation_error(exc), paths=paths
        ) from None


def _yaml_location(mark: object) -> str:
    if mark is None:
        return "(yaml)"
    return f"(yaml line {mark.line + 1}, column {mark.column + 1})"


def _schema_child_keys() -> dict[str, frozenset[str]]:
    """prefix 路径 -> 该层模型映射允许出现的键集合。

    供 YAML 预检 walker 拼错误路径用：只有 schema 已知的结构层才把
    键名写进路径。未知字段本身可以点名（与 Pydantic 对 extra 键的
    报错口径一致），但未知字段之下、以及本应是标量/列表的位置上
    出现的映射，其键是数据内容而不是配置键名，一律不再拼进路径，
    只报最近的安全路径或行列位置——否则把密钥误粘成键名时会随错误
    消息与热更新 rejected 审计一起泄漏。
    """
    levels: dict[str, frozenset[str]] = {}

    def visit(model_type: type[BaseModel], prefix: str) -> None:
        levels[prefix] = frozenset(model_type.model_fields)
        for name, model_field in model_type.model_fields.items():
            annotation = model_field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                visit(annotation, f"{prefix}.{name}" if prefix else name)

    visit(AppConfig, "")
    # 输入别名 care_known_user_ids 在根层合法；其值是标量列表，
    # 名下不再有结构层。
    levels[""] = levels[""] | {"care_known_user_ids"}
    return levels


_SCHEMA_CHILD_KEYS = _schema_child_keys()


def _child_path_and_known(
    prefix: str, known: frozenset[str] | None, key: str
) -> tuple[str, frozenset[str] | None]:
    """由父级安全路径与本级已知键集，算子键的安全路径与已知键集。

    ``known`` 为 None 表示已进入数据区（未知字段之下或标量/列表
    位置）：路径不再向下拼键名，子层继续保持数据区。
    """
    if known is None:
        return prefix, None
    path = f"{prefix}.{key}" if prefix else key
    if key in known:
        return path, _SCHEMA_CHILD_KEYS.get(path)
    return path, None


def _descend_guard(
    node: object, prefix: str, ancestors: set[int]
) -> None:
    """进入集合节点前的两道检查：标签白名单与别名环检测。

    ``!!set``/``!!pairs``/``!!omap`` 里映射的“键”是数据值而非配置
    键名，把它们拼进错误路径等于把值写进日志/审计，因此这类集合
    标签在下降前整体拒绝，只报它所在的配置路径。递归别名
    （``&a [*a]``）会让节点图自引用，靠祖先集合检出；同一节点被
    多处正常引用（DAG）不算环，离开子树即移除。
    """
    if isinstance(node, yaml.MappingNode):
        allowed_tag = "tag:yaml.org,2002:map"
    elif isinstance(node, yaml.SequenceNode):
        allowed_tag = "tag:yaml.org,2002:seq"
    else:
        return
    location = prefix or _yaml_location(getattr(node, "start_mark", None))
    if node.tag != allowed_tag:
        short_tag = node.tag.rsplit(":", 1)[-1]
        raise ConfigValidationError(
            f"Invalid configuration: {location}: unsupported YAML collection "
            f"tag !!{short_tag}",
            paths=(location,),
        )
    if id(node) in ancestors:
        raise ConfigValidationError(
            f"Invalid configuration: {location}: recursive YAML alias",
            paths=(location,),
        )
    ancestors.add(id(node))


def _check_duplicate_keys(
    node: object,
    prefix: str = "",
    _ancestors: set[int] | None = None,
    _known: frozenset[str] | None = None,
    _at_root: bool = True,
) -> None:
    """拒绝重复映射键；PyYAML 默认会静默保留最后一个，绕过 fail-fast。

    路径只沿 schema 已知结构拼键名（见 ``_schema_child_keys``）；
    数据区里的重复键只报最近的安全路径，不点名重复的键本身。
    """
    ancestors = _ancestors if _ancestors is not None else set()
    known = _SCHEMA_CHILD_KEYS[""] if _at_root else _known
    if isinstance(node, yaml.MappingNode):
        _descend_guard(node, prefix, ancestors)
        seen: set[str] = set()
        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                location = _yaml_location(getattr(key_node, "start_mark", None))
                raise ConfigValidationError(
                    f"Invalid configuration: {location}: mapping keys must be scalars",
                    paths=(location,),
                )
            path, child_known = _child_path_and_known(prefix, known, key_node.value)
            if key_node.value in seen:
                location = (
                    path
                    if known is not None
                    else (prefix or _yaml_location(key_node.start_mark))
                )
                raise ConfigValidationError(
                    f"Invalid configuration: {location}: duplicate mapping key",
                    paths=(location,),
                )
            seen.add(key_node.value)
            _check_duplicate_keys(value_node, path, ancestors, child_known, False)
        ancestors.discard(id(node))
    elif isinstance(node, yaml.SequenceNode):
        _descend_guard(node, prefix, ancestors)
        for index, child in enumerate(node.value):
            child_prefix = f"{prefix}[{index}]"  # 下标只是位置、不是数据，无条件拼
            _check_duplicate_keys(child, child_prefix, ancestors, None, False)
        ancestors.discard(id(node))


# 这些标准标签的构造器失败时 PyYAML 抛的是裸 ValueError（不带位置），
# 因此在正式解析前先用同一个构造器逐个试探已组合的标量节点，
# 失败即可直接报出节点路径，全程不把标量原文带进错误消息。
_TAGGED_SCALAR_PROBES = {
    "tag:yaml.org,2002:int": "construct_yaml_int",
    "tag:yaml.org,2002:float": "construct_yaml_float",
    "tag:yaml.org,2002:bool": "construct_yaml_bool",
    "tag:yaml.org,2002:timestamp": "construct_yaml_timestamp",
    "tag:yaml.org,2002:binary": "construct_yaml_binary",
}


def _validate_tagged_scalars(
    node: object,
    prefix: str = "",
    _ancestors: set[int] | None = None,
    _known: frozenset[str] | None = None,
    _at_root: bool = True,
) -> None:
    ancestors = _ancestors if _ancestors is not None else set()
    known = _SCHEMA_CHILD_KEYS[""] if _at_root else _known
    if isinstance(node, yaml.MappingNode):
        _descend_guard(node, prefix, ancestors)
        for key_node, value_node in node.value:
            if isinstance(key_node, yaml.ScalarNode):
                path, child_known = _child_path_and_known(
                    prefix, known, key_node.value
                )
            else:
                path, child_known = prefix, None
            _validate_tagged_scalars(value_node, path, ancestors, child_known, False)
        ancestors.discard(id(node))
    elif isinstance(node, yaml.SequenceNode):
        _descend_guard(node, prefix, ancestors)
        for index, child in enumerate(node.value):
            child_prefix = f"{prefix}[{index}]"  # 下标只是位置、不是数据，无条件拼
            _validate_tagged_scalars(child, child_prefix, ancestors, None, False)
        ancestors.discard(id(node))
    elif isinstance(node, yaml.ScalarNode) and node.tag in _TAGGED_SCALAR_PROBES:
        probe = yaml.SafeLoader("")
        constructor = getattr(probe, _TAGGED_SCALAR_PROBES[node.tag])
        try:
            constructor(node)
        except Exception:
            location = prefix or _yaml_location(node.start_mark)
            raise ConfigValidationError(
                f"Invalid configuration: {location}: invalid YAML value",
                paths=(location,),
            ) from None


def _node_path_for_mark(node: object, mark: object) -> str | None:
    if mark is None:
        return None

    def visit(
        current: object,
        prefix: str,
        known: frozenset[str] | None,
        depth: int = 0,
    ) -> str | None:
        if depth > 200:  # 防御：异常深嵌套时放弃定位，只报行列位置
            return None
        start = getattr(current, "start_mark", None)
        if (
            start is not None
            and start.line == mark.line
            and start.column == mark.column
            and prefix
        ):
            return prefix
        if isinstance(current, yaml.MappingNode):
            for key_node, value_node in current.value:
                if isinstance(key_node, yaml.ScalarNode):
                    path, child_known = _child_path_and_known(
                        prefix, known, key_node.value
                    )
                else:
                    path, child_known = prefix, None
                found = visit(value_node, path, child_known, depth + 1)
                if found is not None:
                    return found
        elif isinstance(current, yaml.SequenceNode):
            for index, child in enumerate(current.value):
                child_prefix = f"{prefix}[{index}]"  # 下标只是位置、不是数据，无条件拼
                found = visit(child, child_prefix, None, depth + 1)
                if found is not None:
                    return found
        return None

    return visit(node, "", _SCHEMA_CHILD_KEYS[""])


def load_yaml_data(text: str) -> object:
    """解析 YAML 并把所有失败收敛为不回显值的 ConfigValidationError。

    PyYAML 的构造错误（如 ``!!int`` 后跟非数字）原始消息会带上标量
    原文，因此这里只保留出错键路径或行列位置；重复键在解析前先查。
    """
    try:
        node = yaml.compose(text)
    except RecursionError:
        raise ConfigValidationError(
            "Invalid configuration: (yaml): recursive or excessively nested YAML",
            paths=("(yaml)",),
        ) from None
    except yaml.YAMLError as exc:
        location = _yaml_location(getattr(exc, "problem_mark", None))
        raise ConfigValidationError(
            f"Invalid configuration: {location}: invalid YAML syntax",
            paths=(location,),
        ) from None
    if node is not None:
        _check_duplicate_keys(node)
        _validate_tagged_scalars(node)
    try:
        return yaml.load(text, Loader=yaml.SafeLoader)
    except Exception as exc:  # yaml.YAMLError 及其构造阶段的 ValueError 等
        mark = getattr(exc, "problem_mark", None)
        location = _node_path_for_mark(node, mark) or _yaml_location(mark)
        raise ConfigValidationError(
            f"Invalid configuration: {location}: invalid YAML value",
            paths=(location,),
        ) from None


def load_yaml_file(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ConfigValidationError(
            "Invalid configuration: (yaml): file is not valid UTF-8",
            paths=("(yaml)",),
        ) from None
    return load_yaml_data(text)


def model_config_field_paths(model: type[BaseModel] = AppConfig) -> set[str]:
    """返回模型树的叶子字段路径集，供 example 防漂移契约使用。"""
    paths: set[str] = set()

    def visit(model_type: type[BaseModel], prefix: str) -> None:
        for name, model_field in model_type.model_fields.items():
            path = f"{prefix}.{name}" if prefix else name
            annotation = model_field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                visit(annotation, path)
            else:
                paths.add(path)

    visit(model, "")
    return paths


def mapping_config_field_paths(mapping: Mapping[str, Any]) -> set[str]:
    """返回 YAML 映射的叶子字段路径集（空映射本身算一个叶子）。"""
    paths: set[str] = set()

    def visit(value: object, prefix: str) -> None:
        if isinstance(value, Mapping) and value:
            for key, child in value.items():
                visit(child, f"{prefix}.{key}" if prefix else str(key))
        else:
            paths.add(prefix)

    for key, child in mapping.items():
        visit(child, str(key))
    return paths


def _leaf_mapping(value: object, prefix: str = "") -> dict[str, object]:
    if isinstance(value, Mapping) and value:
        leaves: dict[str, object] = {}
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            leaves.update(_leaf_mapping(child, path))
        return leaves
    return {prefix: value}


def changed_config_paths(old: AppConfig, new: AppConfig) -> tuple[str, ...]:
    """按叶子路径比较两棵配置树，只返回路径，不返回任何值。"""
    old_leaves = _leaf_mapping(old.model_dump(mode="python"))
    new_leaves = _leaf_mapping(new.model_dump(mode="python"))
    return tuple(
        sorted(
            path
            for path in set(old_leaves) | set(new_leaves)
            if old_leaves.get(path) != new_leaves.get(path)
        )
    )


# 热更新白名单写死在代码里：只有功能开关可不重启变更；其余键
# （端口/地址、模型、路径、限流数值、名单等）一律必须重启才生效。
HOT_RELOADABLE_CONFIG_PATHS = frozenset(
    {
        "connection_health.enabled",
        "proactive_care.enabled",
        "read_only_tools.enabled",
        "read_only_tools.policies.current_time.enabled",
        "read_only_tools.policies.date_countdown.enabled",
        "read_only_tools.policies.weather_lookup.enabled",
        "read_only_tools.policies.web_search.enabled",
        "read_only_tools.policies.web_search.content_extraction_enabled",
        "tools.alert_private_msg.enabled",
        "tools.friend_management.enabled",
        "tools.group_chat.enabled",
        "tools.qzone.enabled",
        "tools.send_private_msg.enabled",
    }
)


def validate_hot_reload_paths(paths: tuple[str, ...] | list[str]) -> None:
    disallowed = sorted(set(paths) - HOT_RELOADABLE_CONFIG_PATHS)
    if disallowed:
        raise ConfigValidationError(
            "Configuration changes require restart: " + ", ".join(disallowed),
            paths=tuple(disallowed),
        )


def append_config_audit(
    audit_path: Path,
    changed_paths: tuple[str, ...] | list[str],
    *,
    actor: str,
    result_code: str,
    failure_paths: tuple[str, ...] | list[str] = (),
) -> None:
    """追加一行开关变更审计；只记路径与结果码，绝不记配置值。"""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "action": "config.reload",
        "result_code": result_code,
        "changed_paths": sorted(changed_paths),
    }
    if failure_paths:
        record["failure_paths"] = sorted(failure_paths)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


@dataclass
class ConfigReloader:
    """带热更新白名单校验的配置重读器（供后续阶段接线到运行进程）。

    每次重读先完整校验候选配置；只要有一处非白名单变更就整体拒绝，
    并留下 rejected 审计行；全部变更都在白名单内才原子替换。
    """

    path: Path
    config: AppConfig
    _mtime_ns: int
    audit_path: Path | None = None
    actor: str = "local"

    @classmethod
    def load(cls, path: Path, **kwargs) -> "ConfigReloader":
        config = validate_app_config(load_yaml_file(path) or {})
        return cls(path=path, config=config, _mtime_ns=path.stat().st_mtime_ns, **kwargs)

    def _audit_rejected(
        self,
        changed: tuple[str, ...],
        failure_paths: tuple[str, ...] = (),
    ) -> None:
        if self.audit_path is not None:
            append_config_audit(
                self.audit_path,
                changed,
                actor=self.actor,
                result_code="rejected",
                failure_paths=failure_paths,
            )

    def reload_if_changed(self) -> tuple[str, ...]:
        mtime_ns = self.path.stat().st_mtime_ns
        if mtime_ns == self._mtime_ns:
            return ()
        try:
            candidate = validate_app_config(load_yaml_file(self.path) or {})
        except ConfigValidationError as exc:
            # 解析/校验失败的候选同样留 rejected 审计，只记出错路径。
            self._audit_rejected((), failure_paths=exc.paths)
            raise
        changed = changed_config_paths(self.config, candidate)
        try:
            validate_hot_reload_paths(changed)
        except ConfigValidationError as exc:
            self._audit_rejected(changed, failure_paths=exc.paths)
            raise
        previous_config, previous_mtime_ns = self.config, self._mtime_ns
        self.config = candidate
        self._mtime_ns = mtime_ns
        if self.audit_path is not None:
            try:
                append_config_audit(
                    self.audit_path, changed, actor=self.actor, result_code="ok"
                )
            except Exception:
                # 审计写不进去就不算发布成功：恢复旧配置与旧时间戳，
                # 下次重读会再次尝试，不留“已生效未审计”的状态。
                self.config = previous_config
                self._mtime_ns = previous_mtime_ns
                raise
        return changed


@dataclass(frozen=True)
class Settings:
    onebot_secret: str | None = field(repr=False)
    dev: bool
    app: dict
    safety: dict
    persona: str
    root: Path
    config: AppConfig | None = field(default=None, repr=False)
    secret_provider: SecretProvider = field(
        default_factory=EnvironmentSecretProvider, repr=False
    )


def _sanitize_persona_value(value: object) -> str:
    """可配字段只当数据：去掉模板花括号与明显指令围栏，防止注入改写写死章节。"""
    text = "" if value is None else str(value)
    text = text.replace("{{", "").replace("}}", "")
    for marker in ("```", "<|", "SYSTEM:", "系统提示", "## 边界", "## 被追问"):
        text = text.replace(marker, "")
    return " ".join(text.split())


def _render_persona(template: str, profile: dict, display_name: str) -> str:
    """把 persona yaml 与模型显示名填进 persona.md；写死章节靠模板本身，不由字段覆盖。"""
    name = _sanitize_persona_value(profile.get("display_name") or profile.get("name") or "阿年")
    age_desc = _sanitize_persona_value(profile.get("age_desc") or "")
    if not age_desc and profile.get("age") not in (None, ""):
        age_desc = _sanitize_persona_value(f"对外年龄设定为 {profile.get('age')} 岁。")
    role = _sanitize_persona_value(
        profile.get("role_title") or profile.get("relationship") or "知心朋友"
    )
    profession = _sanitize_persona_value(
        profile.get("profession") or profile.get("occupation") or ""
    )
    tags = profile.get("personality_tags") or []
    if isinstance(tags, list):
        tags_text = (
            "、".join(_sanitize_persona_value(x) for x in tags if str(x).strip()) or "温和、耐心"
        )
    else:
        tags_text = _sanitize_persona_value(tags)
    style = profile.get("speaking_style")
    if isinstance(style, dict):
        speaking = _sanitize_persona_value(
            style.get("tone") or style.get("channel") or "QQ 口语短句"
        )
        max_sentences = _sanitize_persona_value(style.get("max_sentences_per_reply") or 3)
        emoji = _sanitize_persona_value(style.get("emoji") or style.get("emoji_level") or "少")
    else:
        speaking = _sanitize_persona_value(
            style or "QQ 口语短句；先接情绪再提问；对方想要时再给建议。"
        )
        max_sentences = _sanitize_persona_value(profile.get("max_sentences_per_reply") or 3)
        emoji = _sanitize_persona_value(profile.get("emoji_level") or "少")
    profession_line = profession if profession else ""
    if profession_line and not profession_line.endswith(("。", ".", "！", "!")):
        profession_line = profession_line + "。"
    age_line = age_desc if age_desc else ""
    city = _sanitize_persona_value(profile.get("city") or "")
    city_line = f"现在在{city}。" if city else ""
    interests = profile.get("interests") or []
    if isinstance(interests, list):
        interests = [ _sanitize_persona_value(x) for x in interests if str(x).strip() ]
        interests_line = ("兴趣：" + "、".join(interests) + "。") if interests else ""
    else:
        interests_line = _sanitize_persona_value(interests)
        interests_line = f"兴趣：{interests_line}。" if interests_line else ""
    mapping = {
        "{{DISPLAY_NAME}}": name,
        "{{NAME}}": name,
        "{{ROLE_TITLE}}": role,
        "{{RELATIONSHIP}}": role,
        "{{AGE_DESC}}": age_line,
        "{{AGE_LINE}}": (
            age_line + ("。" if age_line and not age_line.endswith("。") else "")
        ) if age_line else "",
        "{{PROFESSION}}": profession_line,
        "{{OCCUPATION_LINE}}": profession_line,
        "{{CITY_LINE}}": city_line,
        "{{CITY}}": city,
        "{{INTERESTS_LINE}}": interests_line,
        "{{PERSONALITY_TAGS}}": tags_text,
        "{{SPEAKING_STYLE}}": speaking,
        "{{SPEAKING_CHANNEL}}": speaking,
        "{{SPEAKING_TONE}}": speaking,
        "{{MAX_SENTENCES}}": max_sentences,
        "{{EMOJI_LEVEL}}": emoji,
        "{{EMOJI_POLICY}}": emoji,
        "{{BOUNDARIES_EXTRA}}": "（无）",
        "{{LLM_DISPLAY_NAME}}": _sanitize_persona_value(display_name),
    }
    out = template
    for key, value in mapping.items():
        out = out.replace(key, value)
    return out



def load_settings(secret_provider: SecretProvider | None = None) -> Settings:
    secrets_provider = secret_provider or EnvironmentSecretProvider()
    for name, value in dotenv_values(Path.cwd() / ".env", interpolate=False).items():
        if name in SECRET_ENV_NAMES:
            logging.getLogger("companion.startup").warning(
                "忽略 .env 中的 %s；密钥必须来自进程环境变量", name
            )
        elif value is not None:
            os.environ.setdefault(name, value)
    dev = os.getenv("DEV", "0") == "1"
    secret = secrets_provider.get("ONEBOT_SECRET")
    if not secret:
        raise RuntimeError("ONEBOT_SECRET is required")
    if not secrets_provider.get("GMAIL_APP_PASSWORD"):
        logging.getLogger("companion.startup").warning("邮件告警未配置，已关闭")
    config_path = Path(os.getenv("CONFIG_PATH", str(ROOT / "config.yaml")))
    if not config_path.exists():
        config_path = ROOT / "config.example.yaml"
    raw_app = load_yaml_file(config_path) or {}
    if not isinstance(raw_app, dict):
        raise ConfigValidationError(
            "Invalid configuration: (root): must be a mapping", paths=("(root)",)
        )
    raw_app = deepcopy(raw_app)
    # 覆盖顺序：模型默认值 < config.yaml < 进程环境（仅少量非密钥项）。
    for env_name, key in (("NAPCAT_BASE_URL", "napcat_base_url"),):
        if os.getenv(env_name):
            raw_app[key] = os.environ[env_name]
    app_config = validate_app_config(raw_app)
    if app_config.llm.provider == "codex" and os.getenv("ALLOW_INSECURE_CODEX") != "1":
        raise RuntimeError("llm.provider=codex requires ALLOW_INSECURE_CODEX=1")

    # 兼容视图：只保留文件/环境中显式出现的键（与旧 loader 的 .get
    # 默认语义一致），另加旧 loader 本来就会补的两处：llm.provider
    # 默认值与双名名单。正式默认值全集在 app_config 中。
    app = raw_app
    care_ids = app.pop("care_known_user_ids", None)
    if "allowed_private_user_ids" not in app and care_ids is not None:
        app["allowed_private_user_ids"] = care_ids
    app["allowed_private_user_ids"] = list(app_config.allowed_private_user_ids)
    app["care_known_user_ids"] = list(app_config.care_known_user_ids)
    llm_app = dict(app.get("llm") or {})
    llm_app["provider"] = app_config.llm.provider
    app["llm"] = llm_app
    safety_path = Path(app.get("safety_file", ROOT / "config/safety.yaml"))
    persona_path = Path(app.get("persona_file", ROOT / "config/persona.md"))
    configured = app.get("persona_profile_file")
    candidates = []
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        [
            ROOT / "config/persona.yaml",
            ROOT / "config/persona.example.yaml",
            ROOT / "config/persona.profile.yaml",
        ]
    )
    profile_path = next((path for path in candidates if path.exists()), candidates[-1])
    safety = load_yaml_file(safety_path) or {}
    if not isinstance(safety, dict):
        raise ConfigValidationError(
            "Invalid configuration: (safety): must be a mapping", paths=("(safety)",)
        )
    profile: dict = {}
    if profile_path.exists():
        loaded = load_yaml_file(profile_path) or {}
        if isinstance(loaded, dict):
            profile = loaded
    provider = str(app.get("llm", {}).get("provider", "deepseek"))
    display = str(app.get("llm", {}).get("display_name") or provider)
    persona = _render_persona(persona_path.read_text(encoding="utf-8"), profile, display)
    app["persona_profile"] = profile
    return Settings(
        secret,
        dev,
        app,
        safety,
        persona,
        ROOT,
        config=app_config,
        secret_provider=secrets_provider,
    )
