# -*- coding: utf-8 -*-
"""
Scholar Digest 数据结构定义
使用 Pydantic 2.0 进行数据验证和序列化
模仿 src/core/schema.py 的设计模式
"""
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from datetime import datetime, date
import json


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
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(self.model_dump(mode='json'), f, ensure_ascii=False, indent=2, default=str)
        
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
            lines.extend([
                "### {}. {}".format(i, meta.title),
                "",
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
        
        return "\n".join(lines)


# ==================== 配置类 ====================

class GmailAPISettings(BaseModel):
    """Gmail API 配置"""
    credentials_path: Path = Field(
        Path("config/credentials.json"),
        validation_alias="GMAIL_CREDENTIALS_PATH",
        description="OAuth 2.0 客户端凭据文件路径"
    )
    token_path: Path = Field(
        Path("config/token.json"),
        validation_alias="GMAIL_TOKEN_PATH",
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
    provider: str = Field("gemini", validation_alias="LLM_PROVIDER", description="LLM提供商")
    api_key: Optional[str] = Field(None, validation_alias="GEMINI_API_KEY", description="API密钥")
    model: str = Field("gemini-2.0-flash", validation_alias="LLM_MODEL", description="模型名称")
    
    # 生成参数
    temperature: float = Field(0.3, description="生成温度")
    max_output_tokens: int = Field(8192, description="最大输出token数")


class ProcessingSettings(BaseModel):
    """处理配置"""
    batch_size: int = Field(5, validation_alias="BATCH_SIZE", description="批量处理大小")
    max_emails: int = Field(100, validation_alias="MAX_EMAILS", description="最大处理邮件数")
    days_to_fetch: int = Field(7, validation_alias="DAYS_TO_FETCH", description="获取最近N天的邮件")
    
    # 过滤
    scholar_sender: str = Field(
        "scholaralerts-noreply@google.com",
        description="Google Scholar 发件人地址"
    )
    
    # 关键词过滤
    whitelist: List[str] = Field(
        default_factory=lambda: ["EHR", "Clinical", "Prediction", "GNN", "LLM", "Graph", "Medicine"],
        description="白名单关键词：包含任一则保留"
    )
    blacklist: List[str] = Field(
        default_factory=lambda: ["Biology", "Gene", "Drug", "Vision"],
        description="黑名单关键词：包含任一则剔除"
    )
    
    # 输出
    output_dir: Path = Field(
        Path("output/scholar_digest"),
        validation_alias="OUTPUT_DIR",
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
