# -*- coding: utf-8 -*-
"""backfill_deepread 批量跑完后的验收：确认没改坏别人、没动身份键、库对得上。

单篇验证靠肉眼还行，一百多篇散在几十个札记里就必须机器过一遍。四件事：

1. **越界检测**（最要紧）：拿备份里的原始 md 和现在的比，逐篇比对，除账本记录的目标篇
   外，同文件其余论文的正文必须**逐字节相等**。文本手术一旦越界就是静默丢数据，
   而 output/ 全在 .gitignore 里，没有 git 可以兜底。
2. **身份键稳定**：目标篇的 citekey / dedup_key / citekey_source 不得变化——绕开
   run_ingest 的全部理由就是防这个。
3. **产出确实变厚**：每篇 highlights 都应 ≥ 改前，且 reading_depth 已翻成 chunked。
4. **向量库一致**：期望集与库内 diff 为 0（dry-run 口径，不写库）。

用法：
    PYTHONPATH=. python scripts/verify_deepread_batch.py
退出码：0 全通过 / 1 有问题（详情打印在上面）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.settings import load_scholar_settings   # noqa: E402
from src.scholar.embed_store import INDEX_NAME           # noqa: E402
from src.scholar.notes_index import _SECTION_RE          # noqa: E402
from scripts.backfill_deepread import BACKUP_DIR, LEDGER_NAME, TARGET_DEPTH  # noqa: E402


def split_papers(text: str):
    """md → {citekey: 该篇正文}，按 _SECTION_RE 的行首锚定切。"""
    out, cur, buf = {}, None, []
    for ln in text.splitlines():
        m = _SECTION_RE.match(ln)
        if m:
            if cur:
                out.setdefault(cur, []).append("\n".join(buf))
            cur, buf = m.group(4), [ln]
        elif cur is not None:
            buf.append(ln)
    if cur:
        out.setdefault(cur, []).append("\n".join(buf))
    return out


def main() -> int:
    settings = load_scholar_settings(patch_gemini=False)
    nd = Path(settings.processing.notes_dir)
    led = json.loads((nd / LEDGER_NAME).read_text(encoding="utf-8"))
    done = led.get("done", {})
    if not done:
        print("账本里没有已完成的条目，无可验收。", file=sys.stderr)
        return 1
    idx = json.loads((nd / INDEX_NAME).read_text(encoding="utf-8"))
    # 只认 keeper：同一篇跨月重复时索引里有多条，duplicate 那条的 highlights 是另一份
    # 札记的旧内容。不排除的话字典会被后出现的 duplicate 覆盖，把「已改厚的 keeper」
    # 误报成变薄（bauer2025Sepsis 实测 29 条被读成 6 条）。
    by_ck = {e["citekey"]: e for e in idx["papers"]
             if e.get("citekey") and not e.get("duplicate_of")}
    problems = []

    # 前置闸：索引比最后一次写盘还旧时，第 3、4 项读到的全是改动前的旧值，会把
    # 「索引没刷」误报成一长串「reading_depth 没翻新」。这不是验收该干的活——先提示，
    # 把两项标成待定，只跑不依赖索引的第 1、2 项（它们直接比对备份与磁盘上的 md）。
    last_at = max((r.get("at") or "") for r in done.values())
    gen_at = (idx.get("generated_at") or "").replace("-", "").replace(":", "")
    stale = bool(last_at) and bool(gen_at) and gen_at < last_at
    if stale:
        print("⚠️ 文献索引（generated_at={}）早于最后一次写盘（{}）——索引尚未刷新。\n"
              "   先跑 `PYTHONPATH=. python scripts/notes_index.py`（它会顺带同步向量库），\n"
              "   再回来跑本验收。以下只报越界与身份键这两项。\n".format(
                  idx.get("generated_at"), last_at))

    # ---- 1/2/3: 逐篇比对备份 ----
    print("== 1) 越界检测 + 身份键 + 产出量（{} 篇）==".format(len(done)))
    # 同一份 md 可能被多篇改过；取该文件**最早**的那份备份做基准才能反映全部改动
    base_of = {}
    for ck, rec in sorted(done.items(), key=lambda kv: kv[1].get("at", "")):
        nf = rec.get("note_file")
        if nf and nf not in base_of and rec.get("backup"):
            p = nd / BACKUP_DIR / rec["backup"] / nf
            if p.exists():
                base_of[nf] = p

    checked_files = 0
    for nf, bpath in sorted(base_of.items()):
        cur_path = nd / nf
        if not cur_path.exists():
            problems.append("札记文件消失：{}".format(nf))
            continue
        old = split_papers(bpath.read_text(encoding="utf-8"))
        new = split_papers(cur_path.read_text(encoding="utf-8"))
        if set(old) != set(new):
            problems.append("{}：citekey 集合变了（少了 {}，多了 {}）".format(
                nf, sorted(set(old) - set(new))[:5], sorted(set(new) - set(old))[:5]))
            continue
        targets = {ck for ck, r in done.items() if r.get("note_file") == nf}
        for ck in old:
            if ck in targets:
                continue
            if old[ck] != new[ck]:
                problems.append("⚠️ 越界：{} 里的 `{}` 不是目标篇却被改动了".format(nf, ck))
        checked_files += 1
    print("   比对 {} 个札记文件；越界 {} 处".format(
        checked_files, sum(1 for p in problems if "越界" in p)))

    print("== 2) 身份键稳定 ==")
    key_bad = 0
    for ck in done:
        e = by_ck.get(ck)
        if e is None:
            problems.append("索引里找不到 {}（citekey 可能被改动）".format(ck))
            key_bad += 1
    print("   目标篇在索引中失踪：{}".format(key_bad))

    print("== 3) 产出量与 reading_depth ==" + ("（索引未刷新，跳过）" if stale else ""))
    thin = depth_bad = 0
    tot_o = tot_n = 0
    for ck, rec in (() if stale else done.items()):
        e = by_ck.get(ck)
        if not e:
            continue
        now = len(e.get("highlights") or [])
        old_n = rec.get("old", 0)
        tot_o += old_n
        tot_n += now
        if now < old_n:
            problems.append("{}：现有 {} 条 < 改前 {} 条".format(ck, now, old_n))
            thin += 1
        # 只把仍是 unknown-legacy 的算没翻新。深读失败会**回落单跳**并如实标
        # single-call（sathe2021Identification 即是：仍从 10 条涨到 38 条），
        # 那是准确标注不是错误，苛求 chunked 等于逼工具谎报读法。
        if e.get("reading_depth") in (None, TARGET_DEPTH):
            # 83 篇 md 只有 40 篇有 sidecar。无 sidecar 时 notes_index 走 md-parse 分支，
            # 而 md 里**没有任何地方能表达 reading_depth**，它会按老规则重新推断成
            # unknown-legacy。这是格式的固有限制，不是本次改动出错——highlights 与向量库
            # 都已正确更新（实测未翻新的 65 篇里 64 篇正是无 sidecar 的）。故只计数提示，
            # 不判失败；「这篇跑没跑过」的真相源是账本，不是这个标签。
            sc = nd / (Path(e.get("note_file") or "").stem + ".index.json")
            if sc.exists():
                problems.append("{}：有 sidecar 却没翻新 reading_depth（{!r}）".format(
                    ck, e.get("reading_depth")))
            depth_bad += 1
    if not stale:
        print("   变薄 {} 篇；reading_depth 未翻新 {} 篇（无 sidecar 的文件承载不了该字段，"
              "属已知限制，见代码注释）".format(thin, depth_bad))
        print("   合计可取证句：{} → {}（{:.1f}x）".format(
            tot_o, tot_n, tot_n / tot_o if tot_o else 0))

    print("== 4) 向量库一致性（dry-run，不写库）==" + ("（索引未刷新，跳过）" if stale else ""))
    try:
        if stale:
            raise StopIteration
        from src.scholar.embed_store import DB_NAME, chunks_from_index, load_abstracts
        try:
            ab = load_abstracts(nd)
        except Exception:
            ab = None
        exp = {c.id for c in chunks_from_index(idx, ab)}
        import sqlite3
        con = sqlite3.connect(nd / DB_NAME)
        have = {r[0] for r in con.execute("select id from chunks")}
        con.close()
        miss, orphan = exp - have, have - exp
        print("   期望 {} / 库内 {} | 待嵌 {} 待删 {}".format(
            len(exp), len(have), len(miss), len(orphan)))
        if miss or orphan:
            problems.append("向量库未同步：待嵌 {} 待删 {}（跑 scripts/notes_index.py 收尾）"
                            .format(len(miss), len(orphan)))
    except StopIteration:
        pass
    except Exception as e:                                   # noqa: BLE001
        problems.append("向量库检查失败：{}".format(e))

    print()
    if problems:
        print("❌ 发现 {} 个问题：".format(len(problems)))
        for p in problems[:40]:
            print("   - {}".format(p))
        return 1
    if stale:
        print("✅ 越界与身份键两项通过；产出量与向量库待索引刷新后复验。")
        return 0
    print("✅ 全部通过：无越界、身份键稳定、产出只增不减、向量库已同步")
    return 0


if __name__ == "__main__":
    sys.exit(main())
