"""私聊图片理解：图片下载、魔数嗅探与视觉生成装配。

本模块只负责把 perception 提取出的图片引用（鸭子类型，只读
``.ref``）安全地取回、嗅探为受支持的图片类型并拼成 DeepSeek
视觉接口需要的 base64 data URL；实际生成由 brain 的
DeepSeekVisionClient 完成，本模块不持有密钥。

安全与审计纪律（沿用仓库口径）：

- 引用先经 ``ResourceResolver`` 分类，只有 http/https URL 允许
  下载；本地路径、base64、file_id 等引用一律拒绝，避免把本地
  文件或不透明标识当图片读出。
- 下载目标另有公网边界（Review F1）：每一跳（含重定向目标）都
  校验 scheme/host——IP 字面量与 DNS 解析结果都必须是公网地址，
  localhost 与私网/环回/链路本地一律拒绝，重定向手动跟随且封顶，
  防止消息段 URL 被用作 SSRF 跳板打向本机/内网。公网判定除
  stdlib ``is_global`` 外另过显式特殊用途网段拒绝表（确认复审
  P3），不依赖解释器版本表。取图客户端沿用 httpx 默认
  trust_env：部署环境给该 URL 配置了运维代理时，出站解析与
  连接由代理执行（与同进程其他客户端口径一致，确认复审 P4），
  本地 DNS 答案可能是代理侧的合成地址、不代表真实目标：校验
  器在代理适用时只对全部落 fake-IP 段（198.18.0.0/15）的本地
  答案跳过 DNS 公网判定，存在该段之外的答案时仍逐个判定
  （本地解析失败/为空则回落跳过，代理侧仍会解析）；scheme/
  localhost（含尾点与 *.localhost）/内部后缀主机名
  （metadata.google.internal 与 *.internal/*.local/*.lan 与
  home.arpa/*.home.arpa）/
  字面量 IP 检查不豁免，字面量（含 WHATWG 非规范 IPv4 形式，
  如十进制/十六进制/短式/八进制）非公网即使经代理也拒绝；
  无代理适用时本地校验完整约束直连出站（2026-10-31 真机不
  触发修复：此前校验器未区分代理/直连，在本机 fake-IP 解析
  环境下把全部真实图片域名误判 blocked_target，视觉链路恒
  回退固定文案）。
- 取图主路径为 NapCat 介导（2026-10-31）：直连 CDN 在本机
  fake-IP 代理环境下间歇超时，改由 NapCat ``get_image`` 按
  file_id 返回其本地缓存文件路径，本模块经 ``ResourceResolver``
  校验该路径（LOCAL_PATH、roots 内、图片扩展名白名单）后限量
  读取；NapCat 路径失败才落回既有直连 HTTP 兜底。
- 下载边读边累计字节，超过上限立即中止，不先落全量再判断。
- 日志只记元数据（resolver digest、字节数、嗅探类型、结果码），
  绝不记 ref 原文、图片内容、data URL 或密钥；异常只带结果码，
  不带 ref 与底层错误原文，防止签名 URL 随异常链外泄。

内容安全边界（Review F7，2026-11-01 用户拍板补上）：图片内容
经视觉模型生成客观描述（见 :data:`IMAGE_DESCRIPTION_PROMPT`，
逐字转写图中文字、如实描述内容），描述文本在回复生成之前先过
既有危机网关筛查（接入层以与用户文字相同的 ``SafetyGateway``
口径判定，见 ingress 的 ``_screen_vision_content``）；判定为疑
似危机时严格走既有危机协议（固定话术 + 告警 + observe 语义，
不经本模块）。随图文字仍在 SafetyJudge 阶段按原口径判定，两路
互不替代。描述不可得（视觉生成失败）时筛查无从进行，接入层按
视觉失败收敛——未筛查的图片内容不进入回复生成。
"""

import asyncio
import base64
import hashlib
import ipaddress
import logging
import os
import socket
import stat
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from companion.adapters.resources import (
    ResourceKind,
    ResourceResolver,
    ResourceValidationError,
    _contains_default_ignorable,
    _is_public_ip,
    _normalize_whatwg_host,
    _parse_whatwg_ipv4,
    _validate_normalized_host_form,
)

logger = logging.getLogger("companion.vision")

# httpx 的请求日志在 INFO 级会带完整 URL（取图 URL 是签名 URL）：
# 把 httpx logger 压到 WARNING，避免部署侧把 root 调到 INFO 时
# 签名 URL 随请求行进日志（Review F8）。
logging.getLogger("httpx").setLevel(logging.WARNING)

#: 媒体不可处理时的固定回复；视觉不可用（无密钥/未开启）或图片
#: 全部失败时，调用方逐字沿用这句旧回复，保证行为与接入前一致。
MEDIA_FIXED_REPLY = "我暂时无法处理图片、语音等非文字消息，请用文字告诉我。"

#: 图片内容筛查用的描述提示词（Review F7）：要求视觉模型只做客
#: 观描述、逐字转写图中全部文字——图片里的危机信号常以文字形态
#: 出现（纸条、聊天截图），转写保真度直接决定筛查灵敏度；描述
#: 不带人设、不给建议，随后由危机网关（关键词 + 模型 judge）以
#: 与用户文字相同的口径判定，描述本身不做任何安全裁量。
IMAGE_DESCRIPTION_PROMPT = (
    "请客观、具体地描述这张图片的内容，供安全筛查使用："
    "逐字转写图片中出现的全部文字；如实描述画面中的人物、动作、"
    "场景与物品，包括伤口、血液、药物、危险物品或危险行为等细节。"
    "只陈述你看到的内容，不推测图片之外的事情，不评价、不给建议。"
)


def sniff_image_mime(data: bytes) -> str | None:
    """按魔数嗅探图片 MIME；只认 DeepSeek 视觉支持的四种格式。

    不信任引用自带的类型声明（URL 后缀/上游字段都可伪造），以
    实际字节头为准：嗅探不出即视为非图片，由调用方计 skipped。
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    # WebP 是 RIFF 容器：前 4 字节 "RIFF"、其后 4 字节为长度、
    # 再后 4 字节才是 "WEBP" 标识，短于 12 字节无法判定。
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class ImageFetchError(Exception):
    """图片下载失败；只带机器可读的结果码，不带 ref 或错误原文。

    ``code`` 取值：``invalid_ref``（引用未过 resolver 校验）、
    ``unsupported_ref``（非 URL 类引用）、``blocked_target``（目标
    未过公网边界校验）、``http_status``（非 2xx 或重定向异常）、
    ``too_large``（超过字节上限）、``transport``（传输层失败）；
    NapCat 介导取图另有 ``napcat_timeout`` / ``napcat_transport`` /
    ``napcat_rejected`` / ``napcat_no_path`` / ``path_rejected``
    （见 NapCatImageFetcher）。
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


#: 取图重定向手动跟随的上限（每一跳都重跑公网边界校验）。
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


#: 代理环境下本地 DNS 的合成答案段（基准测试保留段，与拒绝表
#: 同段）：代理适用时全部答案落此段才视为合成答案跳过判定。
_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


async def _resolve_addresses(host: str) -> set[str]:
    """主机名 DNS 解析（独立成函数以便测试替身注入解析结果）。"""
    infos = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(
            host, None, proto=socket.IPPROTO_TCP
        ),
        timeout=5.0,
    )
    return {info[4][0] for info in infos}


def _env_proxy_applies(url: str) -> bool:
    """该 URL 是否会被环境代理接管（与 httpx trust_env 同源判定）。

    用 stdlib urllib 的环境代理解析（读 http_proxy/https_proxy/
    all_proxy 并按 no_proxy 做 bypass 判定），与取图客户端实际
    使用的代理一致；解析异常一律按「不适用」处理（回落完整本地
    校验，fail-closed 方向）。
    """
    try:
        parsed = urlparse(url)
        proxies = urllib.request.getproxies()
        proxy = proxies.get(parsed.scheme) or proxies.get("all")
        if not proxy:
            return False
        return not urllib.request.proxy_bypass(parsed.hostname or "")
    except Exception:  # noqa: BLE001 — 判定失败回落本地校验
        return False


async def _default_target_validator(url: str) -> None:
    """校验取图目标为公网 http/https 地址；不合规抛 blocked_target。

    IP 字面量直接判公网（即使经代理也判，字面量非公网地址永不
    豁免）：除 ``ipaddress`` 规范形式外，另识别 WHATWG 非规范
    IPv4 形式（十进制/十六进制/八进制/短式，见
    :func:`_parse_whatwg_ipv4`）。主机名先过与 download_file 共
    享的归一层（resources 的 :func:`_normalize_whatwg_host`：
    点等价/全角映射 + NFKC 兼容折叠 + 小写；归一前后含 UTS-46
    默认忽略字符直接拒绝——抓取方会先删掉它们再连接，如圆圈
    数字/零宽字符/全角形态的主机在抓取方眼里即 127.0.0.1 /
    localhost），再过与 download_file 共享的形态门（resources
    的 :func:`_validate_normalized_host_form`：含百分号、空标
    签、多重尾点一律拒绝并剥去单个尾点，复审第 5 轮 P3），
    然后排除 localhost 与内部后缀
    （metadata.google.internal 及
    *.internal/*.local/*.lan 与 home.arpa/*.home.arpa，代理适用
    时同样拒绝——归一层与字面量/后缀检查均不因代理豁免，复审
    第 4 轮 P3）。环境代理
    适用该 URL 时，解析与连接由代理执行：尝试本地解析，失败/
    超时/空结果回落跳过判定；全部答案落 fake-IP 段
    （198.18.0.0/15）视为代理合成答案跳过判定；存在该段之外
    的答案时对真实答案逐个判公网、fake-IP 答案忽略（复审
    P2）。无代理时做 DNS 解析且要求全部解析结果都是公网地址，
    解析失败/超时按 fail-closed 拒绝（Review F1）。校验与连接
    之间存在 DNS 重绑定的固有窗口，此处是纵深防御的一层，不
    宣称根除。
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError:
        raise ImageFetchError("blocked_target") from None
    if parsed.scheme not in ("http", "https") or not host:
        raise ImageFetchError("blocked_target")
    if _contains_default_ignorable(host):
        raise ImageFetchError("blocked_target")
    normalized = _normalize_whatwg_host(host)
    if _contains_default_ignorable(normalized):
        raise ImageFetchError("blocked_target")
    try:
        normalized = _validate_normalized_host_form(normalized)
    except ResourceValidationError:
        raise ImageFetchError("blocked_target") from None
    if normalized == "localhost" or normalized.endswith(".localhost"):
        raise ImageFetchError("blocked_target")
    if (
        normalized == "metadata.google.internal"
        or normalized.endswith(".internal")
        or normalized.endswith(".local")
        or normalized.endswith(".lan")
        or normalized == "home.arpa"
        or normalized.endswith(".home.arpa")
    ):
        raise ImageFetchError("blocked_target")
    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(literal):
            raise ImageFetchError("blocked_target")
        return
    is_ipv4_literal, whatwg_literal = _parse_whatwg_ipv4(normalized)
    if is_ipv4_literal:
        if whatwg_literal is None or not _is_public_ip(whatwg_literal):
            raise ImageFetchError("blocked_target")
        return
    if _env_proxy_applies(url):
        try:
            addresses = await _resolve_addresses(normalized)
        except (OSError, TimeoutError):
            return
        if not addresses:
            return
        real_addresses = []
        for address in addresses:
            try:
                candidate = ipaddress.ip_address(address)
            except ValueError:
                candidate = None
            if (
                candidate is not None
                and candidate.version == 4
                and candidate in _FAKE_IP_NETWORK
            ):
                continue
            real_addresses.append(candidate)
        if not real_addresses:
            return
        for candidate in real_addresses:
            if candidate is None or not _is_public_ip(candidate):
                raise ImageFetchError("blocked_target")
        return
    try:
        addresses = await _resolve_addresses(normalized)
    except (OSError, TimeoutError):
        raise ImageFetchError("blocked_target") from None
    if not addresses:
        raise ImageFetchError("blocked_target")
    for address in addresses:
        try:
            is_public = _is_public_ip(ipaddress.ip_address(address))
        except ValueError:
            is_public = False
        if not is_public:
            raise ImageFetchError("blocked_target")


class ImageFetcher:
    """按引用流式下载图片字节；transport 与目标校验器可注入以便测试。

    ``target_validator`` 是 async callable(url) -> None，不合规抛
    ImageFetchError；缺省为 :func:`_default_target_validator` 的
    公网边界。重定向不使用 httpx 自动跟随：手动逐跳校验 + 封顶，
    避免第一跳公网、第二跳内网的绕过。
    """

    def __init__(self, *, timeout: float = 10.0, transport=None, target_validator=None):
        self.timeout = timeout
        self.transport = transport
        self.target_validator = target_validator

    async def fetch(self, ref: str, *, max_bytes: int) -> bytes:
        """下载一张图片；任何失败都收敛为只带 code 的 ImageFetchError。"""
        resolver = ResourceResolver(max_bytes=max_bytes)
        try:
            resolved = resolver.resolve(ref)
        except ResourceValidationError:
            # 不保留异常链：校验消息虽为通用文案，仍按“原文不出
            # 本模块”口径切断，避免后续规则变更把 ref 带进日志。
            raise ImageFetchError("invalid_ref") from None
        if resolved.kind is not ResourceKind.URL:
            logger.info(
                "image fetch skipped: code=unsupported_ref digest=%s",
                resolved.digest,
            )
            raise ImageFetchError("unsupported_ref")
        validator = self.target_validator or _default_target_validator
        try:
            # trust_env 显式为真：_env_proxy_applies 的跳过判定以此为
            # 前提（校验与传输必须同口径，防一侧改默认后悄悄分叉成
            # 「校验以为走代理、传输却直连」的 fail-open）。
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self.transport,
                follow_redirects=False,
                trust_env=True,
            ) as client:
                url = ref
                for hop in range(_MAX_REDIRECTS + 1):
                    await validator(url)
                    async with client.stream("GET", url) as response:
                        status = response.status_code
                        if status in _REDIRECT_STATUSES:
                            location = response.headers.get("location")
                            if location and hop < _MAX_REDIRECTS:
                                url = str(httpx.URL(url).join(location))
                                continue
                            code = (
                                "too_many_redirects" if location else "http_status"
                            )
                            logger.info(
                                "image fetch failed: code=%s digest=%s status=%s",
                                code,
                                resolved.digest,
                                status,
                            )
                            raise ImageFetchError(code)
                        if not 200 <= status < 300:
                            logger.info(
                                "image fetch failed: code=http_status digest=%s status=%s",
                                resolved.digest,
                                status,
                            )
                            raise ImageFetchError("http_status")
                        chunks = bytearray()
                        async for chunk in response.aiter_bytes():
                            chunks.extend(chunk)
                            if len(chunks) > max_bytes:
                                logger.info(
                                    "image fetch failed: code=too_large digest=%s",
                                    resolved.digest,
                                )
                                raise ImageFetchError("too_large")
                        logger.info(
                            "image fetch ok: digest=%s bytes=%s",
                            resolved.digest,
                            len(chunks),
                        )
                        return bytes(chunks)
        except ImageFetchError:
            raise
        except Exception as exc:  # noqa: BLE001 — 契约：任何失败收敛为结果码
            # 传输异常的原文可能带 URL，只记异常类型名与结果码。
            # 此前只捕 httpx.HTTPError：InvalidURL 等非 HTTPError 的
            # httpx 异常会穿透本函数、违反模块「一切失败收敛为
            # ImageFetchError」的口径（2026-10-31 接缝修复时收敛；
            # CancelledError 是 BaseException、不受此捕捉影响）。
            logger.warning(
                "image fetch failed: code=transport digest=%s error=%s",
                resolved.digest,
                type(exc).__name__,
            )
            raise ImageFetchError("transport") from None
        # 循环在最后一跳必然 return 或 raise，此处不可达。
        raise ImageFetchError("http_status")  # pragma: no cover


class NapCatImageFetcher:
    """NapCat 介导取图：按 file_id 向网关要本地缓存文件并限量读取。

    ``get_image`` 是 async callable(file_id) -> 鸭子类型结果（有
    ``.outcome`` 与 ``.data`` 两属性，即网关 ActionResult）；
    ``data["file"]`` 为 NapCat 侧缓存文件的本地绝对路径。路径先经
    ``ResourceResolver`` 校验（分类须 LOCAL_PATH、realpath 落在
    ``roots`` 内、扩展名在图片白名单内），再先 stat 判大小、后
    限量读（读 max_bytes+1 判超限），避免大文件全量入内存。

    任何失败都收敛为只带 code 的 :class:`ImageFetchError`（异常链
    切断口径与 ImageFetcher 一致）：``napcat_timeout``（wait_for
    超时）、``napcat_transport``（get_image 调用抛异常）、
    ``napcat_rejected``（outcome != "ok"）、``napcat_no_path``
    （data 不是 dict 或 file 不是非空 str）、``path_rejected``
    （resolver 拒绝、畸形路径、非普通文件或本地文件不可读）、
    ``too_large``（超上限）、``empty``（读到 0 字节）。
    日志只记 file_id 的 sha256 前 16 位 digest、字节数与 code，
    并带 ``path=napcat`` 标识；绝不记 file_id 原文、路径原文。
    """

    #: NapCat 缓存图片允许的扩展名白名单（小写、不带点）。
    _ALLOWED_EXTS = ("jpg", "jpeg", "png", "gif", "webp")

    def __init__(self, get_image, *, roots, timeout: float = 8.0):
        self.get_image = get_image
        self.roots = tuple(roots)
        self.timeout = timeout

    def _fail(self, code: str, digest: str):
        logger.info("image fetch failed: path=napcat code=%s digest=%s", code, digest)
        raise ImageFetchError(code) from None

    @staticmethod
    def _read_limited(path: str, max_bytes: int) -> bytes:
        """先 stat 判大小、再限量读；超限抛 too_large。

        在线程中同步执行（经 asyncio.to_thread 调用）；文件不可读
        的 OSError 由调用方收敛为 path_rejected。打开前先以 stat
        复验普通文件：FIFO 的大小为 0 却会在 open/read 上永久
        阻塞线程，非普通文件一律 path_rejected（Review P4-2）；
        路径已经 realpath 解析，stat 命中的即最终目标，校验后
        被替换的残余窗口由限量读封顶兜底。
        """
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            raise ImageFetchError("path_rejected")
        if info.st_size > max_bytes:
            raise ImageFetchError("too_large")
        with open(path, "rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ImageFetchError("too_large")
        return data

    async def fetch(self, file_id: str, *, max_bytes: int) -> bytes:
        """取回一张 NapCat 缓存图片；失败收敛为只带 code 的异常。"""
        # digest 用 backslashreplace 编码：file_id 含孤立代理项时
        # 摘要计算不得先于一切门禁抛错（口径与网关 digest 一致，
        # Review P3-1）。
        digest = hashlib.sha256(
            file_id.encode("utf-8", "backslashreplace")
        ).hexdigest()[:16]
        try:
            result = await asyncio.wait_for(
                self.get_image(file_id), timeout=self.timeout
            )
        except (TimeoutError, asyncio.TimeoutError):
            self._fail("napcat_timeout", digest)
        except Exception:  # noqa: BLE001 — 契约：调用失败收敛为结果码
            # CancelledError 是 BaseException、不受此捕捉影响。
            self._fail("napcat_transport", digest)
        if getattr(result, "outcome", None) != "ok":
            self._fail("napcat_rejected", digest)
        data = getattr(result, "data", None)
        raw_path = data.get("file") if isinstance(data, dict) else None
        if not (isinstance(raw_path, str) and raw_path):
            self._fail("napcat_no_path", digest)
        resolver = ResourceResolver(
            allowed_roots=self.roots,
            allowed_exts=self._ALLOWED_EXTS,
            # 大小不在 resolver 口径内判：本 fetcher 的大小权威是
            # _read_limited（stat + 限量读，超限码 too_large）。
            # resolver 自补审第二段 P3 起对本地文件按 stat 大小执
            # 行 max_bytes，若在此传 max_bytes，超限文件会在分类
            # 阶段被误收敛为 path_rejected、破坏 too_large 契约。
        )
        # resolver 内部与 realpath 面对含 NUL/孤立代理项的畸形路径
        # 会抛 ValueError/UnicodeEncodeError（ResourceValidationError
        # 之外）：整段收进同一 try，畸形路径一律 path_rejected，不让
        # 非 ImageFetchError 穿透本类契约（Review P3-1）。_fail 在
        # try 外调用，避免它抛出的 ImageFetchError 被同段误捕。
        try:
            resolved = resolver.resolve(raw_path)
            kind_ok = resolved.kind is ResourceKind.LOCAL_PATH
            path = (
                raw_path[len("file://"):]
                if raw_path.lower().startswith("file://")
                else raw_path
            )
            # resolver 已按 realpath 判 roots/扩展名；读取同样走
            # realpath，避免经符号链接读到校验外目标。
            real_path = os.path.realpath(path) if kind_ok else ""
        except (ResourceValidationError, ValueError, OSError):
            kind_ok, real_path = False, ""
        if not kind_ok:
            self._fail("path_rejected", digest)
        try:
            content = await asyncio.to_thread(
                self._read_limited, real_path, max_bytes
            )
        except ImageFetchError as exc:
            logger.info(
                "image fetch failed: path=napcat code=%s digest=%s",
                exc.code,
                digest,
            )
            raise
        except OSError:
            self._fail("path_rejected", digest)
        if not content:
            # 缓存文件尚未写完/被截断的 0 字节不算取图成功：按
            # 失败收敛，prepare 据此回落 HTTP 兜底（Review P3-2）。
            self._fail("empty", digest)
        logger.info(
            "image fetch ok: path=napcat digest=%s bytes=%s", digest, len(content)
        )
        return content


@dataclass(frozen=True)
class PreparedImages:
    """prepare 的产出：可用的 data URL 与收/跳计数（均为元数据）。

    ``data_urls`` 是完整 base64 载荷，``repr=False`` 防其随对象
    repr 进日志/异常上报（Review F8，与 ResolvedResource.ref 同例）。
    ``skipped`` 统一口径为 received - len(data_urls)：超限、失败、
    非图片、未及尝试的引用都计入，计数永不自相矛盾。
    ``fetch_codes`` 为每条实际处理过的引用汇总一条最终取图结果
    码（只含元数据）：经 NapCat 成功为 ``napcat_ok``、经 HTTP
    成功为 ``http_ok``，失败则沿用取图层既有结果码（如
    ``napcat_no_path`` / ``http_status`` / ``transport`` /
    ``blocked_target`` / ``unknown_type`` / ``too_large_hint``）；
    未及处理的引用（成功/尝试封顶之后）不占条目。
    """

    data_urls: tuple[str, ...] = field(repr=False)
    received: int
    skipped: int
    fetch_codes: tuple[str, ...] = ()


#: prepare 在成功数达上限之外允许的额外尝试数（坏引用不挡后面的
#: 有效引用，Review F9-1；尝试总封顶防引用洪泛放大成下载洪泛）。
_EXTRA_ATTEMPTS = 2


class VisionService:
    """把图片引用序列准备成 data URL，并委托视觉 client 生成。

    ``client`` 只需提供 ``has_credentials()`` 与
    ``generate_with_images(prompt, data_urls)``（即 brain 的
    DeepSeekVisionClient）；无密钥时 prepare 直接返回 None 且不
    发起任何下载，调用方据此走旧固定回复行为。``deadline_seconds``
    是整轮准备的总时限（Review F5）：超时即以已备好的部分收尾，
    不让串行下载把一个回合拖到无界。
    """

    def __init__(
        self,
        *,
        client,
        fetcher,
        max_images: int = 3,
        max_bytes: int = 10 * 1024 * 1024,
        deadline_seconds: float = 25.0,
        napcat_fetcher=None,
    ):
        self.client = client
        self.fetcher = fetcher
        self.max_images = max_images
        self.max_bytes = max_bytes
        self.deadline_seconds = deadline_seconds
        # NapCat 介导取图（主路径）；可构造时传入，也可由装配侧
        # 后置赋值 self.napcat_fetcher，两路都支持。None 时只走
        # 既有 HTTP 取图，行为与接入前一致。
        self.napcat_fetcher = napcat_fetcher

    async def prepare(self, refs) -> PreparedImages | None:
        """逐张下载并嗅探，按顺序尝试至成功数达上限或尝试封顶。

        平台已报告的 size_hint 超限者在下载前直接跳过（Review
        F9-3）；单张失败只跳过该张、不中断其余；整轮受
        deadline_seconds 约束，超时以已备部分收尾。取字节为双
        路径：ref 带 file_id 且装配了 napcat_fetcher 时先走
        NapCat 介导取图，失败落回既有 HTTP 直连兜底；NapCat 取
        回的字节嗅探不出图片类型时同样落回 HTTP 再试一次；一张
        ref 的双路合计仍只计一次 attempts。
        """
        if not self.client.has_credentials():
            return None
        refs = list(refs)
        received = len(refs)
        data_urls: list[str] = []
        fetch_codes: list[str] = []
        attempts = 0
        max_attempts = self.max_images + _EXTRA_ATTEMPTS
        try:
            async with asyncio.timeout(self.deadline_seconds):
                for ref in refs:
                    if len(data_urls) >= self.max_images or attempts >= max_attempts:
                        break
                    size_hint = getattr(ref, "size_hint", None)
                    if isinstance(size_hint, int) and size_hint > self.max_bytes:
                        logger.info(
                            "image prepare skipped: code=too_large_hint bytes=%s",
                            size_hint,
                        )
                        fetch_codes.append("too_large_hint")
                        continue
                    attempts += 1
                    data = None
                    # 字节来源与 HTTP 是否已试：NapCat 字节嗅探
                    # 失败时凭此决定是否回退 HTTP 再试（见下）。
                    data_from_napcat = False
                    http_attempted = False
                    # 本条引用的最终取图结果码：NapCat/HTTP 双路
                    # 中以最后决定该条成败的一路为准；成功分别记
                    # napcat_ok / http_ok，失败沿用取图层既有码。
                    fetch_code: str | None = None
                    # 主路径：NapCat 介导取图（有 fetcher 且 ref 带
                    # file_id 时先试）；失败只记一行 napcat 口径日志，
                    # 落到下面的既有 HTTP 尝试，不直接跳过该张。
                    if self.napcat_fetcher is not None and getattr(
                        ref, "file_id", None
                    ):
                        try:
                            data = await self.napcat_fetcher.fetch(
                                ref.file_id, max_bytes=self.max_bytes
                            )
                            fetch_code = "napcat_ok"
                            data_from_napcat = True
                        except ImageFetchError as exc:
                            logger.info(
                                "image prepare skipped: path=napcat code=%s",
                                exc.code,
                            )
                            fetch_code = exc.code
                            data = None
                        except Exception as exc:  # noqa: BLE001 — 落 HTTP 兜底
                            logger.warning(
                                "image prepare skipped: path=napcat "
                                "code=unexpected error=%s",
                                type(exc).__name__,
                            )
                            fetch_code = "unexpected"
                            data = None
                    if data is None:
                        http_attempted = True
                        try:
                            data = await self.fetcher.fetch(
                                ref.ref, max_bytes=self.max_bytes
                            )
                            fetch_code = "http_ok"
                        except ImageFetchError as exc:
                            logger.info("image prepare skipped: code=%s", exc.code)
                            fetch_codes.append(exc.code)
                            continue
                        except Exception as exc:  # noqa: BLE001 — 单张失败不中断整批
                            logger.warning(
                                "image prepare skipped: code=unexpected error=%s",
                                type(exc).__name__,
                            )
                            fetch_codes.append("unexpected")
                            continue
                    mime = sniff_image_mime(data)
                    if mime is None and data_from_napcat and not http_attempted:
                        # NapCat 缓存字节不是图片（损坏/未写完）不
                        # 算该 ref 的终局：CDN 副本可能完好，按既有
                        # 双路回退口径对 ref 再走一次 HTTP 取图并
                        # 重新嗅探（本 ref 的 attempts 不重复计，
                        # 双路合计一次；复审 P3）。
                        http_attempted = True
                        try:
                            data = await self.fetcher.fetch(
                                ref.ref, max_bytes=self.max_bytes
                            )
                        except ImageFetchError as exc:
                            logger.info(
                                "image prepare skipped: code=%s", exc.code
                            )
                            fetch_codes.append(exc.code)
                            continue
                        except Exception as exc:  # noqa: BLE001 — 单张失败不中断整批
                            logger.warning(
                                "image prepare skipped: code=unexpected error=%s",
                                type(exc).__name__,
                            )
                            fetch_codes.append("unexpected")
                            continue
                        mime = sniff_image_mime(data)
                        if mime is not None:
                            fetch_code = "http_ok"
                    if mime is None:
                        logger.info(
                            "image prepare skipped: code=unknown_type bytes=%s",
                            len(data),
                        )
                        fetch_codes.append("unknown_type")
                        continue
                    logger.info("image prepare ok: mime=%s bytes=%s", mime, len(data))
                    fetch_codes.append(fetch_code or "http_ok")
                    data_urls.append(
                        f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
                    )
        except TimeoutError:
            logger.warning(
                "image prepare deadline: attached=%s received=%s",
                len(data_urls),
                received,
            )
        return PreparedImages(
            data_urls=tuple(data_urls),
            received=received,
            skipped=received - len(data_urls),
            fetch_codes=tuple(fetch_codes),
        )

    async def generate(self, prompt: str, data_urls) -> str | None:
        """委托视觉 client 生成；失败口径由 client 收敛为 None。"""
        return await self.client.generate_with_images(prompt, data_urls)

    async def describe(self, data_urls) -> str | None:
        """生成图片内容的客观描述（Review F7 内容筛查的判定对象）。

        以固定描述提示词委托同一视觉 client，产出供接入层危机
        网关筛查的文本；失败口径与 generate 一致收敛为 None，
        调用方据此按「无法筛查」收敛，绝不跳过筛查直接生成回复。
        """
        return await self.client.generate_with_images(
            IMAGE_DESCRIPTION_PROMPT, data_urls
        )
