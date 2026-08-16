# -*- coding: utf-8 -*-
"""按月历史回填科研札记（headless、稳健、可续跑）。

每月：Gmail + PubMed + arXiv → 跨源去重 → LLM 三态筛选 → 元数据增强
      （Crossref/arXiv/translation-server，均不依赖 Zotero 桌面端）
      → 尽力回查 BBT citekey（拿不到用「作者+年份」兜底）→ top-N 强模型全文精读
      → 科研札记_YYYY-MM_全文精读.md/.docx

设计取舍（供无人值守过夜跑）：
- 不写 Zotero 库：避免桌面端需常驻+选中、避免数百条目污染与跨月重复入库。
  权威元数据由 translation-server（Zotero 翻译引擎，Docker）+ Crossref 提供，
  即"依赖 Zotero 数据矫正"。需要入库时，人在时对每月 digest JSON 跑 `zotero --input` 即可。
- 全局去重：同一篇（DOI / arXiv id / 规范标题）只落在最早出现的月份，跨月不重复。
- 每月独立 try/except；已存在同名 .md 的月份默认跳过（可 --force 覆盖）。进度写 backfill_progress.json。

用法：
  PYTHONPATH=. python scripts/backfill_notes.py --since 2023-01 --until 2026-05
  PYTHONPATH=. python scripts/backfill_notes.py --since 2025-06 --until 2025-06   # 单月验证
  PYTHONPATH=. python scripts/backfill_notes.py --prev-month                      # 上一自然月（launchd 月度 job）
  可选 --no-close-read（跳过精读，快速） / --force（覆盖已存在月份） / --top-n N / --no-index

跨运行去重：seen 集从 output/scholar_notes/literature_index.json 恢复（收尾自动刷新该索引），
新月份不会与历史月重复；--force 重跑时自动剔除待跑月份自己的键。
"""
import argparse
import json
import os
import sys
import time
import traceback
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path  # noqa: E402
from src.scholar.schema import ScholarSettings, DigestOutput, DigestStatus  # noqa: E402
from src.scholar.workflow import ScholarWorkflow  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402
from src.utils.notify import notify      # noqa: E402

logger = get_logger("backfill")


def month_list(since: str, until: str):
    sy, sm = map(int, since.split("-"))
    uy, um = map(int, until.split("-"))
    out = []
    y, m = sy, sm
    while (y, m) <= (uy, um):
        out.append((y, m))
        m += 1
        if m > 12:
            y += 1
            m = 1
    return out


def month_range(y, m):
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1)
    from datetime import timedelta
    return start, (end - timedelta(days=1))


# 元数据增强 / citekey 回查 / 去重键：与周度入库（scripts/ingest_notes.py）共用同一实现，
# 否则两条链路各自演化，同一篇论文按月跑与按周跑会算出不同的 dedup_key 而重复入库。
from src.scholar.ingest import (dedup_key, enrich_segments,  # noqa: E402,F401
                                resolve_citekeys)


def run_month(y, m, settings, seen: set, existing_ckeys: set, args) -> dict:
    proc = settings.processing
    label = "{:04d}-{:02d}".format(y, m)
    note_md = Path(proc.notes_dir) / "科研札记_{}_全文精读.md".format(label)
    if note_md.exists() and not args.force:
        # 完成判定不能只看 md 存在：旧版 write_notes 用裸 open('w')（先截断再写、
        # 顺序 md → references → sidecar），写盘中途被杀会留下半态。半态若照旧记
        # skipped 会 exit 0 且无 notify，被截论文既不进索引/seen、该月又被永久跳过
        # ——静默丢失且无自愈路径。与周度 ingest 的同型防线（sidecar 不可读即拒绝
        # 写入）对齐：半态抛错 → main 记 error + notify + 退非零，人工 --force 重跑
        # 该月即可完整重建（--force 会从 seen/existing_ckeys 剔除本月自己的旧键，
        # 见 main()）。
        # 半态签名按旧写序推导：杀在 md → md 空/半截且 references 未落；杀在
        # references → references 半截；杀在 sidecar → sidecar 半截。注意存量有
        # 43 个 sidecar 出现之前写成的月份（md+references 齐全、无 sidecar），是
        # 合法完成态——sidecar 只能「在则必须可读」，缺失本身不算半态，否则范围
        # 重跑会在每个老月份上误报 error（还诱导用 --force 白白重烧一整月 LLM）。
        sidecar = note_md.with_name("{}.index.json".format(note_md.stem))
        refs = note_md.with_name("{}.references.json".format(note_md.stem))
        half = None
        if not note_md.read_text(encoding="utf-8").strip():
            half = "md 为空（0 字节/纯空白）"
        elif not refs.exists():
            half = "references.json 缺失"
        else:
            try:
                json.loads(refs.read_text(encoding="utf-8"))
            except Exception as e:
                half = "references.json 不可读（{}）".format(e)
        if half is None and sidecar.exists():
            try:
                json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception as e:
                half = "索引 sidecar 存在但不可读（{}）".format(e)
        if half:
            raise RuntimeError(
                "{} 札记 md 已存在但{}——疑似上次写盘中断留下的半态，"
                "不能当「已完成」跳过；请用 --force 重跑该月完整重建。"
                .format(label, half))
        logger.info("⏭️  {} 已有札记，跳过（--force 可覆盖）".format(label))
        return {"month": label, "status": "skipped"}

    dr = month_range(y, m)
    proc.auto_mark_read = False
    proc.zotero_enabled = False          # 不写库
    proc.closeread_enabled = False       # 精读在本驱动里单独跑
    proc.external_sources_enabled = True # PubMed + arXiv
    proc.filter_mode = "llm"
    # 过夜吞吐优化：整月 Gmail 常有数百篇，逐篇跑 LLM 批处理步骤太慢；
    # 交付重点是 top-N 全文精读，非 top-N 只保留摘要即可 → 默认关批处理步骤、加大筛选批量。
    # 注：generate_summary 不产出独立总结（batch prompt 从未要求模型给 summary 字段），
    # 只影响非 top-N 论文是否仍跑一遍关键词/相关度/优先级评分。
    proc.translate_abstracts = False
    proc.generate_summary = bool(args.summary)
    proc.batch_size = max(proc.batch_size, args.batch_size)

    wf = ScholarWorkflow(settings)
    wf.date_range = dr
    out = wf.execute()                   # fetch + 跨源去重 + filter + translate/summary + 出 JSON
    segs = out.segments

    # 全局跨月去重：同一篇只留最早月份。
    # 本月新增键先收进局部 month_keys，write_notes 成功后才并入 seen——过早并入的话，
    # 本月中途失败（Crossref 超时/写盘失败）后同一篇在后续月份会被幽灵键误判成
    # 「早前月已收录」而静默丢失（实际从未落盘）。异常路径自然不并入。
    fresh = []
    month_keys: set = set()
    for seg in segs:
        k = dedup_key(seg.metadata)
        if k in seen or k in month_keys:
            continue
        month_keys.add(k)
        fresh.append(seg)
    dropped = len(segs) - len(fresh)
    logger.info("  {} 入选 {} 篇，跨月去重后 {} 篇（去掉 {} 篇早前月已收录）".format(
        label, len(segs), len(fresh), dropped))

    if not fresh:
        logger.info("  {} 去重后无新论文，不出札记".format(label))
        return {"month": label, "status": "empty", "included": len(segs), "fresh": 0}

    email = proc.zotero_email or proc.external_email or ""
    cr, ax, ts = enrich_segments(fresh, email, proc.zotero_translation_server_url)

    # 增强后再去一次重（同 run_ingest 的补丁）：上面那轮去重算键时多数条目还没有
    # DOI，dedup_key 退到标题；Crossref 补上 DOI 后键会变，可能撞上早前月已收录的
    # DOI 键——漏掉这一轮，同一篇会跨月二次精读、二次入库。month_keys 里是本月
    # 自己刚占的键（含增强前的标题键），不能当"早前月已有"误杀。
    kept, late = [], 0
    for seg in fresh:
        k = dedup_key(seg.metadata)
        if k in seen and k not in month_keys:
            late += 1
            continue
        month_keys.add(k)
        kept.append(seg)
    if late:
        logger.info("  增强后补去重：{} 篇补到 DOI 后发现早前月已收录".format(late))
    fresh = kept
    if not fresh:
        logger.info("  {} 增强后无新论文，不出札记".format(label))
        return {"month": label, "status": "empty", "included": len(segs), "fresh": 0}

    citekeys = resolve_citekeys(fresh, proc.zotero_base_url)

    full_text = 0
    if not args.no_close_read:
        from src.scholar.closereading import close_read_segments
        from src.scholar.llm_client import LLMClient
        llm_client = LLMClient(settings.llm)
        try:
            done = close_read_segments(
                fresh, proc.research_interests, llm_client,
                top_n=args.top_n, email=email,
                model=(settings.llm.closeread_model or settings.llm.model),
                scratch_dir=Path("output/scholar_pdfs"),
                # 与周度 ingest 同一个开关：漏传会让 CLOSEREAD_DEEP 打开后周度深读、
                # 月度回填仍单跳，两代札记在同一索引里无声混存
                deep=proc.closeread_deep, max_chars=proc.closeread_max_chars,
                max_chunks=proc.closeread_max_chunks)
        finally:
            llm_client.close()
        if args.top_n > 0 and fresh and done == 0:
            # 0/N 成功几乎只有 LLM 通路整体故障（限流/欠费）一种解释；照常写盘会把
            # 降级札记固化成"已完成"（note_md.exists() 永久跳过，--force 需要人先发现），
            # 宁可本月不写、由 main 记 error 退非零，重跑即完整恢复。done≥1 照常写盘。
            raise RuntimeError("全文精读 0/{}，视为 LLM 故障批，不写终稿".format(
                min(args.top_n, len(fresh))))
        full_text = sum(1 for s in fresh if s.close_reading and s.close_reading.from_full_text)

    # existing_ckeys 已在 main() 中从 literature_index.json 一次性计算，避免 41 个月重复 IO
    from src.scholar.notes import write_notes
    res = write_notes(
        fresh, citekeys, out_dir=Path(proc.notes_dir),
        instruction=proc.notes_instruction,
        digest_title="科研札记 · {}（全文精读）".format(label),
        filename="科研札记_{}_全文精读".format(label),
        emit_docx=proc.notes_emit_docx, cjk_font=proc.notes_docx_cjk_font,
        fallback_citekeys=True, existing_citekeys=existing_ckeys)  # headless：无 Zotero key 时用人读临时键，避免 MISSING-KEY
    seen |= month_keys           # 落盘成功，本月键此刻才算真正「已收录」

    hit_ck = sum(1 for v in citekeys.values() if v)
    logger.info("  ✅ {} → {} 篇 | citekey {}/{} | 全文精读 {} | 增强 CR{}/AX{}/TS{}".format(
        label, len(fresh), hit_ck, len(fresh), full_text, cr, ax, ts))
    return {"month": label, "status": "ok", "included": len(segs), "fresh": len(fresh),
            "citekey": hit_ck, "full_text": full_text,
            "md": res["note_path"], "docx": res.get("docx_path"),
            # index_sidecar：本月 write_notes 顺手写出的 {slug}.index.json，供 main()
            # 把本月新生成的兜底 citekey 并回 existing_ckeys（碰撞窗口修复，见 main()）。
            "index_sidecar": res.get("index_sidecar")}


def prev_month_label(today=None):
    d = today or date.today()
    y, m = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
    return "{:04d}-{:02d}".format(y, m)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2023-01")
    ap.add_argument("--until", default="2026-05")
    ap.add_argument("--prev-month", action="store_true",
                    help="只跑上一个自然月（覆盖 --since/--until，供 launchd 月度 job 静态调用）")
    ap.add_argument("--no-index", action="store_true", help="收尾不刷新文献索引")
    ap.add_argument("--config", default="config/scholar.env")
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--batch-size", type=int, default=15, help="LLM 筛选批量（过夜吞吐）")
    ap.add_argument("--summary", action="store_true",
                    help="为非 top-N 也跑一遍 LLM 批处理步骤（更慢；不产出独立总结，只影响关键词/相关度/优先级评分）")
    ap.add_argument("--no-close-read", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--token-path", default="", help="独立 Gmail token 副本路径（并行分片防写冲突）")
    args = ap.parse_args()
    if args.prev_month:
        args.since = args.until = prev_month_label()

    settings = ScholarSettings.from_env_file(repo_path(args.config))
    # 相对 notes_dir 锚死仓库根，别随 cwd 漂（见 paths.repo_path）
    settings.processing.notes_dir = repo_path(settings.processing.notes_dir)
    # 并行分片：每个进程用独立 token 副本，避免多进程刷新时并发写 config/token.json 损坏
    if args.token_path:
        import shutil
        tp = Path(args.token_path)
        tp.parent.mkdir(parents=True, exist_ok=True)
        src = settings.gmail.token_path
        if src.exists() and not tp.exists():
            shutil.copy2(str(src), str(tp))
        settings.gmail.token_path = tp
    if settings.llm.provider == "gemini" and settings.llm.model.startswith("gemini"):
        settings.llm.provider = "deepseek"  # 与既有 pipeline 一致，走 deepseek
        logger.info("LLM provider 从 gemini 自动切换为 deepseek（model={}）".format(settings.llm.model))
    months = month_list(args.since, args.until)
    logger.info("=" * 60)
    logger.info("按月回填：{} → {}，共 {} 个月".format(args.since, args.until, len(months)))
    logger.info("精读: {} (top-{}, 模型 {})".format(
        not args.no_close_read, args.top_n, settings.llm.closeread_model or settings.llm.model))
    logger.info("=" * 60)

    # 进度文件按分片区分，避免多进程并行时互相覆盖
    prog_path = Path(settings.processing.notes_dir) / "backfill_progress_{}_{}.json".format(args.since, args.until)
    prog_path.parent.mkdir(parents=True, exist_ok=True)
    results = []
    if prog_path.exists():
        try:
            results = json.loads(prog_path.read_text(encoding="utf-8")).get("results", [])
        except Exception:
            results = []
    # 全局去重集：从文献索引恢复（跨运行持久化——跑新月份不与历史月重复）。
    # --force 重跑历史月时剔除待跑月份自己的键，否则本月论文全在 seen 里会被 dedup 成空札记。
    from src.scholar.notes_index import load_seen_keys, existing_citekeys
    run_months = {"{:04d}-{:02d}".format(y, m) for y, m in months}
    seen: set = load_seen_keys(
        Path(settings.processing.notes_dir) / "literature_index.json",
        exclude_months=run_months if args.force else None)
    logger.info("跨运行去重集：从索引恢复 {} 个键".format(len(seen)))
    # 与 seen 同源一次性计算 existing_ckeys，避免 41 个月每轮重复读 literature_index.json。
    # --force 重跑历史月时排除本次要重写的札记文件自己的旧 citekey——否则本月兜底键重算出
    # 同一个 base 时会被判「库内已占用」而加消歧后缀，下一轮又因为后缀键才是「已占用」而
    # 改回原键，来回改名（citekey 抖动，同 read_pdf._rebuild_month 已修过的坑）。
    own_note_files = ({"科研札记_{}_全文精读.md".format(label) for label in run_months}
                      if args.force else set())
    idx_path = Path(settings.processing.notes_dir) / "literature_index.json"
    existing_ckeys: set = existing_citekeys(idx_path, exclude_note_files=own_note_files)

    for i, (y, m) in enumerate(months, 1):
        label = "{:04d}-{:02d}".format(y, m)
        logger.info("\n" + "#" * 60)
        logger.info("# [{}/{}] 月份 {}".format(i, len(months), label))
        logger.info("#" * 60)
        t0 = time.time()
        try:
            r = run_month(y, m, settings, seen, existing_ckeys, args)
        except SystemExit:
            raise
        except Exception as e:
            logger.error("❌ {} 失败：{}".format(label, e))
            logger.error(traceback.format_exc())
            r = {"month": label, "status": "error", "error": str(e)}
        if r.get("status") == "ok" and r.get("index_sidecar"):
            # 碰撞窗口修复：existing_ckeys 只在循环开始前从 literature_index.json 算过
            # 一次快照，主索引要等本轮全部月份跑完才由 update_index 刷新——本月刚生成
            # 的兜底 citekey 若不当场并回 existing_ckeys，下一个月遇到同样的 base
            # （常客作者连续几个月都在库里投稿很常见）会看不到本月已占用这个键，算出
            # 同一个兜底 citekey，两个月各出一篇同名 citekey 的札记。
            try:
                existing_ckeys |= existing_citekeys(Path(r["index_sidecar"]))
            except Exception as e:
                logger.warning("  ⚠️ 并回本月 sidecar citekey 失败（不影响本月已写盘的札记）: {}".format(e))
        r["elapsed_sec"] = round(time.time() - t0, 1)
        results = [x for x in results if x.get("month") != label] + [r]
        # 原子写：先写 tmp 再 os.replace，避免半写 JSON 丢失整月进度
        # （参考 src/scholar/notes_index.write_if_changed() 的 tmp+replace 模式）
        tmp_path = prog_path.with_suffix(prog_path.suffix + '.tmp-{}'.format(os.getpid()))
        tmp_path.write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        os.replace(tmp_path, prog_path)
        logger.info("  ⏱️ {} 用时 {}s".format(label, r["elapsed_sec"]))

    # 收尾：增量刷新文献索引（供论文项目 agent 检索；也是下次运行去重集的来源）
    # 刷新失败不再只 warning：索引就是下次运行的去重集，坏了会连锁到后续每次跑。
    index_err = None
    if not args.no_index:
        try:
            from src.scholar.notes_index import update_index, write_outputs
            notes_dir = Path(settings.processing.notes_dir)
            index_data = update_index(notes_dir)
            write_outputs(index_data, notes_dir)
            # best-effort 向量同步：batch 回填后跟进向量库（失败只 warning，不影响回填结果）
            try:
                from src.scholar.embeddings import EmbeddingClient, resolve_embedding_base_url
                from src.scholar.embed_store import DB_NAME, sync_store
                client = EmbeddingClient(
                    base_url=resolve_embedding_base_url(settings.llm),
                    model=settings.llm.embedding_model,
                )
                try:
                    stats = sync_store(notes_dir / DB_NAME, index_data, client)
                finally:
                    client.close()
                logger.info("向量库已同步：+{} 嵌入 / -{} 删除 / {} 元数据刷新".format(
                    stats.embedded, stats.deleted, stats.meta_refreshed))
            except Exception as e2:
                logger.warning("向量库同步跳过（不影响回填结果）：{}".format(e2))
                # 每月 1 日 launchd 无人值守——弹系统通知，向量库静默落后要有人知道
                notify("Scholar 月度回填", "向量库同步失败（回填已完成）：{}".format(str(e2)[:120]))
        except Exception as e:
            index_err = e
            logger.warning("⚠️ 刷新文献索引失败（可手动跑 scripts/notes_index.py）: {}".format(e))

    ok = [r for r in results if r.get("status") == "ok"]
    # 只统计本次 run_months：progress 文件跨运行续用，历史遗留的陈旧 error 条目
    # 不该让这次全部成功的运行误报非零退出。
    errs = [r for r in results if r.get("month") in run_months and r.get("status") == "error"]
    logger.info("\n" + "=" * 60)
    logger.info("回填完成：成功 {} 个月 / 共 {}".format(len(ok), len(months)))
    logger.info("札记目录: {}".format(settings.processing.notes_dir))
    logger.info("=" * 60)
    if errs or index_err is not None:
        # launchd 月度 job 无人值守：失败必须退非零 + 弹通知，否则整月缺失无人知晓
        parts = []
        if errs:
            parts.append("失败月份: {}".format(", ".join(sorted(r.get("month", "?") for r in errs))))
        if index_err is not None:
            parts.append("收尾索引刷新失败: {}".format(index_err))
        msg = "；".join(parts)
        notify("Scholar 月度回填失败", msg)
        logger.error("❌ {}".format(msg))
        sys.exit(1)


if __name__ == "__main__":
    main()
