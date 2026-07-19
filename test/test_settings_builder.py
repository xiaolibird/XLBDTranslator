from pathlib import Path

import pytest

from src.core.schema import (APISettings, FileSettings, LoggingSettings,
                             ProcessingSettings, Settings)
from src.workflow.builder import PRESETS, SettingsBuilder


def make_minimal_settings(tmp_path: Path) -> Settings:
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4\n%fake")

    # Use model_construct to avoid full validation side-effects during tests
    api = APISettings.model_construct(gemini_api_key="fakekey", gemini_model="gemini-2.0-flash")
    files = FileSettings.model_construct(document_path=doc, output_base_dir=tmp_path / "out", modes_config_path=Path("config/modes.json"))
    processing = ProcessingSettings.model_construct()
    logging = LoggingSettings.model_construct()

    settings = Settings.model_construct(api=api, files=files, processing=processing, logging=logging)
    return settings


def test_use_preset_applies_modifications(tmp_path: Path):
    base = make_minimal_settings(tmp_path)
    builder = SettingsBuilder(base)

    settings = builder.use_preset('fast').build()

    # fast preset sets batch_size to 10
    assert settings.processing.batch_size == 10


def test_gemini_model_mapping_and_document_path(tmp_path: Path):
    base = make_minimal_settings(tmp_path)
    builder = SettingsBuilder(base)

    settings = builder.gemini_model('gemini-1.5-pro').document_path(tmp_path / 'doc.pdf').build()

    assert settings.api.gemini_model == 'gemini-1.5-pro'
    assert settings.files.document_path.name == 'doc.pdf'


def test_invalid_batch_size_raises(tmp_path: Path):
    base = make_minimal_settings(tmp_path)
    builder = SettingsBuilder(base)

    builder.batch_size(-5)
    with pytest.raises(ValueError):
        builder.build()


# ==================== preset 键落位回归 ====================
#
# 背景：_apply_setting 曾经只手工枚举约 20 个字段，PRESETS 里的 temperature/top_p/
# glossary_* 等"质量类"核心旋钮落到 else 分支被 logger.debug 静默丢弃——设了等于没设。
# 现在改为数据驱动的 _SETTING_ROUTES 路由表，这里对每个预设档位抽查若干代表性键，
# 断言它们真实落到了 built settings 的对应字段上，值与 PRESETS 定义一致。

# 抽查键：覆盖生成参数、分块、术语表三类此前会被丢弃的字段
REPRESENTATIVE_KEYS = [
    "temperature", "top_p", "top_k", "max_output_tokens",
    "min_chunk_size", "max_chunk_size",
    "glossary_min_terms", "glossary_max_terms", "glossary_preamble_ratio",
    "glossary_stop_threshold", "skip_pretranslate_if_glossary_exists",
    "reprocess_pretranslated", "enable_progressive_glossary",
]


@pytest.mark.parametrize("preset_name", list(PRESETS.keys()))
def test_preset_representative_keys_land_on_settings(tmp_path: Path, preset_name: str):
    base = make_minimal_settings(tmp_path)
    settings = SettingsBuilder(base).use_preset(preset_name).build()

    preset = PRESETS[preset_name]
    checked = 0
    for key in REPRESENTATIVE_KEYS:
        if key not in preset:
            continue
        checked += 1
        assert getattr(settings.processing, key) == preset[key], (
            f"预设 {preset_name} 的 {key} 未正确落位到 settings.processing"
        )
    # 每个预设都定义了这些键，抽查不应为空（否则测试本身失去意义）
    assert checked > 0


@pytest.mark.parametrize("preset_name", list(PRESETS.keys()))
def test_preset_all_keys_land_somewhere(tmp_path: Path, preset_name: str):
    """更严格的兜底：预设里的每个键都必须真实落到某个 settings 子对象上（不再静默丢弃）"""
    from src.workflow.builder import _SETTING_ROUTES

    base = make_minimal_settings(tmp_path)
    settings = SettingsBuilder(base).use_preset(preset_name).build()

    preset = PRESETS[preset_name]
    for key, expected in preset.items():
        if key == "description":
            continue
        assert key in _SETTING_ROUTES, f"预设键 {key} 没有路由，会被静默丢弃"
        sub_obj_name, field_name = _SETTING_ROUTES[key]
        actual = getattr(getattr(settings, sub_obj_name), field_name)
        assert actual == expected, f"预设 {preset_name} 的 {key} 落位后值不一致: {actual!r} != {expected!r}"
