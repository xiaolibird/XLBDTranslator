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

from src.scholar.settings import load_scholar_settings  # noqa: E402
from src.scholar.llm_client import LLMClient  # noqa: E402
from src.scholar.notes_index import INDEX_JSON  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402

logger = get_logger("read_pdf")


def _cur_month():
    d = date.today()
    return "{:04d}-{:02d}".format(d.year, d.month)


def _month_arg(v):
    """--month 的 argparse type：入口拦下 "2026-7" 这类畸形月份。

    不拦的话文件照常落盘、退出 0，但 notes_index 的 NOTE_MD_RE 认不出文件名，
    这篇精读对索引/seen/向量库全部不可见，下月还会被当新论文重读（详见
    notes_index.validate_note_label 的注释）。专题批次（2026-07-28-TFM 等）
    是存量在用的合法形状，校验口径与 NOTE_MD_RE 同构、不额外收紧。
    """
    from src.scholar.notes_index import validate_note_label
    try:
        return validate_note_label(v)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def _load_settings(config):
    # 薄包装保名：test_read_pdf_cli.py monkeypatch 钉住这个符号。逻辑全在共享 loader。
    return load_scholar_settings(config)


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
                index_path=notes_dir / INDEX_JSON,
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
    _print_attention(outs, failed,
                     title_ignored=(args.title or "") if len(pdfs) > 1 else "")
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


def _print_attention(outs, failed, title_ignored: str = ""):
    """把「必须有人看一眼」的事项汇总到输出最末。

    单篇时散在中间也看得见，21 篇一批时必被淹掉——今天正是这样漏掉一条「索引里已有同文」，
    白读了一篇几个月前已精读过的论文。title_ignored 同理：批量时被丢弃的 --title 若只在
    循环前 warning 一次，会落在同一个被淹掉的位置。
    """
    dups = [r for r in outs if r.get("duplicate")]
    thin = [r for r in outs if not r.get("skipped")
            and (r.get("meta_source") == "pdf-only" or not r.get("authors_n"))]
    skipped = [r for r in outs if r.get("skipped") == "final"]
    n = len(dups) + len(thin) + len(skipped) + len(failed) + (1 if title_ignored else 0)
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
        print("    → citekey 会退化成 anon*、bibliography 缺卷期页；"
              "可**单独对那一篇**重跑 ingest --title \"精确标题\"（--title 批量时不生效）")
    if title_ignored:
        print("  · --title \"{}\" 本次被忽略：批量时不生效".format(title_ignored[:40]))
        print("    → 需要覆盖标题请单独对那一篇重跑 ingest --title")
    for r in skipped:
        print("  · 已 final 未覆盖：{}".format(r["title"][:48]))
    for name, err in failed:
        print("  · ingest 失败：{} —— {}".format(name, err[:80]))


def _sync_embedding_best_effort(notes_dir: Path, index: dict, settings) -> None:
    """best-effort 向量库同步：手动精读重建索引后跟进向量库。

    收敛在 embed_store.sync_store_best_effort（契约见其 docstring）。这次收敛顺带
    修掉一个漂移：此前这里失败只 warning 不 notify，而本文件 Y3 复审早已论证过
    「实际跑这条路径的是自动权限模式的 agent 会话，warning 没人看」——同一论证
    对向量同步等价成立，现在 notify 是共享函数的统一行为。
    """
    from src.scholar.embed_store import sync_store_best_effort
    sync_store_best_effort(notes_dir, index, settings,
                           notify_title="Scholar 手动精读", context="手动精读归档")


def _sync_topics_best_effort(notes_dir: Path, note_md: str) -> None:
    """best-effort 触发概念页路由 + 合成（P2：让概念页跟着新论文长）。

    收敛在 topics.trigger_topic_refresh（W3/W6/W7/Y3 战史与安全前提见其 docstring；
    W6 正是本脚本——三条入库路径里它曾是漏接概念页的那条）。返回值有意忽略：
    read_pdf 是交互/skill 驱动入口，docs/skills/read-paper 的批量协议依赖现行
    退出码语义，概念页失败靠共享函数里的 notify 保证可见，不影响本次精读退出码。
    """
    from src.scholar.topics import trigger_topic_refresh
    trigger_topic_refresh(notes_dir, note_md=note_md,
                          notify_title="Scholar 手动精读", subject="精读已正常归档")


def _reuse_citekeys(notes_dir: Path, month: str, segments) -> dict:
    """按 dedup_key 沿用上一轮 sidecar 里的 citekey，返回 {paper_id: citekey|None}。

    为什么必须沿用：finalize/regen 是**整月重建**，此前 citekeys 全传 None，
    write_notes 会对每篇现算 `_fallback_citekey(metadata)`。于是任何在札记侧做过的
    改键（如 audit_citekeys_vs_pmlr.py 按 PMLR 官方 slug 修正复姓/年份，实测 22 篇）
    都会在下一次 regen 被 bundle 里的旧元数据顶回去——bundle 里根本没有 citekey 字段，
    它不是身份、只是每次现算的派生量。沿用的锚点用 dedup_key（doi/arxiv/场地 id/标题），
    与索引侧同一套键梯，故 bundle 元数据变动不影响匹配。

    两侧撞键都拒绝沿用（宁可退回 fallback 让 used 集合去消歧，也不能让两篇共用一个键）：
      - 旧 sidecar 里同一 dedup_key 出现多次（历史撞键态）；
      - **本批 segments 里同一 dedup_key 出现多次**（同 DOI 的 PDF 改标题重 ingest
        会产出两个 paper_id、两份 bundle）。此时若都命中映射就会拿到同一个显式键，
        而 write_notes 对显式键不做查重（notes.py 的消歧循环只处理 None），
        结果是一份 md 里两篇同 citekey —— 静默撞键。
    """
    from src.scholar._citekey_utils import recompute_entry_key
    from src.scholar.ingest import dedup_key as seg_dedup_key

    out = {seg.paper_id: None for seg in segments}
    sidecar = notes_dir / "科研札记_{}_手动精读.index.json".format(month)
    if not sidecar.exists():
        return out
    try:
        rows = (json.loads(sidecar.read_text(encoding="utf-8")) or {}).get("papers") or []
    except Exception as e:                      # 坏 sidecar 不该挡住重建
        # 代价必须可见：不沿用 = 札记侧已修正的键（audit_citekeys_vs_pmlr 实测 22 篇）
        # 在这一轮 regen 被 bundle 里的旧元数据顶回，而 citekey 是 all_references /
        # vault / 已发出的 [@key] 引用的身份锚。warning 在自动权限模式的 agent 会话里
        # 看不见（同 _sync_embedding_best_effort 的论证），故走 notify。
        logger.error("  ⛔ 读 sidecar 失败，本次**不沿用 citekey**（本月键将被重算，"
                     "已在札记侧修正过的键会被顶回）（{}）: {}".format(sidecar.name, e))
        try:
            from src.utils.notify import notify
            notify("Scholar 手动精读",
                   "sidecar 读失败，{} 月 citekey 不沿用、将被重算：{}".format(month, str(e)[:120]))
        except Exception:                        # notify 失败不该挡住重建
            pass
        return out

    prev: dict = {}
    for r in rows:
        if not isinstance(r, dict) or not r.get("citekey"):
            continue
        k = recompute_entry_key(r)
        prev[k] = None if k in prev else r["citekey"]   # 出现第二次 → 置 None = 拒绝沿用

    seen: dict = {}
    for seg in segments:
        seen.setdefault(seg_dedup_key(seg.metadata), []).append(seg.paper_id)

    reused = 0
    for k, pids in seen.items():
        if len(pids) > 1:                       # 本批自身撞键 → 全走 fallback
            logger.warning("  ⚠️ 本批 {} 篇共用 dedup_key {}，均不沿用旧键".format(len(pids), k))
            continue
        ck = prev.get(k)
        if ck:
            out[pids[0]] = ck
            reused += 1
    if reused:
        logger.info("  沿用上一轮 citekey {} 篇（按 dedup_key 匹配）".format(reused))
    return out


def _rebuild_month(notes_dir: Path, month: str, settings) -> dict:
    """从当月全部 final bundle 重建手动精读四件套 + 刷索引。"""
    from src.scholar.pdf_ingest import load_bundle, segment_from_bundle, BUNDLE_SUFFIX
    from src.scholar.notes import write_notes
    from src.scholar.notes_index import update_index, write_outputs, existing_citekeys

    # 已有索引的 citekey 全集：fallback citekey 生成时避开，防止新论文与库内重名。
    # 但要排除本月这份手动精读 md 自己的旧条目——否则每次 finalize/regen 整篇重写
    # 本月 md 时，本月论文的上一轮 citekey 会被当成「库内已占用」，被迫加消歧后缀，
    # 下一轮又因为后缀键才是「已占用」而改回原键，来回改名（citekey 抖动）。
    own_note_file = "科研札记_{}_手动精读.md".format(month)
    idx_path = notes_dir / INDEX_JSON
    existing_ckeys = existing_citekeys(idx_path, exclude_note_files={own_note_file})

    mdir = notes_dir / "manual" / month
    bundles = sorted(mdir.glob("*{}".format(BUNDLE_SUFFIX))) if mdir.exists() else []
    segments = []
    skipped = []
    broken = []
    for bf in bundles:
        try:
            data = load_bundle(bf)
        except Exception as e:
            # 读不出 = 这篇的全部亲读核验成果进不了库。只 continue 会让它既不在
            # skipped_drafts 也不在 papers 里，_report_final 照打 ✅——一篇已核验完的
            # 论文永久蒸发且回执是绿的（agent 收到成功回执不会重跑，PDF 也已移出待读）。
            logger.warning("  ⚠️ 读 bundle 失败 {}: {}".format(bf.name, e))
            broken.append(bf.name)
            continue
        if data.get("status") != "final" or not data.get("close_reading_final"):
            skipped.append(bf.name)
            continue
        # 门禁下沉：cmd_finalize 的核验门禁只挡命令行点名的那一份 bundle，而这里是
        # **整月**重建——同月里 agent 自报 status=final 却没写 cross_check_report 的
        # bundle 会随合规 bundle 搭车写进 md/docx/索引/向量库（regen 更是零门禁直达）。
        # 脚本草稿的系统性偏差正是靠亲读核验挡的，故无报告一律拒绝纳入。
        # 只查「报告存在」、不硬卡 verified_count>=1：存量已核验 bundle 的报告是
        # 异构 schema（verified_count 为 None / 用 verified 键，实测 7 份），卡计数
        # 会把真核验过的旧篇挤出当月重建——严格计数门禁留给 cmd_finalize 管新增。
        if not data.get("cross_check_report"):
            logger.warning("  ⛔ {} 无 cross_check_report（status=final 系 agent 自报，"
                           "未经亲读核验），不纳入本月重建；补核验报告后重跑 finalize"
                           .format(bf.name))
            skipped.append(bf.name)
            continue
        # 显式自报「一项都没核验」的 bundle：自己 finalize 会被 cmd_finalize 的
        # verified_count>=1 拒掉，却能靠同月兄弟触发的整月重建搭车入库。只拒显式的 0，
        # None/缺键的 legacy 异构报告照旧放行（上一段已论证）。
        _vc = (data.get("cross_check_report") or {}).get("verified_count") \
            if isinstance(data.get("cross_check_report"), dict) else None
        if isinstance(_vc, int) and _vc < 1:
            logger.warning("  ⛔ {} 的 cross_check_report 自报 verified_count=0（一项都没核验），"
                           "不纳入本月重建".format(bf.name))
            skipped.append(bf.name)
            continue
        try:
            seg = segment_from_bundle(data)
            _inject_cross_check(seg, data.get("cross_check_report"))
        except Exception as e:
            # bundle 的 close_reading_final / cross_check_report 是 agent 手写的 JSON，
            # 形状错误（tag 写成近义词、报告写成字符串）属常态失误。不隔离就是一份坏
            # bundle 抛裸 traceback 炸掉整月归档，且异常里不含文件名，21 篇一批只能二分排查。
            logger.error("  ⛔ bundle 结构非法，跳过 {}（{}: {}）；修好后重跑 finalize"
                         .format(bf.name, type(e).__name__, str(e)[:300]))
            broken.append(bf.name)
            continue
        segments.append(seg)

    proc = settings.processing
    if not segments:
        logger.info("  {} 无 final bundle，跳过重建（草稿 {} 篇）".format(month, len(skipped)))
        # 仍刷一次索引（若该月手动 md 曾存在但现无 final，索引以磁盘为准）
        idx = update_index(notes_dir)
        write_outputs(idx, notes_dir)
        _sync_embedding_best_effort(notes_dir, idx, settings)
        return {"month": month, "papers": 0, "skipped_drafts": skipped,
                "broken_bundles": broken, "index": idx}

    citekeys = _reuse_citekeys(notes_dir, month, segments)
    res = write_notes(
        segments, citekeys, out_dir=notes_dir,
        instruction=proc.notes_instruction,
        digest_title="科研札记 · {}（手动深度精读）".format(month),
        filename="科研札记_{}_手动精读".format(month),
        emit_docx=proc.notes_emit_docx, cjk_font=proc.notes_docx_cjk_font,
        fallback_citekeys=True, index_series="manual",
        existing_citekeys=existing_ckeys,
        # 沿用的是上一轮的兜底键，不是 Zotero 权威键，别在 sidecar 里冒充
        explicit_citekey_source="fallback")
    # 这份索引直接带给 _report_final 复用：全量重建要扫全部札记 md，跑两遍纯属白等
    idx = update_index(notes_dir)
    write_outputs(idx, notes_dir)
    _sync_embedding_best_effort(notes_dir, idx, settings)
    _sync_topics_best_effort(notes_dir, res["note_path"])   # W6：接入 P2，见函数文档
    return {"month": month, "papers": len(segments), "skipped_drafts": skipped,
            "broken_bundles": broken,
            "md": res["note_path"], "docx": res.get("docx_path"), "index": idx}


def _first_list(rep, *keys):
    """按顺序取第一个**确实是数组**的别名值，取不到返回 []。

    不能用 `rep.get(a) or rep.get(b)`：旧 schema 里同名字段可能是计数（实测
    `"added_new": 2`），链式 or 会把它取进来当数组用。
    """
    for k in keys:
        v = rep.get(k)
        if isinstance(v, list):
            return v
    return []


def _inject_cross_check(seg, report):
    """把 agent 的交叉核验报告摘要注入为精读末节「交叉核验记录」（渲染层零改动）。"""
    if not report or not seg.close_reading:
        if seg.close_reading and not report:
            # 静默跳过会让「未经核验的精读」与「核验过的精读」在札记里无法区分。
            # _rebuild_month 纳入时已拒绝无报告的 bundle，此分支正常不可达——
            # 留作纵深防御：将来若有调用方绕过门禁直接注入，至少在日志里留痕。
            logger.warning("⚠️ {} 无 cross_check_report：该篇精读未经亲读核验（legacy bundle）"
                           .format((seg.metadata.title or seg.paper_id or "?")[:60]))
        return
    from src.scholar.schema import CloseReadSection, CloseReadSentence
    # 形状防线：报告与 corrected/added 都是 agent 手写的。非 dict 会在 report.get 处炸；
    # corrected 写成字符串则更坏——`for c in corrected[:20]` 把它逐字符切片，一句纠错
    # 变成十几条单字 highlights 全部进 refutable 取证轴，静默投毒。抛出由 _rebuild_month
    # 的 try 路由进 broken_bundles（拒收该篇并点名），不改 :319 的「无报告即拒」门禁语义。
    if not isinstance(report, dict):
        raise ValueError("cross_check_report 不是 JSON 对象（实为 {}）".format(type(report).__name__))
    # 别名兼容：早期 3 种 schema 用 corrections/additions 等键，只认 corrected/added 会把
    # 这些篇的核验内容静默吞成「纠错 0 处、补漏 0 处」（存量实测约 10 篇已如此）。
    # **别名只在取到数组时才认**：同名字段在旧 schema 里可能是计数——磁盘上真有一份
    # `{"corrections": [...], "added_new": 2, "corrected_or_rewritten": 6}`，把 2 当数组会
    # 让这篇已归档的 final 被拒收，下一次同月 finalize 就把它从 md/索引/书目/向量库一并抹掉。
    # 硬拒只留给**主键显式写错类型**（corrected: "字符串" 的逐字符切片投毒仍拒）。
    corrected = _first_list(report, "corrected", "corrections")
    added = _first_list(report, "added", "additions", "added_new")
    for _k in ("corrected", "added"):
        if _k in report and not isinstance(report[_k], list):
            raise ValueError("cross_check_report 的 {} 必须是数组（实为 {}）"
                             .format(_k, type(report[_k]).__name__))
    if not any(k in report for k in ("corrected", "corrections", "added", "additions")):
        logger.warning("⚠️ cross_check_report 既无 corrected 也无 corrections 键（现有键：{}）"
                       "——纠错/补漏内容不会进札记".format(sorted(report)[:8]))
    verified = report.get("verified_count")
    sents = [CloseReadSentence(
        text="Claude 亲读 PDF 交叉核验：纠错 {} 处、补漏 {} 处{}。".format(
            len(corrected), len(added),
            "、核验通过 {} 项".format(verified) if verified is not None else ""),
        tag=None)]
    # 纠错条**不打 tag**：曾按「纠错 = 论文被证伪/过度断言处」打成「可反驳观点」，但实测
    # 918/3630（25.3%）的 manual refutable 取证条是这类，且内容压倒性是**草稿的错**而非
    # 论文的可质疑处（「Opus 原稿所说的…」「Opus 原稿标注为 p.25，实际跨页」）。而 manual
    # 在 _keeper_rank 里恒为 keeper、topics 还给它加权，等于给 scholar-write / notes_query
    # 的 refutable 轴优先供应勘误噪声。cross_check_report 的 schema 无字段区分「论文级问题」
    # 与「草稿级勘误」，故安全默认是 None：句子仍完整留在「交叉核验记录」节里（留痕不丢），
    # 只是不再冒充可反驳证据。存量随各月 regen 重建自动清理。
    for c in corrected[:20]:
        if isinstance(c, dict):
            txt = c.get("note") or c.get("text") or json.dumps(c, ensure_ascii=False)
            pg = c.get("page")
            sents.append(CloseReadSentence(
                text="纠错{}：{}".format("（p.{}）".format(pg) if pg else "", txt),
                tag=None))
        else:
            sents.append(CloseReadSentence(text="纠错：{}".format(c), tag=None))
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
    try:
        data = load_bundle(bundle)
    except Exception as e:
        logger.error("❌ bundle 不可解析（{}: {}）: {}".format(type(e).__name__, e, bundle.name))
        return 1
    if data.get("status") != "final" or not data.get("close_reading_final"):
        logger.error("❌ bundle 未 final（需 agent 先写回 close_reading_final + status=final）: {}"
                     .format(bundle.name))
        return 1
    # 双轨核验不能只是君子协定：status=final + close_reading_final 非空全是 agent 自报，
    # 不读 PDF 直接把脚本草稿抄成 final 也能通过——而脚本草稿的系统性偏差（把研究画像
    # 写成原文观点、虚构章节）正是要靠亲读核验挡的。机器门禁至少要求核验报告存在且
    # verified_count>=1；伪造报告仍可能，但「忘了核验/偷懒跳过」这类最常见的失败被挡死。
    ccr = data.get("cross_check_report")
    if ccr is not None and not isinstance(ccr, dict):
        # 手写成字符串时，下一行的 .get 会抛 AttributeError 裸 traceback（同一份 bundle
        # 在 _rebuild_month 里也炸，但这里先撞上）——给出指名道姓的错误而不是栈。
        logger.error("❌ cross_check_report 不是 JSON 对象（当前 {}）：{}"
                     .format(type(ccr).__name__, bundle.name))
        return 1
    ccr = ccr or {}
    vc = ccr.get("verified_count")
    if not isinstance(vc, int) or vc < 1:
        logger.error("❌ bundle 缺有效 cross_check_report（需 verified_count>=1，当前: {!r}）：\n"
                     "   亲读 PDF 交叉核验是归档硬前提，不可省。请 agent 完成核验并写回\n"
                     "   cross_check_report（含 verified_count/corrected/added）后再 finalize: {}"
                     .format(vc, bundle.name))
        return 1
    month = data["month"]
    # bundle 里的 month 可能是修复前用畸形 --month ingest 出来的遗留值——finalize 是
    # 落盘前最后一道闸，这里不拦就会写出索引认不出的札记文件（与入口校验同一坑）。
    from src.scholar.notes_index import validate_note_label
    try:
        validate_note_label(month)
    except ValueError as e:
        logger.error("❌ bundle 的归档月份非法（{}）：请把 {} 的 month 改成合法值"
                     "并挪到对应的 manual/<月份>/ 目录后再 finalize".format(e, bundle.name))
        return 1
    # month 字段与所在目录必须一致：finalize 是**按月重建**，只 glob manual/<month>/。
    # 字段写 A 而文件躺在 B 桶时，重建扫的是 A 桶（没有这篇）、这篇所在的 B 桶没人扫，
    # 于是退出码 0、打印「✅ 归档 A：0 篇」，一篇已完成亲读核验的论文彻底消失。
    # 上面那条错误文案本就在教用户维持这个不变量，这里把它变成机器检查。
    bucket = bundle.resolve().parent.name
    if bucket != month:
        logger.error("❌ bundle 的 month 字段（{}）与所在目录（{}）不一致——finalize 按月重建，"
                     "这篇不会被纳入任何一月。请把它挪进 manual/{}/ 后再跑: {}"
                     .format(month, bucket, month, bundle.name))
        return 1
    r = _rebuild_month(notes_dir, month, settings)
    _report_final(r, notes_dir)
    # 在 _rebuild_month 里「拒收」等价于「从已归档的 md 里删掉」：整月 md 被重写、索引重建、
    # 向量库同步，一篇已核验论文当场蒸发。绿回执 + exit 0 会让自动权限模式的 agent 直接
    # 往下走（同 audit_citekeys_vs_pmlr 的论证），所以这里必须非 0。
    return 1 if r.get("broken_bundles") else 0


def cmd_regen(args):
    settings = _load_settings(args.config)
    notes_dir = Path(settings.processing.notes_dir)
    month = args.month or _cur_month()
    r = _rebuild_month(notes_dir, month, settings)
    _report_final(r, notes_dir)
    return 1 if r.get("broken_bundles") else 0


def _report_final(r, notes_dir):
    idx = r.get("index")
    if idx is None:                       # 兜底：调用方没带索引时才重建
        from src.scholar.notes_index import update_index
        idx = update_index(notes_dir)
    collisions = idx.get("citekey_collisions", [])
    print("\n" + "=" * 66)
    if r.get("broken_bundles"):
        print("⛔ 手动精读归档 · {}：{} 篇（**有 bundle 未入库，见下**）".format(
            r["month"], r["papers"]))
    else:
        print("✅ 手动精读归档 · {}：{} 篇".format(r["month"], r["papers"]))
    if r.get("md"):
        print("   札记: {}".format(r["md"]))
    if r.get("docx"):
        print("   docx: {}".format(r["docx"]))
    if r.get("skipped_drafts"):
        print("   ⏭ 跳过 {} 篇 draft（未 agent 核验）: {}".format(
            len(r["skipped_drafts"]), ", ".join(r["skipped_drafts"])))
    if r.get("broken_bundles"):
        # 与 ⏭ 分开报：那是「还没核验」，这是「核验完了但 JSON 坏了、**没入库**」。
        # 混在一起会让 agent 去重做已经做过的核验。
        print("   ⛔ {} 篇 bundle 读不出/结构非法，**未入库**（修好 JSON 后重跑 finalize）: {}"
              .format(len(r["broken_bundles"]), ", ".join(r["broken_bundles"])))
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
    p_ing.add_argument("--month", default=None, type=_month_arg,
                       help="归档月份桶 YYYY-MM[-DD][-批次名]（默认当月）")
    p_ing.add_argument("--title", default=None, help="手动覆盖标题（单篇时；元数据解析用）")
    p_ing.add_argument("--force", action="store_true",
                       help="覆盖已 final 的 bundle（默认跳过——覆盖会丢弃 agent 已写的核验成果）")
    p_ing.set_defaults(func=cmd_ingest)

    p_fin = sub.add_parser("finalize", help="从 final bundle 重建当月手动精读札记")
    p_fin.add_argument("bundle", help="已 final 的 bundle.json 路径")
    p_fin.set_defaults(func=cmd_finalize)

    p_reg = sub.add_parser("regen", help="按现有 final bundle 重建某月（不新增论文）")
    p_reg.add_argument("--month", default=None, type=_month_arg,
                       help="月份桶 YYYY-MM[-DD][-批次名]（默认当月）")
    p_reg.set_defaults(func=cmd_regen)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
