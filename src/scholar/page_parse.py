# -*- coding: utf-8 -*-
"""把已落盘的概念页/问答页拆回结构化数据（生成侧审计的共用基础，2026-08-27）。

为什么要反向解析：生成侧的两个审计器——`scripts/gen_bench.py`（数字接地，零成本）
与 `scripts/entail_audit.py`（蕴含判定，花钱）——都是对**已落盘产物**工作的。
落盘页面才是用户真正读到的东西，而且这样审计不必重跑生成。

解析范围只到生成块（`GEN_BEGIN`/`GEN_END`）内、证据表之前——**批注区绝不纳入**，
那是用户自己写的，把它算进来等于拿用户的话去考模型。
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

# 证据表起始锚。render_evidence_block 固定写「## 本页证据（N 条 · M 篇）」
_EVIDENCE_HEAD_RE = re.compile(r"^##\s+本页证据")
# 用户批注区——生成块之外，绝不纳入评测
_USER_ZONE_RE = re.compile(r"^##\s+我的批注")
# 证据行： - ● **E1** `[@citekey]` 🟩role · section · 来源:行号
_EVIDENCE_ROW_RE = re.compile(r"^-\s+[●○]\s+\*\*E(\d+)\*\*\s+`\[@([^\]]+)\]`(.*)$")
_CITE_RE = re.compile(r"\[@([^\]\s]+)\]")
_QUOTE_RE = re.compile(r"^\s*>\s*(.+)$")
_SMALL_RE = re.compile(r"<small>(.*?)</small>")

GEN_BEGIN = "<!-- BEGIN GENERATED"
GEN_END = "<!-- END GENERATED"


@dataclass
class EvidenceRow:
    """证据表里的一行：一条证据的全部可对照文本。"""
    ref: str                       # "E1"
    citekey: str
    title: str = ""
    quote: str = ""                # 原句（`>` 那行）
    meta: str = ""                 # role · section · 出处行

    @property
    def pool(self) -> str:
        """可用于比对的全部文本：**只有原句 + 标题**。

        ⚠️ 曾经还并入 citekey / 发表年份 / 出处行（含行号）。实测证明那些**一个
        接地数字都贡献不了**，却制造可测的假接地：出处行号让「值超出 3 个 IQR」
        的 3 靠 `.md:944` 接地；年份让「纳入 2025 例患者」对任何 2025 年文献接地；
        出处文件名的月份 `科研札记_2023-01.md` 还会给出值 1。
        口径与生产侧（topics._check_grounding）保持一致，见 grounding.build_pool。
        """
        return " ".join(x for x in (self.quote, self.title) if x)


@dataclass
class Claim:
    """一条论断（含分歧的一方）。"""
    text: str
    citekeys: List[str] = field(default_factory=list)
    section: str = ""              # 所属小节标题，报告里用来定位


@dataclass
class Page:
    slug: str
    frontmatter: Dict[str, str] = field(default_factory=dict)
    claims: List[Claim] = field(default_factory=list)
    evidence: Dict[str, EvidenceRow] = field(default_factory=dict)   # ref -> row

    def pool_for(self, citekeys) -> str:
        """给定 citekey 列表，拼出匹配池。

        一个 citekey 可能对应多条证据（qa 页 28 条只有 15 篇），全部并进来。
        """
        keys = set(citekeys)
        return " ".join(r.pool for r in self.evidence.values() if r.citekey in keys)

    def rows_for(self, citekeys) -> List[EvidenceRow]:
        keys = set(citekeys)
        return [r for r in self.evidence.values() if r.citekey in keys]

    def meta_numbers(self) -> List[str]:
        """页面元数字：论断里的「这 60 条证据」说的是本页证据条数，不来自任何证据。

        ⚠️ **只放 n_evidence**（2026-08-27 对抗审核后收窄）。原先豁免六个字段
        （n_papers/n_claims/n_disputes/n_gaps/evidence_used），后果是：
          - adversarial-evidence 页 n_disputes=3、n_gaps=5，于是论断里出现的**任何**
            3 或 5（「3 国」「5 项研究」）都被无条件记为接地；
          - 合成页上 n_disputes=0、n_claims=1 时，豁免集直接含 `0` 和 `1`。
        而生产侧（topics._check_grounding）从来只传 n_evidence 一个数——两侧口径
        不一致，正是 gen_bench「交叉校验」稳定报假 bug 的三个来源之一。
        """
        v = self.frontmatter.get("n_evidence", "")
        return [v] if v.isdigit() else []


def parse_frontmatter(lines: List[str]) -> Dict[str, str]:
    fm: Dict[str, str] = {}
    if not lines or lines[0].strip() != "---":
        return fm
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        m = re.match(r'^(\w+):\s*"?([^"]*)"?\s*$', ln)
        if m:
            fm[m.group(1)] = m.group(2)
    return fm


def parse_page(path: Path) -> Page:
    """把一页拆成 frontmatter + 论断 + 证据表。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    page = Page(slug=path.stem, frontmatter=parse_frontmatter(lines))

    in_gen = False
    zone = "claims"          # claims → evidence → user
    section = ""
    cur_ref = None           # 当前证据行的 ref，续行（标题/原句）归给它
    for ln in lines:
        if ln.startswith(GEN_BEGIN):
            in_gen = True
            continue
        if ln.startswith(GEN_END):
            in_gen = False
            continue
        if _USER_ZONE_RE.match(ln):
            zone = "user"
            continue
        if _EVIDENCE_HEAD_RE.match(ln):
            zone = "evidence"
            continue
        if not in_gen or zone == "user":
            continue

        if zone == "claims":
            if ln.startswith("## ") or ln.startswith("### "):
                section = ln.lstrip("# ").strip()
            elif ln.startswith("- ") and _CITE_RE.search(ln):
                page.claims.append(Claim(text=ln[2:].strip(),
                                         citekeys=_CITE_RE.findall(ln),
                                         section=section))
        elif zone == "evidence":
            m = _EVIDENCE_ROW_RE.match(ln)
            if m:
                # ⚠️ 续行必须归给刚匹配到的这条，不能用「字典里最后一个 key」：
                # 同一 citekey 有多条证据时插入顺序不变，会把原句挂到别的文献名下。
                # qa 页曾因此整页词面覆盖掉到 0.227、接地率假跌到 43%。
                cur_ref = "E" + m.group(1)
                page.evidence[cur_ref] = EvidenceRow(
                    ref=cur_ref, citekey=m.group(2), meta=m.group(3).strip())
            elif cur_ref and cur_ref in page.evidence:
                row = page.evidence[cur_ref]
                mq = _QUOTE_RE.match(ln)
                ms = _SMALL_RE.search(ln)
                if mq:
                    row.quote = (row.quote + " " + mq.group(1)).strip()
                elif ms:
                    row.title = ms.group(1).strip()
    return page


def iter_pages(topics_dir: Path, only: str = "") -> List[Path]:
    """概念页 + 问答页，排除 INDEX 与下划线开头的辅助文件。"""
    pages = sorted(p for p in topics_dir.glob("*.md")
                   if not p.name.startswith("_") and p.name != "INDEX.md")
    qa_dir = topics_dir / "qa"
    if qa_dir.is_dir():
        pages += sorted(p for p in qa_dir.glob("*.md") if p.name != "INDEX.md")
    if only:
        pages = [p for p in pages if p.stem == only]
    return pages
