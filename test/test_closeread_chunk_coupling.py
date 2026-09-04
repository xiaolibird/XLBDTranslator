# -*- coding: utf-8 -*-
"""钉住分块深读的三处耦合常量。

这套不变式跨三个文件：settings 定正文上限、pdf_ingest 定切块粒度与汇总预算、
closereading 做 [:max_chunks] 切片。此前只靠 settings.py 的一行注释维系，
而那行注释的算术是错的（说 120000 对应 11 块「正好被 12 卡住」，实际 12 块要
137400，白留一块额度整整一年没人发现）。注释不会失败，测试会。
"""
import inspect
import math

import pytest

from src.scholar import pdf_ingest
from src.scholar.settings import ProcessingSettings, load_scholar_settings


def _chunk_defaults():
    """从 chunk_text 的签名取切块粒度——写死数字会让本测试与实现脱钩。"""
    sig = inspect.signature(pdf_ingest.chunk_text)
    return sig.parameters["size"].default, sig.parameters["overlap"].default


def _effective():
    """生产上真正生效的配置（env 覆盖之后）。

    只测 ProcessingSettings() 默认值是不够的：ProcessingSettings 是 BaseModel，
    env 覆盖发生在 load_scholar_settings 里。往 config/scholar.env 加一行
    PROCESSING__CLOSEREAD_MAX_CHARS=200000 就能让真实块数 18 > 块顶 12、尾部被
    `chunk_text(...)[:max_chunks]` 静默切掉，而只钉默认值的测试照样全绿
    （2026-08-28 对抗审实测复现）。故两个都钉。
    """
    try:
        return load_scholar_settings().processing
    except Exception as e:  # 配置文件缺失/损坏时不该让本文件整片红
        pytest.skip("load_scholar_settings 不可用：{}".format(e))


@pytest.fixture(params=["default", "effective"])
def settings(request):
    return ProcessingSettings() if request.param == "default" else _effective()


def _n_chunks(n_chars: int, size: int, overlap: int) -> int:
    return math.ceil((n_chars - overlap) / (size - overlap))


def test_max_chars_fits_within_max_chunks(settings):
    """正文上限切出的块数不得超过块顶，否则尾部被 closereading 静默切掉。"""
    s = settings
    size, overlap = _chunk_defaults()
    assert _n_chunks(s.closeread_max_chars, size, overlap) <= s.closeread_max_chunks


def test_max_chars_actually_uses_the_last_chunk():
    """反向：块顶不得白留额度。这是**效率偏好**，只钉默认值，不钉生效配置。

    2026-08-28 修掉的 bug 是 max_chars=120000 只切 11 块、max_chunks=12 恒不生效，
    等于付了 12 块的钱只用 11 块。但「必须恰好吃满」与上面那条 `<=` 性质不同：
    `<=` 是**安全**不变式（超了尾部被静默切掉），`==` 只是别浪费钱。把 `==` 钉到
    生效 env 上会让「主动调低 max_chars 省 LLM 调用」这个完全合理的动作变成红灯
    （对抗审 2026-08-28 指出）。故这条只对默认值成立。
    """
    s = ProcessingSettings()
    size, overlap = _chunk_defaults()
    assert _n_chunks(s.closeread_max_chars, size, overlap) == s.closeread_max_chunks


def test_chunk_text_really_produces_that_many_chunks():
    """算术不能只在公式里成立——拿真实文本过一遍 chunk_text（同上，只钉默认值）。"""
    s = ProcessingSettings()
    size, overlap = _chunk_defaults()
    chunks = pdf_ingest.chunk_text("x" * s.closeread_max_chars, size=size, overlap=overlap)
    assert len(chunks) == s.closeread_max_chunks


def test_synth_budget_keeps_per_chunk_allowance(settings):
    """汇总预算与块数零和耦合：每块配额不得掉到实测块笔记长度以下太多。

    实测块笔记约 5.8k 字符。配额低于 5000 会让均摊裁剪常态化，等于用已读章节的
    保真度去买尾部覆盖——抬 max_chars 却不抬本预算就是这个后果。
    """
    s = settings
    per_chunk = pdf_ingest._SYNTH_NOTES_BUDGET / s.closeread_max_chunks
    assert per_chunk >= 5000, (
        "每块配额 {:.0f} < 5000：closeread_max_chunks({}) 与 _SYNTH_NOTES_BUDGET({}) "
        "失配——抬块数没同步抬预算，或预算被调低".format(
            per_chunk, s.closeread_max_chunks, pdf_ingest._SYNTH_NOTES_BUDGET))


# ---------------------------------------------------------------------------
# md ↔ index 往返契约（2026-08-28 的严重回归就出在这里）
# ---------------------------------------------------------------------------

class _CR:
    """最小 CloseReading 替身：只带渲染路径读的那几个属性。"""
    def __init__(self, truncated=None, used=None, raw=None, source="unpaywall"):
        self.from_full_text = True
        self.truncated = truncated
        self.body_chars = used
        self.body_chars_raw = raw
        self.source = source
        self.sections = []


def test_closeread_heading_stays_two_state_and_keeps_source():
    """精读节标题必须保持两态，否则 _CLOSEREAD_RE 的「· 来源」组会匹配空。

    第一版把覆盖率塞进了标题（`### 全文精读（正文截断，覆盖 47%） · 来源 \\`x\\``），
    正则仍 match、group(1) 仍对，但 group(2) 静默变 None——**恰恰是被截断的那批**
    在重解析后丢掉 reading_source，而库里 43 份无 sidecar 的 md 全走这条路。
    """
    from src.scholar.notes_index import _CLOSEREAD_RE
    for cr in (_CR(), _CR(truncated=True, used=120000, raw=256992)):
        line = "### 全文精读{}".format(
            " · 来源 `{}`".format(cr.source) if cr.source else "")
        m = _CLOSEREAD_RE.match(line)
        assert m is not None
        assert m.group(1) == "全文精读"
        assert m.group(2) == "unpaywall", "被截断篇的 reading_source 丢了：{!r}".format(line)
