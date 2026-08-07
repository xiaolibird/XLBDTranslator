# -*- coding: utf-8 -*-
"""回归测试：PromptManager 的完整版/简化版选择不能只看**主** provider。

缺陷（support.py:515）：PromptManager.__init__ 用 settings.api.translator_provider
（主 provider）判断 is_cloud_provider，白名单漏了 'ollama'。当主 provider=ollama
（本地零成本主力）+ FALLBACK_PROVIDERS=deepseek,gemini 时，回退链里实例化的
云端 OpenAICompatibleTranslator（deepseek）/GeminiTranslator/ClaudeAgentTranslator
读的是同一个 settings.api.translator_provider，全部被误判为"本地"，拿到丢失反
翻译腔规则、引号/省略号规则、术语加粗规则的简化版 prompt，回退前后译文风格与
标点口径不一致。

修复：
1. support.py is_cloud_provider 集合补上 'ollama'（否则主 provider=ollama 时
   is_cloud_provider 恒为 False，殃及同链所有云端实例）。
2. engine.py OpenAICompatibleTranslator 把 self.prompt_manager 的创建挪到
   self.is_local 算出之后，显式传 force_simple=self.is_local——本实例是否
   真的本地（Ollama）不再依赖主 provider 设置，而由自己的 base_url 检测结果
   决定，与 ClaudeAgentTranslator 的 force_simple 用法对称。
"""
from types import SimpleNamespace

from src.core.schema import Settings
from src.translator.engine import OpenAICompatibleTranslator
from src.translator.support import PromptManager


def _full_system_instruction() -> str:
    from pathlib import Path
    path = Path(__file__).parent.parent / "config" / "prompts" / "system_instruction.md"
    return path.read_text(encoding="utf-8")


def _simple_system_instruction() -> str:
    from pathlib import Path
    path = Path(__file__).parent.parent / "config" / "prompts" / "system_instruction_simple.md"
    return path.read_text(encoding="utf-8")


def _make_settings(translator_provider: str, **api_overrides) -> Settings:
    api = {"TRANSLATOR_PROVIDER": translator_provider}
    api.update(api_overrides)
    return Settings(api=api, files={}, processing={}, logging={})


# ==================== 1. PromptManager 单元级：主 provider=ollama 也算"云端默认" ====================

def test_prompt_manager_is_cloud_provider_includes_ollama():
    """主 provider='ollama' 且 force_simple 未强制时，is_cloud_provider 判定为
    True——ollama 本身通过 force_simple 显式降级，而不是靠白名单漏掉它。"""
    settings = SimpleNamespace(
        api=SimpleNamespace(translator_provider="ollama"),
        processing=SimpleNamespace(translation_mode_entity=None),
    )
    pm = PromptManager(settings, force_simple=False)
    assert pm.system_instruction_base == _full_system_instruction()
    assert pm.system_instruction_base != _simple_system_instruction()


def test_prompt_manager_ollama_main_with_force_simple_still_gets_simple():
    """force_simple=True 时（Ollama 实例自己的场景），即便 is_cloud_provider
    因主 provider=ollama 而判 True，仍必须拿简化版——force_simple 优先。"""
    settings = SimpleNamespace(
        api=SimpleNamespace(translator_provider="ollama"),
        processing=SimpleNamespace(translation_mode_entity=None),
    )
    pm = PromptManager(settings, force_simple=True)
    assert pm.system_instruction_base == _simple_system_instruction()


# ==================== 2. OpenAICompatibleTranslator 接线级：按实例而非主 provider ====================

def test_cloud_fallback_gets_full_prompt_when_main_provider_is_ollama():
    """核心回归：主 provider=ollama，回退链里的云端 openai-compatible/deepseek
    实例必须拿完整版 prompt，不能因为主 provider 是本地而被连坐降级。"""
    settings = _make_settings(
        "ollama",
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://api.deepseek.com",
        OPENAI_MODEL="deepseek-v4-flash",
    )
    fallback = OpenAICompatibleTranslator(settings, provider="openai-compatible")
    assert fallback.is_local is False
    assert fallback.prompt_manager.system_instruction_base == _full_system_instruction()


def test_ollama_instance_gets_simple_prompt_even_when_main_provider_is_cloud():
    """反向场景：主 provider=deepseek（云端），但回退链/本机里的 ollama 实例
    自己必须始终拿简化版 prompt，不因主 provider 是云端而被误判成完整版。"""
    settings = _make_settings(
        "deepseek",
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://api.deepseek.com",
        OPENAI_MODEL="deepseek-v4-flash",
    )
    ollama_translator = OpenAICompatibleTranslator(settings, provider="ollama")
    assert ollama_translator.is_local is True
    assert ollama_translator.prompt_manager.system_instruction_base == _simple_system_instruction()


def test_ollama_main_provider_ollama_instance_itself_still_gets_simple():
    """主 provider=ollama 且实例化的也是 ollama 自身：必须仍是简化版
    （回归修复不能把 ollama 自己也带偏成完整版）。"""
    settings = _make_settings("ollama")
    ollama_translator = OpenAICompatibleTranslator(settings, provider="ollama")
    assert ollama_translator.is_local is True
    assert ollama_translator.prompt_manager.system_instruction_base == _simple_system_instruction()
