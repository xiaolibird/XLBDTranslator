# -*- coding: utf-8 -*-
"""notify() 的 AppleScript 拼接回归测试。

历史缺陷：json.dumps 默认 ensure_ascii=True 把中文转成 \\uXXXX，
AppleScript 字符串只认 \\" \\\\ \\n \\r \\t，遇到 \\u 直接报
'syntax error (-2741)'，而 check=False+capture_output 把失败吞成无声——
全部 9 处调用点标题都是中文，launchd 无人值守链路的唯一告警面从未弹过。
"""
import shutil
import subprocess
import sys

import pytest

import src.utils.notify as notify_mod
from src.utils.notify import notify


def _capture_cmd(monkeypatch):
    """替换 notify 模块内的 subprocess.run，截获它拼出的 osascript 命令。

    顺带**显式退出 pytest 静默**：notify 现在跑在测试里会直接 return（2026-08-24 加，
    防止 best-effort 路径的异常把测试噪音推进用户通知中心，见 notify 的 docstring）。
    本文件这几个用例要验的恰恰是「真发出去时拼的命令对不对」，所以必须先摘掉那道闸。
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    calls = []
    monkeypatch.setattr(notify_mod.subprocess, "run",
                        lambda cmd, *a, **k: calls.append(cmd))
    return calls


def test_chinese_text_kept_literal_no_unicode_escape(monkeypatch):
    """中文必须原样进 AppleScript，绝不能出现 \\uXXXX 转义。"""
    calls = _capture_cmd(monkeypatch)
    notify("札记入库失败", "2026-08 月份处理异常，详见日志")

    assert calls and calls[0][0] == "osascript"
    script = calls[0][2]
    assert "札记入库失败" in script
    assert "2026-08 月份处理异常" in script
    assert "\\u" not in script  # ensure_ascii=True 的产物，一出现就是回归


def test_quotes_and_backslashes_still_escaped(monkeypatch):
    """ensure_ascii=False 不能丢掉 json.dumps 对双引号/反斜杠的转义——
    那是防注入与防语法错误的另一半。"""
    calls = _capture_cmd(monkeypatch)
    notify('标题带"引号"', "正文带\\反斜杠")

    script = calls[0][2]
    assert '\\"引号\\"' in script
    assert "\\\\反斜杠" in script


@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("osacompile") is None,
                    reason="需要 macOS 的 osacompile 做真实语法校验")
def test_generated_applescript_actually_compiles(monkeypatch, tmp_path):
    """终极防线：把 notify 实际拼出的脚本交给 osacompile 做真语法编译。
    修复前（\\uXXXX）这里必然 -2741 语法错误，修复后必须编译通过。
    用 osacompile 而非 osascript：只验语法，不真弹通知打扰用户。"""
    real_run = subprocess.run  # notify_mod.subprocess 就是本模块的 subprocess，补丁会一起换掉
    calls = _capture_cmd(monkeypatch)
    notify("周报生成失败", "ingest_notes --auto 抛异常：连接超时")

    script = calls[0][2]
    result = real_run(
        ["osacompile", "-e", script, "-o", str(tmp_path / "n.scpt")],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_osascript_missing_is_silent(monkeypatch):
    """osascript 不可用时静默跳过——告警本身不该把任务弄挂。"""
    def _boom(*a, **k):
        raise OSError("osascript not found")
    monkeypatch.setattr(notify_mod.subprocess, "run", _boom)
    notify("标题", "正文")  # 不抛即通过


# ---------------- pytest 内静默（2026-08-24 加） ----------------

def test_silent_under_pytest(monkeypatch):
    """跑在 pytest 里绝不真弹通知。

    这不是洁癖：`embed_store.sync_store_best_effort` 会把任何异常翻译成一条系统通知，
    而测试正是靠喂残缺对象去触发那条异常路径的（`test_read_pdf_cli._FakeSettings` /
    `test_pdf_ingest._Settings` 都不带 `llm`）。不拦的话每跑一次全量测试就往用户通知
    中心推几条「向量库同步失败：'_FakeSettings' object has no attribute 'llm'」——
    2026-08-24 用户就是被这个骚扰到来问的。告警面被测试噪音污染，等于告警失效。
    """
    calls = []
    monkeypatch.setattr(notify_mod.subprocess, "run", lambda *a, **k: calls.append(a))
    notify("不该出现的标题", "不该出现的正文")
    assert calls == [], "跑在 pytest 里却真的执行了 osascript"


def test_best_effort_sync_failure_does_not_notify_in_tests(monkeypatch):
    """端到端复现那条真实骚扰路径：残缺 settings → 异常 → best-effort 兜底 → 本应弹窗。

    钉住的是「测试环境下这条路不弹」，而不是「这条路不存在」——生产里它必须照弹。
    """
    from src.scholar.embed_store import sync_store_best_effort

    calls = []
    monkeypatch.setattr(notify_mod.subprocess, "run", lambda *a, **k: calls.append(a))

    class _NoLlm:            # 与 test_read_pdf_cli._FakeSettings 同款：没有 .llm
        pass

    got = sync_store_best_effort("/nonexistent", {"papers": []}, _NoLlm(),
                                 notify_title="Scholar 手动精读", context="手动精读归档")
    assert got is None       # 契约：异常吞掉、返回 None、不改调用方退出码
    assert calls == [], "测试里触发 best-effort 失败竟然真弹了系统通知"
