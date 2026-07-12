from pathlib import Path

import pytest

from src.core.schema import (APISettings, FileSettings, LoggingSettings,
                             ProcessingSettings, Settings)
from src.workflow.builder import SettingsBuilder


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
