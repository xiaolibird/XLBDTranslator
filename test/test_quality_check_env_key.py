"""回归测试：ENABLE_QUALITY_CHECK 的正确 env 键名与文档/模板一致性。

背景（已对抗验证的缺陷）：
- README.md:17 / README.en.md:15 曾教用户写裸键 `ENABLE_QUALITY_CHECK=false` 关闭译后质检。
- 该字段实际定义在 ProcessingSettings（src/core/schema.py），Settings 用
  env_nested_delimiter='__'，正确键名必须带节前缀 `PROCESSING__ENABLE_QUALITY_CHECK`。
- Settings(BaseSettings) 未设置 extra，pydantic-settings 默认 extra='forbid'，
  裸键会在 Settings.from_env_file 阶段抛 ValidationError（extra_forbidden），
  main.py 读配置即崩，一行都翻译不了。

守护点：
1. 裸键 `ENABLE_QUALITY_CHECK=false` 必须仍然导致 ValidationError（证明缺陷的根因
   ——若某天 Settings 改成 extra='allow'，此断言会先失败提醒复核文档）。
2. 带节前缀 `PROCESSING__ENABLE_QUALITY_CHECK=false` 必须能正确生效为 False。
3. README.md / README.en.md 不得再出现裸键 `ENABLE_QUALITY_CHECK=false`
   （必须是 `PROCESSING__ENABLE_QUALITY_CHECK=false`）。
4. config/config.env.template 必须包含该开关的条目，避免用户无文档可查。
"""
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.schema import Settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent

BASE_VARS = (
    "API__GEMINI_API_KEY=fake-key\n"
    "FILES__OUTPUT_BASE_DIR=output\n"
    "PROCESSING__BATCH_SIZE=5\n"
    "LOGGING__LOG_LEVEL=INFO\n"
)


def _env_file(tmp_path: Path, extra: str) -> Path:
    p = tmp_path / f"case_{abs(hash(extra))}.env"
    p.write_text(BASE_VARS + extra, encoding="utf-8")
    return p


def test_bare_enable_quality_check_key_is_rejected(tmp_path: Path):
    """裸键（README 曾教的写法）必须仍然崩，证明该缺陷的根因确实存在。
    schema.py 现在把 extra_forbidden 包装成 ConfigError 并在消息里提示
    SECTION__FIELD 格式，但裸键依然会导致 from_env_file 失败——README 绝不能再教这么写。
    """
    env = _env_file(tmp_path, "ENABLE_QUALITY_CHECK=false\n")
    with pytest.raises(ConfigError, match="ENABLE_QUALITY_CHECK"):
        Settings.from_env_file(env)


def test_prefixed_enable_quality_check_key_works(tmp_path: Path):
    """正确键名 PROCESSING__ENABLE_QUALITY_CHECK 必须能生效。"""
    env_off = _env_file(tmp_path, "PROCESSING__ENABLE_QUALITY_CHECK=false\n")
    s_off = Settings.from_env_file(env_off)
    assert s_off.processing.enable_quality_check is False

    env_on = _env_file(tmp_path, "PROCESSING__ENABLE_QUALITY_CHECK=true\n")
    s_on = Settings.from_env_file(env_on)
    assert s_on.processing.enable_quality_check is True


@pytest.mark.parametrize("readme_name", ["README.md", "README.en.md"])
def test_readme_does_not_teach_bare_key(readme_name: str):
    text = (PROJECT_ROOT / readme_name).read_text(encoding="utf-8")
    assert "PROCESSING__ENABLE_QUALITY_CHECK=false" in text
    # 裸键不应再出现在文档里（避免它前面没有 PROCESSING__ 前缀）
    assert "ENABLE_QUALITY_CHECK=false" not in text.replace(
        "PROCESSING__ENABLE_QUALITY_CHECK=false", ""
    )


def test_config_template_documents_the_switch():
    text = (PROJECT_ROOT / "config" / "config.env.template").read_text(encoding="utf-8")
    assert "PROCESSING__ENABLE_QUALITY_CHECK" in text
