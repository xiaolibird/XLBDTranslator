# -*- coding: utf-8 -*-
"""
Scholar Digest 数据结构定义
使用 Pydantic 2.0 进行数据验证和序列化
模仿 src/core/schema.py 的设计模式

配置类（ScholarSettings 及其子类）已拆分至 settings.py，本文件末尾
re-export 保持旧 import 路径（from .schema import ScholarSettings 等）向后兼容。
"""
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
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
    # 上面那个 date 的**真实精度**。date 类型必须凑齐年月日才能构造，所以各来源在只拿到
    # 「年」或「年月」时一律补 1 —— 全库 445 条 [[Y,1,1]] 与 689 条 [[Y,M,1]] 就是这么来的，
    # 参考文献于是渲染出论文并不存在的月份（如 "2026 (January)"）。
    # 解法不是让 publication_date 可空（那会连累 citekey 取年与索引 year 字段），
    # 而是**另存精度、只在产出层截断**：CSL 的 issued（出版日期，注意与 issue 期号不是一回事）
    # 按 day/month/year 输出三/二/一段 date-parts；Zotero 的 date 同理截断 ISO 串。
    # None = 存量条目，精度未知，产出层用启发式（(m,d)==(1,1)→年、d==1→年月）兜底。
    date_precision: Optional[Literal["day", "month", "year"]] = Field(
        None, description="publication_date 的真实精度（None=存量未知）")
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

    @classmethod
    def from_digest_json(cls, seg_data: Dict[str, Any]) -> 'PaperSegment':
        """从旧版 digest JSON 的单条 segment dict 还原（deep-research 加载论文用）。

        metadata 里的 date/datetime 字段在裸 JSON 里是 ISO 字符串，PaperMetadata(**dict)
        不会自动 fromisoformat（那是 model_validate 才有的行为），需先手动转换。
        无效数据交给调用方 try/except 处理（原逻辑：跳过并记录警告，不中断整批）。
        """
        meta_data = dict(seg_data.get('metadata', {}))
        if meta_data.get('publication_date') and isinstance(meta_data['publication_date'], str):
            meta_data['publication_date'] = date.fromisoformat(meta_data['publication_date'])
        if meta_data.get('email_received_at') and isinstance(meta_data['email_received_at'], str):
            meta_data['email_received_at'] = datetime.fromisoformat(meta_data['email_received_at'])
        if meta_data.get('extracted_at') and isinstance(meta_data['extracted_at'], str):
            meta_data['extracted_at'] = datetime.fromisoformat(meta_data['extracted_at'])

        meta = PaperMetadata(**meta_data)
        return cls(
            segment_id=seg_data.get('segment_id', 0),
            paper_id=seg_data.get('paper_id', ''),
            metadata=meta,
            original_abstract=seg_data.get('original_abstract', ''),
            translated_abstract=seg_data.get('translated_abstract', ''),
            priority_score=seg_data.get('priority_score', 0.0),
        )


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
    prompt_version: Optional[str] = Field(None, description="裁决 prompt 版本标签+模板哈希（如 filter-v3@a1b2c3d4）")
    decided_at: datetime = Field(default_factory=datetime.now, description="裁决时间")
    # RAG phase 3：札记库语义近邻（{citekey, year, one_line, sim}），随裁决固化供复盘；
    # 向量库/Ollama 不可用时上游返回空列表，此处默认 [] 且不影响旧 digest JSON 反序列化。
    library_neighbors: List[dict] = Field(
        default_factory=list, description="裁决时参考的札记库语义近邻（本地已收文献）"
    )


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
    # 汇总步的预算裁剪留痕。缺失 = 未知（老 bundle / 老 digest JSON），不是"没裁过"——
    # 与 fulltext_chars 那组量尺同一套「缺失≠false」口径。n_chunks 说的是"读了几块"，
    # 这两项说的是"其中有多少进了汇总 prompt"，不写出来 chunked/12 块就是虚报。
    synth_truncated: Optional[bool] = Field(None, description="汇总步是否对块笔记做过裁剪")
    synth_dropped_chunks: Optional[int] = Field(
        None, description="汇总步整块丢弃的块数（均摊裁剪后仍超预算时才发生）")
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
        tmp_path = output_path.with_suffix(output_path.suffix + '.tmp-{}'.format(os.getpid()))
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


# ==================== 配置类 re-export ====================
# 实体定义已迁往 settings.py；此处 re-export 保持旧路径
# `from .schema import ScholarSettings` / `from src.scholar.schema import LLMSettings` 等继续可用。
from .settings import (  # noqa: E402,F401
    GmailAPISettings,
    LLMSettings,
    ProcessingSettings,
    ScholarSettings,
)


# ==================== 类型别名 ====================
PaperSegmentList = List[PaperSegment]
