# -*- coding: utf-8 -*-
"""系统通知工具：跨脚本复用的 osascript 弹窗（失败静默）。"""
import json
import subprocess


def notify(title: str, text: str):
    """弹出 macOS 系统通知。

    launchd 无人值守脚本（digest / ingest / backfill / sync_vault）失败时
    只落日志无人翻——通知才是用户真正的告警面。osascript 不可用就静默跳过，
    告警本身不该反过来把任务弄挂。
    """
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
