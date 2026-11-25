# -*- coding: utf-8 -*-
"""
全文精读：OA/arXiv 论文 → 下载 PDF → 抽全文 → 强模型精读 → 结构化 + 句级角色标记。

句级角色按「对后续工作流的用途」（句级）：可引用证据 / 可反驳观点 / 方法论借鉴。
付费墙无全文 → 基于摘要的降级精读（from_full_text=False）。
复用：fulltext.ipv4_client 下载、PyMuPDF(fitz) 抽文本、llm_client.LLMClient 调模型。
"""
import hashlib
import json
import re
import dataclasses as _dc
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .schema import (
    PaperSegment, CloseReading, CloseReadSection, CloseReadSentence,
)
from .fulltext import (
    ipv4_client, resolve_oa_pdf, europepmc_fulltext, europepmc_pmcid,
)
from ..utils.logger import get_logger
from ..utils.json_tools import strip_code_fences as _strip_json

logger = get_logger(__name__)


# ---------------- LLM 调用元数据（可信度加固 WO-2）----------------

def call_llm_with_meta(llm, prompt: str, **kwargs) -> Tuple[str, Optional[Any]]:
    """调 llm，返回 (文本, CallMeta|None)。

    真实 LLMClient 走 call_with_meta 拿「实际应答者」；测试里的假 llm（只实现 .call，
    或 MagicMock）退回 .call，meta 为 None——判据是**类上**有没有 call_with_meta，
    这样 MagicMock 自动生成的属性不会被误当成带 meta 的客户端。
    """
    if hasattr(type(llm), "call_with_meta"):
        text, meta = llm.call_with_meta(prompt, **kwargs)
        return text, meta
    return llm.call(prompt, **kwargs), None


def meta_provenance(meta) -> Dict[str, Any]:
    """CallMeta → {provider, served_model, fell_back}；meta 为 None（取不到）时三项均 None。"""
    if meta is None:
        return {"provider": None, "served_model": None, "fell_back": None}
    return {"provider": meta.provider or None,
            "served_model": meta.served_model or None,
            "fell_back": bool(meta.fell_back)}


def chunk_models_from_metas(metas) -> Optional[List[str]]:
    """各块 meta → served_model 去重（保序）列表；一块 meta 都没收到（假 llm）→ None。"""
    metas = [m for m in (metas or []) if m is not None]
    if not metas:
        return None
    out: List[str] = []
    for m in metas:
        if m.served_model and m.served_model not in out:
            out.append(m.served_model)
    return out


def apply_meta_to_closereading(cr: CloseReading, meta) -> None:
    """把应答者留痕写进 CloseReading（model 仍是请求值，不动）。"""
    for k, v in meta_provenance(meta).items():
        setattr(cr, k, v)


def prompt_fingerprint(label: str, *templates: str) -> str:
    """prompt 指纹 `label@md5前8位`（对模板原文取，与 workflow._get_filter_prompt_version 同法）。"""
    digest = hashlib.md5("\x00".join(templates).encode("utf-8")).hexdigest()[:8]
    return "{}@{}".format(label, digest)


_VALID_TAGS = {"可引用证据", "可反驳观点", "方法论借鉴"}


# ---------------- PDF 下载 + 抽文本 ----------------

# auto 精读的抽文本预算：总量 40000 字符，单页最多 20000 字符。
# 单页上限的依据：本地 4829 页实测 median 3940 / p99 12852，超过 20000 的只有 16 页（0.33%），
# 全是图表矢量文字层的病态页——一页就能吃光整篇预算，让精读永远读不到正文后半。
# 取 20000 而非更小值是为了不误伤正常长页；把覆盖面推到后半篇是总预算的职责，不靠压低单页上限硬挤。
AUTO_MAX_CHARS = 40000
AUTO_PAGE_MAX_CHARS = 20000


_ARXIV_PDF_RE = re.compile(r"^https?://(?:www\.)?arxiv\.org/pdf/([^?#]+?)(?:\.pdf)?(?:[?#].*)?$", re.I)


def _arxiv_export_url(url: str) -> Optional[str]:
    """arxiv.org/pdf/<id>[.pdf] → export.arxiv.org/pdf/<id>；非 arXiv 主站 PDF 链接返回 None。"""
    m = _ARXIV_PDF_RE.match(url or "")
    return "https://export.arxiv.org/pdf/{}".format(m.group(1)) if m else None


def download_pdf(url: str, dest: Path, client=None, timeout: float = 60.0,
                 max_bytes: int = 40_000_000, *, validate: bool = False) -> Optional[Path]:
    """下载 PDF 到 dest。非 200 / 非 PDF / 超限 / 异常 → None（不抛出）。

    validate=True 时加 fulltext.validate_pdf_bytes 三闸（体积 / 可解析页数），并带 PDF Accept 头、
    对 403/429 做礼貌退避——额外通道（EPMC render / OpenAlex / S2）给的链接反爬页也回 200，
    存下来会在 _pdf_text_with_stats 炸成空文本。默认 False = 历史行为逐字节不变。
    """
    if not url:
        return None
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    own = client is None
    c = client or ipv4_client(timeout=timeout)
    try:
        if validate:
            from .fulltext import polite_get, PDF_ACCEPT_HEADERS
            r = polite_get(c, url, follow_redirects=True, headers=PDF_ACCEPT_HEADERS)
        else:
            r = c.get(url)
        alt = _arxiv_export_url(url) if r.status_code in (403, 406, 429) else None
        if alt:
            # 主站 CDN 按 IP+TLS 指纹罚 406（见 fulltext.tls_context），罚期内 export 镜像照常可用
            # （2026-09-22 实测同一时刻主站 406、export 200）。只回退一次，仍受 _arxiv_throttle 节流。
            logger.info("  ↪ arXiv 主站返回 {}，改走 export 镜像".format(r.status_code))
            r = c.get(alt)
        if r.status_code != 200:
            logger.warning("  ⚠️ 下载 PDF 返回 {}: {}".format(r.status_code, url[:60]))
            return None
        data = r.content
        if len(data) > max_bytes:
            logger.warning("  ⚠️ PDF 超过 {}MB，跳过: {}".format(max_bytes // 1_000_000, url[:60]))
            return None
        if validate:
            from .fulltext import validate_pdf_bytes, _PDF_MAGIC_WINDOW
            magic_ok = b"%PDF-" in data[:_PDF_MAGIC_WINDOW]
        else:
            magic_ok = data[:5].startswith(b"%PDF")
        if not magic_ok:
            logger.warning("  ⚠️ 非 PDF 内容，跳过: {}".format(url[:60]))
            return None
        if validate:
            ok, why = validate_pdf_bytes(data)
            if not ok:
                logger.warning("  ⚠️ PDF 校验不过（{}），跳过: {}".format(why, url[:60]))
                return None
        dest.write_bytes(data)
        return dest
    except Exception as e:
        logger.warning("  ⚠️ 下载 PDF 失败({}): {}".format(url[:60], e))
        return None
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass


@_dc.dataclass
class PdfText:
    """一次 PDF 抽文的全部产物（2026-09-05 起 `_pdf_text_ex` 的返回值）。

    - text：喂读正文——先按单页上限削页、再按总上限截尾之后的文本；
    - raw_chars：全部页 get_text 原长之和，**不受任何上限影响**（回答「这篇有多长」）；
    - page_dropped：被单页上限丢掉的字符数之和（>0 即「单页上限削页」这种成因成立）；
    - tail：总上限之外的余量（已过单页上限），供两轮读的第二轮；空串 = 没被总上限截尾；
    - fractions_fixed：按 drawing 层修回的叠式分数个数。
    """
    text: str = ""
    raw_chars: int = 0
    page_dropped: int = 0
    tail: str = ""
    n_pages: int = 0
    fractions_fixed: int = 0

    @property
    def truncated_by_total_cap(self) -> bool:
        return bool(self.tail)

    @property
    def truncated_by_page_cap(self) -> bool:
        return self.page_dropped > 0


# ---- 叠式分数（docs/bugs/2026-09-03-ingest-draft-systematic-errors.md 第 1 类） ----
# PyMuPDF 把「8 над 3」排成两行普通文本，分数线是 drawing 不是字符，文本层拼出 `83`。
# 几何上这是确定的：一条短细横线，正上方与正下方各有一个短词块且横向重叠。
_FRAC_LINE_MIN_W = 2.0     # pt；短于这个的是下划线残片/噪点
_FRAC_LINE_MAX_W = 40.0    # pt；长于这个的是表格线/分隔线
_FRAC_LINE_MAX_H = 0.8     # pt；线段的垂直偏差上限（真正的水平线）
_FRAC_GAP = 9.0            # pt；分子底边/分母顶边到分数线的最大距离
_FRAC_TOKEN_MAX_LEN = 6    # 分子/分母词块的最大长度（8、3、dt、pss、(t) 这类）


_FRAC_TOKEN_RE = re.compile(r"^[\w(){}\-+.|·]{1,6}$")     # 不含 [ ] ,：引文标号 [13] 与表格单元 `low,` 不是操作数


def _fraction_like(num: str, den: str) -> bool:
    """分子/分母长得像分数的操作数。宁可漏（漏 = 维持旧行为）也不要误改。

    排除的形态都来自 30 份真 PDF 的扫描（2026-09-05）：表格格线上下的两格单词（Embed/Angle）、
    引文标号（[13] / in）、表格里的 NA 与小数（0.97 / NA）、`from` 下面一条格线一个 `2`。
    规则：都是 ≤6 字符的干净词块；不含逗号方括号、不以 . 结尾；任一侧是 ≥2 字母的纯字母串
    （ex、NA、in、al）就不算——单字母变量（r、n、B、x）允许；且至少一侧含数字/符号，或两侧都 ≤2 字符。
    """
    for t in (num, den):
        if not _FRAC_TOKEN_RE.match(t) or t.endswith((".", ",")):
            return False
        if t.isalpha() and len(t) >= 2:
            return False
    for t in (num, den):
        if t.count("(") != t.count(")") or t.count("{") != t.count("}"):
            return False
    numeric = lambda t: bool(re.fullmatch(r"-?\d+(?:\.\d+)?", t))     # noqa: E731
    if numeric(num) and numeric(den):
        # 两个纯数字上下叠：真分数几乎只有整数比（8/3、1/2）；带负号或 ≥2 位小数的是表格单元格
        if num.startswith("-") or den.startswith("-"):
            return False
        if any(len(t.split(".")[1]) >= 2 for t in (num, den) if "." in t):
            return False
    has_digit_or_sym = any(ch.isdigit() or ch in "(){}-+.|" for ch in num + den)
    return has_digit_or_sym or (len(num) <= 2 and len(den) <= 2)


def stacked_fractions(page) -> list:
    """从 drawing 层认出该页的叠式分数：返回 [(分子, 分母)]，按页面自上而下排序。

    只认「短细横线 + 上下各**恰一个**短词块 + 横向重叠」这一形态；任何一项不满足就不算——
    宁可漏（漏了 = 维持旧行为）也不要误认。返回 [(分子, 分母, 是否在阅读顺序里相邻)]；
    多词块分子（`dx(t)`）会漏——取舍见 bug md。
    """
    try:
        drawings = page.get_drawings()
        # sort=True：与 _pdf_text_ex 抽正文用的 get_text("text", sort=True) 同一阅读顺序，
        # 这样「分子词块与分母词块在 words 里相邻」⇔「它们在正文里相邻」——几何证据与文本证据
        # 才真正指向同一位置（2026-09-05 第 1 轮审计 R1-2.1：全页正则找相邻对会改到别处的数字）。
        words = page.get_text("words", sort=True)  # (x0, y0, x1, y1, text, block, line, word)
    except Exception:                             # noqa: BLE001
        return []
    horiz = []                                     # 该页全部水平线段（任意宽度），用来认表格行线
    for d in drawings:
        for it in d.get("items", []):
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.y - p2.y) <= _FRAC_LINE_MAX_H:
                    horiz.append((min(p1.x, p2.x), max(p1.x, p2.x), (p1.y + p2.y) / 2))
            elif it[0] == "re" and it[1].height <= _FRAC_LINE_MAX_H:   # 极扁矩形也是画出来的横线
                r_ = it[1]
                horiz.append((r_.x0, r_.x1, (r_.y0 + r_.y1) / 2))
    # 同一条线常被 get_drawings 报两遍（路径描边 + 回程），先按坐标去重，否则一个分数认成两个
    horiz = sorted({(round(a, 1), round(b, 1), round(yy, 1)) for a, b, yy in horiz})
    lines = []
    for x0, x1, y in horiz:
        if not (_FRAC_LINE_MIN_W < (x1 - x0) < _FRAC_LINE_MAX_W):
            continue
        # 分数线是**孤立**的：同一水平高度上还有别的线段 = 表格行线被切成了一格一格
        #（30 份真 PDF 扫描里 `94 / 0.005`、`72.5 / 78.9` 这类假阳性全是表格）。
        if sum(1 for a, b, yy in horiz if abs(yy - y) <= 0.6 and not (a == x0 and b == x1)) >= 1:
            continue
        lines.append((x0, x1, y))
    out = []
    for x0, x1, y in lines:
        bar_w = x1 - x0
        cx = (x0 + x1) / 2

        def _pick(pred):
            """该侧**恰一个**词块：与分数线横向重叠 ≥ 一半自身宽度、居中偏差 < 0.6 倍线宽、
            自身不比线宽太多（分数线是按操作数宽度画的）。多于一个候选 = 不是干净的分数，放弃。
            返回 (词块在 words 里的下标, 词面)。"""
            cand = []
            for i, w in enumerate(words):
                tok = w[4]
                if not tok or len(tok) > _FRAC_TOKEN_MAX_LEN or not pred(w):
                    continue
                ww = max(0.1, w[2] - w[0])
                ov = min(w[2], x1) - max(w[0], x0)
                if ov < 0.5 * ww:
                    continue
                if abs((w[0] + w[2]) / 2 - cx) > 0.6 * max(bar_w, 1.0):
                    continue
                if ww > 1.8 * bar_w + 2:
                    continue
                cand.append((i, tok))
            return cand[0] if len(cand) == 1 else None

        pn = _pick(lambda w: w[3] <= y + 1 and (y - w[3]) < _FRAC_GAP)
        pd_ = _pick(lambda w: w[1] >= y - 1 and (w[1] - y) < _FRAC_GAP)
        if not pn or not pd_ or not _fraction_like(pn[1], pd_[1]):
            continue
        # 分子词块与分母词块在阅读顺序里紧挨着（真实排版里 PyMuPDF 把二者拼成同一行，
        # 如 `83`）才允许改文本；不相邻说明文本层里它们落在别处，改写无处可落、一律不动。
        adjacent = (pd_[0] - pn[0]) == 1
        out.append((y, pn[1], pd_[1], adjacent))
    out.sort()
    seen, uniq = set(), []
    for _y, n, d, adj in out:
        if (n, d) not in seen:           # 同页同一分数只报一次（fix 时按唯一相邻匹配改）
            seen.add((n, d))
            uniq.append((n, d, adj))
    return uniq


def fix_stacked_fractions(text: str, fracs) -> tuple:
    """把文本层里紧邻出现的「分子 分母」改写成「分子/分母」。返回 (新文本, 改了几处)。

    只在该分子分母对在页文本里**恰好唯一**匹配时才改：分数是几何证据、相邻是文本证据，
    两者都成立才动手；多处匹配说明词面有歧义（同一页多次出现 `1` 与 `N`），一律不改。
    """
    if not text or not fracs:
        return text, 0
    n_fixed = 0
    for fr in fracs:
        num, den = fr[0], fr[1]
        if len(fr) > 2 and not fr[2]:
            continue                      # 几何上是分数，但分子分母在阅读顺序里不相邻：文本无处可改
        pat = re.compile(r"(?<![\w.])" + re.escape(num) + r"[ \t]*\n?[ \t]*" + re.escape(den) + r"(?![\w])")
        hits = list(pat.finditer(text))
        if len(hits) != 1:
            continue
        m = hits[0]
        text = text[:m.start()] + "{}/{}".format(num, den) + text[m.end():]
        n_fixed += 1
    return text, n_fixed


def _fraction_fix_enabled() -> bool:
    """settings.processing.pdf_fraction_fix 的进程级开关；这里是纯函数模块，不读 settings，
    由 settings 加载时写进环境变量 XLBD_PDF_FRACTION_FIX（缺省 = 开）。"""
    import os
    return os.environ.get("XLBD_PDF_FRACTION_FIX", "1").strip().lower() not in ("0", "false", "no", "off")


# ---- 参考文献区识别（两轮读第二轮要把它剔掉，否则尾段预算全喂给了引文列表） ----
_REF_MARK_RE = re.compile(
    r"\b(?:19[5-9]\d|20[0-4]\d)[a-z]?\b"          # 年份
    r"|\[\d{1,3}\]"                                # [12]
    r"|\bdoi:|10\.\d{4,9}/"                        # DOI
    r"|\bpp?\.\s*\d|\bvol\.\s*\d|\bno\.\s*\d"      # 页/卷/期
    r"|\bet al\.?|\bIn:\s|\bProc(?:eedings|\.)\b|\barXiv:\s*\d",
    re.I)
_REF_WINDOW = 2000
_REF_MIN_DENSITY = 7        # 每 2000 字符至少 7 个引文标记才算「引文密度高」（正文段落一般 0~3，但引用密集的讨论段可达 11~12——靠「至少一条像条目的行」这一关挡住，见 strip_reference_block）
_REF_MIN_WINDOWS = 2        # 至少连续 2 个窗口（≈4000 字符）才认作参考文献区


_REF_HEADING_RE = re.compile(r"^\s*(?:\d+\.?\s*)?(?:references|bibliography|literature cited|works cited)\s*$",
                             re.I | re.M)
# 一条引文条目的行首形态：[12] / 12. Author / Author, A. / Author AB, / Author, Anne
_REF_ENTRY_RE = re.compile(r"^\s*(?:\[\d{1,3}\]|\d{1,3}\.\s+[A-Z]|[A-Z][A-Za-z'\-]+,\s+(?:[A-Z]\.|[A-Z][a-z]+)|[A-Z][A-Za-z'\-]+\s+[A-Z]{1,3}[,.])")


def _is_ref_entry_line(line: str) -> bool:
    """行首像条目**且**行内至少有一个引文标记（年份 / [n] / DOI / pp.）才算——
    「Appendix A. Proof of …」「However, The …」行首也像，但没有年份，不是条目。"""
    return bool(_REF_ENTRY_RE.match(line)) and _ref_marks(line) > 0


def _ref_marks(seg: str) -> int:
    """窗口内的引文标记数。`w′[1]`、`x[k]` 这类紧跟字母/撇号的方括号不算（那是下标，不是引文）。"""
    n = 0
    for m in _REF_MARK_RE.finditer(seg):
        if m.group(0).startswith("[") and m.start() > 0 and (seg[m.start() - 1].isalnum() or seg[m.start() - 1] in "'′)"):
            continue
        n += 1
    return n


def strip_reference_block(text: str, *, window: int = _REF_WINDOW,
                          min_density: int = _REF_MIN_DENSITY,
                          min_windows: int = _REF_MIN_WINDOWS) -> tuple:
    """去掉文本里的参考文献区。返回 (剩余文本, 去掉的字符数)。

    两步：先按固定窗口数引文标记（年份 / [n] / DOI / pp. / et al.）找**最长的连续高密度段**——
    不认标题（`References` 常与别的字词同行、大小写编号五花八门，实测 10 份里 6 份认不出）；
    再把边界**收缩到行**：起点向后收到「References 标题行」或第一条像引文条目的行，终点向前收到
    最后一条像引文条目的行——按 2000 字符整窗切会在首尾各多吞最多 1999 字符正文
    （第 1 轮审计 R1-2.2）。附录在它之后、正文在它之前，两边都保留并按原序拼接。
    识别不出来（或收缩后剩不到半个窗口）就原样返回——代价只是第二轮预算里混进引文，不会丢正文。
    """
    if not text or len(text) < window * min_windows:
        return text, 0
    dens = [_ref_marks(text[s:s + window]) for s in range(0, len(text), window)]
    best = (0, 0)
    cur = None
    for i, d in enumerate(dens + [0]):
        if d >= min_density and cur is None:
            cur = i
        elif d < min_density and cur is not None:
            if i - cur > best[1] - best[0]:
                best = (cur, i)
            cur = None
    if best[1] - best[0] < min_windows:
        return text, 0
    s, e = best[0] * window, min(best[1] * window, len(text))

    # ---- 边界收缩到行 ----
    # 起点：往前一个窗口里若有 References 标题行，从标题行开始；否则从密集段内第一条像条目的行开始
    lo = max(0, s - window)
    head = _REF_HEADING_RE.search(text, lo, e)
    if head is not None:
        s2 = head.start()
    else:
        m = _REF_ENTRY_RE.search(text[s:e], 0, len(text[s:e]))
        # 用 finditer 在段内按行找第一条条目行
        s2 = None
        for lm in re.finditer(r"(?m)^.*$", text[s:e]):
            if _is_ref_entry_line(lm.group(0)):
                s2 = s + lm.start()
                break
        if s2 is None:
            # 既没有 References 标题、也没有任何一条像引文条目的行——这不是参考文献区，
            # 而是引用密集的正文（讨论/相关工作里 `(Smith et al., 2019)` 式引用让标记数达标）。
            # 整窗切会把正文当参考文献剔掉（第 2 轮审计 R2-3.5），必须原样返回。
            return text, 0
        del m
    # 终点：向后再看一个窗口，取最后一条像条目的行的行尾
    hi = min(len(text), e + window)
    e2 = None
    for lm in re.finditer(r"(?m)^.*$", text[s2:hi]):
        if _is_ref_entry_line(lm.group(0)):
            e2 = s2 + lm.end()
    if e2 is None or e2 <= s2:
        e2 = e
    if e2 - s2 < window // 2:
        return text, 0
    return text[:s2] + text[e2:], e2 - s2


def _pdf_text_ex(path: Path, max_chars: int = 40000,
                 page_max_chars: Optional[int] = None,
                 fix_fractions: Optional[bool] = None) -> PdfText:
    """抽全文并给出全部量尺（见 PdfText）。失败返回空 PdfText（text=""、raw_chars=0）。

    测试替身兼容：既有测试（test_closereading / test_closereading_deep / test_fulltext_extra_routes）
    monkeypatch 的是模块属性 `_pdf_text_with_stats`。若发现它被换成了别的可调用对象，就把抽取交给
    替身、只带回 (text, raw)——尾段与削页量在替身下为空/0，符合替身语义。真正的抽取在
    `_pdf_text_ex_impl`；薄包装 `_pdf_text_with_stats` 直接调 impl 而不是本函数，所以
    「spy 型」替身（替身内部再调回原函数）不会绕成无限递归（第 1 轮审计 R1-1.2）。
    """
    cur = globals().get("_pdf_text_with_stats")
    if cur is not None and cur is not _PDF_TEXT_WITH_STATS_ORIG:
        t, r = cur(path, max_chars=max_chars, page_max_chars=page_max_chars)
        return PdfText(text=t or "", raw_chars=int(r or 0))
    return _pdf_text_ex_impl(path, max_chars=max_chars, page_max_chars=page_max_chars,
                             fix_fractions=fix_fractions)


def _pdf_text_ex_impl(path: Path, max_chars: int = 40000,
                      page_max_chars: Optional[int] = None,
                      fix_fractions: Optional[bool] = None) -> PdfText:
    """真正的抽取（不查测试替身）。

    raw_chars = 全部页 get_text 的原长之和，**不受 page_max_chars / max_chars 影响**——
    它要回答的是「这篇论文有多长」，而 text 回答的是「我们喂进去多少」，二者相减才是截断量。
    若 raw 也按单页上限收，truncated 判定会自证为假（cap 后的长度 == 喂进去的长度）。
    只遍历一次 PDF：抽两遍要多付一次解析开销，且两次结果不保证逐字节一致。

    fix_fractions（缺省取进程开关）：按 drawing 层修叠式分数，见 stacked_fractions。
    """
    try:
        import fitz  # PyMuPDF
    except Exception as e:
        logger.warning("  ⚠️ 未安装 PyMuPDF，无法抽全文: {}".format(e))
        return PdfText()
    if fix_fractions is None:
        fix_fractions = _fraction_fix_enabled()
    try:
        parts = []
        raw_chars = 0
        page_dropped = 0
        n_fixed = 0
        n_pages = 0
        # 不再在页循环里按累计长度提前 break：读完所有页、末尾统一截断。
        # 对输出逐字节无影响（原 break 发生在越界页 append 之后），但让后续统计能拿到全页原始长度。
        with fitz.open(str(path)) as doc:
            for page in doc:
                n_pages += 1
                t = page.get_text("text", sort=True)
                if not t:
                    continue
                if fix_fractions:
                    fr = stacked_fractions(page)
                    if fr:
                        t, k = fix_stacked_fractions(t, fr)
                        n_fixed += k
                raw_chars += len(t)          # 先记原长，再做任何截断
                if page_max_chars is not None and len(t) > page_max_chars:
                    page_dropped += len(t) - page_max_chars
                    t = t[:page_max_chars]
                parts.append(t)
        joined = "".join(parts)
        if n_fixed:
            logger.info("  叠式分数修回 {} 处（{}）".format(n_fixed, Path(path).name))
        return PdfText(text=joined[:max_chars], raw_chars=raw_chars, page_dropped=page_dropped,
                       tail=joined[max_chars:], n_pages=n_pages, fractions_fixed=n_fixed)
    except Exception as e:
        logger.warning("  ⚠️ 抽全文失败({}): {}".format(path, e))
        return PdfText()


def _pdf_text_with_stats(path: Path, max_chars: int = 40000,
                         page_max_chars: Optional[int] = None):
    """抽全文并同时给出「原始长度」：返回 (text, raw_chars)。失败返回 ("", 0)。

    薄包装：真正的抽取在 `_pdf_text_ex`（2026-09-05 起），签名与返回类型向后兼容——
    既有调用方（pdf_ingest.extract_pdf_text_with_stats、测试桩）不必改。
    """
    # 直接调 impl（不经 _pdf_text_ex 的替身检查）：spy 型替身调回本函数时不会再绕回替身
    r = _pdf_text_ex_impl(path, max_chars=max_chars, page_max_chars=page_max_chars)
    return r.text, r.raw_chars


# 供 _pdf_text_ex 判断「_pdf_text_with_stats 有没有被测试 monkeypatch 成替身」（见其函数头注释）。
_PDF_TEXT_WITH_STATS_ORIG = _pdf_text_with_stats


def pdf_to_text(path: Path, max_chars: int = 40000,
                page_max_chars: Optional[int] = None) -> str:
    """用 PyMuPDF(fitz) 抽全文纯文本，截断到 max_chars。失败返回空串。

    page_max_chars 非 None 时，每页先截到该上限再拼接：避免单张图表矢量文字层页
    （实测有 45000+ 字符的病态页）把整篇预算吃光，导致精读只覆盖到论文最前面几页。
    默认 None = 保持原有单页语义，manual 链路（pdf_ingest.extract_pdf_text）不传即行为不变，
    分块边界不漂移、历史札记与新札记可比。

    薄包装：真正的抽取在 _pdf_text_with_stats，签名与返回类型保持向后兼容。
    """
    return _pdf_text_with_stats(path, max_chars=max_chars,
                                page_max_chars=page_max_chars)[0]


# ---------------- 精读 prompt + 解析 ----------------

_CLOSEREAD_PROMPT = """你是一名方法学审稿助手，为一位研究者做论文全文精读。

## 研究者的研究主线（据此判断联想）
{research_interests}

## 待精读论文
标题：{title}
{body_label}：
{body}

## 任务
基于上面的{body_label}，输出结构化中文精读，分为这些小节：
研究问题、方法与数据、实验方法、关键结论、可质疑点、对我研究的联想。
「实验方法」节按可复现标准整理：数据集/划分、预处理、模型与超参（优化器/学习率/batch/epoch/种子/硬件）、
评估协议与指标、基线、代码与数据可得性。**仅在可得文本确有报告时才写，{page_rule}
可得文本未报告的写「原文未报告：X」，不许推测填补**（只有摘要时本节通常很短，这是正常的）。
每个小节由若干句子组成。按**对后续工作流的用途**给句子打一个标记 tag：
- "可引用证据"：该句含具体数字/效应量/可溯源结果，写作时可直接取证
- "可反驳观点"：该句是作者的主张/立场/可质疑处，是写 critique 的靶子
- "方法论借鉴"：该句描述的方法/建模思路，对我的研究有方法学借鉴价值
其余句子 tag 置为 null（如纯背景/动机）。允许同一小节里不同句子有不同 tag（句级）。

## 只输出 JSON（不要额外文字）
{{
  "sections": [
    {{"heading": "研究问题", "sentences": [{{"text": "……", "tag": null}}]}},
    {{"heading": "方法与数据", "sentences": [{{"text": "……", "tag": "方法论借鉴"}}]}},
    {{"heading": "实验方法", "sentences": [{{"text": "……（可得文本未报告的写「原文未报告：X」）", "tag": "方法论借鉴"}}]}},
    {{"heading": "关键结论", "sentences": [{{"text": "……", "tag": "可引用证据"}}]}},
    {{"heading": "可质疑点", "sentences": [{{"text": "……", "tag": "可反驳观点"}}]}},
    {{"heading": "对我研究的联想", "sentences": [{{"text": "……", "tag": null}}]}}
  ]
}}"""



def parse_closeread(response: str) -> Optional[CloseReading]:
    """把 LLM 的 JSON 精读解析为 CloseReading。解析失败返回 None。"""
    try:
        data = json.loads(_strip_json(response))
    except Exception:
        return None
    raw_sections = data.get("sections") if isinstance(data, dict) else None
    if not isinstance(raw_sections, list):
        return None
    sections = []
    for sec in raw_sections:
        if not isinstance(sec, dict):
            continue
        sents = []
        for st in sec.get("sentences", []) or []:
            if not isinstance(st, dict):
                continue
            txt = (st.get("text") or "").strip()
            if not txt:
                continue
            tag = st.get("tag")
            tag = tag if tag in _VALID_TAGS else None
            sents.append(CloseReadSentence(text=txt, tag=tag))
        if sents:
            sections.append(CloseReadSection(heading=(sec.get("heading") or "").strip() or "精读", sentences=sents))
    if not sections:
        return None
    return CloseReading(sections=sections)


_NUM_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*")


def verify_citable_numbers(cr: CloseReading, body_text: str, label: str = "") -> int:
    """廉价数值幻觉防线：「可引用证据」句里的数字必须在喂入正文中出现过。

    这些句子会进 highlight/向量库/scholar-write 取证链，被当成可溯源事实直接写进
    论文——一个模型编出来（或自行换算出来）的数字在下游没有任何再核验的机会。
    校验口径故意保守：数字 token 去千分位逗号后做子串匹配，任一 token 在正文里
    找不到 → 该句 tag 降级为 None（句子保留、内容不动，只是不再冒充可引用证据），
    逐句留告警。换算/改写过的数字（0.44→44%）也会被降级——无法逐字溯源的数字
    本来就不该顶着「可引用」进库。返回降级句数。
    """
    body_norm = body_text.replace(",", "")
    demoted = 0
    for sec in cr.sections:
        for s in sec.sentences:
            if s.tag != "可引用证据":
                continue
            missing = [t for t in _NUM_TOKEN_RE.findall(s.text)
                       if t.replace(",", "") not in body_norm]
            if missing:
                s.tag = None
                demoted += 1
                logger.warning("  ⚠️ 可引用证据数字回查不中({})，降级为普通句：{}…（未命中: {}）"
                               .format(label, s.text[:50], " ".join(missing[:5])))
    return demoted


# 「模型侧挂了」的判据：额度/鉴权终局（401/402/403/quota…）+ 限流（429 / RESOURCE_EXHAUSTED /
# rate limit / session limit）。前者复用 pdf_ingest.is_credit_error（整词匹配，防 "正文 4021 字符"
# 这类误触），后者是本函数补的——gemini 免费层耗尽回的正是 429 RESOURCE_EXHAUSTED，
# claude-agent 的会话上限回的是 "session limit"。
_RATE_LIMIT_RE = re.compile(r"(?i)\b429\b|resource[_ ]exhausted|rate.?limit|too many requests"
                            r"|session limit|exceeded your current quota|overloaded")


def is_llm_unavailable(exc) -> bool:
    """这次失败是不是**模型侧**不可用（额度耗尽/限流/鉴权），而不是「这篇没正文可读」。

    两者在账本里此前完全同形（都记 `no_output`）：2026-09-04 那批 260 篇里，35 篇全文明明
    已经到手、纯粹是 gemini 免费层耗尽，却和「Unpaywall/EPMC 都没有这篇」混在一起进了
    待下载清单，而 expand 批重跑会跳过 failed——它们就此安静地等一个根本不需要的 PDF。
    见 docs/bugs/2026-09-04-quota-failure-looks-like-no-fulltext.md。
    """
    from .pdf_ingest import is_credit_error      # 函数内 import：pdf_ingest 模块级 import 本模块
    if is_credit_error(exc):
        return True
    return bool(_RATE_LIMIT_RE.search(str(exc)))


def close_read(seg: PaperSegment, body_text: str, research_interests: str,
               llm, model: Optional[str] = None, from_full_text: bool = True,
               source: Optional[str] = None,
               raw_chars: Optional[int] = None,
               diag: Optional[dict] = None) -> Optional[CloseReading]:
    """对单篇论文做精读。llm 为 LLMClient；body_text 为全文或摘要。失败返回 None。

    raw_chars 为抽取源的原始长度（未截断），由调用方从抽取环节透传；None = 未知。

    diag：可选出参字典，记下**这次为什么没产出**——`no_body`（既无全文也无摘要）/
    `llm_error` + `llm_unavailable`（模型侧挂了：额度、限流、鉴权）/ `parse_failed`（返回不可解析）。
    调用方（backfill_deepread）据此把「真抓不到全文」与「额度耗尽」分开记账、分开熔断。
    """
    def _note(**kw):
        if diag is not None:
            diag.update(kw)

    if not body_text or not body_text.strip():
        # 既无 OA 全文又无摘要 → 这篇在札记里会整篇没有精读内容。别静默，否则
        # 只能靠事后数 highlights 才发现（2026-07-27 实测踩过）。
        logger.warning("  ⚠️ 无全文也无摘要，跳过精读({}): {}".format(
            seg.paper_id[:8], (seg.metadata.title or "")[:50]))
        _note(no_body=True)
        return None
    # 页码规则按输入定：auto 链路喂的 pdf_to_text/EPMC 正文没有 [p.N] 页锚，模型看不见
    # 页边界，此时"可标注页码"的邀请只会诱导编页码（backfill 链踩过的同款教训）。
    # 只有文本里真带页锚时才允许标页码，且必须按锚标。
    has_page_anchors = "[p." in body_text
    page_rule = ("并按可得文本中的 [p.N] 页锚标注页码；" if has_page_anchors
                 else "**可得文本没有页码标记，禁止标注任何页码**（此时写出的页码必然是编造）；")
    prompt = _CLOSEREAD_PROMPT.format(
        research_interests=research_interests or "（未提供研究主线）",
        title=seg.metadata.title or "",
        body_label="全文" if from_full_text else "摘要",
        body=body_text,
        page_rule=page_rule,
    )
    try:
        resp, _meta = call_llm_with_meta(llm, prompt, model=model, max_tokens=8192, json_mode=True)
    except Exception as e:
        logger.warning("  ⚠️ 精读 LLM 调用失败({}): {}".format(seg.paper_id[:8], e))
        _note(llm_error="{}: {}".format(type(e).__name__, str(e)[:200]),
              llm_unavailable=is_llm_unavailable(e))
        return None
    cr = parse_closeread(resp)
    if cr is None:
        logger.warning("  ⚠️ 精读输出解析失败({})".format(seg.paper_id[:8]))
        _note(parse_failed=True)
        return None
    cr.from_full_text = from_full_text
    cr.model = model
    cr.source = source
    # 量尺：喂进去多少 / 源有多长 / 是否被截断。auto 走的是一次性单跳，故 n_chunks=1。
    cr.body_chars = len(body_text)
    cr.body_chars_raw = raw_chars
    cr.truncated = (raw_chars is not None and raw_chars > len(body_text))
    cr.reading_depth = "single-call"
    cr.n_chunks = 1
    apply_meta_to_closereading(cr, _meta)
    cr.prompt_version = prompt_fingerprint("closeread-single", _CLOSEREAD_PROMPT)
    return cr


def deep_close_read(seg: PaperSegment, body_text: str, research_interests: str,
                    llm, model: Optional[str] = None, source: Optional[str] = None,
                    from_full_text: bool = True,
                    max_chunks: int = 12,
                    diag: Optional[dict] = None) -> Optional[CloseReading]:
    """分块深读：切块 → 逐块通读 → 汇总成扩展分节精读。失败返回 None（调用方回落单跳）。

    完全复用 manual 精读的三个纯函数与两个 prompt——分节补齐（结果与效应量 / 图表与补充
    材料要点 / 逐节通读要点）与「保留关键数值」的硬要求都是免费带来的，auto 侧不另写 prompt。

    截断量三件套（body_chars/body_chars_raw/truncated）由调用方补：raw_chars 只有抽取
    环节知道，这里拿不到，放在一处落盘才不会两边算法不一致。
    """
    # 函数内 import：pdf_ingest 模块级已 `from .closereading import pdf_to_text`，
    # 这里再在模块级反向 import 会成环。
    from .pdf_ingest import chunk_text, deep_read_chunks, synthesize_deep_read

    chunks = chunk_text(body_text)[:max_chunks]
    if not chunks:
        if diag is not None:
            diag["no_body"] = True
        return None
    chunk_metas: List[Any] = []
    notes = deep_read_chunks(chunks, llm, model, max_workers=4, meta_out=chunk_metas)
    if diag is not None:
        # 块级的额度/鉴权终局由 deep_read_chunks 标 `_api_error`（限流走同一条 except）；
        # 全部块都挂且带该标记 = 模型侧不可用，不是「这篇没正文」。
        n_ok = sum(1 for n in notes if not n.get("_error"))
        if n_ok == 0 and any(n.get("_api_error") for n in notes):
            diag["llm_unavailable"] = True
            diag.setdefault("llm_error", "分块通读全部失败且为额度/鉴权/限流类错误")
    # 接住汇总步的预算裁剪信息：max_chunks=12 并不能保证不越 60000 预算（实测 12 块 ×
    # 每块 ~5.8k 字符即超），而 auto 链路没有 manual 那样的亲读核验兜底，裁剪必须留痕——
    # 否则 reading_depth="chunked" / n_chunks=12 是在虚报"完整分块深读"。
    _binfo = {}
    cr, _one_line, _api_err = synthesize_deep_read(notes, llm, model, research_interests,
                                                   budget_info=_binfo)
    if cr is None:
        # 全块失败或汇总失败：交回调用方走单跳，保证最差不比现状差
        if diag is not None and _api_err:
            diag["llm_unavailable"] = True
            diag.setdefault("llm_error", "汇总步遇额度/鉴权类终局")
        return None
    # synthesize_deep_read 是给 manual 链路写的，它把 source 写死 'manual-pdf'、
    # 并**无条件**置 from_full_text=True。两项都必须由 auto 侧覆写回真实值：
    # source 关系到 reading_source 统计与 series 语义；from_full_text 一旦被污染，
    # notes_index 的 has_full_text_reading、vault 选集、ingest/backfill 的 full_text
    # 计数会一起失真，且写进 sidecar 后不可逆。
    cr.source = source
    cr.from_full_text = from_full_text
    cr.reading_depth = "chunked"
    cr.n_chunks = len(chunks)
    # 应答者留痕：provider/served_model/fell_back 已由 synthesize_deep_read 按汇总步写好；
    # 这里补各块的 served_model（块间换人时可见）与 auto 链路的 prompt 标签
    cr.chunk_models = chunk_models_from_metas(chunk_metas)
    if any(m is not None and m.fell_back for m in chunk_metas):
        cr.fell_back = True
    from .pdf_ingest import chunked_prompt_version
    cr.prompt_version = chunked_prompt_version("closeread-chunked")
    if _binfo.get("note"):
        cr.synth_truncated = True
        cr.synth_dropped_chunks = int(_binfo.get("n_dropped") or 0)
        logger.warning("  ⚠️ 汇总预算裁剪：{}".format(_binfo["note"]))
    return cr


def _route_kwargs(extra_routes: bool, route_delay: float, budget=None) -> dict:
    """额外通道开着才产出关键字参数；关着返回空 dict → 下游调用形状与历史版本完全一致。

    budget（fulltext.RouteBudget）非空时一并透传：同一精读批共享一份额度与熔断状态。"""
    if not extra_routes:
        return {}
    kw = {"extra_routes": True, "route_delay": route_delay}
    if budget is not None:
        kw["budget"] = budget
    return kw


def _set_truncation_causes(cr: CloseReading, ex: Optional["PdfText"]) -> None:
    """把两种截断成因写到 CloseReading 上。ex=None（EPMC/摘要路径，没有页级信息）时只能判总上限：
    EPMC 正文没有「页」，page_cap 恒 False；total_cap 由 raw > body 推得。"""
    if ex is not None:
        cr.truncated_by_total_cap = ex.truncated_by_total_cap
        cr.truncated_by_page_cap = ex.truncated_by_page_cap
    else:
        cr.truncated_by_page_cap = False
        cr.truncated_by_total_cap = bool(cr.truncated)


def read_tail_appendix(cr: CloseReading, seg: PaperSegment, tail_text: str,
                       research_interests: str, llm, model: Optional[str],
                       *, max_chars: int, max_chunks: int, source: Optional[str],
                       diag: Optional[dict] = None) -> str:
    """两轮读的第二轮：把总上限之外的尾段（去掉参考文献区）按独立预算再读一遍，
    产出的分节以「附录·」前缀并入 cr.sections。返回**真正喂进模型的尾段文本**（"" = 没读）——
    调用方用它（而不是整段原始尾段）拼数字回查的对照源，长度即 tail_chars。

    为什么不是抬上限：closeread_max_chars 与汇总预算是零和耦合，抬上限 = 拿已读章节的保真度
    换尾部覆盖（settings 注释与 test_closeread_chunk_coupling.py 钉着这条）。另起一轮各自预算
    才是加法。前缀「附录·」让 notes_index._CR_SECTION_RE 照常解析、highlights 的 section 溯源
    能区分两轮，vault 单篇页也能一眼看出哪些句子来自尾段。
    """
    if not tail_text or max_chars <= 0:
        return ""
    body, n_refs = strip_reference_block(tail_text)
    body = body[:max_chars]
    if diag is not None:
        diag["tail_refs_cut"] = n_refs                 # 被当成参考文献剔掉的字符数，可回查
    if len(body.strip()) < 2000:                 # 去掉引文后只剩零碎，不值一次汇总
        if diag is not None:
            diag["tail_skipped"] = "尾段去掉参考文献（{} 字符）后仅 {} 字符".format(n_refs, len(body.strip()))
        return ""
    cr2 = deep_close_read(seg, body, research_interests, llm, model=model,
                          source=source, from_full_text=True, max_chunks=max_chunks)
    if cr2 is None or not cr2.sections:
        if diag is not None:
            diag["tail_failed"] = True
        return ""
    for sec in cr2.sections:
        sec.heading = "附录·" + sec.heading
    cr.sections.extend(cr2.sections)
    cr.n_chunks = (cr.n_chunks or 0) + (cr2.n_chunks or 0)
    if cr2.synth_truncated:
        cr.synth_truncated = True
        cr.synth_dropped_chunks = (cr.synth_dropped_chunks or 0) + (cr2.synth_dropped_chunks or 0)
    logger.info("  两轮读第二轮：尾段 {} 字符（剔除参考文献 {}）→ {} 块 / {} 节".format(
        len(body), n_refs, cr2.n_chunks, len(cr2.sections)))
    # 返回**真正喂进模型的尾段**：数字回查的对照源只能是模型读过的文本，不能是整段原始尾段
    #（被剔掉的参考文献里年份/页码必然命中，会给编出来的数字背书——第 1 轮审计 R1-2.6）。
    return body


def close_read_segment(seg: PaperSegment, research_interests: str, llm,
                       email: str = "", model: Optional[str] = None,
                       scratch_dir: Optional[Path] = None, oa=None,
                       deep: bool = False, max_chars: Optional[int] = None,
                       max_chunks: int = 12, *, extra_routes: bool = False,
                       route_delay: float = 0.0, route_budget=None,
                       tail_max_chars: int = 0, tail_max_chunks: int = 4,
                       diag: Optional[dict] = None) -> Optional[CloseReading]:
    """端到端精读一篇：解析 OA → 下 PDF → 抽全文 → 精读；无全文则用摘要降级。

    oa 非空时复用预解析结果（close_read_segments 择优时已解析，避免重复网络请求）。
    deep=True 时走分块深读（预算放宽到 max_chars），失败回落单跳；默认值即现行行为。
    extra_routes/route_delay 透传给 resolve_oa_pdf（额外四路，默认关，见 settings.fulltext_extra_routes）。
    diag：可选出参，透传给 close_read / deep_close_read 记「这次为什么没产出」，
    并额外记 `from_full_text`（拿没拿到全文）——账本据此区分「全文到手但模型挂了」与「真抓不到全文」。
    """
    from .paths import repo_path
    scratch_dir = repo_path(scratch_dir or "output/scholar_pdfs")  # 锚定仓库根，防 cwd 漂移双树缓存
    if oa is None:
        # 额外通道参数只在开关打开时才传：默认路径的调用形状与历史逐字节一致
        #（既有测试与调用方的 resolve_oa_pdf 桩不认这两个关键字）。
        oa = resolve_oa_pdf(seg.metadata, email=email,
                            **_route_kwargs(extra_routes, route_delay, route_budget))
    full_text, from_full, source = "", False, None
    raw_chars: Optional[int] = None
    pdf_ex: Optional[PdfText] = None            # PDF 路径才有页级量尺（削页 / 尾段）
    # 预算必须对 PDF 与 EPMC 两条全文来源同时生效：只放宽 PDF 那条的话，
    # EPMC 正文仍被钉死在 40k，对现存的 europepmc 条目是净回退。
    eff_max_chars = max_chars if (deep and max_chars) else AUTO_MAX_CHARS
    if oa and oa.pdf_url:
        dest = scratch_dir / "{}.pdf".format(seg.paper_id[:16])
        # 额外通道给的是**候选列表**（含 pdf_url 自己），逐个试到一个过校验为止；
        # 主链路（arXiv/Unpaywall）candidates 为空 → 只试 pdf_url、不加校验，行为不变。
        cands = getattr(oa, "candidates", None) or []     # 旧式 OA 桩对象没有这个字段
        urls = list(cands) if cands else [oa.pdf_url]
        validate = bool(cands)
        pdf = None
        for u in urls:
            pdf = download_pdf(u, dest, validate=True) if validate else download_pdf(u, dest)
            if pdf:
                if u != oa.pdf_url:
                    logger.info("  额外通道候选命中：{}".format(u[:80]))
                break
        if pdf:
            pdf_ex = _pdf_text_ex(pdf, max_chars=eff_max_chars, page_max_chars=AUTO_PAGE_MAX_CHARS)
            full_text = pdf_ex.text
            if full_text.strip():
                from_full, source, raw_chars = True, oa.source, pdf_ex.raw_chars
            else:
                pdf_ex = None
    if not from_full:
        # PDF 拿不到不等于没有全文：Elsevier/Cell、MDPI、OUP 对机器人下 PDF 回 403，
        # 但同一篇的 OA 全文在 Europe PMC 有干净 XML。降级成摘要前先走这条。
        # return_stats=True 时被调方在所有出口（含 return None 那几条）一律返回 2-元组，
        # 解包才安全——这行对每一篇拿不到 PDF 的论文都会执行，绝大多数走的正是失败出口。
        _res = europepmc_fulltext(doi=seg.metadata.doi, pmid=seg.metadata.pmid,
                                  max_chars=eff_max_chars, return_stats=True, return_tail=True)
        # 向后兼容：既有测试替身返回 2-元组 (text, raw) 甚至裸 None；真实实现返回 3-元组 (text, raw, tail)
        if isinstance(_res, tuple):
            epmc, epmc_raw, epmc_tail = (tuple(_res) + (0, ""))[:3]
        else:
            epmc, epmc_raw, epmc_tail = _res, 0, ""
        if epmc and epmc.strip():
            full_text, from_full, source, raw_chars = epmc, True, "europepmc", epmc_raw
            # EPMC 正文没有「页」，但总上限之外的余量同样能走两轮读的第二轮——否则
            # 「by_total_cap ⇒ 两轮读能救」这句话对 EPMC 来源不成立（第 1 轮审计 R1-1.1）。
            pdf_ex = PdfText(text=epmc, raw_chars=int(epmc_raw or 0), page_dropped=0, tail=epmc_tail or "")
    if not from_full:
        # 降级：用摘要。摘要本身就是全部可得文本，不存在截断
        full_text = seg.translated_abstract or seg.original_abstract or ""
        source = "abstract"
        raw_chars = len(full_text)
    # 数字回查的对照源与喂读正文分开算：全文路径两者是同一份英文正文；摘要降级路径
    # 必须对照英文原摘要——translated_abstract 是 digest 批量翻译的 LLM 输出，自身没有
    # 任何数字保真校验，拿它回查等于让一段未校验的 LLM 输出给另一段背书（原文 AUC 0.87
    # 译丢成 0.78 → 精读引用 0.78 → 在译文里自证命中 → 错数顶着「可引用证据」进
    # highlight/向量库/scholar-write 取证链）。数字是语言不变量，对照原摘要既挡精读
    # 幻觉也挡翻译丢位；仅当原摘要缺失才退回译文——此时喂读与对照本就同源，聊胜于无。
    verify_text = (seg.original_abstract or full_text) if not from_full else full_text
    if diag is not None:
        diag["from_full_text"] = bool(from_full)
        diag["source"] = source
        # 额外通道被熔断/额度抑制过 → 这次「没全文」可能是暂时的，不是「真没有」。
        # 记进 diag，让 backfill_deepread 把它与确定性 no_output 分开记账（第 1 轮审计 R1-2.4）。
        sup = ((getattr(oa, "extra", None) or {}).get("routes_suppressed") if oa is not None else None)
        if sup:
            diag["routes_suppressed"] = sup
    if deep and from_full:
        # 摘要降级篇一律走单跳：几千字符切块无意义，且这是 from_full_text 被污染的唯一入口
        cr = deep_close_read(seg, full_text, research_interests, llm, model=model,
                             source=source, from_full_text=from_full,
                             max_chunks=max_chunks, diag=diag)
        if cr is not None:
            cr.body_chars = len(full_text)
            cr.body_chars_raw = raw_chars
            cr.truncated = (raw_chars is not None and raw_chars > len(full_text))
            _set_truncation_causes(cr, pdf_ex)
            # 两轮读第二轮：PDF 与 EPMC 两条全文来源都带尾段（EPMC 经 _jats_to_text_ex）；
            # tail_max_chars=0 = 关掉，行为与旧版一致。
            if pdf_ex is not None and pdf_ex.tail and tail_max_chars > 0:
                # 第二轮是加法：它自己出任何异常都不许把第一轮 13 次调用的产物拖下水（R1-2.7）
                try:
                    fed = read_tail_appendix(cr, seg, pdf_ex.tail, research_interests, llm, model,
                                             max_chars=tail_max_chars, max_chunks=tail_max_chunks,
                                             source=source, diag=diag)
                except Exception as e:                                  # noqa: BLE001
                    logger.warning("  ⚠️ 两轮读第二轮异常，保留第一轮结果({}): {}".format(seg.paper_id[:8], e))
                    if diag is not None:
                        diag["tail_failed"] = "{}: {}".format(type(e).__name__, str(e)[:120])
                    fed = ""
                cr.tail_chars = len(fed) if fed else None
                if fed:
                    # 数字回查的对照源只加**真正喂进第二轮的尾段**（不是整段原始尾段：被剔掉的
                    # 参考文献里年份/页码必然命中，会给编出来的数字背书——R1-2.6）
                    verify_text = full_text + "\n" + fed
            verify_citable_numbers(cr, verify_text, seg.paper_id[:8])
            return cr
    cr = close_read(seg, full_text, research_interests, llm, model=model,
                    from_full_text=from_full, source=source, raw_chars=raw_chars, diag=diag)
    if cr is not None:
        if from_full:
            _set_truncation_causes(cr, pdf_ex)
        # 数字回查压在唯一出口上：deep 与单跳两条路径的精读都过同一道闸
        verify_citable_numbers(cr, verify_text, seg.paper_id[:8])
    return cr


def close_read_segments(segments, research_interests: str, llm, top_n: int = 5,
                        email: str = "", model: Optional[str] = None,
                        scratch_dir: Optional[Path] = None,
                        prefer_full_text: bool = True, candidate_factor: int = 4,
                        deep: bool = False, max_chars: Optional[int] = None,
                        max_chunks: int = 12, *, extra_routes: bool = False,
                        route_delay: float = 0.0, extra_max_per_batch: int = 80,
                        host_breaker_failures: int = 3,
                        tail_max_chars: int = 0, tail_max_chunks: int = 4,
                        diag: Optional[dict] = None) -> int:
    """对高优先级 top-N 篇做精读，就地写入 seg.close_reading。返回成功篇数。

    prefer_full_text=True：在优先级前 candidate_factor*top_n 候选里预解析 OA，
    优先挑「能拿到全文」的高优先级论文（全文精读是核心诉求），不足再用高优先级摘要降级。
    这样避免 top-N 恰好都是付费墙论文导致全文精读 0/5。

    extra_routes=True 时择优阶段的 OA 预解析也走额外四路（每篇候选多打 2~3 个 API，
    route_delay 节流）。整批共享一份 fulltext.RouteBudget：额外四路总调用不超过
    extra_max_per_batch，同一宿主连续 host_breaker_failures 次终局失败即熔断——
    择优阶段 candidate_factor*top_n 篇不再能放大成几十次请求（2026-09-05 默认打开时配套）。
    tail_max_chars/tail_max_chunks：两轮读第二轮的预算（0 = 关），透传给 close_read_segment。
    """
    ranked = sorted(segments, key=lambda s: s.priority_score, reverse=True)
    n = max(0, top_n)
    oa_map = {}
    budget = None
    if extra_routes:
        from .fulltext import RouteBudget
        budget = RouteBudget(max_calls=extra_max_per_batch, breaker_failures=host_breaker_failures)
    if prefer_full_text and 0 < n < len(ranked):
        cand = ranked[:max(n, candidate_factor * n)]
        # 候选逐篇都打同两个 API（Unpaywall/EPMC），共享连接免得每篇重做 TCP+TLS 握手；
        # timeout 取两者默认的较大值（europepmc_pmcid 的 20s）
        with ipv4_client(timeout=20.0) as xc:
            for seg in cand:
                try:
                    oa = resolve_oa_pdf(seg.metadata, email=email, client=xc,
                                        **_route_kwargs(extra_routes, route_delay, budget))
                except Exception:
                    oa = None
                oa_map[id(seg)] = oa
            has_ft = [s for s in cand if oa_map.get(id(s)) and oa_map[id(s)].pdf_url]
            # 成员判断按对象身份（id）而不是 `in` 列表：PaperSegment 是 pydantic 模型，
            # `in` 走全字段深比较——候选 candidate_factor*top_n 篇时是 O(n²) 次含摘要
            # 全文的深比较，且两个字段恰好全等的不同对象会被误判为同一篇。
            has_ft_ids = {id(s) for s in has_ft}
            # 无 OA PDF 的再问一次 Europe PMC——否则 Elsevier/MDPI/OUP 这批（PDF 被反爬挡死、
            # 但 EPMC 有全文）会被择优逻辑系统性判成"拿不到全文"而让位给摘要降级篇。
            # 结果走 fulltext._PMCID_CACHE：route_epmc_render 已经问过的 DOI 这里不重复触网；
            # 仍要触网的按 route_delay 节流，免得把 www.ebi.ac.uk 打跳闸（第 2 轮审计 R2-1.3）。
            import time as _time
            _sleep = _time.sleep
            for seg in cand:
                if id(seg) in has_ft_ids:
                    continue
                if route_delay > 0:
                    _sleep(route_delay)
                try:
                    if europepmc_pmcid(doi=seg.metadata.doi, pmid=seg.metadata.pmid, client=xc):
                        has_ft.append(seg)
                        has_ft_ids.add(id(seg))
                except Exception:
                    pass
        has_ft = sorted(has_ft, key=lambda s: s.priority_score, reverse=True)
        no_ft = [s for s in cand if id(s) not in has_ft_ids]
        chosen = (has_ft[:n] + no_ft)[:n]  # 先全文可得（已按优先级），不足补高优先级降级
        chosen = sorted(chosen, key=lambda s: s.priority_score, reverse=True)
        logger.info("  精读择优：候选 {} 篇，其中全文可得 {} 篇 → 选 {} 篇".format(
            len(cand), len(has_ft), len(chosen)))
    else:
        chosen = ranked[:n]

    done = ft = 0
    for seg in chosen:
        try:
            sub = {} if diag is not None else None
            if sub is not None:
                diag[seg.paper_id] = sub
            cr = close_read_segment(seg, research_interests, llm, email=email,
                                    model=model, scratch_dir=scratch_dir,
                                    oa=oa_map.get(id(seg)), deep=deep,
                                    max_chars=max_chars, max_chunks=max_chunks,
                                    tail_max_chars=tail_max_chars, tail_max_chunks=tail_max_chunks,
                                    diag=sub,
                                    **({"route_budget": budget} if budget is not None else {}),
                                    **_route_kwargs(extra_routes, route_delay))
        except Exception as e:
            logger.warning("  ⚠️ 精读失败({}): {}".format(seg.paper_id[:8], e))
            if diag is not None:
                diag.setdefault(seg.paper_id, {}).update(
                    llm_error="{}: {}".format(type(e).__name__, str(e)[:200]),
                    llm_unavailable=is_llm_unavailable(e))
            cr = None
        if cr:
            seg.close_reading = cr
            done += 1
            if cr.from_full_text:
                ft += 1
            # 分篇级薄输出告警：整批 done/N 只能发现「全挂」，发现不了「都成功但每篇都很薄」。
            # 8 条取自 manual 精读样本的下界（可取证句子数远高于此），低于它基本等于没读进去。
            # 只告警不抛：整批 0/N 的 RuntimeError 语义（ingest.py）不能因为薄输出而更容易触发。
            n_tagged = sum(1 for sec in cr.sections for st in sec.sentences if st.tag)
            if n_tagged < 8 or cr.truncated:
                # 截断分两种成因，后果完全不同（见 docs/bugs/2026-09-04-truncated-flag-conflates-two-causes.md）：
                #   总上限截尾（body_chars 恰好等于 eff_max_chars）—— 丢的是正文**尾部**，抬上限有用；
                #   单页上限削页（body_chars 小于上限）—— 丢的是某张病态长页的后半，抬总上限**完全无用**。
                # 成因由抽取环节直接记在 CloseReading 上（truncated_by_total_cap / truncated_by_page_cap），
                # 日志里说清这一条，就够挡住「按覆盖率排序挑最该重跑的几篇」那类误判。
                cause = ""
                if cr.truncated:
                    # 2026-09-05 起成因由抽取环节直接记在 CloseReading 上（可同时为真），不再从
                    # body_chars 与上限的相等关系倒推——那种倒推在「页削 + 总截同时发生」时只说得出一半。
                    parts_c = []
                    if cr.truncated_by_total_cap:
                        parts_c.append("总上限截尾" + ("，尾段已另起一轮读 {} 字符".format(cr.tail_chars)
                                                     if cr.tail_chars else "，抬 closeread_tail_max_chars 有用"))
                    if cr.truncated_by_page_cap:
                        parts_c.append("单页上限削页（抬总上限无用）")
                    if not parts_c:
                        parts_c.append("成因未知：老量尺")
                    cause = "（已截断·" + "；".join(parts_c) + "）"
                logger.warning(
                    "  ⚠️ 精读偏薄({}): 可取证句 {} 条，正文 {}/{} 字符{}".format(
                        seg.paper_id[:8], n_tagged, cr.body_chars, cr.body_chars_raw, cause))
    logger.info("  全文精读 {}/{} 篇（其中真全文 {}，top-{}）".format(done, len(chosen), ft, top_n))
    return done
