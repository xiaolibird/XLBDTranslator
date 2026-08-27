# -*- coding: utf-8 -*-
"""札记库**生成侧**质量 benchmark（2026-08-27 起）。

与 rag_bench.py 的分工：那个测检索排序（hit@k / nDCG@10 / MRR），到 nDCG@10 为止；
本脚本测**检索之后**——论断有没有忠实于它引的证据。这一段此前没有任何量化基线
（见 docs/decisions/rag_verification_plan_2026-08.md 的 P0）。

为什么必须有：现有防线（topics.validate_synthesis / qa.validate_qa）校验的是
**编号合法性**，不是内容一致性。qa.py 自己的注释写得很直白——「防线保证 citekey
与原句真实存在，不保证转述没有失真」。一条论断只要挂了个存在的 [@key]，哪怕内容
与原句无关甚至相反，全部检查都会放行。

**零 LLM 成本，故意的**：本脚本只审计**已落盘**的 topics/qa 页面，不重跑生成。
理由有三——(1) 重跑要烧订阅额度（8 个概念页目前已需分批防触顶）；(2) 已落盘页面
就是真实产物，直接测它最诚实；(3) 零成本才能在每次改动前后反复跑。所有判据都是
确定性的字符串运算，没有 LLM 裁判，基线本身不会浮动。

三个指标：
  numeric_grounding  论断里的数字，能在它所引证据的匹配池里找到的比例。**主指标**。
                     抄错量级（0.06→0.6）、张冠李戴（把 A 的 AUC 安到 B 头上）这两类
                     失真纯字符串就能抓，精度高、零成本。
  lexical_overlap    论断与匹配池的词面覆盖率。**弱信号，单独不可判定**——转述本就
                     会换词，低覆盖不等于失真。只看它的**分布漂移**：改了 prompt 或
                     换了模型后整体分布下移，值得回头看。
  report_fields      frontmatter 里 dropped_claims / invalid_refs / stripped_cites 的
                     聚合。这些字段一直在产出，只是从来没人聚合过。

匹配池（一条论断能拿来对照的全部文本）= 该论断所引每个 citekey 在本页证据表里的
    **原句 + 标题**，仅此两样。
曾经还并入 citekey / 发表年份 / 出处行，理由是"年份常来自文献自身，不放宽会造出
一堆假失真"。**这个理由被实测证伪**（2026-08-27 对抗审核）：砍掉后全库未接地数
一个都没变，而它们制造可测的假接地。详见 `src.scholar.grounding.build_pool`。

用法（仓库根）：
    python scripts/gen_bench.py                 # 全量跑，人读汇总
    python scripts/gen_bench.py --json          # 机器可读（前后对照断言用）
    python scripts/gen_bench.py --detail        # 打印每条未接地数字的上下文
    python scripts/gen_bench.py --page shadow-variable   # 只跑一页

退出码：0=跑完（**不设通过线**，通过线写在验收文档里，口径同 rag_bench.py）；
        2=页面目录不可用。
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# 判据与生产链路（topics.validate_synthesis / qa.validate_qa）共用同一个模块——
# 两边各写一套的话，bench 报 100% 而生产链路报别的，谁也不知道该信哪个。
from src.scholar.grounding import build_pool, check_numbers, norm as _norm  # noqa: E402
from src.scholar.page_parse import iter_pages, parse_page  # noqa: E402

TOPICS_DIR = REPO / "output" / "scholar_notes" / "topics"

# 页面解析走 page_parse（与 entail_audit 共用）；这里只留重建论断行时要用的引用正则
_CITE_RE = re.compile(r"\[@([^\]\s]+)\]")



def audit_claim(claim: str, evidence: dict, exempt=None):
    """审一条论断，返回 (NumberCheck, 词面覆盖率)。

    判据全部委托给 src.scholar.grounding——与生产链路同源。
    """
    from src.scholar.vault import tokenize

    keys = _CITE_RE.findall(claim)
    pool = build_pool(*(evidence.get(k, "") for k in keys))
    chk = check_numbers(claim, pool, exempt=exempt)

    body = _norm(_CITE_RE.sub("", claim))
    ctoks = set(tokenize(body))
    ptoks = set(tokenize(_norm(pool)))
    overlap = (len(ctoks & ptoks) / len(ctoks)) if ctoks else 0.0
    return chk, overlap


def audit_page(path: Path, meta_exempt: bool = True):
    page = parse_page(path)
    fm = page.frontmatter
    claims = ["- {} {}".format(c.text, " ".join("[@{}]".format(k) for k in c.citekeys))
              for c in page.claims]
    evidence = {}
    for row in page.evidence.values():
        evidence[row.citekey] = (evidence.get(row.citekey, "") + " " + row.pool).strip()

    # 元数字豁免：论断里的 "60 条证据" 说的是本页证据条数，来源是页面元信息而不是
    # 任何一条证据，不豁免会稳定造出假失真。
    exempt = set(page.meta_numbers()) if meta_exempt else set()

    total = grounded = n_derived = 0
    overlaps, details = [], []
    for c in claims:
        chk, ov = audit_claim(c, evidence, exempt=exempt)
        total += chk.total
        grounded += chk.grounded
        n_derived += len(chk.derived)
        overlaps.append(ov)
        if chk.ungrounded or chk.derived:
            details.append({"claim": c.strip()[:200], "ungrounded": chk.ungrounded,
                            "derived": chk.derived, "cites": _CITE_RE.findall(c)})

    # 主指标只把 ungrounded 算作失分；derived 从分母里剔出去单列
    denom = total - n_derived
    report = {k: int(fm[k]) for k in ("dropped_claims", "invalid_refs",
                                      "stripped_cites", "n_gaps",
                                      "numbers_checked", "ungrounded_numbers")
              if fm.get(k, "").lstrip("-").isdigit()}

    # 交叉校验：生产链路（validate_*）在生成时也算了一遍并写进 frontmatter，
    # 本脚本是事后从渲染好的 markdown 反算的。
    #
    # ⚠️ 两者**不该期望相等**（2026-08-27 对抗审核订正）。此前这里写着"不一致说明
    # 有 bug"，但有一处口径差异是**结构性不可消除**的：论断渲染时证据编号已被回译
    # 成 [@citekey]，页面上不再有 E 编号，所以本脚本只能按 citekey 合池（该 citekey
    # 的**全部**证据行），而生产侧按 ref 取池（**只有那一条**）。同一篇论文贡献多条
    # 证据时，bench 的池严格更宽 → bench 只会更宽松，不会更严。
    #
    # 于是正确的不变式是**单向**的：production_ungrounded >= bench_ungrounded。
    # mismatch < 0 才是真 bug（bench 报了生产没报的，说明解析或判据跑偏）。
    # 另一个后果值得记住：按 citekey 合池会让**张冠李戴**（把同一篇另一条证据的
    # 数字安到本条上）在 bench 侧隐形——而那正是本 bench 声称能抓的两类失真之一。
    # 这类失真只有生产侧那条防线看得见，不要以为 bench 全绿就等于没有。
    #
    # 旧版页面没有这两个字段，此时为 None（不是 0，别把"没测过"读成"没问题"）。
    n_ungrounded = sum(len(d["ungrounded"]) for d in details)
    mismatch = None
    if "ungrounded_numbers" in report:
        mismatch = report["ungrounded_numbers"] - n_ungrounded

    return {
        "page": path.stem,
        "n_claims": len(claims),
        "n_evidence_rows": len(evidence),
        "numeric_total": total,
        "numeric_grounded": grounded,
        "numeric_derived": n_derived,
        "numeric_ungrounded": n_ungrounded,
        "numeric_rate": round(grounded / denom, 4) if denom else None,
        "lexical_overlap": round(sum(overlaps) / len(overlaps), 4) if overlaps else 0.0,
        "report": report,
        "production_mismatch": mismatch,
        "details": details,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="札记库生成侧质量 benchmark")
    ap.add_argument("--topics-dir", default=str(TOPICS_DIR))
    ap.add_argument("--page", help="只跑某一页（不含 .md）")
    ap.add_argument("--detail", action="store_true", help="打印未接地数字明细")
    ap.add_argument("--no-meta-exempt", action="store_true",
                    help="关掉元数字豁免（标定豁免规则本身时用）")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    root = Path(args.topics_dir)
    if not root.is_dir():
        print("概念页目录不存在：{}".format(root), file=sys.stderr)
        return 2

    pages = iter_pages(root, args.page or "")
    if not pages:
        print("没有可评测的页面（--page 拼错了？）", file=sys.stderr)
        return 2

    rows = [audit_page(p, meta_exempt=not args.no_meta_exempt) for p in pages]
    num_total = sum(r["numeric_total"] for r in rows)
    num_ok = sum(r["numeric_grounded"] for r in rows)
    num_der = sum(r["numeric_derived"] for r in rows)
    denom = num_total - num_der
    claims_total = sum(r["n_claims"] for r in rows)
    overall = {
        "n_pages": len(rows),
        "n_claims": claims_total,
        "numeric_total": num_total,
        "numeric_grounded": num_ok,
        "numeric_derived": num_der,
        "numeric_rate": round(num_ok / denom, 4) if denom else None,
        "lexical_overlap": round(
            sum(r["lexical_overlap"] * r["n_claims"] for r in rows) / claims_total, 4
        ) if claims_total else 0.0,
        "dropped_claims": sum(r["report"].get("dropped_claims", 0) for r in rows),
        "invalid_refs": sum(r["report"].get("invalid_refs", 0) for r in rows),
        "stripped_cites": sum(r["report"].get("stripped_cites", 0) for r in rows),
    }

    if args.as_json:
        print(json.dumps({"overall": overall, "pages": rows},
                         ensure_ascii=False, indent=2))
        return 0

    print("生成侧 benchmark（{} 页 · {} 条论断）\n".format(len(rows), claims_total))
    print("  {:32s} {:>7s} {:>8s} {:>8s} {:>9s} {:>7s}".format(
        "page", "claims", "numeric", "derived", "grounded", "lex"))
    for r in rows:
        rate = "—" if r["numeric_rate"] is None else "{:.0%}".format(r["numeric_rate"])
        print("  {:32s} {:>7d} {:>8d} {:>8d} {:>9s} {:>7.3f}".format(
            r["page"][:32], r["n_claims"], r["numeric_total"], r["numeric_derived"],
            rate, r["lexical_overlap"]))
    rate = "—" if overall["numeric_rate"] is None else "{:.1%}".format(overall["numeric_rate"])
    print("\n  总计：数字 {}/{} 接地（{}）；另有 {} 个派生量（比值/百分点差，不计入分母）"
          .format(num_ok, denom, rate, num_der))
    print("  词面覆盖 {:.3f}（弱信号，只看分布漂移）".format(overall["lexical_overlap"]))
    print("  ValidationReport 聚合：dropped_claims={} invalid_refs={} stripped_cites={}".format(
        overall["dropped_claims"], overall["invalid_refs"], overall["stripped_cites"]))

    checked = [r for r in rows if r["production_mismatch"] is not None]
    # 不变式是单向的：生产侧池更窄 → 生产 ungrounded >= bench ungrounded。
    # mismatch < 0 才是 bug；> 0 是那条结构性口径差异的正常表现。
    bad = [r for r in checked if r["production_mismatch"] < 0]
    looser = [r for r in checked if r["production_mismatch"] > 0]
    if not checked:
        print("  交叉校验：0/{} 页带生产计数（旧版页面，重新生成后才有）".format(len(rows)))
    elif bad:
        print("  ⛔ 交叉校验违反不变式 {} 页——bench 报了生产没报的，"
              "只可能是解析或判据跑偏：".format(len(bad)))
        for r in bad:
            print("     {}：frontmatter {} < 本脚本 {}".format(
                r["page"], r["report"]["ungrounded_numbers"], r["numeric_ungrounded"]))
    else:
        note = ""
        if looser:
            note = "（{} 页生产侧更严，属按 ref vs 按 citekey 的正常差异）".format(len(looser))
        print("  交叉校验：{}/{} 页满足 生产 ≥ bench ✓{}".format(
            len(checked), len(rows), note))

    if args.detail:
        print("\n逐条人工判（真失真 / 还是本脚本的规则该放宽）：")
        for r in rows:
            for d in r["details"]:
                tags = []
                if d["ungrounded"]:
                    tags.append("未接地 " + "、".join(d["ungrounded"]))
                if d["derived"]:
                    tags.append("派生 " + "、".join(d["derived"]))
                print("\n  [{}] {} · 引用 {}".format(
                    r["page"], " | ".join(tags), " ".join(d["cites"][:4])))
                print("    {}".format(d["claim"][:180]))
    else:
        n_bad = sum(1 for r in rows for d in r["details"] if d["ungrounded"])
        if n_bad:
            print("\n  {} 条论断含未接地数字，加 --detail 看明细".format(n_bad))
    return 0


if __name__ == "__main__":
    sys.exit(main())
