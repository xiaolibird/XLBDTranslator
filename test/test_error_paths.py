import io
from pathlib import Path

import pytest
import streamlit as st

from src.workflow.builder import SettingsBuilder
from web.components.model_selector import ModelSelector
from web.services.backend_adapter import BackendAdapter


def test_modelselector_no_api_key_returns_defaults():
    ModelSelector.clear_cache()
    models = ModelSelector.get_multimodal_models(api_key=None)
    assert isinstance(models, list)
    assert len(models) > 0


def test_settingsbuilder_unknown_preset_raises(tmp_path: Path):
    base = tmp_path / "doc.pdf"
    base.write_bytes(b"%PDF-1.4\n%fake")
    from src.core.schema import (APISettings, FileSettings, LoggingSettings,
                                 ProcessingSettings, Settings)
    api = APISettings.model_construct(gemini_api_key="k", gemini_model="m")
    files = FileSettings.model_construct(document_path=base, output_base_dir=tmp_path / "out", modes_config_path=Path("config/modes.json"))
    processing = ProcessingSettings.model_construct()
    logging = LoggingSettings.model_construct()
    settings = Settings.model_construct(api=api, files=files, processing=processing, logging=logging)

    builder = SettingsBuilder(settings)
    with pytest.raises(ValueError):
        builder.use_preset('this-does-not-exist')


def test_settings_document_path_validation_contract(tmp_path: Path):
    """document_path 走真实加载路径（from_env_file）的契约：
    - 未提供时允许为 None（main.py 先 from_env_file 构造、后由 builder 注入路径）
    - 指向不存在的文件 -> 校验失败（FileNotFoundError 包装为 ValidationError）
    - 不支持的扩展名 -> 校验失败
    """
    from pydantic import ValidationError
    from src.core.schema import Settings

    # Settings 的四个子配置均为必填：env 文件每一节至少要出现一个变量
    base_vars = (
        "API__GEMINI_API_KEY=fake-key\n"
        "FILES__OUTPUT_BASE_DIR=output\n"
        "PROCESSING__BATCH_SIZE=5\n"
        "LOGGING__LOG_LEVEL=INFO\n"
    )

    def env_file(extra: str = "") -> Path:
        p = tmp_path / f"case_{abs(hash(extra))}.env"
        p.write_text(base_vars + extra, encoding="utf-8")
        return p

    # 未提供 document_path 是合法初始状态
    s = Settings.from_env_file(env_file())
    assert s.files.document_path is None

    # 不存在的文件必须拒绝（before-validator 抛出的 FileNotFoundError 原样传播）
    with pytest.raises(FileNotFoundError):
        Settings.from_env_file(env_file(f"FILES__DOCUMENT_PATH={tmp_path / 'missing.pdf'}\n"))

    # 不支持的格式必须拒绝
    bad = tmp_path / "doc.docx"
    bad.write_bytes(b"fake")
    with pytest.raises(ValidationError):
        Settings.from_env_file(env_file(f"FILES__DOCUMENT_PATH={bad}\n"))


def test_backendadapter_missing_token_or_file_shows_none(monkeypatch):
    st.session_state.clear()
    # no api token
    st.session_state['uploaded_files'] = []
    adapter = BackendAdapter()
    result = adapter.build_settings_from_ui()
    assert result is None
