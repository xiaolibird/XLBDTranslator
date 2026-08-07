# -*- coding: utf-8 -*-
"""_step_process_papers 的批次失败率短路守卫回归测试。

背景：_step_process_papers 对每批 _process_batch 一律 except+continue。LLM 回退链
耗尽（欠费/限流/CLI 缺失全部命中）时每一批都会同样失败，逐批 catch+continue 会把
"通路级故障"悄悄吞成"处理完成 0/N"，Step 4 照常产出 status=COMPLETED 的降级
digest、进程退出码 0。对齐 src/scholar/ingest.py 对全文精读 0/N 的同款守卫：
全部批次失败时必须 raise，不产出降级终稿。

全部离线：不涉及网络/LLM 调用。
"""
from pathlib import Path

import pytest

from src.scholar.schema import DigestStatus, PaperMetadata, PaperSegment, ScholarSettings
from src.scholar.workflow import ScholarWorkflow

MINIMAL_ENV = """
GMAIL__CREDENTIALS_PATH=fake/creds.json
GMAIL__TOKEN_PATH=fake/token.json
LLM__PROVIDER=gemini
LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST
LLM__MODEL=fake-model
"""


def _make_settings(tmp_path: Path, batch_size: int = 2) -> ScholarSettings:
    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(MINIMAL_ENV, encoding="utf-8")
    settings = ScholarSettings.from_env_file(env_file)
    settings.processing.output_dir = tmp_path / "out"
    settings.processing.batch_size = batch_size
    settings.processing.translate_abstracts = True
    settings.processing.generate_summary = True
    return settings


def _make_paper(segment_id: int, title: str) -> PaperSegment:
    return PaperSegment(
        segment_id=segment_id,
        paper_id="paper_{}".format(segment_id),
        metadata=PaperMetadata(paper_id="paper_{}".format(segment_id), title=title,
                               source_email_id="email_1"),
    )


def test_all_batches_failing_raises_instead_of_degraded_completion(tmp_path):
    """4 篇论文分 2 批，两批全失败（模拟回退链耗尽）：必须 raise，不能吞成
    「处理完成 0/4」后静默 return。"""
    settings = _make_settings(tmp_path, batch_size=2)
    wf = ScholarWorkflow(settings)
    wf.segments = [_make_paper(i, "P{}".format(i)) for i in range(1, 5)]

    def _always_fail(batch):
        raise RuntimeError("所有 LLM provider 均不可用（回退链已耗尽）")

    wf._process_batch = _always_fail

    with pytest.raises(RuntimeError, match="2/2"):
        wf._step_process_papers()


def test_partial_batch_failure_does_not_raise(tmp_path):
    """4 篇论文分 2 批，只有 1 批失败：保守阈值，仍应正常返回（不 raise）。"""
    settings = _make_settings(tmp_path, batch_size=2)
    wf = ScholarWorkflow(settings)
    wf.segments = [_make_paper(i, "P{}".format(i)) for i in range(1, 5)]

    calls = []

    def _fail_first_batch_only(batch):
        calls.append(batch)
        if len(calls) == 1:
            raise RuntimeError("单批瞬时失败")
        for seg in batch:
            seg.status = DigestStatus.COMPLETED

    wf._process_batch = _fail_first_batch_only

    wf._step_process_papers()  # 不应抛异常
    assert len(calls) == 2


def test_all_batches_succeeding_does_not_raise(tmp_path):
    """对照组：全部批次成功时行为不变。"""
    settings = _make_settings(tmp_path, batch_size=2)
    wf = ScholarWorkflow(settings)
    wf.segments = [_make_paper(i, "P{}".format(i)) for i in range(1, 5)]

    def _succeed(batch):
        for seg in batch:
            seg.status = DigestStatus.COMPLETED

    wf._process_batch = _succeed

    wf._step_process_papers()  # 不应抛异常
    assert all(s.is_processed for s in wf.segments)


def test_empty_segments_does_not_raise(tmp_path):
    """无论文时 total_batches=0，守卫不应误触发（0 全部失败 of 0 批 ≠ 故障）。"""
    settings = _make_settings(tmp_path, batch_size=2)
    wf = ScholarWorkflow(settings)
    wf.segments = []

    wf._step_process_papers()  # 不应抛异常
