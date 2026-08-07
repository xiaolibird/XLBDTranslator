# -*- coding: utf-8 -*-
"""execute() 第 7 步（标题翻译）soft/瞬态失败降级回归测试。

背景：_post_translate_titles 内部调用 translator.translate_titles，该方法
（engine.py 的 translate_titles）自身的 _reraise_if_provider_fatal 对 hard
与 soft 一视同仁上抛（那层是为了让回退链 FallbackTranslator 感知配额/认证
状态，语义不能改）。但 execute() 此前对 _post_translate_titles 没有任何
保护：一次标题阶段的 429/quota（soft）错误会直接终止整个 execute()——
即便正文已 100% 翻译完成、后续只差 cleanup + 渲染两步，也会颗粒无收，
产不出任何成品。

修复：execute() 把 _post_translate_titles() 包一层 try，只有
classify_fatal(e)=='hard'（402 欠费/401 认证/404 模型下线，provider 确定
不可用）才继续上抛终止整轮；soft（429/quota）及其他瞬态失败降级为标题保
留原文 + 记警告，继续走 cleanup/render。

本文件绕过真实文档解析/术语表生成等重步骤（monkeypatch 为 no-op），只保留
execute() 自身的步骤编排与 _post_translate_titles 的真实实现，配合一个
按需抛异常的 fake translator，验证：
1. translate_titles 抛 soft APIError → execute() 不 raise，产出仍被渲染，
   标题保留原文（未被覆写）。
2. translate_titles 抛 hard APIError → execute() 必须 raise，且不应产出
   （render 未被调用）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.core.exceptions import APIError
from src.core.schema import ContentSegment, SegmentList
from src.translator.fallback import classify_fatal
from src.workflow.workflow import TranslationWorkflow


class _FakeCheckpoint:
    """_post_translate_titles 需要的最小 checkpoint 假件：无任何标题缓存命中，
    确保待翻译标题总会被送进 translator.translate_titles。"""

    def __init__(self):
        self.title_translations = {}
        self.filenames_saved = None

    def get_title_translation(self, title):
        return self.title_translations.get(title)

    def save_title_translation(self, original, translated):
        self.title_translations[original] = translated

    def save_checkpoint(self):
        pass

    def save_filenames(self, original_filename, translated_filename):
        self.filenames_saved = (original_filename, translated_filename)


def _make_workflow(tmp_path, translate_titles_fn):
    """绕过 __init__、绕过真实文档解析/术语表/翻译循环等重步骤，只保留
    execute() 的步骤编排 + _post_translate_titles 的真实实现。"""
    wf = TranslationWorkflow.__new__(TranslationWorkflow)
    wf.settings = SimpleNamespace(
        processing=SimpleNamespace(
            translation_mode_entity=None,
            enable_glossary_edit=False,
        )
    )
    wf.file_path = tmp_path / "mybook.epub"
    wf.file_path.write_text("dummy", encoding="utf-8")
    wf.project_name = "fakehash"
    wf.project_dir = tmp_path
    wf.doc_title = "mybook"  # ASCII，非中文，需要翻译
    wf.translated_doc_title = ""
    wf.all_segments = SegmentList(
        [ContentSegment(segment_id=1, original_text="hello", translated_text="你好", content_type="text")]
    )
    wf.checkpoint = _FakeCheckpoint()
    wf.glossary = {}
    wf.produced_outputs = []
    wf.still_failed_count = 0
    wf.translator = SimpleNamespace(translate_titles=translate_titles_fn)

    # 重步骤全部 no-op：只关心 execute() 对 _post_translate_titles 的
    # 编排行为，不关心解析/术语表/翻译循环/质检本身
    wf._optimize_batch_size_for_provider = lambda: None
    wf._initialize_translator = lambda: None
    wf._load_document = lambda: None
    wf._generate_glossary = lambda: None
    wf._initialize_checkpoint = lambda: None
    wf._run_translation_loop = lambda: None
    wf._quality_check = lambda: None
    wf._cleanup_resources = lambda: None
    wf._save_structure_map = lambda segs: None

    render_calls = []
    wf._render_output = lambda: render_calls.append(True) or wf.produced_outputs.append(tmp_path / "out.md")
    wf._render_calls = render_calls

    return wf


def test_soft_error_in_title_stage_degrades_and_continues_to_render(tmp_path):
    """标题阶段抛 soft APIError（429/quota）→ execute() 不 raise，仍渲染，
    标题保留原文（未被覆写为空/污染值）。"""
    soft_exc = APIError("429 Too Many Requests: rate limit exceeded, please retry later")
    assert classify_fatal(soft_exc) == "soft"  # 前置校验，避免测试空转

    def _raise_soft(titles):
        raise soft_exc

    wf = _make_workflow(tmp_path, _raise_soft)

    wf.execute()  # 不应 raise

    assert wf._render_calls == [True], "标题阶段 soft 失败后仍应继续渲染"
    assert wf.produced_outputs, "应有产出（渲染未被跳过）"
    # 标题保留原文：_post_translate_titles 因异常提前退出，回填逻辑未执行，
    # translated_doc_title 维持初始空值（即用原文 doc_title 渲染）
    assert wf.translated_doc_title == ""
    assert wf.doc_title == "mybook"


def test_hard_error_in_title_stage_propagates_and_skips_render(tmp_path):
    """标题阶段抛 hard APIError（402 欠费）→ execute() 必须整体 raise，
    不应继续渲染（render 未被调用）。"""
    hard_exc = APIError("402 Payment Required: insufficient balance")
    assert classify_fatal(hard_exc) == "hard"  # 前置校验，避免测试空转

    def _raise_hard(titles):
        raise hard_exc

    wf = _make_workflow(tmp_path, _raise_hard)

    with pytest.raises(APIError):
        wf.execute()

    assert wf._render_calls == [], "hard 致命错误必须终止整轮，不应继续渲染"
    assert not wf.produced_outputs


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
