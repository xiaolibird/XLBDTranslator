# -*- coding: utf-8 -*-
"""系统通知工具：跨脚本复用的 osascript 弹窗（失败静默、测试内静默）。"""
import json
import os
import subprocess


def _under_pytest() -> bool:
    """当前是否跑在 pytest 里。

    `PYTEST_CURRENT_TEST` 由 pytest 在每个用例前后自动设置/清除，是官方认可的
    「我在测试里」判据（比检查 sys.modules 里有没有 pytest 更准——后者在被测代码
    只是 import 了 pytest 时也为真）。
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def notify(title: str, text: str):
    """弹出 macOS 系统通知。

    launchd 无人值守脚本（digest / ingest / backfill / sync_vault）失败时
    只落日志无人翻——通知才是用户真正的告警面。osascript 不可用就静默跳过，
    告警本身不该反过来把任务弄挂。

    **跑在 pytest 里时一律不弹**：`sync_store_best_effort` 这类 best-effort 收尾
    会把任何异常翻译成一条系统通知，而测试正是靠喂残缺对象去触发那条异常路径的
    （`test_read_pdf_cli._FakeSettings` / `test_pdf_ingest._Settings` 都不带 `llm`）。
    不拦的话每跑一次 `pytest test/` 就往用户通知中心推几条
    「向量库同步失败：'_FakeSettings' object has no attribute 'llm'」——
    2026-08-24 用户就是这么被骚扰到来问的。告警面被测试噪音污染，等于告警失效。
    要断言通知内容的用例请直接 monkeypatch 调用方模块里的 `notify` 符号。
    """
    if _under_pytest():
        return
    try:
        subprocess.run(
            # 双引号包裹 AppleScript 字符串：json.dumps 转义双引号与反斜杠即可，
            # 必须 ensure_ascii=False——AppleScript 只认 \" \\ \n \r \t，
            # 默认的 \uXXXX 转义会让 osascript 报 -2741 语法错误（中文标题必挂）
            ["osascript", "-e",
             "display notification {} with title {}".format(
                 json.dumps(text, ensure_ascii=False),
                 json.dumps(title, ensure_ascii=False))],
            capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass
