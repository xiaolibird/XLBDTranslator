# -*- coding: utf-8 -*-
"""每日续跑 job 的 plist 文本级断言。

不用 plistlib：模板里的注释含 `--`，expat 会拒（同 test_backfill_abstracts.py 的先例）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.scholar.paths import REPO_ROOT                          # noqa: E402

PLIST = REPO_ROOT / "config" / "launchd" / "com.xlbd.scholar-inventory.plist"
INSTALL = REPO_ROOT / "scripts" / "install_inventory_resume.sh"


def test_plist_runs_the_resume_path_daily_and_never_at_load():
    t = PLIST.read_text(encoding="utf-8")
    assert "<string>scripts/inventory_missing_ids.py</string>" in t
    assert "<string>--resume-from</string>" in t and "<string>latest</string>" in t
    assert "StartCalendarInterval" in t
    # **不许有 RunAtLoad**：漏跑一天没损失（第二天补上），而 RunAtLoad 会让每次登录
    # 都白烧一次当日 OpenAlex 额度。这与备份 job 的取舍相反，是有意的。
    # 断言看 <key> 而不是全文——注释里正解释着「为什么不设 RunAtLoad」，
    # 拿全文去 `not in` 会把那句解释当成设置项（第一版就是这么写错的）。
    assert "<key>RunAtLoad</key>" not in t, "本 job 刻意不设 RunAtLoad，见 plist 注释"
    # 每天只有一个时刻：没有 Weekday 键（那是周任务）
    assert "<key>Weekday</key>" not in t


def test_install_script_has_uninstall_and_refuses_without_baseline():
    t = INSTALL.read_text(encoding="utf-8")
    assert "--uninstall" in t and "launchctl bootout" in t
    assert "没有盘点报告" in t, "没有基线就不该装——装了每天只会退 2"
    assert "临时任务" in t, "它不是常驻服务，说明必须写在回执里"
    # 邮箱不许写死进仓库文件
    assert "@" not in t.split("EMAIL=\"${INVENTORY_EMAIL:-}\"")[1].split("if [[ -z")[0]
