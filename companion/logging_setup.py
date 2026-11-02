"""companion.* 日志装配：由进程入口（main.py）在装配应用前调用一次。

背景（实证，2026-10-31）：各业务模块只写
``logging.getLogger("companion.*")``，全仓从未给这棵日志树装过
handler 或级别。生产以 ``python -m uvicorn main:app`` 启动时，
uvicorn 的默认 dictConfig 只配置 ``uvicorn`` / ``uvicorn.error`` /
``uvicorn.access`` 三个 logger，既不动 root 也不动 ``companion``。
于是 ``companion.*`` 的有效级别停留在 root 默认的 WARNING、整条
祖先链上没有任何 handler：

- INFO 记录在 logger 处就被 ``isEnabledFor(INFO) == False`` 丢弃，
  永远到不了进程输出（journal 里因此只有 uvicorn 自己的行）；
- WARNING / ERROR 没有任何已配置的 handler 承接，只能靠
  ``logging.lastResort`` 以「只有消息、没级别没 logger 名」的裸行
  漏到 stderr，不成其为可用的 journal 日志。

修复口径：只给 ``companion`` 这棵树装配——级别 INFO + 一个写
stderr 的 StreamHandler（systemd 单元同时把 stdout/stderr 收进
journal；stderr 亦与 uvicorn 默认 handler 的去向一致）。root 与
第三方库的级别一概不动，避免把无关库的 INFO 一并放大。
注：下文「不会双写」以 root 无 handler 为前提，若未来引入 root 级 basicConfig 需复核。
"""

import logging
import sys

#: 本模块所装 handler 的标记属性名；幂等判定只认带此标记的
#: handler，不误删/不误判其它代码装配的 handler。
_HANDLER_FLAG = "_companion_stream_handler"


def configure_companion_logging(level: int = logging.INFO) -> logging.Logger:
    """给 ``companion`` logger 设定级别并装一个 stderr handler。

    幂等：重复调用不会叠加 handler（已有本模块所装 handler 时只
    确保级别后原样返回）。不改 ``propagate``：记录仍向 root 传播，
    pytest caplog 等既有观测方式不受影响；祖先链上自此恒有
    handler 承接，``logging.lastResort`` 不再介入，不会双写。
    """
    logger = logging.getLogger("companion")
    logger.setLevel(level)
    for handler in logger.handlers:
        if getattr(handler, _HANDLER_FLAG, False):
            return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(levelname)s %(name)s: %(message)s")
    )
    setattr(handler, _HANDLER_FLAG, True)
    logger.addHandler(handler)
    return logger
