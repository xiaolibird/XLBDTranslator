# -*- coding: utf-8 -*-
"""
Scholar Digest 配置定义
从 schema.py 拆分而来：ScholarSettings 及其全部配置子类/构建辅助函数。
schema.py 保留论文域模型，本文件保留配置模型，二者通过 schema.py 末尾的
re-export 对外仍呈现同一路径（向后兼容旧 import）。
"""
from pathlib import Path
from typing import Optional, List, Literal
from pydantic import AliasChoices, BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .research_profile import get_default_research_interests as _default_research_interests
from .research_profile import get_whitelist_keywords as _default_whitelist_keywords


# ==================== 配置类 ====================

class GmailAPISettings(BaseModel):
    """Gmail API 配置"""
    credentials_path: Path = Field(
        Path("config/credentials.json"),
        description="OAuth 2.0 客户端凭据文件路径"
    )
    token_path: Path = Field(
        Path("config/token.json"),
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
    provider: str = Field("deepseek", description="LLM提供商 (gemini, deepseek, ollama, claude-agent, openai-compatible)")
    api_key: Optional[str] = Field(None, validation_alias=AliasChoices("api_key", "GEMINI_API_KEY"), description="Gemini API密钥")
    model: str = Field("deepseek-v4-flash", description="模型名称")
    # 回退链（逗号分隔）：主 provider 欠费/认证失败/CLI 缺失/内容拦截时依序切换。
    # 与 translator 侧对齐的推荐链：deepseek → ollama → claude-agent → gemini
    fallback_providers: str = Field("", description="回退提供商链，逗号分隔（如 'ollama,claude-agent,gemini'）")
    # 本地 Ollama 独立配置（与 openai 兼容组分离，可同链共存）
    ollama_base_url: str = Field("http://localhost:11434/v1", description="Ollama 服务地址（OpenAI 兼容端点）")
    ollama_model: str = Field("qwen3.5:35b", description="Ollama 模型名称")
    # 札记库语义检索用（scripts/notes_embed.py / notes_search.py），与翻译/摘要模型完全独立
    embedding_model: str = Field("bge-m3", description="本地 embedding 模型（札记库语义检索）")
    embedding_base_url: Optional[str] = Field(None, description="Embedding 服务地址；None 时从 ollama_base_url 去掉尾部 /v1 派生")

    # OpenAI 兼容提供商（DeepSeek 等）。未设置时回退复用主配置
    # config/config.env 中的 API__OPENAI_API_KEY / API__OPENAI_BASE_URL
    openai_api_key: Optional[str] = Field(None, description="OpenAI兼容API密钥（DeepSeek等）")
    base_url: Optional[str] = Field(None, description="OpenAI兼容API地址")

    # 白名单裁决专用模型（None 时复用主模型 model）
    filter_model: Optional[str] = Field(None, description="过滤裁决使用的模型，未设置时复用 model")
    # 全文精读专用模型（强模型；None 时复用主模型 model）
    closeread_model: Optional[str] = Field(None, description="全文精读使用的强模型，未设置时复用 model")

    # 生成参数
    temperature: float = Field(0.3, description="生成温度")
    max_output_tokens: int = Field(8192, description="最大输出token数")


class ProcessingSettings(BaseModel):
    """处理配置"""
    batch_size: int = Field(5, description="批量处理大小")
    max_emails: int = Field(100, description="最大处理邮件数")
    days_to_fetch: int = Field(7, description="获取最近N天的邮件")

    # 过滤
    scholar_sender: str = Field(
        "scholaralerts-noreply@google.com",
        description="Google Scholar 发件人地址"
    )

    # 过滤模式：llm = 黑名单确定性预过滤 + LLM 相关性裁决（宽松语义匹配）
    #           keyword = 传统黑白名单关键词匹配
    filter_mode: Literal["llm", "keyword"] = Field(
        "llm",
        description="白名单过滤模式：llm（LLM 相关性裁决）或 keyword（关键词匹配）"
    )

    # 研究方向描述 / 论文主题与定位（注入 LLM 裁决 prompt 的 {{RESEARCH_INTERESTS}}）
    # 默认值来自 config/research_profile.yaml（换研究方向改那一处即可）；
    # default_factory 只在 env 无覆盖时才被调用，故优先级仍是 env > yaml > 内嵌默认。
    research_interests: str = Field(
        default_factory=_default_research_interests,
        description="论文主题与定位，用于 LLM 方法学审稿裁决"
    )

    # 关键词过滤（keyword 模式的白名单；两种模式共用黑名单做确定性预过滤）
    # 默认值来自 config/research_profile.yaml（whitelist_keywords，换研究方向改那一处即可）；
    # default_factory 只在 env 无覆盖时才被调用，故优先级仍是 env > yaml > 内嵌默认。
    whitelist: List[str] = Field(
        default_factory=_default_whitelist_keywords,
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
        description="是否在 digest 中并入 PubMed/arXiv 检索式抓取的论文"
    )
    external_max_results: int = Field(
        25,
        description="每个外部来源最多抓取的论文数"
    )
    external_email: str = Field(
        "",
        description="NCBI E-utilities 礼貌参数用的联系邮箱（可选，无需密钥）"
    )
    arxiv_query: str = Field(
        "(all:MNAR OR all:\"missing not at random\" OR all:\"informative missingness\" "
        "OR all:\"missingness mechanism\") AND (all:EHR OR all:\"electronic health record\" "
        "OR all:ICU OR all:\"clinical prediction\") AND (all:\"causal discovery\" "
        "OR all:transportability OR all:\"distribution shift\" OR all:\"external validation\" "
        "OR all:attention OR all:\"graph neural network\")",
        description="arXiv 检索式（Atom API 语法，字段前缀 all:/abs:）"
    )
    pubmed_query: str = Field(
        "(missing not at random OR MNAR OR informative missingness OR missingness mechanism) "
        "AND (electronic health record OR EHR OR ICU OR clinical prediction) "
        "AND (causal discovery OR causal structure OR transportability OR distribution shift "
        "OR external validation OR attention OR graph neural network)",
        description="PubMed 检索式（E-utilities term 语法）"
    )

    # Zotero 联动（B1 全自动写库 + citekey + pandoc 札记）
    # 默认关闭：写库需 Zotero 桌面端在线，无人值守 cron 下可能没开，建议交互式运行。
    zotero_enabled: bool = Field(
        False,
        description="digest 后是否把入选论文写入 Zotero 并生成 pandoc 札记"
    )
    zotero_base_url: str = Field(
        "http://localhost:23119",
        description="Zotero 连接器服务地址（本地）"
    )
    zotero_collection: str = Field(
        "",
        description="要求写入的分类名（防呆）：设了就必须在 Zotero 里选中同名分类，否则拒绝写入；空=写入当前选中"
    )
    zotero_attach_pdf: bool = Field(
        True,
        description="是否解析 OA PDF（arXiv/Unpaywall）作为附件让 Zotero 自抓"
    )
    zotero_enrich_crossref: bool = Field(
        True,
        description="写库前用 Crossref 标题检索规范作者+补 DOI（修正 Scholar 邮件解析的脏作者）"
    )
    zotero_translation_server_url: str = Field(
        "",
        description="Zotero translation-server 地址（如 http://localhost:1969）。设了则把标识符交给它做权威解析；空=用自建 item"
    )
    zotero_email: str = Field(
        "",
        description="Unpaywall 礼貌参数邮箱（联系句柄，非密钥）；为空时复用 external_email"
    )
    notes_dir: Path = Field(
        Path("output/scholar_notes"),
        description="pandoc 科研札记输出目录"
    )
    notes_instruction: str = Field(
        "",
        description="自定义归纳指令（写入札记手记区注释，指导你后续整理）"
    )
    # 全文精读（从正文归纳 + 句级三色联想）
    closeread_enabled: bool = Field(
        False,
        description="是否对高优先级 top-N 做全文精读（下 PDF 抽全文 → 强模型精读）"
    )
    closeread_top_n: int = Field(
        5,
        description="每次精读覆盖的高优先级论文数（控成本）"
    )
    # 分块深读（复用 manual 精读的 chunk→synth 通路）。**默认关**：它是唯一真正抬高
    # 无人值守产线成本的开关（单批 top_n=5 约 42 次 LLM 调用，最坏 65 次），
    # 不打开则全部产线行为与今天逐字节一致，回退 = 把开关关回去。
    closeread_deep: bool = Field(
        False,
        description="精读是否走分块深读（切块逐块通读 + 汇总），关闭则维持单跳精读"
    )
    # 120000 的依据：263 篇实测正文长度 p75=100,086、p90=128,272，120k 覆盖 p75；
    # 该上限下实测最坏块数 11，正好被下面 12 的块顶卡住（两个数是配套的，改一个要重算另一个）。
    closeread_max_chars: int = Field(
        120000,
        description="深读模式下喂进模型的正文上限（仅 closeread_deep=True 生效；"
                    "关闭时一律沿用 AUTO_MAX_CHARS=40000）"
    )
    closeread_max_chunks: int = Field(
        12,
        description="深读模式下的块数硬顶（防病态长 PDF 切出几十块吃掉整夜窗口）"
    )
    # 样式化 docx 输出
    notes_emit_docx: bool = Field(
        True,
        description="生成札记时是否同时产出样式化 docx（单元格着色/句级三色/字体区分）"
    )
    notes_docx_cjk_font: str = Field(
        "",
        description="docx 中文字体名（如 思源宋体/宋体）；空=不显式设置"
    )

    # 输出
    output_dir: Path = Field(
        Path("output/scholar_digest"),
        description="输出目录"
    )

    # 功能开关
    auto_mark_read: bool = Field(True, description="自动标记邮件为已读")
    translate_abstracts: bool = Field(False, description="是否翻译摘要")
    # 名不副实：批处理 prompt（workflow._build_batch_prompt）从未要求模型产出独立的
    # summary 字段，本开关实际只决定「翻译/关键词/相关度/优先级均已关闭时是否仍跑一遍
    # LLM 批处理步骤」——不产出任何独立于摘要之外的"AI总结"内容。别指望靠它拿到总结。
    generate_summary: bool = Field(True, description="是否仍执行 LLM 批处理步骤（不产出独立摘要总结，仅影响关键词/相关度/优先级评分）")

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


def load_scholar_settings(config="config/scholar.env", *,
                          patch_gemini: bool = True,
                          require: bool = False) -> ScholarSettings:
    """脚本入口统一的配置加载：锚定路径 + 可选 gemini→deepseek 迁移补丁。

    此前 7 个入口（ingest_notes / read_pdf / backfill_notes / backfill_methods /
    screen_journal / notes_search / cli）各自手写加载，漂移出四类差异：config 是否过
    repo_path、是否打 gemini 补丁、缺失时静默还是回退、notes_dir/output_dir 是否锚定。
    收敛后的统一契约：

    - `config` 一律过 `paths.repo_path` 锚定仓库根（从别处/launchd cwd 未设对时启动，
      不再静默读到另一份不存在的配置而落回默认值）。
    - 缺失时：`require=True` 抛 FileNotFoundError（交互式入口映射成自己的退出码）；
      否则 warning + 裸 ScholarSettings() 回退（无人值守入口的既有语义，静默回退升级
      为有警告）。
    - `patch_gemini=True`（默认）时执行 deepseek-chat 下线后的迁移补丁：config 写的
      是 gemini provider + gemini 模型名 → 实际走 deepseek。**撤销这块补丁时只删这里**。
      cli.py 的 digest 主链路历史上不打此补丁（provider 切换走 --provider 参数），
      必须显式传 patch_gemini=False，勿改变其行为。
    - notes_dir/output_dir 无条件锚定 repo_path：二者默认是相对路径，语义是「仓库里的
      那个目录」，不锚定就会随 cwd 漂（写去另一棵目录树 / 向量库找不到 / filter 的
      library_neighbors 静默全空，见 screen_journal 的历史注释）。
    """
    from ..utils.logger import get_logger
    from .paths import repo_path
    logger = get_logger(__name__)

    cfg = repo_path(config)
    if cfg.exists():
        settings = ScholarSettings.from_env_file(cfg)
        logger.info("已加载配置文件: {}".format(cfg))
    else:
        if require:
            raise FileNotFoundError(cfg)
        logger.warning("配置文件不存在: {}，使用默认配置".format(cfg))
        settings = ScholarSettings()
    if patch_gemini and settings.llm.provider == "gemini" and settings.llm.model.startswith("gemini"):
        settings.llm.provider = "deepseek"
        logger.info("LLM provider 从 gemini 自动切换为 deepseek（model={}）".format(settings.llm.model))
    settings.processing.notes_dir = repo_path(settings.processing.notes_dir)
    settings.processing.output_dir = repo_path(settings.processing.output_dir)
    return settings
