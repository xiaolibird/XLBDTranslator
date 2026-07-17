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
from .fulltext import ipv4_client, resolve_oa_pdf
from ..utils.logger import get_logger

logger = get_logger(__name__)

_VALID_TAGS = {"可引用证据", "可反驳观点", "方法论借鉴"}


# ---------------- PDF 下载 + 抽文本 ----------------

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


def pdf_to_text(path: Path, max_chars: int = 40000) -> str:
    """用 PyMuPDF(fitz) 抽全文纯文本，截断到 max_chars。失败返回空串。"""
    try:
        import fitz  # PyMuPDF
    except Exception as e:
        logger.warning("  ⚠️ 未安装 PyMuPDF，无法抽全文: {}".format(e))
        return ""
    try:
        parts = []
        total = 0
        with fitz.open(str(path)) as doc:
            for page in doc:
                t = page.get_text("text", sort=True)
                if not t:
                    continue
                parts.append(t)
                total += len(t)
                if total >= max_chars:
                    break
        return "".join(parts)[:max_chars]
    except Exception as e:
        logger.warning("  ⚠️ 抽全文失败({}): {}".format(path, e))
        return ""


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
研究问题、方法与数据、关键结论、可质疑点、对我研究的联想。
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
    {{"heading": "关键结论", "sentences": [{{"text": "……", "tag": "可引用证据"}}]}},
    {{"heading": "可质疑点", "sentences": [{{"text": "……", "tag": "可反驳观点"}}]}},
    {{"heading": "对我研究的联想", "sentences": [{{"text": "……", "tag": null}}]}}
  ]
}}"""


def _strip_json(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


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


def close_read(seg: PaperSegment, body_text: str, research_interests: str,
               llm, model: Optional[str] = None, from_full_text: bool = True,
               source: Optional[str] = None) -> Optional[CloseReading]:
    """对单篇论文做精读。llm 为 LLMClient；body_text 为全文或摘要。失败返回 None。"""
    if not body_text or not body_text.strip():
        return None
    prompt = _CLOSEREAD_PROMPT.format(
        research_interests=research_interests or "（未提供研究主线）",
        title=seg.metadata.title or "",
        body_label="全文" if from_full_text else "摘要",
        body=body_text,
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
    return cr


def close_read_segment(seg: PaperSegment, research_interests: str, llm,
                       email: str = "", model: Optional[str] = None,
                       scratch_dir: Optional[Path] = None, oa=None) -> Optional[CloseReading]:
    """端到端精读一篇：解析 OA → 下 PDF → 抽全文 → 精读；无全文则用摘要降级。

    oa 非空时复用预解析结果（close_read_segments 择优时已解析，避免重复网络请求）。
    """
    scratch_dir = Path(scratch_dir or "output/scholar_pdfs")
    if oa is None:
        oa = resolve_oa_pdf(seg.metadata, email=email)
    full_text, from_full, source = "", False, None
    if oa and oa.pdf_url:
        dest = scratch_dir / "{}.pdf".format(seg.paper_id[:16])
        pdf = download_pdf(oa.pdf_url, dest)
        if pdf:
            full_text = pdf_to_text(pdf)
            if full_text.strip():
                from_full, source = True, oa.source
    if not from_full:
        # 降级：用摘要
        full_text = seg.translated_abstract or seg.original_abstract or ""
        source = "abstract"
    return close_read(seg, full_text, research_interests, llm, model=model,
                      from_full_text=from_full, source=source)


def close_read_segments(segments, research_interests: str, llm, top_n: int = 5,
                        email: str = "", model: Optional[str] = None,
                        scratch_dir: Optional[Path] = None,
                        prefer_full_text: bool = True, candidate_factor: int = 4) -> int:
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
        for seg in cand:
            try:
                oa = resolve_oa_pdf(seg.metadata, email=email)
            except Exception:
                oa = None
            oa_map[id(seg)] = oa
        has_ft = [s for s in cand if oa_map.get(id(s)) and oa_map[id(s)].pdf_url]
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
                                    oa=oa_map.get(id(seg)))
        except Exception as e:
            logger.warning("  ⚠️ 精读失败({}): {}".format(seg.paper_id[:8], e))
            cr = None
        if cr:
            seg.close_reading = cr
            done += 1
            if cr.from_full_text:
                ft += 1
    logger.info("  全文精读 {}/{} 篇（其中真全文 {}，top-{}）".format(done, len(chosen), ft, top_n))
    return done
