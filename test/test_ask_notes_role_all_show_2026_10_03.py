# -*- coding: utf-8 -*-
"""ask_notes 的三处改进（2026-10-03，由 ask-notes skill 实测暴露）。

1. `--role all`：显式清空角色限制。--reask 沿用存档里的 `--role citable`，而 argparse 的
   append 没有「清空」写法；实测一次沿用的 citable 把「标成可反驳观点但含硬数字」的句子
   整批挡掉（MDLA 的跨库缺失率数字），却无从取消。
2. `--show N`：--dry-run 列出前 N 条（默认 10）。
3. 参数回显：`_run_one` 开头打印实际生效的角色/min-sim/cap/max-evidence/-q 数。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("ask_notes_script", ROOT / "scripts" / "ask_notes.py")
AN = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AN)

from src.scholar import qa as Q  # noqa: E402


class _Args:
    def __init__(self, query=None, role=None, min_sim=AN._UNSET,
                 max_evidence=AN._UNSET, per_paper_cap=AN._UNSET):
        self.query = query
        self.role = role
        self.min_sim = min_sim
        self.max_evidence = max_evidence
        self.per_paper_cap = per_paper_cap


def _archived(**kw):
    base = dict(extra_queries=["存档词"], roles=["citable"], min_sim=0.5,
                max_evidence=28, per_paper_cap=3, has_params=True)
    base.update(kw)
    return Q.ArchivedAskParams(**base)


class TestRoleAll:
    def test_role_all_clears_archived_role_restriction(self):
        """存档里是 citable，--role all 之后变成空列表（= 三个角色都要）。"""
        _eq, roles, *_ = AN._resolve_ask_params(_Args(role=["all"]), _archived())
        assert roles == []

    def test_without_role_flag_still_inherits_archived_role(self):
        """不给 --role 仍沿用存档（既有语义不变）。"""
        _eq, roles, *_ = AN._resolve_ask_params(_Args(), _archived())
        assert roles == ["citable"]

    def test_explicit_roles_still_replace(self):
        _eq, roles, *_ = AN._resolve_ask_params(
            _Args(role=["citable", "refutable", "method"]), _archived())
        assert roles == ["citable", "refutable", "method"]

    def test_role_all_on_fresh_question_is_harmless(self):
        _eq, roles, *_ = AN._resolve_ask_params(_Args(role=["all"]), Q.ArchivedAskParams())
        assert roles == []

    def test_role_all_wins_even_if_mixed_with_others(self):
        """`--role all --role citable` 这种自相矛盾的写法：all 优先（清空限制），不报错。"""
        _eq, roles, *_ = AN._resolve_ask_params(_Args(role=["citable", "all"]), _archived())
        assert roles == []

    def test_empty_roles_is_accepted_by_build_qa_spec(self):
        """清空后传给 build_qa_spec 的是空列表，必须被接受（缺省三者都要）。"""
        spec = Q.build_qa_spec("某个问题", roles=[])
        assert spec.roles == []


class TestCliSurface:
    def _parser_help(self):
        import io
        import contextlib
        buf = io.StringIO()
        old = sys.argv
        sys.argv = ["ask_notes.py", "--help"]
        try:
            with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
                AN.main()
        finally:
            sys.argv = old
        return buf.getvalue()

    def test_help_mentions_role_all_and_show(self):
        h = self._parser_help()
        assert "all" in h and "--show" in h

    def test_role_choices_include_all(self):
        h = self._parser_help()
        assert "{citable,refutable,method,all}" in h
