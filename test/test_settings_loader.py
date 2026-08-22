# -*- coding: utf-8 -*-
"""load_scholar_settings 统一加载器回归（F1 收敛：7 处各自手写的加载归一）。

锁住三件事：
1. **缺失语义分叉**：require=True 抛 FileNotFoundError（交互入口映射退出码），
   require=False 回退默认配置——两态都不允许静默读错文件；
2. **gemini→deepseek 迁移补丁**：只在 patch_gemini=True 且 provider/model 双 gemini
   时生效——cli.py 的 digest 主链路必须能用 patch_gemini=False 保持原行为；
3. **路径锚定**：notes_dir/output_dir 无条件锚到仓库根——从任意 cwd 启动都不能
   写去另一棵目录树（历史上真丢过数据，见 paths.py 模块注释）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.scholar.settings import ScholarSettings, load_scholar_settings  # noqa: E402
from src.scholar import paths as P                                       # noqa: E402


@pytest.fixture
def fake_repo(monkeypatch, tmp_path):
    """把 repo_path 的锚点指向 tmp 假仓库——测试绝不读真实 config/scholar.env。"""
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(P, "REPO_ROOT", tmp_path)
    return tmp_path


def _write_env(repo: Path, text: str = "") -> Path:
    cfg = repo / "config" / "scholar.env"
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_missing_config_require_raises(fake_repo):
    with pytest.raises(FileNotFoundError):
        load_scholar_settings(require=True)


def test_missing_config_falls_back_to_defaults(fake_repo):
    s = load_scholar_settings()
    assert isinstance(s, ScholarSettings)
    # 回退到默认配置也必须锚定，不然默认相对路径照样随 cwd 漂
    assert s.processing.notes_dir.is_absolute()
    assert s.processing.notes_dir == fake_repo / "output" / "scholar_notes"


def test_missing_config_does_not_leak_production_config(fake_repo, monkeypatch):
    """「使用默认配置」必须是**真**默认值，不能把 cwd 下的生产 config 读进来。

    此前用裸 ScholarSettings() 回退，它走类级 model_config.env_file='config/scholar.env'
    ——**cwd 相对**。于是在仓库根跑时，--config 打个错字会从「用默认值、跑不动、立刻
    暴露」变成「照着生产配置连同 API key 和整条回退链真跑起来」，日志还写着"使用默认配置"。
    这里在 cwd 下摆一份"生产 config"，断言它没有被读进来。
    """
    (fake_repo / "config").mkdir(exist_ok=True)
    poison = fake_repo / "cwd_here"
    (poison / "config").mkdir(parents=True)
    (poison / "config" / "scholar.env").write_text(
        "LLM__PROVIDER=claude-agent\nLLM__MODEL=sonnet\n"
        "LLM__GEMINI_API_KEY=SHOULD_NOT_BE_READ\n"
        "LLM__FALLBACK_PROVIDERS=deepseek,gemini\n", encoding="utf-8")
    monkeypatch.chdir(poison)

    s = load_scholar_settings("config/absent.env")
    assert s.llm.provider != "claude-agent", "把 cwd 下的 config 当成默认配置读进来了"
    assert s.llm.model != "sonnet"
    assert not s.llm.api_key, "默认配置不该带 API key"
    assert s.llm.fallback_providers == ""


def test_patch_gemini_applied(fake_repo):
    _write_env(fake_repo, "LLM__PROVIDER=gemini\nLLM__MODEL=gemini-2.5-pro\n")
    s = load_scholar_settings()
    assert s.llm.provider == "deepseek"
    assert s.llm.model == "gemini-2.5-pro"  # 补丁只换 provider，模型名保留供日志追溯


def test_patch_gemini_disabled_keeps_provider(fake_repo):
    """cli.py digest 主链路的契约：patch_gemini=False 时行为与收敛前完全一致。"""
    _write_env(fake_repo, "LLM__PROVIDER=gemini\nLLM__MODEL=gemini-2.5-pro\n")
    s = load_scholar_settings(patch_gemini=False)
    assert s.llm.provider == "gemini"


def test_patch_skipped_when_model_not_gemini(fake_repo):
    """provider=gemini 但模型名不是 gemini 系（人工指定过模型）不打补丁。"""
    _write_env(fake_repo, "LLM__PROVIDER=gemini\nLLM__MODEL=deepseek-v4-flash\n")
    s = load_scholar_settings()
    assert s.llm.provider == "gemini"


def test_paths_anchored_from_foreign_cwd(fake_repo, monkeypatch, tmp_path_factory):
    """从别的 cwd（模拟 launchd cwd 未设对 / 复合命令里 cd 过）加载，路径仍锚仓库根。"""
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    monkeypatch.chdir(elsewhere)
    _write_env(fake_repo,
               "PROCESSING__NOTES_DIR=output/scholar_notes\n"
               "PROCESSING__OUTPUT_DIR=output/scholar_digest\n")
    s = load_scholar_settings()
    assert s.processing.notes_dir == fake_repo / "output" / "scholar_notes"
    assert s.processing.output_dir == fake_repo / "output" / "scholar_digest"
