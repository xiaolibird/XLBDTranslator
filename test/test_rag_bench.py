# -*- coding: utf-8 -*-
"""rag_bench.run_one 的退出码契约测试（2026-08-21 第 2 轮对抗审计修复）。

缺陷背景：notes_search.py 曾无全局异常兜底，任何未捕获异常（配置炸启动/依赖缺失）
都以 Python 默认退出码 1 结束，与"无命中"契约撞车——bench 把全部 case 记 miss，
打印 0% 假基线且退出码 0。修复是双向收紧：
- notes_search cli_entry 把未预期异常转独立退出码 4；
- rag_bench 对 exit 1 强校验 stdout 必须是合法 JSON（--json 模式真无命中也会打印
  results:[] 的 JSON），非 JSON 即判环境失败 raise；timeout 转 case 级 error。
"""
import json
import subprocess
import types

import pytest

import scripts.notes_search as ns
import scripts.rag_bench as rb


def _fake_proc(returncode, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _patch_run(monkeypatch, proc=None, exc=None):
    def fake_run(*a, **kw):
        if exc is not None:
            raise exc
        return proc
    monkeypatch.setattr(rb.subprocess, "run", fake_run)


def test_exit1_with_valid_json_is_true_no_hit(monkeypatch):
    """exit 1 + 合法 JSON（results:[]）= 真无命中，计入成绩。"""
    stdout = json.dumps({"total": 0, "shown": 0, "results": []})
    _patch_run(monkeypatch, proc=_fake_proc(1, stdout=stdout))
    ranked, err = rb.run_one("q", "hybrid", "auto", 10)
    assert ranked == [] and err is None


def test_exit1_with_non_json_stdout_raises_env_failure(monkeypatch):
    """exit 1 且 stdout 非 JSON = notes_search 崩溃（Python 默认退出码），必须 raise
    而不是记 miss——否则异地/CI 产出 0% 假基线且退出码 0。"""
    _patch_run(monkeypatch, proc=_fake_proc(
        1, stdout="", stderr="Traceback (most recent call last): ..."))
    with pytest.raises(RuntimeError, match="疑似崩溃"):
        rb.run_one("q", "hybrid", "auto", 10)


def test_exit4_crash_sentinel_raises_env_failure(monkeypatch):
    """notes_search 兜底层的 exit 4（及一切未知退出码）判环境失败。"""
    _patch_run(monkeypatch, proc=_fake_proc(4, stderr="notes_search 未预期内部错误"))
    with pytest.raises(RuntimeError, match="崩溃"):
        rb.run_one("q", "hybrid", "auto", 10)
    _patch_run(monkeypatch, proc=_fake_proc(7, stderr="?"))
    with pytest.raises(RuntimeError, match="崩溃"):
        rb.run_one("q", "hybrid", "auto", 10)


def test_exit23_still_env_failure(monkeypatch):
    """原有契约不回归：2/3（向量库/Ollama）仍是环境失败。"""
    for code in (2, 3):
        _patch_run(monkeypatch, proc=_fake_proc(code, stderr="库缺失"))
        with pytest.raises(RuntimeError, match="环境失败"):
            rb.run_one("q", "hybrid", "auto", 10)


def test_timeout_becomes_case_error_not_crash(monkeypatch):
    """TimeoutExpired 不再裸 traceback 炸掉整个 bench，转该 case 的 error 记录。"""
    _patch_run(monkeypatch, exc=subprocess.TimeoutExpired(cmd=["x"], timeout=120))
    ranked, err = rb.run_one("q", "hybrid", "auto", 10)
    assert ranked is None and "超时" in err


def test_exit0_hits_parsed(monkeypatch):
    """exit 0 正常命中路径不回归。"""
    stdout = json.dumps({"results": [{"citekey": "abc2024"}, {"citekey": "def2025"}]})
    _patch_run(monkeypatch, proc=_fake_proc(0, stdout=stdout))
    ranked, err = rb.run_one("q", "hybrid", "auto", 10)
    assert ranked == ["abc2024", "def2025"] and err is None


# ---------- notes_search.cli_entry 兜底层 ----------

def test_cli_entry_unexpected_exception_exits_4(monkeypatch, capsys):
    """未预期异常 → 4（不是 Python 默认的 1，避免与"无命中"撞车），traceback 上 stderr。"""
    monkeypatch.setattr(ns, "main", lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert ns.cli_entry() == 4
    err = capsys.readouterr().err
    assert "boom" in err and "exit 4" in err


def test_cli_entry_keyboard_interrupt_exits_130(monkeypatch, capsys):
    monkeypatch.setattr(ns, "main", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert ns.cli_entry() == 130


def test_cli_entry_passes_through_normal_return(monkeypatch):
    monkeypatch.setattr(ns, "main", lambda: 1)
    assert ns.cli_entry() == 1
    monkeypatch.setattr(ns, "main", lambda: 0)
    assert ns.cli_entry() == 0


# ---------------- --rerank 三档透传（A/B 对照的命令行接线）----------------

def _capture_cmd(monkeypatch):
    """桩 subprocess.run，记录 rag_bench 拼出的 notes_search 命令行。"""
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(list(cmd))
        return _fake_proc(0, stdout=json.dumps({"results": []}))
    monkeypatch.setattr(rb.subprocess, "run", fake_run)
    return cmds


def test_rerank_auto_passes_no_flag(monkeypatch):
    """auto=跟随 notes_search 自己的默认：命令行上不出现任何 rerank 开关。"""
    cmds = _capture_cmd(monkeypatch)
    rb.run_one("q", "hybrid", "auto", 10, rerank="auto")
    assert "--rerank" not in cmds[0] and "--no-rerank" not in cmds[0]


def test_rerank_default_param_is_auto(monkeypatch):
    """不传 rerank 参数 = auto（锁默认值，防止改签名时悄悄变成强制开/关）。"""
    cmds = _capture_cmd(monkeypatch)
    rb.run_one("q", "hybrid", "auto", 10)
    assert "--rerank" not in cmds[0] and "--no-rerank" not in cmds[0]


def test_rerank_on_passes_rerank_flag(monkeypatch):
    cmds = _capture_cmd(monkeypatch)
    rb.run_one("q", "dense", "auto", 10, rerank="on")
    assert "--rerank" in cmds[0] and "--no-rerank" not in cmds[0]


def test_rerank_off_passes_no_rerank_flag(monkeypatch):
    cmds = _capture_cmd(monkeypatch)
    rb.run_one("q", "hybrid", "auto", 10, rerank="off")
    assert "--no-rerank" in cmds[0] and "--rerank" not in cmds[0]


# ---------------- nDCG@10 / MRR（对齐 BEIR/MTEB 口径）----------------

def test_ndcg_perfect_and_empty():
    """gold 全排最前 = 1.0；一条没中 = 0.0；空输入不炸。"""
    assert rb.ndcg_at_k(["a", "b", "c"], ["a"], 10) == 1.0
    assert rb.ndcg_at_k(["a", "b"], ["a", "b"], 10) == 1.0      # IDCG 按 min(len(gold),k) 算
    assert rb.ndcg_at_k(["x", "y"], ["a"], 10) == 0.0
    assert rb.ndcg_at_k([], ["a"], 10) == 0.0
    assert rb.ndcg_at_k(["a"], [], 10) == 0.0


def test_ndcg_is_sensitive_where_hit_at_k_is_blind():
    """这才是补 nDCG 的理由：hit@5 对档内名次挪动完全失明。

    两个排序的 gold 都落在前 5，hit@1 与 hit@5 给出**完全相同**的数字，
    而 nDCG 能分出高下。本项目两次修复复盘都撞上过"@1/@5 全不变、逐 case
    却有 5 处名次变动"，那些变动的方向过去无法裁决。
    """
    better = rb.ndcg_at_k(["x", "gold", "y", "z", "w"], ["gold"], 10)
    worse = rb.ndcg_at_k(["x", "y", "z", "w", "gold"], ["gold"], 10)
    assert better > worse                       # nDCG 分得出
    hit5 = lambda ranked: any(g in ranked[:5] for g in ["gold"])
    assert hit5(["x", "gold", "y", "z", "w"]) == hit5(["x", "y", "z", "w", "gold"])  # hit@5 分不出


def test_ndcg_respects_cutoff_k():
    """k 之外的命中不计分——limit=10 时第 11 位的 gold 等于没召回。"""
    assert rb.ndcg_at_k(["x"] * 10 + ["gold"], ["gold"], 10) == 0.0
    assert rb.ndcg_at_k(["x"] * 9 + ["gold"], ["gold"], 10) > 0.0
