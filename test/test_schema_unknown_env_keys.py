"""
回归测试：Settings.from_env_file 对未知 env 键的分类。

修复前 from_env_file 对任何不在 _known_env_keys 里的键统一打一条「将被忽略」的
warning，然后无条件 `return cls(_env_file=...)`。但 Settings 的 extra 是
pydantic-settings 默认的 'forbid'——两类未知键实际行为完全相反：

- 带合法 SECTION__ 前缀但字段名手滑（如 PROCESSING__MAX_CONTEXT_LENGT）：
  pydantic-settings 确实静默忽略这个键，程序仍能正常启动，warning 属实。
- 不带合法 SECTION__ 前缀（如漏写 API__ 前缀写成 TRANSLATOR_PROVIDER=deepseek）：
  extra='forbid' 会让 cls(_env_file=...) 直接抛裸 pydantic ValidationError
  终止程序——是致命错误，被说成「将被忽略」反而把排查方向引向错误的一侧。

修复后：合法前缀+字段名手滑 -> 仍是 warning，正常启动；
       无合法前缀 -> 提前抛出可读的 ConfigError，而不是让用户看见裸 pydantic
       traceback，也不再谎称「已忽略」。
"""
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.schema import Settings

# Settings 的四个子配置均为必填：env 文件每一节至少要出现一个变量
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


def test_known_key_still_loads_cleanly(tmp_path: Path):
    """基线：没有多余键时正常加载，不受本次改动影响。"""
    s = Settings.from_env_file(_env_file(tmp_path, ""))
    assert s.processing.batch_size == 5


def test_misspelled_field_with_legal_prefix_only_warns_and_still_loads(tmp_path: Path, caplog):
    """合法 SECTION__ 前缀但字段名手滑：应继续被静默忽略（warning 提示），
    程序仍能正常启动——这条是 pydantic-settings 的真实行为，不能被本次修复破坏。"""
    env_path = _env_file(tmp_path, "PROCESSING__MAX_CONTEXT_LENGT=999\n")
    with caplog.at_level("WARNING"):
        s = Settings.from_env_file(env_path)
    assert s is not None
    assert any("PROCESSING__MAX_CONTEXT_LENGT" in rec.message for rec in caplog.records)
    assert any("将被忽略" in rec.message for rec in caplog.records)


def test_key_without_legal_prefix_raises_readable_config_error(tmp_path: Path):
    """[medium] 回归：漏写 SECTION__ 前缀的键必须提前抛出可读的 ConfigError，
    而不是让用户看到裸 pydantic ValidationError，也不能被误标「将被忽略」后
    仍然继续往下跑。"""
    env_path = _env_file(tmp_path, "TRANSLATOR_PROVIDER=deepseek\n")
    with pytest.raises(ConfigError) as exc_info:
        Settings.from_env_file(env_path)
    message = str(exc_info.value)
    assert "TRANSLATOR_PROVIDER" in message
    # 明确说的是「会导致启动失败」，不能再是「将被忽略」这种误导性措辞
    assert "忽略" not in message


def test_key_without_legal_prefix_does_not_log_ignored_warning(tmp_path: Path, caplog):
    """同一场景下不应再打出「将被忽略」的温和提示（此前的误导性行为）。"""
    env_path = _env_file(tmp_path, "TRANSLATOR_PROVIDER=deepseek\n")
    with caplog.at_level("WARNING"):
        with pytest.raises(ConfigError):
            Settings.from_env_file(env_path)
    assert not any("TRANSLATOR_PROVIDER" in rec.message and "将被忽略" in rec.message
                   for rec in caplog.records)


def test_export_prefixed_key_is_not_a_false_positive(tmp_path: Path):
    """回归：手写 `line.split('=', 1)` 解析会把 `export API__GEMINI_MODEL=x` 的
    键名误判成 "EXPORT API__GEMINI_MODEL"（未知、且无合法 SECTION__ 前缀），
    进而错误地抛出 ConfigError——但 python-dotenv/裸 pydantic 加载对 `export `
    前缀是合法支持的，不应被本次的未知键门禁拦截。"""
    env_path = _env_file(tmp_path, "export API__GEMINI_MODEL=gemini-2.5-pro\n")
    s = Settings.from_env_file(env_path)
    assert s.api.gemini_model == "gemini-2.5-pro"


@pytest.mark.parametrize("extra", [
    "HTTP_PROXY=\n",
    "FOO=\n",
    'PROXY_URL=""\n',
    "FOO\n",  # 裸键，无等号
    "API_KEY=sk-xxx\n",  # 与 api 字段 env 名 "API" 撞前缀但缺 __
    "APIX=1\n",
    "FILESY=1\n",
], ids=[
    "empty_http_proxy", "empty_foo", "empty_quoted_proxy_url",
    "bare_key_no_eq", "api_key_prefix_collision", "apix_prefix_collision",
    "filesy_prefix_collision",
])
def test_empty_value_and_prefix_collision_keys_load_cleanly(tmp_path: Path, extra: str):
    """[medium] 回归 opus 审计发现的假阳性：pydantic-settings 2.12 的 dotenv
    provider 对空值键（`FOO=`/裸 `FOO`/`PROXY_URL=""`）在 `if not env_value:
    continue` 处直接跳过，对键名前缀撞上某个顶层字段 env 名但缺 `__` 的键
    （`API_KEY`/`APIX`/`FILESY` 撞上 api/files 字段的 env 名 API/FILES）走
    `env_name.startswith(field_env_name)` 纯前缀匹配静默丢弃——这两类键
    pydantic 从不报 extra_forbidden。旧的预测式守卫会把这些原本能正常启动的
    config.env 错判为致命 ConfigError；修复后必须权威地不报错、正常加载。"""
    env_path = _env_file(tmp_path, extra)
    s = Settings.from_env_file(env_path)
    assert s.processing.batch_size == 5


def test_illegal_top_level_key_error_includes_correct_hint(tmp_path: Path):
    """[medium] 真正的非法顶层键（示例用 opus 指出的 ENABLE_QUALITY_CHECK，
    该键实际归属 PROCESSING 节但漏写了 SECTION__ 前缀）必须被 pydantic 权威
    裁决为 extra_forbidden，并被转成含「XXX → SECTION__XXX」具体修正建议的
    ConfigError——不是泛泛的"缺前缀"提示，而是动态从 ProcessingSettings 字段
    生成的准确落点。"""
    env_path = _env_file(tmp_path, "ENABLE_QUALITY_CHECK=false\n")
    with pytest.raises(ConfigError) as exc_info:
        Settings.from_env_file(env_path)
    message = str(exc_info.value)
    assert "ENABLE_QUALITY_CHECK" in message
    assert "PROCESSING__ENABLE_QUALITY_CHECK" in message
    assert "忽略" not in message


def test_translator_provider_hint_points_to_api_section(tmp_path: Path):
    """既有回归用例的补强：确认 hint 映射对 TRANSLATOR_PROVIDER 给出的正确
    落点是 API__TRANSLATOR_PROVIDER（而不仅仅是原样把键名塞进消息里）。"""
    env_path = _env_file(tmp_path, "TRANSLATOR_PROVIDER=deepseek\n")
    with pytest.raises(ConfigError) as exc_info:
        Settings.from_env_file(env_path)
    assert "TRANSLATOR_PROVIDER → API__TRANSLATOR_PROVIDER" in str(exc_info.value)


def test_multiline_quoted_value_is_not_a_false_positive(tmp_path: Path):
    """回归：手写解析按行切分，会把带换行的引号取值
    `API__OPENAI_BASE_URL="https://a\\nfoo=bar\\n"` 中的 "foo" 当成独立的未知
    键行，进而误判为无合法前缀而抛出 ConfigError。dotenv_values 与 pydantic 走
    同一套解析器，应把整个引号块识别为一个值。"""
    env_path = _env_file(
        tmp_path, 'API__OPENAI_BASE_URL="https://a\nfoo=bar\n"\n'
    )
    s = Settings.from_env_file(env_path)
    assert s.api.openai_base_url == "https://a\nfoo=bar\n"


# ============ document_path 校验契约（自 test_error_paths.py 迁入） ============


def test_settings_document_path_validation_contract(tmp_path: Path):
    """document_path 走真实加载路径（from_env_file）的契约：
    - 未提供时允许为 None（main.py 先 from_env_file 构造、后由 builder 注入路径）
    - 指向不存在的文件 -> 校验失败（FileNotFoundError 包装为 ValidationError）
    - 不支持的扩展名 -> 校验失败
    """
    from pydantic import ValidationError

    # 未提供 document_path 是合法初始状态
    s = Settings.from_env_file(_env_file(tmp_path, ""))
    assert s.files.document_path is None

    # 不存在的文件必须拒绝（before-validator 抛出的 FileNotFoundError 原样传播）
    with pytest.raises(FileNotFoundError):
        Settings.from_env_file(
            _env_file(tmp_path, f"FILES__DOCUMENT_PATH={tmp_path / 'missing.pdf'}\n")
        )

    # 不支持的格式必须拒绝
    bad = tmp_path / "doc.docx"
    bad.write_bytes(b"fake")
    with pytest.raises(ValidationError):
        Settings.from_env_file(_env_file(tmp_path, f"FILES__DOCUMENT_PATH={bad}\n"))
