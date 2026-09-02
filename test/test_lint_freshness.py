# -*- coding: utf-8 -*-
"""派生物新鲜度检查（lint_freshness）：三态判定、死 job 诊断、健壮性、输出纪律、
与 lint 报告的拼接/frontmatter 契约。全 tmp 构造，不读真实 vault/桌面/生产库。
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import lint as L                                          # noqa: E402
from src.scholar import lint_freshness as F                                # noqa: E402

NOW = datetime(2026, 9, 2, 12, 0, 0)
STAMP_OLD = "2026-09-01T10:00:00"          # 26 小时前：超一切 grace
STAMP_RECENT = "2026-09-02T11:58:00"       # 2 分钟前：任何子项的 grace 内


def _mk_notes(tmp_path, index_stamp=STAMP_OLD, with_refs=True):
    notes = tmp_path / "notes"
    (notes / "topics").mkdir(parents=True)
    (notes / "literature_index.json").write_text(
        json.dumps({"generated_at": index_stamp, "papers": []}), encoding="utf-8")
    if with_refs:
        (notes / "all_references.json").write_text("[]", encoding="utf-8")
    return notes


def _mk_store(notes, source=STAMP_OLD, built=None):
    db = notes / "embeddings.sqlite3"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO meta VALUES ('source_generated_at', ?)", (source,))
    con.execute("INSERT INTO meta VALUES ('built_at', ?)",
                (built or NOW.isoformat(timespec="seconds"),))
    con.commit()
    con.close()
    return db


def _mk_vault(tmp_path, notes, src_idx=STAMP_OLD, src_tm="auto", generated_at=None):
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    if src_tm == "auto":
        src_tm = F.topics_mtime(notes)
    (vault / "_meta.json").write_text(json.dumps({
        "source_index_generated_at": src_idx,
        "source_topics_mtime": src_tm,
        "generated_at": generated_at or NOW.isoformat(timespec="seconds"),
    }), encoding="utf-8")
    return vault


def _mk_timeline(tmp_path, src=STAMP_OLD, written_at=None, with_xlsx=True):
    xlsx = tmp_path / "时间线.xlsx"
    meta = tmp_path / ".时间线.xlsx.meta.json"
    if with_xlsx:
        xlsx.write_bytes(b"PK fake")
    meta.write_text(json.dumps({
        "source_index_generated_at": src, "papers": 0,
        "written_at": written_at or NOW.isoformat(timespec="seconds"),
    }), encoding="utf-8")
    return xlsx, meta


def _probe_never(label):  # 测试默认：诊断分支不该被走到时炸出来
    raise AssertionError("job_probe 不应被调用（label={}）".format(label))


def _check(notes, vault=None, xlsx=None, meta=None, probe=_probe_never, **kw):
    return F.check_freshness(notes / "literature_index.json", notes,
                             vault, xlsx, meta, now=NOW, job_probe=probe, **kw)


def _item(rep, key):
    return next(it for it in rep.items if it.key == key)


def _full_env(tmp_path, **store_kw):
    """四个数据源全新鲜的一套环境。"""
    notes = _mk_notes(tmp_path)
    # 让 topics 有一页，且 mtime 固定到过去（防"刚创建"落进 grace 窗干扰断言）
    page = notes / "topics" / "a.md"
    page.write_text("x", encoding="utf-8")
    old = NOW.timestamp() - 7200
    os.utime(page, (old, old))
    _mk_store(notes, **store_kw)
    vault = _mk_vault(tmp_path, notes)
    xlsx, meta = _mk_timeline(tmp_path)
    return notes, vault, xlsx, meta


# ---------------------------------------------------------------------------
# 1. 三态判定
# ---------------------------------------------------------------------------

def test_all_fresh(tmp_path):
    notes, vault, xlsx, meta = _full_env(tmp_path)
    rep = _check(notes, vault, xlsx, meta)
    assert [it.state for it in rep.items] == [F.FRESH] * 4
    assert rep.n_stale == 0


def test_behind_within_grace_is_pending_not_fresh(tmp_path):
    """源侧刚变：绝不显示新鲜（说谎），也不判陈旧（月度链路的狼来了）——第三态。"""
    notes = _mk_notes(tmp_path, index_stamp=STAMP_RECENT)
    _mk_store(notes, source=STAMP_OLD)
    rep = _check(notes)
    it = _item(rep, "embed")
    assert it.state == F.PENDING
    assert "分钟前" in it.detail


def test_behind_beyond_grace_is_stale_with_job_hint(tmp_path):
    notes = _mk_notes(tmp_path, index_stamp=STAMP_OLD)
    _mk_store(notes, source="2026-08-01T00:00:00")
    rep = _check(notes)
    it = _item(rep, "embed")
    assert it.state == F.STALE
    assert "com.xlbd.scholar-embed" in it.detail and "cron_embed.err.log" in it.detail


def test_grace_override_zero_forces_stale(tmp_path):
    """验收口径：--grace-seconds 0 把「源侧刚变」的未判定压成陈旧。"""
    notes = _mk_notes(tmp_path, index_stamp=STAMP_RECENT)
    _mk_store(notes, source=STAMP_OLD)
    rep = _check(notes, grace_overrides={k: 0 for k in F.GRACE_DEFAULTS})
    assert _item(rep, "embed").state == F.STALE


# ---------------------------------------------------------------------------
# 2. embed 子项边界
# ---------------------------------------------------------------------------

def test_embed_db_missing_is_pending(tmp_path):
    notes = _mk_notes(tmp_path)
    rep = _check(notes)
    it = _item(rep, "embed")
    assert it.state == F.PENDING and "从未建库" in it.detail


def test_embed_locked_is_pending(tmp_path):
    notes = _mk_notes(tmp_path)
    db = _mk_store(notes)
    holder = sqlite3.connect(db)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        meta, err = F.read_store_meta(db)
        assert meta is None and err == "locked"
        rep = _check(notes)
        it = _item(rep, "embed")
        assert it.state == F.PENDING and "locked" in it.detail
    finally:
        holder.rollback()
        holder.close()


def test_embed_heartbeat_dead_escalates_even_within_grace(tmp_path):
    """embed 例外：built_at 是可信心跳（每次 sync 无条件刷新+RunAtLoad）——
    落后 ∧ 心跳超龄 → 直接陈旧，哪怕源侧刚变。"""
    notes = _mk_notes(tmp_path, index_stamp=STAMP_RECENT)
    _mk_store(notes, source=STAMP_OLD,
              built=(NOW - timedelta(days=8)).isoformat(timespec="seconds"))
    rep = _check(notes)
    it = _item(rep, "embed")
    assert it.state == F.STALE and "停跑" in it.detail


def test_embed_old_heartbeat_but_fresh_is_fine(tmp_path):
    """三周没登录且索引没动：built_at 冻结但派生物不落后 → 新鲜，心跳不误报。"""
    notes = _mk_notes(tmp_path)
    _mk_store(notes, built=(NOW - timedelta(days=21)).isoformat(timespec="seconds"))
    rep = _check(notes)
    assert _item(rep, "embed").state == F.FRESH


def test_index_stamp_unreadable_is_pending(tmp_path):
    notes = tmp_path / "notes"
    (notes / "topics").mkdir(parents=True)
    (notes / "literature_index.json").write_text("{broken", encoding="utf-8")
    (notes / "all_references.json").write_text("[]", encoding="utf-8")
    _mk_store(notes)
    rep = _check(notes)
    assert _item(rep, "embed").state == F.PENDING


# ---------------------------------------------------------------------------
# 3. vault 子项：双戳逐分量 + 死 job 诊断
# ---------------------------------------------------------------------------

def test_vault_topics_component_judged_by_its_own_age(tmp_path):
    """逐分量：索引分量同步、topics 分量刚写 → 未判定（用 topics mtime 自己的年龄，
    不用索引戳年龄——混算会把月度链路里的健康 vault 判陈旧）。"""
    notes, vault, _x, _m = _full_env(tmp_path)
    fresh_page = notes / "topics" / "b.md"
    fresh_page.write_text("刚写的", encoding="utf-8")   # mtime=now，grace 内
    rep = _check(notes, vault)
    it = _item(rep, "vault")
    assert it.state == F.PENDING
    assert "topics 分量" in it.detail


def test_vault_topics_component_stale_lists_lagging_pages(tmp_path):
    notes, vault, _x, _m = _full_env(tmp_path)
    lag_page = notes / "topics" / "c.md"
    lag_page.write_text("一小时前写的", encoding="utf-8")
    # 比 vault 快照（_full_env 的 a.md：NOW-7200）新，但距 NOW 一小时 > grace 600
    old = NOW.timestamp() - 3600
    os.utime(lag_page, (old, old))
    # vault 快照落后（generated_at 设新，避免触发死 job 诊断分支）
    rep = _check(notes, vault)
    it = _item(rep, "vault")
    assert it.state == F.STALE
    assert "c.md" in it.detail and "com.xlbd.scholar-vault" in it.detail


def test_vault_meta_corrupted_is_pending(tmp_path):
    """裸 write_text 非原子：读到半截 JSON → 未判定，绝不误报陈旧、绝不抛异常。"""
    notes, vault, _x, _m = _full_env(tmp_path)
    (vault / "_meta.json").write_text('{"source_index', encoding="utf-8")
    rep = _check(notes, vault)
    it = _item(rep, "vault")
    assert it.state == F.PENDING and "不可读" in it.detail


def test_vault_topics_mtime_wrong_type_is_pending(tmp_path):
    notes, vault, _x, _m = _full_env(tmp_path)
    meta = json.loads((vault / "_meta.json").read_text(encoding="utf-8"))
    meta["source_topics_mtime"] = "not-a-float"
    (vault / "_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    rep = _check(notes, vault)
    it = _item(rep, "vault")
    assert it.state == F.PENDING and "类型异常" in it.detail


def test_vault_dir_none_is_pending(tmp_path):
    notes = _mk_notes(tmp_path)
    rep = _check(notes, vault=None)
    assert _item(rep, "vault").state == F.PENDING


def test_dead_job_probe_missing_escalates(tmp_path):
    """落后 + self 戳 8 天 + launchctl 查无 label → 陈旧「job 已卸载」。"""
    notes, _v, _x, _m = _full_env(tmp_path)
    vault = _mk_vault(tmp_path, notes, src_idx="2026-08-01T00:00:00",
                      generated_at=(NOW - timedelta(days=8)).isoformat(timespec="seconds"))
    rep = _check(notes, vault, probe=lambda label: ("missing", ""))
    it = _item(rep, "vault")
    assert it.state == F.STALE and "已卸载" in it.detail


def test_dead_job_probe_loaded_keeps_verdict_with_note(tmp_path):
    """job 在位：维持原判（这里索引分量刚变 → 未判定），附上次真跑与退出码。"""
    notes = _mk_notes(tmp_path, index_stamp=STAMP_RECENT)
    page = notes / "topics" / "a.md"
    page.write_text("x", encoding="utf-8")
    old = NOW.timestamp() - 7200
    os.utime(page, (old, old))
    vault = _mk_vault(tmp_path, notes, src_idx=STAMP_OLD,
                      generated_at=(NOW - timedelta(days=9)).isoformat(timespec="seconds"))
    rep = _check(notes, vault, probe=lambda label: ("loaded", "上次退出码 3"))
    it = _item(rep, "vault")
    assert it.state == F.PENDING
    assert "job 在位" in it.detail and "退出码 3" in it.detail


def test_dead_job_probe_unavailable_keeps_verdict(tmp_path):
    """Linux CI / SSH 非 gui：诊断不可用 → 维持原判附注，不升格不崩。"""
    notes, _v, _x, _m = _full_env(tmp_path)
    vault = _mk_vault(tmp_path, notes, src_idx="2026-08-01T00:00:00",
                      generated_at=(NOW - timedelta(days=8)).isoformat(timespec="seconds"))
    rep = _check(notes, vault, probe=lambda label: ("unavailable", ""))
    it = _item(rep, "vault")
    assert it.state == F.STALE            # 原判本就是陈旧（超 grace），不因诊断不可用而降
    assert "诊断不可用" in it.detail


# ---------------------------------------------------------------------------
# 4. xlsx 子项
# ---------------------------------------------------------------------------

def test_xlsx_sidecar_without_body_is_stale(tmp_path):
    notes = _mk_notes(tmp_path)
    xlsx, meta = _mk_timeline(tmp_path, with_xlsx=False)
    rep = _check(notes, xlsx=xlsx, meta=meta)
    it = _item(rep, "xlsx")
    assert it.state == F.STALE and "本体不存在" in it.detail


def test_xlsx_never_exported_is_pending(tmp_path):
    notes = _mk_notes(tmp_path)
    rep = _check(notes, xlsx=tmp_path / "nope.xlsx", meta=tmp_path / ".nope.meta.json")
    it = _item(rep, "xlsx")
    assert it.state == F.PENDING and "从未导出" in it.detail


def test_xlsx_behind_beyond_grace_is_stale(tmp_path):
    notes = _mk_notes(tmp_path)
    xlsx, meta = _mk_timeline(tmp_path, src="2026-08-01T00:00:00")
    rep = _check(notes, xlsx=xlsx, meta=meta,
                 probe=lambda label: ("unavailable", ""))
    assert _item(rep, "xlsx").state == F.STALE


# ---------------------------------------------------------------------------
# 5. refs 子项
# ---------------------------------------------------------------------------

def test_refs_missing_is_stale(tmp_path):
    notes = _mk_notes(tmp_path, with_refs=False)
    rep = _check(notes)
    it = _item(rep, "refs")
    assert it.state == F.STALE and "不存在" in it.detail


def test_refs_corrupt_is_stale(tmp_path):
    notes = _mk_notes(tmp_path)
    (notes / "all_references.json").write_text("[broken", encoding="utf-8")
    rep = _check(notes)
    it = _item(rep, "refs")
    assert it.state == F.STALE and "损坏" in it.detail


# ---------------------------------------------------------------------------
# 6. 输出纪律（回归：违者污染既有告警链）
# ---------------------------------------------------------------------------

def _worst_case_report(tmp_path):
    """四项全陈旧的报告，文本面最大化。"""
    notes = _mk_notes(tmp_path, index_stamp=STAMP_OLD, with_refs=False)
    _mk_store(notes, source="2026-08-01T00:00:00",
              built=(NOW - timedelta(days=9)).isoformat(timespec="seconds"))
    page = notes / "topics" / "lag.md"
    page.write_text("x", encoding="utf-8")
    old = NOW.timestamp() - 7200
    os.utime(page, (old, old))
    vault = _mk_vault(tmp_path, notes, src_idx="2026-08-01T00:00:00", src_tm=1.0,
                      generated_at=(NOW - timedelta(days=9)).isoformat(timespec="seconds"))
    xlsx, meta = _mk_timeline(tmp_path, with_xlsx=False)
    return _check(notes, vault, xlsx=xlsx, meta=meta,
                  probe=lambda label: ("missing", ""))


def test_output_bans_reserved_prefixes_and_ack_id_shape(tmp_path):
    """禁用 🚨（撤稿通知按前缀拼）、⏳（stale 节专属）、反引号#ID（会被当 ack ID）。"""
    rep = _worst_case_report(tmp_path)
    blob = rep.render_block(now=NOW) + "\n" + "\n".join(rep.stdout_lines())
    assert "🚨" not in blob
    assert "⏳" not in blob
    assert not L.ids_in_text(blob), "freshness 输出不得出现 `#…` 形态（会污染 ack 全集）"


def test_stdout_prefix_contract(tmp_path):
    """只有陈旧行用 🧭⚠（触发低音量通知），其余 🧭。"""
    rep = _worst_case_report(tmp_path)
    assert rep.n_stale == 4
    for ln in rep.stdout_lines():
        assert ln.startswith("🧭⚠")
    notes2, vault2, xlsx2, meta2 = _full_env(tmp_path / "fresh")
    rep2 = _check(notes2, vault2, xlsx2, meta2)
    for ln in rep2.stdout_lines():
        assert ln.startswith("🧭") and not ln.startswith("🧭⚠")


# ---------------------------------------------------------------------------
# 7. 与 lint 报告的拼接 / frontmatter / 告警面契约
# ---------------------------------------------------------------------------

def _minimal_body():
    """带四个 section 标记的最小 body（模拟 render_lint_report 的产出形状）。"""
    lines = ["# 知识层 lint 报告", "", "> 状态行占位", ""]
    for key in L.LINT_SECTIONS:
        lines += [L.LINT_SECTION_MARK.format(key, NOW.isoformat(timespec="seconds")),
                  L.SECTION_HEADING[key], "", "内容占位", ""]
    return "\n".join(lines)


def test_insert_block_lands_before_first_marker_and_never_carries():
    body = L._insert_freshness_block(_minimal_body(), "## 🧭 派生物新鲜度\n\n- 占位")
    first_marker = body.index("<!-- LINT-SECTION")
    assert body.index("派生物新鲜度") < first_marker
    # 结转视角：freshness 文本在首标记前 → 不属于任何 section，永不被结转
    secs = L.split_lint_sections(body)
    assert set(secs) == set(L.LINT_SECTIONS)
    for _ran, text in secs.values():
        assert "派生物新鲜度" not in text


def test_write_report_freshness_roundtrip_and_skip_key_absent(tmp_path):
    """三连跑（跑→跳→跳）：跑轮写块与计数键；跳轮块缺席、整键缺席（preserved
    穿透被显式剔除——这正是 build_lint_frontmatter 那段注释防的化石）。"""
    from src.scholar.vault import split_frontmatter
    topics = tmp_path / "topics"
    topics.mkdir()
    body = _minimal_body()
    # 第 1 跑：带 freshness
    path, status = L.write_lint_report(topics, body, L.LintCounts(), now=NOW,
                                       freshness_block="## 🧭 派生物新鲜度\n\n- ⚠ 占位陈旧",
                                       freshness_stale=2)
    text = path.read_text(encoding="utf-8")
    fm, _b = split_frontmatter(text)
    assert fm["n_freshness_stale"] == 2
    assert "派生物新鲜度" in text
    # 第 2 跳：块与键都必须缺席（不能靠 preserved 复活旧值）
    path, status = L.write_lint_report(topics, body, L.LintCounts(), now=NOW)
    text = path.read_text(encoding="utf-8")
    fm, _b = split_frontmatter(text)
    assert "n_freshness_stale" not in fm
    assert "派生物新鲜度" not in text
    # 第 3 跳：保持缺席（防第 2 轮只是碰巧）
    path, _ = L.write_lint_report(topics, body, L.LintCounts(), now=NOW)
    fm, _b = split_frontmatter(path.read_text(encoding="utf-8"))
    assert "n_freshness_stale" not in fm
    # 四节的 checks_ran_at 全程不含 freshness（块无标记）
    assert set(fm["checks_ran_at"]) == set(L.LINT_SECTIONS)


def test_summarize_rc0_scans_stale_lines_positive_and_negative():
    """告警面：rc0 也扫 🧭⚠；全新鲜的 🧭 行绝不触发（负例是防"每月都弹"的关键）。"""
    out = L.summarize_lint_run("🧭 向量库：新鲜\n🧭⚠ vault：陈旧——落后 3 天\n", "", 0)
    assert out.ok and out.freshness_alert
    assert "vault" in out.freshness_alert
    neg = L.summarize_lint_run("🧭 向量库：新鲜\n🧭 vault：新鲜\n", "", 0)
    assert neg.ok and neg.freshness_alert == ""
    # rc1（撤稿）时字段并存，互不挤占
    both = L.summarize_lint_run("🚨 已撤稿：[@x]\n🧭⚠ vault：陈旧\n", "", 1)
    assert both.alert and both.freshness_alert


# ---------------------------------------------------------------------------
# 8. CLI 层：生产守卫（tmp 库绝不读真实桌面/vault/生产派生物）
# ---------------------------------------------------------------------------

def test_cli_guard_skips_freshness_on_non_production_library(tmp_path, monkeypatch, capsys):
    """tmp notes_dir：freshness 整块不执行（比"全部未判定"更强的隔离——报告逐字节
    与改动前一致，191 个既有 CLI 用例的零改动承诺靠它成立），stdout 说明原因。"""
    import scripts.lint_notes as LN
    notes = tmp_path / "notes"
    topics = notes / "topics"
    topics.mkdir(parents=True)
    (notes / "literature_index.json").write_text(
        json.dumps({"generated_at": STAMP_OLD, "papers": []}), encoding="utf-8")
    env = tmp_path / "t.env"
    env.write_text("LLM__PROVIDER=gemini\nLLM__GEMINI_API_KEY=FAKE\nLLM__MODEL=m\n"
                   "GMAIL__CREDENTIALS_PATH=f/c.json\nGMAIL__TOKEN_PATH=f/t.json\n"
                   "PROCESSING__NOTES_DIR={}\n".format(notes), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["lint_notes.py", "--config", str(env),
                                      "--offline", "--skip-contradictions",
                                      "--skip-stale", "--skip-coverage"])
    assert LN.main() == 0
    outtext = capsys.readouterr().out
    assert "非生产库" in outtext
    report = (topics / "_lint.md").read_text(encoding="utf-8")
    assert "派生物新鲜度" not in report
    assert "n_freshness_stale" not in report


def test_cli_skip_freshness_flag_parses(tmp_path, monkeypatch, capsys):
    import scripts.lint_notes as LN
    notes = tmp_path / "notes"
    (notes / "topics").mkdir(parents=True)
    (notes / "literature_index.json").write_text(
        json.dumps({"generated_at": STAMP_OLD, "papers": []}), encoding="utf-8")
    env = tmp_path / "t.env"
    env.write_text("LLM__PROVIDER=gemini\nLLM__GEMINI_API_KEY=FAKE\nLLM__MODEL=m\n"
                   "GMAIL__CREDENTIALS_PATH=f/c.json\nGMAIL__TOKEN_PATH=f/t.json\n"
                   "PROCESSING__NOTES_DIR={}\n".format(notes), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["lint_notes.py", "--config", str(env),
                                      "--offline", "--skip-contradictions",
                                      "--skip-stale", "--skip-coverage",
                                      "--skip-freshness", "--grace-seconds", "0"])
    assert LN.main() == 0
    assert "非生产库" not in capsys.readouterr().out   # skip 时守卫消息也不打


# ---------------------------------------------------------------------------
# 9. plist 文本级回归（照 test_backfill_abstracts 的先例：不用 plistlib——
#    模板注释里的 `--vault-dir` 等双连字符会被 expat 拒解析）
# ---------------------------------------------------------------------------

def test_vault_plist_watches_index_topics_and_qa():
    """W5 完整修复的防回滚：三条 WatchPaths 缺一不可（目录 watch 不递归，
    少 qa/ 那条 = 归档问答永远到不了 Obsidian，事故原样复发）。"""
    plist = (Path(__file__).resolve().parents[1] /
             "config" / "launchd" / "com.xlbd.scholar-vault.plist").read_text(
                 encoding="utf-8")
    for needle in (
        "__REPO_ROOT__/output/scholar_notes/literature_index.json",
        "__REPO_ROOT__/output/scholar_notes/topics</string>",
        "__REPO_ROOT__/output/scholar_notes/topics/qa</string>",
    ):
        assert needle in plist, "vault plist WatchPaths 缺：{}".format(needle)


# ---------------------------------------------------------------------------
# 10. 第 1 轮审计修复的回归
# ---------------------------------------------------------------------------

def test_grace_override_bad_values_never_raise(tmp_path):
    """铁律回归：grace_overrides 的 None/bool/NaN 值被忽略保默认，绝不 TypeError。"""
    notes = _mk_notes(tmp_path, index_stamp=STAMP_RECENT)
    _mk_store(notes, source=STAMP_OLD)
    rep = _check(notes, grace_overrides={"embed": None, "vault": True,
                                         "xlsx": float("nan"), "unknown": 0})
    assert _item(rep, "embed").state == F.PENDING   # 默认 1800s 生效，未被 None 顶掉


def test_newline_in_filename_cannot_inject_markers(tmp_path):
    """POSIX 文件名可含换行：伪 LINT-SECTION 标记/伪 🚨 行必须被 detail 单行化洗掉。"""
    notes, _v, _x, _m = _full_env(tmp_path)
    evil = notes / "topics" / "b\n<!-- LINT-SECTION fake ran_at=2099-01-01T00:00:00 -->\n🚨 假撤稿.md"
    evil.write_text("x", encoding="utf-8")
    old = NOW.timestamp() - 7200
    os.utime(evil, (old, old))
    vault = _mk_vault(tmp_path, notes, src_tm=1.0)   # topics 分量落后 → 陈旧列落后页
    rep = _check(notes, vault, probe=lambda label: ("unavailable", ""))
    blob = rep.render_block(now=NOW) + "\n" + "\n".join(rep.stdout_lines())
    for line in blob.splitlines():
        assert not line.strip().startswith("🚨")
        assert not L._LINT_SECTION_RE.match(line.strip())
    # 端到端：插进报告后 checks_ran_at 不被伪标记穿透
    body = L._insert_freshness_block(_minimal_body(), rep.render_block(now=NOW))
    assert set(L.split_lint_sections(body)) == set(L.LINT_SECTIONS)


def test_single_clock_now_injection(tmp_path):
    """单钟回归：epoch 侧年龄从注入的 now 推导，不再独立取 time.time()。"""
    notes, vault, xlsx, meta = _full_env(tmp_path)
    lag = notes / "topics" / "z.md"
    lag.write_text("x", encoding="utf-8")
    old = time.time() - 100                          # 真实时钟下 100 秒前
    os.utime(lag, (old, old))
    future = datetime.fromtimestamp(time.time() + 7200)   # 注入 2 小时后的 now
    rep = F.check_freshness(notes / "literature_index.json", notes, vault, xlsx, meta,
                            now=future, job_probe=lambda label: ("unavailable", ""))
    # 注入未来 now 后 topics 分量年龄 ≈ 2 小时 > grace600 → 陈旧（双钟分叉时会误判未判定）
    assert _item(rep, "vault").state == F.STALE


def test_inline_fake_gen_end_sentinel_is_neutralized(tmp_path):
    """第 2 轮审计：**行内**伪 GEN_END 哨兵不需要换行——vault.extract_user_zone 用
    substring find，下一轮 merge 会在伪哨兵处截断把生成块尾巴复制进用户区。
    _oneline 必须中和 HTML 注释定界符。"""
    from src.scholar.vault import GEN_END
    notes, _v, _x, _m = _full_env(tmp_path)
    evil = notes / "topics" / ("x " + GEN_END + " y.md")
    evil.write_text("x", encoding="utf-8")
    old = NOW.timestamp() - 3600
    os.utime(evil, (old, old))
    vault = _mk_vault(tmp_path, notes, src_tm=1.0)
    rep = _check(notes, vault, probe=lambda label: ("unavailable", ""))
    blob = rep.render_block(now=NOW) + "\n".join(rep.stdout_lines())
    assert GEN_END not in blob
    assert "<!--" not in blob and "-->" not in blob


# ---------------------------------------------------------------------------
# 11. 备份快照子项（阶段 3：backup_dir=None 时整项缺席，兼容此前全部用例）
# ---------------------------------------------------------------------------

def test_backup_item_absent_without_dir(tmp_path):
    notes, vault, xlsx, meta = _full_env(tmp_path)
    rep = _check(notes, vault, xlsx, meta)
    assert not any(it.key == "backup" for it in rep.items)


def test_backup_item_fresh_stale_pending(tmp_path):
    from src.scholar import backup_naming as bn
    notes, vault, xlsx, meta = _full_env(tmp_path)
    bdir = tmp_path / "backups"
    # 目录不存在 → 未判定
    rep = _check(notes, vault, xlsx, meta, backup_dir=bdir)
    it = _item(rep, "backup")
    assert it.state == F.PENDING and "不存在" in it.detail
    # 空目录 → 未判定（首份未产生）
    bdir.mkdir()
    rep = _check(notes, vault, xlsx, meta, backup_dir=bdir)
    assert _item(rep, "backup").state == F.PENDING
    # 3 天前的快照 → 新鲜
    ts3 = NOW - timedelta(days=3)
    (bdir / (bn.SNAPSHOT_PREFIX + bn.format_ts(ts3) + bn.TAR_SUFFIX)).write_bytes(b"x")
    rep = _check(notes, vault, xlsx, meta, backup_dir=bdir)
    assert _item(rep, "backup").state == F.FRESH
    # 15 天前（唯一一份）→ 陈旧，带责任 job；按**文件名**判龄不开内容
    (bdir / (bn.SNAPSHOT_PREFIX + bn.format_ts(ts3) + bn.TAR_SUFFIX)).unlink()
    ts15 = NOW - timedelta(days=15)
    (bdir / (bn.SNAPSHOT_PREFIX + bn.format_ts(ts15) + bn.TAR_SUFFIX)).write_bytes(b"x")
    rep = _check(notes, vault, xlsx, meta, backup_dir=bdir)
    it = _item(rep, "backup")
    assert it.state == F.STALE
    assert "com.xlbd.scholar-backup" in it.detail and "缺份" in it.detail


def test_backup_item_counts_icloud_placeholder(tmp_path):
    """驱逐占位符视同存在：只认名不认内容，绝不触发 iCloud 同步。"""
    from src.scholar import backup_naming as bn
    notes, vault, xlsx, meta = _full_env(tmp_path)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    ts = NOW - timedelta(days=2)
    (bdir / ("." + bn.SNAPSHOT_PREFIX + bn.format_ts(ts) + bn.TAR_SUFFIX + ".icloud")
     ).write_bytes(b"")
    rep = _check(notes, vault, xlsx, meta, backup_dir=bdir)
    assert _item(rep, "backup").state == F.FRESH
