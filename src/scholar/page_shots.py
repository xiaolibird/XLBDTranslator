# -*- coding: utf-8 -*-
"""原文截图：把句级证据的页码锚渲染成 PDF 页面图，并按图注裁出论文的图/表。

为什么要有：句级证据是 agent 的**中文转述**，不是原文摘抄——「转述有没有失真」无法靠文本比对
回验（topics 防线保证 citekey 与原句存在，不保证转述准确，见 scholar-notes skill 的实测反例）。
截图把证据直接挂回原文版面：核数字、看表格、看图，一眼到位；也绕开文本抽取的双栏串行与
表格行列串位（脚本草稿系统性偏差的来源之一）。

三个不平凡的点：
  1. **页码口径**：札记里的页码可能是期刊印刷页（Mitra 2023 标的是 13–23，PDF 只有 11 页），
     也可能就是 PDF 页序。`PageMap` 先读 PDF 自带的页标签，再从页眉/页脚的数字推偏移，
     都不成才按 PDF 页序解释；超界返回 None，绝不「就近凑一页」。
  2. **图表裁剪**：按图注（Fig. N / Figure N / Table N / Box N）定位，图取图注上方的
     图形区，表取图注下方直到下一段正文。版面千奇百怪，所以**拿不准就退回整页**，
     并在返回值里标 `how`，调用方据此决定要不要提示「此为整页」。
  3. **幂等与体积**：文件名由 (citekey, 页, 标签) 决定论推出；已存在且不旧于 PDF 就跳过。
     JPEG 而非 PNG——论文页面是照片级连续色调 + 文字，120dpi JPEG q82 约 150–250KB，
     PNG 要 3–5 倍；全库数千页时差的是 GB 级。
"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PAGE_DPI = 120
CROP_DPI = 170          # 图表裁剪区域小，给高一点的分辨率才看得清坐标轴与表内数字
JPEG_QUALITY = 82
ASSET_DIR = "assets/shots"
# 裁剪算法一变就 +1：每篇目录下的 `.v` 记着生成它的版本，不一致则整篇强制重渲染。
# 没有它的话，改了算法重跑也只会命中旧缓存——错图永远留在库里。
SHOT_VERSION = "5"

_KINDS = {"fig": "Fig.", "table": "Table", "box": "Box", "algorithm": "Algorithm"}
_QUAL = r"(Extended\s+Data|Supplementary|Supplemental|Supp\.?|Suppl\.?|Appendix|扩展数据|补充材料|补充|附)?"
# 证据句里的标签：英文 Fig./Figure/Table/Box/Algorithm + 中文 图/表/算法；编号可带 S/A 前缀（附录表）
_LABEL_RE = re.compile(
    _QUAL + r"\s*(Fig(?:ure|\.)?|Table|Box|Algorithm|图|(?<![代列发报图])表|算法)\s*\.?\s*([SA]?\.?\d+)"
    # 「算法 9 种」「表 3 个」是数量不是编号
    r"(?!\d|\s*(?:种|个|项|类|次|篇|条|张|组|步|层|倍|年|天|月|名|例|家|位|维|折|%|％))",
    re.I)
# 版面上的图注：必须在块首；编号后面要么是标点/竖线/换行，要么直接接首字母大写的标题词。
# 「Table 2 presents the F1…」「Tables 2 and 3 summarize…」是正文，不是图注（实测会被误配）。
_CAPTION_RE = re.compile(
    r"^\s*" + _QUAL + r"\s*(Fig(?:ure|\.)?|Table|Box|Algorithm)\s*\.?\s*([SA]?\.?\d+)[a-h]?"
    r"(?P<tail>[^\n]{0,24})", re.I)
_PROSE_TAIL = re.compile(
    r"^\s*(?:,|and\b|&|to\b|through\b|presents?\b|shows?\b|displays?\b|summari[sz]es?\b|reports?\b|"
    r"lists?\b|gives?\b|illustrates?\b|depicts?\b|compares?\b|provides?\b|contains?\b|"
    r"demonstrates?\b|indicates?\b|is\b|are\b|in\b|we\b|the\b|of\b|for\b|also\b)")
# ↑ 刻意**区分大小写**：正文里编号后接的是小写词（"Table 2 presents"），而不少投稿版/预印本的
#   图注是「Figure 1 The flowchart of…」——编号后无标点、直接接首字母大写的标题词。
# 动词永远列不全：真库 4,366 个被接受的图注块里有 46 个是「Figure 9 reveals / places / highlights /
# zooms into…」这类正文句（TabPFN-3.5 的 Fig. 9 因此被定位到引用它的那一页而不是图所在页）。
# 通则：编号后隔空白接**小写词**就是正文。豁免两种真图注：算法标题常是小写函数名
# （"Algorithm 1 sample-batch(D)"）；续表页眉 "Table 1 continued"。
_LOWER_TAIL = re.compile(r"^\s+(?!continued\b|cont\b)[a-z]{2,}\b")


def _is_prose_mention(kind: str, tail: str) -> bool:
    if _PROSE_TAIL.match(tail or ""):
        return True
    return not kind.lower().startswith("algorithm") and bool(_LOWER_TAIL.match(tail or ""))


def _norm(qual: str, kind: str, num: str) -> Tuple[str, str, str]:
    k = kind.lower()
    k = ("fig" if k.startswith("fig") or k == "图" else
         "table" if k in ("table", "表") else
         "algorithm" if k in ("algorithm", "算法") else "box")
    q = re.sub(r"\s+", " ", (qual or "").lower())
    q = ("ext" if q in ("extended data", "扩展数据") else
         "supp" if q.rstrip(".") in ("supplementary", "supplemental", "supp", "suppl", "appendix",
                                      "补充材料", "补充", "附") else "")
    n = num.upper().replace(".", "")
    if not q and n[:1] in ("S", "A"):
        q = "supp"                       # Table S3 / Table A6 本身就是附录编号
    return q, k, n


def norm_label(label: str) -> Optional[Tuple[str, str, str]]:
    """'Fig. 2a' / '图2' / 'Extended Data Fig 1' / 'Table A6' → (限定, 类别, 编号)；认不出返回 None。"""
    m = _LABEL_RE.search(label or "")
    return _norm(m.group(1), m.group(2), m.group(3)) if m else None


def label_text(key: Tuple[str, str, str]) -> str:
    q, k, n = key
    return "{}{} {}".format({"ext": "Extended Data ", "supp": "Supplementary ", "": ""}[q], _KINDS[k], n)


def labels_in(text: str) -> List[str]:
    """从一句证据里抽出它提到的图表标签（去重保序），统一成英文规范写法。"""
    out, seen = [], set()
    for m in _LABEL_RE.finditer(text or ""):
        key = _norm(m.group(1), m.group(2), m.group(3))
        if key[2].lstrip("SA") in ("", "0"):
            continue
        if key not in seen:
            seen.add(key)
            out.append(label_text(key))
    return out


_PAGE_SEP = re.compile(r"[,，、;；]|\s+and\s+|\s*和\s*")
_PAGE_NOT_MAIN = re.compile(r"(?i)^\s*(supp|suppl|supplement|appendix|app\b|附|补充|ext|s\s*\d|§|sec|section|fig|table|图|表)")
_PAGE_RANGE = re.compile(r"(\d+)\s*[-–—~至]\s*(\d+)")


def parse_pages(page: Optional[str]) -> List[int]:
    """札记页码 → 正文页号列表。宁缺勿错：认不准的段落返回空，调用方就不限页、不出整页图。

    '16' → [16]；'15-16' / '17–18' → 区间（上限 6 页防误写）；'p.7' / '第4页' / 'R71' / 'e12' → 单页；
    '5, 7-8' → [5,7,8]（**逐段**解析：此前只看前两个数，会得出 [5,6,7]——多渲染没被引用的第 6 页、
    丢掉第 8 页）；'112-5' → [112..115]（缩写区间）；'241-259' → [241,259]（跨度大只取首尾）；
    'Supp Fig 20' / 'S3' / '§5.7' → []（补充材料页、节号不是正文页码——此前会去渲染正文第 20 页，
    还会把逐句出处的搜索锁在那一页。2026-09-21 对抗审核 F4，写法全部来自真库）。
    """
    if page is None:
        return []
    out: List[int] = []
    for seg in _PAGE_SEP.split(str(page)):
        seg = seg.strip()
        if not seg or _PAGE_NOT_MAIN.match(seg):
            continue
        seg = re.sub(r"(?i)^(pp?\.?|pages?|第)\s*", "", seg)
        m = _PAGE_RANGE.search(seg)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if hi < lo and len(m.group(2)) < len(m.group(1)):          # '112-5' = 112–115
                hi = int(m.group(1)[:-len(m.group(2))] + m.group(2))
            if lo < hi <= lo + 6:
                out.extend(range(lo, hi + 1))
            elif lo != hi:
                out.extend(sorted((lo, hi)))
            else:
                out.append(lo)
            continue
        if re.search(r"\d\.\d", seg):                                  # '5.7'：节号，不是页码
            continue
        nums = re.findall(r"\d+", seg)
        if nums:
            out.append(int(nums[0]))
    seen, uniq = set(), []
    for n in out:
        if n not in seen and n > 0:
            seen.add(n); uniq.append(n)
    return sorted(uniq)[:8]


# ---------------------------------------------------------------------------
# 页码口径
# ---------------------------------------------------------------------------

@dataclass
class PageMap:
    n_pages: int
    offset: int = 0              # 印刷页 = PDF 页序(1 起) + offset
    how: str = "pdf-order"       # label / header / pdf-order
    labels: Optional[Dict[int, int]] = None   # 印刷页 → PDF 页序(1 起)，来自 PDF 页标签

    def to_printed(self, pdf_index: int) -> int:
        """PDF 页序(1 起) → 印刷页（札记与引用定位符用的口径）。"""
        if self.labels:
            for printed, idx in self.labels.items():
                if idx == pdf_index:
                    return printed
        return pdf_index + self.offset

    def to_pdf(self, printed: int) -> Optional[int]:
        """印刷页 → PDF 页序(1 起)；落不到本 PDF 内返回 None。"""
        if self.labels and printed in self.labels:
            return self.labels[printed]
        idx = printed - self.offset
        return idx if 1 <= idx <= self.n_pages else None


def _edge_numbers(page) -> List[int]:
    """页眉/页脚区域里的独立整数（候选印刷页码）。"""
    h = page.rect.height
    out = []
    for x0, y0, x1, y1, txt, *_ in page.get_text("blocks"):
        if y1 < h * 0.09 or y0 > h * 0.91:
            for tok in re.findall(r"(?<![\d.\-/])\d{1,4}(?![\d.\-/])", txt):
                out.append(int(tok))
    return out


def build_page_map(doc, wanted: Optional[List[int]] = None) -> PageMap:
    """推断这份 PDF 的页码口径。`wanted` 是札记里出现过的页码，用来判断要不要找偏移。"""
    n = doc.page_count
    # 1) PDF 自带页标签（出版商 PDF 常有；arXiv 基本没有）
    labels = {}
    try:
        for i in range(n):
            lab = doc[i].get_label()
            if lab and lab.isdigit():
                labels.setdefault(int(lab), i + 1)
    except Exception:
        labels = {}
    if labels and any(k != v for k, v in labels.items()):
        return PageMap(n, 0, "label", labels)
    # 2) 札记页码都落在 PDF 页序内 → 先不急着找偏移；但若页眉页脚强烈指向某个偏移，仍以它为准
    votes: Dict[int, int] = {}
    for i in range(n):
        for num in set(_edge_numbers(doc[i])):
            off = num - (i + 1)
            if off >= 0:
                votes[off] = votes.get(off, 0) + 1
    best = max(votes.items(), key=lambda kv: (kv[1], -kv[0])) if votes else (0, 0)
    need = max(2, (n + 1) // 2)             # 过半页面一致才采信，挡住「每页都印着年份 2023」
    if best[0] > 0 and best[1] >= need:
        return PageMap(n, best[0], "header")
    if wanted and max(wanted) > n and best[0] > 0 and best[1] >= 2:
        return PageMap(n, best[0], "header")   # 札记页码明显超界时放宽门槛
    return PageMap(n, 0, "pdf-order")


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _save(pix, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.jpg")
    pix.save(str(tmp), jpg_quality=JPEG_QUALITY)
    tmp.replace(out)


def render_page(doc, pdf_index: int, out: Path, dpi: int = PAGE_DPI) -> Path:
    import fitz
    page = doc[pdf_index - 1]
    _save(page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False), out)
    return out


def _captions(page) -> List[Tuple[Tuple[str, str, str], "object"]]:
    """本页全部**真**图注：[(标签键, Rect)]。"""
    import fitz
    out = []
    for b in page.get_text("blocks"):
        if len(b) > 6 and b[6] != 0:          # 图像块
            continue
        txt = b[4] or ""
        if re.match(r"^\s*(Tables|Figures|Figs)\b", txt):
            continue
        m = _CAPTION_RE.match(txt)
        if not m or _is_prose_mention(m.group(2), m.group("tail") or ""):
            continue
        out.append((_norm(m.group(1), m.group(2), m.group(3)), fitz.Rect(b[:4])))
    return out


def _caption_blocks(page, key, lenient: bool = False):
    """本页上与标签键匹配的图注块。

    严格：Extended Data 必须两边一致（Nature 论文里 Table 1 与 Extended Data Table 1 是两张表）。
    lenient：忽略限定词——札记常把「Extended Data Table 1」简写成「Table 1」，全文找不到
    严格匹配时才用（见 find_label_page），且回执会标 how='…(lenient)' 提醒人核对。
    """
    q, k, n = key
    return [r for (cq, ck, cn), r in _captions(page)
            if ck == k and cn == n and (lenient or cq == q or "ext" not in (cq, q))]


def _text_blocks(page):
    return [(__import__("fitz").Rect(b[:4]), b[4] or "") for b in page.get_text("blocks")
            if not (len(b) > 6 and b[6] != 0)]


def _full_caption(page, cap):
    """图注常被拆成多个文本块（换行/分栏）。把紧邻其下、左缘对齐的续行并进来。"""
    import fitz
    reg = fitz.Rect(cap)
    rest = sorted([r for r, _ in _text_blocks(page) if r.y0 >= cap.y1 - 2 and r != cap], key=lambda r: r.y0)
    for r in rest:
        if r.y0 - reg.y1 > 5 or abs(r.x0 - cap.x0) > 8 or r.width > cap.width * 1.3:
            break
        reg |= r
    return reg


def _is_prose(rect, text: str, col_width: float) -> bool:
    """成段正文：够宽、多行、以字母为主且有句读。表格单元格/坐标轴标注都不满足。"""
    t = (text or "").strip()
    if len(t) < 160 or rect.width < col_width * 0.6 or t.count("\n") < 2:
        return False
    letters = sum(ch.isalpha() or ch.isspace() for ch in t) / max(len(t), 1)
    return letters > 0.8 and len(re.findall(r"[.;,] ", t)) >= 3


def _stop_before(region, cap, page, others, below: bool):
    """区域向下（或向上）延伸时，遇到别的图注或成段正文就截止。"""
    import fitz
    reg = fitz.Rect(region)
    walls = list(others) + [r for r, t in _text_blocks(page) if r != cap and _is_prose(r, t, reg.width)]
    for w in walls:
        if _x_overlap(w, reg) <= 0:
            continue
        if below and w.y0 >= cap.y1 and w.y0 < reg.y1:
            reg.y1 = min(reg.y1, w.y0 - 2)
        if (not below) and w.y1 <= cap.y0 and w.y1 > reg.y0:
            reg.y0 = max(reg.y0, w.y1 + 2)
    return reg


def _widen(region, cap, page, others):
    """检测到的区域常比实际版面窄一圈（宽表右侧几列、图左缘的子图标号、紧贴表顶的表头行）。

    只并入**紧贴**区域（间隙 < 10pt，小于常见栏间距）且**纵向完全落在区域内**的文字块与线条——
    相邻栏的正文段落通常比图表高、不会完全落在区域纵向范围内，因而不会被拉进来；成段正文与
    别的图注一律不并。纵向只向「远离图注」的一侧补一行以内（表头行/末行）。
    """
    import fitz
    reg = fitz.Rect(region)
    items = [(r, t) for r, t in _text_blocks(page)]
    items += [(fitz.Rect(d["rect"]), "") for d in page.get_drawings()]
    for _ in range(3):                                   # 并入后边界变了，再看一轮
        grew = False
        for r, t in items:
            if r.get_area() > page.rect.get_area() * 0.5 or any(r == o for o in others) or r == cap:
                continue
            if t and _is_prose(r, t, reg.width):
                continue
            inside_y = r.y0 >= reg.y0 - 2 and r.y1 <= reg.y1 + 2
            touch_x = (0 <= reg.x0 - r.x1 < 10) or (0 <= r.x0 - reg.x1 < 10) or \
                      (_x_overlap(r, reg) > 0 and (r.x0 < reg.x0 - 1 or r.x1 > reg.x1 + 1))
            if inside_y and touch_x and r.width < page.rect.width * 0.95:
                new = reg | r
                if new != reg:
                    reg, grew = new, True
        if not grew:
            break
    cap_below = cap.y0 >= region.y0 + region.height * 0.5
    for r, t in items:                                   # 远离图注一侧：紧贴的一行（≤ 16pt 高）
        if t and not _is_prose(r, t, reg.width) and r.height <= 16 and _x_overlap(r, reg) > r.width * 0.5 \
                and not any(r == o or r.intersects(o) for o in others) and not _CAPTION_RE.match(t):
            if cap_below and 0 <= reg.y0 - r.y1 < 6:
                reg.y0 = r.y0
            if (not cap_below) and 0 <= r.y0 - reg.y1 < 6:
                reg.y1 = r.y1
    return reg


def _x_overlap(a, b) -> float:
    return max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))


def _detect_tables(page, **kw):
    import fitz
    try:
        found = page.find_tables(**kw).tables
    except Exception:
        return []
    area = page.rect.get_area()
    out = []
    for t in found:
        r = fitz.Rect(t.bbox)
        if t.row_count >= 2 and t.col_count >= 2 and r.get_area() < area * 0.8:
            out.append(r)
    return out


def _match_tables(caps, tables):
    """把表注配给表格。先整页推断口径（表注在上/在下），再按间距贪心配对。

    口径必须整页一致地推：Scientific Reports 的表注在表**下方**，逐个取最近会把
    「Table 1 的表注」配给紧随其后的 Table 2 的表（2026-09-20 实测 che2018 p.8）。
    """
    best = {}
    for orient in ("cap_above", "cap_below"):
        pairs = []
        for ci, c in enumerate(caps):
            for ti, t in enumerate(tables):
                gap = (t.y0 - c.y1) if orient == "cap_above" else (c.y0 - t.y1)
                if -6 <= gap <= 70 and _x_overlap(c, t) > min(c.width, t.width) * 0.3:
                    pairs.append((gap, ci, ti))
        used_c, used_t, got, cost = set(), set(), {}, 0.0
        for gap, ci, ti in sorted(pairs):
            if ci in used_c or ti in used_t:
                continue
            used_c.add(ci); used_t.add(ti); got[ci] = ti; cost += max(gap, 0)
        if (len(got), -cost) > (len(best.get("m", {})), -best.get("cost", 1e9)) or not best:
            best = {"m": got, "cost": cost, "orient": orient}
    return best.get("m", {}), best.get("orient", "cap_above")


def _rule_table(page, cap, other_caps):
    """三线表兜底：表注旁一组等宽的长横线，首尾之间就是表。"""
    import fitz
    rules = []
    for d in page.get_drawings():
        r = fitz.Rect(d["rect"])
        if r.height < 2.5 and r.width > max(80, cap.width * 0.5):
            rules.append(r)
    if not rules:
        return None
    for below in (True, False):
        near = sorted([r for r in rules if (r.y0 >= cap.y1 - 3 if below else r.y1 <= cap.y0 + 3)],
                      key=lambda r: r.y0 if below else -r.y0)
        if not near or abs((near[0].y0 - cap.y1) if below else (cap.y0 - near[0].y1)) > 40:
            continue
        first = near[0]
        group = [r for r in near if abs(r.x0 - first.x0) < 15 and abs(r.x1 - first.x1) < 15]
        for oc in other_caps:                       # 别越过下一个图注
            group = [r for r in group if (r.y0 < oc.y0 if below and oc.y0 > cap.y1 else True)
                     and (r.y1 > oc.y1 if (not below) and oc.y1 < cap.y0 else True)]
        if len(group) >= 2:
            # 不能用 `reg |= r`：画线命令画出的表线高度为 0，fitz 把它当空矩形、求并时直接忽略——
            # 三条 booktabs 线并出来还是第一条线，随后被「区域过小」判失败。LaTeX 出的 ML 论文
            # 几乎全是这种表（2026-09-21 用 TabPFN-3.5 的 Table 2–5 查出）。
            return fitz.Rect(min(r.x0 for r in group), min(r.y0 for r in group),
                             max(r.x1 for r in group), max(r.y1 for r in group))
    return None


def find_figure_bbox(page, label: str):
    """按图注定位图/表/Box/算法的版面区域。返回 (fitz.Rect | None, how)。

    how: 'caption+graphics' 图注 + 图形区 / 'caption+table' 图注 + 检测到的表 /
         'caption+rules' 三线表兜底 / 'box' 有底色的整框 / 'not-found'。
    拿不准就返回 (None, 'not-found')，调用方退回整页——错裁比不裁危险，它会让人核错对象。
    """
    import fitz
    key = norm_label(label)
    if not key:
        return None, "not-found"
    caps_all = _captions(page)
    mine = _caption_blocks(page, key)
    lenient = False
    if not mine:
        mine, lenient = _caption_blocks(page, key, lenient=True), True
    if not mine:
        return None, "not-found"
    cap0 = mine[0]
    kind = key[1]
    pr = page.rect
    others = [r for _, r in caps_all if r != cap0]
    cap = _full_caption(page, cap0) if kind != "box" else cap0
    for o in others:                              # 续行不许并过下一个图注
        if o.y0 >= cap0.y1 and o.y0 < cap.y1:
            cap.y1 = o.y0 - 2
    gfx = []
    for d in page.get_drawings():
        r = fitz.Rect(d["rect"])
        if r.width > 4 and r.height > 4 and r.get_area() < pr.get_area() * 0.9:
            gfx.append(r)
    for img in page.get_images(full=True):
        try:
            gfx.extend(fitz.Rect(r) for r in page.get_image_rects(img[0]))
        except Exception:
            pass
    blocks = [fitz.Rect(b[:4]) for b in page.get_text("blocks") if not (len(b) > 6 and b[6] != 0)]

    if kind == "table":
        tcaps = [r for (q, k, n), r in caps_all if k == "table"]
        region, how = None, "not-found"
        for kw in ({}, {"horizontal_strategy": "lines", "vertical_strategy": "text"}):
            tabs = _detect_tables(page, **kw)
            if not tabs:
                continue
            m, _ = _match_tables(tcaps, tabs)
            ci = tcaps.index(cap0)
            if ci in m:
                region, how = fitz.Rect(tabs[m[ci]]) | cap, "caption+table"
                break
        if region is None:
            rt = _rule_table(page, cap, others)
            if rt is None:
                return None, "not-found"
            region, how = rt | cap, "caption+rules"
        below = region.y1 > cap.y1 + 4
        region = _stop_before(region, cap, page, others, below=below) | cap
        if region.height < cap.height + 30:       # 只裁到表注本身（无线表没检出）→ 交给整页兜底
            return None, "not-found"
        # 有表线的表，底边（表注在下时是顶边）以**最外一条表线**为准：检测到的表格区域常多带一行
        # 紧随其后的小节标题（太短，不算成段正文，_stop_before 挡不住；serrano2026 p.7 实测）。
        hr = [fitz.Rect(d["rect"]) for d in page.get_drawings()]
        # 不能用 r.intersects：画线命令画出的表线高度为 0，fitz 视空矩形与任何东西都不相交
        hr = [r for r in hr if r.height < 2.5 and r.width > region.width * 0.35
              and _x_overlap(r, region) > 0 and region.y0 - 2 <= r.y0 and r.y1 <= region.y1 + 2]
        if len(hr) >= 2:
            if below and region.y1 - max(r.y1 for r in hr) > 10:
                region.y1 = max(r.y1 for r in hr) + 4
            if (not below) and min(r.y0 for r in hr) - region.y0 > 10:
                region.y0 = min(r.y0 for r in hr) - 4
        for t in blocks:                          # 表脚注：紧贴表底的小字块
            if 0 <= t.y0 - region.y1 < 10 and _x_overlap(t, region) > t.width * 0.6 and t.height < 60 \
                    and not any(t == o for o in others):
                region |= t
    elif kind in ("fig", "algorithm"):
        def grow(side_above: bool):
            pool = ([r for r in gfx if r.y1 <= cap.y0 + 6] if side_above else
                    [r for r in gfx if r.y0 >= cap.y1 - 6])
            # 同栏或跨栏的图形才算；页眉装饰线之类与图注不相干的不要
            pool = [r for r in pool if _x_overlap(r, cap) > 0 or r.width > pr.width * 0.5]
            pool.sort(key=(lambda r: -r.y1) if side_above else (lambda r: r.y0))
            if not pool:
                return None
            edge = pool[0].y1 if side_above else pool[0].y0
            if abs((cap.y0 - edge) if side_above else (edge - cap.y1)) > 60:
                return None                        # 离图注太远，多半不是它的图
            reg = fitz.Rect(pool[0])
            for r in pool[1:]:
                gap = (reg.y0 - r.y1) if side_above else (r.y0 - reg.y1)
                if gap < 40:
                    reg |= r
            for oc in others:                      # 不越过别的图注
                if side_above and oc.y1 <= cap.y0 and oc.y1 > reg.y0 and _x_overlap(oc, reg) > 0:
                    reg.y0 = oc.y1 + 2
                if (not side_above) and oc.y0 >= cap.y1 and oc.y0 < reg.y1 and _x_overlap(oc, reg) > 0:
                    reg.y1 = oc.y0 - 2
            return reg
        region = grow(True) or grow(False)         # 多数期刊图注在图下；JMIR 等在图上
        if region is None:
            return None, "not-found"
        for t, txt in _text_blocks(page):          # 坐标轴/图例等图内文字
            # 文字环绕排版时，旁边那栏正文会与图形区相交——成段正文不是图的一部分（cornelis2026 Fig.1）
            if t.intersects(region) and t.height < region.height and not any(t == o for o in others) \
                    and not _is_prose(t, txt, region.width) and t != cap0:
                region |= t
        region |= cap
        how = "caption+graphics"
    else:                                          # Box：标题块所在的有底色矩形
        holders = [r for r in gfx if r.contains(cap0) and r.get_area() > cap0.get_area() * 4]
        if not holders:
            return None, "not-found"
        region = min(holders, key=lambda r: r.get_area())
        how = "box"
    if kind != "box":
        region = _widen(region, cap, page, others)
    region = (region + (-8, -8, 8, 8)) & pr
    for o in others:                               # 8pt 留白不许压进紧随其后的下一个图注
        if _x_overlap(o, region) > 0 and o.y0 >= cap.y1 and region.y0 < o.y0 < region.y1:
            region.y1 = o.y0 - 1
    if region.get_area() < pr.get_area() * 0.02 or region.height < 36:
        return None, "not-found"
    return region, how + ("(lenient)" if lenient else "")


def render_figure(doc, pdf_index: int, label: str, out: Path, dpi: int = CROP_DPI):
    """裁出图/表；定位失败返回 (None, 'not-found')，不落盘。"""
    import fitz
    page = doc[pdf_index - 1]
    rect, how = find_figure_bbox(page, label)
    if rect is None:
        return None, how
    _save(page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), clip=rect, alpha=False), out)
    return out, how


def find_label_page(doc, label: str, hint: Optional[int] = None) -> Optional[int]:
    """图注在哪一页（PDF 页序，1 起）。先看 hint 页及其前后一页——图常被排到引用它的下一页。"""
    n = norm_label(label)
    if not n:
        return None
    order = list(range(1, doc.page_count + 1))
    if hint:
        order.sort(key=lambda i: (abs(i - hint), i))
    for lenient in (False, True):
        for i in order:
            if _caption_blocks(doc[i - 1], n, lenient):
                return i
    return None


# ---------------------------------------------------------------------------
# 面向调用方：一篇论文要哪些截图
# ---------------------------------------------------------------------------

def shot_name(citekey: str, printed: int, label: str = "", tag: str = "") -> str:
    safe = re.sub(r"[^\w.-]+", "_", citekey, flags=re.U)
    suf = "_" + tag if tag else ""
    if label:
        q, k, num = norm_label(label)
        return "{}/{}{}{}{}.jpg".format(safe, q, k, num, suf)
    return "{}/p{:04d}{}.jpg".format(safe, printed, suf)


def pdf_tag(pdf_path: Path) -> Tuple[List[int], str]:
    """PDF 指纹 [size, mtime_ns] 与它的 6 位短标记。同一 citekey 可能对应两份 PDF（同一篇在两个
    月份桶里各读过一次，甚至是期刊版与 arXiv 版），凡按 citekey 落盘的派生物都要靠它区分。"""
    import hashlib
    st = Path(pdf_path).stat()
    fp = [st.st_size, st.st_mtime_ns]
    return fp, hashlib.sha1("{}:{}".format(*fp).encode()).hexdigest()[:6]


_TAGGED = re.compile(r"_[0-9a-f]{6}\.jpg$")


def _owner_tag(cdir: Path, tag: str) -> str:
    """这个 citekey 目录里不带后缀的文件名（p0016.jpg / fig2.jpg）归哪份 PDF 所有。

    第一份来的 PDF 占用裸名（绝大多数论文只有一份 PDF，文件名保持好读）；之后来的**另一份** PDF
    一律用带指纹后缀的名字。此前整页图/图表图按 citekey 共用裸名，而陈旧判据是「图比 PDF 旧」——
    图刚渲染过、永远比两份 PDF 都新——于是后一份 PDF 静默复用前一份渲染的图，回执和 md 都显示
    成功，图却来自另一个版本（对抗审核 F3；真库 choi2020Learning 的 AAAI 版与 arXiv 版共用 5 张）。
    """
    f = cdir / ".pdf"
    try:
        owner = f.read_text().strip()
    except OSError:
        owner = ""
    if not owner:
        cdir.mkdir(parents=True, exist_ok=True)
        f.write_text(tag)
        owner = tag
    return owner


def shots_for_paper(pdf_path: Path, citekey: str, highlights: List[dict], out_root: Path,
                    force: bool = False) -> Dict[str, dict]:
    """渲染一篇论文被证据引用到的全部页面与图表。

    返回 {相对路径: {"kind": page|figure, "printed": 16, "pdf_page": 4, "label": "Fig. 2", "how": ...}}；
    另有键 "_map" 记页码口径、"_missing" 记落不到 PDF 内的页码/找不到的图表（供回执如实上报）。
    """
    import fitz
    pdf_path = Path(pdf_path)
    out: Dict[str, dict] = {}
    missing: List[str] = []
    wanted_pages: List[int] = []
    wanted_labels: List[Tuple[str, Optional[int]]] = []
    for h in highlights:
        pages = parse_pages(h.get("page") or h.get("pages"))
        wanted_pages.extend(pages)
        for lab in labels_in(h.get("text") or ""):
            wanted_labels.append((lab, pages[0] if pages else None))
    if not wanted_pages and not wanted_labels:
        return {"_map": {"how": "none"}, "_missing": []}
    cdir = out_root / shot_name(citekey, 1).split("/")[0]
    _, mytag = pdf_tag(pdf_path)
    tag = "" if _owner_tag(cdir, mytag) == mytag else mytag       # 裸名归第一份 PDF，另一份带后缀
    vfile = cdir / (".v_" + tag if tag else ".v")
    if not force and (not vfile.exists() or vfile.read_text().strip() != SHOT_VERSION):
        force = True
        if vfile.parent.is_dir():                 # 旧版本的整页图/图表图清掉，免得留下不再被引用的错图
            # 逐句出处图（q_*）不归这里管：它们有自己的版本戳 PASSAGE_VERSION 与缓存。这里一并删掉的话，
            # passages 缓存仍记着「这句已命中」→ 不重算 → 返回时又因文件不在被滤掉：那一句的出处图
            # 静默消失且永不自愈（2026-09-21 对抗审核 F1；升一次 SHOT_VERSION 就是全库 1.5 万张）。
            for old in vfile.parent.glob("*.jpg"):
                mine = old.name.endswith("_{}.jpg".format(tag)) if tag else not _TAGGED.search(old.name)
                if mine and not old.name.startswith("q_"):       # 只清**这份 PDF** 名下的图
                    old.unlink()
    with fitz.open(str(pdf_path)) as doc:
        pm = build_page_map(doc, wanted_pages)
        stale_before = pdf_path.stat().st_mtime
        for printed in sorted(set(wanted_pages)):
            idx = pm.to_pdf(printed)
            if idx is None:
                missing.append("p.{}".format(printed))
                continue
            rel = shot_name(citekey, printed, tag=tag)
            dst = out_root / rel
            if force or not dst.exists() or dst.stat().st_mtime < stale_before:
                render_page(doc, idx, dst)
            out[rel] = {"kind": "page", "printed": printed, "pdf_page": idx}
        seen = set()
        for lab, printed in wanted_labels:
            key = norm_label(lab)
            if key in seen:
                continue
            seen.add(key)
            hint = pm.to_pdf(printed) if printed else None
            idx = find_label_page(doc, lab, hint)
            if idx is None:
                missing.append(lab)
                continue
            rel = shot_name(citekey, 0, lab, tag=tag)
            dst = out_root / rel
            if force or not dst.exists() or dst.stat().st_mtime < stale_before:
                got, how = render_figure(doc, idx, lab, dst)
            else:
                got, how = dst, "cached"
            if got is None:
                # 图注找到了但区域拿不准：退回图注所在的整页，宁可大也不裁错
                rel = "{}/pdf{:04d}{}.jpg".format(rel.split("/")[0], idx, "_" + tag if tag else "")
                dst = out_root / rel
                if force or not dst.exists() or dst.stat().st_mtime < stale_before:
                    render_page(doc, idx, dst)
                out.setdefault(rel, {"kind": "page", "pdf_page": idx, "how": "figure-fallback", "labels": []})
                out[rel].setdefault("labels", []).append(lab)
                continue
            out[rel] = {"kind": "figure", "label": lab, "pdf_page": idx, "how": how}
        vfile.parent.mkdir(parents=True, exist_ok=True)
        vfile.write_text(SHOT_VERSION)
        out["_map"] = {"how": pm.how, "offset": pm.offset, "n_pages": pm.n_pages}
        out["_missing"] = missing
    return out


# ---------------------------------------------------------------------------
# 句 → 原文出处段落（2026-09-20：用户要的是「每一句都有对应正文位置的截图」，不只是图表）
# ---------------------------------------------------------------------------
# 证据句是中文转述、原文是英文，无法靠文本搜索定位。做法：把 PDF 正文切成行级滑动窗口，
# 用多语种向量（bge-m3）跨语言匹配；句中数字在窗口里出现是强加分；札记标了页码的只在
# 那一页找。够自信才配图——配错出处比不配更糟，它会让人对着错的原文去核。

PASSAGE_DPI = 150
WIN_LINES, WIN_STRIDE = 6, 3
PASSAGE_MIN_SCORE = 0.62   # 0.60–0.62 一档实测出现过「相关但不是出处」的弱匹配（shi2025 缺失率设置→Table 2 表注）
_REF_HEAD = re.compile(r"^\s*(References|REFERENCES|Bibliography|参考文献)\s*$")
_NUM = re.compile(r"\d+(?:\.\d+)?")
# 这些分节是 agent 自己的话，原文里没有出处
NO_SOURCE_SECTIONS = ("对我研究的联想", "交叉核验记录", "逐节通读要点", "版本差异")


def passage_windows(doc) -> List[dict]:
    """正文的行级滑动窗口：[{page(1起), rect, text}]。References 之后、页眉页脚不要。"""
    import fitz
    out: List[dict] = []
    for pi in range(doc.page_count):
        page = doc[pi]
        h = page.rect.height
        stop = False
        for blk in page.get_text("dict").get("blocks", []):
            if blk.get("type") != 0:
                continue
            lines = []
            for ln in blk.get("lines", []):
                txt = "".join(sp.get("text", "") for sp in ln.get("spans", [])).strip()
                r = fitz.Rect(ln["bbox"])
                if not txt or r.y1 < h * 0.06 or r.y0 > h * 0.945:
                    continue
                if _REF_HEAD.match(txt):
                    stop = True
                    break
                lines.append((r, txt))
            if stop:
                break
            if not lines:
                continue
            n = len(lines)
            starts = [0] if n <= WIN_LINES + 2 else list(range(0, n - WIN_LINES + 1, WIN_STRIDE))
            if starts[-1] + WIN_LINES < n and n > WIN_LINES + 2:
                starts.append(n - WIN_LINES)
            for s0 in starts:
                seg = lines if n <= WIN_LINES + 2 else lines[s0:s0 + WIN_LINES]
                text = re.sub(r"(\w)- (\w)", r"\1\2", " ".join(t for _, t in seg))
                if len(text) < 60:
                    continue
                rect = fitz.Rect(seg[0][0])
                for r, _ in seg[1:]:
                    rect |= r
                out.append({"page": pi + 1, "rect": tuple(rect), "text": text, "blk": (pi, blk.get("number", 0)), "pos": s0})
        if stop:
            break
    return out


def _nums(text: str) -> set:
    return {x.rstrip("0").rstrip(".") if "." in x else x for x in _NUM.findall(text.replace(",", ""))} - {"1", "2", "3", "0"}


def locate_passages(doc, sentences: List[dict], embed, page_map: Optional[PageMap] = None,
                    windows: Optional[List[dict]] = None, win_vecs=None) -> List[Optional[dict]]:
    """为每句找原文出处。返回与 sentences 等长的列表：{page, rect, score} 或 None。

    sentences: [{"text", "page"(札记页码，可无), "section"}]；embed: texts -> (n,d) ndarray。
    """
    import numpy as np
    import fitz
    wins = windows if windows is not None else passage_windows(doc)
    res: List[Optional[dict]] = [None] * len(sentences)
    todo = [i for i, s in enumerate(sentences)
            if not any((s.get("section") or "").startswith(k) for k in NO_SOURCE_SECTIONS) and (s.get("text") or "").strip()]
    if not wins or not todo:
        return res
    W = win_vecs if win_vecs is not None else embed([w["text"] for w in wins])
    Q = embed([sentences[i]["text"] for i in todo])
    W = W / (np.linalg.norm(W, axis=1, keepdims=True) + 1e-9)
    Q = Q / (np.linalg.norm(Q, axis=1, keepdims=True) + 1e-9)
    sim = Q @ W.T
    wnums = [_nums(w["text"]) for w in wins]
    wpage = np.array([w["page"] for w in wins])
    for qi, si in enumerate(todo):
        s = sentences[si]
        sc = sim[qi].copy()
        hint = s.get("page")
        if not hint:                               # 句子正文里自带的页码提示：「（§5.7, p.14）」
            m = re.search(r"(?<![A-Za-z])pp?\.\s*(\d{1,4}(?:\s*[-–]\s*\d{1,4})?)", s.get("text") or "")
            hint = m.group(1) if m else None
        want = [page_map.to_pdf(p) for p in parse_pages(hint)] if page_map else []
        want = [w for w in want if w]
        if want:                                   # 札记标了页码：只在那一页（含区间）里找
            sc[~np.isin(wpage, want)] = -1.0
        qn = _nums(s["text"])
        if qn:
            sc = sc + np.array([min(0.12, 0.04 * len(qn & wn)) for wn in wnums])
        b = int(np.argmax(sc))
        if sc[b] < PASSAGE_MIN_SCORE:
            continue
        rect = fitz.Rect(wins[b]["rect"])
        for nb in (b - 1, b + 1):                  # 出处常跨窗口：相邻窗口也够像就并进来
            if 0 <= nb < len(wins) and wins[nb]["blk"] == wins[b]["blk"] and sc[nb] >= sc[b] - 0.04:
                rect |= fitz.Rect(wins[nb]["rect"])
        res[si] = {"page": wins[b]["page"], "rect": tuple(rect), "score": round(float(sc[b]), 3)}
    return res


def render_passage(doc, pdf_index: int, rect, out: Path, dpi: int = PASSAGE_DPI) -> Path:
    import fitz
    page = doc[pdf_index - 1]
    clip = (fitz.Rect(rect) + (-10, -1.5, 10, 1.5)) & page.rect   # 纵向留白再大就会露出邻行的半行字
    _save(page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), clip=clip, alpha=False), out)
    return out


PASSAGE_VERSION = "3"


def sentence_key(text: str) -> str:
    import hashlib
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:10]


def passages_for_paper(pdf_path: Path, citekey: str, sentences: List[dict], out_root: Path, embed,
                       force: bool = False) -> Dict[str, dict]:
    """逐句原文出处截图。返回 {句子键: {"rel", "printed", "score"}}（没把握的句子不在其中）。

    结果缓存在 `<citekey>/passages.json`，键 = PDF 指纹 + 算法版本 + 句子内容哈希：finalize 是
    整月重建，不缓存的话每入库一篇新论文，当月所有旧论文都要重新算一遍向量（126 篇的月份
    要十几分钟）。只有**新句子**才触发向量计算；PDF 换了或算法升版则整篇重算。

    `embed=None` 是只读缓存模式：Ollama 没起时 finalize 也要把**已经算过**的出处图写回 md——
    此前那种情况下整步跳过，整月 md 会被重写成一张逐句图都没有的版本（对抗审核 F5）。
    """
    import json
    import fitz
    pdf_path = Path(pdf_path)
    safe = shot_name(citekey, 1).split("/")[0]
    cdir = out_root / safe
    fp, tag = pdf_tag(pdf_path)
    # 缓存与截图文件名都带 PDF 指纹：同一 citekey 可能对应两份 PDF（同一篇在两个月份桶里各读过
    # 一次）。按 citekey 共用一份缓存时，后重建的月份会因「指纹变了」清空目录，把先重建那个月
    # 的截图删掉——2026-09-20 全量实测 4 篇、209 个坏链。
    cfile = cdir / "passages.{}.json".format(tag)
    cache = {}
    if cfile.exists() and not force:
        try:
            cache = json.loads(cfile.read_text("utf-8"))
        except Exception:
            cache = {}
    if cache.get("pdf_fp") != fp or cache.get("v") != PASSAGE_VERSION:
        cache = {"pdf_fp": fp, "v": PASSAGE_VERSION, "items": {}}
        if cdir.is_dir():
            for old in cdir.glob("q_{}_*.jpg".format(tag)):
                old.unlink()
    items = cache["items"]
    keyed = [(sentence_key(x.get("text")), x) for x in sentences]
    # 命中过但图不在了（被误删、assets 目录被清过）也算新句：缓存只记「算过」，图才是产物，
    # 不重算的话这一句的出处图永远回不来。负缓存（None）不受影响。
    new = [(k, x) for k, x in keyed
           if k not in items or (items[k] and not (out_root / items[k]["rel"]).is_file())]
    if embed is None:                                  # 向量服务不可用：只读缓存，算过的图照出，新句留给下一轮
        new = []
    if new:
        with fitz.open(str(pdf_path)) as doc:
            pm = build_page_map(doc, [p for _, x in keyed for p in parse_pages(x.get("page"))])
            found = locate_passages(doc, [x for _, x in new], embed, pm)
            for (k, _), r in zip(new, found):
                if r is None:
                    items[k] = None
                    continue
                rel = "{}/q_{}_{}.jpg".format(safe, tag, k)
                render_passage(doc, r["page"], r["rect"], out_root / rel)
                items[k] = {"rel": rel, "printed": pm.to_printed(r["page"]), "score": r["score"]}
        cdir.mkdir(parents=True, exist_ok=True)
        tmp = cfile.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
        tmp.replace(cfile)
    return {k: items[k] for k, _ in keyed if items.get(k) and (out_root / items[k]["rel"]).is_file()}
