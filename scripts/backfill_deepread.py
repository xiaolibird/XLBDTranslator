# -*- coding: utf-8 -*-
"""给 2026-08-01 深读改造之前的 auto 存量精读补深度（reading_depth=unknown-legacy）。

背景见 `docs/decisions/legacy_closeread_backfill_2026-08.md`。一句话：auto 精读在
2026-08-01 前是「一次单跳、40k 字符预算」，实测平均只覆盖论文 56.6% 的页面
（全覆盖仅 19/263）；改造后分块深读 120k 覆盖 97.6%。库里 auto 有全文精读的 356 篇
中 **334 篇是改造前的产物**，平均 11.4 条可取证句，而深读那批是 56.1 条。
单篇 PoC 实测 6 → 76 条（12.7 倍），且旧札记 100% 缺「实验方法」节。

**为什么单独成脚本**，三个现成的都不行：
- `backfill_methods.py` 只扫 `notes_dir/manual` 的 bundle，这批全是 series=auto 没 bundle
  （也正是那次「实验方法回填 221 篇」一篇都没覆盖到它们的原因）；
- `backfill_notes.py --force` 是重做整月：重跑 Gmail/PubMed 抓取 + LLM 三态筛选，
  会改变库的构成，太重；
- `read_pdf.py regen` 按现有 bundle 重建札记，**不重读原文**。

**为什么做文本级手术而不是走 `ingest.run_ingest`**（这条是本脚本的核心设计）：
目标篇散在几十个月度札记里，而那些文件**总共含 1600+ 篇论文**。run_ingest → write_notes
是整篇重写，非目标篇要靠 `_rehydrate_close_readings` 从 md 回读，而它的 docstring 明说
「model/read_at/body_chars 等量尺字段 md 没存、回读后为默认值」——为了一百来篇让一千多篇
无辜论文丢掉量尺字段，且把整份文件置于「解析失败即静默丢数据」的风险下，代价不成比例。
它还会无条件走 `enrich_segments` + `resolve_citekeys`，带 citekey 抖动风险（代码注释
自陈「同 backfill_notes.run_month / read_pdf._rebuild_month 同源坑」），而改 citekey
必须扫派生物。所以这里只替换目标篇在 md 里的**精读节**，其余字节一个不动。

md 精读节的格式契约与 `notes._paper_section` 的渲染、`notes_index` 的行首正则三方一致
（`_SECTION_RE` / `_CLOSEREAD_RE` / `_CR_SECTION_RE` / `_TAG_LINE_RE`），渲染函数
`_render_closeread` 与 `_paper_section` 的精读分支逐行对齐，**改一处必须同步另一处**。

用法：
    PYTHONPATH=. python scripts/backfill_deepread.py scan
    PYTHONPATH=. python scripts/backfill_deepread.py run --citekey meng2021Mimicif   # 干跑
    PYTHONPATH=. python scripts/backfill_deepread.py run --citekey meng2021Mimicif --apply
    PYTHONPATH=. python scripts/backfill_deepread.py run --limit 10 --apply
    PYTHONPATH=. python scripts/backfill_deepread.py restore <备份目录>              # 回滚

**不带 `--apply` 就是干跑**：照常花 LLM 重读并打印新旧对比，但一个字节都不写盘。

`output/` 全部在 .gitignore 内（`git ls-files output/scholar_notes` 为空），**回滚不能靠
git**，所以每次写盘前把 md + sidecar 原件复制到
`output/scholar_notes/.backfill_deepread_backup/<时间戳>/`，`restore` 子命令可整批还原。

收尾（本脚本**不自动做**，改完看一眼再手动跑，避免半截状态被固化进派生物）：
    PYTHONPATH=. python scripts/notes_index.py        # 或 build_vault/index 的既有入口
    PYTHONPATH=. python scripts/notes_embed.py        # chunk id 内容寻址，自动删旧嵌新
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.settings import load_scholar_settings          # noqa: E402
from src.scholar.embed_store import INDEX_NAME                   # noqa: E402
from src.scholar.notes_index import _CLOSEREAD_RE, _SECTION_RE   # noqa: E402
from src.scholar.notes import _TAG_MARK                          # noqa: E402
from src.scholar.schema import PaperMetadata, PaperSegment       # noqa: E402
from src.utils.logger import get_logger                          # noqa: E402

logger = get_logger("backfill_deepread")

LEDGER_NAME = "backfill_deepread_progress.json"
BACKUP_DIR = ".backfill_deepread_backup"
TARGET_DEPTH = "unknown-legacy"
_FAIL_STREAK_STOP = 3   # 连续失败几篇就判通路级故障并中止（见 cmd_run 的熔断）


# ---------------- 目标集 ----------------

def _keepers(index: dict) -> List[dict]:
    """与 embed_store.chunks_from_index 同一口径的 keeper 过滤。"""
    out = []
    for e in index.get("papers") or []:
        if not isinstance(e, dict):
            continue
        ck = e.get("citekey")
        if e.get("duplicate_of") or not ck:
            continue
        if ck.startswith("MISSING-KEY-") or e.get("citekey_source") == "missing":
            continue
        out.append(e)
    return out


def select_targets(index: dict, decision: str = "", tier: str = "") -> List[dict]:
    """选 reading_depth=unknown-legacy 的存量精读，可按 decision / priority_tier 收窄。

    只挑 has_full_text_reading 为真的：没做过全文精读的（纯题录 / 只读摘要）不是本课题
    的缺口——那是筛选阶段的决定，不是缺陷，重读它们属于「扩大精读面」的另一件事。
    """
    out = []
    for e in _keepers(index):
        if e.get("reading_depth") != TARGET_DEPTH or not e.get("has_full_text_reading"):
            continue
        if decision and e.get("decision") != decision:
            continue
        if tier and e.get("priority_tier") != tier:
            continue
        out.append(e)
    return out


# ---------------- 账本 ----------------

def _ledger_path(notes_dir: Path) -> Path:
    return notes_dir / LEDGER_NAME


def load_ledger(notes_dir: Path) -> dict:
    p = _ledger_path(notes_dir)
    if not p.exists():
        return {"done": {}, "failed": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d.setdefault("done", {})
        d.setdefault("failed", {})
        return d
    except Exception as e:
        raise RuntimeError("账本 {} 读取失败（{}）：拒绝继续，否则会重复消耗 LLM 额度并"
                           "重复改写札记。请先修复或删除它。".format(p, e))


def save_ledger(notes_dir: Path, led: dict) -> None:
    tmp = _ledger_path(notes_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(led, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_ledger_path(notes_dir))


# ---------------- 段落重建 ----------------

def segment_from_entry(entry: dict, sid: int, abstracts: Optional[Dict[str, str]] = None
                       ) -> PaperSegment:
    """索引条目 → PaperSegment（**只为重读用**，不参与元数据增强）。

    刻意不调 enrich_segments / resolve_citekeys：本脚本沿用索引里已有的 citekey 与
    元数据，绝不让重读这件事把身份键搅动了（见模块 docstring）。
    """
    # 摘要挂在 PaperSegment.original_abstract 上，**不在 PaperMetadata**（它没有
    # abstract 字段；pydantic 会静默吞掉这个未知入参，直到读取时才 AttributeError）。
    abstract = (abstracts or {}).get(entry.get("citekey") or "", "") or ""
    meta = PaperMetadata(
        paper_id=str(sid),
        title=entry.get("title") or "",
        authors=list(entry.get("authors") or []),
        url=entry.get("url") or "",
        doi=entry.get("doi") or "",
        arxiv_id=entry.get("arxiv_id") or "",
        journal=entry.get("journal") or "",
    )
    seg = PaperSegment(segment_id=sid, paper_id=str(sid), metadata=meta,
                       original_abstract=abstract)
    seg.priority_score = entry.get("priority_score") or 1.0
    return seg


def deep_read_local_pdf(seg: PaperSegment, pdf: Path, proc, llm, model: str):
    """用**本地** PDF 走深读，绕开 OA 解析与下载。返回 CloseReading 或 None。

    为什么需要这条通道：`close_read_one` 的全文来源只有两个——`resolve_oa_pdf` 给的
    pdf_url，或 Europe PMC 的 XML；出版商站点（Thieme/Springer/PLOS/HAL 等）对机器人
    403 时两条都断，而 `download_pdf` **每次强制重下、不复用已存在文件**，所以把 PDF
    手工丢进 `output/scholar_pdfs/` 也接不上。更要命的是本库 `abstracts.json` 只收录
    3 篇，全文一断就连降级用的摘要都是空的 —— 精读必然零产出（8 篇 `no_output` 全是
    这个机制，不是精读质量问题）。

    刻意复用 `deep_close_read` + `verify_citable_numbers` 这两个原装件，只把「怎么拿到
    正文」换掉：分块策略、提示词、数字回查闸全部与自动链路逐字一致，人工补的 PDF 不能
    享受另一套（更松的）标准。
    """
    from src.scholar.closereading import (AUTO_PAGE_MAX_CHARS, _pdf_text_with_stats,
                                          deep_close_read, verify_citable_numbers)
    text, raw = _pdf_text_with_stats(
        pdf, max_chars=proc.closeread_max_chars, page_max_chars=AUTO_PAGE_MAX_CHARS)
    if not text.strip():
        logger.warning("  本地 PDF 抽不出文字层（可能是扫描件）：{}".format(pdf.name))
        return None
    cr = deep_close_read(seg, text, proc.research_interests, llm, model=model,
                         source="local-pdf", from_full_text=True,
                         max_chunks=proc.closeread_max_chunks)
    if cr is None:
        return None
    cr.body_chars = len(text)
    cr.body_chars_raw = raw
    cr.truncated = (raw is not None and raw > len(text))
    verify_citable_numbers(cr, text, seg.paper_id[:8])
    return cr


def find_local_pdf(pdf_dir: Optional[Path], entry: dict) -> Optional[Path]:
    """在 --pdf-dir 里按 citekey / doi / arxiv_id 找手工下载的 PDF（大小写不敏感）。

    多种命名都认，省得用户改名：`<citekey>.pdf` 最稳；DOI 里的 `/` 换成 `_` 或 `-`
    也认（浏览器另存常见）；arXiv 用编号（`2410.17506.pdf`）。
    """
    if not pdf_dir or not pdf_dir.is_dir():
        return None
    cands = {(entry.get("citekey") or "").lower()}
    doi = (entry.get("doi") or "").lower()
    if doi:
        cands |= {doi.replace("/", "_"), doi.replace("/", "-"), doi.split("/")[-1]}
    ax = (entry.get("arxiv_id") or "").lower()
    if ax:
        cands |= {ax, "arxiv" + ax, ax.replace(".", "_")}
    cands.discard("")
    for p in pdf_dir.iterdir():
        if p.suffix.lower() != ".pdf":
            continue
        if p.stem.lower() in cands:
            return p
    return None


# ---------------- md 渲染 / 手术 ----------------

def _render_closeread(cr, level: int = 2) -> List[str]:
    """渲染精读节的 md 行。**与 notes._paper_section 的精读分支逐行对齐**，改一处同步两处。

    level=2（聚合札记每篇一节）时精读节是 `###`，与 _CLOSEREAD_RE 的锚定一致。
    """
    sub = "#" * (level + 1)
    label = "全文精读" if cr.from_full_text else "精读（仅摘要降级）"
    src = " · 来源 `{}`".format(cr.source) if cr.source else ""
    out = ["{} {}{}".format(sub, label, src), ""]
    for sec in cr.sections:
        out.append("**【{}】**".format(sec.heading))
        for st in sec.sentences:
            marker = "〔{}〕".format(st.tag) if st.tag in _TAG_MARK else ""
            out.append("- {}{}".format(marker, st.text))
        out.append("")
    return out


def _find_paper_span(lines: List[str], citekey: str, note_line: Optional[int]
                     ) -> Tuple[int, int]:
    """定位某篇在 md 里的行区间 [start, end)。

    优先用索引里的 note_line（1-based，`_locate_headings` 与 `parse_note_md` 同用
    `_SECTION_RE` 扫同一份文件，行号可直接对齐）——同 citekey 出现多次时（近重复文献
    各自精读）按 citekey 找会认错节，note_line 才是唯一无歧义的锚。
    """
    start = None
    if note_line and 1 <= note_line <= len(lines):
        m = _SECTION_RE.match(lines[note_line - 1])
        if m and m.group(4) == citekey:
            start = note_line - 1
    if start is None:
        hits = [i for i, ln in enumerate(lines)
                if (_SECTION_RE.match(ln) or None) and _SECTION_RE.match(ln).group(4) == citekey]
        if len(hits) != 1:
            raise RuntimeError(
                "定位失败：note_line={} 对不上，且按 citekey `{}` 在 md 里找到 {} 处标题"
                "（0 处=已改名/已删，多处=近重复文献各自成节）。拒绝盲改。"
                .format(note_line, citekey, len(hits)))
        start = hits[0]
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _SECTION_RE.match(lines[j]) or lines[j].startswith("# "):
            end = j
            break
    return start, end


def replace_closeread(md_text: str, citekey: str, note_line: Optional[int],
                      new_lines: List[str]) -> Tuple[str, int, int]:
    """把某篇的精读节整段换成 new_lines。返回 (新全文, 旧节行数, 新节行数)。

    只动这一节：篇内的标题/裁决/摘要、以及**其余所有论文**的字节一律不碰。
    找不到既有精读节时插在该篇末尾（正常路径不会走到——目标集限定
    has_full_text_reading=True，必有精读节）。
    """
    lines = md_text.splitlines()
    start, end = _find_paper_span(lines, citekey, note_line)

    cr_start = None
    for j in range(start + 1, end):
        if _CLOSEREAD_RE.match(lines[j]):
            cr_start = j
            break
    if cr_start is None:
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        merged = lines[:insert_at] + [""] + new_lines + lines[insert_at:]
        return "\n".join(merged) + "\n", 0, len(new_lines)

    cr_end = end
    for k in range(cr_start + 1, end):
        ln = lines[k]
        if ln.startswith("### ") or ln.startswith("## ") or ln.startswith("# "):
            cr_end = k
            break
    old_n = cr_end - cr_start
    merged = lines[:cr_start] + new_lines + lines[cr_end:]
    return "\n".join(merged) + "\n", old_n, len(new_lines)


# ---------------- sidecar ----------------

def sidecar_path(notes_dir: Path, note_file: str) -> Path:
    return notes_dir / (Path(note_file).stem + ".index.json")


def update_sidecar(path: Path, citekey: str, cr) -> str:
    """同步 sidecar 的量尺字段。返回一行人读说明（未改动/已更新/无 sidecar）。

    83 篇 md 里只有 40 篇有 sidecar，其余走 notes_index 的 md-parse 分支——那条路
    highlights 直接从 md 解析，改完 md 就够了。有 sidecar 时也分两种：
    条目**没有** `highlights` 字段的（v1/v2 老 sidecar，本批 19/21 属此）由
    notes_index 从 md 回填，同样不用管；只有含 highlights 的新 sidecar 必须一并更新，
    否则索引会「直接沿用 sidecar、不触碰 md」，md 改了而库里纹丝不动。

    `reading_depth` 一律显式写入：notes_index 只在条目**缺**该字段时才推断
    （auto + has_full_text_reading → 'unknown-legacy'），显式写了才不会被打回原形。
    """
    if not path.exists():
        return "无 sidecar（走 md-parse，无需同步）"
    d = json.loads(path.read_text(encoding="utf-8"))
    hit = None
    for e in d.get("papers") or []:
        if isinstance(e, dict) and e.get("citekey") == citekey:
            hit = e
            break
    if hit is None:
        return "⚠️ sidecar 里没有 {}（跳过，md 已改）".format(citekey)

    hit["has_full_text_reading"] = bool(cr.from_full_text)
    if cr.source:
        hit["reading_source"] = cr.source
    hit["reading_depth"] = getattr(cr, "reading_depth", None) or "chunked"
    for f in ("body_chars", "body_chars_raw", "truncated"):
        v = getattr(cr, f, None)
        if v is not None:
            hit[{"body_chars": "fulltext_chars",
                 "body_chars_raw": "fulltext_chars_raw",
                 "truncated": "fulltext_truncated"}[f]] = v
    note = "sidecar 量尺已更新"
    if "highlights" in hit:
        from src.scholar._citekey_utils import _collect_highlights
        # 三元组顺序是 (heading, tag, text)——不是 (heading, text, tag)。
        triples = [(sec.heading, st.tag, st.text)
                   for sec in cr.sections for st in sec.sentences]
        try:
            hl, tc = _collect_highlights(triples)
            hit["highlights"], hit["tag_counts"] = hl, tc
            note += " + highlights 同步"
        except Exception as e:                        # noqa: BLE001
            raise RuntimeError(
                "sidecar 含 highlights 但同步失败（{}）：不能只改 md——notes_index 会"
                "直接沿用 sidecar 的旧 highlights，导致 md 与库不一致。".format(e))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return note


# ---------------- 备份 ----------------

def backup_files(notes_dir: Path, stamp: str, paths: List[Path]) -> Path:
    dst = notes_dir / BACKUP_DIR / stamp
    dst.mkdir(parents=True, exist_ok=True)
    for p in paths:
        if p.exists():
            shutil.copy2(p, dst / p.name)
    return dst


def cmd_restore(notes_dir: Path, src: str) -> int:
    d = Path(src)
    if not d.is_absolute():
        d = notes_dir / BACKUP_DIR / src
    if not d.is_dir():
        print("备份目录不存在：{}".format(d), file=sys.stderr)
        return 2
    n = 0
    for f in sorted(d.iterdir()):
        if f.is_file():
            shutil.copy2(f, notes_dir / f.name)
            n += 1
            print("  还原 {}".format(f.name), flush=True)
    print("已从 {} 还原 {} 个文件。记得重跑索引与向量库同步。".format(d, n), flush=True)
    return 0


# ---------------- 子命令 ----------------

def cmd_scan(notes_dir: Path, index: dict) -> int:
    import collections
    all_t = select_targets(index)
    led = load_ledger(notes_dir)
    print("reading_depth={} 且做过全文精读：{} 篇".format(TARGET_DEPTH, len(all_t)), flush=True)
    print("  已完成（账本）：{}   失败：{}".format(len(led["done"]), len(led["failed"])), flush=True)
    for label, kw in (("INCLUDE", {"decision": "INCLUDE"}),
                      ("tier=high", {"tier": "high"}),
                      ("INCLUDE+high", {"decision": "INCLUDE", "tier": "high"})):
        print("  {:<14} {} 篇".format(label, len(select_targets(index, **kw))), flush=True)
    hl = [len(e.get("highlights") or []) for e in all_t]
    print("  现有可取证句：合计 {}，平均 {:.1f}".format(sum(hl), sum(hl) / len(hl) if hl else 0), flush=True)
    print("  涉及札记文件：{} 个".format(len(collections.Counter(e.get("note_file") for e in all_t))), flush=True)
    print("  来源分布：{}".format(collections.Counter(e.get("reading_source") for e in all_t).most_common()), flush=True)
    return 0


def cmd_run(args, settings, notes_dir: Path, index: dict) -> int:
    targets = select_targets(index, args.decision, args.tier)
    if args.citekey:
        want = set(args.citekey)
        targets = [e for e in targets if e.get("citekey") in want]
        missing = want - {e.get("citekey") for e in targets}
        if missing:
            print("这些 citekey 不在目标集里（可能已重跑过/不是 legacy/非 keeper）：{}".format(
                sorted(missing)), file=sys.stderr)
            if not targets:
                return 2
    led = load_ledger(notes_dir)
    if getattr(args, "only_failed", False):
        # 失败过的多半是全文抓不到（本库 abstracts.json 只有 3 篇，全文一断就零产出），
        # 补了 PDF 再来一轮才有意义；已成功的不动。
        targets = [e for e in targets if e.get("citekey") in led["failed"]]
        if not targets:
            print("账本里没有失败条目。", file=sys.stderr)
            return 1
    elif not args.redo:
        targets = [e for e in targets if e.get("citekey") not in led["done"]]
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("没有待处理的论文。", file=sys.stderr)
        return 1

    print("待处理 {} 篇{}：".format(len(targets), "" if args.apply else "（**干跑**，不写盘）"), flush=True)
    for e in targets:
        print("   {:<28} 现有 {:>3} 条 | {}".format(
            e["citekey"], len(e.get("highlights") or []), (e.get("title") or "")[:52]))

    abstracts = {}
    ab_path = notes_dir / "abstracts.json"
    if ab_path.exists():
        try:
            abstracts = json.loads(ab_path.read_text(encoding="utf-8"))
        except Exception:
            abstracts = {}

    from src.scholar.closereading import close_read_segments
    from src.scholar.llm_client import LLMClient
    proc = settings.processing
    pdf_dir = Path(args.pdf_dir).expanduser() if getattr(args, "pdf_dir", "") else None
    if pdf_dir and not pdf_dir.is_dir():
        print("--pdf-dir 不是目录：{}".format(pdf_dir), file=sys.stderr)
        return 2
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    ok = fail = 0
    streak = 0          # 连续失败计数——限流/欠费是通路级故障，见下方熔断

    for n, entry in enumerate(targets, start=1):
        ck = entry["citekey"]
        print("\n[{}/{}] {} …".format(n, len(targets), ck), flush=True)
        seg = segment_from_entry(entry, n, abstracts)
        llm = LLMClient(settings.llm)
        local = find_local_pdf(pdf_dir, entry)
        try:
            if local is not None:
                print("   📄 用本地 PDF：{}".format(local.name), flush=True)
                seg.close_reading = deep_read_local_pdf(
                    seg, local, proc, llm,
                    settings.llm.closeread_model or settings.llm.model)
                done = 1 if seg.close_reading else 0
            else:
                done = close_read_segments(
                    [seg], proc.research_interests, llm, top_n=1,
                    email=proc.zotero_email or proc.external_email or "",
                    model=(settings.llm.closeread_model or settings.llm.model),
                    scratch_dir=Path("output/scholar_pdfs"),
                    deep=proc.closeread_deep, max_chars=proc.closeread_max_chars,
                    max_chunks=proc.closeread_max_chunks)
        except Exception as e:                        # noqa: BLE001
            logger.warning("  重读异常：{}".format(e))
            done = 0
        finally:
            llm.close()

        cr = seg.close_reading
        new_n = sum(len(s.sentences) for s in cr.sections) if (cr and cr.sections) else 0
        old_n = len(entry.get("highlights") or [])
        if not done or not cr or not cr.sections or new_n == 0:
            print("   ❌ 重读未产出，跳过（不写盘）", flush=True)
            led["failed"][ck] = {"at": stamp, "reason": "no_output"}
            fail += 1
            streak += 1
            # 熔断：单篇失败是常事（抓不到全文/解析不出），但**连着**失败几乎只有一种
            # 成因——通路级故障（订阅限流、欠费、Ollama 挂）。这批要跑十来个小时且每篇
            # 都真花额度，不熔断就会在故障期间把剩下几十篇全烧成 failed，还得人工挑出来
            # 重跑。账本已落盘，修好后原样重跑即可，会自动跳过已完成的。
            if streak >= _FAIL_STREAK_STOP:
                save_ledger(notes_dir, led)
                print("\n⛔ 连续 {} 篇失败，判定通路级故障（限流/欠费/服务不可用），中止。\n"
                      "   已完成的都在账本里，修好后重跑本命令会自动续上。".format(streak),
                      file=sys.stderr)
                return 1
            continue
        # 净变差就不写：深读产出反而少于既有，多半是这次抓全文失败退化成摘要级，
        # 写进去等于用坏数据覆盖好数据。宁可留旧的，让它下次再来。
        if new_n < old_n:
            print("   ❌ 新产出 {} 条 < 既有 {} 条，判定退化，不写盘".format(new_n, old_n), flush=True)
            led["failed"][ck] = {"at": stamp, "reason": "fewer:{}<{}".format(new_n, old_n)}
            fail += 1
            streak += 1     # 退化多半也是抓全文失败退化成摘要级，同样按通路故障计
            continue

        tagged = sum(1 for s in cr.sections for st in s.sentences if st.tag in _TAG_MARK)
        print("   ✅ {} → {} 条（带 role tag {}）| 全文={} 源={} 块={} {}".format(
            old_n, new_n, tagged, cr.from_full_text, cr.source,
            getattr(cr, "n_chunks", "?"),
            "截断" if getattr(cr, "truncated", False) else ""))
        print("      sections: {}".format(
            ", ".join("{}({})".format(s.heading, len(s.sentences)) for s in cr.sections)))

        if not args.apply:
            continue

        md_path = notes_dir / (entry.get("note_file") or "")
        if not md_path.exists():
            print("   ❌ 札记文件不存在：{}".format(md_path), flush=True)
            led["failed"][ck] = {"at": stamp, "reason": "no_md"}
            fail += 1
            continue
        sc_path = sidecar_path(notes_dir, entry["note_file"])
        bdir = backup_files(notes_dir, stamp, [md_path, sc_path])
        try:
            text = md_path.read_text(encoding="utf-8")
            new_text, o_lines, n_lines = replace_closeread(
                text, ck, entry.get("note_line"), _render_closeread(cr))
            tmp = md_path.with_suffix(".tmp")
            tmp.write_text(new_text, encoding="utf-8")
            tmp.replace(md_path)
            note = update_sidecar(sc_path, ck, cr)
        except Exception as e:                        # noqa: BLE001
            print("   ❌ 写盘失败：{}\n      备份在 {}，可用 restore 还原".format(e, bdir), flush=True)
            led["failed"][ck] = {"at": stamp, "reason": "write:{}".format(e)}
            fail += 1
            save_ledger(notes_dir, led)
            continue
        print("   💾 md 精读节 {} 行 → {} 行 | {} | 备份 {}".format(
            o_lines, n_lines, note, bdir.name))
        led["failed"].pop(ck, None)   # 重试成功就不再挂在失败清单上
        led["done"][ck] = {"at": stamp, "old": old_n, "new": new_n,
                           "note_file": entry.get("note_file"), "backup": bdir.name}
        ok += 1
        streak = 0
        save_ledger(notes_dir, led)

    save_ledger(notes_dir, led)
    print("\n完成：成功 {} / 失败 {}{}".format(
        ok, fail, "" if args.apply else "（干跑，未写盘）"))
    if ok and args.apply:
        print("下一步（本脚本不自动做）：刷新 literature_index → 跑 notes_embed.py 同步向量库", flush=True)
    return 0 if fail == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="给 auto 存量单跳精读补深度重读")
    ap.add_argument("--config", default="")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="看盘子")
    r = sub.add_parser("run", help="重读（默认干跑，--apply 才写盘）")
    r.add_argument("--citekey", action="append", help="指定 citekey（可重复）")
    r.add_argument("--decision", default="", help="按裁决过滤，如 INCLUDE")
    r.add_argument("--tier", default="", help="按优先级过滤，如 high")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--apply", action="store_true", help="真写盘（默认只干跑）")
    r.add_argument("--redo", action="store_true", help="连账本里已完成的也重跑")
    r.add_argument("--pdf-dir", default="",
                   help="手工下载的 PDF 目录（文件名用 citekey/doi/arxiv 均可）；命中即用本地 "
                        "PDF 深读，绕开被反爬挡死的出版商站点")
    r.add_argument("--only-failed", action="store_true",
                   help="只跑账本里失败过的（配 --pdf-dir 补全文后重试）")
    s = sub.add_parser("restore", help="从备份还原")
    s.add_argument("backup", help="备份目录名或绝对路径")
    args = ap.parse_args()

    # load_scholar_settings 的 config 走 repo_path()，不吃 None——没传就别传。
    settings = (load_scholar_settings(args.config, patch_gemini=False)
                if args.config else load_scholar_settings(patch_gemini=False))
    notes_dir = Path(settings.processing.notes_dir)
    if args.cmd == "restore":
        return cmd_restore(notes_dir, args.backup)
    index = json.loads((notes_dir / INDEX_NAME).read_text(encoding="utf-8"))
    if args.cmd == "scan":
        return cmd_scan(notes_dir, index)
    return cmd_run(args, settings, notes_dir, index)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中断（已完成的篇目已记进账本，重跑会自动跳过）", file=sys.stderr)
        sys.exit(130)
