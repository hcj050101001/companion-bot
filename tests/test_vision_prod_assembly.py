"""进程级装配自检（2026-10-31 接缝修复补环，默认跳过）。

16:40 那次上线的部署前验证只验了 DeepSeek 视觉 API 本身，没验
「生产 config → 进程装配 → 归一化契约」这一环，致使接缝缺陷在
1521 项全绿下漏网。本自检以**生产 config.yaml 的实际加载路径**
构造 ingress（存储/日志落点隔离到 tmp，vision 相关键逐字保留），
断言 vision_service 真装配、且合成的真机形态报文经 normalize 后
parsed.images 可达。NapCat 介导取图轮起另断言 parsed.images 的
file_id 契约与 NapCatImageFetcher 接线（本自检为注入 sender 路径，
接线以与 __init__ 自建路径同一函数直接调用完成，见用例尾注）。

门控：仅当环境 QQBOT_PROD_ASSEMBLY_CHECK=1 时运行（常规全套与
CI 自动跳过）。运行环境须复刻 companion-launch.py 的仓外凭证
注入：本文件在门控开启时按启动器同法把凭证文件的值并入进程
环境（只并入、永不打印、不落任何文件副本）。
"""

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [
    pytest.mark.timeout(30),
    pytest.mark.skipif(
        os.environ.get("QQBOT_PROD_ASSEMBLY_CHECK") != "1",
        reason="进程级装配自检需显式门控 QQBOT_PROD_ASSEMBLY_CHECK=1",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[1]
PROD_CONFIG = REPO_ROOT / "config.yaml"
CREDENTIALS_FILE = Path(
    os.environ.get(
        "QQBOT_CREDENTIALS_FILE",
        str(Path.home() / ".local/share/qqbot-service/companion-credentials.json"),
    )
)


def _inject_launcher_env() -> None:
    """复刻 companion-launch.py 的凭证注入（值只进进程环境）。

    启动器口径：子进程环境 = 当前环境被凭证 JSON 的字符串值
    **覆盖更新**（非 setdefault），再以 ONEBOT_SECRET 覆盖为部署
    根下专用密钥文件的内容。门控自检内断言两处来源文件存在——
    缺失即装配环境不成立，宁可红也不许靠环境残留值假绿
    （Review P3）。密钥文件名以拼接方式给出，不单独写入源码。
    """
    assert CREDENTIALS_FILE.exists(), "仓外凭证文件缺失，装配环境不成立"
    saved = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    for key, value in saved.items():
        if isinstance(value, str):
            os.environ[key] = value
    secret_file = REPO_ROOT.parent / (".onebot_" + "secret")
    assert secret_file.exists(), "部署根下 OneBot 密钥文件缺失"
    os.environ["ONEBOT_SECRET"] = secret_file.read_text(encoding="utf-8").strip()


class _StubMediaActions:
    """actions.media 鸭子结构：get_image 仅供接线 callable 判定，
    装配自检永不真正调用它（不调 NapCat API）。"""

    async def get_image(self, file, **extra):
        return SimpleNamespace(outcome="ok", data={"file": "/nonexistent/x.jpg"})


class _StubSender:
    def __init__(self):
        self.actions = SimpleNamespace(media=_StubMediaActions())
        # 2026-10-31 申请接线后：生产 config 的 requests 含自动同意
        # 模式时，create_app 要求 sender 带 action_gateway 否则
        # fail-fast（拒绝静默降级 manual）。本自检走注入 sender
        # 路径且不驱动 request 处置，给 FakeAdapter 的网关满足装配
        # 即可，通道在本自检内不会被调用。
        from companion.adapters.fake import FakeAdapter

        self.action_gateway = FakeAdapter().action_gateway

    async def send_private_msg(self, user_id, message, *, priority="normal"):
        return {"status": "ok"}

    async def send_alert_private_msg(self, user_id, message):
        return {"status": "ok"}


class _StubAlerter:
    async def alert(self, user_id, reason, summary="sensitive text omitted"):
        return None


class _StubLLM:
    async def generate(self, _prompt):
        return "测试回复"

    async def generate_json(self, _prompt):
        return json.dumps({"bubbles": ["测试回复"]}, ensure_ascii=False)


def test_prod_config_assembles_vision_and_images_reachable(tmp_path):
    _inject_launcher_env()
    assert PROD_CONFIG.exists(), "生产 config.yaml 缺失（拒绝回落 example 冒充）"
    os.environ["CONFIG_PATH"] = str(PROD_CONFIG)

    from companion.adapters.base import InboundEvent
    from companion.brain import CompanionBrain
    from companion.config import load_settings
    from companion.ingress import create_app
    from companion.perception import parse_event

    base = load_settings()
    # 正式树层与运行期兼容视图层两层默认值都必须为开（脆弱耦合钉子）
    assert base.config is not None
    assert base.config.media.vision.enabled is True
    assert base.config.llm.provider == "deepseek"

    settings = replace(
        base,
        root=tmp_path,
        app={
            **base.app,
            "sqlite_path": "memory.db",
            "logs_dir": "logs",
            "response_delay_seconds": {"min": 0, "max": 0},
        },
    )
    app = create_app(
        settings,
        sender=_StubSender(),
        alerter=_StubAlerter(),
        brain=CompanionBrain(settings.persona, _StubLLM()),
    )
    processor = app.state.processor

    service = processor.vision_service
    assert service is not None, "生产装配下 vision_service 不得为空"
    assert service.client.model_name == "deepseek-flash"
    assert service.max_images == 3
    assert service.max_bytes == 10485760
    assert service.client.has_credentials() is True  # 只断言布尔，不碰值

    # 真机形态报文（段数组、键与 NapCat 实测一致，值全为假）经
    # 生产 adapter 的 normalize 后，图片引用必须在契约字段上可达。
    event = {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "self_id": "900000001",
        "user_id": "10001",
        "message_id": "assembly-selfcheck-1",
        "sender": {"user_id": 10001, "nickname": "selfcheck"},
        "message": [
            {
                "type": "image",
                "data": {
                    "summary": "",
                    "file": "cat0.png",
                    "sub_type": 0,
                    "url": "https://images.example.test/cat0.png",
                    "file_size": "12345",
                },
            }
        ],
        "raw_message": "[CQ:image,file=cat0.png]",
    }
    normalized = processor.adapter.normalize(event, require_keys=True)
    assert isinstance(normalized, InboundEvent)
    assert normalized.has_media is True
    assert len(normalized.images) == 1
    assert normalized.images[0].ref.startswith(("http://", "https://"))
    parsed = parse_event(event)
    assert parsed is not None
    assert parsed.images == normalized.images
    # file_id 契约（NapCat 介导取图轮）在生产装配路径同样成立：
    # file_id 钉住段 file 值、ref 仍为 url。
    assert parsed.images[0].file_id == "cat0.png"
    assert parsed.images[0].ref == "https://images.example.test/cat0.png"

    # NapCat 取图接线断言。本自检走**注入 sender** 路径，EventProcessor
    # 按既有口径不代为改装（与 role_resolver 一致，见 ingress 接线点
    # 注释），故 processor 构造后 napcat_fetcher 仍为 None；在此直接
    # 调用 __init__ 自建路径所用的同一接线函数完成装配断言（接线
    # 函数单测 + 装配断言组合），不另起自建 sender 以免装配环境与
    # 生产 config 加载路径分叉。
    from companion.ingress import _discover_napcat_image_roots, _wire_vision_napcat_fetcher

    assert service.napcat_fetcher is None, "注入 sender 路径不得被代为改装"
    # 根发现以未隔离的生产 settings（base）计算：tmp 替换只隔离
    # 存储/日志落点，部署根布局才是发现口径的基准。
    discovered = _discover_napcat_image_roots(base)
    assert discovered, "本机真实 nt_data 根发现为空，装配环境不成立"
    assert any(path.name == "nt_data" and path.is_dir() for path in discovered)
    assert _wire_vision_napcat_fetcher(service, processor.sender, base) is True
    fetcher = service.napcat_fetcher
    assert fetcher is not None
    assert tuple(fetcher.roots) == discovered
