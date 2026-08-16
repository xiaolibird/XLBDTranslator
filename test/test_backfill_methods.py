# -*- coding: utf-8 -*-
"""`scripts/backfill_methods.py`：给存量精读回填「实验方法」节。

这批用例钉的都是回填时**实际踩到**的坑，不是假想边界：
  · 校验器死磕 "(p." 字面量，把手写那篇的「（Methods p.12）」误判成没有页码锚；
  · 产出漏掉整个「代码与数据可得性」维度（原文 p.11 明写 GitHub + Zenodo）；
  · `--force` 原本是往 sections 里再插一节，导致同一篇出现两个「实验方法」。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _load():
    spec = importlib.util.spec_from_file_location(
        "backfill_methods", REPO / "scripts" / "backfill_methods.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


def _sent(text, tag=None):
    return {"text": text, "tag": tag}


def _ok_sents():
    """一组能通过全部校验的最小产出。"""
    return [
        _sent("【队列与划分】训练集 52,695 例、验证集 13,174 例(p.8)。", "可引用证据"),
        _sent("【模型与超参】Adam，20 epoch，batch 256，lr 1e-2(p.10)。", "可引用证据"),
        _sent("【代码与数据可得性】代码见 https://example.org/repo (p.11)。", "可引用证据"),
        _sent("【原文未报告】随机种子、硬件型号、训练耗时。"),
    ]


# ---------------- parse_sentences ----------------

def test_parse_sentences_接受裸_json():
    resp = json.dumps({"sentences": [{"text": "【队列与划分】n=100(p.3)。", "tag": "可引用证据"}]},
                      ensure_ascii=False)
    out = M.parse_sentences(resp)
    assert out == [{"text": "【队列与划分】n=100(p.3)。", "tag": "可引用证据"}]


def test_parse_sentences_剥代码围栏():
    body = json.dumps({"sentences": [{"text": "【基线】以 LASSO 为基线(p.5)。", "tag": None}]},
                      ensure_ascii=False)
    out = M.parse_sentences("```json\n" + body + "\n```")
    assert len(out) == 1


def test_parse_sentences_非法_tag_归_none():
    resp = json.dumps({"sentences": [{"text": "【评估协议】五折交叉验证(p.7)。", "tag": "随便编的"}]},
                      ensure_ascii=False)
    assert M.parse_sentences(resp)[0]["tag"] is None


def test_parse_sentences_丢掉过短的空转句():
    """「无」「N/A」这类占位句进了库就是噪声，索引会把它们聚合进 highlights。"""
    resp = json.dumps({"sentences": [
        {"text": "无", "tag": None},
        {"text": "【队列与划分】样本量 1200 例(p.4)。", "tag": "可引用证据"},
    ]}, ensure_ascii=False)
    assert len(M.parse_sentences(resp)) == 1


@pytest.mark.parametrize("resp", ["", "这不是 JSON", "{}", json.dumps({"sentences": "x"})])
def test_parse_sentences_坏输入返回空表(resp):
    assert M.parse_sentences(resp) == []


# ---------------- validate ----------------

def test_validate_合格产出放行():
    assert M.validate(_ok_sents()) == ""


def test_validate_句数过少不放行():
    assert "太少" in M.validate(_ok_sents()[:2])


def test_validate_缺原文未报告不放行():
    sents = [s for s in _ok_sents() if "原文未报告" not in s["text"]]
    assert "原文未报告" in M.validate(sents)


def test_validate_缺代码与数据可得性不放行():
    """实测漏过：原文 p.11 明写 GitHub + Zenodo，产出里这一维度整个消失。"""
    sents = [s for s in _ok_sents() if "代码与数据可得性" not in s["text"]]
    sents.append(_sent("【评估协议】用 ROC-AUC 与 Brier 分数(p.11)。", "可引用证据"))
    assert "代码与数据可得性" in M.validate(sents)


def test_validate_全无页码锚不放行():
    sents = [_sent(s["text"].replace("(p.", "(").replace("p.11", "第十一页"), s["tag"])
             for s in _ok_sents()]
    assert "页码锚" in M.validate(sents)


def test_validate_认_methods_p12_这种写法():
    """手写那篇用的是「（Methods p.12）」；页码锚只该认 `p.数字`，不该绑定括号写法。"""
    sents = [
        _sent("【队列与划分】糖尿病样本 93,593 人（Methods p.12）。", "可引用证据"),
        _sent("【模型与超参】LASSO，5 折交叉验证（Methods p.13）。", "可引用证据"),
        _sent("【代码与数据可得性】代码已公开：https://github.com/x/y （Methods p.14）。", "可引用证据"),
        _sent("【原文未报告】未报告随机种子；未报告硬件。"),
    ]
    assert M.validate(sents) == ""


# ---------------- fact_check（结构之外的事实回查） ----------------
#
# 缺口实锤：validate() 只看结构，页码不超界的编造（p.6 的「lr 3e-4」、伪造 GitHub URL）
# 全部放行，带「可引用证据」tag 直通 scholar-write 取证链；auto 链路的
# verify_citable_numbers 防线没盖到回填这条路。

_FACT_BODY = ("[p.8]\n训练集 52,695 例，验证集 13,174 例。\n"
              "[p.10]\nAdam, 20 epochs, batch 256, lr 1e-2.\n"
              "[p.11]\nCode: https://github.com/real/repo  Data: zenodo.org/record/123\n")


def test_fact_check_数字与URL均可溯源时放行():
    sents = [
        _sent("【队列与划分】训练集 52,695 例(p.8)。", "可引用证据"),
        _sent("【代码与数据可得性】代码见 https://github.com/real/repo (p.11)。", "可引用证据"),
        _sent("【原文未报告】随机种子、硬件。"),
    ]
    bad, demoted = M.fact_check(sents, _FACT_BODY)
    assert bad == "" and demoted == 0
    assert sents[0]["tag"] == "可引用证据"    # 合格句的 tag 不许被误摘


def test_fact_check_编造数字降级不拦截():
    """口径同 auto 链路 verify_citable_numbers：句子保留、tag 摘掉，不整批打回。
    3e-4 按科学计数法整体回查——拆成裸的 3 和 4 去查必然全中、等于没查。"""
    sents = [_sent("【模型与超参】lr 3e-4，batch 999(p.10)。", "可引用证据"),
             _sent("【原文未报告】其余各项。")]
    bad, demoted = M.fact_check(sents, _FACT_BODY)
    assert bad == ""
    assert demoted == 1 and sents[0]["tag"] is None


def test_fact_check_非可引用证据句的数字不查():
    """降级只管冒充「可引用证据」的句子；方法论借鉴句里的数字本就不进取证链。"""
    sents = [_sent("【评估协议】大约 9999 次重复实验（转述）。", "方法论借鉴")]
    bad, demoted = M.fact_check(sents, _FACT_BODY)
    assert bad == "" and demoted == 0 and sents[0]["tag"] == "方法论借鉴"


def test_fact_check_编造URL整批不合格():
    """伪造的仓库地址会被可得性设卡当成「代码可得」写进札记，必须硬拦走重试。"""
    sents = [_sent("【代码与数据可得性】代码见 https://github.com/fake/nope (p.11)。", "可引用证据")]
    bad, _ = M.fact_check(sents, _FACT_BODY)
    assert "URL" in bad and "github.com/fake/nope" in bad


def test_fact_check_折行URL去空白后仍认():
    """PDF 抽取常把长 URL 折行；去空白对照，别把真链接误杀成编造。"""
    body = "[p.11]\nCode at https://github.com/real/very-\nlong-repo-name here"
    sents = [_sent("【代码与数据可得性】代码见 https://github.com/real/very-long-repo-name (p.11)。", None)]
    bad, _ = M.fact_check(sents, body)
    assert bad == ""


def test_fact_check_模型补scheme的裸域名放行():
    """原文只写裸 zenodo.org/record/123，模型按 prompt「完整 URL」要求补了 https://——
    去 scheme 再试一次才算不中，别把照抄判成编造。"""
    sents = [_sent("【代码与数据可得性】数据见 https://zenodo.org/record/123 (p.11)。", "可引用证据")]
    bad, _ = M.fact_check(sents, _FACT_BODY)
    assert bad == ""


# ---------------- insert_section ----------------

def _bundle(headings):
    return {"close_reading_final": {
        "sections": [{"heading": h, "sentences": [_sent("占位句，够长够长。")]} for h in headings]}}


def test_insert_插在方法与数据之后():
    d = _bundle(["研究问题", "方法与数据", "结果与效应量"])
    M.insert_section(d, _ok_sents())
    assert [s["heading"] for s in d["close_reading_final"]["sections"]] == [
        "研究问题", "方法与数据", "实验方法", "结果与效应量"]


def test_insert_没有方法与数据时退到研究问题之后():
    d = _bundle(["研究问题", "结果与效应量"])
    M.insert_section(d, _ok_sents())
    assert [s["heading"] for s in d["close_reading_final"]["sections"]][1] == "实验方法"


def test_insert_锚点全无时放第二位():
    d = _bundle(["自定义节甲", "自定义节乙"])
    M.insert_section(d, _ok_sents())
    assert [s["heading"] for s in d["close_reading_final"]["sections"]] == [
        "自定义节甲", "实验方法", "自定义节乙"]


def test_insert_重跑是替换不是并排插两节():
    """--force 的语义是替换。插出两个同名节，渲染和索引都会把它当两节各算一遍。"""
    d = _bundle(["研究问题", "方法与数据", "实验方法", "结果与效应量"])
    M.insert_section(d, _ok_sents())
    headings = [s["heading"] for s in d["close_reading_final"]["sections"]]
    assert headings.count("实验方法") == 1
    assert headings == ["研究问题", "方法与数据", "实验方法", "结果与效应量"]
    # 换进去的是新内容，不是旧占位句
    sec = [s for s in d["close_reading_final"]["sections"] if s["heading"] == "实验方法"][0]
    assert sec["sentences"] == _ok_sents()


# ---------------- has_methods ----------------

def test_只回填_final_且有精读正文的_bundle():
    """draft 的 close_reading_final 是 None，插节会直接写崩；语义上也该由精读协议自己产出。

    实测撞上过：另一个会话正往同一个札记目录 ingest，落下了新的 draft bundle。
    """
    final = _bundle(["研究问题", "方法与数据"])
    final["status"] = "final"
    assert M.is_backfillable(final) is True

    draft = _bundle(["研究问题"])
    draft["status"] = "draft"
    assert M.is_backfillable(draft) is False

    assert M.is_backfillable({"status": "final", "close_reading_final": None}) is False
    assert M.is_backfillable({"status": "final", "close_reading_final": {"sections": []}}) is False


def test_has_methods():
    assert M.has_methods(_bundle(["研究问题", "实验方法"])) is True
    assert M.has_methods(_bundle(["研究问题", "方法与数据"])) is False
    assert M.has_methods({"close_reading_final": None}) is False
    assert M.has_methods({}) is False


# ---------------- 账本 ----------------

def test_账本存取往返(tmp_path):
    (tmp_path / "manual").mkdir()
    led = M.load_ledger(tmp_path)
    assert led["papers"] == {}
    led["papers"]["abc"] = {"status": "ok", "n": 9}
    M.save_ledger(tmp_path, led)
    assert M.load_ledger(tmp_path)["papers"]["abc"]["n"] == 9


def test_账本损坏时从空账本继续而不是炸掉(tmp_path):
    """223 篇跑到一半账本写坏，不该让整轮回填起不来——旧文件保留，另行排查。"""
    (tmp_path / "manual").mkdir()
    M._ledger_path(tmp_path).write_text("{坏掉的 json", encoding="utf-8")
    assert M.load_ledger(tmp_path)["papers"] == {}


# ---------------- run 收尾强制页码对账 ----------------

def test_run_收尾强制页码对账且超界非零上抛(tmp_path, monkeypatch):
    """verify 原是独立子命令，run 收尾只打印 regen 命令、既不自动跑也不提示——回填句
    插在亲读核验之后、regen 归档无人再看，编造检测全靠人记得。现在 run 必须对本轮
    写过盘的月份自动对账，且发现超界要以非零退出把 regen 挡下来。"""
    import argparse
    import types
    fitz = pytest.importorskip("fitz")

    mdir = tmp_path / "notes" / "manual" / "2026-07"
    mdir.mkdir(parents=True)
    pdf = tmp_path / "a.pdf"
    doc = fitz.open()
    page = doc.new_page()
    for i in range(20):                       # 凑过 500 字符的扫描件门槛
        page.insert_text((40, 60 + i * 14),
                         "cohort 52695 cases; code https://github.com/real/repo line {}".format(i))
    doc.save(str(pdf))
    doc.close()

    bf = mdir / "a.paper.json"
    bf.write_text(json.dumps({
        "status": "final", "month": "2026-07", "paper_id": "pid1", "pdf_path": str(pdf),
        "segment": {"metadata": {"title": "T"}},
        "close_reading_final": {"sections": [
            {"heading": "研究问题", "sentences": [_sent("占位句，够长够长。")]}]},
    }, ensure_ascii=False), encoding="utf-8")

    resp = json.dumps({"sentences": [
        {"text": "【队列与划分】共 52695 例(p.1)。", "tag": "可引用证据"},
        {"text": "【代码与数据可得性】代码见 https://github.com/real/repo (p.1)。", "tag": "可引用证据"},
        {"text": "【原文未报告】随机种子、硬件。", "tag": None},
    ]}, ensure_ascii=False)

    class _LLM:
        def __init__(self, cfg):
            pass

        def call(self, *a, **k):
            return resp

    monkeypatch.setattr(M, "_load_settings", lambda cfg: types.SimpleNamespace(
        llm=types.SimpleNamespace(closeread_model="m", model="m"),
        processing=types.SimpleNamespace(notes_dir=str(tmp_path / "notes"))))
    monkeypatch.setattr(M, "LLMClient", _LLM)

    verified = []

    def _fake_verify(vargs):
        verified.append(vargs.month)
        return 1                              # 模拟对账发现超界

    monkeypatch.setattr(M, "cmd_verify", _fake_verify)

    rc = M.cmd_run(argparse.Namespace(
        config="cfg", month="", paper=[], limit=0, workers=1, force=False,
        retry_failed=False, abort_after=5, backup_dir=str(tmp_path / "bak")))

    assert verified == ["2026-07"]            # 自动对账，且只查本轮写过盘的月份
    assert rc == 1                            # 超界要非零上抛，不能吞掉
    assert M.has_methods(json.loads(bf.read_text(encoding="utf-8")))  # 回填本身成功写盘


# ---------------- 页码锚抽取 ----------------

def test_pdf_文本带页码标记(tmp_path):
    fitz = pytest.importorskip("fitz")
    p = tmp_path / "t.pdf"
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), "PAGE-BODY-{}".format(i + 1))
    doc.save(str(p))
    doc.close()

    text, n_pages, raw = M.pdf_text_with_pages(p)
    assert n_pages == 3
    assert "[p.1]" in text and "[p.3]" in text
    assert text.index("[p.1]") < text.index("[p.2]") < text.index("[p.3]")
    assert raw > 0


def test_pdf_单页超长被截断(tmp_path):
    """实测有 45000+ 字符的矢量文字层病态页，不设限会把整篇预算吃光。"""
    fitz = pytest.importorskip("fitz")
    p = tmp_path / "big.pdf"
    doc = fitz.open()
    page = doc.new_page()
    for i in range(400):
        page.insert_text((20, 20 + i * 2), "x" * 200)
    doc.save(str(p))
    doc.close()

    text, _, raw = M.pdf_text_with_pages(p, page_max=500)
    assert raw > 500                       # 原始页确实超长
    assert len(text) < 700                 # 但送进 prompt 的被截到 page_max + 页标
