"""companion.* 日志装配的回归测试。

钉住的故障（2026-10-31 现网）：uvicorn 默认日志配置只配置
uvicorn.*，companion 日志树无 handler 且有效级别为 root 默认的
WARNING，INFO 被直接丢弃、WARNING/ERROR 无 handler 承接，journal
里看不到 companion 的应用日志。装配后 INFO/ERROR 都必须落到
companion logger 的 stderr handler 上，且重复装配不叠加。
"""

import io
import logging

import pytest

from companion.logging_setup import _HANDLER_FLAG, configure_companion_logging


@pytest.fixture
def restore_companion_logger():
    """装配会改全局 logging 状态：测完恢复原 handlers 与级别，
    避免 stderr handler 残留到其它用例造成噪声输出。"""
    logger = logging.getLogger("companion")
    handlers, level = list(logger.handlers), logger.level
    try:
        yield logger
    finally:
        logger.handlers = handlers
        logger.setLevel(level)


def _flagged(logger):
    return [h for h in logger.handlers if getattr(h, _HANDLER_FLAG, False)]


def test_configure_is_idempotent_and_enables_info(restore_companion_logger):
    logger = restore_companion_logger
    configure_companion_logging()
    configure_companion_logging()
    assert len(_flagged(logger)) == 1
    child = logging.getLogger("companion.vision")
    assert child.getEffectiveLevel() == logging.INFO
    assert child.isEnabledFor(logging.INFO)


def test_info_and_error_reach_the_handler_stream(restore_companion_logger):
    logger = restore_companion_logger
    configure_companion_logging()
    (handler,) = _flagged(logger)
    stream = io.StringIO()
    handler.setStream(stream)
    child = logging.getLogger("companion.vision")
    child.info("probe info message")
    child.error("probe error message")
    out = stream.getvalue()
    assert "INFO companion.vision: probe info message" in out
    assert "ERROR companion.vision: probe error message" in out
