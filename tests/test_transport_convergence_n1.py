"""N1 传输收敛契约（方案 §3.1/C15）：静态断言层。

- companion/tools/__init__.py 不再直连 HTTP：不 import httpx、不含
  OneBot 动作 URL 路径拼接（发送/探活一律经 adapters 的
  ActionGateway + OneBotTransport）。
- 全树中只有 adapters/transport.py 可以构造动作 URL；既有独立只读
  探针 ops/connection_health.py 与 ops/watchdog.py 各自带一份
  "/get_status" 字面量（独立进程/模块的健康探针，不经运行时发送链，
  allowlist 注明理由保留）。adapters/onebot.py 对 get_status 只按
  动作名引用（get_action("get_status")），不算 URL 字面量。
- 回执映射只有一份：receipt_from_result 定义在 adapters/transport.py，
  adapters/onebot.py 与 services/delivery.py 都 import 它，且两文件
  不再各自定义 _ACCEPTED_STATUSES / _REJECTED_STATUSES。
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPANION = ROOT / "companion"

pytestmark = pytest.mark.timeout(10)

_URL_PATH_LITERALS = ("/send_private_msg", "/get_status")
_URL_LITERAL_ALLOWLIST = {
    # 唯一运行时传输：按动作名拼 {base}/<action>。
    COMPANION / "adapters" / "transport.py",
    # 既有独立只读探针（连接健康模块）：自带 get_status 探针实现，
    # 不经运行时发送链，N1 不收编（方案 C16 健康语义保留）。
    COMPANION / "ops" / "connection_health.py",
    # 既有独立只读探针（watchdog 进程）：同上，自带 get_status 探针。
    COMPANION / "ops" / "watchdog.py",
}


def _companion_sources() -> dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(COMPANION.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def test_tools_client_no_longer_imports_httpx_or_builds_action_urls():
    source = (COMPANION / "tools" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "httpx" not in imported
    for literal in (
        "/send_private_msg",
        "/send_group_msg",
        "/publish_message",
        "/set_friend_add_request",
        "/get_status",
    ):
        assert literal not in source, literal


def test_action_url_literals_only_in_allowlisted_files():
    offenders = {
        path
        for path, source in _companion_sources().items()
        if any(literal in source for literal in _URL_PATH_LITERALS)
    }
    assert offenders <= _URL_LITERAL_ALLOWLIST, offenders


def test_receipt_mapping_has_a_single_definition():
    sources = _companion_sources()
    definitions = [
        path
        for path, source in sources.items()
        if "def receipt_from_result" in source
    ]
    assert definitions == [COMPANION / "adapters" / "transport.py"]
    for relative in ("adapters/onebot.py", "services/delivery.py"):
        source = sources[COMPANION / relative]
        assert "receipt_from_result" in source, relative
        assert "from companion.adapters.transport import" in source, relative
        assert "_ACCEPTED_STATUSES" not in source, relative
        assert "_REJECTED_STATUSES" not in source, relative
