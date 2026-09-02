# -*- coding: utf-8 -*-
"""backfill_abstracts 的 dedup_key 漂移对账 + watcher 覆盖回归。

锁两件事（2026-08-21 缺陷修复，成因见 scripts/backfill_abstracts.py 头注）：
1. **_reconcile_drift**：元数据 enrich 后索引键漂移（title:xxx → doi:yyy），
   sidecar 旧键摘要必须迁到新键，否则该篇 ab: 厚 chunk 在下次同步被静默删除、
   摘要成孤儿且无自动入口补抓；不可判（候选键撞多条目）与条目已删的孤儿不许乱动。
2. **launchd plist**：com.xlbd.scholar-embed 的 WatchPaths 必须含 abstracts.json——
   回填只写 sidecar 不碰索引，不盯它则喂厚要等下一次无关索引变动才生效。
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.scholar._citekey_utils import dedup_key_fields  # noqa: E402


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "backfill_abstracts", REPO / "scripts" / "backfill_abstracts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_script()

TITLE = "Shadow Variables for Missing Not at Random Mechanisms in EHR"
TITLE_KEY = dedup_key_fields(None, None, TITLE)          # title:shadowvariables...


def _paper(dedup_key, title=TITLE, **kw):
    e = {"dedup_key": dedup_key, "title": title, "citekey": "x2026",
         "doi": None, "arxiv_id": None, "url": None, "duplicate_of": None}
    e.update(kw)
    return e


def _store(abstracts=None, failures=None):
    return {"generated_at": None,
            "abstracts": dict(abstracts or {}), "failures": dict(failures or {})}


def test_title_to_doi_drift_migrates():
    """Crossref 补上 DOI 后旧 title: 键的摘要迁到新 doi: 键，failures 同步清掉。

    title: 键是派生量（撞名不可分），迁移须凭 sidecar 冻结的 citekey 与目标对上。
    """
    papers = [_paper("doi:10.1/x", doi="10.1/x")]
    store = _store(abstracts={TITLE_KEY: {"abstract": "A", "source": "openalex",
                                          "citekey": "x2026"}},
                   failures={TITLE_KEY: {"reason": "r", "class": "network"}})
    changed = MOD._reconcile_drift(papers, store)
    assert changed == 1
    assert TITLE_KEY not in store["abstracts"]
    assert store["abstracts"]["doi:10.1/x"]["abstract"] == "A"
    assert store["abstracts"]["doi:10.1/x"]["migrated_from"] == TITLE_KEY
    assert store["failures"] == {}


def test_migrated_key_not_refetched():
    """迁移过的键不再算缺失——对账必须发生在 todo 计算之前才有意义。"""
    papers = [_paper("doi:10.1/x", doi="10.1/x")]
    store = _store(abstracts={TITLE_KEY: {"abstract": "A", "source": "openalex",
                                          "citekey": "x2026"}})
    MOD._reconcile_drift(papers, store)
    todo = [e for e in papers if e["dedup_key"] not in store["abstracts"]]
    assert todo == []


def test_vanished_entry_same_norm_title_not_migrated():
    """2026-08-21 第 3 轮缺陷：原条目 A 已移出索引，另一篇 B 规范化标题相同 →
    A 的孤儿摘要不得迁给 B（citekey 对不上），孤儿原样保留、B 走 todo 正常抓。"""
    papers = [_paper("doi:10.9/z", doi="10.9/z", citekey="b2026")]
    store = _store(abstracts={TITLE_KEY: {"abstract": "A-abs", "source": "openalex",
                                          "citekey": "a2025"}})
    assert MOD._reconcile_drift(papers, store) == 0
    assert store["abstracts"][TITLE_KEY]["abstract"] == "A-abs"
    assert "doi:10.9/z" not in store["abstracts"]


def test_legacy_title_orphan_without_citekey_not_migrated():
    """老 sidecar 记录没存 citekey → 身份证据不足，title: 键不迁（新键重抓兜底）。"""
    papers = [_paper("doi:10.1/x", doi="10.1/x")]
    store = _store(abstracts={TITLE_KEY: {"abstract": "A", "source": "openalex"}})
    assert MOD._reconcile_drift(papers, store) == 0
    assert TITLE_KEY in store["abstracts"]
    assert "doi:10.1/x" not in store["abstracts"]


def test_retracted_entry_contests_candidate_key():
    """撞键判定须覆盖全索引：撤稿条目与主条目规范标题相同 → 候选键不可判，不迁。"""
    papers = [_paper("doi:10.1/x", doi="10.1/x", citekey="x2026"),
              _paper("doi:10.8/r", doi="10.8/r", citekey="r2024",
                     flags=["RETRACTED"])]
    store = _store(abstracts={TITLE_KEY: {"abstract": "A", "source": "openalex",
                                          "citekey": "x2026"}})
    assert MOD._reconcile_drift(papers, store) == 0
    assert TITLE_KEY in store["abstracts"]


def test_stale_failure_on_new_key_cleared_after_migration():
    """新键此前抓取失败留下的 failures 记录：迁移成功后必须清掉，否则永不失效
    （该键已在 abstracts 里，不再进 todo，flush 只清本轮抓到的键）。"""
    papers = [_paper("doi:10.1/x", doi="10.1/x")]
    store = _store(abstracts={TITLE_KEY: {"abstract": "A", "source": "openalex",
                                          "citekey": "x2026"}},
                   failures={"doi:10.1/x": {"reason": "r", "class": "network"}})
    assert MOD._reconcile_drift(papers, store) == 1
    assert store["abstracts"]["doi:10.1/x"]["abstract"] == "A"
    assert store["failures"] == {}


def test_collision_is_undecidable():
    """两条目规范标题相同（候选键撞车）→ 不可判，孤儿原样保留、不迁移。"""
    papers = [_paper("doi:10.1/x", doi="10.1/x"),
              _paper("doi:10.2/y", doi="10.2/y")]
    store = _store(abstracts={TITLE_KEY: {"abstract": "A", "source": "openalex"}})
    assert MOD._reconcile_drift(papers, store) == 0
    assert TITLE_KEY in store["abstracts"]


def test_vanished_entry_orphan_kept():
    """条目已从索引删除的孤儿键保留（断点缓存），不误删也不乱迁。"""
    papers = [_paper("doi:10.9/z", title="A Completely Different Paper Title Here",
                     doi="10.9/z")]
    store = _store(abstracts={TITLE_KEY: {"abstract": "A", "source": "openalex"}})
    assert MOD._reconcile_drift(papers, store) == 0
    assert TITLE_KEY in store["abstracts"]


def test_new_key_already_fetched_drops_stale():
    """新键已单独抓过时，旧键是纯冗余：删旧、绝不覆盖新。"""
    papers = [_paper("doi:10.1/x", doi="10.1/x")]
    store = _store(abstracts={
        TITLE_KEY: {"abstract": "OLD", "source": "openalex", "citekey": "x2026"},
        "doi:10.1/x": {"abstract": "NEW", "source": "crossref"}})
    assert MOD._reconcile_drift(papers, store) == 1
    assert TITLE_KEY not in store["abstracts"]
    assert store["abstracts"]["doi:10.1/x"]["abstract"] == "NEW"
    assert "migrated_from" not in store["abstracts"]["doi:10.1/x"]


def test_orphan_failure_key_cleared():
    """failures 段孤儿键（能映射到现存条目）清掉：新键会自然进 todo 重试。"""
    papers = [_paper("doi:10.1/x", doi="10.1/x")]
    store = _store(failures={TITLE_KEY: {"reason": "r", "class": "network"}})
    assert MOD._reconcile_drift(papers, store) == 1
    assert store["failures"] == {}


def test_arxiv_rung_also_maps():
    """键梯中间层：doi 补上前的 arxiv: 旧键同样能迁。"""
    papers = [_paper("doi:10.1/x", doi="10.1/x", arxiv_id="2501.00001")]
    store = _store(abstracts={"arxiv:2501.00001": {"abstract": "A", "source": "arxiv"}})
    assert MOD._reconcile_drift(papers, store) == 1
    assert store["abstracts"]["doi:10.1/x"]["migrated_from"] == "arxiv:2501.00001"


def test_candidate_still_owned_elsewhere_not_orphan_map():
    """候选键仍被另一条目实占（如预印本/正刊两条并存）→ 它不是孤儿，不得当映射目标。"""
    papers = [_paper("doi:10.1/x", doi="10.1/x"),
              _paper(TITLE_KEY)]      # 另一条目当前键就是 title:
    store = _store(abstracts={TITLE_KEY: {"abstract": "A", "source": "openalex"}})
    assert MOD._reconcile_drift(papers, store) == 0
    assert store["abstracts"][TITLE_KEY]["abstract"] == "A"


def test_watcher_covers_abstracts_sidecar():
    """plist 回归：WatchPaths 必须同时盯索引与 abstracts.json（P0 喂厚自动生效的唯一通路）。

    文本级校验而非 plistlib：模板注释里的 `--cite` 等双连字符 expat 会拒解析
    （plutil -lint 宽容放行，安装脚本靠它把关，不冲突）。
    """
    text = (REPO / "config" / "launchd" / "com.xlbd.scholar-embed.plist").read_text(
        encoding="utf-8")
    m = re.search(r"<key>WatchPaths</key>\s*<array>(.*?)</array>", text, re.S)
    assert m, "plist 里找不到 WatchPaths 数组"
    watched = re.findall(r"<string>([^<]+)</string>", m.group(1))
    names = [Path(p).name for p in watched]
    assert "literature_index.json" in names
    assert "abstracts.json" in names


def test_agents_md_selfheal_doc_matches_watcher():
    """AGENTS.md 回归：向量库自愈说明必须与 plist 现实一致。

    2026-08-21 第 2 轮审计缺陷：plist 已加 abstracts.json 后，AGENTS.md 仍写
    「WatchPaths 盯住 literature_index.json……索引是所有改动的公共下游，盯住它就
    一网打尽」——照此操作的 agent 会在回填摘要后误刷索引、或误判 sidecar 变更
    不会自动同步。锁三点：提及 abstracts.json 由 backfill 直写不经索引、
    「一网打尽/公共下游」论断已删、派生物清单覆盖 ab:/h: 两种 chunk id 形态。
    """
    agents_md = REPO / "output" / "scholar_notes" / "AGENTS.md"
    if not agents_md.exists():
        pytest.skip("本机无 output/scholar_notes/AGENTS.md（output/ 不进 git，CI 必缺），跳过实文档回归")
    text = agents_md.read_text(encoding="utf-8")
    # WatchPaths 段：两个文件都要被点名，且写明 abstracts.json 不经索引
    assert "abstracts.json" in text, "AGENTS.md 对 abstracts.json 零提及"
    assert "backfill_abstracts.py" in text
    # 过时论断不许回潮（索引已不是所有改动的公共下游）
    assert "一网打尽" not in text
    assert "所有改动的公共下游" not in text
    # 改键派生物清单：三级 chunk id 形态（与 src/scholar/embed_store.py 生成式一致）
    assert "p:<citekey>" in text
    assert "ab:<citekey>" in text
    assert "h:<citekey>" in text
