# -*- coding: utf-8 -*-
"""全测试目录共享的夹具。

目前只有一件事：把「人工去重裁决」的仓库来源关在 tmp 外面。
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_repo_overrides(monkeypatch, tmp_path_factory):
    """把人工裁决的来源关进 tmp——**任何**测试文件里的 _global_pass 都不得读仓库那份真文件。

    生产行为不改（仓库 config 与 notes_dir 取并集是有意设计，见 _read_override_files），
    这里只是把仓库那一侧的路径指向一个不存在的文件。不隔离的话，往
    config/dedup_overrides.json 里加一条涉及 title:a / doi:10.1/aaa 之类通用键的真实裁决，
    就会让不相干的用例变红或变绿；test_notes_index.test_override_transitivity_and_bad_file_is_tolerated
    的「文件损坏」分支尤其名不副实——tmp 里的损坏文件被跳过后，仓库那份仍被正常读入，
    测的根本不是「唯一来源损坏」。

    ⚠️ 必须放在 conftest.py 而不是某个测试文件里：update_index → _global_pass →
    _read_override_files 这条链在 test_vault.py 与 test_pdf_ingest.py 里同样会走，
    只在 test_notes_index.py 加 autouse fixture 等于只护住了三分之一（实测毒化仓库文件后
    test_vault.py 有 3 个用例变红）。

    import 放在函数体内：这份 conftest 对整个 test/ 目录生效，模块级导入
    src.scholar.notes_index 会让所有测试文件（含与 scholar 无关的翻译链路）都被迫
    在收集期加载它。
    """
    from src.scholar import notes_index as ni
    monkeypatch.setattr(
        ni, "REPO_OVERRIDES_PATH",
        tmp_path_factory.mktemp("no_repo_overrides") / ni.DEDUP_OVERRIDES_JSON)
