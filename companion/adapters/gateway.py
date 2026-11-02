"""ActionGateway：唯一出站收口（方案 §3.1/§3.2/§5，N1 落地）。

全部出站动作经本网关：查 ActionSpec 注册表（未登记不可调用）、校验
执行主体 principal、L3 硬禁、参数/资源校验、operation_id 防重放、
policy 开关、限流、确认门、dry_run、发送额度（仅私聊发送策略），
最后才经注入的 transport 派发。结果分两层表达：本地门禁结果
（outcome=rejected + gate，或 simulated）与派发后结果（ok / rejected /
ambiguous 三态）绝不混淆——dry_run/pending_confirmation 是独立的
simulated，绝不映射为 ok。

N4 起 L2 在 canonical policy 路径上执行完整授权链（方案 §5）：
逐资源授权上下文（AuthorizationContext）必备且目标须在范围内
（目标按动作声明派生，无关参数不得改写，见 _TARGET_KIND_BY_
ACTION）、不可逆档（set_group_leave / delete_friend）须持绑定
确认令牌（授权链处 peek 校验、全部派发前门禁通过后才 consume）、
群管理动作执行前经 role_resolver 复核机器人群内角色、dry_run
默认为真（policy 须显式关）。legacy policy 路径（friend_management /
qzone 两先例）沿用其既有门控口径（policy 显式配置 + confirmed），
与 N1 的确认门特判同一处理；其中 L2 的 legacy 路径另有授权下限
（c2b，补审第二段 P3）：AuthorizationContext 必备且覆盖声明目标，
不再整段豁免授权链。

审计纪律：logger ``companion.actions`` 每调用一行（principal/
authorizer/action/policy_key/outcome/gate/耗时 ms），绝不记
params/data/URL；凡来自平台/异常的字符串（error_code 等）先过
redact 再截断。

N5 追记：文件域（登记表 domain ∈ {E, E2, F}）动作的审计行在行尾
追加资源摘要（``resources=<sha256 前 16 位,...> size=<n|none>``，
原文永不落日志，非文件域行逐字不变）；F 域 canonical 动作默认
纳入 ActionCircuitBreaker 管控（派发前预检 + 派发后记账，门禁
拒绝不计失败）；注入 ResourceResolver 时文件域资源在步骤 d
另过 resolver.resolve 校验（未注入行为不变）。

N6 追记：L3 受控解锁（方案 §5/§6 N6）——硬禁仍是默认：构造时未
注入解锁名单（``l3_unlocked``) 的网关对 L3 一律 hard_banned，与
N1 起的行为逐字一致。解锁名单是构造期快照（运维配置在进程启动
时注入、重启才换新名单），invoke 无解锁参数、运行期改配置对象
不影响已构造实例。名单内动作仍须依次过：principal == ops_cli
(L3 的 allowed_principals 本就只有 ops_cli，llm_tool 解锁后仍禁）、
授权上下文（AuthorizationContext 必备、目标在 c3 段结构性钉死
为 account、须被 account 范围覆盖，注入参数无从降格）、参数/
资源校验与限流；审计口径不变（每调用一行，只记主体/授权人/
动作/结果码，返回值内容永不入日志）。

补审第二段追记（历史补审 N4–N6 段 13 条修复）：步骤 d 对资源键
值改走路径语义分类（``validate_path_semantic_ref``）——带路径形
态的值一律按本地路径处理，相对路径以构造期 ``resource_base_dir``
解析为 realpath 后查 ``resource_roots``，界外/无 base 一律拒绝，
堵住 ``../`` 穿越把本地文件外发；download_file 另过 SSRF 边界
与落盘名校验。确认令牌的 consume 点后移到派发内层（账本
try_begin 与熔断 allow 之后、真正派发之前）：账本故障/同 id 竞
态/派发点熔断等非执行退出不再烧单次 nonce。L2 的 legacy
policy 路径（friend_management 先例）不再整段豁免授权链：至少
须持覆盖声明目标的 AuthorizationContext（policy 开关与
confirmed 确认门等先例口径不变）；AuthorizationContext 构造期
即归一化范围字段（str/bytes 直接 TypeError，杜绝子串授权）。
"""

import hashlib
import inspect
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from companion import content_extract
from companion.adapters.action_registry import ACTION_REGISTRY, REGISTRY_BY_NAME
from companion.adapters.circuit import ActionCircuitBreaker
from companion.adapters.resources import (
    ResourceKind,
    ResourceValidationError,
    validate_download_url,
    validate_on_disk_name,
    validate_path_semantic_ref,
    validate_resource_ref,
)
from companion.adapters.transport import TransportError, extract_platform_message_id
from companion.multipart import review_bubbles

# 内容审查提取层已迁至 companion.content_extract（与 services
# 投递层共用同一实现）；本模块只保留 invoke 与既有测试所用的
# 私有别名，提取逻辑禁止再在本模块维护第二份。
_PayloadUnreviewable = content_extract.PayloadUnreviewable
_payload_all_strings = content_extract.collect_all_strings
_payload_visible_texts = content_extract.payload_visible_texts
_xml_visible_texts = content_extract.xml_visible_texts

logger = logging.getLogger("companion.actions")

# ------------------------------------------------------------------ 主体

PRINCIPAL_SYSTEM = "system"
PRINCIPAL_PRODUCT_COMMAND = "product_command"
PRINCIPAL_OPS_CLI = "ops_cli"
PRINCIPAL_LLM_TOOL = "llm_tool"
PRINCIPAL_SCHEDULER = "scheduler"
PRINCIPALS = frozenset(
    {
        PRINCIPAL_SYSTEM,
        PRINCIPAL_PRODUCT_COMMAND,
        PRINCIPAL_OPS_CLI,
        PRINCIPAL_LLM_TOOL,
        PRINCIPAL_SCHEDULER,
    }
)

_ALL_PRINCIPALS = PRINCIPALS
_L2_PRINCIPALS = frozenset({PRINCIPAL_SYSTEM, PRINCIPAL_PRODUCT_COMMAND, PRINCIPAL_OPS_CLI})
_L3_PRINCIPALS = frozenset({PRINCIPAL_OPS_CLI})

QUOTA_POLICY_KEYS = frozenset({"send_private_msg", "alert_private_msg"})
_NOT_CONFIGURED_POLICY_KEYS = frozenset({"qzone", "friend_management"})
#: 资源键：值（含列表元素）按 validate_resource_ref 的边界校验。
#: N5 补入文件域新键（file_path/folder_path/thumb_path/files）——
#: 这些键承载本地路径，缺席时裸绝对路径可直通平台（Review P2-2）。
#: 集成复审补入段字段 thumb / audio：thumb 是 video 段的封面
#: 字段（adapters/base.py VideoSegment 文档「file 必填，可含
#: thumb」，仓库自身 CQ 序列化器实证发出 thumb=），audio 是
#: music 自定义段的音频地址字段——两者与 file 同为段级资源
#: 承载字段，缺席时 dict 形态裸绝对路径整段脱检（集成复审 P3）。
_RESOURCE_PARAM_KEYS = frozenset(
    {
        "file",
        "url",
        "path",
        "image",
        "record",
        "file_path",
        "folder_path",
        "thumb_path",
        "files",
        "thumb",
        "audio",
    }
)
_LEDGER_KINDS = frozenset({"message", "mutation"})

# policy_key 别名白名单的额外映射（Review 修复 1）：spec 的
# allowed_policy_keys = {spec.policy_key} ∪ 此表对应项；调用方给出的
# policy_key 不在集合内一律 policy_key_forbidden，杜绝借另一动作的
# 合法 policy_key 绕过本动作自身的开关/额度口径。
_EXTRA_POLICY_KEYS: dict[str, frozenset] = {
    "send_private_msg": frozenset({"alert_private_msg"}),
    "set_friend_add_request": frozenset({"friend_management"}),
    # N4：qzone 工具面迁 send_qzone_msg 后仍以 policy_key=qzone 走
    # 既有 qzone 配置段（enabled/dry_run/限流口径逐项不变）。
    "send_qzone_msg": frozenset({"qzone"}),
}

# 审计字段清洗（Review 修复 8）：调用方字符串只允许短 token 形态。
_AUDIT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

# 资源校验递归化（Review 修复 4）：任意字符串中的 scheme:// 候选
# 过 validate_resource_ref，CQ 码资源参数改走路径语义分类与 dict
# 叶子平价（平价轮裁决）。参数名大小写不敏感、等号两侧容忍空白
# （复审 P3）：与内容提取层的归一口径对齐——同一引用换个大小写/
# 间距形态不得整段绕过资源校验。
#
# CQ 候选提取（集成复审 P2）：旧 _CQ_FILE_RE 把每次匹配锚定在
# 字面 [CQ: 前缀上，findall 非重叠消费使同一 CQ 码内的第二个
# file= 永远不被提取——良性值在前即可让恶意值整段逃逸。改按
# 「先切码段跨度、段内 finditer 全部资源参数出现逐个校验」：
# 与内容层 _cq_param_segments 的多出现口径一致（同名参数的每
# 次出现都是平台解析器可能区别对待的独立出现，逐个受检）。
# 码段跨度的界定刻意宽于内容层 _CQ_CODE_RE 的类型令牌文法
# （任意类型字符、未闭合时延伸到串尾，与旧 _CQ_FILE_RE 的覆
# 盖面保持一致）：资源层漏掉一个跨度即逃逸，方向上只许更严。
# 参数名集 = 段级资源承载字段（file/image/record/video/file
# 段的 file、video 段的 thumb、image/share/music 段的 url 与
# image 封面、music 自定义段的 audio），与 dict 形态的
# _RESOURCE_PARAM_KEYS 段字段部分同口径（集成复审 P3 审计）。
_SCHEME_REF_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s,\]]+")
_CQ_SPAN_RE = re.compile(r"\[CQ:[^\]]*(?:\]|$)")
_CQ_RESOURCE_PARAM_RE = re.compile(
    r"[\s,](file|thumb|url|image|audio)\s*=\s*([^,\]]+)", re.IGNORECASE
)


def _cq_resource_candidates(text: str) -> list[str]:
    """提取字符串中全部 CQ 码段内的资源参数值（逐个出现、保序）。"""
    candidates: list[str] = []
    for span in _CQ_SPAN_RE.findall(text):
        for match in _CQ_RESOURCE_PARAM_RE.finditer(span):
            candidates.append(match.group(2))
    return candidates

# ------------------------------------------------------------- 脱敏 helper

_SENSITIVE_KEY_PARTS = (
    "token",
    "cookie",
    "csrf",
    "clientkey",
    "secret",
    "password",
    "passwd",
    "sign",
    "key",
)
_URL_QUERY = re.compile(r"(https?://[^\s?'\"]+)\?[^\s'\"]*", re.IGNORECASE)
_PARAM_VALUE = re.compile(
    r"(?i)\b(token|access_token|sign|signature|clientkey|cookie|csrf|secret|"
    r"password|passwd|api_key|apikey|key)=([^&\s'\"]+)"
)


def redact_text(s) -> str:
    """脱敏单个字符串：URL 的 query 部分整体打码、疑似密钥参数值打码。"""
    text = str(s)
    text = _URL_QUERY.sub(r"\1?<redacted>", text)
    text = _PARAM_VALUE.sub(r"\1=<redacted>", text)
    return text


def _truncate_redacted(value) -> str:
    return redact_text(value)[:64]


def _audit_token(value) -> str:
    """审计行调用方字段清洗：仅短 token 形态原样保留，其余一律记 invalid。

    含换行/空白/超长等形态的字符串若原样进日志可伪造审计行，故先过
    本清洗（Review 修复 8）；error_code 不走此路，维持 redact+截断。
    """
    text = value if isinstance(value, str) else ""
    return text if _AUDIT_TOKEN_RE.match(text) else "invalid"


def _check_resource_refs(value, allowed_roots, base_dir=None) -> bool:
    """迭代校验 params 任意深度的资源引用（Review 修复 4；封版集成裁决合并）。

    遍历骨架取补审第三段版：显式栈迭代（旧版递归在数百层嵌套载
    荷上先于内容审查爆 RecursionError 穿出 invoke，违「每次
    invoke 必返 ActionResult」；迭代后深度不再受调用栈限制）；
    键名 strip().lower() 归一后比对资源键集（'File' / ' file '
    等大小写与空白变体与正形同口径受检，与内容提取层的键名归
    一保持一致）；资源键上下文穿透嵌套容器（进入资源键即置位、
    容器内保持，后代字符串不脱离资源口径）；环保护按（容器
    id, 是否资源上下文）记账——同一共享子结构在普通上下文验过
    后又在资源上下文出现时须按资源口径再验一次，两种上下文的
    校验口径不同，不是一句纯判定可合并。

    校验器与签名取补审第二段已封版口径：资源上下文内的 str 叶
    子不分深度一律过路径语义分类（validate_path_semantic_ref，
    补审第二段 P2：相对引用以 base_dir 解析 realpath 后查
    roots，不再按不透明 file_id 放行）；CQ 码段内资源参数的
    每次出现逐个提取后同走路径语义分类、与 dict 叶子完全同
    口径（平价轮裁决，R-S2-1 既定修法落实；CQ 匹配的大小写
    不敏感与等号空白容忍取第三段归一版，重复出现逐个提取取
    集成复审 P2 口径）；任意 str 中的裸 scheme:// 候选仍逐个
    过 validate_resource_ref（N1 冻结口径，自由文本 URL 扫描
    与资源参数不是一类）；任一不过返回 False。普通文本中的
    http/https 链接由 validate_resource_ref 放行。
    """
    seen: set[tuple[int, bool]] = set()
    stack = [(value, False)]
    while stack:
        current, in_resource = stack.pop()
        if isinstance(current, dict):
            marker = (id(current), in_resource)
            if marker in seen:
                continue
            seen.add(marker)
            for key, item in current.items():
                child_resource = in_resource or (
                    isinstance(key, str)
                    and key.strip().lower() in _RESOURCE_PARAM_KEYS
                )
                stack.append((item, child_resource))
        elif isinstance(current, (list, tuple)):
            marker = (id(current), in_resource)
            if marker in seen:
                continue
            seen.add(marker)
            stack.extend((item, in_resource) for item in current)
        elif isinstance(current, str):
            if in_resource:
                # 资源键上下文内的字符串本体即资源引用（N5：
                # files 等列表形态此前只被 scheme 扫描覆盖，裸
                # 路径漏检，Review P2-2/P2-3）；不分深度过路径
                # 语义分类（补审第二段 P2 + 第三段复审 P3 穿透：
                # 嵌套后代改走弱校验等于把逃逸下沉一层，不许）。
                try:
                    validate_path_semantic_ref(
                        current, allowed_roots=allowed_roots, base_dir=base_dir
                    )
                except ResourceValidationError:
                    return False
            for candidate in _SCHEME_REF_RE.findall(current):
                try:
                    validate_resource_ref(candidate, allowed_roots=allowed_roots)
                except ResourceValidationError:
                    return False
            for candidate in _cq_resource_candidates(current):
                # CQ 资源参数与 dict 资源键叶子同口径过路径语
                # 义分类（平价轮裁决：同一资源值不因 dict/CQ
                # 书写形态异判；R-S2-1 记载的既定修法在此落
                # 实）。任意 str 的裸 scheme:// 扫描是自由文本
                # URL 扫描、与 CQ 资源参数不是一类，维持
                # validate_resource_ref 不动。
                try:
                    validate_path_semantic_ref(
                        candidate, allowed_roots=allowed_roots, base_dir=base_dir
                    )
                except ResourceValidationError:
                    return False
    return True


def _check_download_params(canonical: str, params_dict: dict) -> bool:
    """download_file / upload_file_stream 的专属参数边界（补审第二段 P3）。

    download_file 的 ``url`` 决定平台进程去抓取的地址、``name``
    决定落盘位置，此前两者都不在任何资源键表内、网关零校验：
    url 过 SSRF 边界（validate_download_url），name 与
    upload_file_stream 的 ``filename`` 只许 basename 形态
    （validate_on_disk_name）。字段缺省（平台侧全可选）时不设闸。
    """
    if canonical == "download_file":
        url = params_dict.get("url")
        if isinstance(url, str) and url:
            try:
                validate_download_url(url)
            except ResourceValidationError:
                return False
        name = params_dict.get("name")
        if isinstance(name, str):
            try:
                validate_on_disk_name(name)
            except ResourceValidationError:
                return False
    elif canonical == "upload_file_stream":
        filename = params_dict.get("filename")
        if isinstance(filename, str):
            try:
                validate_on_disk_name(filename)
            except ResourceValidationError:
                return False
    return True


# ------------------------------------------------- N5 文件域审计/解析

#: 文件域（落盘审计与 ResourceResolver 接线的域口径）：登记表
#: domain ∈ {E, E2, F} 的 canonical 动作。
_FILE_DOMAINS = frozenset({"E", "E2", "F"})

#: 审计资源摘要与 resolver 校验共用的资源键（以 bundle schema
#: 证据字段为准：file/files/file_path/folder_path/url/base64/path/
#: file_id/fileset_id/image/video/thumb_path/chunk_data 等）。
_FILE_RESOURCE_KEYS = (
    "file",
    "files",
    "file_path",
    "folder_path",
    "url",
    "base64",
    "path",
    "file_id",
    "fileset_id",
    "file_set_id",
    "image",
    "video",
    "thumb_path",
    "chunk_data",
)


def _is_file_domain(action: str) -> bool:
    entry = REGISTRY_BY_NAME.get(action)
    return entry is not None and entry.domain in _FILE_DOMAINS


def _resource_digest(value: str) -> str:
    """资源值的审计摘要：sha256 前 16 位（与 resources.py 的 digest
    口径一致），再经 _audit_token 清洗后才可进审计行——原文（真实
    路径/签名 URL）永不落日志。"""
    # backslashreplace：孤立代理项等畸形 Unicode 不得让摘要计算在
    # 门禁之前抛错、绕过审计与 rejected 口径（Review P3-5）。
    digest = hashlib.sha256(value.encode("utf-8", "backslashreplace")).hexdigest()[:16]
    return _audit_token(digest)


def _file_audit_summary(params) -> str:
    """文件域审计行尾摘要：``resources=<digest,...> size=<n|none>``。

    从 params 的资源键取字符串值（列表逐元素）摘要化；声明大小取
    file_size / size 参数的 int 值，其余形态一律 none。无资源键时
    resources=none（格式恒定，便于审计解析）。
    """
    digests: list[str] = []
    size = None
    if isinstance(params, dict):
        for key in _FILE_RESOURCE_KEYS:
            if key not in params:
                continue
            value = params[key]
            values = value if isinstance(value, (list, tuple)) else (value,)
            for item in values:
                if isinstance(item, str) and item:
                    digests.append(_resource_digest(item))
        for size_key in ("file_size", "size"):
            candidate = params.get(size_key)
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                size = candidate
                break
    if len(digests) > 20:
        # 审计行体积上限：超长资源列表只记前 20 个摘要 + 余数。
        digests = [*digests[:20], f"+{len(digests) - 20}"]
    resources = ",".join(digests) if digests else "none"
    return f"resources={resources} size={size if size is not None else 'none'}"


# 内容审查提取层已迁至 companion.content_extract（与 services
# 投递层共用同一实现，禁止再在本模块维护第二份键集/提取逻辑）；
# 本模块经顶部导入保留私有别名，兼容 invoke 与既有测试引用点。
def redact_value(v):
    """递归脱敏：dict/list/tuple/str 逐层处理，敏感键名命中则值整体打码。"""
    if isinstance(v, dict):
        out = {}
        for key, item in v.items():
            key_text = str(key).lower()
            if any(part in key_text for part in _SENSITIVE_KEY_PARTS):
                out[key] = "<redacted>"
            else:
                out[key] = redact_value(item)
        return out
    if isinstance(v, list):
        return [redact_value(item) for item in v]
    if isinstance(v, tuple):
        return tuple(redact_value(item) for item in v)
    if isinstance(v, str):
        return redact_text(v)
    return v


# ------------------------------------------------------------- 参数校验表
# 仅 schema_status == verified 时强制（见 invoke 步骤 d）；未列者不设闸。

_PARAM_RULES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "get_msg": (("message_id",), ()),
    "get_forward_msg": (("id",), ()),
    "fetch_emoji_like": (("message_id",), ()),
    "get_group_member_info": (("group_id", "user_id"), ()),
    "get_group_files_by_folder": (("group_id", "folder_id"), ()),
    # N6 Review P2-1：E2 读族必填按 bundle schema 证据补齐（此前
    # 无规则、门面又用错字段名，错参只能到平台才 400）。
    "get_flash_file_list": (("fileset_id",), ()),
    "get_flash_file_url": (("fileset_id",), ()),
    "get_fileset_info": (("fileset_id",), ()),
    "get_fileset_id": (("share_code",), ()),
    "get_share_link": (("fileset_id",), ()),
    "get_group_file_url": (("group_id", "file_id"), ()),
    "get_image": (("file",), ()),
    "get_record": (("file",), ()),
    "get_file": (("file_id",), ()),
    "check_url_safely": (("url",), ()),
    "send_private_msg": (("user_id", "message"), ("group_id",)),
    "send_group_msg": (("group_id", "message"), ()),
    "set_friend_add_request": (("flag",), ()),
    # N2 消息形态：发送类动作的必填参数（目标二选一者由门面校验，
    # 网关只设已确知必填项）。
    "send_msg": (("message",), ()),
    "send_group_forward_msg": (("group_id", "messages"), ()),
    "send_private_forward_msg": (("user_id", "messages"), ()),
    "send_forward_msg": (("messages",), ()),
    "forward_friend_single_msg": (("message_id", "user_id"), ()),
    "forward_group_single_msg": (("message_id", "group_id"), ()),
    "send_group_ai_record": (("group_id", "character", "text"), ()),
    "send_like": (("user_id",), ()),
    "set_msg_emoji_like": (("message_id", "emoji_id"), ()),
    "friend_poke": (("user_id",), ()),
    "group_poke": (("group_id", "user_id"), ()),
    # N4 L2 群管理与关系动作（仅 schema verified 者在此设闸）。
    "delete_msg": (("message_id",), ()),
    ".handle_quick_operation": (("context", "operation"), ()),
    "set_group_kick": (("group_id", "user_id"), ()),
    "set_group_ban": (("group_id", "user_id"), ()),
    "set_group_whole_ban": (("group_id",), ()),
    "set_group_admin": (("group_id", "user_id"), ()),
    "set_group_card": (("group_id", "user_id"), ()),
    "set_group_name": (("group_id", "group_name"), ()),
    "set_group_special_title": (("group_id", "user_id"), ()),
    "set_group_leave": (("group_id",), ()),
    "set_group_portrait": (("group_id", "file"), ()),
    "set_group_add_request": (("flag", "sub_type"), ()),
    "set_essence_msg": (("message_id",), ()),
    "delete_essence_msg": (("message_id",), ()),
    "_send_group_notice": (("group_id", "content"), ()),
    "_del_group_notice": (("group_id", "notice_id"), ()),
    "delete_friend": (("user_id",), ()),
    "set_qq_avatar": (("file",), ()),
    "set_self_longnick": (("longnick",), ()),
    "send_group_sign": (("group_id",), ()),
    # N5 文件/媒体域（必填口径以 bundle payload schema 证据为准，
    # docs/napcat-schema-evidence-n5.md：optional=False 且无 schema
    # default 的字段才算必填——upload_file / file_retention 带
    # default 故不在此列；download_file 与三个 download_*_stream
    # 动作全部字段可选、无必填，不设条目；get_group_files_by_folder
    # 的 folder_id 维持既有更严口径不变，见 N5 阶段文档已知差异）。
    "upload_private_file": (("user_id", "file", "name"), ()),
    "upload_group_file": (("group_id", "file", "name"), ()),
    "delete_group_file": (("group_id", "file_id"), ()),
    "create_group_file_folder": (("group_id",), ()),
    "delete_group_folder": (("group_id",), ()),
    "trans_group_file": (("group_id", "file_id"), ()),
    "move_group_file": (
        ("group_id", "file_id", "current_parent_directory", "target_parent_directory"),
        (),
    ),
    "rename_group_file": (
        ("group_id", "file_id", "current_parent_directory", "new_name"),
        (),
    ),
    "create_flash_task": (("files",), ()),
    "send_flash_msg": (("fileset_id",), ()),
    "download_fileset": (("fileset_id",), ()),
    "send_online_file": (("user_id", "file_path"), ()),
    "send_online_folder": (("user_id", "folder_path"), ()),
    "receive_online_file": (("user_id", "msg_id", "element_id"), ()),
    "refuse_online_file": (("user_id", "msg_id", "element_id"), ()),
    "cancel_online_file": (("user_id", "msg_id"), ()),
    # N5 升 verified 后设闸：bundle 实证唯一必填 user_id（见 actions.py
    # get_online_file_msg 的 N5 裁决修正注记）。
    "get_online_file_msg": (("user_id",), ()),
    "upload_file_stream": (("stream_id",), ()),
    "upload_image_to_qun_album": (("group_id", "album_id", "album_name", "file"), ()),
    "upload_images_to_qun_album": (("group_id", "album_id", "album_name", "files"), ()),
    "upload_video_to_qun_album": (("group_id", "album_id", "album_name", "file"), ()),
    "set_group_album_media_like": (("group_id", "album_id", "batch_id"), ()),
    "cancel_group_album_media_like": (("group_id", "album_id", "batch_id"), ()),
    "del_group_album_media": (("group_id", "album_id", "lloc"), ()),
    "do_group_album_comment": (("group_id", "album_id", "content"), ()),
    # N6：L3 族必填口径以 bundle payload schema 证据为准
    # （docs/napcat-schema-evidence-n6.md）：get_cookies /
    # get_credentials 的 domain 为唯一必填；send_packet 的 cmd /
    # data 必填、rsp 带 schema 默认值不列必填。其余 L3 动作
    # payload 为空、不设条目。注：硬禁在步骤 c 先行——未解锁时
    # 本规则不达，解锁后才实际生效。
    "get_cookies": (("domain",), ()),
    "get_credentials": (("domain",), ()),
    "send_packet": (("cmd", "data"), ()),
}
for _group_action in (
    "get_group_info",
    "get_group_info_ex",
    "get_group_member_list",
    "get_group_honor_info",
    "get_group_shut_list",
    "get_group_at_all_remain",
    "get_essence_msg_list",
    "_get_group_notice",
    "get_group_file_system_info",
    "get_group_root_files",
):
    _PARAM_RULES[_group_action] = (("group_id",), ())
for _user_action in ("get_stranger_info", "nc_get_user_status", "get_friend_msg_history"):
    _PARAM_RULES[_user_action] = (("user_id",), ())
del _group_action, _user_action


def _allowed_principals_for(level: str | None) -> frozenset:
    if level in {"L0", "L1"}:
        return _ALL_PRINCIPALS
    if level == "L2":
        return _L2_PRINCIPALS
    if level == "L3":
        return _L3_PRINCIPALS
    return frozenset()


# ------------------------------------------------- L2 授权（方案 §5，N4）

#: 不可逆档：canonical 路径执行须持绑定确认令牌，confirmed 布尔
#: 标记不足以放行（方案 §5「不可逆档额外要求二次确认」的最强档）。
_L2_TOKEN_REQUIRED = frozenset(
    {
        "set_group_leave",
        "delete_friend",
        # N5 不可逆删除档：删群文件 / 删群文件夹 / 删相册媒体，
        # 与 set_group_leave/delete_friend 同口径（绑定令牌必备）。
        "delete_group_file",
        "delete_group_folder",
        "del_group_album_media",
    }
)

#: 须机器人为群管理员（或群主）才能执行的群管理动作（参数须带
#: group_id，角色才可复核）。set_essence_msg / delete_essence_msg /
#: set_group_add_request 不在此列：其参数只有 message_id / flag，
#: 不标识目标群，网关无从按群复核——三者的逐资源约束由授权上下
#: 文的 account 范围与调用方审计承担（见 derive_target 口径）。
_L2_ADMIN_ACTIONS = frozenset(
    {
        "set_group_kick",
        "set_group_kick_members",
        "set_group_ban",
        "set_group_whole_ban",
        "set_group_card",
        "set_group_name",
        "set_group_special_title",
        "set_group_portrait",
        "_send_group_notice",
        "_del_group_notice",
        "set_group_add_option",
        "set_group_member_invite_policy",
        "set_group_member_permissions",
        "set_group_new_member_history_visibility",
        "set_group_robot_add_option",
        "set_group_search",
        # N5 文件域删除：三者证据字段均含 group_id，可按群复核角色。
        # del_group_album_media 的 bundle 字段表为 group_id/album_id/
        # lloc（docs/napcat-schema-evidence-n5.md），含 group_id，
        # 故同入本集（不是只有令牌档）。
        "delete_group_file",
        "delete_group_folder",
        "del_group_album_media",
    }
)

_ROLE_RANK = {"member": 1, "admin": 2, "owner": 3}


def _required_role(action: str, params: dict) -> str | None:
    """动作在目标群要求的机器人最低角色；不要求时返回 None。

    set_group_admin 增删管理员须群主；set_group_leave 解散
    （is_dismiss）须群主、单纯退群须在群内；delete_msg 仅在带
    group_id（群消息）时须管理员，私聊撤回不设角色门。
    """
    if action == "set_group_admin":
        return "owner"
    if action == "set_group_leave":
        return "owner" if params.get("is_dismiss") else "member"
    if action == "delete_msg":
        return "admin" if params.get("group_id") is not None else None
    if action in _L2_ADMIN_ACTIONS:
        return "admin"
    return None


#: 每动作的授权目标派生口径（Claude Code Review N4 P1-1 修复）：
#: 目标只能由该动作语义上真实的目标参数派生，调用方注入的无关
#: group_id/user_id 不得改写目标——否则只持群/单用户范围授权的
#: 调用方能把 account 级动作（清缓存/改资料/删说说等）伪装成
#: group/user 目标越权执行。未声明的动作走通用链回退（group →
#: user → account），新增 L2 动作必须在此声明。
#: - "group"：严格取 params["group_id"]，缺失即 ("none", None)
#:   （covers 恒 False，fail closed）；
#: - "user"：严格取 params["user_id"]，缺失同上；
#: - "account"：恒账号级（参数内的目标键一律忽略）；
#: - "quick"：.handle_quick_operation 的目标藏在其 context 事件
#:   dict 内，下探一层。残余风险（Review P3-1）：context 由调用
#:   方提供、operation 的真实作用目标网关无从校验——该动作的
#:   授权宜用 account 范围，群/用户范围只作纵深防御的一层。
_TARGET_KIND_BY_ACTION: dict[str, str] = {
    **{
        name: "group"
        for name in (
            "set_group_kick",
            "set_group_kick_members",
            "set_group_ban",
            "set_group_whole_ban",
            "set_group_admin",
            "set_group_card",
            "set_group_name",
            "set_group_special_title",
            "set_group_leave",
            "set_group_portrait",
            "_send_group_notice",
            "_del_group_notice",
            "set_group_add_option",
            "set_group_member_invite_policy",
            "set_group_member_permissions",
            "set_group_new_member_history_visibility",
            "set_group_robot_add_option",
            "set_group_search",
            "set_group_remark",
            "send_group_sign",
            "set_group_todo",
            "complete_group_todo",
            "cancel_group_todo",
            # N5：文件域 L2 删除的目标均为其 group_id 参数。
            "delete_group_file",
            "delete_group_folder",
            "del_group_album_media",
        )
    },
    **{name: "user" for name in ("delete_friend", "set_friend_remark")},
    **{
        name: "account"
        for name in (
            "delete_msg",
            "set_essence_msg",
            "delete_essence_msg",
            "set_group_add_request",
            "set_friend_add_request",
            "set_doubt_friends_add_request",
            "delete_qzone_msg",
            "delete_custom_face",
            "set_qq_profile",
            "set_qq_avatar",
            "set_self_longnick",
            "clean_cache",
            "send_qzone_msg",
        )
    },
    # N6：L3 敏感动作恒账号级——授权目标只认 account 范围，参数里
    # 出现任何 group_id/user_id 也不得把目标派生降格为群/用户范围
    # （与 L2 的 P1-1 修复同一防注入原则，防解锁后借参数改写目标）。
    **{
        name: "account"
        for name in (
            "get_cookies",
            "get_csrf_token",
            "get_credentials",
            "get_clientkey",
            "nc_get_rkey",
            "get_rkey",
            "get_rkey_server",
            "send_packet",
        )
    },
    ".handle_quick_operation": "quick",
}


def derive_target(action, params) -> tuple[str, str | None]:
    """按动作声明从 params 派生授权目标：(kind, value)。

    声明口径见 _TARGET_KIND_BY_ACTION：group/user 严格取声明键、
    account 恒账号级、quick 下探 context；未声明动作回退通用链
    （group_id → user_id → account，context 同口径下探一层）。
    """
    if not isinstance(params, dict):
        params = {}
    kind = _TARGET_KIND_BY_ACTION.get(action)
    if kind == "group":
        group_id = params.get("group_id")
        return ("group", str(group_id)) if group_id is not None else ("none", None)
    if kind == "user":
        user_id = params.get("user_id")
        return ("user", str(user_id)) if user_id is not None else ("none", None)
    if kind == "account":
        return ("account", None)
    group_id = params.get("group_id")
    if group_id is None and isinstance(params.get("context"), dict):
        group_id = params["context"].get("group_id")
    if group_id is not None:
        return ("group", str(group_id))
    user_id = params.get("user_id")
    if user_id is None and isinstance(params.get("context"), dict):
        user_id = params["context"].get("user_id")
    if user_id is not None:
        return ("user", str(user_id))
    return ("account", None)


def target_label(kind: str, value: str | None) -> str:
    """目标的令牌绑定标签：group:<id> / user:<id> / account。"""
    return "account" if value is None else f"{kind}:{value}"


def params_digest(params) -> str:
    """参数摘要（确认令牌绑定用）：规范化 JSON 的 sha256。

    签发方与网关必须对同一 params dict 计算，结果才一致；值经
    ``default=str`` 兜底不可序列化对象。摘要只用于绑定校验，
    不落日志、不作审计字段。
    """
    payload = json.dumps(
        params if isinstance(params, dict) else {},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthorizationContext:
    """L2 逐资源授权上下文（方案 §5 三元组之一：对哪个资源有权）。

    由接线层（产品命令路径/运维 CLI）在调用前显式构造并注入：
    ``authorizer`` 为授权人标识（非空字符串），``groups`` /
    ``users`` 为允许的目标范围，``account`` 表示账号级动作获授。
    范围外目标在网关一律 authorization_scope 拒绝。主体（谁在
    请求）由 principal 回答，机器人角色（能否执行）由 role 复核
    回答，三者互不替代。
    """

    authorizer: str
    groups: frozenset = frozenset()
    users: frozenset = frozenset()
    account: bool = False

    def __post_init__(self):
        # 安全边界类型的构造期防御（补审第二段 P4）：范围字段若被
        # 误传 str/bytes，covers() 的 ``in`` 会退化为子串匹配——
        # 短 ID 可子串命中长 ID 的授权范围、无声扩权。本类型是授
        # 权链的信任根，构造期即拒绝该形态；其余可迭代集合一律
        # 归一为 frozenset[str]（元素统一 str 口径，与 derive_target
        # 的 str 化目标同口径）。authorizer 只做类型防御：空串的
        # 拒绝点在各消费门（网关 c2/c2b/c3 与 GatewayRequestChannel
        # 构造均以 authorization_missing / ValueError 处置，且有
        # 测试以空 authorizer 钉住该网关门）——构造期禁空会令该门
        # 不可达，故不在此重复设闸。
        if not isinstance(self.authorizer, str):
            raise TypeError("authorizer must be a string")
        for field_name in ("groups", "users"):
            value = getattr(self, field_name)
            if isinstance(value, (str, bytes)):
                raise TypeError(
                    f"{field_name} must be a collection of ids, not a string"
                )
            object.__setattr__(
                self, field_name, frozenset(str(item) for item in value)
            )

    def covers(self, kind: str, value: str | None) -> bool:
        if kind == "group":
            return value in self.groups
        if kind == "user":
            return value in self.users
        if kind == "account":
            return self.account
        return False  # "none"（声明目标缺失）等未知种类一律不覆盖


class ConfirmationTokenStore:
    """二次确认令牌存储（方案 §5：绑定动作+参数摘要+目标+有效期+单次）。

    ``issue`` 由接线层在取得用户/运维确认后签发（或经
    ``ActionGateway.issue_confirmation`` 统一签发，Review P3-6）。
    网关两段校验（Review P2-2）：``peek`` 在授权链处只校验绑定
    （绑定失配的既存记录即销，防探测），``consume`` 在全部派发
    前门禁、账本 try_begin 与派发点熔断 allow 全部通过后、真正
    派发前才消费——dry_run/角色失败/账本故障等非执行结果不烧
    单次 nonce（补审第二段 P3：消费点自 i3 后移至派发内层）。
    ``revoke_all`` 推进 epoch 并清空——权限变更后已签发令牌立即
    全部失效。进程内存储：进程重启即全失效（保守方向）。
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._epoch = 0
        self._records: dict[str, tuple] = {}

    def issue(
        self, *, action: str, target: str, params_digest: str, ttl_seconds: float = 300
    ) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._records[token] = (
                action,
                target,
                params_digest,
                self._clock() + ttl_seconds,
                self._epoch,
            )
        return token

    def _matches(self, record, *, action: str, target: str, params_digest: str) -> bool:
        rec_action, rec_target, rec_digest, expires_at, epoch = record
        return (
            epoch == self._epoch
            and self._clock() <= expires_at
            and rec_action == action
            and rec_target == target
            and rec_digest == params_digest
        )

    def peek(self, token, *, action: str, target: str, params_digest: str) -> bool:
        """只校验绑定不消费；既存记录绑定失配（含过期/撤权）即销。"""
        if not isinstance(token, str) or not token:
            return False
        with self._lock:
            record = self._records.get(token)
            if record is None:
                return False
            if not self._matches(
                record, action=action, target=target, params_digest=params_digest
            ):
                self._records.pop(token, None)
                return False
            return True

    def consume(self, token, *, action: str, target: str, params_digest: str) -> bool:
        if not isinstance(token, str) or not token:
            return False
        with self._lock:
            record = self._records.pop(token, None)
            if record is None:
                return False
            return self._matches(
                record, action=action, target=target, params_digest=params_digest
            )

    def revoke_all(self) -> None:
        with self._lock:
            self._epoch += 1
            self._records.clear()


# ------------------------------------------------------------------- Spec


@dataclass(frozen=True)
class ActionSpec:
    """一个可调用动作的门控规格（由 ACTION_REGISTRY 派生）。"""

    name: str
    policy_key: str
    tier: str | None
    level: str | None
    kind: str | None
    schema_status: str
    default_enabled: bool
    allowed_principals: frozenset
    required_params: tuple = ()
    forbidden_params: tuple = ()
    idempotent: bool = False
    requires_confirmation: bool = False
    legacy: bool = False
    allowed_policy_keys: frozenset = frozenset()
    #: canonical 路径默认 dry_run=true 的个别动作（登记级别之外
    #: 的保守默认，见 _DRY_DEFAULT_ACTIONS）。
    dry_default: bool = False


#: 个别动作的主体收紧（Review P2-4）：send_qzone_msg 登记级别为
#: L1，但其前身 publish_message 合成 spec 只许 L2 主体（system/
#: product_command/ops_cli）——对外发布动作不因迁移放宽到
#: llm_tool/scheduler，canonical 与 legacy 两条路径共用本 spec
#: 的 allowed_principals（legacy 客户端 _check 另按级别校验，
#: 两层取交集）。
_PRINCIPAL_OVERRIDES: dict[str, frozenset] = {
    "send_qzone_msg": _L2_PRINCIPALS,
}

#: canonical 路径默认 dry_run=true 的个别动作（Review P2-4）：
#: send_qzone_msg 须在 policy 显式写 dry_run=false 才真发，与
#: qzone 配置段历史上的保守默认对齐（legacy 路径不受此默认
#: 影响，其 dry_run 由客户端按 qzone 段显式传入）。
_DRY_DEFAULT_ACTIONS = frozenset({"send_qzone_msg"})


def _build_specs() -> dict[str, ActionSpec]:
    specs: dict[str, ActionSpec] = {}
    for entry in ACTION_REGISTRY:
        if entry.disposition != "registered" or entry.alias_of is not None:
            continue
        required, forbidden = _PARAM_RULES.get(entry.name, ((), ()))
        specs[entry.name] = ActionSpec(
            name=entry.name,
            policy_key=entry.name,
            tier=entry.tier,
            level=entry.level,
            kind=entry.kind,
            schema_status=entry.schema,
            default_enabled=entry.default_enabled,
            allowed_principals=_PRINCIPAL_OVERRIDES.get(
                entry.name, _allowed_principals_for(entry.level)
            ),
            required_params=tuple(required),
            forbidden_params=tuple(forbidden),
            idempotent=entry.kind == "query",
            requires_confirmation=entry.name == "set_friend_add_request",
            allowed_policy_keys=frozenset({entry.name})
            | _EXTRA_POLICY_KEYS.get(entry.name, frozenset()),
            dry_default=entry.name in _DRY_DEFAULT_ACTIONS,
        )
    # N4 处置：qzone 旧端点 publish_message 的 legacy 合成 spec 已
    # 下线——qzone_post 改调登记在册的 send_qzone_msg（policy_key
    # 仍为 qzone，见 _EXTRA_POLICY_KEYS），ACTION_SPECS 自此与登记
    # 表 canonical 条目严格一致、无合成项。
    return specs


ACTION_SPECS: dict[str, ActionSpec] = _build_specs()

_ALIASES: dict[str, str] = {
    entry.name: entry.alias_of
    for entry in ACTION_REGISTRY
    if entry.alias_of is not None and entry.disposition == "registered"
}


def resolve_canonical_name(action) -> str | None:
    """别名解析到 canonical 名；非字符串返回 None。"""
    if not isinstance(action, str):
        return None
    return _ALIASES.get(action, action)


def get_action_spec(action) -> ActionSpec | None:
    """按名（可为别名）查 spec；未登记/非 registered 返回 None。"""
    canonical = resolve_canonical_name(action)
    if canonical is None:
        return None
    return ACTION_SPECS.get(canonical)


def build_action_levels() -> Mapping[str, bool]:
    """§3.4 能力矩阵的 action_levels：由 ActionSpec 注册表生成。

    适配器实现面与网关收口面是同一集合（全部出站只经本网关），故
    「本实现是否提供该动作」即「该动作在 ACTION_SPECS 中」：canonical
    registered 动作名 → True；别名不单列（按 canonical 归一，与
    integrated_count 同口径），未提供的名字不入表、查询方以
    ``.get(name, False)`` 读。N6 落地（此前各阶段只落 segment_types
    与 event_kinds 两维，本维补齐后 §3.4 三维同源于登记表）。
    返回 MappingProxyType（Review P4-2）：能力声明挂在 frozen
    dataclass 上，不许原地改写。
    """
    return MappingProxyType({name: True for name in ACTION_SPECS})


# ------------------------------------------------------------------ 结果


@dataclass(frozen=True)
class ActionResult:
    """网关调用结果：门禁结果与派发后三态分层表达（方案 §3.2）。"""

    outcome: str  # ok / rejected / ambiguous / simulated
    data: object = None
    error_code: str | None = None
    gate: str | None = None
    platform_message_id: str | None = None
    raw: object = None
    operation_id: str | None = None


# ------------------------------------------------------------- operation 账


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    action: str
    state: str  # processing / done / ambiguous / aborted
    outcome: str | None = None
    gate: str | None = None
    created_at: float = 0.0
    finished_at: float | None = None


class InMemoryOperationLedger:
    """进程内 operation 登记表（测试与单进程部署用）。

    接口三件：lookup / try_begin / finish。try_begin 原子语义：无记录
    →建 processing 返回 True；已有 aborted→改回 processing 返回
    True；已有 processing/done/ambiguous→返回 False。本实现同步查改、
    无 await 间隙，单事件循环下即原子。
    """

    def __init__(self):
        self._records: dict[str, OperationRecord] = {}
        self._lock = threading.Lock()

    def lookup(self, operation_id: str) -> OperationRecord | None:
        with self._lock:
            return self._records.get(operation_id)

    def try_begin(self, operation_id: str, action: str) -> bool:
        with self._lock:
            existing = self._records.get(operation_id)
            if existing is not None and existing.state != "aborted":
                return False
            self._records[operation_id] = OperationRecord(
                operation_id=operation_id,
                action=action,
                state="processing",
                created_at=existing.created_at if existing else time.time(),
            )
            return True

    def finish(self, operation_id: str, state: str, outcome, gate) -> None:
        with self._lock:
            existing = self._records.get(operation_id)
            self._records[operation_id] = OperationRecord(
                operation_id=operation_id,
                action=existing.action if existing else "",
                state=state,
                outcome=outcome,
                gate=gate,
                created_at=existing.created_at if existing else time.time(),
                finished_at=time.time(),
            )


class SqliteOperationLedger:
    """落库版 operation 登记表：表 action_operations，与 claim 同库不同表。

    简单实现：单连接 + 锁，每次写以 BEGIN IMMEDIATE 串行（口径参考
    memory 层既有写法）。
    """

    def __init__(self, path):
        self.path = str(path)
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS action_operations ("
                    "operation_id TEXT PRIMARY KEY, action TEXT, state TEXT, "
                    "outcome TEXT, gate TEXT, created_at REAL, finished_at REAL)"
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def lookup(self, operation_id: str) -> OperationRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT operation_id, action, state, outcome, gate, "
                "created_at, finished_at FROM action_operations "
                "WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        return OperationRecord(
            operation_id=row[0],
            action=row[1],
            state=row[2],
            outcome=row[3],
            gate=row[4],
            created_at=row[5] or 0.0,
            finished_at=row[6],
        )

    def try_begin(self, operation_id: str, action: str) -> bool:
        """原子占位：INSERT 新记录，或 UPDATE...WHERE state='aborted'。

        主键冲突（IntegrityError）时仅当既有记录为 aborted 才改回
        processing；rowcount 判定成败，processing/done/ambiguous 一律
        返回 False（Review 修复 2）。
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                try:
                    self._conn.execute(
                        "INSERT INTO action_operations "
                        "(operation_id, action, state, outcome, gate, created_at, "
                        "finished_at) VALUES (?, ?, 'processing', NULL, NULL, ?, NULL)",
                        (operation_id, action, time.time()),
                    )
                    began = True
                except sqlite3.IntegrityError:
                    cursor = self._conn.execute(
                        "UPDATE action_operations SET action=?, "
                        "state='processing', outcome=NULL, gate=NULL, "
                        "finished_at=NULL "
                        "WHERE operation_id=? AND state='aborted'",
                        (action, operation_id),
                    )
                    began = cursor.rowcount > 0
                self._conn.execute("COMMIT")
                return began
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def finish(self, operation_id: str, state: str, outcome, gate) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    "UPDATE action_operations SET state=?, outcome=?, gate=?, "
                    "finished_at=? WHERE operation_id=?",
                    (state, outcome, gate, time.time(), operation_id),
                )
                if cursor.rowcount == 0:
                    self._conn.execute(
                        "INSERT INTO action_operations (operation_id, action, "
                        "state, outcome, gate, created_at, finished_at) "
                        "VALUES (?, '', ?, ?, ?, ?, ?)",
                        (operation_id, state, outcome, gate, time.time(), time.time()),
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ------------------------------------------------------------------ 限流


class _SlidingWindowLimiter:
    """默认限流器：与 tools.ToolRateLimiter 同语义（鸭子类型兼容）。"""

    def __init__(self):
        self.calls = defaultdict(deque)

    def allow(self, tool: str, limit: int, window: int, identity: str = "") -> bool:
        now = time.monotonic()
        bucket = self.calls[f"{tool}:{identity}"]
        while bucket and bucket[0] <= now - window:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


# ------------------------------------------------------------------ 网关


@dataclass
class _LogContext:
    action: str = ""
    policy_key: str = ""
    # N5：文件域动作的审计行尾资源摘要（非文件域恒为空串，审计行
    # 逐字不变）；由 _invoke_inner 在 spec 解析后按原始 params 计算。
    file_summary: str = ""


class ActionGateway:
    """唯一出站收口：门控顺序见模块 docstring 与方案 §3.1/§5。"""

    def __init__(
        self,
        transport,
        *,
        policies=None,
        quota=None,
        limiter=None,
        operation_ledger=None,
        resource_roots=(),
        resource_base_dir=None,
        role_resolver=None,
        role_cache_ttl: float = 60.0,
        confirmation_tokens: ConfirmationTokenStore | None = None,
        circuit_breaker=None,
        circuit_actions=None,
        resource_resolver=None,
        l3_unlocked=None,
    ):
        self.transport = transport
        # policies 原 dict 对象直接持有（窗口 B/调用方热更同一对象），不拷贝。
        self.policies = policies if policies is not None else {}
        self.quota = quota
        self.limiter = limiter if limiter is not None else _SlidingWindowLimiter()
        # 账本恒存在（Review 修复 2）：未注入时默认进程内账本，不再有
        # 「无账本」状态——显式 operation_id 的防重放对一切网关生效。
        self.operation_ledger = (
            operation_ledger if operation_ledger is not None else InMemoryOperationLedger()
        )
        self.resource_roots = tuple(resource_roots)
        # 相对资源路径的解析基准（补审第二段 P2）：接线层显式注入
        # 的专用目录，相对路径 realpath 后仍须落 resource_roots 内；
        # 未注入（None）时一切相对路径形态在步骤 d 被拒（fail
        # closed），不透明 file_id 与 URL 不受影响。
        self.resource_base_dir = (
            str(resource_base_dir) if resource_base_dir is not None else None
        )
        # L2 执行前角色复核（N4）：role_resolver 为 async callable
        # (group_id: str) -> "owner"/"admin"/"member"/None；未注入时
        # 需角色的 L2 动作一律 role_unverified fail closed。结果按
        # (group) 缓存 role_cache_ttl 秒；revoke_authorizations 清空。
        self.role_resolver = role_resolver
        self.role_cache_ttl = float(role_cache_ttl)
        self._role_cache: dict[str, tuple[str | None, float]] = {}
        # 确认令牌存储恒存在（与账本同口径）：签发/消费只经本 store。
        self.confirmation_tokens = (
            confirmation_tokens if confirmation_tokens is not None else ConfirmationTokenStore()
        )
        # N5 相册熔断：未注入时默认构造（threshold=3、cooldown=300s）；
        # 管控面未注入时由登记表 domain="F" 的 canonical 动作派生。
        # 熔断只拦派发与记账（见 _invoke_inner 的预检与 _dispatch），
        # 非 F 域动作不在 circuit_actions 内、行为逐字不变。
        self.circuit_breaker = (
            circuit_breaker
            if circuit_breaker is not None
            else ActionCircuitBreaker(threshold=3, cooldown_seconds=300)
        )
        self.circuit_actions = (
            frozenset(circuit_actions)
            if circuit_actions is not None
            else frozenset(
                entry.name
                for entry in ACTION_REGISTRY
                if entry.domain == "F"
                and entry.disposition == "registered"
                and entry.alias_of is None
            )
        )
        # N5 资源解析器接线：未注入（None）时文件域动作行为与现状
        # 逐字一致；注入时在步骤 d 对文件域资源做 resolve 校验。
        self.resource_resolver = resource_resolver
        # N6 L3 受控解锁名单（方案 §5）：构造期快照为 frozenset——
        # 名单来自运维配置（config 的 actions.l3_unlock，接线层在
        # 进程启动时注入），运行期修改来源集合/配置对象不影响本
        # 实例，换名单只能重建网关（即重启生效）。名单外的名字
        # （非 L3 / 未登记）在构造期即抛 ValueError（接线错误须在
        # 启动期暴露，不许静默吞掉一个写错的解锁项）。
        unlocked = frozenset(l3_unlocked or ())
        for name in unlocked:
            spec = ACTION_SPECS.get(name)
            if spec is None or spec.level != "L3":
                raise ValueError(f"l3_unlocked entry is not an L3 action: {name!r}")
        self._l3_unlocked = unlocked

    @property
    def l3_unlocked(self) -> frozenset:
        """本实例的 L3 解锁名单快照（只读；换名单须重建网关）。"""
        return self._l3_unlocked

    def revoke_authorizations(self) -> None:
        """权限变更收口：已签发确认令牌全部失效 + 角色缓存清空（§5）。

        目前无自动调用点：权限/角色变更的接线层（运维 CLI、未来的
        角色变更 notice 处理）在变更生效时必须显式调用（Review
        P3-7 文档化）；漏调的代价是已签发令牌活到自然过期（≤TTL）。
        """
        self.confirmation_tokens.revoke_all()
        self._role_cache.clear()

    def issue_confirmation(self, action, params, *, ttl_seconds: float = 300) -> str:
        """统一签发入口（Review P3-6）：按网关同一口径派生目标与
        参数摘要后签发绑定令牌——签发方不必自行复刻 derive_target
        / params_digest，杜绝两处口径漂移导致的静默校验失败。
        别名先解析到 canonical（与网关执行时的绑定一致）。"""
        canonical = resolve_canonical_name(action)
        if canonical is None or canonical not in ACTION_SPECS:
            raise ValueError(f"unknown action: {action!r}")
        kind, value = derive_target(canonical, params)
        return self.confirmation_tokens.issue(
            action=canonical,
            target=target_label(kind, value),
            params_digest=params_digest(params),
            ttl_seconds=ttl_seconds,
        )

    async def _resolve_role(self, group_id) -> str | None:
        """带 TTL 缓存的角色复核：解析失败/异常一律 None（fail closed）。

        只缓存成功解析到的角色（Review P2-1/P3-7）：失败不入缓存，
        瞬时故障不会把整段 TTL 都锁死为未核实；正向缓存的固有代价
        是机器人被撤权后最多一个 TTL 内仍按旧角色放行——平台是最
        终仲裁者，此处只作纵深防御，TTL 默认 60s。group_id 为空
        直接未核实，不拿 "None" 去查平台。
        """
        if group_id is None or group_id == "":
            return None
        key = str(group_id)
        now = time.monotonic()
        cached = self._role_cache.get(key)
        if cached is not None and now - cached[1] <= self.role_cache_ttl:
            return cached[0]
        if self.role_resolver is None:
            return None
        try:
            role = self.role_resolver(key)
            if inspect.isawaitable(role):
                role = await role
        except Exception:  # noqa: BLE001 — 复核通道故障即未核实
            return None
        if role not in _ROLE_RANK:
            return None
        self._role_cache[key] = (role, now)
        return role

    # -- 审计 -----------------------------------------------------------

    @staticmethod
    def _audit(
        principal, ctx: _LogContext, result: ActionResult, elapsed_ms: int,
        authorizer=None,
    ) -> None:
        # authorizer（Review P3-5）：L2 管理动作的审计须能回答
        # 「谁授权」，与 principal（谁在请求）并列记录，均经清洗。
        # N5：文件域动作（ctx.file_summary 非空）在行尾追加资源摘要
        # （digest + 声明大小，绝不含路径/URL 原文）；非文件域不加，
        # 审计行逐字不变。
        message = (
            "action principal=%s authorizer=%s action=%s policy_key=%s "
            "outcome=%s gate=%s elapsed_ms=%d"
        )
        args = [
            _audit_token(principal) if principal is not None else "none",
            _audit_token(authorizer) if authorizer is not None else "none",
            _audit_token(ctx.action),
            _audit_token(ctx.policy_key),
            result.outcome,
            result.gate or "none",
            elapsed_ms,
        ]
        if ctx.file_summary:
            message += " %s"
            args.append(ctx.file_summary)
        logger.info(message, *args)

    def _check_resolver_resources(self, params_dict: dict) -> bool:
        """注入 ResourceResolver 时对文件域资源值的显式解析校验。

        params 中资源键的字符串值（列表逐元素）逐个过 resolver
        .resolve：本地路径的根目录/扩展名、base64 的估算大小（
        chunk_data/base64 键的裸 base64 按 base64:// 形态估算）与
        声明大小（file_size/size）都在 resolver 口径内校验；任一
        ResourceValidationError 即 False（调用方回 resource_rejected）。
        """
        resolver = self.resource_resolver
        size_hint = None
        for size_key in ("file_size", "size"):
            candidate = params_dict.get(size_key)
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                size_hint = candidate
                break

        #: 路径语义键：值必须解析为本地路径/URL/base64，按不透明
        #: file_id 放行即绕过根目录边界（Review P2-3）。
        path_semantic_keys = {"file_path", "folder_path", "thumb_path"}

        def _resolve_one(key, value) -> bool:
            # fail closed（Review P2-3）：资源键下的 dict 不是合法
            # 资源形态、嵌套列表逐元素递归校验，不再跳过。
            if isinstance(value, dict):
                return False
            if isinstance(value, (list, tuple)):
                return all(_resolve_one(key, item) for item in value)
            if not isinstance(value, str) or not value:
                return True
            candidate = value
            if key in {"base64", "chunk_data"} and not candidate.lower().startswith(
                "base64://"
            ):
                candidate = "base64://" + candidate
            try:
                resolved = resolver.resolve(candidate, size_hint=size_hint)
            except ResourceValidationError:
                return False
            if key in path_semantic_keys and resolved.kind == ResourceKind.FILE_ID:
                return False
            return True

        for key in _FILE_RESOURCE_KEYS:
            if key not in params_dict:
                continue
            value = params_dict[key]
            values = value if isinstance(value, (list, tuple)) else (value,)
            for item in values:
                if not _resolve_one(key, item):
                    return False
        return True

    # -- 主入口 -----------------------------------------------------------

    async def invoke(
        self,
        action,
        params=None,
        *,
        principal=None,
        policy_key=None,
        dry_run=False,
        confirmed=False,
        identity="",
        priority="normal",
        rate_limit=6,
        window_seconds=60,
        quota_token=None,
        preflight=None,
        operation_id=None,
        authorization=None,
        confirmation_token=None,
    ) -> ActionResult:
        started = time.monotonic()
        ctx = _LogContext(
            action=action if isinstance(action, str) else "",
            policy_key=policy_key or (action if isinstance(action, str) else ""),
        )
        result = await self._invoke_inner(
            action,
            params,
            principal=principal,
            policy_key=policy_key,
            dry_run=dry_run,
            confirmed=confirmed,
            identity=identity,
            priority=priority,
            rate_limit=rate_limit,
            window_seconds=window_seconds,
            quota_token=quota_token,
            preflight=preflight,
            operation_id=operation_id,
            authorization=authorization,
            confirmation_token=confirmation_token,
            ctx=ctx,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        authorizer = (
            authorization.authorizer
            if isinstance(authorization, AuthorizationContext)
            else None
        )
        self._audit(principal, ctx, result, elapsed_ms, authorizer=authorizer)
        return result

    async def _invoke_inner(
        self,
        action,
        params,
        *,
        principal,
        policy_key,
        dry_run,
        confirmed,
        identity,
        priority,
        rate_limit,
        window_seconds,
        quota_token,
        preflight,
        operation_id,
        authorization,
        confirmation_token,
        ctx: _LogContext,
    ) -> ActionResult:
        # a. 别名解析 + spec 查表：未登记不可调用。
        spec = get_action_spec(action)
        if spec is None:
            return ActionResult(outcome="rejected", gate="unknown_action")
        canonical = spec.name
        ctx.action = canonical
        # N5 落盘审计：文件域动作的资源摘要在此按原始 params 计算
        # （无论后续门禁在哪一步拒绝，审计行都有摘要且不含原文）。
        if _is_file_domain(canonical):
            ctx.file_summary = _file_audit_summary(params)
        effective_policy_key = policy_key or spec.policy_key
        ctx.policy_key = effective_policy_key
        # canonical 路径 = 以动作自身 policy_key 调用（N4 的 L2 授权
        # 链只对 canonical 路径生效；friend_management/qzone 两条
        # legacy policy 路径沿用先例门控，其中 L2 的 legacy 路径另
        # 有授权下限 c2b，见模块 docstring）。
        canonical_path = effective_policy_key == canonical
        # critical 能力派生（补审第三段 P3）：critical 不是调用方可
        # 自报的优先级——只有系统主体的私聊固定话术面才派生为真：
        # canonical 必须是 send_private_msg 且 effective key 落在
        # 其额度别名集（QUOTA_POLICY_KEYS）内。合法来源只有两类，
        # 都是 system 主体、内容为固定话术：① 处理器危机/确认回复
        # （ingress 以 critical=True 经 DeliveryService 以
        # send_private_msg 键发出）；② 运维告警通道
        # （tools.send_alert_private_msg 以 alert_private_msg 键
        # 发出）。其余自报 critical（群消息、llm_tool 主体、其它
        # 动作）一律按 normal 处理：内容复核、限流与普通额度池
        # 照常适用，绝不花危机预留池。
        critical = (
            priority == "critical"
            and principal == PRINCIPAL_SYSTEM
            and canonical == "send_private_msg"
            and effective_policy_key in QUOTA_POLICY_KEYS
        )
        token_confirmed = False
        # peek 通过的令牌在此登记，到派发内层（账本 try_begin 与
        # 熔断 allow 之后、真正派发之前）才 consume（Review P2-2
        # + 补审第二段 P3：dry_run/角色失败/账本故障等非执行结果
        # 不烧 nonce）。
        pending_consume: tuple | None = None

        # a2. policy_key 别名白名单（Review 修复 1）：effective key 必须
        #     落在 spec 派生的 allowed_policy_keys 内，否则不派发——借
        #     另一动作的合法 key（如 get_status）调用已停用动作的后门
        #     在此关闭。
        if effective_policy_key not in spec.allowed_policy_keys:
            return ActionResult(outcome="rejected", gate="policy_key_forbidden")

        # b. principal：缺失/未知先判；L3 硬禁紧随其后（对一切合法主体
        #    一律 hard_banned，故 forbidden 判定置于 L3 之后，见步骤 c）。
        if principal is None:
            return ActionResult(outcome="rejected", gate="principal_missing")
        if principal not in PRINCIPALS:
            return ActionResult(outcome="rejected", gate="principal_unknown")

        # c. L3 硬禁（N1 起）与受控解锁（N6，方案 §5）：未在构造期
        #    解锁名单内的 L3 动作在参数校验之前一律 hard_banned，
        #    policies 写 enabled=True 也无效。名单内动作放行到下方
        #    主体判定（allowed_principals 只有 ops_cli，llm_tool 等
        #    其余主体解锁后仍 principal_forbidden）与 c3 授权段。
        l3_unlocked_here = spec.level == "L3" and canonical in self._l3_unlocked
        if spec.level == "L3" and not l3_unlocked_here:
            return ActionResult(outcome="rejected", gate="hard_banned")
        if principal not in spec.allowed_principals:
            return ActionResult(outcome="rejected", gate="principal_forbidden")

        # c3. L3 解锁后的授权段（N6）：与 L2 授权链同一机制——
        #     AuthorizationContext 必备且 authorizer 非空。目标在此
        #     结构性钉死为 account（Review P3-1）：不经 derive_target
        #     派生、与 _TARGET_KIND_BY_ACTION 的登记无关，注入
        #     group_id/user_id 参数无从把目标降格为群/用户范围，
        #     群/用户范围的上下文永远过不了本段。缺失/越界在参数
        #     校验之前拒绝，与一切 L3 拒绝一样落审计行（invoke
        #     出口统一记账）。
        if l3_unlocked_here:
            if not isinstance(authorization, AuthorizationContext) or not authorization.authorizer:
                return ActionResult(
                    outcome="rejected", gate="authorization_missing",
                    operation_id=operation_id,
                )
            if not authorization.covers("account", None):
                return ActionResult(
                    outcome="rejected", gate="authorization_scope",
                    operation_id=operation_id,
                )

        # c2. L2 授权链（N4，方案 §5；仅 canonical 路径）：授权上下文
        #     必备且目标在范围内；不可逆档另须绑定确认令牌（缺令牌
        #     回 simulated 待确认、令牌不匹配直接拒）；须确认档
        #     （set_friend_add_request）持有效令牌亦视为已确认（步骤
        #     h 以 token_confirmed 放行）。目标按动作声明派生
        #     （derive_target），只读原始 params，参数形态的权威
        #     校验仍在步骤 d。此处对令牌只 peek 不消费：消费推迟到
        #     全部派发前门禁通过之后（见 i2 之后，Review P2-2）。
        if spec.level == "L2" and canonical_path:
            if not isinstance(authorization, AuthorizationContext) or not authorization.authorizer:
                return ActionResult(
                    outcome="rejected", gate="authorization_missing",
                    operation_id=operation_id,
                )
            target_kind, target_value = derive_target(canonical, params)
            if not authorization.covers(target_kind, target_value):
                return ActionResult(
                    outcome="rejected", gate="authorization_scope",
                    operation_id=operation_id,
                )
            if confirmation_token is not None and (
                canonical in _L2_TOKEN_REQUIRED or spec.requires_confirmation
            ):
                digest = params_digest(params)
                label = target_label(target_kind, target_value)
                matched = self.confirmation_tokens.peek(
                    confirmation_token,
                    action=canonical,
                    target=label,
                    params_digest=digest,
                )
                if matched:
                    token_confirmed = True
                    pending_consume = (confirmation_token, canonical, label, digest)
                elif canonical in _L2_TOKEN_REQUIRED:
                    return ActionResult(
                        outcome="rejected", gate="confirmation_invalid",
                        operation_id=operation_id,
                    )
            if canonical in _L2_TOKEN_REQUIRED and not token_confirmed:
                return ActionResult(
                    outcome="simulated", gate="pending_confirmation",
                    operation_id=operation_id,
                )

        # c2b. L2 legacy policy 路径的授权下限（补审第二段 P3）：
        #      friend_management 先例此前整段跳过授权链——调用方自选
        #      policy_key + 自断 confirmed 即可处置任意好友申请，
        #      「逐资源授权」保证对该动作不成立。先例口径（policy
        #      显式配置 + confirmed 确认门，见步骤 f/h）保留，但授权
        #      下限与 canonical 路径对齐：AuthorizationContext 必备
        #      且覆盖按动作声明派生的目标（set_friend_add_request
        #      为 account 级）。例外不再能由调用方仅凭 invoke 参数
        #      自选开启——缺授权上下文在参数校验之前即拒绝。生产
        #      接线（HttpOneBotClient.manage_friend）在其垫片层显式
        #      注入上下文，端到端行为不变。
        if spec.level == "L2" and not canonical_path:
            if not isinstance(authorization, AuthorizationContext) or not authorization.authorizer:
                return ActionResult(
                    outcome="rejected", gate="authorization_missing",
                    operation_id=operation_id,
                )
            legacy_kind, legacy_value = derive_target(canonical, params)
            if not authorization.covers(legacy_kind, legacy_value):
                return ActionResult(
                    outcome="rejected", gate="authorization_scope",
                    operation_id=operation_id,
                )

        # d. 参数校验（仅 schema verified）+ 资源引用边界。
        if params is None:
            params_dict: dict = {}
        elif isinstance(params, dict):
            params_dict = dict(params)
        else:
            return ActionResult(outcome="rejected", gate="invalid_params")
        if spec.schema_status == "verified":
            for required in spec.required_params:
                if required not in params_dict or params_dict[required] is None:
                    return ActionResult(outcome="rejected", gate="invalid_params")
            for forbidden in spec.forbidden_params:
                if forbidden in params_dict:
                    return ActionResult(outcome="rejected", gate="invalid_params")
        # 资源校验递归化（Review 修复 4）：params 任意深度的资源键值、
        # scheme:// 候选与 CQ file= 值逐个校验，任一不过即拒绝。
        # 资源键值走路径语义分类（补审第二段 P2）：相对路径以
        # resource_base_dir 为基准解析后查 roots。
        if not _check_resource_refs(
            params_dict, self.resource_roots, self.resource_base_dir
        ):
            return ActionResult(outcome="rejected", gate="resource_rejected")
        # 下载族专属边界（补审第二段 P3）：download_file 的 url 过
        # SSRF 边界、name / upload_file_stream 的 filename 只许
        # basename——三者都不在资源键表内，此处单独设闸。
        if not _check_download_params(canonical, params_dict):
            return ActionResult(outcome="rejected", gate="resource_rejected")

        # d0. ResourceResolver 接线（N5）：仅当注入了 resolver 且
        #     动作属文件域（E/E2/F）时，对本地路径/base64 资源做
        #     resolver.resolve 校验（大小/扩展名/根目录）；未注入
        #     时本段不存在、行为与现状逐字一致。
        if self.resource_resolver is not None and _is_file_domain(canonical):
            if not self._check_resolver_resources(params_dict):
                return ActionResult(outcome="rejected", gate="resource_rejected")

        # d1. 熔断预检（N5，仅 circuit_actions 管控面，默认 F 域）：
        #     open 状态在此以 state() 只读预检拒绝（不消费 half-open
        #     试探名额、不烧确认令牌——试探的 allow() 记账在真正
        #     派发处 _dispatch 内）；告警行含动作名与熔断状态。
        if canonical in self.circuit_actions:
            circuit_state = self.circuit_breaker.state(canonical)
            if circuit_state == "open":
                logger.warning(
                    "circuit_open action=%s state=%s",
                    _audit_token(canonical),
                    circuit_state,
                )
                return ActionResult(outcome="rejected", gate="circuit_open")

        # d2. 出站内容复核（N2，Codex Review #2）：新消息动作
        #     （send_group_msg / send_msg / forward 系 / AI 语音 /
        #     ark 分享）的 message/messages/text 载荷在派发前过与纯
        #     文本同一套 review 规则——token 只证明额度预留，不证明
        #     内容已审；载荷形态（str/list/单个 dict/CQ 串）经
        #     _payload_visible_texts 统一提取，形态不构成绕过（补审
        #     第三段 P2）。send_private_msg 不在此列：其复核在回复
        #     链 OutputGuard 与 DeliveryService（语义冻结）；派生
        #     critical 通道的固定话术本身即受审产物，不重复拦截。
        if (
            spec.kind == "message"
            and canonical != "send_private_msg"
            and not critical
        ):
            payload_texts: list[str] = []
            unreviewable = False
            for payload_key in ("message", "messages", "text"):
                if payload_key in params_dict:
                    try:
                        payload_texts.extend(
                            _payload_visible_texts(params_dict[payload_key])
                        )
                    except (_PayloadUnreviewable, RecursionError):
                        # 提取不完（结构超深等）即 fail-closed
                        # 拦截（复审原理收口轮：超限即拦）；提取
                        # 层已立深度界，RecursionError 仅作兜底，
                        # 保证「每次 invoke 必返 ActionResult」。
                        unreviewable = True
            if unreviewable:
                return ActionResult(
                    outcome="rejected", gate="content_review_blocked"
                )
            if payload_texts:
                allowed, _reason = review_bubbles(
                    payload_texts, user_text="", enforce_size=False
                )
                if not allowed:
                    return ActionResult(
                        outcome="rejected", gate="content_review_blocked"
                    )
                # 拼接整串送审（结构兜底，顺序/连续性收口轮）：
                # 内容审查的判定对象最终必须是「按渲染顺序拼
                # 成的完整文本流」本身，而不只是片段列表——提
                # 取层任何残余的排序/分片差都不许再让跨界拆分
                # 短语逃审。拼接约定为直接相接（无分隔符）：渲
                # 染时相邻的文本在阅读流里就是相接的，分隔符
                # 只会给拆分短语留出隔断面。整串以单条再过一
                # 遍同一套 review：良性拼接串照常放行（本步只
                # 增审查口径、不改派发路径，良性多片段载荷恰
                # 一次派发有钉子钉死）。
                if len(payload_texts) > 1:
                    joined_stream = "".join(payload_texts)
                    allowed, _reason = review_bubbles(
                        [joined_stream], user_text="", enforce_size=False
                    )
                    if not allowed:
                        return ActionResult(
                            outcome="rejected", gate="content_review_blocked"
                        )

        # d2b. 管理类文本载荷复核（Review P3-8，部分接受）：非
        #      message 动作的顶层文本载荷（群公告 content、qzone
        #      的 message 等对外文本）同样过出站复核；群名/名片/
        #      头衔等短标签由持授权的可信主体设置，不在此列（已
        #      文档化的接受残余：其风险由授权链与审计承担）。
        if spec.kind != "message":
            extra_texts = [
                params_dict[key]
                for key in ("content", "message")
                if isinstance(params_dict.get(key), str) and params_dict[key]
            ]
            if extra_texts:
                allowed, _reason = review_bubbles(
                    extra_texts, user_text="", enforce_size=False
                )
                if not allowed:
                    return ActionResult(
                        outcome="rejected", gate="content_review_blocked"
                    )

        # e. operation 账本（仅 message/mutation；Review 修复 2）：
        #    显式 operation_id 先 lookup——done 回记录 outcome 不派发，
        #    ambiguous/processing 一律 replay_blocked，aborted 放行到
        #    静态门禁后的 try_begin。账本恒存在（__init__ 默认进程内
        #    账本）；lookup 抛错 fail closed。
        needs_ledger = spec.kind in _LEDGER_KINDS
        op_id = operation_id
        explicit_op = op_id is not None
        if needs_ledger:
            if op_id is None:
                op_id = uuid.uuid4().hex
            elif explicit_op:
                try:
                    record = self.operation_ledger.lookup(op_id)
                except Exception:  # noqa: BLE001 — 账本不可信即不派发
                    return ActionResult(
                        outcome="rejected", gate="ledger_unavailable", operation_id=op_id
                    )
                if record is not None:
                    if record.state == "done":
                        return ActionResult(
                            outcome=record.outcome or "rejected",
                            gate=None,
                            raw=None,
                            operation_id=op_id,
                        )
                    if record.state in ("ambiguous", "processing"):
                        # 结果不明或在途（含崩溃遗留）：禁止自动重放。
                        return ActionResult(
                            outcome="rejected", gate="replay_blocked", operation_id=op_id
                        )
                    # aborted：此前尝试死在派发前门禁，同 id 可重试，
                    # 继续走静态门禁，try_begin 时原子改回 processing。

        def _result(outcome, gate, **kwargs) -> ActionResult:
            return ActionResult(outcome=outcome, gate=gate, operation_id=op_id, **kwargs)

        def _finish_ledger(state: str, outcome, gate) -> None:
            """落账收尾：finish 抛错只记 warning，不改已派发/已判定结果。"""
            try:
                self.operation_ledger.finish(op_id, state, outcome, gate)
            except Exception:  # noqa: BLE001 — 落账失败不改写结果
                logger.warning("operation ledger finish failed op=%s", op_id)

        def _abort_ledger(result: ActionResult) -> ActionResult:
            """额度段 gate 失败收场：finish(aborted) 后原样返回，容许重试。"""
            _finish_ledger("aborted", result.outcome, result.gate)
            return result

        # f. policy 开关。
        policy = self.policies.get(effective_policy_key, {})
        if not isinstance(policy, dict):
            policy = {}
        if effective_policy_key in _NOT_CONFIGURED_POLICY_KEYS and "enabled" not in policy:
            return _result("rejected", "not_configured")
        # N6：已解锁 L3 的启用决定就是解锁名单本身（default_enabled
        # 恒 False 是硬禁默认，不二次否决名单内动作）；policy 显式
        # 写 enabled=False 仍可单独关停某个已解锁动作。
        enabled = policy.get("enabled", spec.default_enabled or l3_unlocked_here)
        if not enabled:
            return _result("rejected", "disabled")

        # g. 限流（派生 critical 豁免；limit/window 取 policy 覆盖优先）。
        if not critical and self.limiter is not None:
            limit = int(policy.get("rate_limit", rate_limit))
            window = int(policy.get("window_seconds", window_seconds))
            if not self.limiter.allow(effective_policy_key, limit, window, identity):
                return _result("rejected", "rate_limited")

        # h. 确认门：未确认只模拟，绝不派发、绝不映射 ok。N4 起
        #    canonical 路径持有效绑定令牌（token_confirmed）亦算确认。
        if (spec.requires_confirmation or effective_policy_key == "friend_management") and (
            not confirmed and not token_confirmed
        ):
            return _result("simulated", "pending_confirmation")

        # i. dry_run：同为 simulated。N4 起 L2 canonical 路径默认
        #    dry_run=true（方案 §5）：policy 须显式写 dry_run=false
        #    才真执行；个别动作（spec.dry_default，如 send_qzone_msg）
        #    在 canonical 路径同样默认 true（Review P2-4）；legacy
        #    路径与 L0/L1 维持默认假的旧口径。
        dry_default = canonical_path and (spec.level == "L2" or spec.dry_default)
        if dry_run or policy.get("dry_run", dry_default):
            return _result("simulated", "dry_run")

        # i2. L2 角色复核（N4，方案 §5 三元组之三）：群管理动作在
        #     真执行前复核机器人自身在目标群的角色（resolver 经 L0
        #     查询，网关侧带 TTL 缓存）；无 resolver / 查不到 / 角色
        #     不足一律 fail closed，绝不先派发再说。
        if spec.level == "L2":
            required_role = _required_role(canonical, params_dict)
            if required_role is not None:
                role = await self._resolve_role(params_dict.get("group_id"))
                if role is None:
                    return _result("rejected", "role_unverified")
                if _ROLE_RANK[role] < _ROLE_RANK[required_role]:
                    return _result("rejected", "role_insufficient")

        # i2b. 熔断消费前复检（N5 Review P2-1）：d1 预检之后仍有
        #      await（角色复核等），电路可能在此期间打开——open
        #      时在令牌消费与账本占位之前拒绝，避免白烧一次性
        #      令牌。half-open 不在此拒（试探名额归派发点 allow()）。
        if canonical in self.circuit_actions and (
            self.circuit_breaker.state(canonical) == "open"
        ):
            logger.warning(
                "circuit_open action=%s state=%s",
                _audit_token(canonical),
                self.circuit_breaker.state(canonical),
            )
            return _result("rejected", "circuit_open")

        # j0. try_begin 原子占位（Review 修复 2）：全部静态门禁通过
        #     之后、额度段之前。返回 False 说明并发同 id 已占位——再
        #     lookup 一次按状态表处置（done 回记录、其余/无记录一律
        #     replay_blocked）；抛错 fail closed。
        if needs_ledger:
            try:
                began = self.operation_ledger.try_begin(op_id, canonical)
            except Exception:  # noqa: BLE001 — 账本不可信即不派发
                return _result("rejected", "ledger_unavailable")
            if not began:
                try:
                    record = self.operation_ledger.lookup(op_id)
                except Exception:  # noqa: BLE001
                    return _result("rejected", "ledger_unavailable")
                if record is not None and record.state == "done":
                    return ActionResult(
                        outcome=record.outcome or "rejected",
                        gate=None,
                        raw=None,
                        operation_id=op_id,
                    )
                return _result("rejected", "replay_blocked")

        # k 派发（账本 finish 在派发后落 done/ambiguous）；j 额度路径
        # 在外层包住派发，额度 gate 失败先 finish(aborted) 再返回。
        async def _dispatch() -> ActionResult:
            # 熔断派发记账（N5）：allow() 只在此（真正派发点）调用，
            # half-open 试探名额只被真实派发消费；记账只认派发后
            # 结果——ok 记成功，其余（平台 rejected / ambiguous，
            # 含传输异常在 _call_transport 内归一的 ambiguous）记
            # 失败；一切网关门禁拒绝都在派发前返回、不经此路、不计。
            my_probe = None
            if canonical in self.circuit_actions:
                if not self.circuit_breaker.allow(canonical):
                    logger.warning(
                        "circuit_open action=%s state=%s",
                        _audit_token(canonical),
                        self.circuit_breaker.state(canonical),
                    )
                    result = _result("rejected", "circuit_open")
                    if needs_ledger:
                        # 派发点熔断拒绝属本地门禁：账本记 aborted
                        # 容许同 operation_id 在电路恢复后重试
                        # （Review P2-1），不记 done。确认令牌的
                        # consume 在本分支之后（见下），此路返回不
                        # 烧令牌，签发方无须重新签发（补审第二段
                        # P3：消费点后移消除了原 i3 的竞态烧令牌）。
                        return _abort_ledger(result)
                    return result
                my_probe = self.circuit_breaker.probe_generation(canonical)
            # 确认令牌消费（Review P2-2 + 补审第二段 P3）：消费点在
            # 一切派发前门禁（授权/参数/限流/确认/dry_run/角色/熔断
            # 预检）、账本 try_begin（j0）与派发点熔断 allow() 全部
            # 通过之后、真正派发之前的最后一刻——单次 nonce 只被
            # 真实执行尝试消耗；账本故障/同 id 竞态/派发点熔断打开
            # 都在此点之前退出，令牌完好可复用。并发同令牌只有一个
            # consume 能成功（store 锁内原子），另一个在此被拒、
            # 账本记 aborted 容许其换新令牌后重试。
            if pending_consume is not None:
                token_text, token_action, token_target, token_digest = pending_consume
                if not self.confirmation_tokens.consume(
                    token_text,
                    action=token_action,
                    target=token_target,
                    params_digest=token_digest,
                ):
                    if my_probe is not None:
                        # 已占的 half-open 试探名额必须结清，否则
                        # probing 悬挂（与下方取消路径同口径）。
                        self.circuit_breaker.abandon_probe(
                            canonical, probe_gen=my_probe
                        )
                    result = _result("rejected", "confirmation_invalid")
                    if needs_ledger:
                        return _abort_ledger(result)
                    return result
            try:
                result = await self._call_transport(canonical, params_dict, op_id)
            except BaseException:
                # 取消/超时以 CancelledError（BaseException）穿透
                # _call_transport 的 Exception 归一：持有的试探
                # 名额必须在此结清，否则 probing 永久悬挂、动作
                # 被熔断死锁（Review P1-1）。原异常继续上抛。
                if my_probe is not None:
                    self.circuit_breaker.abandon_probe(
                        canonical, probe_gen=my_probe
                    )
                raise
            if canonical in self.circuit_actions:
                # 世代口径（Review P3-4）：本派发持有试探则以其
                # 世代记账；未持有而他人试探在飞时，本结果是迟到
                # 结果，以永不匹配的世代号（-1）记账，熔断器忽略
                # 其对试探的影响；无试探在飞则常规记账（None）。
                current_probe = self.circuit_breaker.probe_generation(canonical)
                if my_probe is not None:
                    gen_arg = my_probe
                elif current_probe is not None:
                    gen_arg = -1
                else:
                    gen_arg = None
                if result.outcome == "ok":
                    self.circuit_breaker.record_success(canonical, probe_gen=gen_arg)
                else:
                    self.circuit_breaker.record_failure(canonical, probe_gen=gen_arg)
            if needs_ledger:
                state = "ambiguous" if result.outcome == "ambiguous" else "done"
                _finish_ledger(state, result.outcome, result.gate)
            return result

        # j. 额度（逐行对齐现 ToolGateway.invoke 的额度段语义）。
        #    send_private_msg 触发双重派生（Review 修复 1）：canonical
        #    动作必须是 send_private_msg 且 effective key 落在其额度
        #    别名集内，不再单看调用方给的 key；无 token 时网关按
        #    identity 自预留（N1 语义，冻结）。
        #    其余 message 类动作（N2）：effective key 必须等于 canonical。
        #    有 token 时校验且 token 的计费域与目标派生域必须一致
        #    （Review #6：user 域预留不得冒充群发）；无 token 时网关
        #    按参数派生目标自行预留（Review #3：缺 token 不再等于
        #    免记账），目标不可派生时 fail closed。两级额度的扣减
        #    统一发生在 SendQuota.reserve（子桶与账号总池原子扣减）。
        is_private_send = (
            canonical == "send_private_msg"
            and effective_policy_key in QUOTA_POLICY_KEYS
        )
        # get_mini_app_ark 虽登记 kind=message，但只生成卡片数据、
        # 不投递给任何目标，不计发送额度。
        is_new_message = (
            spec.kind == "message"
            and canonical not in {"send_private_msg", "get_mini_app_ark"}
            and effective_policy_key == canonical
        )
        quota_path = self.quota is not None and (is_private_send or is_new_message)
        if quota_path:
            quota = self.quota
            if is_new_message:
                if params_dict.get("group_id") is not None and (
                    params_dict.get("user_id") is None
                ):
                    quota_identity = f"g:{params_dict['group_id']}"
                    quota_scope = "group"
                elif params_dict.get("user_id") is not None:
                    quota_identity = str(params_dict["user_id"])
                    quota_scope = "user"
                elif identity:
                    quota_identity = identity
                    quota_scope = "user"
                else:
                    return _abort_ledger(_result("rejected", "quota_unavailable"))
            else:
                quota_identity = identity or "anonymous"
                quota_scope = None
            if quota_token is not None:
                try:
                    accepted = quota.accept_token(
                        quota_token,
                        critical=critical,
                        identity=quota_identity,
                        scope=quota_scope,
                    )
                except Exception:  # noqa: BLE001 — 记账不可信 fail closed
                    accepted = False
                if not accepted:
                    return _abort_ledger(_result("rejected", "quota_unavailable"))
            else:
                try:
                    reservation = quota.reserve(
                        quota_identity,
                        1,
                        critical=critical,
                        scope=quota_scope or "user",
                    )
                except Exception:  # noqa: BLE001 — accounting must fail closed
                    return _abort_ledger(_result("rejected", "quota_unavailable"))
                if reservation is None:
                    return _abort_ledger(_result("rejected", "rate_limited"))
                try:
                    accepted = quota.accept_token(
                        reservation.consume(),
                        critical=critical,
                        identity=quota_identity,
                        scope=quota_scope,
                    )
                except Exception:  # noqa: BLE001
                    accepted = False
                if not accepted:
                    return _abort_ledger(_result("rejected", "quota_unavailable"))
            try:
                slot_ok = quota.acquire_slot(critical)
                if inspect.isawaitable(slot_ok):
                    slot_ok = await slot_ok
            except Exception:  # noqa: BLE001
                slot_ok = False
            if not slot_ok:
                return _abort_ledger(_result("rejected", "slot_busy"))
            try:
                if preflight is not None:
                    try:
                        verdict = preflight()
                        if inspect.isawaitable(verdict):
                            verdict = await verdict
                    except Exception:  # noqa: BLE001 — 复查故障 fail closed
                        verdict = False
                    if not verdict:
                        return _abort_ledger(_result("rejected", "quota_unavailable"))
                return await _dispatch()
            finally:
                try:
                    quota.release_slot(critical)
                except Exception:  # noqa: BLE001
                    pass

        return await _dispatch()

    async def _call_transport(self, canonical: str, params_dict: dict, op_id) -> ActionResult:
        try:
            response = await self.transport.post_action(canonical, params_dict)
        except TransportError:
            return ActionResult(
                outcome="ambiguous", error_code="transport", operation_id=op_id
            )
        except Exception:  # noqa: BLE001 — 替身/异常一律结果不明，不猜 ok
            return ActionResult(
                outcome="ambiguous", error_code="transport", operation_id=op_id
            )
        if not isinstance(response, dict):
            return ActionResult(
                outcome="ambiguous", error_code="transport", operation_id=op_id
            )
        status = response.get("status")
        if status in {"ok", "async"}:
            data = response.get("data")
            return ActionResult(
                outcome="ok",
                data=data,
                raw=response,
                platform_message_id=extract_platform_message_id(response),
                operation_id=op_id,
            )
        retcode = response.get("retcode")
        if retcode is not None:
            error_code = _truncate_redacted(retcode)
        elif isinstance(status, str):
            error_code = _truncate_redacted(status)
        else:
            error_code = None
        return ActionResult(
            outcome="rejected", error_code=error_code, raw=response, operation_id=op_id
        )


# 兼容别名：账本协议的简短引用（窗口 B/测试可用任一名）。
OperationLedger = InMemoryOperationLedger


def _id_value(value):
    """数字型 ID 转 int（OneBot 惯例，与 tools._numeric_id 同口径）。"""
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return value


def make_gateway_role_resolver(gateway: ActionGateway, *, principal: str = PRINCIPAL_SYSTEM):
    """生产用角色解析器：经网关自身 L0 查询复核机器人群内角色。

    先 get_login_info 取机器人 user_id（进程内缓存一次），再
    get_group_member_info 查其在目标群的 role（owner/admin/member）。
    任一查询非 ok 或数据形态不符返回 None（网关按 role_unverified
    fail closed）。解析器只发 L0 查询，不产生任何管理动作。查询
    用专用 identity（role:<group>）与放宽的 rate_limit（Review
    P2-1）：角色复核是内部元查询，不能与普通 L0 调用共享默认
    6 次/60s 桶，否则多群场景下复核会被自身限流打成未核实。
    """
    state: dict[str, str | None] = {"self_id": None}

    async def resolver(group_id: str) -> str | None:
        if state["self_id"] is None:
            info = await gateway.invoke(
                "get_login_info", {},
                principal=principal, identity="role:self", rate_limit=60,
            )
            if info.outcome != "ok" or not isinstance(info.data, dict):
                return None
            user_id = info.data.get("user_id")
            if user_id is None:
                return None
            state["self_id"] = str(user_id)
        result = await gateway.invoke(
            "get_group_member_info",
            {"group_id": _id_value(group_id), "user_id": _id_value(state["self_id"])},
            principal=principal, identity=f"role:{group_id}", rate_limit=60,
        )
        if result.outcome != "ok" or not isinstance(result.data, dict):
            return None
        role = result.data.get("role")
        return role if role in _ROLE_RANK else None

    return resolver


__all__ = [
    "ACTION_SPECS",
    "PRINCIPALS",
    "PRINCIPAL_LLM_TOOL",
    "PRINCIPAL_OPS_CLI",
    "PRINCIPAL_PRODUCT_COMMAND",
    "PRINCIPAL_SCHEDULER",
    "PRINCIPAL_SYSTEM",
    "ActionGateway",
    "ActionResult",
    "ActionSpec",
    "AuthorizationContext",
    "ConfirmationTokenStore",
    "InMemoryOperationLedger",
    "OperationLedger",
    "OperationRecord",
    "SqliteOperationLedger",
    "build_action_levels",
    "derive_target",
    "get_action_spec",
    "make_gateway_role_resolver",
    "params_digest",
    "redact_text",
    "redact_value",
    "resolve_canonical_name",
    "target_label",
]
