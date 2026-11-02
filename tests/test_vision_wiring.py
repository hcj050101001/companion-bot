"""视觉 NapCat 取图的生产接线守卫（确认复审 P2-1 / P3-3）。

上一轮教训：接线函数本身有测试、调用点却无默认套件覆盖时，删掉
接线仍会全绿、只能靠真机发现。本文件在默认套件内钉住：

1. EventProcessor 自建 sender 路径（sender=None）真的调用接线：
   部署根同级 napcat-v4 缓存树存在时，vision_service 的
   napcat_fetcher 被装上且 roots 含发现到的 nt_data 目录；
2. 生产 sender_policies 给 get_image 的限流额度：网关默认
   6 次/60 秒会被多图回合打满（单回合尝试上限即 5 次）——经
   真实 ActionGateway + FakeTransport 连续调用 8 次不得被限流；
3. _discover / _wire 的口径单测（相对解析、空串跳过、去重、
   glob 发现；缺 get_image / 缺根时跳过并留结果码日志）；
4. 注入 sender 时不接管注入的 vision_service（与 role_resolver
   同一段的既有口径）。
"""

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from companion.adapters.fake import FakeTransport
from companion.config import load_settings
from companion.ingress import (
    EventProcessor,
    _discover_napcat_image_roots,
    _wire_vision_napcat_fetcher,
)
from companion.vision import ImageFetcher, VisionService

pytestmark = pytest.mark.timeout(10)


@pytest.fixture
def deploy_settings(tmp_path, monkeypatch):
    """root = tmp_path/repo 的 Settings；部署根同级可放 napcat-v4 树。"""
    monkeypatch.setenv("ONEBOT_SECRET", "vision-wiring-secret")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ALERT_QQ", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("CONFIG_PATH", str(root / "config.yaml"))
    (root / "config.yaml").write_text(
        yaml.safe_dump({"allowed_private_user_ids": ["10001"]}), encoding="utf-8"
    )
    base = load_settings()
    return replace(
        base,
        onebot_secret="vision-wiring-secret",
        dev=False,
        root=root,
        app={
            **base.app,
            "allowed_private_user_ids": ["10001"],
            "sqlite_path": "memory.db",
            "logs_dir": "logs",
            "response_delay_seconds": {"min": 0, "max": 0},
        },
    )


def _nt_data(tmp_path, name="nt_qq_ab12"):
    path = tmp_path / "napcat-v4" / "home" / ".config" / "QQ" / name / "nt_data"
    path.mkdir(parents=True)
    return path


def test_processor_self_built_sender_wires_napcat_fetcher(deploy_settings, tmp_path):
    nt_data = _nt_data(tmp_path)
    processor = EventProcessor(deploy_settings)
    service = processor.vision_service
    assert service is not None
    assert service.napcat_fetcher is not None
    assert nt_data in service.napcat_fetcher.roots


def test_processor_get_image_policy_survives_multi_image_turns(deploy_settings):
    processor = EventProcessor(deploy_settings)
    gateway = processor.sender.action_gateway
    # 额度本身钉住：低于视觉单回合尝试上限的数倍即视为回退。
    assert gateway.policies["get_image"]["rate_limit"] >= 30
    gateway.transport = FakeTransport()

    async def _calls():
        return [
            await processor.sender.actions.media.get_image(f"img-{i}.png")
            for i in range(8)
        ]

    results = asyncio.run(_calls())
    assert all(result.outcome == "ok" for result in results)


class _StubSender:
    async def send_private_msg(self, user_id, message, *, priority="normal"):
        return {"status": "ok"}

    async def send_alert_private_msg(self, user_id, message):
        return {"status": "ok"}


def test_processor_injected_sender_does_not_rewire_vision(deploy_settings, tmp_path):
    _nt_data(tmp_path)  # 树存在也不许接管注入路径
    service = VisionService(client=SimpleNamespace(), fetcher=ImageFetcher())
    EventProcessor(deploy_settings, sender=_StubSender(), vision_service=service)
    assert service.napcat_fetcher is None


def _fake_settings(root, vision_cfg):
    return SimpleNamespace(
        root=root, app={"media": {"vision": vision_cfg}}
    )


def test_discover_config_roots_relative_blank_and_dedup(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    settings = _fake_settings(
        root, {"image_local_roots": ["cache", "", "   ", "cache", "/abs/cache"]}
    )
    found = _discover_napcat_image_roots(settings)
    assert found == (root / "cache", Path("/abs/cache"))


def test_discover_globs_nt_data_dirs_sorted(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    qq_dir = tmp_path / "napcat-v4" / "home" / ".config" / "QQ"
    (qq_dir / "nt_qq_b" / "nt_data").mkdir(parents=True)
    (qq_dir / "nt_qq_a" / "nt_data").mkdir(parents=True)
    (qq_dir / "other").mkdir()
    settings = _fake_settings(root, {})
    found = _discover_napcat_image_roots(settings)
    assert found == (qq_dir / "nt_qq_a" / "nt_data", qq_dir / "nt_qq_b" / "nt_data")


def test_wire_skips_when_sender_has_no_get_image(tmp_path):
    service = VisionService(client=SimpleNamespace(), fetcher=ImageFetcher())
    sender = SimpleNamespace(actions=SimpleNamespace(media=SimpleNamespace()))
    settings = _fake_settings(tmp_path / "repo", {})
    assert _wire_vision_napcat_fetcher(service, sender, settings) is False
    assert service.napcat_fetcher is None


def test_wire_skips_with_warning_when_no_roots(tmp_path, caplog):
    async def get_image(file_id):
        return SimpleNamespace(outcome="ok", data={})

    service = VisionService(client=SimpleNamespace(), fetcher=ImageFetcher())
    sender = SimpleNamespace(
        actions=SimpleNamespace(media=SimpleNamespace(get_image=get_image))
    )
    settings = _fake_settings(tmp_path / "repo", {})
    with caplog.at_level(logging.WARNING, logger="companion.events"):
        assert _wire_vision_napcat_fetcher(service, sender, settings) is False
    assert service.napcat_fetcher is None
    assert "code=no_roots" in caplog.text


def test_wire_success_uses_configured_roots_and_timeout(tmp_path):
    cached = tmp_path / "cache"
    cached.mkdir()

    async def get_image(file_id):
        return SimpleNamespace(outcome="ok", data={})

    service = VisionService(client=SimpleNamespace(), fetcher=ImageFetcher())
    sender = SimpleNamespace(
        actions=SimpleNamespace(media=SimpleNamespace(get_image=get_image))
    )
    settings = _fake_settings(
        tmp_path / "repo",
        {"image_local_roots": [str(cached)], "napcat_timeout_seconds": 5},
    )
    assert _wire_vision_napcat_fetcher(service, sender, settings) is True
    assert service.napcat_fetcher is not None
    assert service.napcat_fetcher.roots == (cached,)
    assert service.napcat_fetcher.timeout == 5.0
