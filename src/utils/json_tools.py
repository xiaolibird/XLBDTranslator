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
    token 级截断签名。根因在 `llm_client._call_agent`：claude-agent 路径把半截响应
    当成功调用返回。2026-08-20 已在那里补上正文完整性校验（`looks_like_complete_json`）
    + 重试，重试耗尽才把残料交还到这里。**本函数是最后一道防线，不是根因修复**
    ——排查别顺着"漏逗号"去调 prompt 措辞。

    两条曾经写在这里、后被实测推翻的猜测，留档免得再走一遍：
    ① "不看 stop_reason" ——信封里 `stop_reason` 在这种截断下恒为 `end_turn`、
       `terminal_reason` 恒为 `completed`，干净得看不出任何异常，光查信封够不着。
    ② "把 max_tokens 丢掉" ——CLI 默认上限是 64000 token，而这些响应只有约 1500
       token，远够用；且 CLI 认 `CLAUDE_CODE_MAX_OUTPUT_TOKENS`，而 pdf_ingest 传
       的是 1024/2048——真把 max_tokens 传下去反而会**主动制造**截断。

    **抢救是有损的**：畸形点之后的元素全部丢弃。调用方拿到结果后必须自行发现「少了几条」
    并按缺失处理（workflow 两处都已有 id 缺失→单篇回退的分支），且**必须打日志**——
    静默少收数据比直接失败更难查。

    解析不出任何东西时返回 None（不抛），由调用方决定是抛还是走原有回退。
    """
    s = strip_code_fences(text)
    # 模型常在 JSON 前面加开场白（"好的，以下是本批 20 篇的裁决结果："）。`workflow`
    # 的两个解析点只剥 ``` 围栏、不抠前言，少了这一步这种响应会让 filter 整批 20 篇
    # 降级成关键词裁决。
    #
    # **逐个候选起点试，不能只认第一个 `{`**：模型很爱在开场白里回显 schema
    # （"我按 {"id": ..., "decision": ...} 的格式输出："），prompt 里本就摆着 JSON
    # 示例，这是高频形状。只认第一个括号会从那个 schema 回显起截，必然解析失败。
    cands = _json_start_candidates(s)

    # 第一遍**只用严格解析**。这一遍的顺序无关紧要（严格解析要求从该起点到串尾
    # 全部合法，最多一个候选能通过），但它必须走在抢救之前——否则前言里的示例
    # 会被抢救层当成载荷救回来：
    #   '示例：{"id":1,"decision":"EXCLUDE"}\n实际裁决：\n[{"id":1,"decision":"INCLUDE"},…]'
    # 从示例那个 `{` 抢救，得到的是 EXCLUDE 那条，而它的 id 完全合法，
    # `valid_ids` 防线挡不住 —— 静默把 INCLUDE 覆盖成 EXCLUDE。
    # 这正是 `workflow._parse_translation_response` 的 docstring 记载的已知生产危害：
    # 「模型会把 prompt 里输出示例的 id 原样回显……静默覆盖同 id 的真实论文」。
    # 用诚实的整批失败换一篇被投毒，是严格退步。
    for cand in cands:
        try:
            return json.loads(s[cand:])
        except Exception:
            continue

    # 第二遍才抢救。取**救出内容最多**的那个起点：前言里的示例通常是一两条，
    # 真载荷是几十条，按体量取能把上面那种投毒挡在外面；而「前言 + 真截断」
    # 里最外层那个起点本来就救得最多，也会自然胜出。
    best, best_size = None, -1
    for cand in cands:
        got = _salvage_from(s[cand:])
        if got is None:
            continue
        try:
            size = len(json.dumps(got, ensure_ascii=False))
        except Exception:
            continue
        if size > best_size:
            best, best_size = got, size
    return best


# 候选起点最多试几个：前言里的括号通常只有一两处，多了纯属浪费（每次都是 O(n)）。
_MAX_START_CANDIDATES = 6


def _json_start_candidates(s: str) -> list:
    """JSON 可能的起始偏移量列表（正文本就以括号开头时只有 0）。"""
    if not s:
        return []
    if s[0] in "{[":
        return [0]
    out = []
    for i, ch in enumerate(s):
        if ch in "{[":
            out.append(i)
            if len(out) >= _MAX_START_CANDIDATES:
                break
    return out


def _salvage_from(s: str) -> Any:
    """对单个候选起点做「整体解析 → 失败则抢救最长合法前缀」。救不出返回 None。"""
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


def looks_like_complete_json(text: str) -> bool:
    """文本是否是一份**完整**的 JSON（用于在 LLM 客户端层判断响应有没有被截断）。

    2026-08-20：08-17 那轮 digest 的 15 篇（翻译，3 批 × 5）与 40 篇（filter，
    2 批 × 20）都是响应被截断。实测确认那种截断的**信封是干净的**
    （is_error=false / terminal_reason=completed / api_error_status=null），
    只有正文是半截的——光看信封判据一条都够不着，必须校验正文本身。

    **判的是「结构有没有写完」，不是「能不能直接 json.loads」。** 这两件事必须分开：
    尾随逗号、数组元素间漏逗号、多个对象拼接、正文前后带说明文字——都让
    `json.loads` 失败，但它们是**格式跑偏**，重试换不来更好的结果，而 `loads_lenient`
    本来就能救；只有括号栈没闭合（或字符串没收尾）才是**截断**，那才值得重试。

    曾经用「先 json.loads，失败就从散文里抠首个 {...}」做判据，方向是倒挂的：
    `'[{"id":1},{"id":2}]\\n以上是 2 条裁决。'` 判成截断（白重试 + 打假警报），
    而同样内容**加一句开场白**反而判成完整——加句废话就能让成本减半。
    """
    s = strip_code_fences(text or "").strip()
    if not s:
        return False
    try:
        json.loads(s)
        return True
    except Exception:
        pass
    # 没有任何括号 = 模型压根没输出 JSON（实测形状：整段自述"已完成合成，要点：…"，
    # 或反问"这个请求缺少主题——请告知后我再输出 JSON"）。这类重试才有意义。
    if "{" not in s and "[" not in s:
        return False
    # 括号栈没闭合（或字符串没收尾）⇒ 真截断。_close_suffix 返回空串表示无需补任何
    # 闭合符；返回非空表示还欠着括号；返回 None 表示栈非法或字符串未收尾
    # （'[{"id":1},{"id":2,"t":"未闭' 走的正是这条）。
    if _close_suffix(s) != "":
        return False
    # 结构写完了，但直接 json.loads 不过（尾随逗号/漏逗号/前言/尾随说明…）。
    # 此时的判据必须与下游**构造上一致**：闸门放行 ⟺ 抢救层真拿得出东西。
    # 否则会出现「闸门说完整、下游一个字段都取不到」的最坏组合——实测形状是模型
    # 用一整段带花括号的散文反问（"请告知主题，我再输出 {"verdicts":[…]} 形式的
    # 结果"）：括号是配平的，但那是句子的一部分，不是载荷。放行它 = 不重采样 +
    # 下游整批回退 + 还把它记成一次健康调用。
    return loads_lenient(s) is not None
