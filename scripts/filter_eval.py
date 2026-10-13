#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""筛选召回评测集（金标准）工具：登记表、盲标表、gold 构造、统计（WO-6），评测执行与报告（WO-7）。
设计见 docs/decisions/trust_hardening_design_2026-10-13.md §5。

典型流程（1-7 步不调 LLM、不联网；run 会真调 LLM，须在金标准完成并登记之后才跑）：
  1. registry-export --kind batches        导出 manual 批次登记表，用户填 core/side/mixed
  2. registry-import --kind batches        校验并盖登记时间戳；有 mixed 批次则：
     registry-export --kind papers → 用户填 → registry-import --kind papers
  3. label-export                          导出 H-stream 盲标表（用户标 应收/边缘/无关）
  4. label-import --date YYYY-MM-DD        回收盲标表
  5. （库外 P1 摘要由联网步骤补进 p1_out_abstracts.json；build-gold 会列出缺口）
  6. build-gold                            产出 gold_v{N}.jsonl / .meta.json（登记缺失则拒绝）
  7. stats                                 打印各层规模
  8. run --gold v1 [--smoke]               复用生产 _filter_by_llm 跑评测（调 LLM！先 --smoke 看链路）
  9. report [RUN_ID] [--vs BASE_RUN_ID]    渲染报告 / 与另一次运行配对比较

所有产物落在 <notes_dir>/_eval/filter/（gitignore 数据目录；稿件引用清单属未发表信息，不进 git）。
"""
import argparse
import csv
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import filter_eval as FE                                  # noqa: E402
from src.scholar.paths import repo_path                                    # noqa: E402

DEFAULT_BIBS = {
    "arxiv": "~/Desktop/Lab/P4/manuscript/arxiv/bibliography.bib",
    "ieee": "~/Desktop/Lab/P4/manuscript/ieee/references.bib",
}


def _paths(args):
    notes = repo_path(args.notes_dir)
    digest = repo_path(args.digest_dir)
    edir = FE.eval_dir(notes)
    return notes, digest, edir


def _read_bibs(args):
    out = {}
    for name, dflt in DEFAULT_BIBS.items():
        p = Path(getattr(args, "bib_" + name) or dflt).expanduser()
        if p.exists():
            out[name] = p.read_text(encoding="utf-8")
        else:
            print("警告：找不到 bib（{}）：{}".format(name, p), file=sys.stderr)
    return out


def _snapshot(notes, digest, bib_paths, extra_files):
    """输入快照：关键文件的 sha256 + 目录清单摘要，写进 meta 以便复现与审计。"""
    snap = {}
    for tag, p in extra_files.items():
        snap[tag] = {"path": str(p), "sha256": FE._sha256_file(p)}
    for tag, d, pat in (("excluded_dir", digest / "_archive" / "excluded", "*.json"),
                        ("digest_dir", digest, "digest_*[0-9].json"),
                        ("manual_dir", notes / "manual", "*/*.paper.json")):
        listing = "\n".join("{}:{}".format(f.relative_to(d), f.stat().st_size) for f in sorted(d.glob(pat)))
        snap[tag] = {"n_files": len(listing.splitlines()), "listing_sha256": FE._sha256_bytes(listing.encode())}
    return snap


# ---------------- 子命令 ----------------

def cmd_registry_export(args):
    notes, _, edir = _paths(args)
    FE.assert_ignored(edir)
    manual = FE.load_manual_bundles(notes / "manual")
    p = FE.registry_export(edir, args.version, manual, args.kind)
    print("已导出（待填 class 列：{}）：{}".format("core/side/mixed" if args.kind == "batches" else "core/side", p))
    return 0


def cmd_registry_import(args):
    notes, _, edir = _paths(args)
    manual = FE.load_manual_bundles(notes / "manual")
    try:
        st = FE.registry_import(edir, args.version, manual, args.kind)
    except (ValueError, RuntimeError) as e:
        print("登记导入失败：{}".format(e), file=sys.stderr)
        return 2
    if args.kind == "batches":
        c = Counter(st["batches"].values())
        print("批次登记已盖章 {}：{}".format(st["batches_registered_at"], dict(c)))
        if c.get("mixed"):
            print("存在 mixed 批次：请再 registry-export --kind papers 并填写后导入")
    else:
        print("按篇登记已盖章 {}：{} 篇".format(st["papers_registered_at"], len(st["papers"])))
    return 0


def _stream_inputs(args, notes, digest):
    lib = FE.load_library(notes / "literature_index.json")
    manual = FE.load_manual_bundles(notes / "manual")
    stream = FE.load_stream_decisions(digest / "_archive" / "excluded", digest)
    return lib, manual, stream


def cmd_label_export(args):
    notes, digest, edir = _paths(args)
    FE.assert_ignored(edir)
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    sheet, keyf = edir / "label_sheet_{}.csv".format(date), edir / "label_key_{}.json".format(date)
    if (sheet.exists() or keyf.exists()) and not args.force:
        print("已存在 {}；重导会覆盖用户已填内容，确需如此加 --force".format(sheet), file=sys.stderr)
        return 2
    lib, manual, stream = _stream_inputs(args, notes, digest)
    drafts, _ = FE.build_p1(_read_bibs(args), lib)
    known = {d["dedup_key"] for d in drafts} | {m["dedup_key"] for m in manual}
    pool = [c for c in stream if c["dedup_key"] not in known and c["abstract"].strip()]
    sample, plan = FE.sample_stream(pool, seed=args.seed)
    rows, key = FE.label_sheet_rows(sample, seed=args.seed)
    plan["pool_excluded_known"] = sum(1 for c in stream if c["dedup_key"] in known)
    plan["pool_excluded_no_abstract"] = sum(1 for c in stream if not c["abstract"].strip())
    FE.write_label_sheet(sheet, rows)
    keyf.write_text(json.dumps({"exported_at": datetime.now().isoformat(timespec="seconds"),
                                "seed": args.seed, "plan": plan, "key": key},
                               ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print("盲标表 {} 条 → {}\n钥匙（含 LLM 判定，标完之前别打开）→ {}".format(len(rows), sheet, keyf))
    print("抽样计划：{}".format(json.dumps(plan, ensure_ascii=False)))
    return 0


def cmd_label_import(args):
    _, _, edir = _paths(args)
    date = args.date
    sheet, keyf = edir / "label_sheet_{}.csv".format(date), edir / "label_key_{}.json".format(date)
    key = json.loads(keyf.read_text(encoding="utf-8"))["key"]
    try:
        labs = FE.import_labels(sheet, key, allow_partial=args.allow_partial)
    except ValueError as e:
        print("回收失败：{}".format(e), file=sys.stderr)
        return 2
    out = edir / "labels_{}.csv".format(date)
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(("row_id", "item_id", "label", "comment"))
        for r in labs:
            w.writerow((r["row_id"], r["item_id"], r["label"], r["comment"]))
    print("已回收 {} 条 → {}（{}）".format(len(labs), out, dict(Counter(r["label"] for r in labs))))
    return 0


def _latest(edir, pattern):
    fs = sorted(edir.glob(pattern))
    return fs[-1] if fs else None


def cmd_build_gold(args):
    notes, digest, edir = _paths(args)
    FE.assert_ignored(edir)
    paths = FE.registry_paths(edir, args.version)
    state = FE._load_state(paths["state"])
    bibs = _read_bibs(args)
    idx, absf = notes / "literature_index.json", notes / "abstracts.json"
    lib, manual, stream = _stream_inputs(args, notes, digest)
    stable_f = digest / "_archive" / "misc" / "audit_stable_excludes_20260808.json"

    labels = key = None
    labf = Path(args.labels) if args.labels else _latest(edir, "labels_*.csv")
    if labf and labf.exists():
        kf = edir / ("label_key_" + labf.stem.split("labels_")[1] + ".json")
        key = json.loads(kf.read_text(encoding="utf-8"))
        labels = [{"item_id": r["item_id"], "label": r["label"], "comment": r["comment"]}
                  for r in FE._read_csv(labf)]
    p1f = edir / "p1_out_abstracts.json"
    p1_abs = json.loads(p1f.read_text(encoding="utf-8")) if p1f.exists() else {}
    f_over = None
    if args.f_override:
        f_over = [json.loads(l) for l in Path(args.f_override).read_text(encoding="utf-8").splitlines() if l.strip()]
    def _keys(f):
        if not f:
            return set()
        return {l.strip() for l in Path(f).read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.startswith("#")}
    excl, incl = _keys(args.bib_exclude), _keys(args.bib_include)

    try:
        items, stats, review = FE.build_gold(
            lib=lib, manual=manual, abstracts=FE.load_abstract_cache(absf), bibs=bibs, reg_state=state,
            stream=stream, stable=FE.load_stable_audit(stable_f), p1_abstracts=p1_abs, labels=labels,
            label_key=key, f_override=f_over, bib_exclude=excl, bib_include=incl, seed=args.seed)
    except RuntimeError as e:
        print("拒绝构造 gold：{}".format(e), file=sys.stderr)
        return 2

    blob = FE.gold_bytes(items)
    sha = FE._sha256_bytes(blob)
    gp = edir / "gold_v{}.jsonl".format(args.version)
    mp = edir / "gold_v{}.meta.json".format(args.version)
    old_meta = {}
    if mp.exists():
        try:
            old_meta = json.loads(mp.read_text(encoding="utf-8"))
        except ValueError:
            pass
    if gp.exists() and FE._sha256_bytes(gp.read_bytes()) != sha:
        print("v{} 已存在且内容不同；改登记/改输入须新开版本（--version {}）".format(
            args.version, args.version + 1), file=sys.stderr)
        return 2
    edir.mkdir(parents=True, exist_ok=True)
    gp.write_bytes(blob)
    FE._write_csv(edir / "p1_review_v{}.csv".format(args.version),
                  ("source", "bib_key", "type", "keep", "reason", "stratum", "match_by", "title"),
                  [(r["source"], r["bib_key"], r["type"], r["keep"], r["reason"], r["stratum"],
                    r["match_by"], r["title"]) for r in review])
    todo = stats.pop("p1_abstract_todo")
    (edir / "p1_abstract_todo_v{}.json".format(args.version)).write_text(
        json.dumps(todo, ensure_ascii=False, indent=1), encoding="utf-8")
    meta = {
        "gold_version": "v{}".format(args.version), "gold_sha256": sha, "gold_sha8": sha[:8],
        "created_at": old_meta.get("created_at") or datetime.now().isoformat(timespec="seconds"),
        "params": {"seed": args.seed, "test_fraction_pct": FE.TEST_FRACTION_PCT,
                   "abstract_max_chars": FE.ABSTRACT_MAX_CHARS, "bib_exclude": sorted(excl), "bib_include": sorted(incl),
                   "labels_file": str(labf) if labels else None, "f_override_file": args.f_override},
        "registry": {"batches_registered_at": state.get("batches_registered_at"),
                     "papers_registered_at": state.get("papers_registered_at"),
                     "batches_sha256": state.get("batches_sha256"), "papers_sha256": state.get("papers_sha256"),
                     "classes": dict(Counter(state.get("batches", {}).values()))},
        "counts": FE.gold_counts(items), "build_stats": stats,
        "h_sampling_plan": key["plan"] if key else None,
        "snapshot": _snapshot(notes, digest, bibs,
                              {"literature_index": idx, "abstracts": absf, "stable_audit": stable_f,
                               **{"bib_" + k: Path(DEFAULT_BIBS[k]).expanduser() for k in bibs}}),
    }
    mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print("gold v{}（sha8={}）{} 条 → {}".format(args.version, sha[:8], len(items), gp))
    if todo:
        print("库外 P1 缺摘要 {} 条，见 p1_abstract_todo_v{}.json（联网补抓后写入 p1_out_abstracts.json 再重跑）".format(
            len(todo), args.version))
    return 0


def cmd_stats(args):
    _, _, edir = _paths(args)
    gp = edir / "gold_v{}.jsonl".format(args.version)
    items = [json.loads(l) for l in gp.read_text(encoding="utf-8").splitlines() if l.strip()]
    counts = FE.gold_counts(items)
    print("gold v{}  共 {} 条".format(args.version, len(items)))
    print("{:<11}{:>6}{:>6}{:>6}{:>10}".format("层", "n", "dev", "test", "无摘要"))
    for s in FE.STRATA:
        c = counts[s]
        print("{:<11}{:>6}{:>6}{:>6}{:>10}".format(s, c["n"], c["dev"], c["test"], c["no_abstract"]))
    h = [i for i in items if i["stratum"] == "H-stream"]
    if h:
        print("H-stream 人判分布：{}".format(dict(Counter(i["label"] for i in h))))
    mp = edir / "gold_v{}.meta.json".format(args.version)
    if mp.exists():
        m = json.loads(mp.read_text(encoding="utf-8"))
        print("sha8={}  批次登记时间={}".format(m["gold_sha8"], m["registry"]["batches_registered_at"]))
    return 0


def _gold_version(args):
    g = getattr(args, "gold", None)
    return int(str(g).lstrip("vV")) if g else args.version


def _runs_dir(edir):
    return edir / "runs"


def _load_run(edir, ref):
    """ref 为 run_id 或 json 路径；缺省取 runs/ 里最新一份。"""
    rd = _runs_dir(edir)
    if ref:
        p = Path(ref)
        p = p if p.exists() else rd / (ref if ref.endswith(".json") else ref + ".json")
    else:
        fs = sorted(rd.glob("run_*.json"))
        if not fs:
            raise ValueError("{} 里没有运行记录".format(rd))
        p = fs[-1]
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_run(args):
    """评测执行（调 LLM）。批数 ≈ ceil(条目数/20) × 重复次数；每遍结束落一次断点。"""
    notes, _, edir = _paths(args)
    FE.assert_ignored(edir)
    ver = _gold_version(args)
    try:
        gitems, meta, gsha = FE.load_gold(edir, ver)
        FE.check_run_preconditions(meta, gsha, gitems)
        strata = [s for s in args.strata.split(",") if s] if args.strata else None
        items, sel = FE.select_items(gitems, split=args.split, strata=strata, smoke=args.smoke, seed=args.seed)
    except ValueError as e:
        print("拒绝运行：{}".format(e), file=sys.stderr)
        return 2
    if not items:
        print("选不出任何条目（检查 --strata / --split）", file=sys.stderr)
        return 2
    repeats = args.repeats if args.repeats is not None else (1 if args.smoke else FE.DEFAULT_REPEATS)

    from src.scholar.schema import ScholarSettings
    from src.scholar.workflow import FILTER_BATCH_SIZE, ScholarWorkflow
    cfg_path = repo_path(args.config)
    settings = ScholarSettings.from_env_file(cfg_path) if cfg_path.exists() else ScholarSettings()
    wf = ScholarWorkflow(settings)
    lib = FE.load_library(notes / "literature_index.json")
    cit_by_dedup = {e["dedup_key"]: e["citekey"] for e in lib if e.get("dedup_key") and e.get("citekey")}

    started = datetime.now()
    run_id = "run_{}_{}{}".format(started.strftime("%Y%m%d_%H%M%S"), meta["gold_sha8"], "_smoke" if args.smoke else "")
    config = {"gold_version": "gold-v{}@{}".format(ver, meta["gold_sha8"]), "split": args.split,
              "strata": list(strata or FE.DEFAULT_RUN_STRATA), "smoke": args.smoke, "repeats": repeats,
              "seed": args.seed, "abstract_mode": args.abstract_mode, "rag_on": args.rag == "on",
              "batch_size": FILTER_BATCH_SIZE, "prompt_fingerprint": wf._get_filter_prompt_version(),
              "requested_model": settings.llm.filter_model or settings.llm.model,
              "main_provider": settings.llm.provider}
    n_batches = -(-len(items) // FILTER_BATCH_SIZE) * repeats
    print("{}：{} 条 × {} 遍 ≈ {} 批 LLM 调用（rag={}, 摘要={}）".format(
        run_id, len(items), repeats, n_batches, args.rag, args.abstract_mode))
    out = _runs_dir(edir) / (run_id + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)

    def save(reps, status, finished=None):
        rec = FE.assemble_run(run_id, config, sel, items, reps, status=status,
                              started_at=started.isoformat(timespec="seconds"), finished_at=finished)
        rec["llm_batches_ok"] = sum(wf._filter_served_models.values())
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(out)
        return rec

    t0 = time.time()
    try:
        reps = FE.run_eval(wf, items, repeats=repeats, seed=args.seed, abstract_mode=args.abstract_mode,
                           rag_on=args.rag == "on", cit_by_dedup=cit_by_dedup,
                           on_repeat=lambda r, rs: (save(rs, "partial"),
                                                    print("  第 {}/{} 遍完成（{:.0f}s）".format(r + 1, repeats, time.time() - t0))))
    finally:
        wf.close_rag_resources()
    rec = save(reps, "complete", datetime.now().isoformat(timespec="seconds"))
    print("已写入 {}\n".format(out))
    print(FE.format_report(rec))
    return 0


def cmd_report(args):
    _, _, edir = _paths(args)
    try:
        run = _load_run(edir, args.run)
        base = _load_run(edir, args.vs) if args.vs else None
        if args.show_verdicts:
            gitems, _, _ = FE.load_gold(edir, int(run["config"]["gold_version"].split("-v")[1].split("@")[0]))
            if not any(i["stratum"] == "H-stream" and i.get("label") for i in gitems):
                print("gold 里没有已回收标签的 H-stream：盲标完成前不得 --show-verdicts", file=sys.stderr)
                return 2
    except (ValueError, OSError) as e:
        print("report 失败：{}".format(e), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(run["summary"], ensure_ascii=False, indent=1, sort_keys=True))
    else:
        print(FE.format_report(run, baseline=base, show_verdicts=args.show_verdicts))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--digest-dir", default="output/scholar_digest")
    ap.add_argument("--version", type=int, default=1, help="gold / 登记表版本号")
    ap.add_argument("--seed", type=int, default=FE.SEED)
    ap.add_argument("--bib-arxiv", default=None)
    ap.add_argument("--bib-ieee", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("registry-export", "registry-import"):
        p = sub.add_parser(name)
        p.add_argument("--kind", choices=("batches", "papers"), required=True)
    p = sub.add_parser("label-export")
    p.add_argument("--date", default=None)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("label-import")
    p.add_argument("--date", required=True)
    p.add_argument("--allow-partial", action="store_true")
    p = sub.add_parser("build-gold")
    p.add_argument("--labels", default=None, help="回收后的 labels_*.csv；缺省取最新一份")
    p.add_argument("--f-override", default=None, help="F-override 条目 jsonl（dedup_key,title,abstract…）")
    p.add_argument("--bib-exclude", default=None, help="追加剔除的 bib citekey 清单（一行一个）")
    p.add_argument("--bib-include", default=None, help="追加纳入的 bib citekey 清单（捞回被默认规则剔掉的）")
    sub.add_parser("stats")
    p = sub.add_parser("run", help="评测执行（调 LLM；复用生产 _filter_by_llm）")
    p.add_argument("--gold", default=None, help="gold 版本，如 v1（缺省取 --version）")
    p.add_argument("--split", choices=("test", "dev", "all"), default="test", help="只作用于 M-core/M-side")
    p.add_argument("--strata", default=None, help="逗号分隔的层；缺省 " + ",".join(FE.DEFAULT_RUN_STRATA))
    p.add_argument("--repeats", type=int, default=None, help="重复遍数；缺省 2（--smoke 时 1）")
    p.add_argument("--rag", choices=("on", "off"), default="on", help="on=开启并自排除近邻；off=对照臂")
    p.add_argument("--abstract-mode", choices=FE.ABSTRACT_MODES, default="prod",
                   help="prod=截 {} 字符（认证口径）；full=800 字符（对照臂）".format(FE.PROD_ABSTRACT_CHARS))
    p.add_argument("--smoke", action="store_true", help="每组固定抽 {} 条（P1/M-core/H-stream 共 3 批）".format(FE.SMOKE_PER_GROUP))
    p.add_argument("--config", default="config/scholar.env")
    p = sub.add_parser("report", help="渲染运行报告")
    p.add_argument("run", nargs="?", default=None, help="run_id 或 json 路径；缺省最新一份")
    p.add_argument("--vs", default=None, help="基线运行，做共同条目上的配对比较")
    p.add_argument("--show-verdicts", action="store_true", help="逐条打印裁决（盲标完成前禁用）")
    p.add_argument("--json", action="store_true", help="只输出汇总 JSON")
    args = ap.parse_args(argv)
    fn = {"registry-export": cmd_registry_export, "registry-import": cmd_registry_import,
          "label-export": cmd_label_export, "label-import": cmd_label_import,
          "build-gold": cmd_build_gold, "stats": cmd_stats,
          "run": cmd_run, "report": cmd_report}[args.cmd]
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
