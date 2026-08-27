# -*- coding: utf-8 -*-
"""论断—证据蕴含审计（生成侧防线第二层，2026-08-27）。

**它补的是哪个洞**：第一层（`src/scholar/grounding.py` 的数字接地，接在
`validate_*` 里）只能检验含数字的论断——全库实测那只占 31.2%。剩下 68.8% 的纯定性
论断（"作者认为 X 不足以处理 Y"）此前**没有任何把关**：措辞失真、强度漂移、范围
放大、因果方向倒置，字符串比对一个都抓不了。

**与 gen_bench 的分工**：那个零 LLM 成本、可反复跑，是每次改动的验收关；**本脚本
要花钱**（每批一次 LLM 调用），是按需深审。所以默认不跑全库——必须显式给
`--page` 或 `--limit`，防止手滑烧掉一大笔订阅额度。

**判定的可信度**：LLM 判定本身会错，所以 prompt 里把"拿不准判 supported"写死了，
并要求每条非 supported 都从证据原句**逐字摘**一段作依据（摘不出来说明判断没有
根据）。脚本侧再验一次这个摘录是否真在证据里——**LLM 说它摘的、和它真摘的，是两
回事**。验不过的裁决降级为 supported 并计入 `unverified`。

用法（仓库根）：
    python scripts/entail_audit.py --page shadow-variable
    python scripts/entail_audit.py --page shadow-variable --only-qualitative
    python scripts/entail_audit.py --limit 30              # 全库前 30 条论断
    python scripts/entail_audit.py --page X --dry-run      # 只看会送多少条、不调 LLM

退出码：0=跑完（不设通过线）；1=有批次失败；2=环境/参数不可用。
"""
import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.scholar.grounding import check_numbers, norm            # noqa: E402
from src.scholar.llm_client import LLMClient                     # noqa: E402
from src.scholar.page_parse import Page, iter_pages, parse_page   # noqa: E402
from src.scholar.settings import load_scholar_settings            # noqa: E402
from src.scholar.topics import flatten_linebreaks, parse_synthesis  # noqa: E402
from src.utils.logger import get_logger                           # noqa: E402

logger = get_logger(__name__)

TOPICS_DIR = REPO / "output" / "scholar_notes" / "topics"
DEFAULT_PROMPT = REPO / "config" / "prompts" / "entailment_audit_prompt.md"
VERDICTS = ("supported", "overreach", "unsupported", "contradicted")
# 「该去看」的档 —— 2026-08-28 拍板：**非 supported 一律算报警**。
# 此前写作 ("unsupported", "contradicted")，把 overreach 排除在外，于是同一件事
# 有两套定义：标定文档按"非 supported"算 16 条报警，脚本按这个常量算只有 6 条。
# 定成前者的理由：三页标定的 16 条报警里 **10 条是 overreach**，而且它们正是那个
# 最有价值的发现（外推与证据共享同一个引用，形态高度模板化）。把最主要的一类
# 排除在"该去看"之外，这条防线的产出就只剩零头。
ALARMING = ("contradicted", "unsupported", "overreach")


@dataclass
class Item:
    """送审的一条：论断 + 它引用的全部证据。"""
    id: str
    page: str
    section: str
    claim: str
    evidence: List[str] = field(default_factory=list)   # 送 LLM 的展示串（带 E 编号表头）
    citekeys: List[str] = field(default_factory=list)
    raw_quotes: List[str] = field(default_factory=list)  # 纯证据内容，用于摘录回验

    @property
    def pool(self) -> str:
        """摘录回验用的池：**只有证据内容**，不含脚本拼的表头。

        ⚠️ 这里曾经用 `" ".join(self.evidence)`，而 evidence 每条形如
        `E1（li2026Criticrag）：正文…` —— 前缀是本脚本自己拼的装饰。于是模型只要
        把喂给它的表头原样抄回来（≥6 字符即可），一条**凭空捏造的报警**就通过了
        「LLM 说它摘的、和它真摘的是两回事」这道唯一的防伪关。
        实测 `_verify_quote("E1（li2026Criticrag）：", pool)` 返回 True。
        """
        return " ".join(self.raw_quotes)


@dataclass
class Verdict:
    id: str
    verdict: str
    note: str = ""
    quote: str = ""
    verified: bool = True      # quote 是否真能在证据里找到


@dataclass
class Report:
    n_items: int = 0
    n_batches: int = 0
    batches_failed: int = 0
    counts: Dict[str, int] = field(default_factory=dict)
    unverified: int = 0        # LLM 摘录对不上证据、被降级的裁决数
    missing: int = 0           # LLM 漏判的条目（未出现在返回里）
    bad_verdicts: int = 0      # verdict 值非法（大小写/拼写）被兜底成 supported 的
    errors: List[str] = field(default_factory=list)


def collect_items(pages: List[Path], only_qualitative: bool,
                  limit: int) -> List[Item]:
    """把页面拆成送审条目。

    `--only-qualitative` 跳过含数字的论断——它们已被第一层覆盖，重复送审是浪费钱。
    注意这里用的是与第一层**同一个** `check_numbers`，口径一致。
    """
    items: List[Item] = []
    for path in pages:
        page: Page = parse_page(path)
        exempt = page.meta_numbers()
        for i, c in enumerate(page.claims, start=1):
            rows = page.rows_for(c.citekeys)
            if not rows:
                continue      # 引用解析不出证据的，交给编号校验那条防线
            if only_qualitative:
                chk = check_numbers(c.text, page.pool_for(c.citekeys), exempt=exempt)
                if chk.total:
                    continue
            items.append(Item(
                id="C{}".format(len(items) + 1),
                page=page.slug, section=c.section, claim=c.text,
                evidence=[("{}（{}）：{}".format(r.ref, r.citekey, r.quote or r.title))
                          for r in rows],
                citekeys=list(c.citekeys),
                raw_quotes=[(r.quote or r.title) for r in rows],
            ))
            if limit and len(items) >= limit:
                return items
    return items


def build_prompt(batch: List[Item], template: str) -> str:
    blocks = []
    for it in batch:
        ev = "\n".join("  - {}".format(e) for e in it.evidence)
        blocks.append("### {}\n\n**论断**：{}\n\n**它引用的证据**：\n{}".format(
            it.id, it.claim, ev))
    return template.replace("{{PAIR_BLOCK}}", "## 待判定的论断\n\n" + "\n\n".join(blocks))


def _verify_quote(quote: str, pool: str) -> bool:
    """LLM 说它从证据里摘了一段——核实它是不是真摘的。

    宽松比对（归一化 + 去空白 + 换行归一）：模型偶尔会顺手改标点或带回换行。
    但整段对不上就是编的，这类裁决不能算数。

    ⚠️ 必须走 `flatten_linebreaks`：只去 ASCII 空格与全角空格时，一条**真摘录**只要
    带了换行就验不过，而验不过的后果是**降级成 supported**——一条真 contradicted
    就此变成"没问题"，只在 unverified 里留一个数字。三条降级路径全部通向 supported，
    这个不对称已经在标定文档里记过账，这里是第四个消费者。
    """
    def _flat(x: str) -> str:
        return flatten_linebreaks(norm(x)).replace(" ", "").replace("　", "")
    q = _flat(quote)
    if len(q) < 6:
        return False          # 太短的"摘录"没有证明力
    return q in _flat(pool)


def adjudicate(items: List[Item], template: str, llm: LLMClient, *,
               batch_size: int, model: Optional[str], max_tokens: int) -> tuple:
    """分批送审。失败批次如实记账——**不能让缺席的批次看起来像"判过且没问题"**。

    连挂 2 批就熔断（同 lint.py / build_topics.py）：回退链一旦整条耗尽，后面每批
    都会以同一个错误再失败一次，继续跑只是烧时间。
    """
    verdicts: List[Verdict] = []
    rep = Report(n_items=len(items))
    n_batches = (len(items) + batch_size - 1) // max(1, batch_size)
    rep.n_batches = n_batches
    streak = 0
    for bi in range(n_batches):
        batch = items[bi * batch_size:(bi + 1) * batch_size]
        if not batch:
            continue
        if streak >= 2:
            rep.batches_failed += n_batches - bi
            rep.errors.append("连续 2 批失败（多半是 LLM 回退链已整条耗尽），"
                              "中止剩余 {} 批".format(n_batches - bi))
            break
        logger.info("  ▶ 第 {}/{} 批（{} 条）", bi + 1, n_batches, len(batch))
        try:
            raw = llm.call(build_prompt(batch, template), model=model,
                           max_tokens=max_tokens, temperature=0.1, json_mode=True)
            data = parse_synthesis(raw)
        except Exception as e:
            streak += 1
            rep.batches_failed += 1
            rep.errors.append("第 {} 批（{} 条）失败：{}: {}".format(
                bi + 1, len(batch), type(e).__name__, str(e)[:160]))
            continue
        streak = 0

        by_id = {it.id: it for it in batch}
        seen = set()
        for v in (data.get("verdicts") or []):
            if not isinstance(v, dict):
                continue
            vid = str(v.get("id", "")).strip()
            it = by_id.get(vid)
            if it is None or vid in seen:
                continue      # 自造编号/重复，丢弃
            seen.add(vid)
            # ⚠️ 必须 .lower()：prompt 里的分类值全是小写反引号，但 LLM 返回
            # "Unsupported" / "OVERREACH" 很常见。不归一的话它们会静默变成
            # supported——一条真报警凭空消失，而且发生在摘录校验之前，连
            # unverified 都不会 +1。非法值也要记账，否则就是模块 docstring 骂的
            # 那种「缺席看起来像判过且没问题」。
            verdict = str(v.get("verdict", "")).strip().lower()
            if verdict not in VERDICTS:
                rep.bad_verdicts += 1
                rep.errors.append("第 {} 批 {} 的 verdict 非法：{!r}".format(
                    bi + 1, vid, str(v.get("verdict"))[:40]))
                verdict = "supported"
            quote = str(v.get("quote") or "").strip()
            ok = True
            if verdict != "supported":
                ok = _verify_quote(quote, it.pool)
                if not ok:
                    # 摘录对不上证据 = 判断没有依据，降级而不是照单全收
                    rep.unverified += 1
                    verdict = "supported"
            verdicts.append(Verdict(id=vid, verdict=verdict,
                                    note=str(v.get("note") or "").strip(),
                                    quote=quote, verified=ok))
        missing = [i for i in by_id if i not in seen]
        if missing:
            rep.missing += len(missing)
            rep.errors.append("第 {} 批漏判 {} 条：{}".format(
                bi + 1, len(missing), " ".join(sorted(missing)[:8])))

    for v in verdicts:
        rep.counts[v.verdict] = rep.counts.get(v.verdict, 0) + 1
    return verdicts, rep


def main() -> int:
    ap = argparse.ArgumentParser(description="论断—证据蕴含审计（花钱，按需跑）")
    ap.add_argument("--topics-dir", default=str(TOPICS_DIR))
    ap.add_argument("--prompt", default=str(DEFAULT_PROMPT))
    ap.add_argument("--page", default="", help="只审一页（不含 .md）")
    ap.add_argument("--limit", type=int, default=0, help="最多送审几条论断")
    ap.add_argument("--only-qualitative", action="store_true",
                    help="只审不含数字的论断（含数字的已被第一层覆盖）")
    ap.add_argument("--batch-size", type=int, default=8,
                    help="每批送审几条（必须 >=1）")
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--out", default="", help="报告落盘路径（md）")
    ap.add_argument("--dry-run", action="store_true", help="只统计会送多少条，不调 LLM")
    ap.add_argument("--sample", type=int, default=0,
                    help="随机抽 N 条论断连同证据打印出来供**人工**复核，不调 LLM。"
                         "测召回率用：LLM 判 supported 的里面漏了多少，只能靠人去看")
    ap.add_argument("--seed", type=int, default=42, help="抽样种子（可复现）")
    ap.add_argument("--exclude", default="",
                    help="逗号分隔的论断前缀，命中的跳过。测召回率时用来剔掉已报警的条目")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    # 0 或负数会让每一批都切出空列表 → 既不算 failed 也不算 missing → 报告显示
    # "跑完了没问题"，而一次 LLM 都没调。max(1, ...) 只护住了除零，护不住这个。
    if args.batch_size < 1:
        print("--batch-size 必须 >= 1（给了 {}）".format(args.batch_size), file=sys.stderr)
        return 2

    root = Path(args.topics_dir)
    if not root.is_dir():
        print("概念页目录不存在：{}".format(root), file=sys.stderr)
        return 2
    # 花钱的脚本不给"手滑跑全库"的机会
    if not args.page and not args.limit and not args.dry_run:
        print("拒绝无界跑全库（每批一次 LLM 调用）。给 --page 或 --limit；"
              "想看规模先 --dry-run。", file=sys.stderr)
        return 2

    pages = iter_pages(root, args.page)
    if not pages:
        print("没有可审的页面（--page 拼错了？）", file=sys.stderr)
        return 2

    items = collect_items(pages, args.only_qualitative, args.limit)
    if not items:
        print("没有可送审的论断（--only-qualitative 把它们都滤掉了？）", file=sys.stderr)
        return 2

    if args.sample:
        # 召回率只能靠人工复核估计——**不能用同一个 LLM 复审它自己的判决**，
        # 那测出来的是自洽性不是正确性。所以这个模式只负责可复现地抽样并原样
        # 打印，判定由人做。
        import random
        excl = [x.strip() for x in args.exclude.split(",") if x.strip()]
        pool = [it for it in items
                if not any(it.claim.startswith(e) for e in excl)]
        rng = random.Random(args.seed)
        picked = rng.sample(pool, min(args.sample, len(pool)))
        print("人工复核抽样：{} 条（池 {} 条 · seed={}）\n".format(
            len(picked), len(pool), args.seed))
        for n, it in enumerate(picked, start=1):
            print("─" * 78)
            print("[{}] {} · {}".format(n, it.page, it.section or "—"))
            print("\n论断：{}\n".format(it.claim))
            print("证据：")
            for e in it.evidence:
                print("  - {}".format(e))
            print()
        return 0

    n_batches = (len(items) + args.batch_size - 1) // max(1, args.batch_size)
    if args.dry_run:
        by_page: Dict[str, int] = {}
        for it in items:
            by_page[it.page] = by_page.get(it.page, 0) + 1
        print("将送审 {} 条论断 · {} 批（batch-size={}）".format(
            len(items), n_batches, args.batch_size))
        for p, n in sorted(by_page.items()):
            print("  {:34s} {:>4d}".format(p[:34], n))
        return 0

    template = Path(args.prompt).read_text(encoding="utf-8")
    settings = load_scholar_settings()
    llm = LLMClient(settings.llm)
    logger.info("蕴含审计：{} 条论断 · {} 批", len(items), n_batches)
    verdicts, rep = adjudicate(items, template, llm, batch_size=args.batch_size,
                               model=args.model, max_tokens=args.max_tokens)

    by_id = {it.id: it for it in items}
    flagged = [v for v in verdicts if v.verdict != "supported"]

    if args.as_json:
        print(json.dumps({
            "n_items": rep.n_items, "n_batches": rep.n_batches,
            "batches_failed": rep.batches_failed, "counts": rep.counts,
            "unverified": rep.unverified, "missing": rep.missing,
            "bad_verdicts": rep.bad_verdicts,
            "errors": rep.errors,
            "flagged": [{"id": v.id, "page": by_id[v.id].page,
                         "section": by_id[v.id].section,
                         "verdict": v.verdict, "note": v.note, "quote": v.quote,
                         "claim": by_id[v.id].claim,
                         "citekeys": by_id[v.id].citekeys} for v in flagged],
        }, ensure_ascii=False, indent=2))
        return 1 if rep.batches_failed else 0

    print("\n蕴含审计（{} 条论断 · {} 批）\n".format(rep.n_items, rep.n_batches))
    judged = sum(rep.counts.values())
    for k in VERDICTS:
        n = rep.counts.get(k, 0)
        if judged:
            print("  {:14s} {:>4d}  ({:.0%})".format(k, n, n / judged))
    if rep.unverified:
        print("\n  ⚠️ {} 条裁决的证据摘录在原文里找不到，已降级为 supported"
              "（LLM 说它摘的、和它真摘的是两回事）".format(rep.unverified))
    if rep.missing:
        print("  ⚠️ {} 条被 LLM 漏判".format(rep.missing))
    if rep.bad_verdicts:
        print("  ⚠️ {} 条 verdict 值非法，已兜底成 supported".format(rep.bad_verdicts))
    if rep.batches_failed:
        print("\n  ⛔ {} 批未判定——**这些不等于没问题**：".format(rep.batches_failed))
        for e in rep.errors:
            print("     {}".format(e))

    if flagged:
        print("\n可疑论断（逐条人工判）：")
        for v in sorted(flagged, key=lambda x: ALARMING.index(x.verdict)
                        if x.verdict in ALARMING else 9):
            it = by_id[v.id]
            print("\n  [{}] {} · {} · {}".format(
                v.verdict, it.page, it.section[:24] or "—", " ".join(it.citekeys[:3])))
            print("    论断：{}".format(it.claim[:170]))
            if v.note:
                print("    裁决：{}".format(v.note[:170]))
            if v.quote:
                print("    证据：{}".format(v.quote[:140]))

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# 蕴含审计报告", "",
                 "- 送审 {} 条 · {} 批 · 失败 {} 批".format(
                     rep.n_items, rep.n_batches, rep.batches_failed),
                 "- 裁决分布：{}".format(rep.counts or "（无）"),
                 "- 摘录验不过被降级：{} · LLM 漏判：{}".format(rep.unverified, rep.missing),
                 ""]
        for v in flagged:
            it = by_id[v.id]
            lines += ["## [{}] {} · {}".format(v.verdict, it.page, it.section or "—"),
                      "", "**论断**：{}".format(it.claim), "",
                      "**裁决**：{}".format(v.note or "—"), "",
                      "**证据摘录**：{}".format(v.quote or "—"), "",
                      "**引用**：{}".format(" ".join(it.citekeys)), ""]
        out.write_text("\n".join(lines), encoding="utf-8")
        print("\n报告：{}".format(out))
    return 1 if rep.batches_failed else 0


if __name__ == "__main__":
    sys.exit(main())
