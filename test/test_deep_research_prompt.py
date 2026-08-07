# -*- coding: utf-8 -*-
"""deep_research thesis 模板注入回归测试。

契约：
1. thesis_introduction_prompt.md 模板加载后经 _merge_with_template 替换，
   产出 prompt 必须包含论文列表内容与 research_interests 文案；
2. 替换后不得残留任何 "{{" 占位符（证明四个占位符全部命中，不是 no-op）。
3. _call_deep_research 必须经由 LLMClient 走统一回退链，不能钉死已下线的
   Gemini 实验模型 ID、也不能绕开 fallback_providers 直连单一 provider。
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.scholar.deep_research import DeepResearchClient
from src.scholar.schema import PaperMetadata, PaperSegment, ScholarSettings

MINIMAL_ENV = """
GMAIL__CREDENTIALS_PATH=fake/creds.json
GMAIL__TOKEN_PATH=fake/token.json
LLM__PROVIDER=gemini
LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST
LLM__MODEL=fake-model
"""


def _make_settings(tmp_path: Path) -> ScholarSettings:
    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(MINIMAL_ENV, encoding="utf-8")
    settings = ScholarSettings.from_env_file(env_file)
    settings.processing.output_dir = tmp_path / "out"
    return settings


def _make_paper(segment_id: int, title: str) -> PaperSegment:
    return PaperSegment(
        segment_id=segment_id,
        paper_id="paper_{}".format(segment_id),
        original_abstract="A study about missing data mechanisms.",
        metadata=PaperMetadata(
            paper_id="paper_{}".format(segment_id),
            title=title,
            source_email_id="email_1",
        ),
    )


def test_template_merge_injects_papers_and_research_interests(tmp_path):
    """模板加载 + 替换后，产出 prompt 含论文标题与 research_interests，且不含 "{{"。"""
    settings = _make_settings(tmp_path)
    client = DeepResearchClient(settings)

    template_file = Path(__file__).resolve().parents[1] / "config" / "prompts" / "thesis_introduction_prompt.md"
    template = template_file.read_text(encoding="utf-8")

    papers = [_make_paper(1, "MNAR Missingness in Multi-Center EHR Cohorts")]
    prompt = client._merge_with_template(template, papers, research_topic="MNAR missingness mechanisms")

    assert "MNAR Missingness in Multi-Center EHR Cohorts" in prompt
    assert settings.processing.research_interests in prompt
    assert "{{" not in prompt


def test_prompt_file_has_all_four_placeholders():
    """模板文件本身必须包含四个占位符，否则替换是 no-op。"""
    template_file = Path(__file__).resolve().parents[1] / "config" / "prompts" / "thesis_introduction_prompt.md"
    template = template_file.read_text(encoding="utf-8")

    for placeholder in ("{{RESEARCH_TOPIC}}", "{{PAPERS_LIST}}", "{{PAPER_COUNT}}", "{{GENERATION_DATE}}"):
        assert placeholder in template, "缺少占位符: {}".format(placeholder)


# ---------------------------------------------------------------- _call_deep_research 走 LLMClient 回退链

def test_call_deep_research_uses_llm_client_not_hardcoded_gemini_model(tmp_path):
    """回归：_call_deep_research 曾硬编码已下线的 gemini-2.5-pro-exp-03-25，且完全
    绕开 LLMClient（不吃 provider/fallback_providers 配置）。现在必须经
    self.llm_client（LLMClient 实例）调用，模型名取 closeread_model/model，
    不得再出现那个下线的实验模型 ID。"""
    settings = _make_settings(tmp_path)
    settings.llm.closeread_model = "deepseek-v4-pro"
    client = DeepResearchClient(settings)

    fake_llm_client = MagicMock()
    fake_llm_client.call.return_value = "生成的内容"
    fake_llm_client.current_provider = "deepseek"
    client._llm_client = fake_llm_client  # 绕过懒加载，直接注入假 LLMClient

    result = client._call_deep_research("some prompt")

    assert result["success"] is True
    assert result["content"] == "生成的内容"
    assert result["model"] == "deepseek-v4-pro"
    # 必须调用注入的 LLMClient.call，而不是自己另起一条 Gemini 直连
    fake_llm_client.call.assert_called_once()
    called_kwargs = fake_llm_client.call.call_args.kwargs
    assert called_kwargs.get("model") == "deepseek-v4-pro"
    assert "gemini-2.5-pro-exp-03-25" not in str(fake_llm_client.call.call_args)


def test_call_deep_research_falls_back_to_main_model_without_closeread_model(tmp_path):
    """closeread_model 未设置时退回主模型 model（与 ingest.run_ingest 的取值口径一致）。"""
    settings = _make_settings(tmp_path)
    settings.llm.closeread_model = None
    settings.llm.model = "fake-model"
    client = DeepResearchClient(settings)

    fake_llm_client = MagicMock()
    fake_llm_client.call.return_value = "内容"
    fake_llm_client.current_provider = "gemini"
    client._llm_client = fake_llm_client

    result = client._call_deep_research("prompt")
    assert result["model"] == "fake-model"


def test_call_deep_research_propagates_llm_client_failure_as_result_error(tmp_path):
    """LLMClient 回退链耗尽时的异常必须落进 result['error']，success=False（保持
    既有契约：cli.py 靠 result['success'] 判定是否 sys.exit(1)）。"""
    settings = _make_settings(tmp_path)
    client = DeepResearchClient(settings)

    fake_llm_client = MagicMock()
    fake_llm_client.call.side_effect = RuntimeError("所有 LLM provider 均不可用（回退链已耗尽）")
    client._llm_client = fake_llm_client

    result = client._call_deep_research("prompt")
    assert result["success"] is False
    assert "回退链已耗尽" in result["error"]


def test_llm_client_lazily_constructed_from_settings_llm(tmp_path):
    """llm_client 属性懒加载出真正的 LLMClient（settings.llm 驱动），确认没有
    另起一条独立于 LLMClient 之外的 Gemini 专线。"""
    settings = _make_settings(tmp_path)
    client = DeepResearchClient(settings)
    from src.scholar.llm_client import LLMClient

    assert client._llm_client is None
    lc = client.llm_client
    assert isinstance(lc, LLMClient)
    assert client.llm_client is lc  # 懒加载只建一次

    with patch.object(lc, "close") as mock_close:
        client.close()
        mock_close.assert_called_once()
    assert client._llm_client is None
