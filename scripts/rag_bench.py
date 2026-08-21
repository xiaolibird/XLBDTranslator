# -*- coding: utf-8 -*-
"""札记库检索质量 benchmark（固化资产，2026-08-21 起）。

背景：此前「概念换述 @1 ~30%」「5 case 全 top-1」等数字全是会话内临时实测，
case 集不落盘导致改语料（如摘要喂厚）前后无法同口径对比。本脚本 + case 文件
`test/data/rag_bench_cases.jsonl` 是唯一权威口径；基线数字存
`docs/decisions/rag_bench_baseline_2026-08.md`，改动检索语料/阈值前后都必须复跑。

case 文件每行一个 JSON 对象：
    {"id": "para-001", "type": "paraphrase", "query": "……", "gold": ["citekeyA"]}
- type ∈ {paraphrase(概念换述), zh_oneline(中文判词), en_title(英文标题), legacy_5case}
- gold 是 citekey 列表，命中任一即算对（同主题多篇时避免 @1 判定过严）

评测口径：每条 query 起一个 notes_search.py 子进程（--json --limit 10，默认
hybrid + level auto），与真实用户/skill 调用路径完全一致——不复刻内部函数，
检索实现怎么改 bench 都自动跟着。@1 = gold 出现在第 1 位；@5 = 前 5 位。

用法（仓库根）：
    python scripts/rag_bench.py                      # 全量跑，人读汇总
    python scripts/rag_bench.py --json               # 机器可读（验收断言用）
    python scripts/rag_bench.py --type paraphrase    # 只跑一类
    python scripts/rag_bench.py --mode dense         # 换检索模式对照

退出码：0=跑完（不设通过线，通过线写在验收文档里）；2=case 文件/向量库不可用。
"""
import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_CASES = REPO / "test" / "data" / "rag_bench_cases.jsonl"
SEARCH_SCRIPT = REPO / "scripts" / "notes_search.py"
VALID_TYPES = {"paraphrase", "en_paraphrase", "zh_oneline", "en_title", "legacy_5case"}


def load_cases(path: Path):
    cases = []
    seen_ids = set()
    for ln, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError("case 文件第 {} 行不是合法 JSON：{}".format(ln, e))
        for key in ("id", "type", "query", "gold"):
            if not c.get(key):
                raise ValueError("case 文件第 {} 行缺字段 {}".format(ln, key))
        if c["type"] not in VALID_TYPES:
            raise ValueError("case {} 的 type '{}' 不在 {}".format(c["id"], c["type"], sorted(VALID_TYPES)))
        if c["id"] in seen_ids:
            raise ValueError("case id 重复：{}".format(c["id"]))
        if not isinstance(c["gold"], list) or not all(isinstance(g, str) for g in c["gold"]):
            raise ValueError("case {} 的 gold 必须是 citekey 字符串列表".format(c["id"]))
        seen_ids.add(c["id"])
        cases.append(c)
    if not cases:
        raise ValueError("case 文件没有可用条目：{}".format(path))
    return cases


def run_one(query: str, mode: str, level: str, limit: int, lanes: str = "both"):
    """跑一条 query，返回 (rank(1-based，未命中 None 用于 gold 判定的候选列表), error)。"""
    cmd = [sys.executable, str(SEARCH_SCRIPT), query,
           "--json", "--mode", mode, "--limit", str(limit), "--paper-lanes", lanes]
    if level != "auto":
        cmd += ["--level", level]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO), timeout=120)
    except subprocess.TimeoutExpired:
        return None, "notes_search 超时（>120s）"
    if proc.returncode in (2, 3):
        # 向量库缺失/Ollama 不可用——环境级失败，整个 bench 没法跑
        raise RuntimeError("notes_search 环境失败（exit {}）：{}".format(
            proc.returncode, proc.stderr.strip()[:300]))
    if proc.returncode not in (0, 1):
        # 4=notes_search 未预期内部错误兜底；其余未知码同样按崩溃处理，
        # 绝不能悄悄计入成绩产出假基线
        raise RuntimeError("notes_search 崩溃（exit {}）：{}".format(
            proc.returncode, proc.stderr.strip()[:300]))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        if proc.returncode == 1:
            # --json 契约下"真无命中"也会打印 results:[] 的完整 JSON 再 exit 1
            # （notes_search.py as_json 分支）；exit 1 且 stdout 非 JSON 只能是
            # 崩溃（Python 未捕获异常默认退出码恰为 1）——判环境失败而非 miss，
            # 否则异地/CI 会拿到 0% 假基线且退出码 0
            raise RuntimeError("notes_search 疑似崩溃（exit 1 且 stdout 非 JSON）：{}".format(
                proc.stderr.strip()[:300] or proc.stdout[:200]))
        return None, "输出不是 JSON：{}".format(proc.stdout[:200])
    return [r["citekey"] for r in data.get("results", [])], None


def main() -> int:
    ap = argparse.ArgumentParser(description="札记库检索质量 benchmark")
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--mode", choices=["dense", "hybrid", "sparse"], default="hybrid")
    ap.add_argument("--level", choices=["auto", "paper", "highlight"], default="auto")
    ap.add_argument("--type", dest="only_type", choices=sorted(VALID_TYPES),
                    help="只跑一类 case")
    ap.add_argument("--limit", type=int, default=10, help="每条 query 取前 N 位（默认 10）")
    ap.add_argument("--paper-lanes", choices=["both", "thin"], default="both",
                    help="透传 notes_search：thin=喂厚前行为（A/B 对照）")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    cases_path = Path(args.cases)
    if not cases_path.exists():
        print("case 文件不存在：{}".format(cases_path), file=sys.stderr)
        return 2
    try:
        cases = load_cases(cases_path)
    except ValueError as e:
        print("case 文件非法：{}".format(e), file=sys.stderr)
        return 2
    if args.only_type:
        cases = [c for c in cases if c["type"] == args.only_type]
        if not cases:
            print("type={} 无 case".format(args.only_type), file=sys.stderr)
            return 2

    results = []
    try:
        for c in cases:
            ranked, err = run_one(c["query"], args.mode, args.level, args.limit,
                                  lanes=args.paper_lanes)
            if ranked is None:
                results.append({**c, "rank": None, "error": err})
                continue
            rank = next((i for i, ck in enumerate(ranked, start=1) if ck in set(c["gold"])),
                        None)
            results.append({"id": c["id"], "type": c["type"], "query": c["query"],
                            "gold": c["gold"], "rank": rank, "top": ranked[:5]})
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    by_type = defaultdict(list)
    for r in results:
        by_type[r["type"]].append(r)

    def rate(rows, k):
        hit = sum(1 for r in rows if r["rank"] is not None and r["rank"] <= k)
        return hit, len(rows), (hit / len(rows) if rows else 0.0)

    summary = {}
    for t, rows in sorted(by_type.items()):
        h1, n, r1 = rate(rows, 1)
        h5, _, r5 = rate(rows, 5)
        summary[t] = {"n": n, "at1": h1, "at1_rate": round(r1, 4),
                      "at5": h5, "at5_rate": round(r5, 4)}
    h1, n, r1 = rate(results, 1)
    h5, _, r5 = rate(results, 5)
    summary["_overall"] = {"n": n, "at1": h1, "at1_rate": round(r1, 4),
                           "at5": h5, "at5_rate": round(r5, 4)}

    if args.as_json:
        print(json.dumps({"mode": args.mode, "level": args.level,
                          "summary": summary, "results": results},
                         ensure_ascii=False, indent=2))
        return 0

    print("RAG benchmark（mode={} level={}，{} cases）\n".format(args.mode, args.level, n))
    for t, s in summary.items():
        if t == "_overall":
            continue
        print("  {:12s} n={:<3d} @1 {:>3d}/{:<3d} ({:.0%})   @5 {:>3d}/{:<3d} ({:.0%})".format(
            t, s["n"], s["at1"], s["n"], s["at1_rate"], s["at5"], s["n"], s["at5_rate"]))
    s = summary["_overall"]
    print("  {:12s} n={:<3d} @1 {:>3d}/{:<3d} ({:.0%})   @5 {:>3d}/{:<3d} ({:.0%})".format(
        "总计", s["n"], s["at1"], s["n"], s["at1_rate"], s["at5"], s["n"], s["at5_rate"]))
    misses = [r for r in results if r["rank"] is None or r["rank"] > 1]
    if misses:
        print("\n未拿 @1 的 case：")
        for r in misses:
            print("  [{}] rank={} gold={} query={}".format(
                r["id"], r["rank"], ",".join(r["gold"]), r["query"][:60]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
