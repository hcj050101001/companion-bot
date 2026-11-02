"""NapCat 动作登记表（代码内注册表骨架，N0 落成）。

来源与口径：全接口接入方案 §1.1/§2/§5/§2.6——本机 NapCat 4.18.41
bundle 动作注册表对象（未注释 182 条）× 调研报告命名集（144）× 方案
登记表三方对账的固化结果，逐项处置见每条 ``disposition``。三方一致
契约测试在 tests/test_napcat_registry_n0.py；证据与定名记录在
docs/napcat-verification-n0.md；bundle 快照在
tests/fixtures/napcat/bundle_registry_snapshot.json。

状态五元组（方案 §1.1）：existence（verified/absent）× schema
（verified/unverified）× implemented（facade+fake 是否落地）×
behavior_verified（契约用例是否覆盖）× default_enabled。「已接入」
计数只认 implemented 与 behavior_verified 双达标者——N1 阶段落地
L0 canonical 62 条，N2 阶段落地 owner_stage=N2 的 canonical 31 条
（消息形态发送类 + L1 互动 mutation + get_ai_record），合计 93 条；
其余条目两者仍为 False。注：send_private_msg 的现网在用仍是旧路径，
门面落地指框架内契约面接通，不等于现网已切换。
schema 两档口径：verified = 官方文档有参数/返回说明且 bundle 在册
（双源），或官方仓库 action 类证实；unverified = 仅在册/单源，schema
内容待验——第 2 级只读探针只能复证在册与必填参数存在，不升级此档。

本模块是纯数据（N0 落地时为不被运行时 import 的骨架；N1 起被
gateway 导入派生 ActionSpec，数据本身仍无运行时副作用）。等级
默认开关按方案 §5：L0 默认开、L1/L2 默认关、L3 默认硬禁（硬禁
自 N1 Gateway 落地起强制，本表先行登记）。

N4 追记：owner_stage=N4 的 canonical 39 条（L2 群管理/关系/资料/
运维 33 + L1 群签到/群待办/好友备注/qzone 发送 6）已落门面与 Fake
契约（implemented/behavior_verified 双 True），已接入计数 93 → 132。
L2 的运行时门控（逐资源授权、确认令牌、角色复核、默认 dry_run）
在 Gateway 的 canonical 路径执行，详见 adapters/gateway.py 与
tests/test_group_domain_n4.py。

N5 追记：文件与媒体域 canonical 28 条（E 写 9 + E2 写 12 + F 写 7）
已落门面与 Fake 契约（implemented/behavior_verified 双 True），
已接入计数 132 → 160（以 integrated_count() 实测为准）。按 bundle
payload schema 证据（docs/napcat-schema-evidence-n5.md，「官方仓库
action 类证实」档）共 28 条 schema 由 unverified 升 verified
（新接写动作 19 + 已接 L0 的 E2/F/E 读动作 8 + test_download_stream 1）；
证据集 40 个动作中有 28 个的 returnSchema 为 p.Any、无字段级
return 定义，
不影响 payload schema 档（caveat 见 N5 阶段文档）。定案纠正：
move_group_file / rename_group_file 经 schema 证据复核为独立
canonical（原 N0 别名登记撤销），别名条目 7 → 5、canonical
167 → 169。test_download_stream 定性为测试专用合成流：不接门面、
implemented 保持 False、owner_stage 改 N6 终审处置。

N6 追记（终审，全线收口）：L3 八条（owner_stage=N6）经受控解锁
落地（Gateway 构造期名单 + ops_cli + account 授权 + 审计，见
adapters/gateway.py），implemented/behavior_verified 双 True，
已接入计数 160 → 167。终审改判两条：test_download_stream（平台
测试脚手架）与 .get_word_slices（注册表有键、无 handler 类）均
改判 excluded_internal——registered 174 → 172、canonical 169 →
167，至此 registered 的 canonical 条目全部接入（167/167）。
schema 终审升档 8 条（get_clientkey / get_rkey / get_rkey_server /
send_packet / get_share_link / get_unidirectional_friend_list /
bot_exit / set_restart，均据 bundle 动作类证据，docs/napcat-
schema-evidence-n6.md），verified 130 → 138。pending 两条
（get_group_todo、delete_unidirectional_friend）经终审维持挂起
并写明路径；ops_only 两条维持原处置。全量终局见
docs/napcat-integration-closeout.md。

待验复核追记（2026-11-01，分支 ai/qqbot-action-verify，证据见
docs/napcat-action-verify-2026-11-01.md）：pending 两条经 bundle
全文穷举搜索（严于注册表/动作类抽取）与现网复探后销账——
get_group_todo 改判 nonexistent（1 → 2），
delete_unidirectional_friend 改判 not_supported（4 → 5），
pending_verification 2 → 0、至此清零。总条目 191 不变；已接入
计数 167 与 schema verified 138 均不变（两条本就未接入、无
schema 可验）。两条的 review_point 升级重对账口径不变，未来
版本出现新事实时按新证据再改判。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionEntry:
    """一条动作登记（方案 §2 登记表 + §3.1 ActionSpec 的 N0 骨架形态）。"""

    name: str
    domain: str | None  # A 消息 / B 群管理 / C 好友与关系 / D 历史与媒体解析 /
    # E 文件传输与群文件 / E2 闪传·在线·流式 / F 群相册 / G 资料与自身状态 / H 系统凭证
    tier: str | None  # OB11 / GCQ / NC / AFX
    level: str | None  # L0 只读 / L1 低风险互动 / L2 关系与群管理 / L3 账号安全敏感
    kind: str | None  # message / query / mutation / sensitive
    disposition: str  # registered / ops_only / excluded_internal / deprecated /
    # not_supported / pending_verification / nonexistent / not_registered
    existence: str  # verified / absent
    schema: str  # verified / unverified
    implemented: bool  # 门面 + Fake 是否落地（N0 全 False）
    behavior_verified: bool  # 契约用例是否覆盖（N0 全 False）
    default_enabled: bool  # 按等级的默认开关（方案 §5），门面落地后生效
    alias_of: str | None  # 别名条目指向的 canonical 名
    owner_stage: str | None  # 责任阶段（挂起项为复核阶段）
    review_point: str  # 复核点
    notes: str


NAPCAT_VERSION = "4.18.41"
BUNDLE_MD5 = "7fdb4903c8867e7db0b3345ad226c805"
REVIEW_POINT_DEFAULT = "N0 已复核；每次 NapCat 升级时重跑三方对账"
ACTION_REGISTRY: tuple[ActionEntry, ...] = (
    ActionEntry(
        "send_private_msg",
        domain="A", tier="OB11", level="L1", kind="message",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="现网唯一发送通道；N2 门面与投递段通道已落地，现网仍走旧路径"),
    ActionEntry(
        "send_group_msg",
        domain="A", tier="OB11", level="L1", kind="message",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="N2 已接线（门面 + 投递群寻址 + 两级额度）；运行时默认硬关，仅 Fake 验收"),
    ActionEntry(
        "send_msg",
        domain="A", tier="OB11", level="L1", kind="message",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="按 message_type 分发的统一门面"),
    ActionEntry(
        "delete_msg",
        domain="A", tier="OB11", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="撤回；受 LRU 约束，只能撤近期消息"),
    ActionEntry(
        "get_msg",
        domain="A", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="ID 过期即失败，失败口径须在契约明示"),
    ActionEntry(
        "get_forward_msg",
        domain="A", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "send_like",
        domain="A", tier="OB11", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="官方提示注意频率；注册表 rate 字段随 N1 落"),
    ActionEntry(
        "send_group_forward_msg",
        domain="A", tier="GCQ", level="L1", kind="message",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "send_private_forward_msg",
        domain="A", tier="GCQ", level="L1", kind="message",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "send_forward_msg",
        domain="A", tier="NC", level="L1", kind="message",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "forward_friend_single_msg",
        domain="A", tier="NC", level="L1", kind="message",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "forward_group_single_msg",
        domain="A", tier="NC", level="L1", kind="message",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_msg_emoji_like",
        domain="A", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "fetch_emoji_like",
        domain="A", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "friend_poke",
        domain="A", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "group_poke",
        domain="A", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "send_poke",
        domain="A", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "send_group_ai_record",
        domain="A", tier="NC", level="L1", kind="message",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="AI 语音（群）；与 get_ai_characters 配套"),
    ActionEntry(
        "get_ai_characters",
        domain="A", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="N0 第 2 级探针：空参回 400 schema 校验错，存在必填参数"),
    ActionEntry(
        "get_ai_record",
        domain="A", tier="NC", level="L1", kind="query",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="AI 语音合成（取 record 数据）；在册·schema 待验；探针同 get_ai_characters"),
    ActionEntry(
        "mark_msg_as_read",
        domain="A", tier="GCQ", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "mark_private_msg_as_read",
        domain="A", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "mark_group_msg_as_read",
        domain="A", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "_mark_all_as_read",
        domain="A", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="隐藏 API 命名，门面内封装；单源，schema 待验"),
    ActionEntry(
        "set_input_status",
        domain="A", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        ".handle_quick_operation",
        domain="A", tier="GCQ", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="事件快捷操作（批请求/回复），属关系处置"),
    ActionEntry(
        "translate_en2zh",
        domain="A", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="单源，schema 待验"),
    ActionEntry(
        "click_inline_keyboard_button",
        domain="A", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="在册·schema 待验（原 Apifox-only）"),
    ActionEntry(
        ".get_word_slices",
        domain="A", tier="NC", level="L0", kind="query",
        disposition="excluded_internal", existence="verified", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage=None,
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "N6 终审改判 excluded_internal（证据升级，非口径漂移）：N0 时"
            "仅凭注册表对象在册登记为 registered；N6 以动作类抽取复核"
            "（docs/napcat-schema-evidence-n6.md）证实该键名在全 bundle "
            "仅出现 1 次（注册表对象本身）、无任何 actionName 绑定类，"
            "4.18.41 无 handler——与 unknown 占位键同形态，非接口。"
            "门面已同步下线。未来路径：NapCat 升级重跑抽取若出现动作"
            "类，或值守 WebUI Debug 实调证实可达，再经登记表修订恢复"
            " registered"
        )),
    ActionEntry(
        "get_group_info",
        domain="B", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_group_info_ex",
        domain="B", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_group_list",
        domain="B", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_group_member_info",
        domain="B", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_group_member_list",
        domain="B", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="大群返回量大，契约须分页/截断口径"),
    ActionEntry(
        "get_group_honor_info",
        domain="B", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_group_shut_list",
        domain="B", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_group_signed_list",
        domain="B", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="N0 定名落定：注册表键名即此名；探针证必填参数 schema 存在，schema 内容待验"),
    ActionEntry(
        "get_group_at_all_remain",
        domain="B", tier="GCQ", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_group_system_msg",
        domain="B", tier="GCQ", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_group_ignored_notifies",
        domain="B", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "N0 定名落定：注册名为 canonical；报"
            "告名 get_group_ignore_add_request 在"
            " bundle 并存注册，记别名条目"
        )),
    ActionEntry(
        "get_group_ignore_add_request",
        domain="B", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=True,
        alias_of="get_group_ignored_notifies", owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="报告名；与注册名并存于 bundle，按别名条目登记"),
    ActionEntry(
        "set_group_kick",
        domain="B", tier="OB11", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_group_ban",
        domain="B", tier="OB11", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_group_whole_ban",
        domain="B", tier="OB11", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_group_admin",
        domain="B", tier="OB11", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_group_card",
        domain="B", tier="OB11", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_group_name",
        domain="B", tier="OB11", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_group_special_title",
        domain="B", tier="OB11", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_group_leave",
        domain="B", tier="OB11", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="退群/解散（is_dismiss）；不可逆，确认门槛最高档"),
    ActionEntry(
        "set_group_portrait",
        domain="B", tier="GCQ", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_group_add_request",
        domain="B", tier="OB11", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "send_group_sign",
        domain="B", tier="GCQ", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="群签到 canonical（go-cqhttp 名）；set_group_sign 为并存别名"),
    ActionEntry(
        "set_group_sign",
        domain="B", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of="send_group_sign", owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="群签到 NapCat 名，与 send_group_sign 并存注册"),
    ActionEntry(
        "get_essence_msg_list",
        domain="B", tier="GCQ", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_essence_msg",
        domain="B", tier="GCQ", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "delete_essence_msg",
        domain="B", tier="GCQ", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "_send_group_notice",
        domain="B", tier="GCQ", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "_get_group_notice",
        domain="B", tier="GCQ", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "_del_group_notice",
        domain="B", tier="NC", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_group_kick_members",
        domain="B", tier="NC", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记（注册表独有候选）；批量踢人确切名，§2.5 待验销账"),
    ActionEntry(
        "set_group_todo",
        domain="B", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；群待办族（写）"),
    ActionEntry(
        "complete_group_todo",
        domain="B", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；群待办族（写）"),
    ActionEntry(
        "cancel_group_todo",
        domain="B", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；群待办族（写）"),
    ActionEntry(
        "get_group_detail_info",
        domain="B", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；与 get_group_info/_ex 并存对账；探针证必填参数 schema 存在"),
    ActionEntry(
        "get_group_share_link",
        domain="B", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；探针证必填参数 schema 存在"),
    ActionEntry(
        "set_group_add_option",
        domain="B", tier="NC", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；群设置族"),
    ActionEntry(
        "set_group_member_invite_policy",
        domain="B", tier="NC", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；群设置族"),
    ActionEntry(
        "set_group_member_permissions",
        domain="B", tier="NC", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；群设置族"),
    ActionEntry(
        "set_group_new_member_history_visibility",
        domain="B", tier="NC", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；群设置族"),
    ActionEntry(
        "set_group_robot_add_option",
        domain="B", tier="NC", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；群设置族"),
    ActionEntry(
        "set_group_search",
        domain="B", tier="NC", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；群设置族"),
    ActionEntry(
        "set_group_remark",
        domain="B", tier="NC", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="在册·schema 待验（原 Apifox-only）"),
    ActionEntry(
        "get_friend_list",
        domain="C", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_unidirectional_friend_list",
        domain="C", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "在册·文档冲突（官方 ❌ × bundle 未注"
            "释在册，以本机为准）；注册表另有一条"
            "同键注释行（重复登记被注掉）；N6 以 "
            "bundle 动作类证据升 schema verified"
            "（payload 空、return 为单向好友数组）"
        )),
    ActionEntry(
        "get_stranger_info",
        domain="C", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_recent_contact",
        domain="C", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_profile_like",
        domain="C", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "nc_get_user_status",
        domain="C", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_robot_uin_range",
        domain="C", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_friend_add_request",
        domain="C", tier="OB11", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="现网 friend_management 有先例（默认关 + 二次确认），口径沿用"),
    ActionEntry(
        "delete_friend",
        domain="C", tier="GCQ", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="不可逆，确认门槛最高档"),
    ActionEntry(
        "fetch_custom_face",
        domain="C", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "create_collection",
        domain="C", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_collection_list",
        domain="C", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_friends_with_category",
        domain="C", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记"),
    ActionEntry(
        "get_doubt_friends_add_request",
        domain="C", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记"),
    ActionEntry(
        "set_doubt_friends_add_request",
        domain="C", tier="NC", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记"),
    ActionEntry(
        "set_friend_remark",
        domain="C", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="在册·schema 待验（原 Apifox-only）"),
    ActionEntry(
        "send_qzone_msg",
        domain="C", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；QQ 空间族，对应现行 qzone 能力面"),
    ActionEntry(
        "delete_qzone_msg",
        domain="C", tier="NC", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；QQ 空间族"),
    ActionEntry(
        "fetch_custom_face_detail",
        domain="C", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；自定义表情族"),
    ActionEntry(
        "add_custom_face",
        domain="C", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；自定义表情族"),
    ActionEntry(
        "set_custom_face_desc",
        domain="C", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；自定义表情族"),
    ActionEntry(
        "delete_custom_face",
        domain="C", tier="NC", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；自定义表情族"),
    ActionEntry(
        "get_group_msg_history",
        domain="D", tier="GCQ", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="依赖 NTQQ 本地数据（非 NapCat LRU 存储）"),
    ActionEntry(
        "get_friend_msg_history",
        domain="D", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="同 get_group_msg_history"),
    ActionEntry(
        "get_image",
        domain="D", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_record",
        domain="D", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_file",
        domain="D", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="带凭证 URL 的结果在日志/诊断中一律脱敏（N1 交付物）"),
    ActionEntry(
        "can_send_image",
        domain="D", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "can_send_record",
        domain="D", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "ocr_image",
        domain="D", tier="GCQ", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes="OCR 接入排 N5（方案 §6）；.ocr_image 为并存别名"),
    ActionEntry(
        ".ocr_image",
        domain="D", tier="GCQ", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=False, behavior_verified=False, default_enabled=True,
        alias_of="ocr_image", owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes="OCR 隐藏名，与 ocr_image 并存注册"),
    ActionEntry(
        "fetch_ptt_text",
        domain="D", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；语音转文字"),
    ActionEntry(
        "get_emoji_likes",
        domain="D", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；与 fetch_emoji_like 并存对账；探针证必填参数 schema 存在"),
    ActionEntry(
        "upload_private_file",
        domain="E", tier="GCQ", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes="N5 接通；schema 定稿证据：docs/napcat-schema-evidence-n5.md"),
    ActionEntry(
        "download_file",
        domain="E", tier="GCQ", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "落盘行为须审计；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "get_private_file_url",
        domain="E", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "N0 定名落定：注册表键名即此名；探针证必填参数 schema 存在；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "upload_group_file",
        domain="E", tier="GCQ", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes="N5 接通；schema 定稿证据：docs/napcat-schema-evidence-n5.md"),
    ActionEntry(
        "delete_group_file",
        domain="E", tier="GCQ", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes="N5 接通；schema 定稿证据：docs/napcat-schema-evidence-n5.md"),
    ActionEntry(
        "create_group_file_folder",
        domain="E", tier="GCQ", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes="N5 接通；schema 定稿证据：docs/napcat-schema-evidence-n5.md"),
    ActionEntry(
        "delete_group_folder",
        domain="E", tier="GCQ", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "N0 定名落定：注册表键名即此名；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "trans_group_file",
        domain="E", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "N5 定案纠正（按方案分工，非口径漂移）：bundle schema "
            "证据显示 trans/move/rename 为三个独立动作、字段互不"
            "相同，三者均为独立 canonical；trans 仅 group_id+"
            "file_id。证据：docs/napcat-schema-evidence-n5.md §2"
        )),
    ActionEntry(
        "move_group_file",
        domain="E", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "N5 定案纠正：独立 canonical（原 N0 别名登记撤销）；"
            "字段 group_id/file_id/current_parent_directory/"
            "target_parent_directory，证据 docs/napcat-schema-evidence-n5.md §2"
        )),
    ActionEntry(
        "rename_group_file",
        domain="E", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "N5 定案纠正：独立 canonical（原 N0 别名登记撤销）；"
            "字段 group_id/file_id/current_parent_directory/new_name，"
            "证据 docs/napcat-schema-evidence-n5.md §2"
        )),
    ActionEntry(
        "get_group_file_system_info",
        domain="E", tier="GCQ", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_group_root_files",
        domain="E", tier="GCQ", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_group_files_by_folder",
        domain="E", tier="GCQ", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_group_file_url",
        domain="E", tier="GCQ", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "create_flash_task",
        domain="E2", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "闪传文件会过期，契约须明示；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "get_flash_file_list",
        domain="E2", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "探针证必填参数 schema 存在；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "get_flash_file_url",
        domain="E2", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "探针证必填参数 schema 存在；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "send_flash_msg",
        domain="E2", tier="NC", level="L1", kind="message",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "以 flashtransfer 段发送；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "get_fileset_info",
        domain="E2", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "探针证必填参数 schema 存在；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "get_fileset_id",
        domain="E2", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "探针证必填参数 schema 存在；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "get_share_link",
        domain="E2", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "探针证必填参数 schema 存在；N6 补抽 bundle 动作类证据升 "
            "verified（payload 唯一必填 fileset_id；return 为 p.Any 无"
            "字段级定义，入 N6 终审 p.Any 口径，docs/napcat-schema-"
            "evidence-n6.md）"
        )),
    ActionEntry(
        "download_fileset",
        domain="E2", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "落盘动作，审计；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "send_online_file",
        domain="E2", tier="NC", level="L1", kind="message",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes="N5 接通；schema 定稿证据：docs/napcat-schema-evidence-n5.md"),
    ActionEntry(
        "send_online_folder",
        domain="E2", tier="NC", level="L1", kind="message",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes="N5 接通；schema 定稿证据：docs/napcat-schema-evidence-n5.md"),
    ActionEntry(
        "get_online_file_msg",
        domain="E2", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "N0 定名落定：注册表确切名；探针证必填参数 schema 存在；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "receive_online_file",
        domain="E2", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes="N5 接通；schema 定稿证据：docs/napcat-schema-evidence-n5.md"),
    ActionEntry(
        "refuse_online_file",
        domain="E2", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "N0 定名落定：确切名为 refuse（非 reject）；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "cancel_online_file",
        domain="E2", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes="N5 接通；schema 定稿证据：docs/napcat-schema-evidence-n5.md"),
    ActionEntry(
        "upload_file_stream",
        domain="E2", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "流式族；跨设备/Docker 场景用，本机同机部署低优先，接入不延期；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "download_file_stream",
        domain="E2", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "流式族；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "download_file_image_stream",
        domain="E2", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "流式族；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "download_file_record_stream",
        domain="E2", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "流式族；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "test_download_stream",
        domain="E2", tier="NC", level="L1", kind="mutation",
        disposition="excluded_internal", existence="verified", schema="verified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage=None,
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "N6 终审改判 excluded_internal：N5 定性（docs/napcat-schema-"
            "evidence-n5.md §3）为 NapCat 侧测试专用合成流——不读 URL、"
            "不调核心 API、只产合成文本块并可按入参触发测试错误，属"
            "平台测试脚手架、非产品接口（与 test_auto_register_01/02 "
            "同族）；schema 证据保留 verified（抽取事实不变），框架不"
            "接门面、不建 spec"
        )),
    ActionEntry(
        "get_qun_album_list",
        domain="F", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "单源（第三方 SDK），schema 待验；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "upload_image_to_qun_album",
        domain="F", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes="N5 接通；schema 定稿证据：docs/napcat-schema-evidence-n5.md"),
    ActionEntry(
        "upload_images_to_qun_album",
        domain="F", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "v4.18.30 新增，多图一帖；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "upload_video_to_qun_album",
        domain="F", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "v4.18.30 新增；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "set_group_album_media_like",
        domain="F", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes="N5 接通；schema 定稿证据：docs/napcat-schema-evidence-n5.md"),
    ActionEntry(
        "get_group_album_media_list",
        domain="F", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "单源（第三方 SDK），schema 待验；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "del_group_album_media",
        domain="F", tier="NC", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "补登记；删相册媒体确切名，§2.1 F 域待验销账；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "do_group_album_comment",
        domain="F", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "补登记；相册评论确切名，§2.1 F 域待验销账；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "cancel_group_album_media_like",
        domain="F", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N5",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "补登记；"
            "N5 schema 定稿证据：docs/napcat-schema-evidence-n5.md"
        )),
    ActionEntry(
        "set_qq_profile",
        domain="G", tier="GCQ", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_qq_avatar",
        domain="G", tier="NC", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_self_longnick",
        domain="G", tier="NC", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_online_status",
        domain="G", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "_get_model_show",
        domain="G", tier="GCQ", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="在册·schema 待验；机型展示，低优先"),
    ActionEntry(
        "_set_model_show",
        domain="G", tier="GCQ", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="在册·schema 待验；机型展示，低优先"),
    ActionEntry(
        "get_online_clients",
        domain="G", tier="GCQ", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "ArkSharePeer",
        domain="G", tier="NC", level="L1", kind="message",
        disposition="registered", existence="verified", schema="verified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of="send_ark_share", owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="旧名，已标 deprecated；标准化名为 send_ark_share"),
    ActionEntry(
        "ArkShareGroup",
        domain="G", tier="NC", level="L1", kind="message",
        disposition="registered", existence="verified", schema="verified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of="send_group_ark_share", owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="旧名，已标 deprecated；标准化名为 send_group_ark_share"),
    ActionEntry(
        "send_ark_share",
        domain="G", tier="NC", level="L1", kind="message",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；Ark 标准化名"),
    ActionEntry(
        "send_group_ark_share",
        domain="G", tier="NC", level="L1", kind="message",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；Ark 标准化名"),
    ActionEntry(
        "get_mini_app_ark",
        domain="G", tier="NC", level="L1", kind="message",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "set_diy_online_status",
        domain="G", tier="NC", level="L1", kind="mutation",
        disposition="registered", existence="verified", schema="unverified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N2",
        review_point=REVIEW_POINT_DEFAULT,
        notes="补登记；与 set_online_status 并存对账"),
    ActionEntry(
        "get_login_info",
        domain="H", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "get_status",
        domain="H", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes="现网探活在用（旧路径）；good 为 NapCat 上报值，盲区注记沿用"),
    ActionEntry(
        "get_version_info",
        domain="H", tier="OB11", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "clean_cache",
        domain="H", tier="OB11", level="L2", kind="mutation",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N4",
        review_point=REVIEW_POINT_DEFAULT,
        notes="运维动作，L2 审计"),
    ActionEntry(
        "get_cookies",
        domain="H", tier="OB11", level="L3", kind="sensitive",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "默认硬禁（N1 起生效）；N6 补受控解锁（网关构造期名单 + "
            "ops_cli 主体 + account 授权 + 全量审计，见 gateway.py 与 "
            "tests/test_l3_unlock_n6.py）；返回值是凭证，禁入日志/记忆"
        )),
    ActionEntry(
        "get_csrf_token",
        domain="H", tier="OB11", level="L3", kind="sensitive",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes="同 get_cookies"),
    ActionEntry(
        "get_credentials",
        domain="H", tier="OB11", level="L3", kind="sensitive",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes="同 get_cookies"),
    ActionEntry(
        "get_clientkey",
        domain="H", tier="NC", level="L3", kind="sensitive",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "同 cookies 口径；N6 以 bundle 动作类证据升 schema verified"
            "（payload 空、return clientkey 可选，docs/napcat-schema-"
            "evidence-n6.md）；凭证类从未实调"
        )),
    ActionEntry(
        "nc_get_rkey",
        domain="H", tier="NC", level="L3", kind="sensitive",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes="媒体 URL 密钥材料，L3 口径；N0 未实调"),
    ActionEntry(
        "get_rkey",
        domain="H", tier="NC", level="L3", kind="sensitive",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "补登记；与 nc_get_rkey 并存对账；N6 以 bundle 动作类证据升"
            " schema verified（payload 空、return 为 RKey 数组）；未实调"
        )),
    ActionEntry(
        "get_rkey_server",
        domain="H", tier="NC", level="L3", kind="sensitive",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "补登记；N6 以 bundle 动作类证据升 schema verified（payload"
            " 空、return 含 private/group_rkey 与 expired_time）；未实调"
        )),
    ActionEntry(
        "nc_get_packet_status",
        domain="H", tier="NC", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "check_url_safely",
        domain="H", tier="GCQ", level="L0", kind="query",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=True,
        alias_of=None, owner_stage="N1",
        review_point=REVIEW_POINT_DEFAULT,
        notes=""),
    ActionEntry(
        "send_packet",
        domain="H", tier="NC", level="L3", kind="sensitive",
        disposition="registered", existence="verified", schema="verified",
        implemented=True, behavior_verified=True, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "默认禁用且永不进工具面，仅类型化封装 + 审计；N6 以 bundle "
            "动作类证据升 schema verified（cmd/data 必填、rsp 带默认）；"
            "解锁后也只许 ops_cli 在隔离运维边界调用，运行时服务内无"
            "调用点（收敛测试钉死）"
        )),
    ActionEntry(
        "bot_exit",
        domain="H", tier="NC", level="L3", kind="sensitive",
        disposition="ops_only", existence="verified", schema="verified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage=None,
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "登出：运维灾难级动作，只登记为 runbook 动作、永不进运行时"
            "门面；N6 终审维持 ops_only——bundle 动作类证据已补抽"
            "（payload/return 均为空对象，schema 升 verified 仅记抽取"
            "事实，不改变处置：登出永不进 Gateway 解锁面）"
        )),
    ActionEntry(
        "set_restart",
        domain="H", tier="OB11", level=None, kind="mutation",
        disposition="ops_only", existence="verified", schema="verified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage=None,
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "在册·文档冲突（官方标不支持 × bundle"
            " 未注释在册，以本机为准）；运维专用"
            "，与 bot_exit 同格，只走 runbook；"
            "N6 终审维持 ops_only（bundle 动作类证据：payload 空、"
            "return Null，schema 升 verified 仅记抽取事实）"
        )),
    ActionEntry(
        "clean_stream_temp_file",
        domain=None, tier=None, level=None, kind=None,
        disposition="excluded_internal", existence="verified", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage=None,
        review_point=REVIEW_POINT_DEFAULT,
        notes="流传输内部清理，非接口"),
    ActionEntry(
        "test_auto_register_01",
        domain=None, tier=None, level=None, kind=None,
        disposition="excluded_internal", existence="verified", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage=None,
        review_point=REVIEW_POINT_DEFAULT,
        notes="测试占位，非接口"),
    ActionEntry(
        "test_auto_register_02",
        domain=None, tier=None, level=None, kind=None,
        disposition="excluded_internal", existence="verified", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage=None,
        review_point=REVIEW_POINT_DEFAULT,
        notes="测试占位，非接口"),
    ActionEntry(
        "unknown",
        domain=None, tier=None, level=None, kind=None,
        disposition="excluded_internal", existence="verified", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage=None,
        review_point=REVIEW_POINT_DEFAULT,
        notes="占位键，非接口"),
    ActionEntry(
        "get_guild_list",
        domain=None, tier="AFX", level=None, kind=None,
        disposition="deprecated", existence="verified", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage=None,
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "频道/Guild 已废弃（仅 Apifox legacy "
            "列目，bundle 在册但 QQ 频道已被腾讯"
            "收缩），按不存在处理、不接入"
        )),
    ActionEntry(
        "get_guild_service_profile",
        domain=None, tier="AFX", level=None, kind=None,
        disposition="deprecated", existence="verified", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage=None,
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "频道/Guild 已废弃（仅 Apifox legacy "
            "列目，bundle 在册但 QQ 频道已被腾讯"
            "收缩），按不存在处理、不接入"
        )),
    ActionEntry(
        "set_group_anonymous",
        domain=None, tier="OB11", level=None, kind=None,
        disposition="not_supported", existence="absent", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes="官方文档明示「不会支持」；bundle 仅注释行。"),
    ActionEntry(
        "set_group_anonymous_ban",
        domain=None, tier="OB11", level=None, kind=None,
        disposition="not_supported", existence="absent", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes="官方文档明示「不会支持」；bundle 仅注释行。"),
    ActionEntry(
        "qidian_get_account_info",
        domain=None, tier="OB11", level=None, kind=None,
        disposition="not_supported", existence="absent", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes="官方明示不支持；bundle 仅注释行。"),
    ActionEntry(
        "reload_event_filter",
        domain=None, tier="OB11", level=None, kind=None,
        disposition="not_supported", existence="absent", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes="官方明示不支持；bundle 仅注释行。"),
    ActionEntry(
        "delete_unidirectional_friend",
        domain=None, tier="OB11", level=None, kind=None,
        disposition="not_supported", existence="absent", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "2026-11-01 待验复核改判 not_supported"
            "（证据见 docs/napcat-action-verify-2"
            "026-11-01.md）：官方标 ❌、调研报告"
            "「不支持」名单在列、bundle 全文穷举"
            "搜索仅 1 处出现——注册表枚举的注释行"
            "（DeleteUnidirectionalFriendList），"
            "无动作类、无核心方法；其读侧 get_un"
            "idirectional_friend_list 有完整动作"
            "类，证同族系选择性实现、删除名有意不"
            "注册。唯一反证 Apifox 索引按 §2.6 结"
            "论 3「以本机为准」与同形四条先例裁决"
            "不敌本机证据，三方冲突就此销账；升级"
            "重对账若注释行转在册，按 L2 关系口径"
            "补接。本条全程仅静态验证、从未实调"
            "（删除语义红线）。"
        )),
    ActionEntry(
        "get_group_todo",
        domain=None, tier="NC", level=None, kind=None,
        disposition="nonexistent", existence="absent", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "2026-11-01 待验复核改判 nonexistent"
            "（证据见 docs/napcat-action-verify-2"
            "026-11-01.md）：bundle 全文穷举搜索"
            "该名 0 出现——注册表枚举无 GetGroupT"
            "odo 键、无动作类、核心层仅 Set/Comp"
            "lete/Cancel 三个 Oidb 写方法、WebUI"
            " Debug 示例表亦无；N0（2026-10-31）"
            "与本轮（2026-11-01）两次现网实调"
            "（空参 + 机器人测试群真实群号）均回"
            "分发层「不支持的Api get_group_todo」"
            "，与同场负对照逐字同形。待办查询动"
            "作在 NapCat 不存在，与 send_red_pac"
            "ket 同格，产品不得规划群待办查询功"
            "能；NapCat 升级重对账为恢复路径。"
        )),
    ActionEntry(
        "send_red_packet",
        domain=None, tier=None, level=None, kind=None,
        disposition="nonexistent", existence="absent", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes=(
            "发红包接口不存在：全部官方清单无此 A"
            "PI（调研报告 §6.1 明示），仅 lucky_k"
            "ing 事件；不算欠账，产品不得规划红包"
            "功能。"
        )),
    ActionEntry(
        "reboot_normal",
        domain=None, tier=None, level=None, kind=None,
        disposition="not_registered", existence="absent", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes="注册表注释行独有，与 set_restart 语义重复，不登记。"),
    ActionEntry(
        "upload_forward_msg",
        domain=None, tier=None, level=None, kind=None,
        disposition="not_registered", existence="absent", schema="unverified",
        implemented=False, behavior_verified=False, default_enabled=False,
        alias_of=None, owner_stage="N6",
        review_point=REVIEW_POINT_DEFAULT,
        notes="注册表注释行独有、未启用，不登记；转发发送走在册 send_forward_msg 系。"),
)

REGISTRY_BY_NAME = {entry.name: entry for entry in ACTION_REGISTRY}


def integrated_count(entries=None) -> int:
    """方案 §1.1 的「已接入」计数：别名计数前归一——只认 canonical 的
    registered 条目中实现 + 行为验证双达标者。"""
    if entries is None:
        entries = ACTION_REGISTRY
    return sum(
        1
        for e in entries
        if e.disposition == "registered"
        and e.alias_of is None
        and e.implemented
        and e.behavior_verified
    )
