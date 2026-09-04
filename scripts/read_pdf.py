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
import os
import sys
import time
from datetime import date
from pathlib import Path

try:                                    # Windows 无 fcntl：锁降级为不加锁（见 _RebuildLock）
    import fcntl
except ImportError:                     # pragma: no cover - 本仓库只在 macOS 跑
    fcntl = None

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

    # 研究画像按批动态补一段「本批阅读目的」：分块 LLM 手里只有一条静态画像（如「EHR 缺失
    # 机制」），凡不匹配的都判「与我研究无关、不必强行建立关联」——而那四篇恰是本批的全部
    # 目的（2026-09-03 景观地基文献 4 篇中 3 篇如此，且是 memory 记过一次修复后的复发；见
    # docs/bugs/2026-09-03-ingest-draft-systematic-errors.md 第 4 类）。ingest 是唯一知道
    # 「为什么读这批」的位置，故在这里注入，不改配置文件里的长期画像。
    research_interests = _research_interests_with_context(
        proc.research_interests, getattr(args, "context", "") or "")

    outs, failed = [], []
    for pdf_path in pdfs:
        try:
            r = ingest_pdf(
                pdf_path, notes_dir, month, llm,
                model=model, email=email,
                research_interests=research_interests,
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
    any_no_draft = False
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
        st = r["draft_status"]
        if st == "api_error":
            any_api_err = True
            print("   ⚠️ LLM API 无额度/鉴权失败：脚本草稿这一轨不可用。")
        elif st in _NO_DRAFT_STATUSES:
            any_no_draft = True
            print("   ⚠️ 脚本草稿这一轨缺失（{}）：{}".format(st, (r.get("draft_note") or "")[:300]))
        elif st == "degraded":
            print("   ⚠️ 草稿不完整（degraded）：{}".format((r.get("draft_note") or "")[:300]))
        elif r.get("draft_note"):
            print("   ℹ️ draft_note    : {}".format(r["draft_note"][:300]))
    if any_api_err or any_no_draft:
        why = ("API 没钱" if any_api_err else "汇总步失败/无可用块，脚本轨缺失")
        print("\n🔁 回退协议（{}）：不依赖脚本草稿，改用**两个 subagent 对抗生成**——".format(why))
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


# 「无脚本草稿」的三态：走回退协议（两个 subagent 对抗生成）。api_error 单列是因为文案不同。
_NO_DRAFT_STATUSES = ("synth_failed", "empty")
# 元数据「未经权威源确认」的两个来源：pdf-llm 是 LLM 从首页抽的，pdf-only 是零散兜底。
_UNCONFIRMED_META_SOURCES = ("pdf-only", "pdf-llm")


def _research_interests_with_context(base: str, context: str) -> str:
    """把 --context（本批阅读目的）拼到研究画像后面；context 为空原样返回。"""
    context = (context or "").strip()
    if not context:
        return base or ""
    return "{}\n\n【本批阅读目的】{}".format((base or "").rstrip(), context)


def _is_thin_metadata(r) -> bool:
    """回执行是否该报「元数据不全」。

    判据是**书目可用性**，不是抽取通道：作者齐全但 DOI/arXiv 皆空的 pdf-llm 记录，书目
    缺 DOI 与卷期页、dedup_key 退化成非 DOI 键，与「零作者」一样是残条——旧判据只看
    `pdf-only or 无作者`，这类记录静默过关（2026-09-03 arXiv 查询遇 SSL 中断后实测漏报）。
    """
    if r.get("skipped"):
        return False
    if r.get("meta_source") in _UNCONFIRMED_META_SOURCES:
        return True
    if not r.get("authors_n"):
        return True
    # 权威源（crossref-*/arxiv）必然带 DOI 或 arXiv id；两者皆空说明身份键会退化
    if not (r.get("doi") or r.get("arxiv_id")):
        return True
    return False


def _print_attention(outs, failed, title_ignored: str = ""):
    """把「必须有人看一眼」的事项汇总到输出最末。

    单篇时散在中间也看得见，21 篇一批时必被淹掉——今天正是这样漏掉一条「索引里已有同文」，
    白读了一篇几个月前已精读过的论文。title_ignored 同理：批量时被丢弃的 --title 若只在
    循环前 warning 一次，会落在同一个被淹掉的位置。
    """
    dups = [r for r in outs if r.get("duplicate")]
    thin = [r for r in outs if _is_thin_metadata(r)]
    bad_draft = [r for r in outs if not r.get("skipped")
                 and r.get("draft_status") not in (None, "ok")]
    skipped = [r for r in outs if r.get("skipped") == "final"]
    n = (len(dups) + len(thin) + len(bad_draft) + len(skipped) + len(failed)
         + (1 if title_ignored else 0))
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
        print("  · 元数据不全（来源 {}、作者 {} 位、DOI {}、arXiv {}）：{}".format(
            r.get("meta_source"), r.get("authors_n", 0),
            r.get("doi") or "无", r.get("arxiv_id") or "无", r["title"][:48]))
        for why in (r.get("meta_degraded") or [])[:4]:
            # 区分「查询失败导致的空」与「本来就没有」：有原因行的是前者，网络恢复后重跑即可补齐
            print("      原因：{}".format(str(why)[:160]))
    if thin:
        print("    → citekey 会退化成 anon*、bibliography 缺 DOI/卷期页、dedup_key 退化成非 DOI 键；"
              "有「原因」行的多为网络瞬断，网络恢复后**单独对那一篇**重跑 ingest（draft 不需 --force）"
              "或加 --title \"精确标题\"（--title 批量时不生效）；否则手工回查 Crossref 补正")
    for r in bad_draft:
        print("  · 脚本草稿 {}：{} —— {}".format(
            r.get("draft_status"), r["title"][:48], (r.get("draft_note") or "")[:200]))
    if bad_draft:
        print("    → degraded=有草稿但部分块失败（失败块页段亲读补齐）；"
              "synth_failed/empty/api_error=无草稿，走回退协议（两个 subagent 对抗生成）")
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


def _archived_keys(notes_dir: Path, month: str):
    """上一轮 sidecar 里已归档条目的 dedup_key 集合；sidecar 不存在/读不出返回 None。

    None = 「不知道上一轮有什么」，调用方据此**不做**止损判断（首次建月、坏 sidecar）——
    宁可放过也不要拿空集当"上一轮什么都没有"，那会让止损闸恒不触发。
    """
    from src.scholar._citekey_utils import recompute_entry_key
    sidecar = notes_dir / "科研札记_{}_手动精读.index.json".format(month)
    if not sidecar.exists():
        return None
    try:
        rows = (json.loads(sidecar.read_text(encoding="utf-8")) or {}).get("papers") or []
    except Exception:
        return None
    return {recompute_entry_key(r) for r in rows if isinstance(r, dict) and r.get("citekey")}


# ── 整月重建的并发防护（见 docs/bugs/2026-09-03-finalize-concurrency.md）────────
# 两道机制防的**不是同一件事**，缺任何一个都有缺口：
#   锁   —— 挡两个重建进程对撞（含跨月：收尾的 update_index 扫全部月份，按月加锁不够）；
#   重查 —— 挡「写 bundle 的那一方根本不持锁」的情况：本工作流里 final bundle 是
#           agent 用 Write 工具直接写的，它不走本脚本、不可能拿锁。
_REBUILD_LOCK_NAME = ".rebuild.lock"
_REBUILD_LOCK_TIMEOUT = 180.0        # 等锁上限（秒）：另一轮重建通常几秒到几十秒
_CONCURRENCY_RETRIES = 3             # 清单不稳时最多重来几轮
_RETRY_BACKOFF = 0.05                # 重来前的小退避（秒）×轮次：不退避的话三轮在
                                     # 毫秒级烧完，只要写入方比一轮重建快就必然耗尽


class _RebuildLock:
    """整月重建的排他文件锁（阻塞等待，有上限）。

    跨月共用**同一把**锁：`_rebuild_month` 收尾会调 `update_index()`，而它扫的是全部
    月份并重写 literature_index.json / INDEX.md / all_references.json——两个会话即使
    精读的是不同月份，全局索引仍然对撞（已由
    test_rebuild_month_rewrites_global_index_across_all_months 钉住）。

    **作用域仅限本脚本的整月重建**（说清楚，别当成"锁到索引层"）：同样重写那份全局
    索引的还有 ingest_notes / backfill_notes / notes_index / promote_identity_doi /
    realign_metadata_ts / book_notes 六个入口，它们都不持这把锁。后果有限——那些产物
    全是派生的、且走 `_atomic_write`，最坏是"陈旧但完整"的覆盖、下一轮自愈——但
    「跨月也不安全」这条运维提示在本次修复后对**那些入口**依然成立。真要根治得把锁
    上提到 notes_index.write_outputs 一侧，那是另一件事。

    等待而非直接失败：另一轮重建通常几秒到几十秒，让本轮排队后拿到**新鲜**的 bundle
    列表重建，比让 agent 收到一个失败回执再人工重跑要好。
    flock 随进程退出自动释放，不存在需要手动清理的残留（同 embed_store 的既有注释）；
    锁文件本身留着不删——删它反而会让两个进程各锁各的 inode，锁形同虚设。
    """

    def __init__(self, notes_dir: Path, timeout: float = None):
        self.path = Path(notes_dir) / _REBUILD_LOCK_NAME
        self.degraded = False        # True = 这一轮没真加上锁（fail-open），回执要说出来
        # 在**调用时**读模块常量，不写成默认参数——默认参数在函数定义时求值，之后
        # 改 _REBUILD_LOCK_TIMEOUT（测试里调小、排障时调大）全都不生效。
        self.timeout = _REBUILD_LOCK_TIMEOUT if timeout is None else timeout
        self._fd = None

    def acquire(self) -> bool:
        if fcntl is None:                        # Windows：不加锁，只靠提交前重查
            self.degraded = True
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 锁文件若是 FIFO，`open(..., "w")` 会**无限阻塞**且 timeout 包不住它
            # （压测抓到过挂死栈）。与 _collect_month_segments 对非普通文件的口径对齐：
            # 不是普通文件就 fail-open，别把整个 finalize 僵在这里。
            if self.path.exists() and not self.path.is_file():
                logger.warning("  ⚠️ 重建锁 {} 不是普通文件（目录/管道？）：本轮不加锁，"
                               "仅靠提交前重查兜底".format(self.path))
                self.degraded = True
                return True
            self._fd = open(str(self.path), "w")
        except OSError as e:
            logger.warning("  ⚠️ 打不开重建锁 {}（{}）：本轮不加锁，仅靠提交前重查兜底"
                           .format(self.path, e))
            self._fd = None
            self.degraded = True
            return True
        deadline = time.time() + self.timeout
        waited = False
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if waited:
                    logger.info("  ✅ 已拿到重建锁，继续（用的是等待后重新读的 bundle 列表）")
                return True
            except OSError:
                if time.time() >= deadline:
                    self._fd.close()
                    self._fd = None
                    return False
                if not waited:
                    logger.info("  ⏳ 另一轮整月重建正在进行，等待其完成（上限 {:.0f}s）……"
                                .format(self.timeout))
                    waited = True
                time.sleep(0.5)

    def release(self):
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fd.close()
            self._fd = None


def _stat_fp(path: Path):
    """单个文件的指纹 `(mtime_ns, size)`；不存在/读不到返回 None。"""
    try:
        st = Path(path).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _bundle_inventory(mdir: Path, suffix: str) -> dict:
    """当月 bundle 目录的现状指纹 `{文件名: (mtime_ns, size)}`。

    **刻意用 os.scandir 而不是复用采集阶段那次 `glob`**：重查的全部意义就是「重新问一次
    磁盘」，若两边共用同一条读取路径，那条路径本身的陈旧就对守卫完全隐形（守卫要能
    发现的恰恰是「我手上那份列表不对」）。同 双录记账 的道理。

    **只比路径集不够**，指纹必须带 mtime+size：本工作流的常态是 ingest 先落 draft、
    agent 后把**同一个文件**改写成 final，路径集合根本没变（实测：只比路径集 → 差集为空
    → 漏掉；带 mtime → 立刻发现）。
    """
    out = {}
    try:
        with os.scandir(str(mdir)) as it:
            for ent in it:
                if not ent.name.endswith(suffix):
                    continue
                # is_file()/stat() 都放进 per-entry 的 try：DirEntry.is_file() 只吞
                # FileNotFoundError，撞上**符号链接环**会抛 OSError(ELOOP)。放在 try 外面
                # 的话这个异常会落到最外层 except，整份 scandir 从那一条起**静默截断**
                # （实测 6 个好文件收到 0 个）→ 与采集侧（Path.is_file() 对 ELOOP 返回
                # False）口径分叉 → 指纹恒不相等 → 该月永久卡死，回执还谎称"另有会话在
                # 归档"（第 3 轮压测实证）。单条出错只跳过这一条，别连累整个目录。
                try:
                    if not ent.is_file():
                        continue
                    st = ent.stat()
                except OSError:              # 正被删/被换/链接环：跳过这一条即可
                    continue
                out[ent.name] = (st.st_mtime_ns, st.st_size)
    except (OSError, ValueError):            # 目录不存在（本月还没 ingest 过）
        pass
    return out


def _collect_month_segments(mdir: Path, suffix: str):
    """扫当月 bundle → `(segments, skipped, broken, consumed)`。

    `consumed` 是**本轮实际处理过的**那些文件的指纹，与 `_bundle_inventory()` 同形；
    提交前拿它跟磁盘现状比，就能回答「我这份列表是不是已经过时了」。
    每份文件在**读之前**取 stat：读完之后再取会把「我读的时候它正被改写」记成没变。
    """
    from src.scholar.pdf_ingest import load_bundle, segment_from_bundle

    segments, skipped, broken = [], [], []
    consumed = {}
    bundles = sorted(mdir.glob("*{}".format(suffix))) if mdir.exists() else []
    for bf in bundles:
        try:
            # is_file() 必须与 _bundle_inventory 的 scandir+is_file() 口径一致：不一致时
            # 一个名叫 <x>.paper.json 的**目录**会进 consumed 却进不了 disk，指纹恒不相等
            # → 三轮全判"并发"→ 该月从此永久归档不进，回执还教人"稍后重跑"（压测实证）。
            #
            # 但**必须记进 broken**：净删除闸的触发条件是 `(broken or skipped)`，只 continue
            # 的话非普通文件（同名目录、悬空符号链接）与 stat 失败的条目会从 consumed、disk、
            # broken 三处同时消失 → 闸门失明 → 那篇已归档论文在**绿回执 + exit 0** 下被
            # 整篇重写抹掉（第 2 轮审计实证，正是本文件最忌讳的失败模式）。
            # 只进 broken、不进 consumed：这样与重查侧的 disk 仍然对齐，不复发上面那个死锁。
            if not bf.is_file():
                logger.warning("  ⚠️ {} 不是普通文件（同名目录/管道/悬空软链？），"
                               "按「读不出」处理".format(bf.name))
                broken.append(bf.name)
                continue
            st = bf.stat()
            consumed[bf.name] = (st.st_mtime_ns, st.st_size)
        except OSError as e:
            logger.warning("  ⚠️ stat 失败 {}: {}（按「读不出」处理）".format(bf.name, e))
            broken.append(bf.name)
            continue
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
    return segments, skipped, broken, consumed


def _stale_inventory_detail(consumed: dict, disk: dict) -> str:
    """把「清单 vs 磁盘」的差异说成人话（新增 / 被改写 / 已消失），供日志点名。"""
    added = sorted(set(disk) - set(consumed))
    gone = sorted(set(consumed) - set(disk))
    changed = sorted(n for n in set(consumed) & set(disk) if consumed[n] != disk[n])
    bits = []
    if added:
        bits.append("新出现 {} 份（{}）".format(len(added), "、".join(added[:3])))
    if changed:
        bits.append("被改写 {} 份（{}）".format(len(changed), "、".join(changed[:3])))
    if gone:
        bits.append("已消失 {} 份（{}）".format(len(gone), "、".join(gone[:3])))
    return "；".join(bits) or "指纹不一致"


def _rebuild_month(notes_dir: Path, month: str, settings,
                   allow_removals: bool = False) -> dict:
    """从当月全部 final bundle 重建手动精读四件套 + 刷索引。

    allow_removals：跳过「净删除止损闸」。默认 False——整月重建是**整篇重写**，一份
    bundle 被拒收（结构非法 / 读不出 / verified_count=0）就等于把那篇已归档论文从
    md、references、sidecar、索引、书目、向量库里一起删掉。上一轮把回执改红、退出码
    改成 1 只解决了「你会知道出事了」，没解决「已经出事了」。所以在**动库之前**先比对：
    这一轮会不会净删掉上一轮已归档的条目？会就整月一字不动，让人先去修那份 JSON。
    确要删（真的不想要那篇了）加 --allow-removals。

    **并发**（见 docs/bugs/2026-09-03-finalize-concurrency.md）：整月重建是「读一份
    bundle 列表 → 整篇重写 md」，两步之间只要有人往当月新写/改写一份 final bundle，
    那篇就会被这一轮用陈旧列表**静默抹出**派生视图（bundle 本身还在盘上，数据不丢）。
    上面那道净删除闸拦不住它——并发新增的 bundle 在本轮眼里既不是 broken 也不是
    skipped，闸门整个分支都不触发。这里用两道机制补上：
      1) `_RebuildLock` 排他锁，让两个重建进程串行（含跨月的全局索引对撞）；
      2) **提交前重查**：拿「本轮实际处理过的清单」比对「提交前独立重读的磁盘现状」，
         不一致就用新列表重来（写 bundle 的 agent 不持锁，锁挡不住它）。
    """
    from src.scholar.pdf_ingest import BUNDLE_SUFFIX
    from src.scholar.notes import write_notes
    from src.scholar.notes_index import (update_index, write_outputs, existing_citekeys,
                                         existing_citekey_owners)

    # 已有索引的 citekey 全集：fallback citekey 生成时避开，防止新论文与库内重名。
    # 但要排除本月这份手动精读 md 自己的旧条目——否则每次 finalize/regen 整篇重写
    # 本月 md 时，本月论文的上一轮 citekey 会被当成「库内已占用」，被迫加消歧后缀，
    # 下一轮又因为后缀键才是「已占用」而改回原键，来回改名（citekey 抖动）。
    own_note_file = "科研札记_{}_手动精读.md".format(month)
    idx_path = notes_dir / INDEX_JSON
    mdir = notes_dir / "manual" / month
    proc = settings.processing

    def _locked_rebuild():
        """锁内主体：采集 → 守卫 → 重查 → 写盘 → 刷索引。
        两个 best-effort 同步**刻意留在锁外**（见下），这里只碰月度四件套与全局索引。
        """
        # 已有索引的 citekey 全集：fallback citekey 生成时避开，防止新论文与库内重名。
        # 但要排除本月这份手动精读 md 自己的旧条目——否则每次 finalize/regen 整篇重写
        # 本月 md 时，本月论文的上一轮 citekey 会被当成「库内已占用」，被迫加消歧后缀，
        # 下一轮又因为后缀键才是「已占用」而改回原键，来回改名（citekey 抖动）。
        # 放在锁内取：锁外取到的可能是另一轮重建正在改写的中间态。
        existing_ckeys = existing_citekeys(idx_path, exclude_note_files={own_note_file})
        # 键的占有者（citekey → dedup_key）：手动精读升级一篇已被 auto 浅读的论文时，让 keeper
        # **继承**干净基键而不是拿 `<基键>b`（见 notes.write_notes 的 existing_key_owners）。
        existing_owners = existing_citekey_owners(idx_path, exclude_note_files={own_note_file})

        res = None
        written_papers = 0          # **真正写进盘的那一轮**的篇数（≠ 循环结束时的 segments）
        segments, skipped, broken = [], [], []
        # 只有**通过了重查（一）**的那一轮，其 broken/skipped 才可信：在重查处夭折的那轮，
        # broken 里多半是"对方正写到一半"的半截 JSON，回执点名它 = 教人去修一份好文件
        # （第 2 轮审计实证）。None = 本次调用还没有任何一轮的清单被证实过。
        verified = None
        unstable = False
        for attempt in range(1, _CONCURRENCY_RETRIES + 1):
            segments, skipped, broken, consumed = _collect_month_segments(mdir, BUNDLE_SUFFIX)

            # ── 提交前重查（一）：采集期间磁盘变了吗？──────────────────────────────
            # **必须排在净删除闸之前**：另一会话正用 Write 工具原地改写某份已归档论文的
            # bundle 时，本轮会读到半截 JSON → 记成 broken → 闸门开火 → 直接 return，
            # 一轮都不重试，回执还教用户「去修一份根本没坏的 JSON」（审计实测）。
            # 先按并发重来，重来之后还 broken 才是真的坏了。
            disk = _bundle_inventory(mdir, BUNDLE_SUFFIX)
            if disk != consumed:
                unstable = True
                logger.warning("  ⏳ 采集期间当月 bundle 目录有变动（{}），第 {}/{} 轮，"
                               "改用新列表重建".format(_stale_inventory_detail(consumed, disk),
                                                     attempt, _CONCURRENCY_RETRIES))
                time.sleep(_RETRY_BACKOFF * attempt)   # 小退避：不退避时三轮在毫秒级烧完
                continue
            verified = (list(skipped), list(broken))   # 这一轮的清单经重查证实，可以进回执
            if not segments:
                # 无 final bundle：**不写 md**（整月不动），只刷索引——没有删除风险。
                logger.info("  {} 无 final bundle，跳过重建（草稿 {} 篇）".format(month, len(skipped)))
                # 仍刷一次索引（若该月手动 md 曾存在但现无 final，索引以磁盘为准）
                idx = update_index(notes_dir)
                write_outputs(idx, notes_dir)
                vs, vb = verified if verified else ([], [])
                # papers/md 取**写盘那一轮**的：前面某轮可能已经写出过一份 md，早退时
                # 报 0 篇、且不带 md 键，会与磁盘直接矛盾（回执报 0 而 md 里有货，
                # 还会让锁外的 topics 同步被跳过）——第 2 轮审计实证。
                out = {"month": month, "papers": written_papers,
                       "skipped_drafts": vs, "broken_bundles": vb, "index": idx,
                       "_index_fp": _stat_fp(idx_path)}
                if res is not None:
                    out["md"] = res["note_path"]
                    out["docx"] = res.get("docx_path")
                    out["sidecar_ok"] = res.get("sidecar_ok", True)
                if unstable:
                    # 前面某一轮写过盘/检出过并发，这一轮却一份 final 都没看见——多半读到的
                    # 又是陈旧列表。不能打绿回执 + exit 0（agent 收到绿的就不会重跑）。
                    logger.error("  ⚠️ 本轮一份 final bundle 都没看见，但此前检出过并发变动："
                                 "多半读到的是陈旧列表。请重跑 regen 确认。")
                    out["concurrent"] = True
                return out


            # ── 净删除止损闸（动库之前，见函数 docstring）──────────────────────────
            # 只在**确实有 bundle 被拒收**时才查：没有拒收的正常重建即使条目变少，也是人主动
            # 删了 bundle，不该拦。判据用 dedup_key 而非计数：一进一出时计数不变，但确实删了一篇。
            if (broken or skipped) and not allow_removals:
                from src.scholar.ingest import dedup_key as _seg_dedup_key
                prev_keys = _archived_keys(notes_dir, month)
                if prev_keys:
                    now_keys = {_seg_dedup_key(s.metadata) for s in segments}
                    missing = prev_keys - now_keys
                    if missing and res is None:
                        logger.error(
                            "  ⛔ 本轮重建会从 {} 的札记里**净删除 {} 篇已归档论文**（有 {} 份 bundle 被拒收）。\n"
                            "     整月一字未动。请先修好被拒收的 bundle JSON 再重跑；\n"
                            "     确要删除那些论文请加 --allow-removals。".format(
                                month, len(missing), len(broken) + len(skipped)))
                        idx = update_index(notes_dir)
                        vs, vb = verified if verified else ([], [])
                        return {"month": month, "papers": len(segments), "refused": True,
                                "removed_keys": sorted(missing),
                                "skipped_drafts": vs, "broken_bundles": vb, "index": idx}
                    if missing:
                        # **本次调用的前一轮已经写过盘**，且那一轮是数据完整的（闸当时没响）。
                        # 此刻某份 bundle 才坏掉——已写的 md 本身是对的，回滚它反而丢数据，
                        # 更不能谎称「整月一字未动」（回执会与磁盘现状矛盾，且 refused 分支
                        # 不刷 write_outputs，会让月度 md 与全局索引持久不一致——审计实测）。
                        # 收下已写的结果走正常收尾，broken 照报、退出码照样非 0。
                        logger.error("  ⛔ 已写盘之后又有 {} 份 bundle 变得读不出/非法：已写的内容"
                                     "是完整的，不回滚。修好这些 JSON 后重跑 finalize。"
                                     .format(len(broken) + len(skipped)))
                        break

            citekeys = _reuse_citekeys(notes_dir, month, segments)
            res = write_notes(
                segments, citekeys, out_dir=notes_dir,
                instruction=proc.notes_instruction,
                digest_title="科研札记 · {}（手动深度精读）".format(month),
                filename="科研札记_{}_手动精读".format(month),
                emit_docx=proc.notes_emit_docx, cjk_font=proc.notes_docx_cjk_font,
                fallback_citekeys=True, index_series="manual",
                existing_citekeys=existing_ckeys,
                existing_key_owners=existing_owners,
                # 沿用的是上一轮的兜底键，不是 Zotero 权威键，别在 sidecar 里冒充
                explicit_citekey_source="fallback")
            written_papers = len(segments)

            # ── 提交前重查（二）：写盘期间磁盘变了吗？────────────────────────────
            # 写 md/docx/references/sidecar 这段是本函数里最长的窗口之一，同样要盯。
            # 变了就再来一轮：md 已经写了但可能缺最新那篇，重写一轮即收敛。
            disk = _bundle_inventory(mdir, BUNDLE_SUFFIX)
            if disk == consumed:
                unstable = False
                break
            unstable = True
            logger.warning("  ⏳ 写盘期间当月 bundle 目录有变动（{}），第 {}/{} 轮，重写一轮收敛"
                           .format(_stale_inventory_detail(consumed, disk),
                                   attempt, _CONCURRENCY_RETRIES))
            time.sleep(_RETRY_BACKOFF * attempt)

        if res is None:
            # 采集始终没稳定过：一次都没写盘，整月一字未动（这是安全侧——照写就会抹掉
            # 那些没进列表的论文）。bundle 都在盘上，稍后重跑即可。
            logger.error("  ⛔ 当月 bundle 目录持续在变（{} 轮都没稳定），整月一字未动。"
                         "等另一个会话的归档结束后重跑 regen（bundle 都在盘上，数据没丢）。"
                         .format(_CONCURRENCY_RETRIES))
            vs, vb = verified if verified else ([], [])
            out = {"month": month, "papers": 0, "refused": True, "reason": "concurrent",
                   "skipped_drafts": vs, "broken_bundles": vb}
            if verified is None and broken:
                # 一轮都没通过重查：这份 broken 里多半是对方写到一半的半截 JSON，
                # 点名它等于教人去修一份好文件。单独一个键，回执里说清"不可信"。
                out["unverified_broken"] = sorted(broken)
            return out

        # 这份索引直接带给 _report_final 复用：全量重建要扫全部札记 md，跑两遍纯属白等
        idx = update_index(notes_dir)
        write_outputs(idx, notes_dir)
        # papers 取**写盘那一轮**的篇数（描述 md 里实际有几篇）；skipped/broken 取最后一轮
        # 的（描述磁盘当前状态，是用户要去修的东西）——两者来自不同轮次时必然已标 concurrent
        # 或有 broken，回执上不会被读成"一切正常"。
        vs, vb = verified if verified else ([], [])
        out = {"month": month, "papers": written_papers, "skipped_drafts": vs,
               "broken_bundles": vb,
               "md": res["note_path"], "docx": res.get("docx_path"), "index": idx,
               # 手动精读的 sidecar 是 _reuse_citekeys 的锚：写失败 = 下一轮 regen 键全被重算顶回
               "sidecar_ok": res.get("sidecar_ok", True),
               # 全局索引的指纹：锁外做 best-effort 同步前要重读比对，见 _rebuild_month 尾部
               "_index_fp": _stat_fp(idx_path)}
        if unstable:
            # 写完了，但最后一轮写盘期间磁盘又变了——md 可能缺最新那篇。不回滚（已写的
            # 内容本身是对的），改为**回执标红 + 退出码非 0**，让调用方知道要再跑一次。
            logger.error("  ⚠️ 归档已写盘，但期间当月 bundle 目录仍在变动：本轮 md 可能缺最新的"
                         "那一篇。请在另一个会话结束后重跑 regen 收敛（bundle 都在盘上）。")
            out["concurrent"] = True
        return out

    lock = _RebuildLock(notes_dir)
    if not lock.acquire():
        logger.error("  ⛔ 等重建锁超时（{:.0f}s）：另一轮整月重建仍在进行。整月一字未动，"
                     "稍后重跑 regen 即可（bundle 都在盘上，数据没丢）。"
                     .format(_REBUILD_LOCK_TIMEOUT))
        return {"month": month, "papers": 0, "refused": True, "reason": "locked",
                "skipped_drafts": [], "broken_bundles": []}
    try:
        out = _locked_rebuild()
    finally:
        lock.release()
    if lock.degraded:
        # fail-open 了：重查只看**当月** bundle 目录，挡不住跨月的全局索引对撞——
        # 锁唯一独有的那份保护本轮是缺席的，回执必须说出来，别让人以为串行保证还在。
        out["unlocked"] = True

    # ── 两个 best-effort 同步在**锁外**做 ──────────────────────────────────────
    # 它们本就声明「不影响归档结果」，却是本函数最长的两段：向量库嵌入是分钟级，
    # topics 合成是子进程、默认 timeout 2400s（40 分钟），而等锁上限只有 180s——
    # 放锁内的话，一次慢的概念页合成就能让此后半小时内**任何月份**的 finalize/regen
    # 全部撞 refused/locked（压测实证）。归档本身（md/sidecar/索引）已在锁内完成。
    idx = out.get("index")
    idx_fp_then = out.pop("_index_fp", None)
    if idx is not None and not out.get("refused"):
        # 锁一放开，另一轮重建就可能已经完成并刷新了全局索引。此时**我们手上的 idx 是
        # 陈旧的**：拿它去 sync_store 会把对方刚嵌入的 chunk 当成"库里多出来的"删掉
        # （压测实证：`+0 嵌入 / -1 删除`，被删的正是并发方那篇）。向量库的 0.5 骤缩闸
        # 拦不住——只少一篇远不到一半。指纹变了就跳过：对方那轮更新，它自己会同步。
        if idx_fp_then is not None and _stat_fp(idx_path) != idx_fp_then:
            logger.warning("  ⏭ 全局索引已被另一轮重建刷新，跳过本轮 best-effort 同步"
                           "（对方那轮更新，向量库/概念页由它负责；拿陈旧索引同步会删掉"
                           "对方刚嵌入的向量）")
            out["sync_skipped"] = True
        else:
            _sync_embedding_best_effort(notes_dir, idx, settings)
            if out.get("md"):
                _sync_topics_best_effort(notes_dir, out["md"])   # W6：接入 P2，见函数文档
    return out


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
    r = _rebuild_month(notes_dir, month, settings,
                       allow_removals=getattr(args, "allow_removals", False))
    _report_final(r, notes_dir)
    # 在 _rebuild_month 里「拒收」等价于「从已归档的 md 里删掉」：整月 md 被重写、索引重建、
    # 向量库同步，一篇已核验论文当场蒸发。绿回执 + exit 0 会让自动权限模式的 agent 直接
    # 往下走（同 audit_citekeys_vs_pmlr 的论证），所以这里必须非 0。
    # sidecar 写失败也退非 0：手动精读的 sidecar 是 _reuse_citekeys 的锚，丢了下一轮 regen 会把键全顶回
    return 1 if (r.get("broken_bundles") or r.get("refused") or r.get("concurrent")
                 or r.get("sidecar_ok") is False) else 0


def cmd_regen(args):
    settings = _load_settings(args.config)
    notes_dir = Path(settings.processing.notes_dir)
    month = args.month or _cur_month()
    r = _rebuild_month(notes_dir, month, settings,
                       allow_removals=getattr(args, "allow_removals", False))
    _report_final(r, notes_dir)
    # sidecar 写失败也退非 0：手动精读的 sidecar 是 _reuse_citekeys 的锚，丢了下一轮 regen 会把键全顶回
    return 1 if (r.get("broken_bundles") or r.get("refused") or r.get("concurrent")
                 or r.get("sidecar_ok") is False) else 0


def _report_final(r, notes_dir):
    idx = r.get("index")
    if idx is None:                       # 兜底：调用方没带索引时才重建
        from src.scholar.notes_index import update_index
        idx = update_index(notes_dir)
    collisions = idx.get("citekey_collisions", [])
    print("\n" + "=" * 66)
    if r.get("refused") and r.get("reason") in ("locked", "concurrent"):
        # 并发导致的拒绝：**没有**净删除清单可报，报成「会净删除 0 篇」会让人以为是数据问题
        why = ("等重建锁超时，另一轮整月重建仍在进行"
               if r.get("reason") == "locked" else
               "当月 bundle 目录持续在变（多半有另一个会话正在同月归档）")
        print("⛔ 手动精读归档 · {}：**整月未改动**（{}）".format(r["month"], why))
        print("   → 数据没丢：bundle 都在 manual/{}/ 下。等对方结束后重跑 regen 即可".format(
            r["month"]))
    elif r.get("refused"):
        print("⛔ 手动精读归档 · {}：**整月未改动**（本轮重建会净删除 {} 篇已归档论文）".format(
            r["month"], len(r.get("removed_keys") or [])))
        print("   净删除的条目（dedup_key）: {}".format(
            ", ".join((r.get("removed_keys") or [])[:5])))
        print("   → 先修好下面被拒收的 bundle JSON 再重跑；确要删除请加 --allow-removals")
    elif r.get("broken_bundles"):
        # 排在 concurrent 之前：两者并存时「有 bundle 没入库」更严重（要人去修 JSON），
        # 首行不能被降级成 ⚠️（审计实测过这个降级）。
        print("⛔ 手动精读归档 · {}：{} 篇（**有 bundle 未入库，见下**）".format(
            r["month"], r["papers"]))
    elif r.get("concurrent"):
        # 写盘成功但期间磁盘还在变：内容是对的，只是可能缺最新一篇——与 broken 分开报，
        # 混在一起会让人去修根本没坏的 JSON。
        print("⚠️ 手动精读归档 · {}：{} 篇（**写盘期间有并发归档，可能缺最新一篇**）".format(
            r["month"], r["papers"]))
    elif r.get("sidecar_ok") is False:
        # 首行不能打 ✅：退出码已是 1，回执首行还说成功会让人（和 agent）直接往下走。
        print("⚠️ 手动精读归档 · {}：{} 篇（**sidecar 写失败，见下**）".format(
            r["month"], r["papers"]))
    else:
        print("✅ 手动精读归档 · {}：{} 篇".format(r["month"], r["papers"]))
    if r.get("unverified_broken"):
        # 与「确实读不出」分开报：这些是重查没通过那一轮记下的，多半是对方写到一半的
        # 半截 JSON，此刻很可能已经好了。教人去修它就是把人引向一份好文件。
        print("   ℹ️ 另有 {} 份 bundle 在未通过重查的那一轮里读不出（多半是对方正在写，"
              "现在多半已经好了，**先别改**）：{}".format(
                  len(r["unverified_broken"]), ", ".join(r["unverified_broken"][:5])))
    if r.get("sync_skipped"):
        print("   ⏭ 向量库/概念页同步本轮跳过：全局索引已被另一轮重建刷新，由它负责同步")
    if r.get("unlocked"):
        print("   ⚠️ 本轮**未加重建锁**（锁文件打不开/平台不支持）：只有提交前重查在兜底，"
              "跨月的全局索引对撞挡不住。若同时在跑别的归档，建议串行重跑一次")
    if r.get("concurrent") and not r.get("refused"):
        # 与 broken 并存时首行让位给 broken，但这条提示本身不能丢
        print("   ⚠️ 写盘期间有并发归档，可能缺最新一篇 → 等对方结束后重跑 regen 收敛")
    if r.get("sidecar_ok") is False:
        print("   ⛔ 本月 sidecar（.index.json）**写失败**：阅读深度量尺无法回读，且下一轮 regen 会因"
              "找不到 sidecar 而不沿用 citekey（键被重算顶回）。修好后立刻重跑 regen。")
    if r.get("md"):
        print("   札记: {}".format(r["md"]))
    if r.get("docx"):
        print("   docx: {}".format(r["docx"]))
    if r.get("skipped_drafts"):
        _sd = r["skipped_drafts"]
        print("   ⏭ 跳过 {} 篇 draft（未 agent 核验）: {}{}".format(
            len(_sd), ", ".join(_sd[:10]),
            " …等共 {} 份".format(len(_sd)) if len(_sd) > 10 else ""))
    if r.get("broken_bundles"):
        # 与 ⏭ 分开报：那是「还没核验」，这是「核验完了但 JSON 坏了、**没入库**」。
        # 混在一起会让 agent 去重做已经做过的核验。
        _bb = r["broken_bundles"]
        print("   ⛔ {} 篇 bundle 读不出/结构非法，**未入库**（修好 JSON 后重跑 finalize）: {}{}"
              .format(len(_bb), ", ".join(_bb[:10]),
                      " …等共 {} 份".format(len(_bb)) if len(_bb) > 10 else ""))
    stale_inline = idx.get("stale_inline_citekeys") or []
    if stale_inline:
        # 挂在索引上、由这里报：update_index 每次都会算出它（持久状态），当场 notify 会让
        # 4 个 launchd job 每周每月刷同一条；而 finalize 是**有 agent 在看回执**的入口。
        print("   ⚠️ {} 条 duplicate 的行内 citekey 与其 keeper 不一致（抄引用会引错论文）：{}{}".format(
            len(stale_inline), "；".join(stale_inline[:3]),
            " …等共 {} 条".format(len(stale_inline)) if len(stale_inline) > 3 else ""))
        print("      → 修复：PYTHONPATH=. python scripts/notes_index.py --fix-inline-citekeys（默认 dry-run）")
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
    p_ing.add_argument("--context", default="",
                       help="本批阅读目的（拼进研究画像，只影响「对我研究的联想」一节；"
                            "如 \"景观-流形-缺失：本批为 A 层地基文献\"）")
    p_ing.add_argument("--force", action="store_true",
                       help="覆盖已 final 的 bundle（默认跳过——覆盖会丢弃 agent 已写的核验成果）")
    p_ing.set_defaults(func=cmd_ingest)

    p_fin = sub.add_parser("finalize", help="从 final bundle 重建当月手动精读札记")
    p_fin.add_argument("bundle", help="已 final 的 bundle.json 路径")
    p_fin.add_argument("--allow-removals", action="store_true",
                       help="允许本轮重建净删除已归档论文（默认拒绝：一份 bundle 被拒收就等于把那篇从 md/索引/书目/向量库一起删掉）")
    p_fin.set_defaults(func=cmd_finalize)

    p_reg = sub.add_parser("regen", help="按现有 final bundle 重建某月（不新增论文）")
    p_reg.add_argument("--month", default=None, type=_month_arg,
                       help="月份桶 YYYY-MM[-DD][-批次名]（默认当月）")
    p_reg.add_argument("--allow-removals", action="store_true",
                       help="允许本轮重建净删除已归档论文（默认拒绝：一份 bundle 被拒收就等于把那篇从 md/索引/书目/向量库一起删掉）")
    p_reg.set_defaults(func=cmd_regen)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
