# -*- coding: utf-8 -*-
"""notes_embed 的 sqlite 异常分流（R3 变异发现:R1/R2 修了它却零回归保护）。

危害不在崩溃而在**误导**:唯一的读者是 cron_embed.err.log,把磁盘写满/卷只读/
schema 未迁移一律说成"被并发写锁定",人会照着"等一会儿再重试"白等到天荒地老。
"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _load():
    spec = importlib.util.spec_from_file_location(
        "notes_embed_cli", REPO / "scripts" / "notes_embed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NE = _load()


@pytest.mark.parametrize("exc, must_have, must_not", [
    (sqlite3.OperationalError("database is locked"), "并发写锁定", "磁盘写满"),
    (sqlite3.OperationalError("database or disk is full"), "不是**并发锁", "并发写锁定（"),
    (sqlite3.OperationalError("no such column: role"), "schema 与代码不符", "并发写锁定（"),
    (sqlite3.DatabaseError("file is not a database"), "损坏或不是 SQLite", "并发写锁定"),
])
def test_sqlite_error_exit_routes_by_cause(exc, must_have, must_not, capsys):
    rc = NE._sqlite_error_exit(exc)
    err = capsys.readouterr().err
    assert rc == 2, "退出码契约是 0/2/3，不许落到 1（那是别处的「无命中」）"
    assert must_have in err
    assert must_not not in err


def test_database_error_is_a_superclass_of_operational_error():
    """正式路径与 dry-run 都只写了一个 except DatabaseError——它必须真能接住
    OperationalError，否则分流形同虚设。"""
    assert issubclass(sqlite3.OperationalError, sqlite3.DatabaseError)
