# -*- coding: utf-8 -*-
"""手动 PDF 深度精读 CLI：ingest（脚本草稿）→[agent 亲读交叉核验]→ finalize（归档进当月手动精读札记）。

三段式（详见 docs/skills/read-paper/SKILL.md 的 agent 协议）：
  1. ingest：抽全文 + 拉权威元数据 + 分块通读汇总 → draft bundle
        PYTHONPATH=. python scripts/read_pdf.py ingest paper.pdf
        PYTHONPATH=. python scripts/read_pdf.py ingest ~/Downloads/待读/      # 整个目录
        PYTHONPATH=. python scripts/read_pdf.py ingest ~/Papers/ -r          # 递归子目录
  2. agent：用 Read 工具亲读整本 PDF，逐条核验脚本草稿的数字/结论/方法，
        把合并终稿写回 bundle 的 close_reading_final + cross_check_report，status=final
  3. finalize：从当月全部 final bundle 重建 科研札记_YYYY-MM_手动精读.{md,docx,references.json,index.json}
        PYTHONPATH=. python scripts/read_pdf.py finalize <bundle.json>

  regen：不新增论文，仅按现有 final bundle 重建某月（改模板/修复后用）
        PYTHONPATH=. python scripts/read_pdf.py regen --month 2026-07

设计取舍：手动精读独立成 `_手动精读` 系列，不并入自动 `_全文精读`——避免月度 launchd
回填见「当月全文精读.md 已存在」而跳过整月。索引里手动深读为 keeper（论文 agent 优先读到）。
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.schema import ScholarSettings  # noqa: E402
from src.scholar.llm_client import LLMClient  # noqa: E402
from src.scholar.paths import repo_path  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402

logger = get_logger("read_pdf")


def _cur_month():
    d = date.today()
    return "{:04d}-{:02d}".format(d.year, d.month)


def _load_settings(config):
    settings = ScholarSettings.from_env_file(Path(config))
    # 与既有 pipeline 一致：gemini 模型名走 deepseek provider
    if settings.llm.provider == "gemini" and settings.llm.model.startswith("gemini"):
        settings.llm.provider = "deepseek"
    # notes_dir 默认是相对路径，语义是「仓库里的那个目录」——锚死，别随 cwd 漂（见 paths.repo_path）
    settings.processing.notes_dir = repo_path(settings.processing.notes_dir)
    return settings


def _expand_pdfs(paths, recursive: bool = False):
    """路径列表 → PDF 文件列表：目录展开成其中的 *.pdf（按名排序，稳定可复现）。

    去重按 resolve() 后的真实路径，避免 `dir/ dir/a.pdf` 这类写法把同一篇读两遍
    （一篇 PDF 走一遍分块通读是真金白银的 LLM 成本）。macOS 的 `._foo.pdf`
    资源分叉文件不是 PDF，一并排除。
    """
    out, seen = [], set()
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            found = sorted(p.rglob("*.pdf") if recursive else p.glob("*.pdf"))
            if not found:
                logger.warning("⚠️ 目录里没有 PDF: {}".format(p))
            cand = found
        elif p.exists():
            cand = [p]
        else:
            logger.error("❌ 路径不存在: {}".format(p))
            continue
        for f in cand:
            if f.name.startswith("._"):
                continue
            key = f.resolve()
            if key not in seen:
                seen.add(key)
                out.append(f)
    return out


def cmd_ingest(args):
    from src.scholar.pdf_ingest import ingest_pdf
    settings = _load_settings(args.config)
    proc = settings.processing
    notes_dir = Path(proc.notes_dir)
    email = proc.zotero_email or proc.external_email or ""
    model = settings.llm.closeread_model or settings.llm.model
    llm = LLMClient(settings.llm)
    month = args.month or _cur_month()

    pdfs = _expand_pdfs(args.pdf, recursive=args.recursive)
    if not pdfs:
        logger.error("❌ 没找到任何 PDF")
        return 2
    if len(pdfs) > len(args.pdf):
        logger.info("展开目录后共 {} 篇 PDF".format(len(pdfs)))

    outs, failed = [], []
    for pdf_path in pdfs:
        try:
            r = ingest_pdf(
                pdf_path, notes_dir, month, llm,
                model=model, email=email,
                research_interests=proc.research_interests,
                title_override=(args.title or "") if len(pdfs) == 1 else "",
                index_path=notes_dir / "literature_index.json",
                force=args.force)
            outs.append(r)
        except Exception as e:
            logger.error("❌ ingest 失败 {}: {}".format(pdf_path.name, e))
            failed.append((pdf_path.name, str(e)))

    fresh = [r for r in outs if not r.get("skipped")]
    print("\n" + "=" * 66)
    print("已 ingest {} 篇 → draft bundle（待 agent 亲读交叉核验）".format(len(fresh)))
    any_api_err = False
    for r in outs:
        print("\n📄 {}".format(r["title"]))
        print("   bundle       : {}".format(r["bundle"]))
        print("   PDF          : {}".format(r["pdf_path"]))
        if r.get("skipped") == "final":
            print("   ⛔ 已 final，本次跳过（未覆盖）。确需重跑：--force（会丢弃已有核验成果）")
            continue
        print("   元数据来源    : {} | DOI={} | arXiv={}".format(
            r["meta_source"], r["doi"], r["arxiv_id"]))
        print("   亲读范围      : {}".format(_read_plan(r.get("n_pages"))))
        print("   分块通读      : {}/{} 块成功 | 脚本草稿 {} | draft_status={}".format(
            r["chunk_ok"], r["chunks"],
            "有" if r["has_close_reading"] else "无", r["draft_status"]))
        if r["draft_status"] == "api_error":
            any_api_err = True
            print("   ⚠️ LLM API 无额度/鉴权失败：脚本草稿这一轨不可用。")
    if any_api_err:
        print("\n🔁 回退协议（API 没钱）：不依赖脚本草稿，改用**两个 subagent 对抗生成**——")
        print("   Opus 亲读整本 PDF 出深读初稿 → Sonnet 亲读同一 PDF 逐条对抗核验+纠错 →")
        print("   主 agent 合并为 close_reading_final + cross_check_report（记录分歧裁决）→ finalize。")
        print("   详见 skill: read-paper 的「回退」节。")
    elif fresh:
        print("\n下一步（agent）：**按上面的「亲读范围」把 PDF 读到最后一页**"
              "（附录里的表格常是核验关键）→ 核验脚本草稿 → 写回 close_reading_final + "
              "cross_check_report + status=final → finalize。协议见 skill: read-paper。")
    _print_attention(outs, failed)
    print("=" * 66)
    return 0 if outs else 1


def _read_plan(n_pages, size: int = 20) -> str:
    """把总页数渲染成亲读窗口提示。

    没有这一行时 agent 只能靠猜决定读到第几页——实际发生过读到 12 页就断言草稿引用的
    附录表格是编造的，而那本 PDF 有 31 页、表都在后半本，每个数都对。
    """
    from src.scholar.pdf_ingest import read_windows
    wins = read_windows(n_pages, size=size)
    if not wins:
        return "页数未知（PyMuPDF 读不出）——务必自行确认总页数后再判断草稿真伪"
    return "{} 页 → {} 个 {} 页窗口：{}".format(
        n_pages, len(wins), size, ", ".join("{}-{}".format(a, b) for a, b in wins))


def _print_attention(outs, failed):
    """把「必须有人看一眼」的事项汇总到输出最末。

    单篇时散在中间也看得见，21 篇一批时必被淹掉——今天正是这样漏掉一条「索引里已有同文」，
    白读了一篇几个月前已精读过的论文。
    """
    dups = [r for r in outs if r.get("duplicate")]
    thin = [r for r in outs if not r.get("skipped")
            and (r.get("meta_source") == "pdf-only" or not r.get("authors_n"))]
    skipped = [r for r in outs if r.get("skipped") == "final"]
    n = len(dups) + len(thin) + len(skipped) + len(failed)
    if not n:
        return
    print("\n" + "-" * 66)
    print("⚠️ 需要注意（{} 项）".format(n))
    for r in dups:
        d = r["duplicate"]
        print("  · 索引里已有同文：{} —— {} @ {}（[@{}]）".format(
            r["title"][:48], d["note_file"], d["month"], d["citekey"]))
    if dups:
        print("    → 先确认是否值得重读；继续 finalize 则手动深读成为 keeper（旧条目标 duplicate）")
    for r in thin:
        print("  · 元数据不全（来源 {}、作者 {} 位）：{}".format(
            r.get("meta_source"), r.get("authors_n", 0), r["title"][:48]))
    if thin:
        print("    → citekey 会退化成 anon*、bibliography 缺卷期页；可加 --title \"精确标题\" 重跑")
    for r in skipped:
        print("  · 已 final 未覆盖：{}".format(r["title"][:48]))
    for name, err in failed:
        print("  · ingest 失败：{} —— {}".format(name, err[:80]))


def _sync_embedding_best_effort(notes_dir: Path, index: dict, settings) -> None:
    """best-effort 向量库同步：手动精读重建索引后跟进向量库。

    与 ingest_notes.py 的挂钩一致——任何异常只 log warning，绝不打断手动精读主流程。
    """
    try:
        from src.scholar.embeddings import EmbeddingClient, resolve_embedding_base_url
        from src.scholar.embed_store import sync_store
        client = EmbeddingClient(
            base_url=resolve_embedding_base_url(settings.llm),
            model=settings.llm.embedding_model,
        )
        try:
            stats = sync_store(notes_dir / "embeddings.sqlite3", index, client)
        finally:
            client.close()
        logger.info("向量库已同步：+{} 嵌入 / -{} 删除 / {} 元数据刷新".format(
            stats.embedded, stats.deleted, stats.meta_refreshed))
    except Exception as e:
        logger.warning("向量库同步跳过（不影响手动精读结果）：{}".format(e))


def _rebuild_month(notes_dir: Path, month: str, settings) -> dict:
    """从当月全部 final bundle 重建手动精读四件套 + 刷索引。"""
    from src.scholar.pdf_ingest import load_bundle, segment_from_bundle, BUNDLE_SUFFIX
    from src.scholar.notes import write_notes
    from src.scholar.notes_index import update_index, write_outputs

    # 已有索引的 citekey 全集：fallback citekey 生成时避开，防止新论文与库内重名
    existing_ckeys = set()
    idx_path = notes_dir / "literature_index.json"
    if idx_path.exists():
        try:
            existing_ckeys = {p.get("citekey") for p in
                              json.loads(idx_path.read_text(encoding="utf-8")).get("papers", [])
                              if p.get("citekey")}
        except Exception:
            pass  # 索引损坏时退化为只查本批，仍安全

    mdir = notes_dir / "manual" / month
    bundles = sorted(mdir.glob("*{}".format(BUNDLE_SUFFIX))) if mdir.exists() else []
    segments = []
    skipped = []
    for bf in bundles:
        try:
            data = load_bundle(bf)
        except Exception as e:
            logger.warning("  ⚠️ 读 bundle 失败 {}: {}".format(bf.name, e))
            continue
        if data.get("status") != "final" or not data.get("close_reading_final"):
            skipped.append(bf.name)
            continue
        seg = segment_from_bundle(data)
        _inject_cross_check(seg, data.get("cross_check_report"))
        segments.append(seg)

    proc = settings.processing
    if not segments:
        logger.info("  {} 无 final bundle，跳过重建（草稿 {} 篇）".format(month, len(skipped)))
        # 仍刷一次索引（若该月手动 md 曾存在但现无 final，索引以磁盘为准）
        idx = update_index(notes_dir)
        write_outputs(idx, notes_dir)
        _sync_embedding_best_effort(notes_dir, idx, settings)
        return {"month": month, "papers": 0, "skipped_drafts": skipped, "index": idx}

    citekeys = {seg.paper_id: None for seg in segments}
    res = write_notes(
        segments, citekeys, out_dir=notes_dir,
        instruction=proc.notes_instruction,
        digest_title="科研札记 · {}（手动深度精读）".format(month),
        filename="科研札记_{}_手动精读".format(month),
        emit_docx=proc.notes_emit_docx, cjk_font=proc.notes_docx_cjk_font,
        fallback_citekeys=True, index_series="manual",
        existing_citekeys=existing_ckeys)
    # 这份索引直接带给 _report_final 复用：全量重建要扫全部札记 md，跑两遍纯属白等
    idx = update_index(notes_dir)
    write_outputs(idx, notes_dir)
    _sync_embedding_best_effort(notes_dir, idx, settings)
    return {"month": month, "papers": len(segments), "skipped_drafts": skipped,
            "md": res["note_path"], "docx": res.get("docx_path"), "index": idx}


def _inject_cross_check(seg, report):
    """把 agent 的交叉核验报告摘要注入为精读末节「交叉核验记录」（渲染层零改动）。"""
    if not report or not seg.close_reading:
        return
    from src.scholar.schema import CloseReadSection, CloseReadSentence
    corrected = report.get("corrected") or []
    added = report.get("added") or []
    verified = report.get("verified_count")
    sents = [CloseReadSentence(
        text="Claude 亲读 PDF 交叉核验：纠错 {} 处、补漏 {} 处{}。".format(
            len(corrected), len(added),
            "、核验通过 {} 项".format(verified) if verified is not None else ""),
        tag=None)]
    # 纠错条 = 论文被证伪/过度断言处 = 天然的「可反驳观点」，打 tag 让其进 refutable highlights
    for c in corrected[:20]:
        if isinstance(c, dict):
            txt = c.get("note") or c.get("text") or json.dumps(c, ensure_ascii=False)
            pg = c.get("page")
            sents.append(CloseReadSentence(
                text="纠错{}：{}".format("（p.{}）".format(pg) if pg else "", txt),
                tag="可反驳观点"))
        else:
            sents.append(CloseReadSentence(text="纠错：{}".format(c), tag="可反驳观点"))
    seg.close_reading.sections.append(
        CloseReadSection(heading="交叉核验记录", sentences=sents))


def cmd_finalize(args):
    settings = _load_settings(args.config)
    notes_dir = Path(settings.processing.notes_dir)
    from src.scholar.pdf_ingest import load_bundle
    bundle = Path(args.bundle).expanduser()
    if not bundle.exists():
        logger.error("❌ bundle 不存在: {}".format(bundle))
        return 1
    data = load_bundle(bundle)
    if data.get("status") != "final" or not data.get("close_reading_final"):
        logger.error("❌ bundle 未 final（需 agent 先写回 close_reading_final + status=final）: {}"
                     .format(bundle.name))
        return 1
    month = data["month"]
    r = _rebuild_month(notes_dir, month, settings)
    _report_final(r, notes_dir)
    return 0


def cmd_regen(args):
    settings = _load_settings(args.config)
    notes_dir = Path(settings.processing.notes_dir)
    month = args.month or _cur_month()
    r = _rebuild_month(notes_dir, month, settings)
    _report_final(r, notes_dir)
    return 0


def _report_final(r, notes_dir):
    idx = r.get("index")
    if idx is None:                       # 兜底：调用方没带索引时才重建
        from src.scholar.notes_index import update_index
        idx = update_index(notes_dir)
    collisions = idx.get("citekey_collisions", [])
    print("\n" + "=" * 66)
    print("✅ 手动精读归档 · {}：{} 篇".format(r["month"], r["papers"]))
    if r.get("md"):
        print("   札记: {}".format(r["md"]))
    if r.get("docx"):
        print("   docx: {}".format(r["docx"]))
    if r.get("skipped_drafts"):
        print("   ⏭ 跳过 {} 篇 draft（未 agent 核验）: {}".format(
            len(r["skipped_drafts"]), ", ".join(r["skipped_drafts"])))
    manual = [e for e in idx["papers"] if e.get("series") == "manual" and not e.get("duplicate_of")]
    print("   索引: 手动深读 {} 篇 · 撞键 {} 组".format(len(manual), len(collisions)))
    if collisions:
        print("   ⚠️ 撞键: {}（合并 bibliography 前跑 notes_index.py --fix-collisions）".format(
            "; ".join(c["citekey"] for c in collisions)))
    print("=" * 66)


def main():
    ap = argparse.ArgumentParser(description="手动 PDF 深度精读：ingest/finalize/regen")
    ap.add_argument("--config", default="config/scholar.env")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ing = sub.add_parser("ingest", help="抽元数据 + 分块通读 → draft bundle")
    p_ing.add_argument("pdf", nargs="+", help="PDF 路径或目录（目录展开为其中全部 *.pdf）")
    p_ing.add_argument("-r", "--recursive", action="store_true",
                       help="目录递归下钻子目录（默认只取该目录一层）")
    p_ing.add_argument("--month", default=None, help="归档月份 YYYY-MM（默认当月）")
    p_ing.add_argument("--title", default=None, help="手动覆盖标题（单篇时；元数据解析用）")
    p_ing.add_argument("--force", action="store_true",
                       help="覆盖已 final 的 bundle（默认跳过——覆盖会丢弃 agent 已写的核验成果）")
    p_ing.set_defaults(func=cmd_ingest)

    p_fin = sub.add_parser("finalize", help="从 final bundle 重建当月手动精读札记")
    p_fin.add_argument("bundle", help="已 final 的 bundle.json 路径")
    p_fin.set_defaults(func=cmd_finalize)

    p_reg = sub.add_parser("regen", help="按现有 final bundle 重建某月（不新增论文）")
    p_reg.add_argument("--month", default=None, help="月份 YYYY-MM（默认当月）")
    p_reg.set_defaults(func=cmd_regen)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
