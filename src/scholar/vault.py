# -*- coding: utf-8 -*-
"""科研札记库 → Obsidian vault：per-paper 笔记 + 语义相似图谱 + 静态 MOC 索引。

数据源是 `literature_index.json` + 月度 md 切片（不依赖 PaperSegment，故 2021→2026 全部
历史一视同仁）。切片正则复用 `notes_index._SECTION_RE`——md 格式契约一旦变动，两边测试同时挂。

**安全第一原则**：vault 里有用户手写内容（`## 我的札记` 与自加的 frontmatter 键/tag）。
生成块用哨兵注释包裹，哨兵缺失或 YAML 解析失败一律**不覆盖**，改写 `.conflict.md` 并报错退出。
frontmatter 走按键合并而非文本 diff（Obsidian 的 Properties UI 会重写整个 frontmatter）。
"""
import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..utils.logger import get_logger
from .notes_index import _SECTION_RE, write_if_changed

logger = get_logger(__name__)

VAULT_SCHEMA_VERSION = 1

GEN_BEGIN = "<!-- BEGIN GENERATED v{} · 由 scripts/build_vault.py 生成 · 手改此块会在重建时丢失 -->"
GEN_BEGIN_RE = re.compile(r"^<!-- BEGIN GENERATED v(\d+)\b.*-->\s*$")
GEN_END = "<!-- END GENERATED · 以下内容属于你，生成器永不触碰 -->"
USER_HEADING = "## 我的札记"
DEFAULT_USER_ZONE = "\n{}\n\n".format(USER_HEADING)

PAPERS_DIR = "01-文献"
MOC_DIR = "_MOC"
OVERVIEW = "00-总览.md"
META_JSON = "_meta.json"

# frontmatter 受管键：只有这些由生成器覆盖，其余（用户自加）原样保留
MANAGED_KEYS = (
    "citekey", "title", "aliases", "authors", "authors_n", "year", "journal",
    "doi", "arxiv_id", "url", "month", "series", "decision", "priority_tier",
    "priority_rank", "confidence", "bucket", "role", "flags",
    "has_full_text_reading", "reading_source", "n_highlights",
    "n_citable", "n_refutable", "n_method", "one_line", "origin",
    "tags", "cssclasses", "vault_schema",
)
MANAGED_TAG_PREFIXES = ("scholar", "tier/", "bucket/", "role/", "flag/",
                        "series/", "decision/", "reading/", "year/")

# 句级三色：对齐 docx_builder._TAG_COLOR（方法论借鉴=墨绿 / 可引用证据=紫 / 可反驳观点=蓝）。
# 用方块而非圆形——圆形 🔴🟠🟢 已被 priority_tier 占用，避免语义相撞。
ROLE_EMOJI = {"citable": "🟪", "refutable": "🟦", "method": "🟩"}
ROLE_LABEL = {"citable": "可引用证据", "refutable": "可反驳观点", "method": "方法论借鉴"}
ROLE_ORDER = ("citable", "refutable", "method")
TIER_EMOJI = {"high": "🔴", "mid": "🟠", "low": "🟢"}

# bucket 中文语义取自 config/prompts/whitelist_filter_prompt.md（workflow.py:46-47 同源）
BUCKET_LABEL = {
    "A": "缺失机制方法学", "B": "缺失感知建模", "C": "缺失与因果",
    "D": "跨域跨中心迁移", "E": "对抗性证据", "F": "多库ICU基准", "G": "临床落地场景",
}

# 全库 82% 的 highlights 是旧三类（方法学创新/重要发现）经 TAG_TO_ROLE 近似映射而来，
# 不是当时按新三轴标注的。不写明这点，用户会把「重要发现」当成作者亲口给的可引用效应量。
LEGACY_TAGS = ("方法学创新", "重要发现", "研究背景")
LEGACY_WARN = ("> [!warning]- 关于句级角色的口径\n"
               "> 本库 82% 的句级标记来自历史三类（方法学创新／重要发现），由 `TAG_TO_ROLE`\n"
               "> 近似映射为 method／citable，**不是**当时按新三轴（可引用证据／可反驳观点／方法论借鉴）\n"
               "> 标注的。引用前请回原文核实，尤其别把「重要发现」直接当作者给出的可引用效应量。")

_FN_BAD = re.compile(r'[/\\:*?"<>|#^\[\]%]')
_META_LINE = re.compile(r"^\*\*(优先级|裁决|一句话用处|作者|期刊/来源|DOI|链接)\*\*")
_CN_CHAR = re.compile(r"[一-鿿]")
_EN_WORD = re.compile(r"[a-z]{3,}")
_STOP = frozenset("""the and for with from that this are was were has have been being
their there where which while would could should about into over under more most
such than then them they using used use based can may our its not but all any
data model models method methods result results study studies paper papers approach
one two three new non via per within without across among also both each other
""".split())


# ---------------- 选取与切片 ----------------

def select_papers(index: Dict[str, Any], *, include_maybe: bool = False) -> List[Dict[str, Any]]:
    """keeper ∧（INCLUDE ∨ 已精读）。include_maybe=True 时放宽为 keeper 全集。"""
    out = []
    for e in index.get("papers") or []:
        if not isinstance(e, dict) or e.get("duplicate_of") or not e.get("citekey"):
            continue
        if include_maybe or e.get("decision") == "INCLUDE" or e.get("has_full_text_reading"):
            out.append(e)
    return out


def slice_section(lines: List[str], note_line: Optional[int], citekey: str) -> Optional[List[str]]:
    """从月度 md 切出该论文的小节体。失败返回 None（调用方跳过并计入 slice_failures）。

    终止于下一个 `## ` **或 `# `**——后者不可省：全库 59 处 `# 参考文献` 都在最后一篇之后，
    漏了会把参考文献和 `::: {#refs}` 吞进正文。尾部单独一行的 `---` 是小节分隔线，剥掉。
    """
    if not note_line or note_line < 1 or note_line > len(lines):
        return None
    start = note_line - 1
    m = _SECTION_RE.match(lines[start])
    if not m or m.group(4) != citekey:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        s = lines[i]
        if s.startswith("## ") or s.startswith("# "):
            end = i
            break
    body = lines[start:end]
    while body and (body[-1].strip() == "" or body[-1].strip() == "---"):
        body.pop()
    return body


def strip_meta_lines(body: List[str]) -> List[str]:
    """丢弃已进 frontmatter 的元信息行，从首个 `### ` 起保留，并把 `###` 提升为 `##`。"""
    out: List[str] = []
    started = False
    for line in body[1:]:                       # body[0] 是 `## 🟠 中 13. Title [@key]`
        if not started:
            if line.startswith("### "):
                started = True
            elif _META_LINE.match(line) or not line.strip():
                continue
            else:
                continue                        # 标题与首个 ### 之间的其它零散行一并丢弃
        if line.startswith("### "):
            out.append("## " + line[4:])
        else:
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return out


def load_bodies(entries: List[Dict[str, Any]], notes_dir: Path
                ) -> Tuple[Dict[str, List[str]], List[str]]:
    """按 note_file 分组读盘（每文件只读一次），切出各篇小节体。返回 (ok, failures)。"""
    notes_dir = Path(notes_dir)
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for e in entries:
        by_file.setdefault(e.get("note_file") or "", []).append(e)
    bodies: Dict[str, List[str]] = {}
    failures: List[str] = []
    for fname, group in by_file.items():
        lines: List[str] = []
        if fname:
            try:
                lines = (notes_dir / fname).read_text(encoding="utf-8").splitlines()
            except Exception as exc:
                logger.warning("  读不到 {}（{}），该文件 {} 篇走无正文降级".format(
                    fname, type(exc).__name__, len(group)))
        for e in group:
            body = slice_section(lines, e.get("note_line"), e["citekey"]) if lines else None
            if body is None:
                failures.append("{}@{}".format(e["citekey"], e.get("month") or "?"))
                continue
            bodies[e["citekey"]] = strip_meta_lines(body)
    return bodies, failures


# ---------------- 文件命名 ----------------

def safe_filename(citekey: str, used_lower: Set[str]) -> str:
    """citekey → 合法且唯一的文件名（不含 .md）。

    NFC 归一 + 替换 Obsidian/文件系统敏感字符 + 限长 200 字节（APFS 上限 255）+
    大小写不敏感去冲突（macOS APFS 默认不区分大小写）。frontmatter 里的 citekey 仍是原值。
    """
    name = unicodedata.normalize("NFC", (citekey or "").strip()) or "untitled"
    name = _FN_BAD.sub("-", name).replace(" ", "")
    name = name.strip(". ") or "untitled"
    if len(name.encode("utf-8")) > 200:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        b = name.encode("utf-8")[:190]
        name = b.decode("utf-8", errors="ignore").rstrip() + "-" + digest
    base, n = name, 1
    while name.lower() in used_lower:
        n += 1
        name = "{}-{}".format(base, n)
    used_lower.add(name.lower())
    return name


# ---------------- frontmatter ----------------

def _y(v: Any) -> str:
    """标量 → YAML。字符串走 json.dumps：JSON 转义集合是 YAML 双引号标量的子集，天然合法。"""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def _y_list(vals: List[Any]) -> str:
    return "[{}]".format(", ".join(_y(v) for v in vals))


def build_tags(e: Dict[str, Any]) -> List[str]:
    """受管 tag（ASCII 嵌套 slug；中文语义留给 MOC 页标题）。"""
    tags = ["scholar"]
    if e.get("priority_tier"):
        tags.append("tier/{}".format(e["priority_tier"]))
    if e.get("series"):
        tags.append("series/{}".format(e["series"]))
    if e.get("decision"):
        tags.append("decision/{}".format(e["decision"]))
    if e.get("reading_source"):
        tags.append("reading/{}".format(re.sub(r"[^A-Za-z0-9_-]+", "-", e["reading_source"])))
    if e.get("year"):
        tags.append("year/{}".format(e["year"]))
    for b in (e.get("bucket") or []):
        tags.append("bucket/{}".format(b))
    if e.get("role"):
        tags.append("role/{}".format(e["role"]))
    for f in (e.get("flags") or []):
        tags.append("flag/{}".format(f))
    return tags


def merge_tags(new: List[str], old: Any) -> List[str]:
    """新受管 tag ∪（旧 tags − 受管前缀）——用户自加的 `待复现` 不被吃掉。"""
    if isinstance(old, str):
        old = [old]
    kept = []
    for t in (old or []):
        t = str(t)
        if not any(t == p or t.startswith(p) for p in MANAGED_TAG_PREFIXES):
            kept.append(t)
    out = list(new)
    for t in kept:
        if t not in out:
            out.append(t)
    return out


def build_frontmatter(e: Dict[str, Any], preserved: Optional[Dict[str, Any]] = None) -> str:
    """受管键在前用新值，用户自加键原样跟在后面。不含任何随运行变化的值（幂等前提）。"""
    tc = e.get("tag_counts") or {}
    hls = e.get("highlights") or []
    origin = "{}#L{}".format(e.get("note_file") or "", e.get("note_line") or 0)
    managed: List[Tuple[str, str]] = [
        ("citekey", _y(e.get("citekey"))),
        ("title", _y(e.get("title") or "")),
        ("aliases", _y_list([e["title"]] if e.get("title") else [])),
        ("authors", _y_list((e.get("authors") or [])[:8])),
        ("authors_n", _y(len(e.get("authors") or []))),
        ("year", _y(e.get("year"))),
        ("journal", _y(e.get("journal"))),
        ("doi", _y(e.get("doi"))),
        ("arxiv_id", _y(e.get("arxiv_id"))),
        ("url", _y(e.get("url"))),
        ("month", _y(e.get("month"))),                    # 带引号：防被推断成日期
        ("series", _y(e.get("series"))),
        ("decision", _y(e.get("decision"))),
        ("priority_tier", _y(e.get("priority_tier"))),
        ("priority_rank", _y(e.get("priority_rank"))),
        ("confidence", _y(e.get("confidence"))),
        ("bucket", _y_list(e.get("bucket") or [])),
        ("role", _y(e.get("role"))),
        ("flags", _y_list(e.get("flags") or [])),
        ("has_full_text_reading", _y(bool(e.get("has_full_text_reading")))),
        ("reading_source", _y(e.get("reading_source"))),
        ("n_highlights", _y(len(hls))),
        ("n_citable", _y(int(tc.get("citable") or 0))),
        ("n_refutable", _y(int(tc.get("refutable") or 0))),
        ("n_method", _y(int(tc.get("method") or 0))),
        ("one_line", _y(e.get("one_line") or "")),
        ("origin", _y(origin)),
        ("tags", _y_list(merge_tags(build_tags(e), (preserved or {}).get("tags")))),
        ("cssclasses", _y_list(["scholar-note", "tier-{}".format(e.get("priority_tier") or "none")])),
        ("vault_schema", _y(VAULT_SCHEMA_VERSION)),
    ]
    lines = ["---"] + ["{}: {}".format(k, v) for k, v in managed]
    for k, v in (preserved or {}).items():
        if k in MANAGED_KEYS:
            continue
        lines.append("{}: {}".format(k, _dump_preserved(v)))
    lines.append("---")
    return "\n".join(lines)


def _dump_preserved(v: Any) -> str:
    """用户自加键原样回写（用 JSON 流式表达，YAML 兼容）。"""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return _y(v)


# ---------------- 句级证据 ----------------

def block_id(role: str, text: str, used: Set[str]) -> str:
    """内容哈希块 ID：中间插入一条 highlight 不会让用户已建的块引用集体指错（序号 ID 会）。"""
    h = hashlib.sha1("{}\x00{}".format(role or "", text or "").encode("utf-8")).hexdigest()[:8]
    bid, n = "hl-{}".format(h), 1
    while bid in used:
        n += 1
        bid = "hl-{}-{}".format(h, n)
    used.add(bid)
    return bid


def build_highlight_blocks(e: Dict[str, Any]) -> List[str]:
    hls = [h for h in (e.get("highlights") or []) if isinstance(h, dict)]
    if not hls:
        return ["## 句级证据", "",
                "> [!info] 本篇仅摘要级入选（无句级精读）。需要证据请回原文或跑 `read-paper` 深读。", ""]
    out = ["## 句级证据（{} 条）".format(len(hls)), ""]
    if any(h.get("tag") in LEGACY_TAGS for h in hls):
        out += [LEGACY_WARN, ""]
    used: Set[str] = set()
    for role in ROLE_ORDER:
        group = [h for h in hls if h.get("role") == role]
        if not group:
            continue
        out += ["### {} {}（{}）".format(ROLE_EMOJI[role], ROLE_LABEL[role], len(group)), ""]
        for h in group:
            text = (h.get("text") or "").strip().replace("\n", " ")
            section = (h.get("section") or "").strip() or "精读"
            legacy = "（原标记：{}）".format(h["tag"]) if h.get("tag") in LEGACY_TAGS else ""
            out.append("> [!quote] {}{}".format(section, legacy))
            out.append("> {}".format(text))
            out.append("^{}".format(block_id(role, text, used)))
            out.append("")
    return out


# ---------------- 合并与写盘（安全核心） ----------------

def split_frontmatter(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """切出 frontmatter dict 与正文。无 frontmatter → ({}, 全文)；解析失败 → (None, 全文)。"""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            end = i
            break
    if end is None:
        return None, text
    raw = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1:])
    try:
        import yaml
        data = yaml.safe_load(raw) if raw.strip() else {}
    except Exception:
        return None, text
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, text
    return data, body


def extract_user_zone(body: str) -> Optional[str]:
    """取哨兵之后的用户区。哨兵缺失返回 None——调用方必须走 conflict 而不是覆盖。"""
    idx = body.find(GEN_END)
    if idx < 0:
        return None
    return body[idx + len(GEN_END):]


def assemble(fm: str, gen_block: str, user_zone: str) -> str:
    """三段拼装：frontmatter / 哨兵包裹的生成块 / 用户区。

    用户区首尾空行归一化（内容本身一字不动）——否则 `split_frontmatter` 的 splitlines 重组
    会吞掉结尾空行，使「重建→再重建」每次都差一个换行，幂等永远不成立。
    """
    return "{}\n\n{}\n{}\n{}\n\n{}\n".format(
        fm, GEN_BEGIN.format(VAULT_SCHEMA_VERSION), gen_block.rstrip("\n"),
        GEN_END, user_zone.strip("\n"))


def merge_note(entry: Dict[str, Any], gen_block: str, existing: Optional[str]
               ) -> Tuple[Optional[str], str]:
    """合并生成块与用户内容。返回 (最终内容 | None, 状态)。

    状态 new / merged / conflict。**conflict 时返回 None，调用方不得覆盖原文件**——
    哨兵被删或 YAML 被改坏都归此类，宁可让用户手工合并，也不吞掉他写的东西。
    """
    if existing is None:
        return assemble(build_frontmatter(entry), gen_block, DEFAULT_USER_ZONE), "new"
    fm, body = split_frontmatter(existing)
    if fm is None:
        return None, "conflict"
    user_zone = extract_user_zone(body)
    if user_zone is None:
        return None, "conflict"
    return assemble(build_frontmatter(entry, preserved=fm), gen_block, user_zone), "merged"


# ---------------- 语义相似边（图谱主力） ----------------

def _tokens(e: Dict[str, Any]) -> List[str]:
    """英文词（≥3 字母、去停用词）+ 中文字符级 2-gram（大量 one_line/highlights 是中文）。"""
    parts = [e.get("title") or "", e.get("one_line") or ""]
    parts += [(h.get("text") or "") for h in (e.get("highlights") or []) if isinstance(h, dict)]
    text = " ".join(parts).lower()
    toks = [w for w in _EN_WORD.findall(text) if w not in _STOP]
    cn = _CN_CHAR.findall(text)
    toks += ["".join(cn[i:i + 2]) for i in range(len(cn) - 1)]
    return toks


def compute_neighbors(entries: List[Dict[str, Any]], k: int = 5
                      ) -> Dict[str, List[Tuple[str, float]]]:
    """TF-IDF + 倒排索引 kNN（纯 stdlib）。邻居强制限定在入选集内，避免悬空 wikilink。"""
    if k <= 0 or len(entries) < 2:
        return {e["citekey"]: [] for e in entries}
    keys = [e["citekey"] for e in entries]
    tfs: List[Dict[str, float]] = []
    df: Dict[str, int] = {}
    for e in entries:
        counts: Dict[str, int] = {}
        for t in _tokens(e):
            counts[t] = counts.get(t, 0) + 1
        tfs.append({t: 1.0 + math.log(c) for t, c in counts.items()})
        for t in counts:
            df[t] = df.get(t, 0) + 1
    n = len(entries)
    vecs: List[Dict[str, float]] = []
    inverted: Dict[str, List[Tuple[int, float]]] = {}
    for i, tf in enumerate(tfs):
        vec = {t: w * math.log(n / df[t]) for t, w in tf.items() if df[t] < n}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vec = {t: v / norm for t, v in vec.items()}
        vecs.append(vec)
        for t, v in vec.items():
            inverted.setdefault(t, []).append((i, v))
    # 极高频词对相似度贡献小却拖慢累加，跳过出现在 >30% 文档里的词
    hot = {t for t, post in inverted.items() if len(post) > 0.3 * n}
    out: Dict[str, List[Tuple[str, float]]] = {}
    for i, vec in enumerate(vecs):
        scores: Dict[int, float] = {}
        for t, v in vec.items():
            if t in hot:
                continue
            for j, w in inverted.get(t, ()):
                if j != i:
                    scores[j] = scores.get(j, 0.0) + v * w
        top = sorted(scores.items(), key=lambda kv: (-kv[1], keys[kv[0]]))[:k]
        out[keys[i]] = [(keys[j], round(s, 3)) for j, s in top if s > 0.02]
    # 双向补全：A→B 则 B→A，让簇更结实（单向链在图上看着是断的）
    for a, nbrs in list(out.items()):
        for b, s in nbrs:
            if all(x != a for x, _ in out.get(b, [])):
                out.setdefault(b, []).append((a, s))
    return out


# ---------------- 单篇生成块 ----------------

def _moc_link(kind: str, name: str) -> str:
    return "[[{}/{}/{}|{}]]".format(MOC_DIR, kind, name, name)


def render_generated_block(e: Dict[str, Any], body: List[str],
                           neighbors: List[Tuple[str, float]],
                           one_lines: Dict[str, str]) -> str:
    """哨兵之间的生成正文：标题 + 概览 callout + 句级证据 + 精读原文 + 相邻文献 + 归属。"""
    tier = TIER_EMOJI.get(e.get("priority_tier") or "", "")
    out = ["# {} {}".format(tier, e.get("title") or "(无标题)"), ""]

    meta = []
    if e.get("one_line"):
        meta.append("> **一句话用处**：{}".format(e["one_line"]))
    badge = []
    if e.get("decision"):
        badge.append("`{}`".format(e["decision"]))
    if e.get("bucket"):
        badge.append("维度 " + "/".join("{}·{}".format(b, BUCKET_LABEL.get(b, b))
                                        for b in e["bucket"]))
    if e.get("role"):
        badge.append("角色 {}".format(e["role"]))
    if e.get("flags"):
        badge.append("⚑ " + "/".join(e["flags"]))
    if badge:
        meta.append("> **裁决**：{}".format(" · ".join(badge)))
    src = []
    if e.get("authors"):
        src.append("{}{}".format(", ".join(e["authors"][:3]),
                                 " et al." if len(e["authors"]) > 3 else ""))
    if e.get("journal"):
        src.append(e["journal"])
    if e.get("year"):
        src.append(str(e["year"]))
    if src:
        meta.append("> **出处**：{}".format(" · ".join(src)))
    if e.get("doi"):
        meta.append("> **DOI**：[{0}](https://doi.org/{0})".format(e["doi"]))
    elif e.get("url"):
        meta.append("> **链接**：{}".format(e["url"]))
    meta.append("> **引用**：`[@{}]`　**原文**：`{}` 第 {} 行".format(
        e.get("citekey"), e.get("note_file") or "?", e.get("note_line") or "?"))
    if meta:
        out += ["> [!abstract] 概览"] + meta + [""]

    out += build_highlight_blocks(e)

    if body:
        out += ["## 原文精读", ""] + body + [""]

    if neighbors:
        out += ["## 相邻文献", ""]
        for key, sim in neighbors:
            ol = (one_lines.get(key) or "").strip()
            out.append("- [[{}]] · 相似度 {:.2f}{}".format(
                key, sim, " · {}".format(ol[:60]) if ol else ""))
        out.append("")

    belong = []
    for b in (e.get("bucket") or []):
        belong.append(_moc_link("维度", "{}-{}".format(b, BUCKET_LABEL.get(b, b))))
    if not e.get("bucket"):
        belong.append(_moc_link("维度", "未分维度"))
    belong.append(_moc_link("角色", e.get("role") or "未标角色"))
    if e.get("month"):
        belong.append(_moc_link("月度", e["month"]))
    if e.get("year"):
        belong.append(_moc_link("年份", str(e["year"])))
    for f in (e.get("flags") or []):
        belong.append(_moc_link("旗标", f))
    out += ["## 归属", "", " · ".join(belong), ""]
    return "\n".join(out)


# ---------------- MOC（静态表格，零插件依赖） ----------------

SHARD_THRESHOLD = 150


def _row(e: Dict[str, Any]) -> str:
    return "| {} | [[{}]] | {} | {} | {} |".format(
        TIER_EMOJI.get(e.get("priority_tier") or "", ""), e["citekey"],
        e.get("year") or "", (e.get("one_line") or "").replace("|", "/")[:70],
        e.get("month") or "")


_TABLE_HEAD = ["| 优先级 | 文献 | 年份 | 一句话用处 | 收录月 |",
               "|:-:|---|:-:|---|:-:|"]


def _moc_page(title: str, desc: str, rows: List[Dict[str, Any]]) -> str:
    body = ["# {}".format(title), "", "{}　共 **{}** 篇。".format(desc, len(rows)), ""]
    body += _TABLE_HEAD
    for e in sorted(rows, key=lambda x: (x.get("month") or "", x.get("priority_rank") or 999),
                    reverse=True):
        body.append(_row(e))
    body += ["", "> [!tip] 图谱里想看真实语义簇，搜索框输入 `-path:_MOC` 过滤掉索引页（Obsidian 内置）。", ""]
    return "\n".join(body)


def _shard_year(e: Dict[str, Any]) -> str:
    return str(e.get("year") or "未知年")


def _shard_month(e: Dict[str, Any]) -> str:
    return str(e.get("month") or "未知月")


def _sharded(kind: str, title: str, desc: str, rows: List[Dict[str, Any]],
             shard_key=_shard_year, unit: str = "篇") -> Dict[str, str]:
    """超阈值的组切子页，父页只列子页与计数——避免 450 篇的巨型星形。

    年份 MOC 必须换 shard_key（按年份再切年份会产出 `2025-2025` 这种废页）。
    """
    pages: Dict[str, str] = {}
    if len(rows) <= SHARD_THRESHOLD:
        pages["{}/{}/{}.md".format(MOC_DIR, kind, title)] = _moc_page(title, desc, rows)
        return pages
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for e in rows:
        groups.setdefault(shard_key(e), []).append(e)
    if len(groups) <= 1:                      # 切不动就不切，免得多一层空壳父页
        pages["{}/{}/{}.md".format(MOC_DIR, kind, title)] = _moc_page(title, desc, rows)
        return pages
    lines = ["# {}".format(title), "",
             "{}　共 **{}** {}，分片如下。".format(desc, len(rows), unit), ""]
    for g in sorted(groups, reverse=True):
        sub = "{}-{}".format(title, g)
        pages["{}/{}/{}.md".format(MOC_DIR, kind, sub)] = _moc_page(
            sub, "{} · {}".format(desc, g), groups[g])
        lines.append("- [[{}]]（{} {}）".format(sub, len(groups[g]), unit))
    lines.append("")
    pages["{}/{}/{}.md".format(MOC_DIR, kind, title)] = "\n".join(lines)
    return pages


def build_moc_pages(entries: List[Dict[str, Any]]) -> Dict[str, str]:
    pages: Dict[str, str] = {}
    # 维度
    for b, label in BUCKET_LABEL.items():
        rows = [e for e in entries if b in (e.get("bucket") or [])]
        if rows:
            pages.update(_sharded("维度", "{}-{}".format(b, label), "筛选维度 {}".format(b), rows))
    unbucketed = [e for e in entries if not e.get("bucket")]
    if unbucketed:
        pages.update(_sharded("维度", "未分维度", "未标注筛选维度", unbucketed))
    # 角色 / 旗标 / 月度 / 年份
    roles: Dict[str, List[Dict[str, Any]]] = {}
    for e in entries:
        roles.setdefault(e.get("role") or "未标角色", []).append(e)
    for name, rows in roles.items():
        pages.update(_sharded("角色", name, "筛选角色 {}".format(name), rows))
    flags: Dict[str, List[Dict[str, Any]]] = {}
    for e in entries:
        for f in (e.get("flags") or []):
            flags.setdefault(f, []).append(e)
    for name, rows in flags.items():
        pages.update(_sharded("旗标", name, "旗标 {}".format(name), rows))
    months: Dict[str, List[Dict[str, Any]]] = {}
    years: Dict[str, List[Dict[str, Any]]] = {}
    journals: Dict[str, List[Dict[str, Any]]] = {}
    for e in entries:
        if e.get("month"):
            months.setdefault(e["month"], []).append(e)
        if e.get("year"):
            years.setdefault(str(e["year"]), []).append(e)
        if e.get("journal"):
            journals.setdefault(e["journal"], []).append(e)
    for name, rows in months.items():
        pages["{}/月度/{}.md".format(MOC_DIR, name)] = _moc_page(name, "收录月 {}".format(name), rows)
    for name, rows in years.items():
        pages.update(_sharded("年份", name, "发表年 {}".format(name), rows,
                              shard_key=_shard_month))
    # 期刊只为 ≥5 篇的建页（长尾建了就是死页）
    used: Set[str] = set()
    for name, rows in journals.items():
        if len(rows) >= 5:
            fn = safe_filename(name, used)
            pages["{}/期刊/{}.md".format(MOC_DIR, fn)] = _moc_page(name, "期刊/来源", rows)
    return pages


def build_evidence_pages(entries: List[Dict[str, Any]]) -> Dict[str, str]:
    """按 role（大组再按 bucket）聚合句级证据，点标题直达原句所在块。"""
    pages: Dict[str, str] = {}
    for role in ROLE_ORDER:
        items: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
        for e in entries:
            used: Set[str] = set()
            for h in (e.get("highlights") or []):
                if not isinstance(h, dict):
                    continue
                text = (h.get("text") or "").strip().replace("\n", " ")
                bid = block_id(h.get("role") or "", text, used)
                if h.get("role") == role:
                    items.append((e, h, bid))
        if not items:
            continue
        label = ROLE_LABEL[role]
        if len(items) <= SHARD_THRESHOLD:
            pages["{}/证据/{}.md".format(MOC_DIR, label)] = _evidence_page(label, role, items)
            continue
        # 两级分片：先按维度，仍超阈值的再按年份——手动深读条目 bucket 恒为空，
        # 只按维度切会把 800+ 条全塞进「未分维度」一页。
        groups: Dict[str, List[Tuple]] = {}
        for e, h, bid in items:
            groups.setdefault((e.get("bucket") or ["未分维度"])[0], []).append((e, h, bid))
        idx = ["# {} {}".format(ROLE_EMOJI[role], label), "",
               "共 **{}** 条，分片如下。".format(len(items)), "", LEGACY_WARN, ""]
        for key in sorted(groups):
            rows = groups[key]
            sub = "{}-{}".format(label, key)
            if len(rows) <= SHARD_THRESHOLD:
                pages["{}/证据/{}.md".format(MOC_DIR, sub)] = _evidence_page(sub, role, rows)
                idx.append("- [[{}]]（{} 条）".format(sub, len(rows)))
                continue
            by_year: Dict[str, List[Tuple]] = {}
            for e, h, bid in rows:
                by_year.setdefault(str(e.get("year") or "未知年"), []).append((e, h, bid))
            idx.append("- **{}**（{} 条）：{}".format(
                sub, len(rows),
                " · ".join("[[{}-{}]]({})".format(sub, y, len(by_year[y]))
                           for y in sorted(by_year, reverse=True))))
            for y, rows_y in by_year.items():
                pages["{}/证据/{}-{}.md".format(MOC_DIR, sub, y)] = _evidence_page(
                    "{}-{}".format(sub, y), role, rows_y)
        idx.append("")
        pages["{}/证据/{}.md".format(MOC_DIR, label)] = "\n".join(idx)
    return pages


def _evidence_page(title: str, role: str, items: List[Tuple]) -> str:
    out = ["# {} {}".format(ROLE_EMOJI[role], title), "",
           "共 **{}** 条句级证据。点句子直达原文所在块；`[@key]` 可直接粘进 pandoc 稿。".format(len(items)),
           "", LEGACY_WARN, ""]
    for e, h, bid in items:
        text = (h.get("text") or "").strip().replace("\n", " ")
        out.append("- [[{}#^{}|{}]] — {} {} · `[@{}]`".format(
            e["citekey"], bid, text[:110].replace("|", "/"),
            TIER_EMOJI.get(e.get("priority_tier") or "", ""), e.get("year") or "",
            e["citekey"]))
    out.append("")
    return "\n".join(out)


def build_overview(entries: List[Dict[str, Any]], index: Dict[str, Any],
                   moc_pages: Dict[str, str]) -> str:
    n = len(entries)
    n_ft = sum(1 for e in entries if e.get("has_full_text_reading"))
    n_manual = sum(1 for e in entries if e.get("series") == "manual")
    n_hl = sum(len(e.get("highlights") or []) for e in entries)
    months = sorted({e.get("month") or "" for e in entries if e.get("month")})
    out = ["# 科研札记 vault 总览", "",
           "- 文献：**{}** 篇（全文精读 {} · 手动深读 {}）".format(n, n_ft, n_manual),
           "- 句级证据：**{}** 条".format(n_hl),
           "- 覆盖：**{}** 个月（{} → {}）".format(len(months), months[0] if months else "-",
                                                   months[-1] if months else "-"),
           "- 来源索引：`{}`（{} 篇 keeper）".format("literature_index.json",
                                                    len([p for p in index.get("papers") or []
                                                         if not p.get("duplicate_of")])),
           "",
           "> [!tip] 用法",
           "> 图谱里输 `-path:_MOC` 只看文献与语义相似边；每篇底部「相邻文献」是 TF-IDF 近邻。",
           "> 想按维度/角色/旗标浏览走下面的 MOC；想按关键词取证用仓库里的 `scripts/notes_query.py`。",
           ""]
    for name, flag in (("必须回应 MUST_ENGAGE", "MUST_ENGAGE"), ("对立证据 THREAT", "THREAT"),
                       ("基准 BENCHMARK", "BENCHMARK"), ("过度声称 OVERCLAIM_PRECEDENT",
                                                          "OVERCLAIM_PRECEDENT")):
        rows = [e for e in entries
                if flag in (e.get("flags") or []) or e.get("role") == flag]
        if not rows:
            continue
        out += ["## {}（{} 篇）".format(name, len(rows)), ""] + _TABLE_HEAD
        out += [_row(e) for e in sorted(rows, key=lambda x: x.get("month") or "", reverse=True)]
        out.append("")
    out += ["## 索引页", ""]
    kinds: Dict[str, List[str]] = {}
    for path in sorted(moc_pages):
        parts = path.split("/")
        if len(parts) >= 3:
            kinds.setdefault(parts[1], []).append(parts[2][:-3])
    for kind in ("维度", "角色", "旗标", "证据", "期刊", "年份", "月度"):
        names = kinds.get(kind) or []
        if not names:
            continue
        shown = names if kind != "月度" else names[-12:]
        out.append("- **{}**：{}{}".format(
            kind, " · ".join("[[{}]]".format(x) for x in sorted(shown, reverse=True)),
            "　…共 {} 页".format(len(names)) if len(shown) < len(names) else ""))
    out.append("")
    return "\n".join(out)


README = """# 科研札记 vault

由 `XLBDTranslator-dev/scripts/build_vault.py` 从 `output/scholar_notes/literature_index.json`
生成。**每篇笔记里 `<!-- BEGIN GENERATED -->` 与 `<!-- END GENERATED -->` 之间的内容会被重建覆盖；
下面的 `## 我的札记` 属于你，生成器永不触碰。**

- 删掉 `END GENERATED` 哨兵或把 frontmatter 的 YAML 改坏，重建时该篇**不会被覆盖**，
  而是另存为 `<citekey>.conflict.md` 并在终端报错——手工合并后删掉 .conflict.md 即可。
- frontmatter 里你自己加的键（如 `status:`）与自定 tag（不带 `tier/` `bucket/` 等受管前缀的）都会保留。

## 怎么用

- **图谱**：搜索框输入 `-path:_MOC` 过滤掉索引页，剩下的边是 TF-IDF 语义近邻（每篇底部「相邻文献」）。
- **浏览**：`_MOC/` 下按维度 / 角色 / 旗标 / 证据 / 期刊 / 年份 / 月度 分类，全是静态表格，**不需要任何插件**。
  （装了 Dataview 可以自己写动态查询，但不是前提。）
- **取证**：`_MOC/证据/` 按 citable / refutable / method 聚合了全部句级证据，点句子直达原文所在块。
- **写作**：`[@citekey]` 可直接粘进 pandoc 稿，书目挂 `output/scholar_notes/all_references.json`。

## 重建

```bash
cd ~/Documents/GitHub/XLBDTranslator-dev
PYTHONPATH=. python scripts/build_vault.py --vault-dir <这个目录> --dry-run   # 先看变更
PYTHONPATH=. python scripts/build_vault.py --vault-dir <这个目录>
```

建议在这个目录 `git init` —— 重建后 `git diff` 应当只显示生成块的变化，你写的部分零 diff。
"""


def write_vault(index: Dict[str, Any], notes_dir: Path, vault_dir: Path, *,
                include_maybe: bool = False, k: int = 5, dry_run: bool = False,
                limit: int = 0, force_regen: bool = False) -> Dict[str, Any]:
    """生成/更新 vault。返回 report（供 CLI 打印与退出码判定）。"""
    notes_dir, vault_dir = Path(notes_dir), Path(vault_dir)
    entries = select_papers(index, include_maybe=include_maybe)
    if limit:
        entries = entries[:limit]
    bodies, slice_failures = load_bodies(entries, notes_dir)
    neighbors = compute_neighbors(entries, k=k)
    one_lines = {e["citekey"]: e.get("one_line") or "" for e in entries}

    report: Dict[str, Any] = {
        "selected": len(entries), "written": 0, "new": 0, "merged": 0, "unchanged": 0,
        "conflicts": [], "slice_failures": slice_failures, "moc_pages": 0,
    }

    used_lower: Set[str] = set()
    files: List[Tuple[Path, str, Dict[str, Any]]] = []
    for e in entries:
        fname = safe_filename(e["citekey"], used_lower)
        path = vault_dir / PAPERS_DIR / "{}.md".format(fname)
        gen = render_generated_block(e, bodies.get(e["citekey"]) or [],
                                     neighbors.get(e["citekey"]) or [], one_lines)
        files.append((path, gen, e))

    pages = build_moc_pages(entries)
    pages.update(build_evidence_pages(entries))
    pages[OVERVIEW] = build_overview(entries, index, pages)
    pages["README.md"] = README
    report["moc_pages"] = len(pages) - 2

    if dry_run:
        for path, gen, e in files:
            existing = path.read_text(encoding="utf-8") if path.exists() else None
            content, status = merge_note(e, gen, existing)
            if status == "conflict":
                report["conflicts"].append(e["citekey"])
            elif existing is None:
                report["new"] += 1
            elif content != existing:
                report["merged"] += 1
            else:
                report["unchanged"] += 1
        return report

    for path, gen, e in files:
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        content, status = merge_note(e, gen, existing)
        if status == "conflict":
            report["conflicts"].append(e["citekey"])
            conflict = path.with_suffix(".conflict.md")
            write_if_changed(conflict, assemble(build_frontmatter(e), gen, DEFAULT_USER_ZONE))
            continue
        if status == "new":
            report["new"] += 1
        if force_regen and existing is not None:
            path.write_text(content, encoding="utf-8")
            report["written"] += 1
            report["merged"] += 1
            continue
        if write_if_changed(path, content):
            report["written"] += 1
            if status == "merged":
                report["merged"] += 1
        else:
            report["unchanged"] += 1

    for rel, text in pages.items():
        if write_if_changed(vault_dir / rel, text.rstrip("\n") + "\n"):
            report["written"] += 1

    # _meta.json 是运行记录（每次都变），别让它一直脏着 git status；已存在则尊重用户的版本
    gi = vault_dir / ".gitignore"
    if not gi.exists():
        gi.write_text("{}\n.obsidian/workspace*.json\n".format(META_JSON), encoding="utf-8")

    meta = {"vault_schema": VAULT_SCHEMA_VERSION,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source_index_generated_at": index.get("generated_at"),
            "counts": {k2: v for k2, v in report.items() if isinstance(v, int)},
            "conflicts": report["conflicts"], "slice_failures": slice_failures}
    (vault_dir / META_JSON).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
