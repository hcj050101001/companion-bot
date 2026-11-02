"""OneBotTransport：全仓唯一的 NapCat HTTP 出口（方案 §3.1 / N1）。

由现行 HttpOneBotClient 的传输部分收敛而来：只负责 POST/GET
``{base}/<action>`` 与响应 dict 的取得，不做门控、不做记账、不重试。
一切传输异常与异形（非 dict）响应收敛为 :class:`TransportError`，
其消息只含动作名与异常类型名——绝不含 URL 参数与请求/响应载荷，
防签名 URL、凭证类参数混入日志与异常链。

``extract_platform_message_id`` 与 ``receipt_from_result`` 是全仓唯一
一份「平台/网关结果 dict → 平台消息 id / 回执三态」实现，语义与
adapters/onebot.py 现行 ``_response_message_id`` / ``send_private``
逐项一致；onebot.py 的旧实现随窗口 B 的收编删除后以本模块为准。
归一逻辑在本模块自带实现，不 import onebot（避免循环依赖）。
"""

import httpx

from companion.adapters.base import SendReceipt

_ACCEPTED_STATUSES = {"ok", "async", "dry_run"}
_REJECTED_STATUSES = {"rate_limited", "disabled", "unavailable", "busy"}


class TransportError(Exception):
    """传输失败：消息只含动作名与异常类型名，绝不含 URL 参数/载荷。"""

    def __init__(self, action: str, error_type: str | None = None):
        self.action = action
        self.error_type = error_type or "TransportError"
        super().__init__(
            f"transport failed for action {action} ({self.error_type})"
        )


def _normalize_message_id(value) -> str | None:
    """message_id 归一（与 onebot._id_text(allow_negative=True) 同语义）。

    仅接受 str/int（不含 bool）；有符号 int32 范围内的负数放行，0 与
    容器/浮点/bool 等异形值判无效。
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, int):
        if value == 0 or not -(2**31) <= value <= 2**31 - 1:
            return None
    text = str(value).strip()
    return text or None


def extract_platform_message_id(result) -> str | None:
    """从 OneBot 响应提取平台 message_id（data.message_id 优先），无则 None。"""
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    candidates = []
    if isinstance(data, dict):
        candidates.append(data.get("message_id"))
    candidates.append(result.get("message_id"))
    for value in candidates:
        normalized = _normalize_message_id(value)
        if normalized is not None:
            return normalized
    return None


def receipt_from_result(result) -> SendReceipt:
    """全仓唯一的结果 dict → 回执三态映射（与 onebot.send_private 同口径）。

    None → accepted；status ∈ {ok, async, dry_run} → accepted（带平台
    message_id）；status ∈ {rate_limited, disabled, unavailable, busy}
    → rejected；其余/异形/非 dict → ambiguous（绝不猜 accepted）。
    """
    if result is None:
        return SendReceipt("accepted")
    if not isinstance(result, dict):
        return SendReceipt("ambiguous")
    status = result.get("status")
    if not isinstance(status, str):
        return SendReceipt("ambiguous")
    if status in _ACCEPTED_STATUSES:
        return SendReceipt("accepted", extract_platform_message_id(result))
    if status in _REJECTED_STATUSES:
        return SendReceipt("rejected")
    return SendReceipt("ambiguous")


class OneBotTransport:
    """NapCat HTTP 传输：POST/GET ``{base}/<action>``，无门控、无重试。"""

    def __init__(self, base_url: str = "http://127.0.0.1:3000", *, timeout: float = 10.0):
        self.base_url = str(base_url).rstrip("/")
        self.timeout = timeout

    async def post_action(self, action: str, params: dict | None = None) -> dict:
        """POST 一个动作并返回响应 dict；任何失败抛 TransportError。"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/{action}", json=params or {}
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001 — 收敛为只含类型名的 TransportError
            raise TransportError(action, type(exc).__name__) from None
        if not isinstance(payload, dict):
            raise TransportError(action, type(payload).__name__) from None
        return payload

    async def get_action(self, action: str) -> dict:
        """GET 一个动作并返回响应 dict；口径与 post_action 一致。"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/{action}")
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001 — 收敛为只含类型名的 TransportError
            raise TransportError(action, type(exc).__name__) from None
        if not isinstance(payload, dict):
            raise TransportError(action, type(payload).__name__) from None
        return payload
