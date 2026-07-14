"""Scholar Digest 新功能回归测试：
1. OpenAI 兼容（DeepSeek）LLM 调用路径
2. 月度切分输出
全部使用假密钥/mock，不发真实请求。
"""
from datetime import datetime
from pathlib import Path

import pytest

from src.scholar.schema import (
    DigestStatus,
    EmailMetadata,
    PaperMetadata,
    PaperSegment,
    ScholarSettings,
)
from src.scholar.workflow import ScholarWorkflow


def make_settings(tmp_path: Path, provider: str) -> ScholarSettings:
    env = tmp_path / "scholar_test.env"
    env.write_text(
        "LLM__PROVIDER={}\n".format(provider)
        + "LLM__OPENAI_API_KEY=FAKE_DEEPSEEK_KEY\n"
        + "LLM__BASE_URL=https://fake.example.com/v1\n"
        + "LLM__MODEL=deepseek-v4-flash\n"
        + "PROCESSING__OUTPUT_DIR={}\n".format(tmp_path / "out"),
        encoding="utf-8",
    )
    return ScholarSettings.from_env_file(env)


def make_segment(seg_id: int, title: str, received: datetime) -> PaperSegment:
    return PaperSegment(
        segment_id=seg_id,
        paper_id="paper_{}".format(seg_id),
        metadata=PaperMetadata(
            paper_id="paper_{}".format(seg_id),
            title=title,
            email_received_at=received,
        ),
    )


def test_deepseek_client_uses_scholar_env_credentials(tmp_path):
    """provider=deepseek 时客户端使用 scholar.env 的 key/base_url（LLMClient.conn 暴露连接字典）"""
    wf = ScholarWorkflow(make_settings(tmp_path, "deepseek"))
    client = wf.llm_client.conn  # 委托 LLMClient；.conn 是连接字典
    assert client["provider"] == "openai-compatible"
    assert client["api_key"] == "FAKE_DEEPSEEK_KEY"
    assert client["base_url"] == "https://fake.example.com/v1"
    assert client["model"] == "deepseek-v4-flash"


def test_call_llm_openai_compatible_posts_chat_completions(tmp_path, monkeypatch):
    """_call_llm 走 /chat/completions 并解析 choices[0].message.content"""
    wf = ScholarWorkflow(make_settings(tmp_path, "deepseek"))
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "摘要结果"}}]}

    class FakeClient:
        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    from src.scholar.llm_client import LLMClient
    lc = LLMClient(wf.settings.llm)
    lc._conn = {
        "client": FakeClient(),
        "base_url": "https://fake.example.com/v1",
        "api_key": "FAKE_DEEPSEEK_KEY",
        "model": "deepseek-v4-flash",
        "provider": "openai-compatible",
    }
    wf._llm_client = lc

    result = wf._call_llm("测试 prompt")
    assert result == "摘要结果"
    assert captured["url"] == "https://fake.example.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer FAKE_DEEPSEEK_KEY"
    assert captured["json"]["model"] == "deepseek-v4-flash"
    assert captured["json"]["messages"][0]["content"] == "测试 prompt"


def test_monthly_markdown_split(tmp_path):
    """跨月的 segments 应拆分出每月一个 Markdown 文件"""
    wf = ScholarWorkflow(make_settings(tmp_path, "deepseek"))
    wf.segments = [
        make_segment(1, "Paper Feb", datetime(2026, 2, 15)),
        make_segment(2, "Paper Mar A", datetime(2026, 3, 1)),
        make_segment(3, "Paper Mar B", datetime(2026, 3, 20)),
    ]
    wf.processed_emails = [
        EmailMetadata(email_id="e1", subject="s", sender="x@y.z", received_at=datetime(2026, 2, 15)),
        EmailMetadata(email_id="e2", subject="s", sender="x@y.z", received_at=datetime(2026, 3, 1)),
    ]

    written = wf._write_monthly_markdown()
    names = sorted(p.name for p in written)
    assert len(written) == 2
    assert names[0].endswith("_2026-02.md") and names[1].endswith("_2026-03.md")

    feb = written[0].read_text(encoding="utf-8")
    mar = written[1].read_text(encoding="utf-8")
    assert "Paper Feb" in feb and "Paper Mar A" not in feb
    assert "Paper Mar A" in mar and "Paper Mar B" in mar and "Paper Feb" not in mar


def test_monthly_split_skipped_for_single_month(tmp_path):
    """只有一个月时不产生额外文件"""
    wf = ScholarWorkflow(make_settings(tmp_path, "deepseek"))
    wf.segments = [make_segment(1, "Only Paper", datetime(2026, 7, 1))]
    wf.processed_emails = []
    assert wf._write_monthly_markdown() == []


def test_parser_splits_scholar_email_by_h3_anchors():
    """Scholar 邮件标准结构（每篇论文一个 h3+a）必须全部提取，而不是只取第一篇"""
    from src.scholar.paper_extractor import ScholarEmailParser

    papers_html = "".join(
        '<h3><a href="https://example.com/p{i}">Paper Title Number {i} About Clinical Prediction</a></h3>'
        '<div>Author {i} - Journal, 2026</div>'
        '<div class="gse_alrt_sni">Abstract snippet for paper {i}.</div>'.format(i=i)
        for i in range(1, 6)
    )
    html = "<html><body><div style='margin-bottom:10px'><a href='#'>wrapper link and some header text for the alert email</a></div>{}</body></html>".format(papers_html)

    meta = EmailMetadata(email_id="e_h3", subject="s", sender="scholaralerts-noreply@google.com", received_at=datetime(2026, 7, 1))
    papers = ScholarEmailParser().parse_email(html, meta)
    assert len(papers) == 5, "应提取全部 5 篇论文，实际 {}".format(len(papers))
    titles = {p.metadata.title for p in papers}
    assert len(titles) == 5


def test_blacklist_matches_whole_words_only(tmp_path):
    """黑名单 ASCII 关键词必须整词匹配：'Gene' 不得误杀 'general'，'Vision' 不得误杀 'supervision'"""
    wf = ScholarWorkflow(make_settings(tmp_path, "deepseek"))
    blacklist = ["Gene", "Vision"]
    whitelist = []

    ok = make_segment(1, "A general framework with supervision for EHR prediction", datetime(2026, 3, 1))
    assert wf._match_keyword(ok, blacklist) is None

    bad_gene = make_segment(2, "Gene expression analysis in cancer", datetime(2026, 3, 1))
    assert wf._match_keyword(bad_gene, blacklist) == "Gene"

    bad_vision = make_segment(3, "Computer Vision for radiology", datetime(2026, 3, 1))
    assert wf._match_keyword(bad_vision, blacklist) == "Vision"


def test_whitelist_whole_word(tmp_path):
    """白名单同样整词匹配：'LLM' 不应被 'allmark' 之类误命中，但正常命中保留"""
    wf = ScholarWorkflow(make_settings(tmp_path, "deepseek"))
    hit = make_segment(1, "An LLM approach to clinical notes", datetime(2026, 3, 1))
    assert wf._match_keyword(hit, ["LLM"]) == "LLM"
    miss = make_segment(2, "The hallmark of allmark systems", datetime(2026, 3, 1))
    assert wf._match_keyword(miss, ["LLM"]) is None


def test_build_batch_prompt_handles_json_braces(tmp_path):
    """prompt 模板含 JSON 示例花括号，不得用 str.format 触发 KeyError"""
    wf = ScholarWorkflow(make_settings(tmp_path, "deepseek"))
    seg = make_segment(1, "Test Paper", datetime(2026, 3, 1))
    prompt = wf._build_batch_prompt([seg])
    assert "Test Paper" in prompt
    assert '"id": 1' in prompt  # 模板示例保留
    assert "__PAPERS_JSON__" not in prompt  # 占位符已替换


def test_parser_populates_email_received_at():
    """解析出的论文必须携带邮件接收时间（月度切分依赖它）"""
    from src.scholar.paper_extractor import ScholarEmailParser

    html = (
        '<h3><a href="https://example.com/1">First Clinical Prediction Paper Title Here</a></h3><div>A - J, 2026</div>'
        '<h3><a href="https://example.com/2">Second Clinical Prediction Paper Title Here</a></h3><div>B - J, 2026</div>'
    )
    received = datetime(2026, 5, 20, 9, 30)
    meta = EmailMetadata(email_id="e_date", subject="s", sender="x", received_at=received)
    papers = ScholarEmailParser().parse_email(html, meta)
    assert len(papers) == 2
    assert all(p.metadata.email_received_at == received for p in papers)


def test_whitelist_matches_plural_forms(tmp_path):
    """整词匹配必须容忍复数：LLMs / Networks / Models（正确性复审发现的召回回归）"""
    wf = ScholarWorkflow(make_settings(tmp_path, "deepseek"))
    whitelist = ["LLM", "Graph Neural Network", "Predictive Model", "EHR"]
    for title in (
        "Large Language Models — LLMs in Healthcare",
        "Graph Neural Networks on Electronic Health Records",
        "Predictive Models for ICU Mortality",
    ):
        seg = make_segment(1, title, datetime(2026, 3, 1))
        assert wf._match_keyword(seg, whitelist) is not None, title
    # 整词边界仍然生效
    miss = make_segment(2, "The hallmark of allmark systems", datetime(2026, 3, 1))
    assert wf._match_keyword(miss, ["LLM"]) is None


def test_empty_keyword_never_matches(tmp_path):
    """空关键词不得命中一切（黑名单混入空项时不能全灭）"""
    wf = ScholarWorkflow(make_settings(tmp_path, "deepseek"))
    seg = make_segment(1, "Any EHR paper", datetime(2026, 3, 1))
    assert wf._match_keyword(seg, ["", "  "]) is None


def test_batch_failure_does_not_overwrite_completed(tmp_path):
    """批次解析异常时，已 COMPLETED 的 segment 不得被回写成 FAILED"""
    wf = ScholarWorkflow(make_settings(tmp_path, "deepseek"))
    done = make_segment(1, "Done paper", datetime(2026, 3, 1))
    done.status = DigestStatus.COMPLETED
    pending = make_segment(2, "Pending paper", datetime(2026, 3, 1))
    batch = [done, pending]

    wf._parse_llm_response("这不是JSON{{{", batch)
    assert done.status == DigestStatus.COMPLETED
    assert pending.status == DigestStatus.FAILED


def test_h3_split_falls_back_when_siblings_lack_content():
    """嵌套布局（h3 与摘要不同层级）下 h3 切分应放弃，回退旧策略而非产出空摘要"""
    from src.scholar.paper_extractor import ScholarEmailParser
    # h3 藏在各自的 td 里，摘要在相邻 tr —— 兄弟遍历拿不到正文
    html = "<table>" + "".join(
        '<tr><td><h3><a href="https://e.com/{i}">Nested Paper Title Number {i} Long Enough</a></h3></td></tr>'
        '<tr><td>Abstract text for paper {i} living in a separate row of the table layout.</td></tr>'.format(i=i)
        for i in range(1, 4)
    ) + "</table>"
    parser = ScholarEmailParser()
    from bs4 import BeautifulSoup
    blocks = parser._split_by_h3_anchors(BeautifulSoup(html, "html.parser"))
    assert blocks == [], "多数片段缺正文时应返回空以回退旧策略"
