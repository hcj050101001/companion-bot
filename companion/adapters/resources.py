"""资源引用契约（方案 C18，N1 基础版 → N5 完整版）。

两层口径，互不替代：

- ``validate_resource_ref``（N1 冻结语义，向后兼容）：纯校验函数，
  http/https URL 放行；带其他 scheme 的引用（含 ``file://`` 与
  ``base64://``）一律拒绝；绝对本地路径必须 realpath 后落在
  allowed_roots 之一内（roots 为空则拒绝一切绝对路径）；其余不透明
  字符串原样放行。Gateway 的 ``_check_resource_refs`` 依赖此口径
  （任意 ``scheme://`` 候选都过本函数），故本函数的拒绝面不得放宽。
- ``ResourceResolver`` / ``ResolvedResource``（N5 新增）：把引用分类
  为 url / local_path / base64 / file_id 四类并产出带摘要、大小、
  敏感度与绑定信息的结构化结果，供文件域动作在派发前显式解析。
  ``file://`` 与 ``base64://`` 只在 resolver 口径下合法，绝不回流
  到 ``validate_resource_ref``。
- ``validate_path_semantic_ref``（补审第二段 P2）：Gateway 步骤 d
  对资源键值的路径语义分类——带路径形态（含分隔符或为 ``.`` /
  ``..``）的值一律按本地路径处理：绝对路径 realpath 后查 roots；
  相对路径以网关显式注入的 ``base_dir`` 解析为 realpath 后查
  roots，界外、或无 base 可解析时一律拒绝。不透明 file_id 与
  http(s) URL 维持 ``validate_resource_ref`` 口径，该函数本身的
  N1 冻结语义不变（自由文本中的 scheme/CQ 候选仍只走它）。
- ``validate_download_url`` / ``validate_on_disk_name``（补审第二
  段 P3）：download_file 的 SSRF 边界（校验前先做 WHATWG 主机名
  归一，字面 IP 含非规范形式一律过与 vision 取图共享的
  ``_is_public_ip``，localhost 与内网后缀主机名拒绝）与落盘
  文件名的 basename 校验，供 Gateway 步骤 d 对下载族参数调用。

审计纪律：``ResolvedResource.digest`` 是 ref 的 sha256 前 16 位，
日志/审计只记 digest，永不落 ref 原文（签名 URL 含凭据）。
"""

import hashlib
import ipaddress
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import parse_qsl, urlparse


class ResourceValidationError(ValueError):
    """资源引用未过访问边界校验。"""


class ResourceKind(str, Enum):
    """资源引用四分类（方案 C18）。"""

    URL = "url"
    LOCAL_PATH = "local_path"
    BASE64 = "base64"
    FILE_ID = "file_id"


# 兼容常量别名（与枚举值同字符串）。
KIND_URL = ResourceKind.URL.value
KIND_LOCAL_PATH = ResourceKind.LOCAL_PATH.value
KIND_BASE64 = ResourceKind.BASE64.value
KIND_FILE_ID = ResourceKind.FILE_ID.value

SENSITIVITY_NORMAL = "normal"
SENSITIVITY_SIGNED_URL = "signed_url"

#: URL 查询参数名命中任一即标 signed_url（签名/凭据类参数）。
_SIGNED_PARAM_NAMES = frozenset(
    {
        "sign",
        "signature",
        "token",
        "access_token",
        "key",
        "api_key",
        "apikey",
        "clientkey",
        "secret",
        "credential",
        "credentials",
        "auth",
        "authorization",
        "expires",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "x-goog-signature",
        "x-goog-credential",
    }
)


def _digest_of(ref: str) -> str:
    """ref 的 sha256 前 16 位（仅供日志/审计，永不落原文）。"""
    return hashlib.sha256(ref.encode("utf-8")).hexdigest()[:16]


def _is_within_roots(real: str, allowed_roots) -> bool:
    for root in allowed_roots:
        root_real = os.path.realpath(str(root))
        if real == root_real or real.startswith(root_real + os.sep):
            return True
    return False


def _extract_ext(name_or_path: str) -> str | None:
    """从路径/URL 路径段提取小写扩展名（不含点）；无扩展名返回 None。"""
    base = name_or_path.split("?", 1)[0].split("#", 1)[0]
    base = base.rstrip("/").rsplit("/", 1)[-1]
    if "." not in base:
        return None
    ext = base.rsplit(".", 1)[-1].lower()
    if not ext or len(ext) > 10 or not ext.isalnum():
        return None
    return ext


def _normalize_exts(allowed_exts) -> frozenset | None:
    if allowed_exts is None:
        return None
    return frozenset(str(e).lower().lstrip(".") for e in allowed_exts)


def _url_sensitivity(ref: str) -> str:
    try:
        query = urlparse(ref).query
        pairs = parse_qsl(query, keep_blank_values=True)
    except ValueError:
        return SENSITIVITY_NORMAL
    for name, _value in pairs:
        lowered = name.lower()
        if lowered in _SIGNED_PARAM_NAMES or "sign" in lowered or lowered.endswith("_key"):
            return SENSITIVITY_SIGNED_URL
    return SENSITIVITY_NORMAL


@dataclass(frozen=True)
class ResolvedResource:
    """resolver 的结构化产出（frozen）。

    - ``ref``：原样引用（仅内存流转，禁止落日志）。
    - ``digest``：ref 的 sha256 前 16 位，供日志/审计。
    - ``size_hint``：已知大小（调用方传入，或 base64 的估算解码
      大小）；未知为 None。
    - ``media_ext``：小写扩展名（不含点），无法判定时 None。
    - ``sensitivity``：``"normal"`` / ``"signed_url"``。
    - ``bound_self_id`` / ``bound_session``：resolve 时的绑定上下文，
      未绑定为 None。
    - ``expires_at``：绑定过期时刻，时钟与 resolver 的 ``clock``
      一致（默认 ``time.monotonic``）；仅当 resolver 设了
      ``binding_ttl`` 且本次 resolve 带了绑定时非 None。
    """

    kind: ResourceKind
    #: repr=False：原文（真实路径/签名 URL）禁止经默认 repr 进入
    #: 异常链/调试日志（Review P3-6）；日志只许用 digest。
    ref: str = field(repr=False)
    digest: str
    size_hint: int | None = None
    media_ext: str | None = None
    sensitivity: str = SENSITIVITY_NORMAL
    bound_self_id: str | None = None
    bound_session: str | None = None
    expires_at: float | None = None


class ResourceResolver:
    """资源引用分类与边界校验（方案 C18 完整版）。

    构造参数：``allowed_roots`` 本地路径白名单根；``max_bytes``
    已知/估算大小上限（None 不限）；``allowed_exts`` 本地文件扩展名
    白名单（小写，带点不带点均可，None 不限）；``clock`` 时钟
    （默认 ``time.monotonic``，``expires_at`` 与之同口径）；
    ``binding_ttl`` 绑定有效期秒数（None 表示绑定不过期）。

    分类规则（``resolve``）：
    ``base64://`` → base64；``http://``/``https://`` → url；
    ``file://`` → local_path（剥前缀后按本地路径规则）；绝对路径
    → local_path；其余不透明字符串 → file_id。scheme 匹配不分大小
    写。非 str、空串、含 ``://`` 的其他 scheme 一律
    ``ResourceValidationError``。
    """

    def __init__(
        self,
        allowed_roots=(),
        *,
        max_bytes=None,
        allowed_exts=None,
        clock=time.monotonic,
        binding_ttl=None,
    ):
        self.allowed_roots = tuple(allowed_roots)
        self.max_bytes = max_bytes
        self.allowed_exts = _normalize_exts(allowed_exts)
        self.clock = clock
        self.binding_ttl = binding_ttl

    # -- 内部校验 -----------------------------------------------------

    def _check_size(self, size: int | None) -> None:
        if size is not None and self.max_bytes is not None and size > self.max_bytes:
            raise ResourceValidationError("resource exceeds max_bytes")

    def _check_local_path(self, path: str) -> str:
        """本地路径规则：必须绝对、realpath 落 roots 内、扩展名合规。

        返回 realpath 后的路径（仅内部判定用，对外 ref 仍原样）。
        """
        if not os.path.isabs(path):
            raise ResourceValidationError("local path must be absolute")
        real = os.path.realpath(path)
        if not _is_within_roots(real, self.allowed_roots):
            raise ResourceValidationError("absolute path outside allowed roots")
        if self.allowed_exts is not None:
            ext = _extract_ext(real)
            if ext is None or ext not in self.allowed_exts:
                raise ResourceValidationError("local path extension not allowed")
        return real

    def _check_local_size(self, real: str, size_hint):
        """本地文件真实大小并入 max_bytes 口径（补审第二段 P3）。

        此前 local_path 分支只查调用方声明的 size_hint：省略或低报
        即可绕过上限。文件存在时以 stat 的真实大小过 ``_check_size``
        （声明值低报不豁免），且 size_hint 缺省时以真实大小落账；
        文件不存在时维持声明口径（存在性不是本层契约）。
        """
        self._check_size(size_hint)
        if os.path.isfile(real):
            try:
                actual = os.path.getsize(real)
            except OSError:
                # isfile 与 getsize 之间的竞态（文件被删/改名/改
                # 权限）：resolver 校验失败一律
                # ResourceValidationError，裸 OSError 不得穿透异
                # 常契约（gateway 与 ImageFetcher 只捕本异常，
                # 复审第 5 轮 P4）。
                raise ResourceValidationError(
                    "local path size unavailable"
                ) from None
            self._check_size(actual)
            if size_hint is None:
                return actual
        return size_hint

    def _build(
        self,
        kind: ResourceKind,
        value: str,
        *,
        self_id,
        session,
        size_hint,
        media_ext,
        sensitivity=SENSITIVITY_NORMAL,
    ) -> ResolvedResource:
        bound_self = str(self_id) if self_id is not None else None
        bound_sess = str(session) if session is not None else None
        expires_at = None
        if self.binding_ttl is not None and (bound_self is not None or bound_sess is not None):
            expires_at = self.clock() + float(self.binding_ttl)
        return ResolvedResource(
            kind=kind,
            ref=value,
            digest=_digest_of(value),
            size_hint=size_hint,
            media_ext=media_ext,
            sensitivity=sensitivity,
            bound_self_id=bound_self,
            bound_session=bound_sess,
            expires_at=expires_at,
        )

    # -- 主入口 ---------------------------------------------------------

    def resolve(self, value, *, self_id=None, session=None, size_hint=None) -> ResolvedResource:
        """分类并校验一个资源引用，返回 ResolvedResource。"""
        if not isinstance(value, str):
            raise ResourceValidationError("resource ref must be a string")
        if not value.strip():
            raise ResourceValidationError("resource ref must not be empty")
        if size_hint is not None and (not isinstance(size_hint, int) or size_hint < 0):
            raise ResourceValidationError("size_hint must be a non-negative int")

        lowered = value.lower()
        if lowered.startswith("base64://"):
            payload = value[len("base64://"):]
            if not payload.strip():
                raise ResourceValidationError("base64 payload must not be empty")
            # 估算解码后大小：base64 每 4 字符约 3 字节。
            estimated = (len(payload) * 3) // 4
            self._check_size(estimated)
            self._check_size(size_hint)
            return self._build(
                ResourceKind.BASE64,
                value,
                self_id=self_id,
                session=session,
                size_hint=size_hint if size_hint is not None else estimated,
                media_ext=None,
            )
        if lowered.startswith(("http://", "https://")):
            self._check_size(size_hint)
            return self._build(
                ResourceKind.URL,
                value,
                self_id=self_id,
                session=session,
                size_hint=size_hint,
                media_ext=_extract_ext(value),
                sensitivity=_url_sensitivity(value),
            )
        if lowered.startswith("file://"):
            path = value[len("file://"):]
            real = self._check_local_path(path)
            size_hint = self._check_local_size(real, size_hint)
            return self._build(
                ResourceKind.LOCAL_PATH,
                value,
                self_id=self_id,
                session=session,
                size_hint=size_hint,
                media_ext=_extract_ext(path),
            )
        if "://" in value:
            scheme = value.split("://", 1)[0].lower()
            raise ResourceValidationError(f"resource scheme not allowed: {scheme}")
        if os.path.isabs(value):
            real = self._check_local_path(value)
            size_hint = self._check_local_size(real, size_hint)
            return self._build(
                ResourceKind.LOCAL_PATH,
                value,
                self_id=self_id,
                session=session,
                size_hint=size_hint,
                media_ext=_extract_ext(value),
            )
        self._check_size(size_hint)
        return self._build(
            ResourceKind.FILE_ID,
            value,
            self_id=self_id,
            session=session,
            size_hint=size_hint,
            media_ext=_extract_ext(value),
        )

    def check_binding(self, resolved: ResolvedResource, *, self_id=None, session=None) -> None:
        """校验已解析资源的绑定与有效期；不合规抛 ResourceValidationError。

        已绑定的 self_id / session 与当前上下文不一致（含当前为
        None）即拒绝；未绑定的字段不校验。``expires_at`` 非 None
        且当前时钟已越过即视为过期拒绝（与绑定字段是否齐全无关，
        过期资源不得在任何上下文复用）。
        """
        if not isinstance(resolved, ResolvedResource):
            raise ResourceValidationError("resolved resource required")
        if resolved.expires_at is not None and self.clock() > resolved.expires_at:
            raise ResourceValidationError("resource binding expired")
        if resolved.bound_self_id is not None:
            if self_id is None or str(self_id) != resolved.bound_self_id:
                raise ResourceValidationError("resource bound to another self_id")
        if resolved.bound_session is not None:
            if session is None or str(session) != resolved.bound_session:
                raise ResourceValidationError("resource bound to another session")


def validate_resource_ref(value, *, allowed_roots=()) -> str:
    """校验资源引用并原样返回；不合规抛 ResourceValidationError。

    N1 冻结语义（不得放宽，见模块 docstring）：与 resolver 的分类
    口径区别在于——此处 ``file://`` / ``base64://`` 等非 http(s)
    scheme 一律拒绝，空串与相对/不透明字符串仍原样放行。
    """
    if not isinstance(value, str):
        raise ResourceValidationError("resource ref must be a string")
    if "://" in value:
        scheme = value.split("://", 1)[0].lower()
        if scheme in {"http", "https"}:
            return value
        raise ResourceValidationError(f"resource scheme not allowed: {scheme}")
    if os.path.isabs(value):
        real = os.path.realpath(value)
        if _is_within_roots(real, allowed_roots):
            return value
        raise ResourceValidationError("absolute path outside allowed roots")
    return value


#: Windows 盘符绝对路径（``C:/...``，分隔符已归一）：在本部署的
#: POSIX 语义下不可能落入任何 roots，按界外绝对路径处理。
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:/")


def _looks_like_path(value: str) -> bool:
    """路径形态判定：含任一分隔符，或就是 ``.`` / ``..`` 本身。"""
    return "/" in value or "\\" in value or value in (".", "..")


def validate_path_semantic_ref(value, *, allowed_roots=(), base_dir=None) -> str:
    """路径语义资源值校验（补审第二段 P2）：原样返回或抛错。

    Gateway 步骤 d 对资源键（file / file_path / folder_path /
    thumb_path / files / path 等）的值调用本函数，与
    ``validate_resource_ref`` 的分工：

    - 含 ``://`` 的值（URL 与其他 scheme）转 ``validate_resource_ref``
      按其冻结口径处置；
    - 不透明字符串（无路径形态，如平台 file_id）原样放行；
    - 带路径形态的值一律按本地路径处理：绝对路径 realpath 后须落
      ``allowed_roots`` 内（roots 为空即拒绝一切路径形态，与绝对
      路径的既有口径对称）；相对路径以 ``base_dir`` 为基准解析为
      realpath 后同样查 roots——``..`` 穿越在 realpath 后无所遁形，
      ``base_dir`` 未配置时相对路径无从解析、一律拒绝（fail
      closed）。反斜杠按分隔符同等对待，盘符形态按界外绝对路径
      拒绝。校验只判定、不改写：通过后原样返回，params 不被重写。
    """
    if not isinstance(value, str):
        raise ResourceValidationError("resource ref must be a string")
    if "://" in value:
        return validate_resource_ref(value, allowed_roots=allowed_roots)
    if not _looks_like_path(value):
        return value
    normalized = value.replace("\\", "/")
    if _DRIVE_PATH_RE.match(normalized):
        raise ResourceValidationError("absolute path outside allowed roots")
    if normalized.startswith("/"):
        real = os.path.realpath(normalized)
    else:
        if base_dir is None:
            raise ResourceValidationError(
                "relative path has no configured base directory"
            )
        real = os.path.realpath(os.path.join(str(base_dir), normalized))
    if not _is_within_roots(real, allowed_roots):
        raise ResourceValidationError("path outside allowed roots")
    return value


# ------------------------------------------------- 公网边界共享判定
#
# 本节的显式拒绝网段、翻译地址解包、公网判定与 WHATWG IPv4 字面量
# 解析原为 companion.vision 取图校验的私有实现（经识图线三轮审查
# 硬化）；补审第二段复审第 3 轮起统一收口于此：vision 取图与
# download_file 的 SSRF 边界共用同一份实现，禁止再写平行校验器
# （第二段修复线曾自写 download 校验，因口径分叉被 WHATWG 等价
# 主机名绕过——复审 P2/P3）。

#: 不依赖 stdlib 版本表的显式拒绝网段：``is_global`` 的判定表在
#: CPython 版本间有差异（如 224.0.0.0/4 组播在 3.12.3 实测为
#: True），特殊用途网段一律在此显式拒绝，使边界与解释器版本
#: 无关（确认复审 P3）。
_DENIED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "100.64.0.0/10",  # RFC 6598 共享地址空间（云元数据常落此段）
        "192.0.0.0/24",  # IETF 协议指派
        "192.88.99.0/24",  # 6to4 中继 anycast（已弃用）
        "198.18.0.0/15",  # 基准测试保留
        "224.0.0.0/4",  # 组播
        "240.0.0.0/4",  # 保留（含广播地址）
    )
)


#: NAT64 翻译前缀：内嵌的末 32 位 IPv4 才是真实目标，必须解包
#: 复核——64:ff9b::a9fe:a9fe 这类内嵌链路本地地址的形式在
#: stdlib 下 is_global 为真，不解包即绕过公网边界（复审 P3）。
_NAT64_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "64:ff9b::/96",  # RFC 6052 NAT64 知名前缀
        "64:ff9b:1::/48",  # RFC 8215 NAT64 本地-use 前缀
    )
)


def _embedded_ipv4(
    ip: ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    """解包 IPv6 翻译/映射形式内嵌的 IPv4；非这类地址返回 None。"""
    if any(ip in network for network in _NAT64_NETWORKS):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    return None


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """公网判定：stdlib is_global 之外再过显式特殊网段拒绝表。

    IPv6 的 NAT64 / v4-mapped / 6to4 形式另解包内嵌 IPv4 防御性
    复核：内嵌地址也必须是公网，整体才算公网（复审 P3）。
    """
    if not ip.is_global:
        return False
    if any(
        ip in network for network in _DENIED_NETWORKS if ip.version == network.version
    ):
        return False
    if ip.version == 6:
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            return _is_public_ip(embedded)
    return True


#: WHATWG 段值溢出哨兵：大于任何合法 IPv4 数值，调用方据此把
#: 该 host 判为非法字面量并 blocked_target。
_WHATWG_OVERFLOW = 256**4


def _parse_whatwg_number(part: str) -> int | None:
    """按 WHATWG number 语法解析一段：十进制 / 0 前缀八进制 /
    0x 十六进制；不是数字形式返回 None。

    转换必须有界：直接 ``int(part, 10)`` 对超过 CPython 整数
    字符串转换上限（4300 位）的十进制段会抛 ValueError 并穿透
    校验器。剥离进制前缀与前导零后，有效数字位数超过该进制下
    2**32 所需最大位数（十进制 10 / 十六进制 8 / 八进制 11）
    时直接返回溢出哨兵；有效数字为空按 0 处理。
    """
    if not part:
        return None
    lowered = part.lower()
    if lowered.startswith("0x"):
        digits = part[2:]
        if not digits:
            return 0
        if any(char not in "0123456789abcdefABCDEF" for char in digits):
            return None
        significant = digits.lstrip("0")
        if not significant:
            return 0
        if len(significant) > 8:
            return _WHATWG_OVERFLOW
        return int(significant, 16)
    if len(part) > 1 and part.startswith("0"):
        if any(char not in "01234567" for char in part):
            return None
        significant = part.lstrip("0")
        if not significant:
            return 0
        if len(significant) > 11:
            return _WHATWG_OVERFLOW
        return int(significant, 8)
    if any(char not in "0123456789" for char in part):
        return None
    significant = part.lstrip("0")
    if not significant:
        return 0
    if len(significant) > 10:
        return _WHATWG_OVERFLOW
    return int(significant, 10)


def _parse_whatwg_ipv4(host: str) -> tuple[bool, ipaddress.IPv4Address | None]:
    """识别 WHATWG 风格 IPv4 字面量（host 已去尾点）。

    返回 ``(是否字面量, 地址)``：每段都是数字形式且段数 1–4 才
    算字面量；段值溢出（非末段 >=256、末段 >=256^(5-段数)）仍
    是字面量但非法，地址为 None，由调用方按 blocked 处理——
    这类形式（如 2130706433 / 0x7f000001 / 127.1 / 0177.0.0.1）
    在 ``ipaddress`` 下抛 ValueError，若落回主机名路径会经代理
    豁免绕过字面量判定（复审 P2）。
    """
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return (False, None)
    values = []
    for part in parts:
        value = _parse_whatwg_number(part)
        if value is None:
            return (False, None)
        values.append(value)
    if any(value >= 256 for value in values[:-1]):
        return (True, None)
    if values[-1] >= 256 ** (5 - len(values)):
        return (True, None)
    address = values[-1]
    for index, value in enumerate(values[:-1]):
        address += value * 256 ** (3 - index)
    return (True, ipaddress.IPv4Address(address))


#: download_file 主机名内网后缀：命中即拒绝（与视觉取图的内部后缀
#: 口径同向；DNS 解析不在网关做——网关只判字面形态，解析后落点
#: 的复核属平台抓取侧与启用前运维评估，见 docs 接受残余口径）。
_DOWNLOAD_INTERNAL_SUFFIXES = (
    ".local",
    ".lan",
    ".home.arpa",
    ".internal",
    ".intranet",
    ".corp",
    ".private",
    ".localdomain",
)

#: WHATWG 主机名归一化映射：平台抓取方的 URL 解析（WHATWG URL
#: 规范的 domain 解析）会把点等价字符 U+3002/U+FF0E/U+FF61 当作
#: 标签分隔点、把全角 ASCII（U+FF01–U+FF5E）映射回 ASCII 后再
#: 判定主机；Python 的 urlparse / ipaddress 不做此归一——等价
#: 写法在 Python 眼里只是普通 DNS 名，在抓取方眼里却归一为
#: 127.0.0.1 / localhost / 169.254.169.254（复审第 3 轮 P2）。
#: 校验前必须先按同一口径归一，使被校验的主机与抓取方实际
#: 连接的主机逐字一致。第 4 轮 P2 再补两步：NFKC 兼容折叠
#: （圆圈数字/上标等全角区段之外的兼容映射）与默认忽略字符
#: 的 fail-closed 拒绝（见下）。
_WHATWG_HOST_TRANSLATION = {0x3002: ".", 0xFF0E: ".", 0xFF61: "."}
_WHATWG_HOST_TRANSLATION.update(
    {code: chr(code - 0xFEE0) for code in range(0xFF01, 0xFF5E + 1)}
)

#: UTS-46 默认忽略字符（Default_Ignorable_Code_Point）：WHATWG /
#: UTS-46 的 domain 处理会在连接前把它们删掉——如
#: ``127<U+200B>.0.0.1`` 实际连到 127.0.0.1、``local<U+200B>host``
#: 连到 localhost——而合法主机名永不含此类字符。校验侧不复刻
#: 删除，直接 fail-closed 拒绝（复审第 4 轮 P2）。本表为手工维护：
#: 按 Unicode Default_Ignorable_Code_Point 属性逐段列入（含补充
#: 面变体选择器、标签字符与速记/乐谱格式控制段——第 4 轮手列时
#: 曾漏后四段，复审第 4 轮 P2 翻出后于第 5 轮补入）。完备性不靠
#: 肉眼保证：tests/test_file_contracts_n5.py 的完备性回归扫描以
#: 仓库自带快照语料（tests/uts46_ignored_snapshot.py，生成自
#: idna 包 IdnaMappingTable 的 ignored 全集并有对账用例逐点核
#: 对现表）逐个断言拒绝，本表今后漏任何一段该扫描必须变红。
_DEFAULT_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD),  # SOFT HYPHEN
    (0x034F, 0x034F),  # COMBINING GRAPHEME JOINER
    (0x061C, 0x061C),  # ARABIC LETTER MARK
    (0x115F, 0x1160),  # HANGUL FILLER
    (0x17B4, 0x17B5),  # KHMER INHERENT AQ / AA
    (0x180B, 0x180F),  # MONGOLIAN FVS / VOWEL SEPARATOR 等
    (0x200B, 0x200F),  # ZERO WIDTH SPACE / JOINER / BIDI MARK
    (0x202A, 0x202E),  # BIDI EMBEDDING / OVERRIDE
    (0x2060, 0x206F),  # WORD JOINER / 不可见格式符
    (0xFE00, 0xFE0F),  # 变体选择器 VS1–VS16
    (0xFEFF, 0xFEFF),  # ZERO WIDTH NO-BREAK SPACE（BOM）
    (0xFFA0, 0xFFA0),  # HALFWIDTH HANGUL FILLER
    (0x1BCA0, 0x1BCA3),  # 速记格式控制（复审第 4 轮 P2 漏段）
    (0x1D173, 0x1D182),  # 乐谱格式控制：开始/结束标记等（同上）
    (0x1D185, 0x1D18B),  # 乐谱组合格式符（同上）
    (0x1D1AA, 0x1D1AD),  # 乐谱组合格式符（同上）
    (0xE0001, 0xE0001),  # LANGUAGE TAG
    (0xE0020, 0xE007F),  # TAG 字符
    (0xE0100, 0xE01EF),  # 补充面变体选择器 VS17–VS256
)


def _contains_default_ignorable(text: str) -> bool:
    """text 是否含 UTS-46 默认忽略字符（抓取方会删掉后再连接）。"""
    return any(
        low <= ord(char) <= high
        for char in text
        for low, high in _DEFAULT_IGNORABLE_RANGES
    )


def _normalize_whatwg_host(host: str) -> str:
    """按 WHATWG/UTS-46 口径归一主机名：点等价/全角映射、NFKC
    兼容折叠、小写。

    NFKC 折叠覆盖全角区段之外的兼容映射形态：圆圈数字、上标
    数字等在抓取方的 UTS-46 处理下会变成 ASCII 数字（如
    ``①②⑦.0.0.1`` → ``127.0.0.1``、``127.0.0.¹`` →
    ``127.0.0.1``），校验前必须同样折叠才看得见真实目标
    （复审第 4 轮 P2）。全角百分号（U+FF05）同在映射/NFKC 内
    回到 ``%``，故百分号编码的判定必须在本归一之后做（见
    :func:`_validate_normalized_host_form`）。
    """
    translated = host.translate(_WHATWG_HOST_TRANSLATION)
    return unicodedata.normalize("NFKC", translated).lower()


def _validate_normalized_host_form(host: str) -> str:
    """归一后主机形态门：download_file 与 vision 取图共用的同一实现。

    :func:`_normalize_whatwg_host` 之后的主机还须过三道形态门：
    含 ``%`` 直接拒绝（百分号编码会被抓取方解码，全角 ％ 亦经
    归一回到 ``%``，故判定必须在归一之后）；尾点只许剥一个，
    剥后为空或仍带尾点即畸形；含空标签（``127..0.1`` 的双点
    形态）即畸形。返回剥尾点后的主机名，不合规抛
    :class:`ResourceValidationError`（vision 侧捕获后转
    blocked_target）。

    这三道门在第 5 轮前只写在 ``validate_download_url`` 函数体
    内、未随归一层共享：vision 接入归一层时漏掉，百分号编码
    回环与空标签形态在代理模式被放行（复审第 5 轮 P3）。故
    下沉至此——今后任何校验方归一后必须过本门，不得再各自
    内联复制。
    """
    if "%" in host:
        raise ResourceValidationError("host is not a plain hostname")
    if host.endswith("."):
        host = host[:-1]
        if not host or host.endswith("."):
            raise ResourceValidationError("host is malformed")
    if "" in host.split("."):
        raise ResourceValidationError("host is malformed")
    return host


def validate_download_url(value) -> str:
    """download_file 的 URL SSRF 边界（补审第二段 P3）：原样返回或抛错。

    只许 http/https；主机名为 localhost 或带内网后缀时拒绝；
    主机为字面 IP 时——含 WHATWG 非规范 IPv4 形式（十进制 /
    十六进制 / 八进制 / 短式）——一律过与 vision 取图共享的
    :func:`_is_public_ip`（stdlib ``is_global`` + 显式拒绝网段
    + NAT64 / v4-mapped / 6to4 内嵌 IPv4 解包复核），非公网
    拒绝。一般资源 URL（图片等）不走本函数，口径不外溢；DNS
    解析不在网关做——本函数只判字面形态，解析后落点的复核属
    平台抓取侧与启用前运维评估（见模块 docstring 的残余口径）。

    匹配前先按 WHATWG 口径归一主机名（复审第 3 轮 P2）：
    :func:`_normalize_whatwg_host` 把点等价字符与全角 ASCII
    映射回 ASCII（抓取方先归一再连接，校验对象必须与之一致，
    否则 ``127\u30020\u30020\u30021``、全角数字 localhost 等
    写法在 Python 侧以普通 DNS 名过关、在抓取方侧却落到回环 /
    链路本地目标）；归一后含 ``%`` 的主机名（百分号编码会被
    抓取方解码）字面拒绝；尾点剥单个后再匹配，剥后仍带尾点
    或含空标签（``127..0.1`` 的双点形态，复审 P4）的畸形主机
    直接拒绝——三道形态门实现在共享的
    :func:`_validate_normalized_host_form`，vision 取图同过本门
    （复审第 5 轮 P3 下沉）。urlparse / hostname 的裸 ``ValueError``（畸形
    括号主机、NFKC 敌对 netloc）一律转
    :class:`ResourceValidationError`（复审 P3），与
    ``validate_path_semantic_ref`` 的契约一致——网关调用方只
    捕本异常并回 resource_rejected，裸异常穿透即契约破裂。

    第 4 轮 P2 补齐 UTS-46 等价面：归一管线另含 NFKC 兼容折叠
    （圆圈数字/上标等兼容映射形与全角同罪，见
    :func:`_normalize_whatwg_host`）；主机名含默认忽略字符
    （抓取方删除后再连接、本机名永不含）时 fail-closed 直接
    拒绝——归一前后各查一遍，防折叠把字符变没或变出。
    """
    if not isinstance(value, str):
        raise ResourceValidationError("download url must be a string")
    try:
        parsed = urlparse(value)
        host = parsed.hostname
    except ValueError:
        raise ResourceValidationError("download url is malformed") from None
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ResourceValidationError("download url scheme not allowed")
    if not host:
        raise ResourceValidationError("download url has no host")
    if _contains_default_ignorable(host):
        raise ResourceValidationError("download host is not a plain hostname")
    host = _normalize_whatwg_host(host)
    if _contains_default_ignorable(host):
        raise ResourceValidationError("download host is not a plain hostname")
    host = _validate_normalized_host_form(host)
    if (
        host == "localhost"
        or host.endswith(".localhost")
        or host == "home.arpa"
        or host.endswith(_DOWNLOAD_INTERNAL_SUFFIXES)
    ):
        raise ResourceValidationError("download host is internal")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not _is_public_ip(ip):
            raise ResourceValidationError("download host is not a public address")
        return value
    is_ipv4_literal, whatwg_ip = _parse_whatwg_ipv4(host)
    if is_ipv4_literal:
        if whatwg_ip is None:
            raise ResourceValidationError(
                "download host is a non-canonical IP literal"
            )
        if not _is_public_ip(whatwg_ip):
            raise ResourceValidationError("download host is not a public address")
    return value


def validate_on_disk_name(value) -> str:
    """落盘文件名校验（补审第二段 P3）：只许 basename 形态。

    download_file 的 ``name``、upload_file_stream 的 ``filename``
    决定平台落盘位置：含任一目录分隔符、或为 ``.`` / ``..`` 、或
    为空，一律拒绝——落盘名不得携带任何目录语义。
    """
    if not isinstance(value, str):
        raise ResourceValidationError("on-disk name must be a string")
    if not value or value in (".", "..") or "/" in value or "\\" in value:
        raise ResourceValidationError("on-disk name must be a plain basename")
    return value
