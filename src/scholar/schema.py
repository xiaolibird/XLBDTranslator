# -*- coding: utf-8 -*-
"""
Scholar Digest 数据结构定义
使用 Pydantic 2.0 进行数据验证和序列化
模仿 src/core/schema.py 的设计模式
"""
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Any, Literal
from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from datetime import datetime, date
import json
import os


class DigestStatus(str, Enum):
    """摘要状态枚举"""
    PENDING = "pending"           # 待处理
    EXTRACTING = "extracting"     # 正在提取
    SUMMARIZING = "summarizing"   # 正在总结
    COMPLETED = "completed"       # 已完成
    FAILED = "failed"             # 失败


class PaperField(str, Enum):
    """论文领域分类"""
    COMPUTER_SCIENCE = "Computer Science"
    BIOLOGY = "Biology"
    MEDICINE = "Medicine"
    PHYSICS = "Physics"
    CHEMISTRY = "Chemistry"
    MATHEMATICS = "Mathematics"
    ENGINEERING = "Engineering"
    SOCIAL_SCIENCES = "Social Sciences"
    ECONOMICS = "Economics"
    PSYCHOLOGY = "Psychology"
    ENVIRONMENTAL_SCIENCE = "Environmental Science"
    MATERIALS_SCIENCE = "Materials Science"
    NEUROSCIENCE = "Neuroscience"
    ARTIFICIAL_INTELLIGENCE = "Artificial Intelligence"
    MACHINE_LEARNING = "Machine Learning"
    OTHER = "Other"


class PaperMetadata(BaseModel):
    """论文元数据 - 存储论文的基本信息"""
    paper_id: str = Field(description="论文唯一标识（基于DOI或标题生成的hash）")
    title: str = Field(description="论文标题")
    authors: List[str] = Field(default_factory=list, description="作者列表")
    
    # 发布信息
    publication_date: Optional[date] = Field(None, description="发布日期")
    journal: Optional[str] = Field(None, description="期刊/会议名称")
    volume: Optional[str] = Field(None, description="卷号")
    issue: Optional[str] = Field(None, description="期号")
    pages: Optional[str] = Field(None, description="页码范围")
    
    # 标识符
    doi: Optional[str] = Field(None, description="DOI标识符")
    arxiv_id: Optional[str] = Field(None, description="arXiv ID")
    pmid: Optional[str] = Field(None, description="PubMed ID")
    url: Optional[str] = Field(None, description="论文链接")
    pdf_url: Optional[str] = Field(None, description="PDF下载链接")
    
    # 分类信息
    field: PaperField = Field(default=PaperField.OTHER, description="论文领域")
    keywords: List[str] = Field(default_factory=list, description="关键词列表")
    
    # 来源追踪
    source_email_id: Optional[str] = Field(None, description="来源邮件ID")
    email_received_at: Optional[datetime] = Field(None, description="邮件接收时间")
    extracted_at: datetime = Field(default_factory=datetime.now, description="提取时间")
    
    # 被引用/相关信息
    citation_count: Optional[int] = Field(None, description="引用次数")
    related_papers: List[str] = Field(default_factory=list, description="相关论文ID列表")
    
    # 来源类型 (用于优先级排序)
    source_type: str = Field(default="unknown", description="来源类型: journal/conference/arxiv/medrxiv/unknown")
    paper_type: str = Field(default="research", description="论文类型: review/research/meta-analysis")
    
    # LLM处理结果（也可存储在Segment中）
    translated_title: str = Field(default="", description="翻译后的标题")
    keywords: List[str] = Field(default_factory=list, description="提取的关键词（LLM处理后更新）")
    relevance_score: float = Field(default=0.0, description="与研究领域相关度 (0-1)")
    priority_score: float = Field(default=0.0, description="综合优先级得分")
    priority_reason: str = Field(default="", description="优先级评分理由")

    model_config = {'use_enum_values': True}

    @field_validator('doi', mode='before')
    @classmethod
    def normalize_doi(cls, v):
        """规范化DOI格式"""
        if v is None:
            return None
        # 移除可能的URL前缀
        if isinstance(v, str):
            v = v.replace('https://doi.org/', '')
            v = v.replace('http://doi.org/', '')
            v = v.replace('doi:', '')
            v = v.strip()
        return v if v else None

    @property
    def formatted_citation(self) -> str:
        """生成格式化的引用字符串"""
        authors_str = ", ".join(self.authors[:3])
        if len(self.authors) > 3:
            authors_str += " et al."
        
        parts = [authors_str]
        if self.publication_date:
            parts.append(f"({self.publication_date.year})")
        parts.append(f'"{self.title}"')
        if self.journal:
            parts.append(self.journal)
        if self.doi:
            parts.append(f"DOI: {self.doi}")
        
        return ". ".join(parts)


class PaperSegment(BaseModel):
    """
    论文内容片段 - 模仿 ContentSegment 的设计
    用于存储论文的摘要和翻译内容
    """
    segment_id: int = Field(description="片段唯一ID")
    paper_id: str = Field(description="关联的论文ID")
    
    # 内容
    original_abstract: str = Field(default="", description="原始摘要（英文）")
    translated_abstract: str = Field(default="", description="翻译后的摘要（中文）")
    summary: str = Field(default="", description="LLM生成的精简总结")
    
    # 元数据引用
    metadata: PaperMetadata = Field(description="论文元数据")
    
    # 处理状态
    status: DigestStatus = Field(default=DigestStatus.PENDING, description="处理状态")
    error_message: Optional[str] = Field(None, description="错误信息")
    processed_at: Optional[datetime] = Field(None, description="处理完成时间")
    
    # LLM 处理结果
    translated_title: str = Field(default="", description="翻译后的标题")
    keywords: List[str] = Field(default_factory=list, description="提取的关键词")
    relevance_score: float = Field(default=0.0, description="与研究领域相关度 (0-1)")
    priority_score: float = Field(default=0.0, description="综合优先级得分")
    priority_reason: str = Field(default="", description="优先级评分理由")

    # 过滤裁决（三态 decision/bucket/flags/role/confidence 随论文进入输出，用于分节与排序）
    filter_decision: Optional["FilterDecision"] = Field(None, description="该论文的过滤裁决记录")

    # 全文精读（从正文归纳，句级三色联想标记；仅对高优先级 top-N 生成）
    close_reading: Optional["CloseReading"] = Field(None, description="全文精读结果（结构化+句级标记）")

    model_config = {'use_enum_values': True}

    @property
    def is_processed(self) -> bool:
        """检查是否已处理"""
        return self.status == DigestStatus.COMPLETED

    @property
    def is_translated(self) -> bool:
        """检查是否已翻译"""
        if not self.translated_abstract or not self.translated_abstract.strip():
            return False
        # 检查是否是失败标签
        failed_markers = ["[Translation Failed", "[翻译失败"]
        for marker in failed_markers:
            if marker in self.translated_abstract:
                return False
        return True

    def to_batch_item(self) -> Dict[str, Any]:
        """转换为批量处理的格式（模仿 batch 格式）"""
        return {
            "segment_id": self.segment_id,
            "paper_id": self.paper_id,
            "title": self.metadata.title,
            "authors": self.metadata.formatted_citation,
            "original_text": self.original_abstract,
            "doi": self.metadata.doi,
            "field": self.metadata.field,
        }


class EmailMetadata(BaseModel):
    """邮件元数据"""
    email_id: str = Field(description="邮件唯一ID（Gmail message ID）")
    thread_id: Optional[str] = Field(None, description="邮件线程ID")
    subject: str = Field(description="邮件主题")
    sender: str = Field(description="发件人")
    received_at: datetime = Field(description="接收时间")
    
    # Google Scholar 特有
    is_google_scholar: bool = Field(default=False, description="是否为Google Scholar邮件")
    alert_query: Optional[str] = Field(None, description="Scholar Alert的搜索查询")
    
    # 处理状态
    is_read: bool = Field(default=False, description="是否已读")
    is_processed: bool = Field(default=False, description="是否已处理")
    papers_extracted: int = Field(default=0, description="提取的论文数量")

    model_config = {'use_enum_values': True}


class FilterDecision(BaseModel):
    """单篇论文的过滤裁决记录（写入 excluded sidecar，保证可审计性）

    方法学审稿三态：decision(INCLUDE/MAYBE/EXCLUDE) + bucket/flags/role/one_line/confidence。
    verdict 兼容旧 sidecar 消费方：INCLUDE/MAYBE→included，EXCLUDE→excluded；
    undecided 仅用于 LLM 裁决失败的关键词回退——白名单未命中≠确认无关，不能冒充 excluded。
    """
    paper_id: str = Field(description="论文唯一标识")
    title: str = Field(description="论文标题")
    verdict: Literal["included", "excluded", "undecided"] = Field(description="裁决结论（向后兼容二值 + 回退未决态）")
    decision: Optional[Literal["INCLUDE", "MAYBE", "EXCLUDE"]] = Field(
        None, description="三态裁决（LLM 方法学审稿；关键词阶段为 None）"
    )
    stage: Literal["blacklist_keyword", "llm_judge", "whitelist_keyword", "keyword_fallback"] = Field(
        description="裁决阶段：黑名单确定性匹配 / LLM 相关性裁决 / 关键词白名单 / LLM 失败后的关键词回退"
    )
    reason: str = Field(default="", description="裁决理由（LLM 的 one_line 或命中的关键词）")
    bucket: List[str] = Field(default_factory=list, description="命中的纳入维度（A..G）")
    exclude_reason: Optional[str] = Field(None, description="排除维度代码（X1..X7），仅 EXCLUDE 时有值")
    flags: List[str] = Field(default_factory=list, description="危险信号标记（THREAT/BENCHMARK/OVERCLAIM_PRECEDENT）")
    role: Optional[str] = Field(None, description="对本文的角色：CITE_SUPPORT/CITE_CONTRAST/MUST_ENGAGE/BACKGROUND/NONE")
    one_line: str = Field(default="", description="一句话用处说明（≤30字）")
    confidence: Optional[float] = Field(None, description="裁决置信度 0.0-1.0")
    model: Optional[str] = Field(None, description="做出裁决的 LLM 模型（确定性阶段为 None）")
    prompt_version: Optional[str] = Field(None, description="裁决 prompt 版本标签+模板哈希（如 filter-v2@a1b2c3d4）")
    decided_at: datetime = Field(default_factory=datetime.now, description="裁决时间")


# ==================== 全文精读结果 ====================

# 句级角色标记：按「对后续工作流的用途」分三类（可空=普通句子）。
#   可引用证据 citable   —— 带具体数字/效应量/可溯源结果，写作时可直接取证
#   可反驳观点 refutable —— 作者的主张/立场/可质疑处，写 critique 时的靶子
#   方法论借鉴 method    —— 可迁移到自身研究的手法
# 旧三类（方法学创新/重要发现/研究背景）保留在 Literal 中仅为**向后兼容加载历史 bundle**，
# 新产出只用新三类（见 ROLE_TAGS）；索引层用 TAG_TO_ROLE 把中文 tag 归一到英文 role slug。
ROLE_TAGS = ("可引用证据", "可反驳观点", "方法论借鉴")
CloseReadTag = Literal[
    "可引用证据", "可反驳观点", "方法论借鉴",
    "方法学创新", "重要发现", "研究背景",  # legacy（历史 bundle 反序列化用）
]

# 中文 tag → 英文 role slug（工作流按 slug 检索）。None = 不进 highlights（去噪）。
TAG_TO_ROLE = {
    "可引用证据": "citable",
    "可反驳观点": "refutable",
    "方法论借鉴": "method",
    # 历史近似映射（换轴前的旧三类，不重跑 LLM）：
    "方法学创新": "method",
    "重要发现": "citable",
    "研究背景": None,
}


class CloseReadSentence(BaseModel):
    """精读正文的一个句子/片段，可带一个句级角色标记。"""
    text: str = Field(description="句子文本（中文）")
    tag: Optional[CloseReadTag] = Field(None, description="句级角色：可引用证据/可反驳观点/方法论借鉴，或无")


class CloseReadSection(BaseModel):
    """精读的一个分节（研究问题/方法与数据/关键结论/可质疑点/对我研究的联想）。"""
    heading: str = Field(description="分节标题")
    sentences: List[CloseReadSentence] = Field(default_factory=list, description="句级片段列表")


class CloseReading(BaseModel):
    """单篇论文的全文精读结果（结构化 + 句级三色标记）。"""
    from_full_text: bool = Field(True, description="是否基于全文（False=付费墙无全文，摘要级降级）")
    sections: List[CloseReadSection] = Field(default_factory=list, description="分节精读内容")
    model: Optional[str] = Field(None, description="精读使用的 LLM 模型")
    source: Optional[str] = Field(None, description="全文来源：arxiv/unpaywall/abstract")
    read_at: datetime = Field(default_factory=datetime.now, description="精读时间")

    # ---- 阅读深度量尺（全部 Optional 且默认 None：老 bundle / 老 digest JSON 反序列化不炸）----
    # 这几个字段只回答一个问题：这篇的「精读」到底喂进去了多少正文、有没有被截断、读了几跳。
    # 不装这把尺子就只能靠事后数 highlights 猜深浅，auto 薄输出会被静默当成正常精读。
    body_chars: Optional[int] = Field(None, description="真正喂进 LLM 的正文字符数")
    # body_chars_raw 的语义只有一条：抽取源的全部原始长度，**不受单页上限(page_max_chars)影响**，
    # 也不受 max_chars 截断影响。cap 之后的长度已由 body_chars 表达；raw 若也按 cap 收，
    # truncated 会自证为假（raw == body_chars 恒成立），量尺就废了。
    # 边界：EPMC 路线仅当文章正文超过 2,000,000 字符时 body_chars_raw 退化为下界。
    body_chars_raw: Optional[int] = Field(None, description="抽取到的原始正文长度（未经任何截断）")
    truncated: Optional[bool] = Field(None, description="正文是否被截断：body_chars_raw > body_chars")
    n_chunks: Optional[int] = Field(None, description="分块精读的块数（单跳为 1）")
    # reading_depth 四态（与 AGENTS.md 逐字一致，不得有第二套定义）：
    #   'chunked'        = manual 全部 + 开关打开后的 auto
    #   'single-call'    = auto 单跳
    #   'unknown-legacy' = 仅 auto 存量条目（由回填写入）
    #   缺失 / None      = 只可能出现在 has_full_text_reading == false 的非精读条目上
    reading_depth: Optional[str] = Field(None, description="阅读深度：single-call / chunked / unknown-legacy")


# 解析 PaperSegment 的前向引用（FilterDecision / CloseReading 定义在其后）
PaperSegment.model_rebuild()


class DigestBatch(BaseModel):
    """
    摘要批次 - 模仿翻译批次的设计
    用于批量发送给LLM进行总结
    """
    batch_id: str = Field(description="批次唯一ID")
    segments: List[PaperSegment] = Field(default_factory=list, description="批次中的论文片段")
    
    # 批次信息
    created_at: datetime = Field(default_factory=datetime.now, description="创建时间")
    processed_at: Optional[datetime] = Field(None, description="处理完成时间")
    status: DigestStatus = Field(default=DigestStatus.PENDING, description="批次状态")
    
    # 统计
    total_papers: int = Field(default=0, description="论文总数")
    processed_papers: int = Field(default=0, description="已处理论文数")

    @model_validator(mode='after')
    def update_counts(self) -> 'DigestBatch':
        """更新统计数据"""
        self.total_papers = len(self.segments)
        self.processed_papers = sum(1 for s in self.segments if s.is_processed)
        return self

    def to_llm_prompt(self) -> str:
        """生成发送给LLM的批量总结prompt"""
        papers_data = []
        for seg in self.segments:
            papers_data.append({
                "id": seg.segment_id,
                "title": seg.metadata.title,
                "authors": ", ".join(seg.metadata.authors[:3]),
                "abstract": seg.original_abstract,
                "field": seg.metadata.field,
                "doi": seg.metadata.doi,
            })
        return json.dumps(papers_data, ensure_ascii=False, indent=2)


class DigestOutput(BaseModel):
    """
    摘要输出 - 完整的处理结果
    模仿书籍的结构，存储为本地JSON文件
    """
    # 基本信息
    digest_id: str = Field(description="摘要唯一ID")
    title: str = Field(default="Scholar Digest", description="摘要标题")
    description: str = Field(default="", description="摘要描述")
    
    # 时间范围
    date_range_start: Optional[date] = Field(None, description="论文日期范围开始")
    date_range_end: Optional[date] = Field(None, description="论文日期范围结束")
    created_at: datetime = Field(default_factory=datetime.now, description="创建时间")
    updated_at: datetime = Field(default_factory=datetime.now, description="更新时间")
    
    # 内容
    segments: List[PaperSegment] = Field(default_factory=list, description="论文片段列表")
    # LLM 裁决失败回退时白名单未命中的论文：单独成列，segments 全链只含 included，
    # 下游消费方（enrich/close_read/write_notes/zotero）结构上碰不到未决论文；旧 digest JSON 无此字段可正常加载。
    undecided_segments: List[PaperSegment] = Field(
        default_factory=list, description="待人工复核的论文（LLM 裁决失败且未命中白名单）"
    )
    emails_processed: List[EmailMetadata] = Field(default_factory=list, description="已处理的邮件列表")
    
    # 统计
    total_papers: int = Field(default=0, description="论文总数")
    total_emails: int = Field(default=0, description="邮件总数")
    fields_distribution: Dict[str, int] = Field(default_factory=dict, description="领域分布")
    
    # 状态
    status: DigestStatus = Field(default=DigestStatus.PENDING, description="整体状态")

    @model_validator(mode='after')
    def update_statistics(self) -> 'DigestOutput':
        """更新统计数据"""
        self.total_papers = len(self.segments)
        self.total_emails = len(self.emails_processed)
        
        # 计算领域分布
        fields = {}
        for seg in self.segments:
            field = seg.metadata.field
            fields[field] = fields.get(field, 0) + 1
        self.fields_distribution = fields
        
        # 更新日期范围
        dates = [seg.metadata.publication_date for seg in self.segments if seg.metadata.publication_date]
        if dates:
            self.date_range_start = min(dates)
            self.date_range_end = max(dates)
        
        self.updated_at = datetime.now()
        return self

    def save_to_file(self, output_path: Path) -> Path:
        """保存到JSON文件"""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # tmp+replace 原子写（同 merge_final.py）：digest JSON 是周度入库唯一裁决来源，
        # 半写文件会让整周论文静默不入库；一处改动同时保护 Step 4 首写与 Step 4.5 精读回写
        tmp_path = output_path.with_suffix(output_path.suffix + '.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(self.model_dump(mode='json'), f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_path, output_path)

        return output_path

    @classmethod
    def load_from_file(cls, file_path: Path) -> 'DigestOutput':
        """从JSON文件加载"""
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls.model_validate(data)

    def to_markdown(self) -> str:
        """转换为Markdown格式"""
        lines = [
            "# {}".format(self.title),
            "",
            "**生成时间**: {}".format(self.created_at.strftime('%Y-%m-%d %H:%M')),
            "**论文数量**: {}".format(self.total_papers),
            "**邮件数量**: {}".format(self.total_emails),
            "",
            "## 领域分布",
            "",
        ]
        
        for field, count in sorted(self.fields_distribution.items(), key=lambda x: -x[1]):
            lines.append("- {}: {}".format(field, count))
        
        lines.extend(["", "---", "", "## 论文列表", ""])
        
        for i, seg in enumerate(self.segments, 1):
            meta = seg.metadata
            # 裁决徽章：三态 decision + 危险信号 flags + 角色 role（方法学审稿输出）
            badge = ""
            fd = seg.filter_decision
            if fd:
                parts = []
                if fd.decision:
                    parts.append("`{}`".format(fd.decision))
                if fd.bucket:
                    parts.append("维度 {}".format("/".join(fd.bucket)))
                if fd.flags:
                    parts.append("⚑ " + "/".join(fd.flags))
                if fd.role and fd.role != "NONE":
                    parts.append("角色 {}".format(fd.role))
                if fd.confidence is not None:
                    parts.append("conf {:.2f}".format(fd.confidence))
                if parts:
                    badge = " ".join(parts)
            title_line = "### {}. {}".format(i, meta.title)
            lines.append(title_line)
            lines.append("")
            if badge:
                lines.append("**裁决**: {}".format(badge))
            if fd and fd.one_line:
                lines.append("**用处**: {}".format(fd.one_line))
            if meta.source_type and meta.source_type != "unknown":
                lines.append("**来源**: {}".format(meta.source_type))
            lines.extend([
                "**优先级**: `{:.2f}` ({})".format(seg.priority_score, meta.priority_reason or ""),
                "**引用数**: {}".format(meta.citation_count if meta.citation_count is not None else "Unknown"),
                "**作者**: {}{}".format(', '.join(meta.authors[:5]), '...' if len(meta.authors) > 5 else ''),
                "**领域**: {}".format(meta.field),
            ])
            if meta.doi:
                lines.append("**DOI**: [{0}](https://doi.org/{0})".format(meta.doi))
            if meta.journal:
                lines.append("**期刊**: {}".format(meta.journal))
            
            lines.extend(["", "#### 摘要", ""])
            if seg.translated_abstract:
                lines.append(seg.translated_abstract)
            elif seg.original_abstract:
                lines.append(seg.original_abstract)
            else:
                lines.append("*摘要暂无*")
            
            if seg.summary:
                lines.extend(["", "#### AI总结", "", seg.summary])

            lines.extend(["", "---", ""])

        # 回退未决论文必须在人读的 digest 里显式露出（此前只藏在 sidecar，等于静默丢弃）
        if self.undecided_segments:
            lines.extend(["## 待人工复核（LLM 裁决失败回退）", ""])
            for i, seg in enumerate(self.undecided_segments, 1):
                meta = seg.metadata
                lines.append("### {}. {}".format(i, meta.title))
                lines.append("")
                if meta.source_type and meta.source_type != "unknown":
                    lines.append("**来源**: {}".format(meta.source_type))
                if meta.doi:
                    lines.append("**DOI**: [{0}](https://doi.org/{0})".format(meta.doi))
                elif meta.url:
                    lines.append("**链接**: {}".format(meta.url))
                fd = seg.filter_decision
                if fd and fd.reason:
                    lines.append("**原因**: {}".format(fd.reason))
                lines.extend(["", "---", ""])

        return "\n".join(lines)


# ==================== 配置类 ====================

class GmailAPISettings(BaseModel):
    """Gmail API 配置"""
    credentials_path: Path = Field(
        Path("config/credentials.json"),
        validation_alias=AliasChoices("credentials_path", "GMAIL_CREDENTIALS_PATH"),
        description="OAuth 2.0 客户端凭据文件路径"
    )
    token_path: Path = Field(
        Path("config/token.json"),
        validation_alias=AliasChoices("token_path", "GMAIL_TOKEN_PATH"),
        description="OAuth 2.0 令牌存储路径"
    )
    scopes: List[str] = Field(
        default_factory=lambda: [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify"
        ],
        description="Gmail API 权限范围"
    )
    
    @field_validator('credentials_path', 'token_path', mode='before')
    @classmethod
    def convert_to_path(cls, v):
        if v is None:
            return v
        return Path(v)


class LLMSettings(BaseModel):
    """LLM 配置（用于论文摘要）"""
    provider: str = Field("deepseek", validation_alias=AliasChoices("provider", "LLM_PROVIDER"), description="LLM提供商 (gemini, deepseek, ollama, claude-agent, openai-compatible)")
    api_key: Optional[str] = Field(None, validation_alias=AliasChoices("api_key", "GEMINI_API_KEY"), description="Gemini API密钥")
    model: str = Field("deepseek-v4-flash", validation_alias=AliasChoices("model", "LLM_MODEL"), description="模型名称")
    # 回退链（逗号分隔）：主 provider 欠费/认证失败/CLI 缺失/内容拦截时依序切换。
    # 与 translator 侧对齐的推荐链：deepseek → ollama → claude-agent → gemini
    fallback_providers: str = Field("", validation_alias=AliasChoices("fallback_providers", "FALLBACK_PROVIDERS"), description="回退提供商链，逗号分隔（如 'ollama,claude-agent,gemini'）")
    # 本地 Ollama 独立配置（与 openai 兼容组分离，可同链共存）
    ollama_base_url: str = Field("http://localhost:11434/v1", validation_alias=AliasChoices("ollama_base_url", "OLLAMA_BASE_URL"), description="Ollama 服务地址（OpenAI 兼容端点）")
    ollama_model: str = Field("qwen3.5:35b", validation_alias=AliasChoices("ollama_model", "OLLAMA_MODEL"), description="Ollama 模型名称")

    # OpenAI 兼容提供商（DeepSeek 等）。未设置时回退复用主配置
    # config/config.env 中的 API__OPENAI_API_KEY / API__OPENAI_BASE_URL
    openai_api_key: Optional[str] = Field(None, validation_alias=AliasChoices("openai_api_key", "OPENAI_API_KEY"), description="OpenAI兼容API密钥（DeepSeek等）")
    base_url: Optional[str] = Field(None, validation_alias=AliasChoices("base_url", "LLM_BASE_URL"), description="OpenAI兼容API地址")

    # 白名单裁决专用模型（None 时复用主模型 model）
    filter_model: Optional[str] = Field(None, validation_alias=AliasChoices("filter_model", "FILTER_MODEL"), description="过滤裁决使用的模型，未设置时复用 model")
    # 全文精读专用模型（强模型；None 时复用主模型 model）
    closeread_model: Optional[str] = Field(None, validation_alias=AliasChoices("closeread_model", "CLOSEREAD_MODEL"), description="全文精读使用的强模型，未设置时复用 model")

    # 生成参数
    temperature: float = Field(0.3, description="生成温度")
    max_output_tokens: int = Field(8192, description="最大输出token数")


class ProcessingSettings(BaseModel):
    """处理配置"""
    batch_size: int = Field(5, validation_alias=AliasChoices("batch_size", "BATCH_SIZE"), description="批量处理大小")
    max_emails: int = Field(100, validation_alias=AliasChoices("max_emails", "MAX_EMAILS"), description="最大处理邮件数")
    days_to_fetch: int = Field(7, validation_alias=AliasChoices("days_to_fetch", "DAYS_TO_FETCH"), description="获取最近N天的邮件")
    
    # 过滤
    scholar_sender: str = Field(
        "scholaralerts-noreply@google.com",
        description="Google Scholar 发件人地址"
    )
    
    # 过滤模式：llm = 黑名单确定性预过滤 + LLM 相关性裁决（宽松语义匹配）
    #           keyword = 传统黑白名单关键词匹配
    filter_mode: Literal["llm", "keyword"] = Field(
        "llm",
        validation_alias=AliasChoices("filter_mode", "FILTER_MODE"),
        description="白名单过滤模式：llm（LLM 相关性裁决）或 keyword（关键词匹配）"
    )

    # 研究方向描述 / 论文主题与定位（注入 LLM 裁决 prompt 的 {{RESEARCH_INTERESTS}}）
    research_interests: str = Field(
        "论文主题：EHR 缺失机制（MNAR）与「缺失条件依赖结构」的跨中心可迁移性；"
        "定位为 causal hypothesis generation，不做 causal identification。"
        "基础工作：MA-GCT（特征专属可学习掩码嵌入 + Guide/Prior 注意力约束）。",
        validation_alias=AliasChoices("research_interests", "RESEARCH_INTERESTS"),
        description="论文主题与定位，用于 LLM 方法学审稿裁决"
    )

    # 关键词过滤（keyword 模式的白名单；两种模式共用黑名单做确定性预过滤）
    whitelist: List[str] = Field(
        default_factory=lambda: [
            # EHR / 临床预测
            "EHR", "Electronic Health Record", "EMR", "Electronic Medical Record",
            "Clinical Prediction", "Predictive Model", "Risk Prediction", "Clinical Decision Support",
            # 缺失机制方法学
            "MNAR", "MAR", "missing not at random", "missingness", "informative missingness",
            "missing data", "imputation", "mask embedding", "missingness-aware", "missingness indicator",
            # 缺失 × 因果
            "missingness graph", "m-graph", "causal discovery", "causal structure", "identifiability",
            # 跨域 / 迁移
            "transportability", "domain shift", "dataset shift", "distribution shift",
            "external validation", "LODO", "site heterogeneity", "generalization",
            # 建模
            "GNN", "Graph Neural Network", "Graph Convolutional", "attention", "feature propagation",
            "LLM", "Large Language Model", "foundation model", "Transformer",
            "Semi-supervised", "Active Learning",
            # venue 基准 / 临床场景
            "MIMIC", "eICU", "AmsterdamUMCdb", "HiRID", "ICU",
            "sepsis", "AKI", "acute kidney injury", "CKD", "mortality prediction",
        ],
        description="白名单关键词：keyword 模式下必须命中其一；llm 模式下仅作 LLM 失败时的回退"
    )
    blacklist: List[str] = Field(
        default_factory=lambda: [
            # 只保留与 EHR/临床机器学习方法学“明确无关”的领域，避免抢在 LLM 之前误杀
            # 影像/基因组/信号等由 LLM 依 X1..X7 判定（否则会误杀 MNAR×影像 等对抗性证据）。
            "天文", "Astronomy", "Astrophysics", "天体",
            "材料科学", "Materials Science", "地质", "Geology",
            "农业", "Agriculture", "考古", "Archaeology",
            "particle physics", "量子化学", "Quantum Chemistry", "催化", "Catalysis",
            "植物学", "Botany", "昆虫", "Entomology",
        ],
        description="黑名单关键词：仅无歧义的完全离题领域（确定性预过滤）；X1-X7 交给 LLM 判定"
    )
    
    # 外部检索来源（PubMed / arXiv，并入每周 digest）
    external_sources_enabled: bool = Field(
        True,
        validation_alias=AliasChoices("external_sources_enabled", "EXTERNAL_SOURCES_ENABLED"),
        description="是否在 digest 中并入 PubMed/arXiv 检索式抓取的论文"
    )
    external_max_results: int = Field(
        25,
        validation_alias=AliasChoices("external_max_results", "EXTERNAL_MAX_RESULTS"),
        description="每个外部来源最多抓取的论文数"
    )
    external_email: str = Field(
        "",
        validation_alias=AliasChoices("external_email", "EXTERNAL_EMAIL"),
        description="NCBI E-utilities 礼貌参数用的联系邮箱（可选，无需密钥）"
    )
    arxiv_query: str = Field(
        "(all:MNAR OR all:\"missing not at random\" OR all:\"informative missingness\" "
        "OR all:\"missingness mechanism\") AND (all:EHR OR all:\"electronic health record\" "
        "OR all:ICU OR all:\"clinical prediction\") AND (all:\"causal discovery\" "
        "OR all:transportability OR all:\"distribution shift\" OR all:\"external validation\" "
        "OR all:attention OR all:\"graph neural network\")",
        validation_alias=AliasChoices("arxiv_query", "ARXIV_QUERY"),
        description="arXiv 检索式（Atom API 语法，字段前缀 all:/abs:）"
    )
    pubmed_query: str = Field(
        "(missing not at random OR MNAR OR informative missingness OR missingness mechanism) "
        "AND (electronic health record OR EHR OR ICU OR clinical prediction) "
        "AND (causal discovery OR causal structure OR transportability OR distribution shift "
        "OR external validation OR attention OR graph neural network)",
        validation_alias=AliasChoices("pubmed_query", "PUBMED_QUERY"),
        description="PubMed 检索式（E-utilities term 语法）"
    )

    # Zotero 联动（B1 全自动写库 + citekey + pandoc 札记）
    # 默认关闭：写库需 Zotero 桌面端在线，无人值守 cron 下可能没开，建议交互式运行。
    zotero_enabled: bool = Field(
        False,
        validation_alias=AliasChoices("zotero_enabled", "ZOTERO_ENABLED"),
        description="digest 后是否把入选论文写入 Zotero 并生成 pandoc 札记"
    )
    zotero_base_url: str = Field(
        "http://localhost:23119",
        validation_alias=AliasChoices("zotero_base_url", "ZOTERO_BASE_URL"),
        description="Zotero 连接器服务地址（本地）"
    )
    zotero_collection: str = Field(
        "",
        validation_alias=AliasChoices("zotero_collection", "ZOTERO_COLLECTION"),
        description="要求写入的分类名（防呆）：设了就必须在 Zotero 里选中同名分类，否则拒绝写入；空=写入当前选中"
    )
    zotero_attach_pdf: bool = Field(
        True,
        validation_alias=AliasChoices("zotero_attach_pdf", "ZOTERO_ATTACH_PDF"),
        description="是否解析 OA PDF（arXiv/Unpaywall）作为附件让 Zotero 自抓"
    )
    zotero_enrich_crossref: bool = Field(
        True,
        validation_alias=AliasChoices("zotero_enrich_crossref", "ZOTERO_ENRICH_CROSSREF"),
        description="写库前用 Crossref 标题检索规范作者+补 DOI（修正 Scholar 邮件解析的脏作者）"
    )
    zotero_translation_server_url: str = Field(
        "",
        validation_alias=AliasChoices("zotero_translation_server_url", "ZOTERO_TRANSLATION_SERVER_URL"),
        description="Zotero translation-server 地址（如 http://localhost:1969）。设了则把标识符交给它做权威解析；空=用自建 item"
    )
    zotero_email: str = Field(
        "",
        validation_alias=AliasChoices("zotero_email", "ZOTERO_EMAIL"),
        description="Unpaywall 礼貌参数邮箱（联系句柄，非密钥）；为空时复用 external_email"
    )
    notes_dir: Path = Field(
        Path("output/scholar_notes"),
        validation_alias=AliasChoices("notes_dir", "NOTES_DIR"),
        description="pandoc 科研札记输出目录"
    )
    notes_instruction: str = Field(
        "",
        validation_alias=AliasChoices("notes_instruction", "NOTES_INSTRUCTION"),
        description="自定义归纳指令（写入札记手记区注释，指导你后续整理）"
    )
    # 全文精读（从正文归纳 + 句级三色联想）
    closeread_enabled: bool = Field(
        False,
        validation_alias=AliasChoices("closeread_enabled", "CLOSEREAD_ENABLED"),
        description="是否对高优先级 top-N 做全文精读（下 PDF 抽全文 → 强模型精读）"
    )
    closeread_top_n: int = Field(
        5,
        validation_alias=AliasChoices("closeread_top_n", "CLOSEREAD_TOP_N"),
        description="每次精读覆盖的高优先级论文数（控成本）"
    )
    # 分块深读（复用 manual 精读的 chunk→synth 通路）。**默认关**：它是唯一真正抬高
    # 无人值守产线成本的开关（单批 top_n=5 约 42 次 LLM 调用，最坏 65 次），
    # 不打开则全部产线行为与今天逐字节一致，回退 = 把开关关回去。
    closeread_deep: bool = Field(
        False,
        validation_alias=AliasChoices("closeread_deep", "CLOSEREAD_DEEP"),
        description="精读是否走分块深读（切块逐块通读 + 汇总），关闭则维持单跳精读"
    )
    # 120000 的依据：263 篇实测正文长度 p75=100,086、p90=128,272，120k 覆盖 p75；
    # 该上限下实测最坏块数 11，正好被下面 12 的块顶卡住（两个数是配套的，改一个要重算另一个）。
    closeread_max_chars: int = Field(
        120000,
        validation_alias=AliasChoices("closeread_max_chars", "CLOSEREAD_MAX_CHARS"),
        description="深读模式下喂进模型的正文上限（仅 closeread_deep=True 生效；"
                    "关闭时一律沿用 AUTO_MAX_CHARS=40000）"
    )
    closeread_max_chunks: int = Field(
        12,
        validation_alias=AliasChoices("closeread_max_chunks", "CLOSEREAD_MAX_CHUNKS"),
        description="深读模式下的块数硬顶（防病态长 PDF 切出几十块吃掉整夜窗口）"
    )
    # 样式化 docx 输出
    notes_emit_docx: bool = Field(
        True,
        validation_alias=AliasChoices("notes_emit_docx", "NOTES_EMIT_DOCX"),
        description="生成札记时是否同时产出样式化 docx（单元格着色/句级三色/字体区分）"
    )
    notes_docx_cjk_font: str = Field(
        "",
        validation_alias=AliasChoices("notes_docx_cjk_font", "NOTES_DOCX_CJK_FONT"),
        description="docx 中文字体名（如 思源宋体/宋体）；空=不显式设置"
    )

    # 输出
    output_dir: Path = Field(
        Path("output/scholar_digest"),
        validation_alias=AliasChoices("output_dir", "OUTPUT_DIR"),
        description="输出目录"
    )
    
    # 功能开关
    auto_mark_read: bool = Field(True, description="自动标记邮件为已读")
    translate_abstracts: bool = Field(False, description="是否翻译摘要")
    generate_summary: bool = Field(True, description="是否生成AI总结")

    @field_validator('output_dir', mode='before')
    @classmethod
    def convert_to_path(cls, v):
        if v is None:
            return Path("output/scholar_digest")
        return Path(v)


class ScholarSettings(BaseSettings):
    """Scholar Digest 全局设置"""
    gmail: GmailAPISettings = Field(default_factory=GmailAPISettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    processing: ProcessingSettings = Field(default_factory=ProcessingSettings)
    
    # 日志
    log_level: str = Field("INFO", validation_alias="LOG_LEVEL", description="日志级别")
    log_file: Optional[Path] = Field(
        Path("logs/scholar_digest.log"),
        validation_alias="LOG_FILE",
        description="日志文件路径"
    )

    model_config = SettingsConfigDict(
        env_file=Path('config/scholar.env'),
        env_file_encoding='utf-8',
        env_nested_delimiter='__',
        case_sensitive=False,
        extra='ignore'
    )

    @classmethod
    def from_env_file(cls, env_file_path: Path = Path('config/scholar.env')) -> 'ScholarSettings':
        """从指定的 .env 文件路径加载设置"""
        return cls(_env_file=env_file_path)


# ==================== 类型别名 ====================
PaperSegmentList = List[PaperSegment]
