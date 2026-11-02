"""VisionConfig（media.vision）配置校验契约。

VisionConfig 有严格边界（见 companion/config.py），但此前没有任何
负例测试：边界被放宽或删除时不会有测试变红。本文件参照
tests/test_config_p1.py 的风格，所有用例都走仓库既有加载路径——
把配置写成临时 YAML 后经 load_settings() 加载（YAML 解析 →
validate_app_config → AppConfig），不断言时直接构造 pydantic
模型绕过加载层；失败断言 ConfigValidationError 且错误点名键路径
（与 test_config_p1 口径一致，错误不回显配置值）。
"""

import pytest
import yaml

from companion.config import ConfigValidationError, load_settings

SYNTHETIC_SECRET = "synthetic-onebot-secret-for-vision-config-tests"


def _load_settings(tmp_path, monkeypatch, raw):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONEBOT_SECRET", SYNTHETIC_SECRET)
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.delenv("NAPCAT_BASE_URL", raising=False)
    return load_settings()


def _raw_with_vision(vision):
    return {
        "allowed_private_user_ids": ["10001"],
        "media": {"vision": vision},
    }


@pytest.mark.parametrize(
    ("vision", "key_path"),
    [
        ({"max_images": 0}, "media.vision.max_images"),
        ({"max_images": 11}, "media.vision.max_images"),
        ({"max_image_bytes": 0}, "media.vision.max_image_bytes"),
        ({"timeout_seconds": 0}, "media.vision.timeout_seconds"),
        (
            {"napcat_timeout_seconds": 0},
            "media.vision.napcat_timeout_seconds",
        ),
        ({"max_image_bytes": 52428801}, "media.vision.max_image_bytes"),
        ({"timeout_seconds": 121}, "media.vision.timeout_seconds"),
        (
            {"napcat_timeout_seconds": 61},
            "media.vision.napcat_timeout_seconds",
        ),
        ({"model": ""}, "media.vision.model"),
        # 非 bool 字符串：StrictBool 必须拒绝（YAML 中 'yes' 以
        # 字符串形态写入，safe_dump 会加引号保真）。
        ({"enabled": "yes"}, "media.vision.enabled"),
        ({"unknown_key": 1}, "media.vision.unknown_key"),
    ],
)
def test_invalid_vision_config_fails_fast_and_names_the_key(
    tmp_path, monkeypatch, vision, key_path
):
    with pytest.raises(ConfigValidationError) as raised:
        _load_settings(tmp_path, monkeypatch, _raw_with_vision(vision))
    assert key_path in str(raised.value)


def test_vision_config_at_upper_bounds_is_accepted(tmp_path, monkeypatch):
    """上界正例：三项恰在文档上界（52428800 / 120 / 60）时必须被
    接受并原样 round-trip，不得被钳制或误拒——与 limit+1 负例成对，
    钉住上界的接受方向。"""
    settings = _load_settings(
        tmp_path,
        monkeypatch,
        _raw_with_vision(
            {
                "max_image_bytes": 52428800,
                "timeout_seconds": 120,
                "napcat_timeout_seconds": 60,
            }
        ),
    )
    vision = settings.config.media.vision
    assert vision.max_image_bytes == 52428800
    assert vision.timeout_seconds == 120
    assert vision.napcat_timeout_seconds == 60


def test_missing_vision_section_gets_documented_defaults(tmp_path, monkeypatch):
    settings = _load_settings(
        tmp_path, monkeypatch, {"allowed_private_user_ids": ["10001"]}
    )
    vision = settings.config.media.vision
    assert vision.enabled is True
    assert vision.model == "deepseek-flash"
    assert vision.max_images == 3
    assert vision.max_image_bytes == 10485760
    assert vision.timeout_seconds == 30
    assert vision.napcat_timeout_seconds == 8
