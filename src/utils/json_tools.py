"""JSON 文本预处理工具（跨 translator/scholar 共用）"""
import json
import re
from typing import Any, Optional


def strip_code_fences(text: str) -> str:
    """剥掉 LLM 响应外层的 markdown 代码围栏（```json ... ```）。

    此前 scholar/closereading.py 与 scholar/pdf_ingest.py 各持一份逐字相同的
    `_strip_json`，改一处不同步另一处；收敛为唯一出处。
    """
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


# 抢救的规模上限。正常响应（20 篇翻译约 22 KB）远在闸下；触发闸说明响应本身已不正常。
_SALVAGE_MAX_CHARS = 200_000


def _close_suffix(head: str) -> Optional[str]:
    """给一段 JSON 前缀算出闭合后缀（按真实的括号栈，忽略字符串内与转义）。栈非法返回 None。"""
    stack = []
    in_str = False
    esc = False
    for c in head:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            stack.append(c)
        elif c in "}]":
            if not stack or {"}": "{", "]": "["}[c] != stack.pop():
                return None
    if in_str:
        return None
    return "".join("}" if b == "{" else "]" for b in reversed(stack))


def loads_lenient(text: str) -> Any:
    """解析 LLM JSON；畸形时抢救出**畸形点之前**的完整前缀，按括号栈闭合后重试。

    实测覆盖的畸形类型（见 test_json_tools.py）：响应被截断、数组元素间漏逗号、
    中途插入散文、尾随逗号、多个 JSON 对象拼接。

    **生产上真正发生的是截断，不是漏逗号。** 2026-08-17 那轮 digest 有 15 篇失败，
    分属**三个独立批次各 5 篇**（翻译 batch=5），报错分别在 line 124 col 1 /
    line 123 col 4 ×2——而 5 篇论文的响应正好 119–125 行，**三次独立调用全部报在
    文末**。漏逗号会报在漏的那一处、随机分布；截断恒报在文档末尾。字符数各异
    （4623/5220/5247）而行数几乎相同，是中文 char/token 比波动、结构长度恒定的
    token 级截断签名。根因在 `llm_client._call_agent`：claude-agent 路径既不用 CLI
    的 `--json-schema`、又不看信封里的 `stop_reason`/`terminal_reason`、还把
    `max_tokens` 整个丢掉，于是半截响应被当成成功调用返回。**本函数只是兜底，
    不是根因修复**——排查请先看 stop_reason，别顺着"漏逗号"去调 prompt 措辞。

    **抢救是有损的**：畸形点之后的元素全部丢弃。调用方拿到结果后必须自行发现「少了几条」
    并按缺失处理（workflow 两处都已有 id 缺失→单篇回退的分支），且**必须打日志**——
    静默少收数据比直接失败更难查。

    解析不出任何东西时返回 None（不抛），由调用方决定是抛还是走原有回退。
    """
    s = strip_code_fences(text)
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        # 从**报错位置**往回扫，而不是从串尾。两个好处：
        # ① 快得多——尾部那一大段永远解析不了，扫它纯属浪费；实测 190 KB 逗号串
        #    从 16.6 s 降到毫秒级。单纯限制尝试次数没用，因为每次 json.loads 本身是 O(n)。
        # ② 更正确——从尾部扫取的是「最长可解析前缀」，当模型漏转义引号让字符串边界
        #    错位时，尾部某个切点可能恰好配平括号栈而解析成功，产出语法合法但字段
        #    张冠李戴的结果。可解析前缀必然在报错点之前，从 e.pos 起扫就排除了这一类。
        start = min(max(e.pos, 0), len(s) - 1) if len(s) else 0
    except Exception:
        return None
    for cut in range(start, 0, -1):
        if s[cut] not in ",]}":
            continue
        head = s[:cut] if s[cut] == "," else s[:cut + 1]
        suffix = _close_suffix(head)
        if suffix is None:
            continue
        try:
            got = json.loads(head + suffix)
        except Exception:
            continue
        # 空容器 = 什么都没救出来，不该冒充成功：`{",,,"` 这类垃圾会在 cut=1 处
        # 闭合成合法的 `{}`，调用方拿到后只会在下游更远的地方以更难懂的方式失败。
        # 真正合法的 {} / [] 在开头那次 json.loads 就返回了，不经过这里。
        if isinstance(got, (dict, list)) and not got:
            continue
        return got
    return None
