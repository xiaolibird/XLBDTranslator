# -*- coding: utf-8 -*-
"""冻结页面 fixture：把「改完必须重跑 gen_bench」从纪律变成会红的测试。

**为什么必须有这个文件**（三次事故，一次比一次难看）：

1. 前置断言写成 Unicode 字母类，CJK 被当字母 → 全库数字 542→389、接地率
   99.5%→76.3%。当时 64 条离线测试**全绿**，靠重跑 gen_bench 才发现。
2. 字符类换成连续码位区段 `À-ɏ`，区段里的 `×÷` 吃掉 `3×3` 的操作数。
   事后补的「紧邻中文」测试也抓不到——样例里没有 ×。
3. 删掉 `cn_number` 后**没有重跑 gen_bench**，于是接地率 539→538 无人知晓，
   三份决策文档跟着一起写错，连给下一轮审核的任务书都传了错的基线，
   而刚设的通过线 0.995 当场就红了（实测 0.9926）。

三次的共同形状：**判据的产出量整体变了，而每一条单点断言看都像合理收紧。**
纪律（"改完记得重跑"）挡不住它——纪律靠人记。这个文件靠 CI。

页面是真实产物的快照（`output/` 不进 git，所以必须在这里留一份）。选这三页的
理由：qa 页带删 `cn_number` 后掉的那个数字、icu-benchmarks 数字最密且含两条已
判读报警、shadow-variable 含 `3×3`。

**期望值变了怎么办**：先想清楚是判据改对了还是改错了，确认对了再更新
`expected.json`，并**同步更新** `docs/decisions/gen_bench_baseline_2026-08.md` 的
基线表与通过线——这一步就是上面第 3 次事故漏掉的那一步。
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import gen_bench as GB  # noqa: E402

FROZEN = Path(__file__).parent / "data" / "gen_bench_frozen"
EXPECTED = json.loads((FROZEN / "expected.json").read_text(encoding="utf-8"))

KEYS = ("n_claims", "numeric_total", "numeric_grounded",
        "numeric_derived", "numeric_ungrounded")


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_frozen_page_metrics_are_exact(name):
    """四元组必须**精确相等**，不是"大于等于"也不是"差不多"。

    容差会把整片失效放过去——那正是这个文件要挡的东西。
    """
    got = GB.audit_page(FROZEN / name)
    assert {k: got[k] for k in KEYS} == EXPECTED[name], name


def test_frozen_corpus_totals():
    """跨页合计——单页断言挡不住"一页涨一页跌"的相互抵消。"""
    rows = [GB.audit_page(FROZEN / n) for n in sorted(EXPECTED)]
    total = sum(r["numeric_total"] for r in rows)
    grounded = sum(r["numeric_grounded"] for r in rows)
    derived = sum(r["numeric_derived"] for r in rows)
    ungrounded = sum(r["numeric_ungrounded"] for r in rows)
    assert (total, grounded, derived, ungrounded) == (350, 337, 9, 4)
    # 会计恒等式：每个数字必然落进三档之一，一个都不许漏计
    assert grounded + derived + ungrounded == total
