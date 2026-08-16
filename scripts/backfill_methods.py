# -*- coding: utf-8 -*-
"""给存量手动精读 bundle 回填「实验方法」节（2026-08-15 之前的精读都没有这一节）。

「实验方法」是 2026-08-15 才加进精读规范的一节：按「他人可照做」的标准从原文整理
数据集/划分、预处理、模型与超参、评估协议、基线、代码可得性，原文没报告的显式写
「原文未报告：X」。当时定的方针是存量不回填，后来改主意——存量 223 篇全部补上。

为什么单独成脚本而不是 `read_pdf.py regen`：regen 是**按现有 bundle 重建札记**，
不重读 PDF；这里要的是重新读一遍原文抽方法学细节，是新的 LLM 工作量。且回填必须
可断点续跑（223 篇 × 一次全文调用），所以带独立进度账本。

用法：
    PYTHONPATH=. python scripts/backfill_methods.py scan                  # 看盘子
    PYTHONPATH=. python scripts/backfill_methods.py repair-paths --apply  # 修失效 pdf_path
    PYTHONPATH=. python scripts/backfill_methods.py run --limit 5         # 试点
    PYTHONPATH=. python scripts/backfill_methods.py run --month 2026-07   # 整月
    PYTHONPATH=. python scripts/backfill_methods.py run                   # 全部待办

回填**只动** bundle 的 `close_reading_final.sections`（插一节），其余字段一律不碰。
写完须对涉及的每个月跑 `read_pdf.py regen --month <M>` 才会进 md/docx/索引/向量库。
`run` 会在收尾对本轮写过盘的月份**强制**跑 `verify` 页码对账（超界即非零退出）；
页内的数字/URL 编造由写盘前的 `fact_check` 拦——两道闸都过了才轮到 regen。
"""
import argparse
import json
import os
import re
import shutil
import sys
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.schema import ScholarSettings  # noqa: E402
from src.scholar.llm_client import LLMClient  # noqa: E402
from src.scholar.paths import repo_path  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402

logger = get_logger("backfill_methods")

SECTION_HEADING = "实验方法"
BUNDLE_SUFFIX = ".paper.json"
LEDGER_NAME = "backfill_methods_progress.json"

# 插在这一节之后；都没有就插在第 2 位（研究问题之后的常规位置）
_ANCHOR_AFTER = ("方法与数据", "研究问题")

# 单页上限：实测有 45000+ 字符的矢量文字层病态页，不设限会把预算吃光（同 pdf_to_text 的教训）
_PAGE_MAX_CHARS = 20_000
# 整篇上限：p90 约 14 万字符，40 万够覆盖 max（46 万那篇只丢尾部参考文献）
_DOC_MAX_CHARS = 400_000


def _cur_ts() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _load_settings(config):
    settings = ScholarSettings.from_env_file(repo_path(config))
    if settings.llm.provider == "gemini" and settings.llm.model.startswith("gemini"):
        settings.llm.provider = "deepseek"
    settings.processing.notes_dir = repo_path(settings.processing.notes_dir)
    return settings


# ---------------- bundle 扫描 ----------------

def _iter_bundles(notes_dir: Path, month: str = ""):
    root = notes_dir / "manual"
    if not root.exists():
        return
    months = [root / month] if month else sorted(p for p in root.iterdir() if p.is_dir())
    for mdir in months:
        if not mdir.is_dir():
            continue
        for bf in sorted(mdir.glob("*" + BUNDLE_SUFFIX)):
            try:
                data = json.loads(bf.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("⚠️ 读 bundle 失败 {}: {}".format(bf.name, e))
                continue
            yield bf, data


def _sections(data: dict) -> list:
    return ((data.get("close_reading_final") or {}).get("sections") or [])


def has_methods(data: dict) -> bool:
    return any(SECTION_HEADING in (s.get("heading") or "") for s in _sections(data))


def is_backfillable(data: dict) -> bool:
    """只回填已 final 且确有精读正文的 bundle。

    draft 还没走过 agent 亲读核验，`close_reading_final` 是 None——往里插节会直接
    写崩（None 不能下标），而且语义上也不该由回填代劳：那一节本就该由精读协议产出。
    实测撞上过：另一个会话正往同一个札记目录 ingest，落下了新的 draft。
    """
    return data.get("status") == "final" and bool(_sections(data))


def _title_of(data: dict) -> str:
    return (((data.get("segment") or {}).get("metadata") or {}).get("title") or "").strip()


def _pdf_of(data: dict) -> Path:
    return Path(os.path.expanduser(data.get("pdf_path") or ""))


# ---------------- 进度账本 ----------------

def _ledger_path(notes_dir: Path) -> Path:
    return notes_dir / "manual" / LEDGER_NAME


def load_ledger(notes_dir: Path) -> dict:
    p = _ledger_path(notes_dir)
    if not p.exists():
        return {"version": 1, "papers": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d.setdefault("papers", {})
        return d
    except Exception as e:                       # 坏账本不该挡住回填，但要吵
        logger.warning("⚠️ 账本损坏，本次从空账本开始（旧文件不删）：{}".format(e))
        return {"version": 1, "papers": {}}


def save_ledger(notes_dir: Path, ledger: dict) -> None:
    p = _ledger_path(notes_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- PDF → 带页码锚的全文 ----------------

def pdf_text_with_pages(path: Path, page_max: int = _PAGE_MAX_CHARS,
                        doc_max: int = _DOC_MAX_CHARS) -> tuple:
    """抽全文，每页前加 `[p.N]` 标记。返回 (text, n_pages, raw_chars)。

    页码锚是这一节的硬要求——「每项标注原文页码」，模型看不见页边界就只能编页码。
    仓库既有的 `pdf_to_text` 不带页标（分块通读不需要），故这里自己抽，不去改它的语义。
    """
    import fitz
    parts, raw = [], 0
    with fitz.open(str(path)) as doc:
        n_pages = doc.page_count
        for i, page in enumerate(doc, 1):
            t = page.get_text() or ""
            raw += len(t)
            parts.append("[p.{}]\n{}".format(i, t[:page_max]))
    text = "\n".join(parts)
    if len(text) > doc_max:
        text = text[:doc_max] + "\n\n［全文过长，已截断至 {} 字符］".format(doc_max)
    return text, n_pages, raw


# ---------------- prompt ----------------

_METHODS_PROMPT = """你在给一篇已精读过的论文补写一节「实验方法」，供研究者**照着复现**。

论文标题：{title}

## 硬要求（违反即作废）
1. **只写原文里确有的内容**。每一项都要能在下面给的全文里找到出处，标注页码（用文本里的
   `[p.N]` 标记，写成「(p.12)」）。宁缺勿造——找不到就归到「原文未报告」，不许推测、
   不许用领域常识补全（例如原文没写 batch size 就不许写「通常为 32」）。
2. **不要把原文措辞升格**：原文写 "5-fold CV" 就写 5 折交叉验证，不要替它说成"嵌套交叉验证"；
   原文写 "logistic regression" 就照写，不要归类成"广义线性模型族"。
3. **必须有一句以「【代码与数据可得性】」开头**。原文给了仓库地址就把**完整 URL 原样抄下来**
   （GitHub/Zenodo/OSF 都算），连同许可、数据使用协议(DUA)、申请方式一并写；原文确实没有
   就写「【代码与数据可得性】原文未报告」。这一项最常被漏——论文的 Code/Data availability
   声明通常紧挨参考文献、离方法节很远，读到那里时别跳过。
4. **最后一句必须是「【原文未报告】…」**，列出你查过但原文确实没给的项（种子、硬件、
   划分比例、完整超参等）。一项都不缺才写「【原文未报告】无——上述各项原文均有交代」。
5. 若这篇根本不是实证/建模研究（综述、观点、理论推导），不要硬凑：写一两句说明它没有实验，
   再用「【原文未报告】」交代缺哪些，就够了（第 3 条仍要遵守）。

## 要覆盖的维度（有则写、无则归入未报告）
数据集/队列与划分（来源、样本量、划分比例、分层、防泄漏）· 结局与标签定义 · 特征与预处理
（缺失处理、标准化、编码）· 模型与超参（优化器/学习率/batch/epoch/种子/早停/正则）·
训练与评估协议（交叉验证、指标、置信区间、统计检验、多重比较校正）· 基线及其配置 ·
硬件与耗时 · 代码与数据可得性（仓库链接、许可、DUA）

## 写法
每句用【维度】开头，例如【队列与划分】【特征】【模型与超参】【评估协议】【基线】
【代码与数据可得性】【原文未报告】。句级 tag 按用途取值：
- "可引用证据"：含具体数字/配置/可溯源事实
- "方法论借鉴"：可迁移到别的课题的做法
- "可反驳观点"：方法上可质疑之处（如「缺失一律置 0」这类可议的处理）
- null：其余（含「原文未报告」那句）

只输出 JSON，不要额外文字：
{{"sentences": [{{"text": "【队列与划分】…（p.4）", "tag": "可引用证据"}}]}}

## 论文全文（带 [p.N] 页码标记）
{body}"""


# ---------------- 生成 + 校验 ----------------

_VALID_TAGS = {"可引用证据", "可反驳观点", "方法论借鉴"}


def _strip_json(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        s = s.rsplit("```", 1)[0]
    i, j = s.find("{"), s.rfind("}")
    return s[i:j + 1] if i >= 0 and j > i else s


def parse_sentences(resp: str) -> list:
    """解析 LLM 回复为 [{'text','tag'}]。解析失败或不合格返回 []（调用方记 failed，不写盘）。"""
    try:
        data = json.loads(_strip_json(resp))
    except Exception:
        return []
    raw = data.get("sentences") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out = []
    for st in raw:
        if not isinstance(st, dict):
            continue
        txt = (st.get("text") or "").strip()
        if len(txt) < 8:                      # 「无」「N/A」这类空转句直接扔
            continue
        tag = st.get("tag")
        out.append({"text": txt, "tag": tag if tag in _VALID_TAGS else None})
    return out


def validate(sents: list) -> str:
    """返回空串=合格，否则返回不合格原因。不合格一律不写盘，留给下一轮重跑。"""
    if len(sents) < 3:
        return "只有 {} 句，太少".format(len(sents))
    if not any("原文未报告" in s["text"] for s in sents):
        return "缺「原文未报告」收尾句（prompt 硬要求，缺了说明模型没照做，怀疑整体质量）"
    # 只认 `p.数字`，不绑定括号写法——手写的那篇用的是「（Methods p.12）」，
    # 死磕 "(p." 会把它误判成没有页码锚。
    if not any(re.search(r"[pP]\.\s*\d", s["text"]) for s in sents):
        return "全无页码锚"
    # 实测漏过一次：论文 p.11 明写 GitHub + Zenodo 链接，产出里既没写进来也没列进未报告，
    # 整个维度凭空消失。Code/Data availability 声明在版式上紧挨参考文献、离方法节最远，
    # 是注意力最容易滑过去的位置；而「有没有代码」恰是这一节最实用的一项，故单独设卡。
    if not any("代码与数据可得性" in s["text"] for s in sents):
        return "缺【代码与数据可得性】（原文有链接就抄链接，没有也要显式写未报告）"
    return ""


# 数字 token 口径与 closereading.verify_citable_numbers 一致（去千分位后子串回查），
# 额外认科学计数法（3e-4）：超参最常这么写，拆成裸的 3 和 4 去回查必然全中、等于没查。
_NUM_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*(?:[eE][+-]?\d+)?")
_URL_RE = re.compile(r"https?://[^\s）】，。；、'\"()\[\]<>]+")


def fact_check(sents: list, body: str) -> tuple:
    """结构之外的事实回查。返回 (不合格原因或空串, 数字降级句数)，就地改 sents 的 tag。

    validate() 只看结构（句数/收尾句/页锚/可得性卡），对内容零回查；而调用方手里
    就握着喂给模型的带页锚全文，做子串回查是零成本的。auto 链路在 close_read 出口
    有 verify_citable_numbers 挡同类编造——「模型编出来的数字在下游没有任何再核验
    的机会」——回填句同样带「可引用证据」tag 直通 highlight/向量库/scholar-write
    取证链，这道防线必须盖到回填这条路。两种口径：
    · 数字（软）：「可引用证据」句里任一数字 token 在原文找不到 → tag 降级为 None
      （句子保留、内容不动，只是不再冒充可引用证据），与 auto 链路同款处置；
    · URL（硬）：编出来的仓库地址会被可得性设卡当成「代码可得」写进札记，比错数字
      更毒——任一 URL 在原文里找不到，整批判不合格，走 backfill_one 的重试路径。
      对照用去空白的原文（PDF 抽取常把长 URL 折行）；模型偶尔会按 prompt 的
      「完整 URL」要求给裸域名补 https:// 前缀，去 scheme 再试一次才算不中。
    """
    body_nospace = re.sub(r"\s+", "", body)
    for s in sents:
        for url in _URL_RE.findall(s["text"]):
            url = url.rstrip(".,;:。；，")
            if url in body_nospace or url.split("://", 1)[-1] in body_nospace:
                continue
            return "URL 在原文中找不到（疑似编造）：{}".format(url), 0
    body_num = body.replace(",", "")
    demoted = 0
    for s in sents:
        if s.get("tag") != "可引用证据":
            continue
        missing = [t for t in _NUM_TOKEN_RE.findall(s["text"])
                   if t.replace(",", "") not in body_num]
        if missing:
            s["tag"] = None
            demoted += 1
            logger.warning("⚠️ 可引用证据数字回查不中，降级为普通句：{}…（未命中: {}）".format(
                s["text"][:50], " ".join(missing[:5])))
    return "", demoted


def insert_section(data: dict, sents: list) -> None:
    """把「实验方法」插进 close_reading_final.sections（就地改 data）。

    已有同名节先摘掉再插：重跑（--force）语义是**替换**，不是并排放两节。
    """
    secs = [s for s in _sections(data) if SECTION_HEADING not in (s.get("heading") or "")]
    for anchor in _ANCHOR_AFTER:
        hit = [i for i, s in enumerate(secs) if anchor in (s.get("heading") or "")]
        if hit:
            pos = hit[0] + 1
            break
    else:                                     # 锚点都没有（分节被改过）→ 放第 2 位
        pos = min(1, len(secs))
    secs.insert(pos, {"heading": SECTION_HEADING, "sentences": sents})
    data["close_reading_final"]["sections"] = secs


def backfill_one(bf: Path, data: dict, llm, model: str, backup_dir: Path) -> dict:
    """回填单篇。返回账本条目 dict（status: ok/failed/no_pdf）。

    ⚠️ `llm` 必须是**这一篇专用**的 LLMClient，不能整轮共用一个——见 cmd_run 里的说明。
    """
    pdf = _pdf_of(data)
    title = _title_of(data) or bf.stem
    if not pdf.exists():
        return {"status": "no_pdf", "note": str(pdf)}

    try:
        body, n_pages, raw = pdf_text_with_pages(pdf)
    except Exception as e:
        return {"status": "failed", "note": "抽文本失败: {}".format(e)}
    if len(body.strip()) < 500:               # 扫描件/加密件，抽不出文本
        return {"status": "failed", "note": "抽出文本仅 {} 字符（疑似扫描件）".format(len(body))}

    prompt = _METHODS_PROMPT.format(title=title, body=body)
    # 试点实测：同一 prompt 同一篇，第一次回了空、重跑一次就是 29 句合格产出——
    # 偶发的坏回复（JSON 没闭合/回了解释文字）不该让这篇进失败名单等人工重跑。
    sents, bad, last_err, n_demoted = [], "", "", 0
    for attempt in (1, 2):
        try:
            resp = llm.call(prompt, model=model, max_tokens=8192, json_mode=True)
        except Exception as e:
            last_err = "LLM 调用失败: {}".format(e)
            continue
        sents = parse_sentences(resp)
        bad = validate(sents)
        if not bad:
            # 结构过关再做事实回查：URL 编造整批重来，数字回查不中只摘 tag 不拦（见 fact_check）
            bad, n_demoted = fact_check(sents, body)
        if not bad:
            break
        last_err = "产出不合格：{}".format(bad)
    if bad or not sents:
        return {"status": "failed", "note": "{}（两次均未通过）".format(last_err), "n": len(sents)}

    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bf, backup_dir / bf.name)
    insert_section(data, sents)
    bf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "ok", "n": len(sents), "demoted": n_demoted,
            "pages": n_pages, "chars": raw, "model": model}


# ---------------- 子命令 ----------------

def cmd_scan(args):
    settings = _load_settings(args.config)
    notes_dir = Path(settings.processing.notes_dir)
    ledger = load_ledger(notes_dir)
    from collections import Counter
    todo, done, nopdf, drafts = Counter(), Counter(), [], []
    for bf, data in _iter_bundles(notes_dir, args.month):
        m = data.get("month") or bf.parent.name
        if not is_backfillable(data):
            drafts.append((m, bf.name, data.get("status")))
            continue
        if has_methods(data):
            done[m] += 1
            continue
        todo[m] += 1
        if not _pdf_of(data).exists():
            nopdf.append((m, bf.name, data.get("pdf_path")))
    print("=" * 66)
    print("已有「{}」节：{} 篇 | 待回填：{} 篇".format(
        SECTION_HEADING, sum(done.values()), sum(todo.values())))
    for m in sorted(set(todo) | set(done)):
        print("  {:16} 待 {:3}  已 {:3}".format(m, todo[m], done[m]))
    if drafts:
        print("\n跳过 {} 篇未 final 的 bundle（回填不代劳精读，走 skill read-paper 走完协议）：".format(len(drafts)))
        for m, n, st in drafts:
            print("   {} {} [{}]".format(m, n, st))
    if nopdf:
        print("\n⚠️ PDF 路径失效 {} 篇（先跑 repair-paths）：".format(len(nopdf)))
        for m, n, p in nopdf[:20]:
            print("   {} {} → {}".format(m, n, p))
    fails = [(k, v) for k, v in ledger["papers"].items() if v.get("status") != "ok"]
    if fails:
        print("\n账本里失败/跳过 {} 篇：".format(len(fails)))
        for k, v in fails[:20]:
            print("   {} [{}] {}".format(k, v.get("status"), v.get("note", "")[:70]))
    print("=" * 66)
    return 0


# 4 位：期刊印刷页码可以到四位（如 JCST 的 p.1499）。只捕 3 位会把 p.1499 截成 149，
# 反而误报成「超出 PDF 页数」——实测踩过。
_PAGE_CITE_RE = re.compile(r"[pP]\.\s*(\d{1,4})")


def cmd_verify(args):
    """把每篇产出引用的页码与 PDF 实际页数对账——**页码超界 = 锚是编的**。

    这是最便宜也最有力的编造检测：模型若在编页码，几乎不可能几百篇全落在实际页数内。
    实测 192 篇零超界，唯一"超界"的是 Annual Review 那篇引了印刷页码 p.416（该刊页码
    393–417），不是 PDF 页——所以对 pages 字段能解析出印刷页区间的条目要放行。
    """
    import fitz
    settings = _load_settings(args.config)
    notes_dir = Path(settings.processing.notes_dir)

    bad, checked, skipped, printed_pg = [], 0, 0, 0
    for bf, data in _iter_bundles(notes_dir, args.month):
        secs = [s for s in _sections(data) if SECTION_HEADING in (s.get("heading") or "")]
        if not secs:
            continue
        pdf = _pdf_of(data)
        if not pdf.exists():
            skipped += 1
            continue
        try:
            with fitz.open(str(pdf)) as doc:
                n = doc.page_count
        except Exception:
            skipped += 1
            continue
        checked += 1
        cited = {int(m) for s in secs[0]["sentences"] for m in _PAGE_CITE_RE.findall(s["text"])}
        over = sorted(x for x in cited if x > n)
        if not over:
            continue
        # 印刷页码豁免：书目 pages 形如 "393-417" 且超界值落在该区间内
        meta = (data.get("segment") or {}).get("metadata") or {}
        rng = re.match(r"\s*(\d+)\s*[-–]\s*(\d+)\s*$", str(meta.get("pages") or ""))
        if rng and all(int(rng.group(1)) <= x <= int(rng.group(2)) for x in over):
            printed_pg += 1
            continue
        bad.append((_title_of(data)[:50], n, over))

    print("=" * 66)
    print("对账 {} 篇（PDF 缺失/读不出 {} 篇跳过；印刷页码豁免 {} 篇）".format(
        checked, skipped, printed_pg))
    print("引用了超出实际页数的页码：{} 篇".format(len(bad)))
    for t, n, over in bad:
        print("  ✗ {} | PDF {} 页，却引了 {}".format(t, n, over))
    print("=" * 66)
    return 1 if bad else 0


def _md5(p: Path) -> str:
    import hashlib
    h = hashlib.md5()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def cmd_repair_paths(args):
    """PDF 被挪走后按文件名在 ~/Downloads、~/Documents 下找回，回填 bundle 的 pdf_path。"""
    settings = _load_settings(args.config)
    notes_dir = Path(settings.processing.notes_dir)
    roots = [Path.home() / "Downloads", Path.home() / "Documents"]
    index = {}
    for r in roots:
        if not r.exists():
            continue
        for f in r.rglob("*.pdf"):
            index.setdefault(f.name, []).append(f)

    fixed, ambiguous, lost = [], [], []
    for bf, data in _iter_bundles(notes_dir, args.month):
        old = data.get("pdf_path") or ""
        if not old or _pdf_of(data).exists():
            continue
        cands = index.get(Path(old).name) or []
        if len(cands) > 1 and len({_md5(c) for c in cands}) == 1:
            # 同名多份但内容哈希一致 = 同一篇的副本（整理目录时拷了一份），不算歧义
            cands = cands[:1]
        if len(cands) == 1:
            fixed.append((bf, data, old, str(cands[0])))
        elif len(cands) > 1:
            ambiguous.append((bf.name, Path(old).name, [str(c) for c in cands]))
        else:
            lost.append((bf.name, old))

    print("可修 {} | 同名多份 {} | 找不到 {}".format(len(fixed), len(ambiguous), len(lost)))
    for bf, data, old, new in fixed:
        print("  {}\n    {} →\n    {}".format(bf.name, old, new))
    for n, base, cs in ambiguous:
        print("  ⚠️ {} 同名 {} 份：{}".format(base, len(cs), cs))
    for n, old in lost:
        print("  ❌ 找不到 {}".format(old))

    if not args.apply:
        print("\n（dry-run；加 --apply 写回）")
        return 0
    for bf, data, old, new in fixed:
        data["pdf_path"] = new
        bf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n✅ 已写回 {} 个 bundle 的 pdf_path".format(len(fixed)))
    return 0


def cmd_run(args):
    settings = _load_settings(args.config)
    notes_dir = Path(settings.processing.notes_dir)
    model = settings.llm.closeread_model or settings.llm.model
    ledger = load_ledger(notes_dir)
    backup_dir = Path(args.backup_dir).expanduser()

    pending = []
    for bf, data in _iter_bundles(notes_dir, args.month):
        pid = data.get("paper_id") or bf.stem
        if args.paper and pid not in args.paper:
            continue
        if not is_backfillable(data):         # draft / 无精读正文：跳过，见 is_backfillable
            continue
        if has_methods(data) and not args.force:
            continue
        if not args.retry_failed and ledger["papers"].get(pid, {}).get("status") in ("no_pdf",):
            continue
        pending.append((bf, data, pid))
    if args.limit:
        pending = pending[:args.limit]
    if not pending:
        print("没有待回填的 bundle。")
        return 0

    print("待回填 {} 篇 | 模型 {} | 并发 {} | 备份 → {}".format(
        len(pending), model, args.workers, backup_dir))

    done = {"ok": 0, "failed": 0, "no_pdf": 0}

    def _one(item):
        bf, data, pid = item
        # **每篇一个独立 LLMClient**，绝不整轮共用。起因是内容过滤污染整条回退链：
        # 「Balancing Safety and Helpfulness in Healthcare AI Assistants」正文含越狱样例，
        # claude CLI 回 AUP 拒答 → 整个 client 被永久推到 ollama → 后续 28 篇全部陪葬
        # （那 28 篇本身毫无问题，换进程重跑全过）。
        # 那个根因已在 llm_client 修掉（内容拒答只临时换下家、收尾拨回链位），这里仍
        # 保留每篇独立 client 作纵深防御：**真·故障**的切换是粘性的（本该如此），
        # 但一篇论文撞上限流也不该让整批几十篇跟着降级。构造开销可忽略。
        try:
            r = backfill_one(bf, data, LLMClient(settings.llm), model, backup_dir)
        except Exception as e:                # 单篇异常不许打断整批
            r = {"status": "failed", "note": "未捕获异常: {}".format(e)}
        r["month"] = data.get("month") or bf.parent.name
        r["title"] = _title_of(data)[:80]
        r["ts"] = _cur_ts()
        return pid, r

    # 连续失败熔断：回退链一旦落到做不了这活的 provider（ollama 的 qwen3.5 思考过程
    # 吃光生成预算 → finish_reason=length → 空产出），后面全是白烧。上面的「每篇独立
    # client」已经堵住最常见的成因（内容过滤污染全链），这道熔断留作兜底——真遇上
    # 主 provider 整体不可用（欠费/限流）时，别闷头跑完几十篇。
    streak = 0
    aborted = False
    ok_months = set()                         # 本轮真正写过盘的月份 → 收尾强制页码对账

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(_one, it): it for it in pending}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                pid, r = fut.result()
            except CancelledError:
                continue
            ledger["papers"][pid] = r
            done[r["status"]] = done.get(r["status"], 0) + 1
            if r["status"] == "ok":
                ok_months.add(r["month"])
            icon = {"ok": "✅", "failed": "❌", "no_pdf": "⏭"}.get(r["status"], "?")
            print("{} [{}/{}] {} {}".format(
                icon, i, len(pending), r["title"][:50],
                "{} 句".format(r["n"]) if r.get("n") else r.get("note", "")[:60]))
            save_ledger(notes_dir, ledger)    # 每篇一存：中途 Ctrl-C 不丢进度

            streak = streak + 1 if r["status"] == "failed" else 0
            if streak >= args.abort_after and not aborted:
                aborted = True
                print("\n⛔ 连续失败 {} 篇，本轮中止——多半是 LLM 回退链切到了做不了这活的 "
                      "provider（查 config/scholar.env 的 LLM__FALLBACK_PROVIDERS）。"
                      "已完成的都已落盘，恢复后重跑本命令即可续上。".format(streak))
                for f in futs:
                    f.cancel()

    print("\n" + "=" * 66)
    print("本轮：成功 {} | 失败 {} | 无 PDF {}".format(
        done.get("ok", 0), done.get("failed", 0), done.get("no_pdf", 0)))

    # 页码对账不再等人手动跑：写盘 ≠ 可以 regen。verify 原是独立子命令，run 收尾只
    # 打印 regen 命令，编造检测全靠人记得——回填句是插在亲读核验**之后**的，regen 归档
    # 无人再看，这里是它进取证链前最后一道有人值守的闸。对本轮真正写过盘的月份逐月
    # 对账，任一超界就以非零退出，把 regen 挡在人工处置之后。
    verify_rc = 0
    for m in sorted(ok_months):
        print("\n▶ 页码对账（本轮写入月份）{}".format(m))
        verify_rc |= cmd_verify(argparse.Namespace(config=args.config, month=m))

    months = sorted({r["month"] for r in ledger["papers"].values() if r.get("status") == "ok"})
    print("涉及月份 {}，逐个跑以下命令才会进 md/docx/索引/向量库：".format(len(months)))
    for m in months:
        print("  PYTHONPATH=. python scripts/read_pdf.py regen --month {}".format(m))
    if verify_rc:
        print("⚠️ 页码对账发现超界引用（见上），先人工处置再 regen。")
    print("=" * 66)
    return verify_rc


def main():
    ap = argparse.ArgumentParser(description="给存量手动精读回填「实验方法」节")
    ap.add_argument("--config", default="config/scholar.env")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="统计待回填盘子")
    p_scan.add_argument("--month", default="", help="只看某月（如 2026-07）")
    p_scan.set_defaults(func=cmd_scan)

    p_ver = sub.add_parser("verify", help="页码锚对账：引用页码 > PDF 实际页数 = 编造")
    p_ver.add_argument("--month", default="")
    p_ver.set_defaults(func=cmd_verify)

    p_rep = sub.add_parser("repair-paths", help="修复失效的 pdf_path（按文件名找回）")
    p_rep.add_argument("--month", default="")
    p_rep.add_argument("--apply", action="store_true", help="真正写回（默认 dry-run）")
    p_rep.set_defaults(func=cmd_repair_paths)

    p_run = sub.add_parser("run", help="回填")
    p_run.add_argument("--month", default="", help="只跑某月")
    p_run.add_argument("--paper", nargs="*", default=[], help="只跑指定 paper_id")
    p_run.add_argument("--limit", type=int, default=0, help="本轮最多跑几篇（试点用）")
    p_run.add_argument("--workers", type=int, default=3,
                       help="并发数（默认 3，与 llm_client 的 agent 信号量 4 留余量）")
    p_run.add_argument("--force", action="store_true",
                       help="已有「实验方法」节的也重跑（替换原节，不是并排插两节）")
    p_run.add_argument("--retry-failed", action="store_true",
                       help="连账本里标 no_pdf 的也再试一次")
    p_run.add_argument("--abort-after", type=int, default=5,
                       help="连续失败几篇就中止本轮（默认 5；多半是 LLM 回退链切坏了）")
    p_run.add_argument("--backup-dir",
                       default="/private/tmp/claude-501/backfill_methods_bundles",
                       help="写盘前把原 bundle 备份到这里")
    p_run.set_defaults(func=cmd_run)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
