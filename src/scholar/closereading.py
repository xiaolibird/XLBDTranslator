# -*- coding: utf-8 -*-
"""
全文精读：OA/arXiv 论文 → 下载 PDF → 抽全文 → 强模型精读 → 结构化 + 句级角色标记。

句级角色按「对后续工作流的用途」（句级）：可引用证据 / 可反驳观点 / 方法论借鉴。
付费墙无全文 → 基于摘要的降级精读（from_full_text=False）。
复用：fulltext.ipv4_client 下载、PyMuPDF(fitz) 抽文本、llm_client.LLMClient 调模型。
"""
import json
import re
from pathlib import Path
from typing import Optional

from .schema import (
    PaperSegment, CloseReading, CloseReadSection, CloseReadSentence,
)
from .fulltext import (
    ipv4_client, resolve_oa_pdf, europepmc_fulltext, europepmc_pmcid,
)
from ..utils.logger import get_logger
from ..utils.json_tools import strip_code_fences as _strip_json

logger = get_logger(__name__)

_VALID_TAGS = {"可引用证据", "可反驳观点", "方法论借鉴"}


# ---------------- PDF 下载 + 抽文本 ----------------

# auto 精读的抽文本预算：总量 40000 字符，单页最多 20000 字符。
# 单页上限的依据：本地 4829 页实测 median 3940 / p99 12852，超过 20000 的只有 16 页（0.33%），
# 全是图表矢量文字层的病态页——一页就能吃光整篇预算，让精读永远读不到正文后半。
# 取 20000 而非更小值是为了不误伤正常长页；把覆盖面推到后半篇是总预算的职责，不靠压低单页上限硬挤。
AUTO_MAX_CHARS = 40000
AUTO_PAGE_MAX_CHARS = 20000


def download_pdf(url: str, dest: Path, client=None, timeout: float = 60.0,
                 max_bytes: int = 40_000_000) -> Optional[Path]:
    """下载 PDF 到 dest。非 200 / 非 PDF / 超限 / 异常 → None（不抛出）。"""
    if not url:
        return None
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    own = client is None
    c = client or ipv4_client(timeout=timeout)
    try:
        r = c.get(url)
        if r.status_code != 200:
            logger.warning("  ⚠️ 下载 PDF 返回 {}: {}".format(r.status_code, url[:60]))
            return None
        data = r.content
        if len(data) > max_bytes:
            logger.warning("  ⚠️ PDF 超过 {}MB，跳过: {}".format(max_bytes // 1_000_000, url[:60]))
            return None
        if not data[:5].startswith(b"%PDF"):
            logger.warning("  ⚠️ 非 PDF 内容，跳过: {}".format(url[:60]))
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


def _pdf_text_with_stats(path: Path, max_chars: int = 40000,
                         page_max_chars: Optional[int] = None):
    """抽全文并同时给出「原始长度」：返回 (text, raw_chars)。失败返回 ("", 0)。

    raw_chars = 全部页 get_text 的原长之和，**不受 page_max_chars / max_chars 影响**——
    它要回答的是「这篇论文有多长」，而 text 回答的是「我们喂进去多少」，二者相减才是截断量。
    若 raw 也按单页上限收，truncated 判定会自证为假（cap 后的长度 == 喂进去的长度）。
    只遍历一次 PDF：抽两遍要多付一次解析开销，且两次结果不保证逐字节一致。
    """
    try:
        import fitz  # PyMuPDF
    except Exception as e:
        logger.warning("  ⚠️ 未安装 PyMuPDF，无法抽全文: {}".format(e))
        return "", 0
    try:
        parts = []
        raw_chars = 0
        # 不再在页循环里按累计长度提前 break：读完所有页、末尾统一截断。
        # 对输出逐字节无影响（原 break 发生在越界页 append 之后），但让后续统计能拿到全页原始长度。
        with fitz.open(str(path)) as doc:
            for page in doc:
                t = page.get_text("text", sort=True)
                if not t:
                    continue
                raw_chars += len(t)          # 先记原长，再做任何截断
                if page_max_chars is not None:
                    t = t[:page_max_chars]
                parts.append(t)
        return "".join(parts)[:max_chars], raw_chars
    except Exception as e:
        logger.warning("  ⚠️ 抽全文失败({}): {}".format(path, e))
        return "", 0


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


def close_read(seg: PaperSegment, body_text: str, research_interests: str,
               llm, model: Optional[str] = None, from_full_text: bool = True,
               source: Optional[str] = None,
               raw_chars: Optional[int] = None) -> Optional[CloseReading]:
    """对单篇论文做精读。llm 为 LLMClient；body_text 为全文或摘要。失败返回 None。

    raw_chars 为抽取源的原始长度（未截断），由调用方从抽取环节透传；None = 未知。
    """
    if not body_text or not body_text.strip():
        # 既无 OA 全文又无摘要 → 这篇在札记里会整篇没有精读内容。别静默，否则
        # 只能靠事后数 highlights 才发现（2026-07-27 实测踩过）。
        logger.warning("  ⚠️ 无全文也无摘要，跳过精读({}): {}".format(
            seg.paper_id[:8], (seg.metadata.title or "")[:50]))
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
        resp = llm.call(prompt, model=model, max_tokens=8192, json_mode=True)
    except Exception as e:
        logger.warning("  ⚠️ 精读 LLM 调用失败({}): {}".format(seg.paper_id[:8], e))
        return None
    cr = parse_closeread(resp)
    if cr is None:
        logger.warning("  ⚠️ 精读输出解析失败({})".format(seg.paper_id[:8]))
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
    return cr


def deep_close_read(seg: PaperSegment, body_text: str, research_interests: str,
                    llm, model: Optional[str] = None, source: Optional[str] = None,
                    from_full_text: bool = True,
                    max_chunks: int = 12) -> Optional[CloseReading]:
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
        return None
    notes = deep_read_chunks(chunks, llm, model, max_workers=4)
    # 接住汇总步的预算裁剪信息：max_chunks=12 并不能保证不越 60000 预算（实测 12 块 ×
    # 每块 ~5.8k 字符即超），而 auto 链路没有 manual 那样的亲读核验兜底，裁剪必须留痕——
    # 否则 reading_depth="chunked" / n_chunks=12 是在虚报"完整分块深读"。
    _binfo = {}
    cr, _one_line, _api_err = synthesize_deep_read(notes, llm, model, research_interests,
                                                   budget_info=_binfo)
    if cr is None:
        # 全块失败或汇总失败：交回调用方走单跳，保证最差不比现状差
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
    if _binfo.get("note"):
        cr.synth_truncated = True
        cr.synth_dropped_chunks = int(_binfo.get("n_dropped") or 0)
        logger.warning("  ⚠️ 汇总预算裁剪：{}".format(_binfo["note"]))
    return cr


def close_read_segment(seg: PaperSegment, research_interests: str, llm,
                       email: str = "", model: Optional[str] = None,
                       scratch_dir: Optional[Path] = None, oa=None,
                       deep: bool = False, max_chars: Optional[int] = None,
                       max_chunks: int = 12) -> Optional[CloseReading]:
    """端到端精读一篇：解析 OA → 下 PDF → 抽全文 → 精读；无全文则用摘要降级。

    oa 非空时复用预解析结果（close_read_segments 择优时已解析，避免重复网络请求）。
    deep=True 时走分块深读（预算放宽到 max_chars），失败回落单跳；默认值即现行行为。
    """
    from .paths import repo_path
    scratch_dir = repo_path(scratch_dir or "output/scholar_pdfs")  # 锚定仓库根，防 cwd 漂移双树缓存
    if oa is None:
        oa = resolve_oa_pdf(seg.metadata, email=email)
    full_text, from_full, source = "", False, None
    raw_chars: Optional[int] = None
    # 预算必须对 PDF 与 EPMC 两条全文来源同时生效：只放宽 PDF 那条的话，
    # EPMC 正文仍被钉死在 40k，对现存的 europepmc 条目是净回退。
    eff_max_chars = max_chars if (deep and max_chars) else AUTO_MAX_CHARS
    if oa and oa.pdf_url:
        dest = scratch_dir / "{}.pdf".format(seg.paper_id[:16])
        pdf = download_pdf(oa.pdf_url, dest)
        if pdf:
            full_text, pdf_raw = _pdf_text_with_stats(
                pdf, max_chars=eff_max_chars, page_max_chars=AUTO_PAGE_MAX_CHARS)
            if full_text.strip():
                from_full, source, raw_chars = True, oa.source, pdf_raw
    if not from_full:
        # PDF 拿不到不等于没有全文：Elsevier/Cell、MDPI、OUP 对机器人下 PDF 回 403，
        # 但同一篇的 OA 全文在 Europe PMC 有干净 XML。降级成摘要前先走这条。
        # return_stats=True 时被调方在所有出口（含 return None 那几条）一律返回 2-元组，
        # 解包才安全——这行对每一篇拿不到 PDF 的论文都会执行，绝大多数走的正是失败出口。
        epmc, epmc_raw = europepmc_fulltext(doi=seg.metadata.doi, pmid=seg.metadata.pmid,
                                            max_chars=eff_max_chars, return_stats=True)
        if epmc and epmc.strip():
            full_text, from_full, source, raw_chars = epmc, True, "europepmc", epmc_raw
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
    if deep and from_full:
        # 摘要降级篇一律走单跳：几千字符切块无意义，且这是 from_full_text 被污染的唯一入口
        cr = deep_close_read(seg, full_text, research_interests, llm, model=model,
                             source=source, from_full_text=from_full,
                             max_chunks=max_chunks)
        if cr is not None:
            cr.body_chars = len(full_text)
            cr.body_chars_raw = raw_chars
            cr.truncated = (raw_chars is not None and raw_chars > len(full_text))
            verify_citable_numbers(cr, verify_text, seg.paper_id[:8])
            return cr
    cr = close_read(seg, full_text, research_interests, llm, model=model,
                    from_full_text=from_full, source=source, raw_chars=raw_chars)
    if cr is not None:
        # 数字回查压在唯一出口上：deep 与单跳两条路径的精读都过同一道闸
        verify_citable_numbers(cr, verify_text, seg.paper_id[:8])
    return cr


def close_read_segments(segments, research_interests: str, llm, top_n: int = 5,
                        email: str = "", model: Optional[str] = None,
                        scratch_dir: Optional[Path] = None,
                        prefer_full_text: bool = True, candidate_factor: int = 4,
                        deep: bool = False, max_chars: Optional[int] = None,
                        max_chunks: int = 12) -> int:
    """对高优先级 top-N 篇做精读，就地写入 seg.close_reading。返回成功篇数。

    prefer_full_text=True：在优先级前 candidate_factor*top_n 候选里预解析 OA，
    优先挑「能拿到全文」的高优先级论文（全文精读是核心诉求），不足再用高优先级摘要降级。
    这样避免 top-N 恰好都是付费墙论文导致全文精读 0/5。
    """
    ranked = sorted(segments, key=lambda s: s.priority_score, reverse=True)
    n = max(0, top_n)
    oa_map = {}
    if prefer_full_text and 0 < n < len(ranked):
        cand = ranked[:max(n, candidate_factor * n)]
        # 候选逐篇都打同两个 API（Unpaywall/EPMC），共享连接免得每篇重做 TCP+TLS 握手；
        # timeout 取两者默认的较大值（europepmc_pmcid 的 20s）
        with ipv4_client(timeout=20.0) as xc:
            for seg in cand:
                try:
                    oa = resolve_oa_pdf(seg.metadata, email=email, client=xc)
                except Exception:
                    oa = None
                oa_map[id(seg)] = oa
            has_ft = [s for s in cand if oa_map.get(id(s)) and oa_map[id(s)].pdf_url]
            # 无 OA PDF 的再问一次 Europe PMC——否则 Elsevier/MDPI/OUP 这批（PDF 被反爬挡死、
            # 但 EPMC 有全文）会被择优逻辑系统性判成"拿不到全文"而让位给摘要降级篇。
            for seg in cand:
                if seg in has_ft:
                    continue
                try:
                    if europepmc_pmcid(doi=seg.metadata.doi, pmid=seg.metadata.pmid, client=xc):
                        has_ft.append(seg)
                except Exception:
                    pass
        has_ft = sorted(has_ft, key=lambda s: s.priority_score, reverse=True)
        no_ft = [s for s in cand if s not in has_ft]
        chosen = (has_ft[:n] + no_ft)[:n]  # 先全文可得（已按优先级），不足补高优先级降级
        chosen = sorted(chosen, key=lambda s: s.priority_score, reverse=True)
        logger.info("  精读择优：候选 {} 篇，其中全文可得 {} 篇 → 选 {} 篇".format(
            len(cand), len(has_ft), len(chosen)))
    else:
        chosen = ranked[:n]

    done = ft = 0
    for seg in chosen:
        try:
            cr = close_read_segment(seg, research_interests, llm, email=email,
                                    model=model, scratch_dir=scratch_dir,
                                    oa=oa_map.get(id(seg)), deep=deep,
                                    max_chars=max_chars, max_chunks=max_chunks)
        except Exception as e:
            logger.warning("  ⚠️ 精读失败({}): {}".format(seg.paper_id[:8], e))
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
                logger.warning(
                    "  ⚠️ 精读偏薄({}): 可取证句 {} 条，正文 {}/{} 字符{}".format(
                        seg.paper_id[:8], n_tagged, cr.body_chars, cr.body_chars_raw,
                        "（已截断）" if cr.truncated else ""))
    logger.info("  全文精读 {}/{} 篇（其中真全文 {}，top-{}）".format(done, len(chosen), ft, top_n))
    return done
