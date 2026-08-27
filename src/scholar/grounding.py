# -*- coding: utf-8 -*-
"""论断的数字接地检查——生成侧防线的第一层（2026-08-27）。

**为什么需要它**：既有防线（`topics.validate_synthesis` / `qa.validate_qa`）校验的
是编号合法性，不是内容一致性。`qa.py` 渲染证据表时那句话说得很直白——「防线保证
citekey 与原句真实存在，不保证转述没有失真」。一条论断只要挂了个存在的 `[E7]`，
哪怕把 0.06 抄成 0.6、或把 A 文献的 AUC 安到 B 头上，全部检查都会放行。

本模块补的就是这一层：**论断里出现的数字，必须能在它所引证据里找到**。纯字符串
运算，零 LLM 成本，精度高。它抓不了措辞失真、立场反转、因果方向倒置——那些是
第二层（LLM 蕴含判定）的活，见 docs/decisions/rag_verification_plan_2026-08.md。

**判据必须唯一**：`scripts/gen_bench.py`（事后审计已落盘页面）与生产链路
（`validate_*` 生成时检查）共用本模块。两边各写一套的话，bench 报 100% 而生产
链路报别的，谁也不知道该信哪个。

**已知局限：只查阿拉伯数字。** 曾加过中文数字归一（「三个 IQR」↔「3 个 IQR」），
三轮对抗审核实测后删除：全库 542 个数字里它只救回 **1 个**，代价却是把最常见捏造值
「1」的假接地面积扩大 81%（70→127 条论断）——因为「一致」「十分」「两者」「进一步」
里的中文数字字被当成了数字注入匹配池。零头收益换可测代价，与删掉 citekey/年份
进池是同一条判断。

由此留下**一个已记账的缺口**：论断侧的中文计数（「**三个** ICU 库被直接对比」）
本层不查；而 `entail_audit --only-qualitative` 又用 `chk.total` 判「这条已被第一层
覆盖」而跳过含阿拉伯数字的论断——全库 136 条被跳过的论断里有 70 条（51%）同时带
论断侧中文数字，于是**两层都不查**。要堵它得让论断侧与池侧成对抽取中文数字并做
量词对齐（「3 个」↔「三个」），那是独立一轮改动，不在本层顺手做。

**其余已知局限（三轮对抗审核实测，假接地面积均为 0，故记账不修）**：

- `(?![A-Z])` 只挡起点不挡尾部缩短，所以 `150DEG`→`15`、`剂量20MG`→`2` 会被
  **截短**而不是跳过。真实语料里只有 1 处（`采样率360Hz` → 池里一个 `36`）。
- `exempt`（页面元数字）的账本与被删掉的 `cn_number` **一模一样**：全库只救回
  1 个数字（「60 条证据」），而 `flat` 会 `rstrip("%")`，于是 n_evidence=60 的页面上
  任何「60%」都自动接地。同一把尺子该量到自己身上——下次收紧时先处理它。
- 证据引句里的页码锚（`（p.7-10,14-16）`）往池里注了 198 个 token（26% 的证据行
  带锚），性质同已剔除的出处行号 `.md:944`，面积还大一个量级。剔掉实测零代价。

标定与首跑基线见 docs/decisions/gen_bench_baseline_2026-08.md。
"""
import re
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Set, Tuple

# 证据编号（E5/E25）是编号不是数字事实，抽数字前先剔掉。
# ⚠️ 不能用 `\bE\d+\b`：CJK 属于 `\w`，所以中文紧邻时 `\b` 不成立——「据E7所示」
# 「证据E12的结论」里的编号剥不掉，随后被当成数字事实报成假失真。更糟的是复合
# 伤害：`entail_audit --only-qualitative` 靠 chk.total 判断"这条已被第一层覆盖"，
# 于是一条纯定性论断只要提了 E7，第一层报假失真、第二层同时跳过它，两层之间漏下去。
# 前后顾写法抄自 qa._INLINE_REF_RE，那里早就做对了。
_EREF_RE = re.compile(r"(?<![A-Za-z0-9])\[?[Ee](\d+)\]?(?![0-9])")
# 引用标记，同样不算论断内容
_CITE_RE = re.compile(r"\[@([^\]\s]+)\]")
# 前置断言用的"字母"字符类。**这一处踩过两次坑，都是同一种失效形状**——论断侧与
# 池侧同时少抽，接地率纹丝不动，主指标完全看不见：
#
#   1. 写成 `[^\W\d_]`（Unicode 字母类）：CJK 也是 Unicode 字母，「共60条」这类
#      不留空格的中文写法被整片挡掉。代价 542→389 个数字、接地率 99.5%→76.3%。
#   2. 写成 `[A-Za-zÀ-ɏ]`（连续码位区段）：U+00C0–U+024F 里装着 `×`(U+00D7) 与
#      `÷`(U+00F7)，于是 `3×3`、`2×2` 的第二个操作数被当成"紧跟字母"挡掉。
#      真实页面（shadow-variable）上正在发生，规模只有 2 个数字所以主指标无感。
#
# 现在显式列出各字母块并**跳过 ×÷ 所在的空隙**，同时覆盖希腊/西里尔/拉丁扩展附加
# （库里 4707 个 citekey 有 50 个非 ASCII，含 `куксенко2024Аналіз`）。
_LETTER = "A-Za-z\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u024F\u0370-\u03FF\u0400-\u04FF\u1E00-\u1EFF"

# 数字：整数/小数/千分位/百分号。
# 千分位必须严格是 `,\d{3}`——宽松写成 `[\d,]*` 会踩这个坑：_norm 的 NFKC 把中文
# 逗号归一成半角，于是「相加为241，与摘要」被读成一个千分位数字 `241,`。
#
# 专名边界（2026-08-27 收窄）：
#   后置 `(?![A-Z])` 只排**大写**字母——专名如 `4CE`（联盟名 Consortium for Clinical
#   Characterization of COVID-19 by EHR）跟大写，而 `48h`/`5mg`/`10ml` 这类
#   **数字+小写单位**是真数量，必须留。原先写成 `(?![A-Za-z])` 把单位一起挡了，
#   实测造出假失真：证据原句「约 48h」，论断「约 48 小时」被判未接地。
#   前置 `(?<![A-Za-z])(?<![A-Za-z]-)` 排掉 `COVID-19` / `GPT-4` 里的数字，同时
#   放过 `60-70%` 这种数字区间（`70` 前面是 `0-`，不是「字母+连字符」）。
#   `(?<!\d)` 防止从数字中间起匹配：没有它，`COVID-19` 的 `1` 被前置断言挡掉后，
#   引擎会从 `9` 重新起匹配，凭空抽出一个 `9`。
#   前置用 `[A-Za-z\u00C0-\u024F]`（ASCII + 拉丁扩展）而不是纯 `[A-Za-z]`：citekey
#   带非 ASCII 作者名时 ASCII 类挡不住——`smajlović2026Secure` 的 `ć` 放行 `2026`
#   进入匹配，再被后置 `(?![A-Z])` 截断成 `202` 塞进池子（真实页面上出现 3 次）。
#   ⚠️ **绝不能图省事写成 `[^\W\d_]`（Unicode 字母类）**——CJK 也是 Unicode 字母，
#   那会把「共60条」这种紧邻中文的数字整片挡掉。实测代价：全库数字总数 542→389、
#   接地率 99.5%→76.3%。改完必须立刻重跑 gen_bench，别只看单元测试绿。
_NUM_RE = re.compile(
    r"(?<!\d)(?<![" + _LETTER + r"])(?<![" + _LETTER + r"]-)"
    r"\d+(?:,\d{3})*(?:\.\d+)?\s*%?(?![A-Z])")

# 派生量标记：论断在证据数字上做算术得出的比值/差值
_DERIVED_RE = re.compile(r"^\s*(?:倍|个百分点|百分点)")

# 归一化时要抹掉的不可见字符（PDF 抽文常带软连字符与零宽字符）
_INVISIBLE = ("­", "​", "‌", "‍", "﻿")


def norm(s: str) -> str:
    """归一化到可比形态：全角→半角（NFKC）、抹掉不可见字符。"""
    s = unicodedata.normalize("NFKC", s or "")
    for ch in _INVISIBLE:
        s = s.replace(ch, "")
    return s


@dataclass
class NumberCheck:
    """一条论断的数字接地结果。

    ungrounded 与 derived **不要混为一谈**：
      derived     紧跟"倍/个百分点"的派生量——论断在证据数字上做算术得出的比值或
                  差值（"eICU 规模约为 AUMCdb 的 8.7 倍" 来自 201k/23k）。按定义
                  不会出现在原句里，不是失真。但**也不静默豁免**：算错的派生数字
                  恰恰是该抓的，所以单列一档留着供抽查。
      ungrounded  没有派生标记又找不到出处的——这才是可疑的那一档。
    """
    total: int = 0
    grounded: int = 0
    ungrounded: List[str] = field(default_factory=list)
    derived: List[str] = field(default_factory=list)

    @property
    def denom(self) -> int:
        """主指标的分母：派生量剔出去。"""
        return self.total - len(self.derived)

    @property
    def rate(self) -> Optional[float]:
        return (self.grounded / self.denom) if self.denom else None

    @property
    def ok(self) -> bool:
        return not self.ungrounded


def _as_number(raw: str) -> Optional[Tuple[float, bool]]:
    """把一个数字 token 解析成 (值, 是否带百分号)。解析不了返回 None。"""
    t = norm(raw).replace(",", "").replace(" ", "")
    is_pct = t.endswith("%")
    try:
        return float(t.rstrip("%")), is_pct
    except ValueError:
        return None


def numbers_match(a: str, b: str) -> bool:
    """两个数字 token 是否指同一个事实。

    规则只有两条：

      1. 数值相等 → 匹配（含千分位/全角差异，`_as_number` 已归一）。
      2. **至少一方带百分号**时，允许百倍换算：证据写 `0.488`、论断写 `48.8%`
         （或反过来、或论断省掉那个 `%` 写成 `48.8`）说的是同一件事。

    第 2 条的「至少一方带 %」是**关键限制**，2026-08-27 修。此前的做法是给每个
    数字无条件展开 ×100 与 ÷100 的变体、再求交集，于是等价类被传递放大：论断
    「共 5 个中心」与证据「p = 0.05」判为接地——因为 0.05 展开出了 5。两个都不带
    百分号的数字之间，永远不该发生百倍换算。
    """
    x, y = _as_number(a), _as_number(b)
    if x is None or y is None:
        return False
    (xv, xp), (yv, yp) = x, y
    if xv == yv:
        return True
    # ⚠️ 必须是 XOR（恰好一方带 %），不是 or（至少一方）。两方都带 % 时做百倍换算
    # 等于放过量级抄错：`50%` 与 `0.5%` 会判匹配，而「缺失率 0.5% 写成 50%」正是
    # 本判据自我标榜要抓的第一类失真，也是 EHR 缺失率场景里最常见的笔误。
    # 实测改成 XOR 在真实 9 页上逐位无变化（百倍换算在本语料上救回的数字是 0 个）。
    if xp != yp:
        return abs(xv - yv * 100.0) < 1e-9 or abs(xv * 100.0 - yv) < 1e-9
    return False


def pool_numbers(pool: str) -> Set[str]:
    """把匹配池切成**数字 token 集合**（保留原始写法，等价判定交给 numbers_match）。

    ⚠️ 这是 2026-08-27 对抗审核抓出的核心判据缺陷的修复。原实现是无边界子串包含
    （`v in pool_flat`，且 pool 先被去掉逗号与空格），两个后果叠加：

      1. 任何数字只要是池中某个更长数字的**子串**就算接地：
         论断「AUC 达到 0.85」+ 证据「AUC 为 0.853」→ 判接地（应报失真）。
         论断「共 48 个中心」+ 证据「始于 1948 年的队列」→ 判接地。
      2. 去空格把**相邻两个数字焊成一个 token**，凭空造出池中不存在的数字：
         证据「eICU-CRD(n=150,753 126,804名患者)」被压成「…150753126804名…」。

    实测代价：全库 591 个数字里，接地数从 583 降到 575（98.65% → 97.29%）。
    也就是说 P0 首跑那个「100% 接地」是虚高的，9 个数字需要人工看。
    """
    return {m.group(0).strip() for m in _NUM_RE.finditer(norm(pool))}


def build_pool(*parts: Optional[str]) -> str:
    """把可对照的文本拼成匹配池：**只有证据原句 + 标题**。

    ⚠️ 曾经把 citekey、发表年份、出处行也塞进来，理由是"年份常来自文献自身而非
    原句，不放宽会造出一堆假失真"。**这个理由被实测证伪**（2026-08-27 第一轮对抗
    审核）：把池砍到只剩原句+标题后，全库 9 页的未接地数**一个都没变**——533 个
    接地数字全部靠原句/标题的阿拉伯数字硬接地，靠 citekey/年份/出处的是 **0 个**。

    而代价是可测的：年份 token 让「共纳入 2025 例患者」对任何 2025 年的文献自动
    接地（假接地从 51/455 涨到 122/455）；出处文件名 `科研札记_2023-01.md` 还会
    抽出月份 `01` → 值 1，让「只有 1 个中心报告」对任何一月份札记的文献接地。
    零收益、可测代价 —— 删掉。

    这条也是个教训：**"放宽以免误报"这类直觉必须先实测再写进代码**，否则就是拿
    真实的假接地去换想象中的假失真。
    """
    return " ".join(p for p in parts if p)


def check_numbers(claim: str, pool: str,
                  exempt: Optional[Sequence[str]] = None) -> NumberCheck:
    """审一条论断的数字接地情况。

    exempt 是**元数字**豁免表：论断里的 "60 条证据" 说的是本页证据条数，来源是页面
    元信息而不是任何一条证据，不豁免会稳定造出假失真。传 len(evidences) 之类。
    """
    pool_nums = pool_numbers(pool)
    exempt_set = {norm(str(e)).replace(",", "").replace(" ", "").rstrip("%")
                  for e in (exempt or [])}

    body = _CITE_RE.sub("", claim or "")   # 引用块本身不算论断内容
    body = _EREF_RE.sub("", body)          # E5/E25 是编号不是事实
    body = norm(body)

    out = NumberCheck()
    for m in _NUM_RE.finditer(body):
        raw = m.group(0).strip()
        if not raw.strip(" ,.%"):
            continue
        out.total += 1
        flat = norm(raw).replace(",", "").replace(" ", "").rstrip("%")
        # 真接地优先于派生标记：一个跟着"个百分点"的数若逐字就在证据里
        # （「绝对提升 2.12 个百分点」而证据写着 2.12%），它是可验证的事实，
        # 不该被移出分母静默豁免。
        #
        # ⚠️ 归因订正（第二轮对抗审核）：这个写法与"先判接地再判派生"在四象限上
        # **逐格等价**，它本身不提供任何新保护。派生量从 7 涨到 9 的真实原因是
        # **匹配池收窄**——`minoccheri2025Supervised` 的年份碎片离开池子后，
        # 「相差超过 20 倍」不再被蒙对。别把功劳记在这一行上。
        hit = any(numbers_match(raw, p) for p in pool_nums)
        if _DERIVED_RE.match(body[m.end():m.end() + 6]) and not hit:
            # 派生量优先于"未接地"，但**不优先于真接地**：一个跟着"个百分点"的数
            # 若逐字就在证据里（如「绝对提升 2.12 个百分点」而证据写着 2.12%），
            # 那它是可验证的事实，不该被移出分母静默豁免。真实页面上有 1 处。
            out.derived.append(raw)
        elif hit:
            out.grounded += 1
        elif flat in exempt_set:
            out.grounded += 1          # 元数字：来源是页面元信息，记为接地
        else:
            out.ungrounded.append(raw)
    return out
