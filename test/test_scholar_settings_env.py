"""ScholarSettings 环境变量加载回归测试。

背景：嵌套子模型 + validation_alias + env_nested_delimiter='__' 的组合下，
旧模板的平铺变量名（GEMINI_API_KEY 等）曾经全部静默不加载（api_key=None、
一律回退默认值）。本测试固定当前契约：双下划线嵌套命名必须生效。
全部使用假值，不涉及真实密钥。
"""
import re
from pathlib import Path

import pytest

from src.scholar.schema import ScholarSettings

REPO_ROOT = Path(__file__).resolve().parents[1]

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


# ---------------------------------------------------------------- 裸键路由缺陷回归
#
# ProcessingSettings 的这四个字段曾在 AliasChoices 里带一个大写裸键别名
# （"BATCH_SIZE"/"MAX_EMAILS"/"DAYS_TO_FETCH"/"OUTPUT_DIR"），而 config/scholar.env
# （真实在用的配置）恰恰写的就是这四个裸键——env_nested_delimiter='__' 下，顶层裸键
# 从不会被路由进 processing 子模型，被 extra='ignore' 静默吞掉；模板文件反而是对的
# （PROCESSING__ 前缀）。用户改 DAYS_TO_FETCH 想拉长回溯窗口时，改了却没生效、也没
# 有任何报错。

def test_bare_top_level_processing_keys_are_silently_ignored(tmp_path: Path):
    """文档化当前 pydantic-settings 行为：不带 PROCESSING__ 前缀的裸键不会路由进
    嵌套子模型，直接被 extra='ignore' 吞掉——这正是 bug 的根因。"""
    env = _write_env(
        tmp_path,
        "BATCH_SIZE=77\nMAX_EMAILS=88\nDAYS_TO_FETCH=99\nOUTPUT_DIR=output/XX\n",
    )
    s = ScholarSettings.from_env_file(env)
    assert s.processing.batch_size == 5             # 默认值：裸键完全没生效
    assert s.processing.max_emails == 100
    assert s.processing.days_to_fetch == 7
    assert s.processing.output_dir == Path("output/scholar_digest")


def test_real_scholar_env_uses_nested_prefix_for_processing_keys():
    """锁定真实配置文件：这四项必须写成 PROCESSING__ 前缀形式，不能退回裸键——
    否则用户按注释改配置会静默不生效。"""
    env_path = REPO_ROOT / "config" / "scholar.env"
    if not env_path.exists():
        pytest.skip("本机无真实 config/scholar.env（gitignore，CI/干净环境必缺），跳过实配审计")
    content = env_path.read_text(encoding="utf-8")
    for bare in ("BATCH_SIZE", "MAX_EMAILS", "DAYS_TO_FETCH", "OUTPUT_DIR"):
        assert not re.search(r"(?m)^{}=".format(bare), content), (
            "config/scholar.env 不应再出现裸键 {}=（须加 PROCESSING__ 前缀，"
            "否则被静默忽略）".format(bare))
        assert "PROCESSING__{}=".format(bare) in content


def test_real_scholar_env_processing_values_actually_take_effect(tmp_path: Path):
    """把真实 config/scholar.env 的四个处理配置项替换成非默认值后加载，确认它们
    确实生效——防止"文件里写的是 PROCESSING__ 前缀但恰好等于默认值，掩盖了
    路由仍然失效"的假阴性。"""
    env_path = REPO_ROOT / "config" / "scholar.env"
    if not env_path.exists():
        pytest.skip("本机无真实 config/scholar.env（gitignore，CI/干净环境必缺），跳过实配审计")
    content = env_path.read_text(encoding="utf-8")
    patched = content
    patched = re.sub(r"(?m)^PROCESSING__BATCH_SIZE=.*$",
                     "PROCESSING__BATCH_SIZE=77", patched)
    patched = re.sub(r"(?m)^PROCESSING__MAX_EMAILS=.*$",
                     "PROCESSING__MAX_EMAILS=88", patched)
    patched = re.sub(r"(?m)^PROCESSING__DAYS_TO_FETCH=.*$",
                     "PROCESSING__DAYS_TO_FETCH=99", patched)
    patched = re.sub(r"(?m)^PROCESSING__OUTPUT_DIR=.*$",
                     "PROCESSING__OUTPUT_DIR=output/XX", patched)
    assert patched != content, "替换未命中任何一行，测试本身失真"

    s = ScholarSettings.from_env_file(_write_env(tmp_path, patched))
    assert s.processing.batch_size == 77
    assert s.processing.max_emails == 88
    assert s.processing.days_to_fetch == 99
    assert s.processing.output_dir == Path("output/XX")
