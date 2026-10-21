# -*- coding: utf-8 -*-
"""macOS 原生弹窗（osascript）的薄封装：入库确认等需要「问用户一句」的场景共用。

设计约束（用户 2026-10-21 明确：不做 web，不做 TUI，只用系统原生弹窗/通知）：

1. **可替换**：所有 osascript 调用都经 `OsaDialogs` 这一个对象；业务代码只依赖它的
   `choose / ask / notify / open_path / available` 五个方法，测试里换成桩即可，
   绝不真弹窗、真发通知。
2. **默认后端在 pytest 里自毁**：`_default_runner` 检测到 `PYTEST_CURRENT_TEST` 直接抛错，
   防止某个用例漏了打桩时往用户屏幕上弹框（`utils.notify` 同款防线，但这里是抛错而非静默——
   弹窗是同步阻塞的，静默吞掉会让测试逻辑悄悄走错分支）。
3. **转义**：AppleScript 字符串只认 `\\"` 与 `\\\\`（以及 `\\n \\r \\t`）。文献标题里引号、反斜杠、
   换行、制表符都可能出现，先把控制字符压成空格，再用 json.dumps(ensure_ascii=False) 转义——
   不能用默认的 \\uXXXX（osascript 报 -2741，中文必挂，notify.py 的旧坑）。
4. **超时分两层**：`display dialog` 自带 `giving up after N`（干净地返回 gave up:true）；
   `choose from list` 没有该属性，只能靠 subprocess 的 timeout 杀进程——两种都归为
   「无人应答」，由调用方挂起。
5. **返回值不靠逗号切分**：`choose from list` 的多选结果被 osascript 打印成 "a, b, c"，
   标题里本来就有逗号，无法还原。脚本里用 ASCII 31（unit separator）显式拼接。
"""
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

SEP = chr(31)                       # 多选结果的分隔符（标题里已清掉控制字符，不会冲突）
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f  ]+")

# runner(args, timeout) -> (returncode, stdout, stderr)；超时抛 subprocess.TimeoutExpired
Runner = Callable[[List[str], Optional[float]], Tuple[int, str, str]]


def clean(s) -> str:
    """压掉控制字符（含换行/制表）与首尾空白；None → 空串。"""
    return _CTRL_RE.sub(" ", str(s if s is not None else "")).strip()


def as_str(s) -> str:
    """Python 字符串 → AppleScript 字符串字面量（带双引号）。"""
    return json.dumps(clean(s), ensure_ascii=False)


def as_list(items: Sequence[str]) -> str:
    """Python 列表 → AppleScript 列表字面量。"""
    return "{" + ", ".join(as_str(x) for x in items) + "}"


@dataclass
class ChooseResult:
    """status: ok（selected 可为空列表=用户一条都没选）/ cancel / timeout / error。"""
    status: str
    selected: List[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class AskResult:
    """status: ok / timeout / error；gave_up=True 表示 giving up after 到点（无人应答）。"""
    status: str
    button: Optional[str] = None
    gave_up: bool = False
    detail: str = ""


def _default_runner(args: List[str], timeout: Optional[float]) -> Tuple[int, str, str]:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("测试中禁止真实 osascript 调用：请给 OsaDialogs 注入桩 runner / 后端")
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    return r.returncode, r.stdout, r.stderr


class OsaDialogs:
    """真实后端。runner 可注入（测试断言脚本文本用）；业务测试更常见的是整体换成 FakeDialogs。"""

    def __init__(self, runner: Optional[Runner] = None):
        self._run = runner or _default_runner

    # ---- 环境 ----
    @staticmethod
    def available() -> bool:
        """当前是否有人坐在这台机器的图形会话前（登录用户 == 控制台属主）。

        launchd 用户代理在登录会话里跑，/dev/console 属主即当前登录的 GUI 用户；
        SSH / 退出登录 / 切到别的用户时不成立，此时弹窗只会失败或排队不显示。
        只是尽力判断：判错也无害——弹窗超时即挂起。
        """
        if sys.platform != "darwin":
            return False
        try:
            return os.stat("/dev/console").st_uid == os.getuid()
        except OSError:
            return False

    # ---- 弹窗 ----
    def choose(self, items: Sequence[str], defaults: Sequence[str] = (), *, title: str, prompt: str,
               ok: str = "确认本页", cancel: str = "暂不确认",
               timeout_s: Optional[float] = None) -> ChooseResult:
        """多选列表。items 必须互不相同（调用方保证，通常带序号前缀）。"""
        script = build_choose_script(items, defaults, title=title, prompt=prompt, ok=ok, cancel=cancel)
        try:
            rc, out, err = self._run(["osascript", "-e", script], timeout_s)
        except subprocess.TimeoutExpired:
            return ChooseResult("timeout")
        except OSError as e:
            return ChooseResult("error", detail=str(e))
        if rc != 0:
            return ChooseResult("error", detail=(err or out).strip()[:300])
        return parse_choose_output(out)

    def ask(self, message: str, buttons: Sequence[str], default_button: Optional[str] = None, *,
            title: str = "Scholar", giving_up_s: Optional[int] = None,
            timeout_s: Optional[float] = None) -> AskResult:
        """多按钮提问（display dialog）。不设 cancel 按钮，Esc 无效，避免 -128 异常分支。"""
        script = build_ask_script(message, buttons, default_button, title=title, giving_up_s=giving_up_s)
        try:
            rc, out, err = self._run(["osascript", "-e", script], timeout_s)
        except subprocess.TimeoutExpired:
            return AskResult("timeout", gave_up=True)
        except OSError as e:
            return AskResult("error", detail=str(e))
        if rc != 0:
            return AskResult("error", detail=(err or out).strip()[:300])
        return parse_ask_output(out)

    # ---- 通知 / 打开文件 ----
    def notify(self, title: str, text: str) -> None:
        """系统通知。复用 utils.notify（失败静默、pytest 内不弹）。"""
        from .notify import notify
        notify(title, text)

    def open_path(self, path) -> bool:
        """用系统默认程序打开文件（看摘要 md）。失败返回 False，不抛。"""
        try:
            rc, _, _ = self._run(["open", str(path)], 20)
        except (OSError, subprocess.SubprocessError):
            return False
        return rc == 0


# ---------------- 脚本生成 / 输出解析（纯函数，测试直接断言） ----------------

def build_choose_script(items: Sequence[str], defaults: Sequence[str], *, title: str, prompt: str,
                        ok: str, cancel: str) -> str:
    lines = ["set theItems to " + as_list(items)]
    clause = ""
    if defaults:
        lines.append("set theDefaults to " + as_list(defaults))
        clause = " default items theDefaults"
    lines.append(
        "set sel to choose from list theItems with title {t} with prompt {p}{d} "
        "OK button name {ok} cancel button name {cn} "
        "with multiple selections allowed and empty selection allowed".format(
            t=as_str(title), p=as_str(prompt), d=clause, ok=as_str(ok), cn=as_str(cancel)))
    lines += [
        'if sel is false then return "CANCEL"',
        "set AppleScript's text item delimiters to (ASCII character 31)",
        "set joined to sel as text",
        'set AppleScript\'s text item delimiters to ""',
        'return "OK:" & joined',
    ]
    return "\n".join(lines)


def parse_choose_output(out: str) -> ChooseResult:
    s = (out or "").rstrip("\r\n")
    if s == "CANCEL":
        return ChooseResult("cancel")
    if s.startswith("OK:"):
        body = s[3:]
        return ChooseResult("ok", body.split(SEP) if body else [])
    return ChooseResult("error", detail="无法解析 osascript 输出：{!r}".format(s[:120]))


def build_ask_script(message: str, buttons: Sequence[str], default_button: Optional[str], *,
                     title: str, giving_up_s: Optional[int]) -> str:
    # message 允许多行：换行在 as_str 里会被压成空格，这里按行拼接 return 字符
    parts = [as_str(m) for m in str(message).split("\n")]
    msg = " & return & ".join(parts)
    d = " default button {}".format(as_str(default_button)) if default_button else ""
    g = " giving up after {}".format(int(giving_up_s)) if giving_up_s else ""
    return "\n".join([
        "set r to display dialog ({m}) with title {t} buttons {b}{d}{g} with icon note".format(
            m=msg, t=as_str(title), b=as_list(buttons), d=d, g=g),
        'return (button returned of r) & "|" & (gave up of r)',
    ])


def parse_ask_output(out: str) -> AskResult:
    s = (out or "").rstrip("\r\n")
    btn, _, gave = s.rpartition("|")
    if not _ or gave not in ("true", "false"):
        return AskResult("error", detail="无法解析 osascript 输出：{!r}".format(s[:120]))
    if gave == "true":
        return AskResult("timeout", button=None, gave_up=True)
    return AskResult("ok", button=btn, gave_up=False)
