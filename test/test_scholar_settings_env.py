"""ScholarSettings 环境变量加载回归测试。

背景：嵌套子模型 + validation_alias + env_nested_delimiter='__' 的组合下，
旧模板的平铺变量名（GEMINI_API_KEY 等）曾经全部静默不加载（api_key=None、
一律回退默认值）。本测试固定当前契约：双下划线嵌套命名必须生效。
全部使用假值，不涉及真实密钥。
"""
from pathlib import Path

from src.scholar.schema import ScholarSettings

NESTED_ENV = """
GMAIL__CREDENTIALS_PATH=fake/creds.json
GMAIL__TOKEN_PATH=fake/token.json
LLM__PROVIDER=fake-provider
LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST
LLM__MODEL=fake-model
PROCESSING__BATCH_SIZE=42
PROCESSING__MAX_EMAILS=999
PROCESSING__DAYS_TO_FETCH=3
PROCESSING__OUTPUT_DIR=fake/out
LOG_LEVEL=DEBUG
LOG_FILE=fake/log.log
"""


def _write_env(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "scholar_test.env"
    p.write_text(content, encoding="utf-8")
    return p


def test_nested_env_names_load(tmp_path: Path):
    """模板中的双下划线嵌套命名必须逐项加载，而不是回退默认值"""
    s = ScholarSettings.from_env_file(_write_env(tmp_path, NESTED_ENV))
    assert s.gmail.credentials_path == Path("fake/creds.json")
    assert s.gmail.token_path == Path("fake/token.json")
    assert s.llm.provider == "fake-provider"
    assert s.llm.api_key == "FAKE_KEY_FOR_TEST"
    assert s.llm.model == "fake-model"
    assert s.processing.batch_size == 42
    assert s.processing.max_emails == 999
    assert s.processing.days_to_fetch == 3
    assert s.processing.output_dir == Path("fake/out")
    assert s.log_level == "DEBUG"
    assert s.log_file == Path("fake/log.log")


def test_missing_api_key_stays_none(tmp_path: Path):
    """env 文件不含 key 时 api_key 必须是 None，而不是残留/默认出一个值"""
    s = ScholarSettings.from_env_file(_write_env(tmp_path, "LLM__PROVIDER=gemini\n"))
    assert s.llm.api_key is None


def test_legacy_alias_still_accepted_in_nested_form(tmp_path: Path):
    """兼容用户手写的 LLM__GEMINI_API_KEY（validation_alias 形式）"""
    s = ScholarSettings.from_env_file(
        _write_env(tmp_path, "LLM__GEMINI_API_KEY=FAKE_LEGACY_KEY\nLLM__API_KEY_UNRELATED=x\n")
    )
    assert s.llm.api_key == "FAKE_LEGACY_KEY"
