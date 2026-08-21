# -*- coding: utf-8 -*-
"""calibrate_digest_neighbors 对损坏 abstracts.json 的处置回归。

锁一件事（2026-08-21 批第 3 轮）：load_abstracts 对「存在但坏」的 sidecar 改为
raise VectorStoreError 后，本脚本必须捕获并给可操作信息退出 2，不能裸 traceback
收场；同时锁旧契约的「缺失/为空」分支仍走原有退出 2 提示。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.scholar.embed_store import ABSTRACTS_NAME  # noqa: E402


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "calibrate_digest_neighbors", REPO / "scripts" / "calibrate_digest_neighbors.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_script()


class _FakeStore:
    records = []


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """把脚本接到 tmp notes_dir：config 不存在走默认 settings，向量库 load 打桩。"""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()

    def fake_repo_path(p):
        if str(p).endswith("scholar.env"):
            return tmp_path / "no-such.env"       # 不存在 -> 默认 ScholarSettings
        return notes_dir

    monkeypatch.setattr(MOD, "repo_path", fake_repo_path)
    monkeypatch.setattr(MOD.VectorStore, "load", staticmethod(lambda _p: _FakeStore()))
    monkeypatch.setattr(sys, "argv", ["calibrate_digest_neighbors.py"])
    return notes_dir


def test_corrupt_abstracts_exits_2_not_traceback(wired, capsys):
    """存在但坏：捕获 VectorStoreError，退出 2 + 可操作提示，不裸 traceback。"""
    (wired / ABSTRACTS_NAME).write_text("{ 截半的烂JSON", encoding="utf-8")
    rc = MOD.main()          # 修复前这里直接 raise VectorStoreError
    assert rc == 2
    err = capsys.readouterr().err
    assert "存在但读不出" in err
    assert "修复" in err     # 提示指向修复 sidecar，而非无声吞掉


def test_missing_abstracts_keeps_old_exit_2(wired, capsys):
    """缺失：load_abstracts 返回空 dict，仍走原「先跑 backfill」提示退出 2。"""
    rc = MOD.main()
    assert rc == 2
    err = capsys.readouterr().err
    assert "backfill_abstracts" in err
