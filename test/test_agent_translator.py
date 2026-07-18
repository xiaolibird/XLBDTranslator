# -*- coding: utf-8 -*-
"""ClaudeAgentTranslator 回归（mock subprocess，不真调 claude CLI）：

- envelope 解析：result 文本抽出 → 翻译 JSON 正常映射回 segment
- CLI 退出码非零 / is_error / 空 result → APIError
- 超时 → APITimeoutError
- CLI 缺失 → APIAuthenticationError（保证 fallback 链能跳过）
- usage limit 错误文本能被 classify_fatal 识别为 soft
"""
import json
import subprocess

import pytest

from src.core.exceptions import APIError, APIAuthenticationError, APITimeoutError
from src.core.schema import Settings
from src.translator import agent as agent_mod
from src.translator.agent import ClaudeAgentTranslator
from src.translator.fallback import classify_fatal


def _make_translator(monkeypatch):
    monkeypatch.setattr(agent_mod.shutil, "which", lambda name: "/usr/local/bin/claude")
    settings = Settings.model_construct()
    monkeypatch.setattr(
        ClaudeAgentTranslator, "__init__",
        _patched_init, raising=True,
    )
    return ClaudeAgentTranslator(settings)


def _patched_init(self, settings):
    # 绕过 PromptManager（需要完整 Settings/prompts 文件），只保留 CLI 相关状态
    self.settings = settings
    self.cli_path = "/usr/local/bin/claude"
    self.model = "sonnet"
    self.use_long_text_mode = True
    self.is_local = False
    self.is_deepseek = False
    self._async_translator = None
    self._cwd = "/tmp"


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_cli_extracts_result(monkeypatch):
    t = _make_translator(monkeypatch)
    envelope = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": '[{"id": 1, "translation": "你好"}]',
    })
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return _proc(stdout=envelope)

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)
    out = t._run_cli("translate this")
    assert out == '[{"id": 1, "translation": "你好"}]'
    assert captured["cmd"][:2] == ["/usr/local/bin/claude", "-p"]
    assert "--model" in captured["cmd"]
    assert captured["input"] == "translate this"


def test_run_cli_nonzero_exit_raises(monkeypatch):
    t = _make_translator(monkeypatch)
    monkeypatch.setattr(agent_mod.subprocess, "run",
                        lambda *a, **k: _proc(returncode=1, stderr="boom"))
    with pytest.raises(APIError, match="退出码 1"):
        t._run_cli("x")


def test_run_cli_error_envelope_raises(monkeypatch):
    t = _make_translator(monkeypatch)
    envelope = json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "You've reached your usage limit",
    })
    monkeypatch.setattr(agent_mod.subprocess, "run", lambda *a, **k: _proc(stdout=envelope))
    with pytest.raises(APIError) as ei:
        t._run_cli("x")
    # 订阅额度耗尽 → fallback 分类为 soft（连续多次才判死）
    assert classify_fatal(ei.value) == 'soft'


def test_run_cli_empty_result_raises(monkeypatch):
    t = _make_translator(monkeypatch)
    envelope = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": ""})
    monkeypatch.setattr(agent_mod.subprocess, "run", lambda *a, **k: _proc(stdout=envelope))
    with pytest.raises(APIError, match="空 result"):
        t._run_cli("x")


def test_run_cli_timeout_raises(monkeypatch):
    t = _make_translator(monkeypatch)

    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=600)

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)
    with pytest.raises(APITimeoutError):
        t._run_cli("x")


def test_missing_cli_raises_auth_error(monkeypatch):
    monkeypatch.setattr(agent_mod.shutil, "which", lambda name: None)
    with pytest.raises(APIAuthenticationError, match="claude CLI"):
        ClaudeAgentTranslator(Settings.model_construct())


def test_chat_completions_merges_system_instruction(monkeypatch):
    t = _make_translator(monkeypatch)
    seen = {}

    def fake_run_cli(prompt, extra_args=None):
        seen["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(t, "_run_cli", fake_run_cli)
    assert t._chat_completions("SYS", "USER") == "ok"
    assert seen["prompt"].startswith("SYS")
    assert seen["prompt"].endswith("USER")
    # 长文本模式：system 为空时不加分隔
    assert t._chat_completions("", "USER-ONLY") == "ok"
    assert seen["prompt"] == "USER-ONLY"
