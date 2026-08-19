# -*- coding: utf-8 -*-
"""
Scholar Digest 工作流
完整的论文摘要处理流程
"""
import json
import hashlib
import re
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Optional, Set, Tuple

from .schema import (
    ScholarSettings,
    PaperSegment,
    PaperSegmentList,
    EmailMetadata,
    DigestOutput,
    DigestBatch,
    DigestStatus,
    FilterDecision,
)
from .gmail_client import GmailClient
from .paths import repo_path
from .paper_extractor import ScholarEmailParser
from .research_profile import get_exclusion_dims, get_inclusion_dims
from ._citekey_utils import _norm_title
from ..utils.json_tools import loads_lenient
from ..utils.logger import get_logger

logger = get_logger(__name__)

# LLM 白名单裁决：每次调用送审的论文数（flash 级模型，单批成本极低）
FILTER_BATCH_SIZE = 20

# 裁决 prompt 模板文件；缺失时使用内嵌兜底模板（保证 cron 无人值守可跑）
FILTER_PROMPT_FILE = repo_path("config/prompts/whitelist_filter_prompt.md")

FILTER_PROMPT_FALLBACK = """<!-- version: filter-v3-embedded -->
# ROLE
你是一名方法学审稿助手，为一篇投稿的论文做文献筛选。

## 论文主题与定位
{{RESEARCH_INTERESTS}}

# TASK
对下面每条记录（title + abstract）输出筛选判定，只依据给定文本，不得脑补。

部分记录带 library_neighbors 字段：本地文献库中与该论文语义最相近的已收文献（含相似度 sim，
citekey/year/one_line）。高相似 ≠ 排除——可能是同方向的新进展，正该收；但若与已收文献纯重复、
增量微小，降为 MAYBE，并在 one_line 中点名重复的 citekey。无该字段的记录按无近邻处理。

{{INCLUSION_DIMS}}

{{EXCLUSION_DIMS}}

# RULES
- 宁可 MAYBE 不可 EXCLUDE：与 C 或 E 沾边的一律不低于 MAYBE。
- 结论与本文相左 ≠ 排除理由；THREAT 类 role 设为 MUST_ENGAGE。
- 摘要不足以判定则 decision=MAYBE 且 confidence≤0.4。

# OUTPUT（严格 JSON 对象，顶层不得是数组，无 markdown，无前后缀）
```json
{
  "verdicts": [
    {"id": 900001, "decision": "INCLUDE|MAYBE|EXCLUDE", "bucket": ["A"], "exclude_reason": null,
     "flags": [], "role": "BACKGROUND", "one_line": "≤30字用处", "confidence": 0.8}
  ]
}
```
示例 id（900001）仅供格式参考，不得出现在你的输出中。

# 示例（精简版，完整版见 config/prompts/whitelist_filter_prompt.md）
- 纯填补 RMSE 增量、无下游/机制分析 → EXCLUDE，exclude_reason="X2"。
- 缺失掩码嵌入模型且掩码本身带预后信号 → INCLUDE，bucket=["B"]。
- 摘要未披露缺失处理方式、证据不足 → MAYBE，confidence≤0.4。
- 实证结论与本文相反（如推翻 attention=因果的主张）→ INCLUDE，flags=["THREAT"]，role="MUST_ENGAGE"。

## 待裁决论文
```json
{{PAPERS_JSON}}
```

id 必须覆盖每一条输入记录。
"""


class ScholarWorkflow:
    """
    Scholar Digest 工作流
    
    完整流程：
    1. 连接 Gmail API（OAuth 认证）
    2. 获取 Google Scholar 邮件
    3. 解析邮件提取论文信息
    4. 批量发送给 LLM 进行摘要和翻译
    5. 保存结果为 JSON 文件
    6. （可选）标记邮件为已读
    """
    
    def __init__(self, settings: ScholarSettings):
        """
        初始化工作流
        
        Args:
            settings: ScholarSettings 配置对象
        """
        self.settings = settings
        
        # 初始化组件
        self.gmail_client = GmailClient(settings)
        self.parser = ScholarEmailParser()
        
        # LLM 客户端（延迟初始化）
        self._llm_client = None
        
        # 工作数据
        self.emails: List[Dict[str, Any]] = []
        self.segments: PaperSegmentList = []
        self.processed_emails: List[EmailMetadata] = []

        # 过滤审计数据：被排除论文（含完整元数据+裁决）与入选论文的裁决记录
        self.excluded: List[Dict[str, Any]] = []
        self.included_decisions: List[FilterDecision] = []
        # LLM 裁决失败回退时白名单未命中的论文：不进 segments 也不算 excluded，
        # 单独随 digest 输出供人工复核（关键词白名单远比三态审稿严格，未命中≠无关）
        self.undecided_segments: List[PaperSegment] = []
        self._filter_prompt_template: Optional[str] = None
        self._filter_prompt_version: Optional[str] = None
        self._filter_fallback_count = 0
        # 抢救计数：两类抢救损坏都不进 processed/failed/fallback 任何一个计数器，
        # 不单列就等于「下次发生时连靠 failed_papers 反推都做不到」。
        self._salvage_batches = 0     # 触发过前缀抢救的批次数
        self._salvage_dropped = 0     # 抢救后因字段残缺被丢弃/降级的条目数

        # RAG phase 3：札记库语义近邻（懒加载缓存，同一次 run 内 filter 与落盘复用）。
        # segment_id -> [{citekey, year, one_line, sim}, ...]；向量库/Ollama 不可用时值为 []。
        self._library_neighbors_cache: Dict[int, List[dict]] = {}
        self._vector_store = None  # 懒加载的 embed_store.VectorStore，失败后不重试
        self._paper_mask = None  # 随 _vector_store 一次算好的 paper 级布尔掩码（万级 records 别每批重建）
        self._embedding_client = None  # 懒加载的 embeddings.EmbeddingClient
        self._library_neighbors_unavailable = False  # 一次失败后本次 run 内不再重试，避免刷屏

        # 跨来源去重（邮件路径与外部检索共用，保证 Gmail/PubMed/arXiv 不重复入库）
        self._seen_paper_ids: set = set()
        self._seen_dois: set = set()

        # 显式日期区间 [start,end]（按月回填时由 CLI 设置；None=用相对 days_to_fetch）
        self.date_range: Optional[Tuple[date, date]] = None

        # 幂等标记：过滤裁决加成（THREAT/MUST_ENGAGE 等）是否已经累加过一次。
        # _sort_by_priority 会把加成原地写入 seg.priority_score 并随 digest JSON 落盘，
        # 若未来有代码把落盘数据重新读回再排序，没有这个标记会导致加成被重复叠加。
        self._priority_bonus_applied: bool = False
        
        # 输出
        self.output_dir = settings.processing.output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # 生成本次运行的唯一 ID
        self.run_id = self._generate_run_id()
        
        logger.info("ScholarWorkflow 初始化完成")
        logger.info("   运行ID: {}".format(self.run_id))
        logger.info("   输出目录: {}".format(self.output_dir))
    
    def _generate_run_id(self) -> str:
        """生成运行ID"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        return f"digest_{timestamp}"
    
    @property
    def llm_client(self):
        """懒加载 LLM 客户端"""
        if self._llm_client is None:
            self._llm_client = self._create_llm_client()
        return self._llm_client
    
    def _create_llm_client(self):
        """创建 LLM 客户端（委托独立 LLMClient helper）"""
        from .llm_client import LLMClient
        return LLMClient(self.settings.llm)
    
    def execute(self) -> DigestOutput:
        """
        执行完整的工作流
        
        Returns:
            DigestOutput 包含完整的处理结果
        """
        logger.info("=" * 60)
        logger.info("📬 Scholar Digest 工作流开始")
        logger.info("=" * 60)
        
        try:
            # Step 1: 认证并获取邮件
            self._step_fetch_emails()
            
            # Step 2: 解析邮件提取论文（黑名单确定性预过滤 + 去重）
            self._step_parse_emails()

            # Step 2.2: 外部检索来源（PubMed/arXiv，并入 digest；跨源去重）
            self._step_fetch_external_sources()

            # Step 2.5: 白名单过滤（LLM 相关性裁决或关键词匹配）
            self._step_filter_papers()

            # Step 3: 批量处理（翻译和摘要）
            if self.settings.processing.translate_abstracts or self.settings.processing.generate_summary:
                self._step_process_papers()
            else:
                # 两开关皆关（月度回填 backfill_notes 即此路径）也必须算规则优先级：
                # 否则 priority_score 恒为默认 0.0，THREAT/MUST_ENGAGE 置顶加成从未应用，
                # 下游 close_read_segments 的 top-N 精读与 write_notes 分层会退化为邮件顺序。
                # 该方法内部会调 _sort_by_priority（带 _priority_bonus_applied 幂等标记），
                # 加成只生效一次；LLM 路径（上面 if 分支）不经过这里，行为不变。
                self._calculate_rule_based_priority()

            # Step 4: 生成输出
            output = self._step_generate_output()

            # Step 4.5: Zotero 联动（写库 + citekey + pandoc 札记；默认关闭，需 Zotero 在线）
            if self.settings.processing.zotero_enabled:
                self._step_sync_zotero()

            # Step 5: 标记邮件为已读
            if self.settings.processing.auto_mark_read:
                self._step_mark_emails_read()
            
            logger.info("=" * 60)
            logger.info("🎉 Scholar Digest 工作流完成!")
            logger.info("   处理邮件: {}".format(len(self.processed_emails)))
            logger.info("   提取论文: {}".format(len(self.segments)))
            logger.info("=" * 60)
            
            return output
            
        except Exception as e:
            logger.error("工作流执行失败: {}".format(e))
            raise
        finally:
            self.close_rag_resources()

    def _step_fetch_emails(self):
        """Step 1: 获取 Google Scholar 邮件"""
        logger.info("\nStep 1: 获取 Google Scholar 邮件")
        logger.info("-" * 40)
        
        # 获取用户信息（同时触发认证）
        profile = self.gmail_client.get_user_profile()
        logger.info("已登录: {}".format(profile.get('emailAddress')))
        
        # 获取邮件（显式区间优先于相对 days；Gmail before: 为排他，末日+1 才含当月末日）
        after = before = None
        if self.date_range:
            s, e = self.date_range
            after = s.strftime('%Y/%m/%d')
            before = (e + timedelta(days=1)).strftime('%Y/%m/%d')
        self.emails = self.gmail_client.fetch_scholar_emails(
            days=self.settings.processing.days_to_fetch,
            max_results=self.settings.processing.max_emails,
            unread_only=False,
            after=after,
            before=before,
        )
        
        logger.info("获取到 {} 封 Scholar 邮件".format(len(self.emails)))
    
    def _step_parse_emails(self):
        """Step 2: 解析邮件提取论文（黑名单确定性预过滤 + DOI/ID 去重）"""
        logger.info("\nStep 2: 解析邮件提取论文")
        logger.info("-" * 40)

        self.parser.reset_counter()
        seen_paper_ids = self._seen_paper_ids
        seen_dois = self._seen_dois

        blacklist = self.settings.processing.blacklist

        for email_data in self.emails:
            metadata = email_data['metadata']
            body = email_data['body']

            logger.info("  处理邮件: {}".format(metadata.subject[:50]))

            # 解析邮件
            papers = self.parser.parse_email(body, metadata)

            extracted_count = 0
            # 预过滤与去重（白名单/LLM 裁决在 Step 2.5 进行）
            for paper in papers:
                # 1. 黑名单确定性预过滤（零成本，剔除明显无关）
                hit = self._match_keyword(paper, blacklist)
                if hit:
                    logger.debug("  跳过论文 (黑名单: {}): {}".format(hit, paper.metadata.title[:50]))
                    self._record_exclusion(paper, FilterDecision(
                        paper_id=paper.paper_id,
                        title=paper.metadata.title,
                        verdict="excluded",
                        stage="blacklist_keyword",
                        reason="黑名单命中: {}".format(hit),
                    ))
                    continue

                # 2. DOI 去重 (如果存在 DOI)
                if paper.metadata.doi:
                    norm_doi = paper.metadata.doi.lower().strip()
                    if norm_doi in seen_dois:
                        logger.debug("  跳过论文 (DOI 重复): {}".format(paper.metadata.title[:50]))
                        continue
                    seen_dois.add(norm_doi)

                # 3. Paper ID 去重 (兜底方案)
                if paper.paper_id not in seen_paper_ids:
                    seen_paper_ids.add(paper.paper_id)
                    self.segments.append(paper)
                    extracted_count += 1

            # 更新邮件元数据
            metadata.papers_extracted = extracted_count
            metadata.is_processed = True
            self.processed_emails.append(metadata)

        logger.info("共提取 {} 篇唯一论文（经过黑名单预过滤与 DOI/ID 去重，待白名单裁决）".format(len(self.segments)))

    def _step_fetch_external_sources(self):
        """Step 2.2: 从 PubMed/arXiv 按检索式抓取论文，跨源去重后并入 self.segments。

        与 Gmail 路径共用黑名单预过滤与 DOI/paper_id 去重；检索失败只 warning 跳过，不中断 digest。
        """
        proc = self.settings.processing
        if not getattr(proc, 'external_sources_enabled', False):
            return

        logger.info("\nStep 2.2: 外部检索来源 (PubMed/arXiv)")
        logger.info("-" * 40)

        # 从已有 segment 之后连续分配 segment_id，保证全局唯一（_filter_by_llm 用它做映射键）
        start_id = (max((s.segment_id for s in self.segments), default=-1)) + 1

        try:
            # 延迟导入放进 try：httpx 缺失（ModuleNotFoundError）也只跳过、不中断 digest
            from .academic_search import fetch_external_papers
            candidates = fetch_external_papers(
                arxiv_query=getattr(proc, 'arxiv_query', ''),
                pubmed_query=getattr(proc, 'pubmed_query', ''),
                max_results=getattr(proc, 'external_max_results', 25),
                days=proc.days_to_fetch or None,
                start_segment_id=start_id,
                email=getattr(proc, 'external_email', ''),
                date_range=self.date_range,
            )
        except Exception as e:
            logger.warning("  ⚠️ 外部检索整体失败（跳过）: {}".format(e))
            return

        blacklist = proc.blacklist
        added = 0
        for paper in candidates:
            # 黑名单预过滤（与邮件路径一致）
            hit = self._match_keyword(paper, blacklist)
            if hit:
                self._record_exclusion(paper, FilterDecision(
                    paper_id=paper.paper_id,
                    title=paper.metadata.title,
                    verdict="excluded",
                    decision="EXCLUDE",
                    stage="blacklist_keyword",
                    reason="黑名单命中: {}".format(hit),
                ))
                continue

            # 跨源 DOI 去重
            if paper.metadata.doi:
                norm_doi = paper.metadata.doi.lower().strip()
                if norm_doi in self._seen_dois:
                    continue
                self._seen_dois.add(norm_doi)

            # paper_id 去重
            if paper.paper_id in self._seen_paper_ids:
                continue
            self._seen_paper_ids.add(paper.paper_id)
            self.segments.append(paper)
            added += 1

        logger.info("  外部来源新增 {} 篇（去重后），当前累计 {} 篇待裁决".format(added, len(self.segments)))

    def _match_keyword(self, paper: PaperSegment, keywords: List[str]) -> Optional[str]:
        """在论文标题+摘要中匹配关键词列表，返回命中的第一个关键词，无命中返回 None。

        ASCII 关键词按整词匹配（避免 'Gene' 命中 'general'、'Vision' 命中
        'supervision'），并容忍复数形式（LLM->LLMs、Network->Networks）；
        中文关键词按包含匹配。空关键词不命中。"""
        content = (paper.metadata.title + " " + (paper.original_abstract or "")).lower()

        for word in keywords:
            w = word.lower().strip()
            if not w:
                continue
            if re.fullmatch(r'[a-z0-9 \-]+', w):
                if re.search(r'\b{}(?:e?s)?\b'.format(re.escape(w)), content):
                    return word
            elif w in content:
                return word
        return None

    # ==================== Step 2.5: 白名单过滤 ====================

    def _step_filter_papers(self):
        """Step 2.5: 白名单过滤（LLM 相关性裁决或关键词匹配）

        黑名单已在 Step 2 确定性剔除；这里对剩余论文做入选判断。
        每篇论文的裁决（判定+理由+模型+prompt 版本）都会记录，
        被排除论文连同元数据固化到 {run_id}_excluded.json。
        """
        mode = self.settings.processing.filter_mode
        logger.info("\nStep 2.5: 白名单过滤 (模式: {})".format(mode))
        logger.info("-" * 40)

        if not self.segments:
            logger.info("无待裁决论文")
            return

        candidates = self.segments
        before = len(candidates)

        if mode == "llm":
            kept = self._filter_by_llm(candidates)
        else:
            kept = self._filter_by_whitelist(candidates, stage="whitelist_keyword")

        self.segments = kept

        # 重算每封邮件的最终提取数（parse 阶段记的是裁决前的数量）
        counts: Dict[str, int] = {}
        for seg in self.segments:
            eid = seg.metadata.source_email_id
            if eid:
                counts[eid] = counts.get(eid, 0) + 1
        for email_meta in self.processed_emails:
            email_meta.papers_extracted = counts.get(email_meta.email_id, 0)

        logger.info("白名单裁决完成: {} -> {} 篇（排除 {} 篇，累计排除 {} 篇）".format(
            before, len(self.segments), before - len(self.segments), len(self.excluded)
        ))

    def _filter_by_whitelist(self, papers: List[PaperSegment], stage: str) -> List[PaperSegment]:
        """关键词白名单过滤，为每篇论文产出裁决记录。

        注意：keyword 模式是纯本地离线兜底路径（LLM 挂了才走它），刻意不注入 RAG 近邻——
        `_library_neighbors` 的消费方是 LLM 裁决 prompt，keyword 模式没有消费方，只会引入
        Ollama 依赖与延迟，违背离线兜底定位。
        """
        whitelist = self.settings.processing.whitelist
        kept = []
        for paper in papers:
            hit = self._match_keyword(paper, whitelist) if whitelist else "（白名单为空，默认入选）"
            if hit:
                decision = FilterDecision(
                    paper_id=paper.paper_id,
                    title=paper.metadata.title,
                    verdict="included",
                    decision="INCLUDE",
                    stage=stage,
                    reason="白名单命中: {}".format(hit),
                    one_line="白名单命中: {}".format(hit),
                )
                paper.filter_decision = decision
                self.included_decisions.append(decision)
                kept.append(paper)
            else:
                logger.debug("  跳过论文 (未命中白名单): {}".format(paper.metadata.title[:50]))
                excl = FilterDecision(
                    paper_id=paper.paper_id,
                    title=paper.metadata.title,
                    verdict="excluded",
                    decision="EXCLUDE",
                    stage=stage,
                    reason="未命中白名单关键词",
                )
                paper.filter_decision = excl
                self._record_exclusion(paper, excl)
        return kept

    def _fallback_partition(self, papers: List[PaperSegment]) -> List[PaperSegment]:
        """LLM 裁决失败时的回退分流：白名单命中者照常入选；未命中者挂 undecided 待人工复核。

        不能沿用 _filter_by_whitelist 的 EXCLUDE 语义——审稿原则是「宁可 MAYBE 不可 EXCLUDE」，
        而关键词只认 ~40 个词面，失败批的未命中远不足以判无关；此前直接 EXCLUDE 会让论文
        静默消失且运行仍算成功。未命中者仍写 excluded sidecar 供审计
        （周度入库的 DIGEST_RE 不读该文件，不会把未决论文误入库）。"""
        whitelist = self.settings.processing.whitelist
        kept: List[PaperSegment] = []
        for paper in papers:
            hit = self._match_keyword(paper, whitelist) if whitelist else "（白名单为空，默认入选）"
            if hit:
                decision = FilterDecision(
                    paper_id=paper.paper_id,
                    title=paper.metadata.title,
                    verdict="included",
                    decision="INCLUDE",
                    stage="keyword_fallback",
                    reason="白名单命中: {}".format(hit),
                    one_line="白名单命中: {}".format(hit),
                    library_neighbors=self._library_neighbors_cache.get(paper.segment_id) or [],
                )
                paper.filter_decision = decision
                self.included_decisions.append(decision)
                kept.append(paper)
            else:
                decision = FilterDecision(
                    paper_id=paper.paper_id,
                    title=paper.metadata.title,
                    verdict="undecided",
                    stage="keyword_fallback",
                    reason="LLM 裁决失败且未命中白名单，待人工复核",
                    library_neighbors=self._library_neighbors_cache.get(paper.segment_id) or [],
                )
                paper.filter_decision = decision
                self.undecided_segments.append(paper)
                self._record_exclusion(paper, decision)
        return kept

    def _filter_by_llm(self, papers: List[PaperSegment]) -> List[PaperSegment]:
        """LLM 相关性裁决：分批送审；单批失败回退关键词白名单（不中断整体流程）"""
        model = self.settings.llm.filter_model or self.settings.llm.model
        prompt_version = self._get_filter_prompt_version()
        logger.info("  裁决模型: {} | prompt: {}".format(model, prompt_version))

        kept: List[PaperSegment] = []
        total_batches = (len(papers) + FILTER_BATCH_SIZE - 1) // FILTER_BATCH_SIZE

        for i in range(0, len(papers), FILTER_BATCH_SIZE):
            batch = papers[i:i + FILTER_BATCH_SIZE]
            batch_num = i // FILTER_BATCH_SIZE + 1
            logger.info("  裁决批次 {}/{} ({} 篇)".format(batch_num, total_batches, len(batch)))

            try:
                prompt = self._build_filter_prompt(batch)
                response = self._call_llm(prompt, model=model, json_mode=True)
                batch_ids = {p.segment_id for p in batch}
                verdicts = self._parse_filter_response(response, valid_ids=batch_ids)
            except Exception as e:
                logger.warning("  批次 {} LLM 裁决失败，回退关键词白名单: {}".format(batch_num, e))
                self._filter_fallback_count += len(batch)
                kept.extend(self._fallback_partition(batch))
                continue

            for paper in batch:
                verdict = verdicts.get(paper.segment_id)
                if verdict is None:
                    # LLM 响应缺失该 id：单篇回退关键词判断
                    logger.warning("  [MISS] 裁决结果缺失 id={}，回退关键词".format(paper.segment_id))
                    self._filter_fallback_count += 1
                    kept.extend(self._fallback_partition([paper]))
                    continue

                # 单篇裁决构造独立容错：flash 级模型常把 confidence 返回成 "high"、
                # bucket/flags 返回非 list 等，绝不能因一篇脏字段让整批 digest 崩溃。
                try:
                    decision = self._build_filter_decision(paper, verdict, model, prompt_version)
                except Exception as e:
                    logger.warning("  裁决字段异常 id={}，回退关键词: {}".format(paper.segment_id, e))
                    self._filter_fallback_count += 1
                    kept.extend(self._fallback_partition([paper]))
                    continue

                # 裁决随论文进入输出（分节/排序用）
                paper.filter_decision = decision
                if decision.verdict == "included":
                    self.included_decisions.append(decision)
                    kept.append(paper)
                else:
                    logger.debug("  跳过论文 (LLM 裁决): {}".format(paper.metadata.title[:50]))
                    self._record_exclusion(paper, decision)

        return kept

    @staticmethod
    def _coerce_str_list(value) -> List[str]:
        """把 LLM 返回的 bucket/flags 容错成 List[str]：list→逐项 str；标量→单元素；None→空。"""
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value else []
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value if v is not None]
        return [str(value)]

    @staticmethod
    def _coerce_confidence(value):
        """把 confidence 容错成 [0,1] 的 float；无法解析或越界时返回 None（不入校验）。"""
        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if f != f:  # NaN
            return None
        return max(0.0, min(1.0, f))

    def _build_filter_decision(self, paper, verdict, model, prompt_version) -> FilterDecision:
        """由 LLM 单篇裁决 dict 构造 FilterDecision（字段全部容错，不因脏输入抛异常）。"""
        # 三态裁决：INCLUDE/MAYBE 入选（MAYBE 一并翻译，输出中用 decision/role 区分），EXCLUDE 排除
        decision_val = str(verdict.get('decision', 'EXCLUDE')).upper()
        if decision_val not in ("INCLUDE", "MAYBE", "EXCLUDE"):
            decision_val = "MAYBE"  # 无法识别时保守保留（宁可 MAYBE 不可 EXCLUDE）
        included = decision_val in ("INCLUDE", "MAYBE")

        exclude_reason = verdict.get('exclude_reason')
        role = verdict.get('role')
        one_line = verdict.get('one_line', '')
        reason = one_line or verdict.get('reason', '')

        return FilterDecision(
            paper_id=paper.paper_id,
            title=paper.metadata.title,
            verdict="included" if included else "excluded",
            decision=decision_val,
            stage="llm_judge",
            reason=str(reason) if reason is not None else "",
            bucket=self._coerce_str_list(verdict.get('bucket')),
            exclude_reason=str(exclude_reason) if exclude_reason not in (None, "") else None,
            flags=self._coerce_str_list(verdict.get('flags')),
            role=str(role) if role not in (None, "") else None,
            one_line=str(one_line) if one_line is not None else "",
            confidence=self._coerce_confidence(verdict.get('confidence')),
            model=model,
            prompt_version=prompt_version,
            library_neighbors=self._library_neighbors_cache.get(paper.segment_id) or [],
        )

    def _record_exclusion(self, paper: PaperSegment, decision: FilterDecision):
        """将被排除的论文连同完整元数据与裁决记录固化（写入 excluded sidecar）"""
        self.excluded.append({
            'paper_id': paper.paper_id,
            'reason': decision.reason,
            'stage': decision.stage,
            'source_email_id': paper.metadata.source_email_id,
            'decision': decision.model_dump(mode='json'),
            'metadata': paper.metadata.model_dump(mode='json'),
            'original_abstract': paper.original_abstract,
        })

    def _get_filter_prompt_template(self) -> str:
        """加载裁决 prompt 模板（优先文件，缺失时用内嵌兜底）"""
        if self._filter_prompt_template is None:
            if FILTER_PROMPT_FILE.exists():
                self._filter_prompt_template = FILTER_PROMPT_FILE.read_text(encoding='utf-8')
            else:
                logger.warning("裁决 prompt 模板缺失 ({})，使用内嵌兜底模板".format(FILTER_PROMPT_FILE))
                self._filter_prompt_template = FILTER_PROMPT_FALLBACK
        return self._filter_prompt_template

    def _get_filter_prompt_version(self) -> str:
        """裁决 prompt 版本标识：模板声明的版本标签 + (模板内容 + 纳入/排除维度 + 研究方向) md5 前 8 位。

        A-G/X1-X7 与 research_interests 已从模板正文移出，改成 {{INCLUSION_DIMS}}/
        {{EXCLUSION_DIMS}}/{{RESEARCH_INTERESTS}} 占位符，单独对模板取 md5 会导致换研究方向
        （只改 config/research_profile.yaml）后指纹不变——审计记录里区分不出换方向前后的裁决。
        故把三者一并拼进 md5 输入，指纹随 profile 变化。
        """
        if self._filter_prompt_version is None:
            template = self._get_filter_prompt_template()
            m = re.search(r'<!--\s*version:\s*(.+?)\s*-->', template)
            label = m.group(1) if m else "unversioned"
            fingerprint_input = "\x00".join([
                template,
                get_inclusion_dims(),
                get_exclusion_dims(),
                self.settings.processing.research_interests,
            ])
            digest = hashlib.md5(fingerprint_input.encode('utf-8')).hexdigest()[:8]
            self._filter_prompt_version = "{}@{}".format(label, digest)
        return self._filter_prompt_version

    def _library_neighbors(self, papers: List[PaperSegment], k: int = 3,
                            min_sim: float = 0.65) -> Dict[int, List[dict]]:
        """查札记库（本地已收文献）语义近邻：segment_id -> [{citekey, year, one_line, sim}, ...]。

        查询文本口径与 _build_filter_prompt 送审文本一致：title + "\\n" + abstract[:800]（截断
        长度对齐现有裁决 prompt，避免两条截断口径打架）。一次性批量 embed 所有候选论文（不是
        逐篇请求 Ollama），查 paper 级向量（level=='paper' 掩码）取 top-k，sim < min_sim 丢弃。
        min_sim=0.65 是实测分界：bge-m3 短文本相似度分布窄，库内真实近邻落在 0.63-0.74，
        完全离题论文（含共享 ML 词汇者）最高 0.61——0.5 挡不住离题项，勿轻易调低。

        结果按 segment_id 缓存在 self._library_neighbors_cache，同一次 run 内 filter 阶段
        （_build_filter_prompt 注入）与裁决落盘（_build_filter_decision/_fallback_partition
        写入 FilterDecision.library_neighbors）复用，不重复查询同一篇论文。

        整个函数 try/except 包死：向量库缺失、Ollama 不可达、embedding 维度异常等任何失败都只
        log warning 并返回 {}（本次 run 内后续调用直接跳过不再重试）——RAG 近邻是审稿辅助信息，
        digest 主流程绝不因它不可用而中断或改变行为。
        """
        result: Dict[int, List[dict]] = {}
        to_query: List[PaperSegment] = []
        for paper in papers:
            cached = self._library_neighbors_cache.get(paper.segment_id)
            if cached is not None:
                result[paper.segment_id] = cached
            else:
                to_query.append(paper)
        if not to_query:
            return result

        if self._library_neighbors_unavailable:
            for paper in to_query:
                self._library_neighbors_cache[paper.segment_id] = []
                result[paper.segment_id] = []
            return result

        try:
            import numpy as np

            from .embed_store import (
                DB_NAME, INDEX_NAME, STALE_DEGRADE_SECONDS, VectorStore,
                model_matches, read_index_generated_at,
            )
            from .embeddings import EmbeddingClient, EmbeddingError, resolve_embedding_base_url

            if self._vector_store is None:
                # repo_path 自锚定：不依赖调用方（scholar_main 锚了，screen_journal 之类的新
                # 调用方忘了锚就会指向 <cwd>/output/... 并被下面的 except 吞成静默降级）
                notes_dir = repo_path(self.settings.processing.notes_dir)
                db_path = notes_dir / DB_NAME
                self._vector_store = VectorStore.load(db_path)
                want_model = getattr(self.settings.llm, "embedding_model", None) or "bge-m3"
                if not model_matches(self._vector_store.model, want_model):
                    raise EmbeddingError(
                        "向量库 embedding 模型 {} 与配置 {} 不一致，先跑 notes_embed.py --full 重建"
                        .format(self._vector_store.model, want_model))
                # 新鲜度：注入给 LLM 的 citekey/year 直接取自 chunk 元数据，库一旧就会把
                # 已注销的旧键与旧年份当成"库里已有这篇"喂进裁决。digest 是周一 09:00 无人
                # 值守跑的，notes_search 那种"打给人看的 stderr 提醒"在这里等于没有——
                # 必须自己告警，落后过久还要直接降级（宁可本轮没有近邻，也不用错身份）。
                index_generated_at = read_index_generated_at(notes_dir / INDEX_NAME)
                warn = self._vector_store.freshness_warning(index_generated_at)
                if warn:
                    logger.warning("  {}".format(warn))
                    lag = self._vector_store.freshness_lag_seconds(index_generated_at)
                    # lag is None = 已判定旧但算不出幅度（时间戳格式异常），从严当超阈值处理
                    if lag is None or lag > STALE_DEGRADE_SECONDS:
                        raise EmbeddingError(
                            "向量库快照落后当前索引 {}，超过 {} 小时阈值——本次拒绝用它做近邻"
                            "（陈旧库会把已改名/已删除的 citekey 与旧年份注入裁决）。"
                            "先跑 PYTHONPATH=. python scripts/notes_embed.py 增量同步".format(
                                "{:.1f} 小时".format(lag / 3600.0) if lag else "（幅度未知）",
                                STALE_DEGRADE_SECONDS // 3600))
                self._paper_mask = np.array(
                    [rec.get("level") == "paper" for rec in self._vector_store.records], dtype=bool)
            store = self._vector_store
            mask = self._paper_mask

            if self._embedding_client is None:
                base_url = resolve_embedding_base_url(self.settings.llm)
                model = getattr(self.settings.llm, "embedding_model", None) or "bge-m3"
                self._embedding_client = EmbeddingClient(base_url=base_url, model=model)
            client = self._embedding_client
            # probe 本地毫秒级（GET /api/tags），Ollama 起着但挂起时快速失败而非等 120s 超时×3
            client.probe()

            texts = [
                "{}\n{}".format(p.metadata.title or "", (p.original_abstract or "")[:800])
                for p in to_query
            ]
            vecs = client.embed(texts)

            n_self = 0
            for paper, vec in zip(to_query, vecs):
                hits = store.search(vec, mask=mask, top_k=k)
                neighbors = []
                self_key = _norm_title(paper.metadata.title)
                for idx, sim in hits:
                    if sim < min_sim:
                        continue
                    rec = store.records[idx]
                    # paper chunk 文本 = title + "\n" + one_line（one_line 为空则只有 title）
                    text_parts = (rec.get("text") or "").split("\n", 1)
                    one_line = text_parts[1] if len(text_parts) > 1 else ""
                    # 自命中：Google Scholar 邮件臂每周会重复推送已入库论文，而 seen 去重发生在
                    # filter 之后，所以这里必然会检索到论文自己。**保留这条近邻**——prompt 要靠它
                    # 判「与已收文献重复 → MAYBE」，journal_screen 的 in_library 列也只读 sim；
                    # 但必须掐掉 one_line：那是当初人工写的裁决判词，喂回给裁决者等于自我背书
                    # （实测 medina2026Aligning 的新判词几乎是自己 one_line 的复述）。
                    # 用标题而非 DOI 比对：Scholar 臂的 doi/arxiv_id 均为 None，且 paper chunk
                    # 文本首行就是标题，无需额外查询。
                    if self_key and _norm_title(text_parts[0]) == self_key:
                        one_line = "（本篇自身的在库记录，仅可用于判定重复，不是独立佐证）"
                        n_self += 1
                    neighbors.append({
                        "citekey": rec.get("citekey"),
                        "year": rec.get("year"),
                        "one_line": one_line,
                        "sim": round(float(sim), 3),
                    })
                self._library_neighbors_cache[paper.segment_id] = neighbors
                result[paper.segment_id] = neighbors
            if n_self:
                logger.info("  ↻ 近邻自命中 {} 条（已入库论文被重复推送），其 one_line 不作为佐证注入", n_self)
        except Exception as e:
            logger.warning(
                "  ⚠️ 札记库语义近邻检索不可用（本次 run 降级为无 RAG 近邻，不影响裁决主流程）: {}".format(e))
            self._library_neighbors_unavailable = True
            for paper in to_query:
                self._library_neighbors_cache[paper.segment_id] = []
                result[paper.segment_id] = []

        return result

    def close_rag_resources(self):
        """释放近邻检索占用的资源（httpx 连接、内存向量矩阵）。幂等；execute() 收尾
        与 classify_segments 的临时 workflow 都会调，忘调也只是等进程退出兜底。"""
        if self._embedding_client is not None:
            try:
                self._embedding_client.close()
            except Exception:
                pass
            self._embedding_client = None
        self._vector_store = None
        self._paper_mask = None

    def _build_filter_prompt(self, batch: List[PaperSegment]) -> str:
        """构建批量裁决 prompt"""
        neighbors_map = self._library_neighbors(batch)
        papers_data = []
        for seg in batch:
            item = {
                "id": seg.segment_id,
                "title": seg.metadata.title,
                "abstract": (seg.original_abstract or "")[:800],
                "journal": seg.metadata.journal,
            }
            neighbors = neighbors_map.get(seg.segment_id) or []
            if neighbors:
                item["library_neighbors"] = neighbors
            papers_data.append(item)

        template = self._get_filter_prompt_template()
        prompt = template.replace("{{RESEARCH_INTERESTS}}", self.settings.processing.research_interests)
        # 纳入/排除维度单点化：内容来自 config/research_profile.yaml，换研究方向改那一处即可
        prompt = prompt.replace("{{INCLUSION_DIMS}}", get_inclusion_dims())
        prompt = prompt.replace("{{EXCLUSION_DIMS}}", get_exclusion_dims())
        prompt = prompt.replace("{{PAPERS_JSON}}", json.dumps(papers_data, ensure_ascii=False, indent=2))
        return prompt

    @staticmethod
    def _verdict_is_complete(item: Dict[str, Any]) -> bool:
        """抢救出的裁决是否字段完整。

        `whitelist_filter_prompt.md` 的输出契约里字段顺序是
        id → decision → bucket → exclude_reason → flags → role → one_line → confidence，
        抢救总在畸形点前停，**最先被削掉的就是末尾的 one_line 与 confidence**。
        只查 id+decision 会放行这种残缺条目：它 stage 仍是 llm_judge、不计入
        filter_fallback_count，与正常裁决无法区分；而空 one_line 会一路流到
        embed_store（`paper_text = title + "\n" + one_line if one_line else title`），
        让该论文的库向量退化成纯标题，此后每轮近邻召回长期失真且无自动重建。
        故抢救模式下要求 one_line 与 confidence 至少有一个在。
        """
        return bool((item.get("one_line") or "").strip()) or item.get("confidence") is not None

    def _parse_filter_response(self, response: str,
                                valid_ids: Optional[Set[int]] = None) -> Dict[int, Dict[str, Any]]:
        """解析方法学审稿裁决响应为 {id: item} 映射（item 含 decision/bucket/flags/role/one_line/confidence）；
        解析失败抛异常由调用方回退。

        valid_ids: 当前批次的合法 id 集合。prompt 里已用软约束要求模型不得输出示例 id 或幻觉 id，
        但 flash 级模型仍会回显示例小节的 id（900001-900004 等）或编造不存在的 id——这里把软约束
        变成代码强制：不在集合内的 id 直接丢弃该条，不让脏 id 污染 verdict 映射（调用方按 segment_id
        查不到时会走单篇回退，行为等价于"这条没被裁决"，安全）。留空（None/未传）时不做过滤，
        兼容测试与其它未传批次上下文的调用方。"""
        salvaged_mode = False
        json_text = response
        if '```json' in response:
            json_text = response.split('```json')[1].split('```')[0]
        elif '```' in response:
            json_text = response.split('```')[1].split('```')[0]

        try:
            verdicts = json.loads(json_text.strip())
        except json.JSONDecodeError as je:
            # 一次截断的响应曾让整批 20 篇一起降级成关键词裁决（08-17 那轮 40 篇 =
            # 2×20，正是两个整批）。抢救畸形点之前的前缀：
            # 救回来的走 LLM 裁决，没救回的由调用方 `verdict is None` 分支逐篇回退，
            # 严格优于整批降级。抢救不出任何东西时仍抛，走原有的批级回退。
            salvaged = loads_lenient(json_text)
            if salvaged is None:
                raise
            logger.warning("  ⚠️ 裁决 JSON 畸形，已抢救前缀（本批部分条目将走单篇回退）: {}".format(je))
            verdicts = salvaged
            salvaged_mode = True
            self._salvage_batches += 1
        if isinstance(verdicts, dict):
            # verdicts: filter-v3 契约（DeepSeek json_object 要求顶层对象）；
            # papers: 旧顶层数组格式的历史包装，宽容兼容
            if 'verdicts' in verdicts:
                verdicts = verdicts['verdicts']
            elif 'papers' in verdicts:
                verdicts = verdicts['papers']
        if not isinstance(verdicts, list):
            raise ValueError("裁决响应不是 JSON 数组，也非 {verdicts:[...]} 对象")

        result = {}
        for item in verdicts:
            if not (isinstance(item, dict) and 'id' in item and 'decision' in item):
                continue
            # 单条 id 非数字（如模型吐出字符串 "N/A"）只丢弃这一条，不能让 int() 抛异常拖垮整批
            try:
                item_id = int(item['id'])
            except (TypeError, ValueError):
                logger.warning("  裁决响应含非数字 id，丢弃该条: {!r}".format(item.get('id')))
                continue
            if valid_ids is not None and item_id not in valid_ids:
                logger.warning("  裁决响应 id={} 不在本批合法 id 集合内，丢弃该条（示例回显/幻觉 id）".format(item_id))
                continue
            if salvaged_mode and not self._verdict_is_complete(item):
                # 抢救模式专属：丢弃残缺条目 → 调用方 `verdict is None` 分支逐篇回退关键词，
                # 计入 filter_fallback_count、可观测；而放行它则是一条伪装成正常 llm_judge
                # 的残缺裁决，任何计数器都抓不到。
                logger.warning("  抢救出的裁决 id={} 字段残缺（缺 one_line/confidence），丢弃改走单篇回退".format(item_id))
                self._salvage_dropped += 1
                continue
            result[item_id] = item
        return result
    
    def _step_process_papers(self):
        """Step 3: 批量处理论文（翻译、关键词提取、优先级评分）"""
        logger.info("\nStep 3: LLM 处理论文")
        logger.info("-" * 40)
        
        # 如果不需要翻译和总结，跳过 LLM 调用
        if not self.settings.processing.translate_abstracts and not self.settings.processing.generate_summary:
            logger.info("跳过 LLM 处理（翻译和总结均已禁用）")
            # 仍然进行基于规则的优先级预排序
            self._calculate_rule_based_priority()
            return
        
        batch_size = self.settings.processing.batch_size
        total = len(self.segments)
        total_batches = (total + batch_size - 1) // batch_size

        # 分批处理；逐批 catch 只为了让个别批次的偶发故障（单批 JSON 解析失败等）
        # 不拖垮整轮，但必须统计成功批次数——LLM 回退链耗尽（欠费/限流/CLI 缺失
        # 全部命中）时每一批都会同样失败，逐批 catch+continue 会把「通路级故障」
        # 悄悄吞成「处理完成 0/N」，随后 Step 4 照常产出 status=COMPLETED 的 digest、
        # 进程退出码 0。对齐 ingest.run_ingest 对全文精读 0/N 的同款守卫：全部批次
        # 失败时直接 raise，让 execute() 的外层 except 捕获、非零退出、不写终稿。
        failed_batches = 0
        for i in range(0, total, batch_size):
            batch_segments = self.segments[i:i + batch_size]
            batch_num = i // batch_size + 1

            logger.info("  处理批次 {}/{} ({} 篇)".format(batch_num, total_batches, len(batch_segments)))

            try:
                self._process_batch(batch_segments)
            except Exception as e:
                logger.error("  批次 {} 处理失败: {}".format(batch_num, e))
                failed_batches += 1
                # 继续处理下一批
                continue

        if total_batches > 0 and failed_batches == total_batches:
            raise RuntimeError(
                "LLM 处理 {}/{} 批全部失败，视为 LLM 通路级故障（回退链耗尽/欠费/限流），"
                "不产出降级 digest".format(failed_batches, total_batches))

        # 按优先级排序
        self._sort_by_priority()

        # 统计
        processed = sum(1 for s in self.segments if s.is_processed)
        logger.info("处理完成: {}/{} 篇论文".format(processed, total))

    def _calculate_rule_based_priority(self):
        """基于规则计算论文优先级（不调用 LLM）"""
        logger.info("计算基于规则的论文优先级...")
        
        for seg in self.segments:
            meta = seg.metadata
            
            # 1. 来源类型评分
            source_score = self._get_source_score(meta.source_type, meta.journal)
            
            # 2. 领域评分（基于字段）
            field_score = self._get_field_score(meta.field)
            
            # 3. 时效性评分
            recency_score = self._get_recency_score(meta.publication_date)
            
            # 4. 论文类型评分
            type_score = self._get_type_score(meta.paper_type)
            
            # 5. 引用次数评分
            citation_score = self._get_citation_score(meta.citation_count)
            
            # 综合评分 (调整权重，加入引用次数)
            seg.priority_score = (
                0.25 * source_score + 
                0.25 * field_score + 
                0.15 * recency_score + 
                0.15 * type_score +
                0.20 * citation_score
            )
            seg.priority_reason = "src:{:.1f} fld:{:.1f} rec:{:.1f} typ:{:.1f} cite:{:.1f}".format(
                source_score, field_score, recency_score, type_score, citation_score
            )

        # 方法学审稿加成（THREAT/MUST_ENGAGE 置顶）统一在 _sort_by_priority 中应用，
        # 这样规则路径与 LLM 覆盖路径（_parse_llm_response 会覆盖 priority_score）
        # 都能在排序前恰好加成一次，避免此处提前加成后被 LLM 结果覆盖导致失效
        self._sort_by_priority()

    def _filter_priority_bonus(self, seg: PaperSegment) -> tuple:
        """根据过滤裁决计算优先级加成：THREAT/MUST_ENGAGE 大幅置顶，MAYBE 轻微降序。"""
        fd = seg.filter_decision
        if not fd:
            return 0.0, ""
        bonus = 0.0
        tags = []
        flags = fd.flags or []
        if "THREAT" in flags:
            bonus += 1.0
            tags.append("THREAT")
        if fd.role == "MUST_ENGAGE":
            bonus += 0.8
            tags.append("MUST_ENGAGE")
        if "BENCHMARK" in flags:
            bonus += 0.2
            tags.append("BENCHMARK")
        if "OVERCLAIM_PRECEDENT" in flags:
            bonus += 0.1
            tags.append("OVERCLAIM")
        if fd.decision == "MAYBE":
            bonus -= 0.1  # MAYBE 排在同类 INCLUDE 之后
            tags.append("MAYBE")
        return bonus, ("flags:" + ",".join(tags) if tags else "")
    
    def _get_citation_score(self, count: Optional[int]) -> float:
        """计算引用次数评分 (对数增长模型)"""
        if count is None or count <= 0:
            return 0.0
        
        import math
        # math.log10(10)=1.0, math.log10(100)=2.0, math.log10(1000)=3.0
        # 我们希望 1000 次引用达到 0.9 以上
        score = math.log10(count + 1) / 3.0
        return min(1.0, score)
    
    def _get_source_score(self, source_type: str, journal: str) -> float:
        """计算来源评分"""
        # 顶级期刊
        top_journals = ["lancet", "nejm", "nature", "science", "jama", "bmj"]
        # 专业期刊
        medical_ai_journals = ["jamia", "jbi", "npj digital medicine", "lancet digital health", 
                               "bmc medical informatics", "artificial intelligence in medicine"]
        
        journal_lower = (journal or "").lower()
        
        if any(j in journal_lower for j in top_journals):
            return 1.0
        elif any(j in journal_lower for j in medical_ai_journals):
            return 0.9
        elif source_type == "journal":
            return 0.7
        elif source_type == "conference":
            return 0.6
        elif source_type in ["arxiv", "medrxiv", "biorxiv"]:
            return 0.4
        else:
            return 0.3
    
    def _get_field_score(self, field: str) -> float:
        """计算领域评分"""
        field_lower = field.lower() if field else ""
        
        if any(kw in field_lower for kw in ["medicine", "medical", "clinical", "health"]):
            return 1.0
        elif any(kw in field_lower for kw in ["artificial intelligence", "machine learning", "deep learning"]):
            return 0.8
        elif any(kw in field_lower for kw in ["computer science", "engineering"]):
            return 0.6
        else:
            return 0.4
    
    def _get_recency_score(self, pub_date) -> float:
        """计算时效性评分。

        每年衰减 0.2，5 年以上归零（1.0 - 5*0.2 = 0.0），clamp 到 [0.0, 1.0]。
        当前年及未来论文 = 1.0，无法解析年份默认 0.3（中性分，既不当成最新也不归零）。
        """
        if not pub_date:
            return 0.3

        from datetime import date
        current_year = date.today().year

        if hasattr(pub_date, 'year'):
            year = pub_date.year
        else:
            return 0.3

        score = 1.0 - (current_year - year) * 0.2
        return max(0.0, min(1.0, score))
    
    def _get_type_score(self, paper_type: str) -> float:
        """计算论文类型评分"""
        type_lower = (paper_type or "").lower()
        
        if any(kw in type_lower for kw in ["review", "meta-analysis", "systematic"]):
            return 1.0
        elif any(kw in type_lower for kw in ["method", "framework", "novel"]):
            return 0.9
        elif "research" in type_lower:
            return 0.7
        else:
            return 0.5

    @staticmethod
    def _clamp_unit_score(value) -> float:
        """把 LLM 自报的 0-1 分数钳制回 [0,1]（None 兜 0，NaN 归 0）。

        json_mode 只保证合法 JSON、不校验数值范围，prompt 里的"(0-1)"是唯一软防线；
        模型偶发按 0-10/0-100 标度整批打分时，9.5 这类越界值会在 _sort_by_priority
        与精读 top-N 选篇里确定性霸榜（THREAT 加成 +1.0 也压不过），还会直通
        deep_research 的 min_priority=0.5 阈值。规则计算路径处处有 clamp
        （_coerce_confidence/_get_recency_score 等），唯独 LLM 覆盖路径此前缺失，
        在此补齐。脏类型（如非数字字符串）仍让 float() 抛异常，交由
        _parse_llm_response 的单篇 try 按既有语义打成 FAILED，不在这里吞。
        """
        f = float(value or 0)
        if f != f:  # NaN 不参与比较运算，max/min 钳不住，显式归 0
            return 0.0
        return max(0.0, min(1.0, f))

    def _sort_by_priority(self):
        """按优先级对论文排序（排序前统一应用一次过滤裁决加成）

        注：无论 priority_score 来自规则计算(_calculate_rule_based_priority)还是
        被 LLM 响应覆盖(_parse_llm_response)，本方法都是两条路径共同且唯一的排序入口，
        所以在这里统一加成可以保证 THREAT/MUST_ENGAGE 等加成在两条路径下都恰好生效一次。

        幂等性：加成是原地累加到 seg.priority_score 并落盘的（下游 notes.py 按此字段
        排序也需要看到加成后的分数，不能只在 sort key 里临时加而不写回）。因此用
        self._priority_bonus_applied 做实例级幂等标记——同一个 workflow 实例内，加成
        只会被应用一次；本方法被重复调用时（例如未来有代码把落盘数据重新读回再排序），
        之后的调用只做排序、不会重复叠加分数。
        """
        if not self._priority_bonus_applied:
            for seg in self.segments:
                bonus, bonus_reason = self._filter_priority_bonus(seg)
                if bonus:
                    seg.priority_score += bonus
                    seg.priority_reason = (seg.priority_reason or "") + " " + bonus_reason
            self._priority_bonus_applied = True

        self.segments.sort(key=lambda x: x.priority_score, reverse=True)
        logger.info("论文已按优先级排序（最高分: {:.2f}）".format(
            self.segments[0].priority_score if self.segments else 0
        ))
    
    def _process_batch(self, batch: List[PaperSegment]):
        """
        处理单个批次的论文
        
        Args:
            batch: 批次中的论文片段
        """
        if not batch:
            return
        
        # 构建 prompt
        prompt = self._build_batch_prompt(batch)

        # 调用 LLM（顶层对象输出，DeepSeek response_format=json_object 硬性要求）
        response = self._call_llm(prompt, json_mode=True)

        # 解析响应并更新段落（valid_ids 挡示例 id 回显/幻觉 id，与 filter 侧同款防护）
        batch_ids = {seg.segment_id for seg in batch}
        self._parse_llm_response(response, batch, valid_ids=batch_ids)
    
    def _build_batch_prompt(self, batch: List[PaperSegment]) -> str:
        """构建批量处理的 prompt（参考 scholar_digest_prompt.md）"""
        papers_data = []
        for seg in batch:
            # 获取邮件接收时间
            email_received = None
            if seg.metadata.email_received_at:
                email_received = seg.metadata.email_received_at.isoformat()
            
            papers_data.append({
                "id": seg.segment_id,
                "title": seg.metadata.title,
                "authors": ", ".join(seg.metadata.authors[:5]) if seg.metadata.authors else "Unknown",
                "abstract": seg.original_abstract[:1500] if seg.original_abstract else "No abstract available",
                "journal": seg.metadata.journal,
                "doi": seg.metadata.doi,
                "url": seg.metadata.url,
                "citation_count": seg.metadata.citation_count,
                "publication_date": str(seg.metadata.publication_date) if seg.metadata.publication_date else None,
                "email_received_at": email_received
            })
        
        prompt = """你是一位相关方法学领域的博士生导师，正在帮助学生筛选和分析论文。

## 研究背景
__RESEARCH_INTERESTS__

## 任务
对以下论文进行：
1. 标题和摘要翻译（首次出现的术语需标注英文原文）
2. 提取5个关键词
3. 评估与研究方向的相关度(0-1)
4. 计算综合优先级(0-1)

## 评分规则
- source_score: 顶级期刊(Lancet/NEJM/Nature)=1.0, 专业期刊(JAMIA/JBI)=0.9, 一般期刊=0.7, 会议=0.6, 预印本=0.4
- field_score: 与上述研究方向高度相关领域=1.0, 相邻方法学领域=0.9, 通用ML=0.6, 其他=0.3
- recency_score: 当前年份=1.0, 每早一年扣 0.2（如去年=0.8, 两年前=0.6, 五年以上=0.0）
- type_score: 综述/Meta分析=1.0, 方法论=0.9, 原创研究=0.7, 应用研究=0.5
- citation_score: 引用量 > 1000 = 1.0, > 100 = 0.7, > 10 = 0.4, 0 = 0.0
- priority_score = 0.25*source + 0.25*field + 0.15*recency + 0.15*type + 0.2*citation

## 输出格式（严格 JSON 对象，顶层不得是数组）
```json
{
  "papers": [
    {
      "id": 900001,
      "translated_title": "中文标题",
      "translated_abstract": "中文摘要（术语标注英文）",
      "keywords": ["关键词1", "关键词2", "关键词3", "关键词4", "关键词5"],
      "relevance_score": 0.85,
      "relevance_reason": "相关度评估理由",
      "source_type": "journal/conference/arxiv",
      "paper_type": "review/research/method",
      "priority_score": 0.82,
      "priority_breakdown": {"source_score": 0.9, "field_score": 0.8, "recency_score": 0.7, "type_score": 0.6}
    }
  ]
}
```
示例 id（900001）仅供格式参考，不得出现在你的输出中。

## 输入论文
```json
__PAPERS_JSON__
```

请严格按照JSON格式输出，ID必须与输入一一对应。"""
        # 不能用 str.format()：模板中的 JSON 示例花括号会被当成格式占位符（KeyError）
        prompt = prompt.replace("__RESEARCH_INTERESTS__", self.settings.processing.research_interests)
        prompt = prompt.replace("__PAPERS_JSON__", json.dumps(papers_data, ensure_ascii=False, indent=2))

        return prompt
    
    def _call_llm(self, prompt: str, model: Optional[str] = None, json_mode: bool = False) -> str:
        """调用 LLM API（委托 LLMClient）

        Args:
            prompt: 提示词
            model: 覆盖默认模型（用于过滤裁决等使用不同档位模型的场景）
            json_mode: 请求结构化 JSON 输出（DeepSeek response_format=json_object 等）
        """
        return self.llm_client.call(prompt, model=model, json_mode=json_mode)
    
    def _parse_llm_response(self, response: str, batch: List[PaperSegment],
                             valid_ids: Optional[Set[int]] = None):
        """解析 LLM 响应并更新段落（新JSON格式）。

        valid_ids: 本批合法 id 集合（segment_id）。与 filter 侧 _parse_filter_response 同款
        防护：flash 级模型会把 prompt 里输出示例的 id 原样回显——若示例 id 落在真实 segment_id
        空间内，回显内容（示例的"中文标题"/"中文摘要"等占位文本）会静默覆盖同 id 的真实论文。
        传入本批 valid_ids 后不在集合内的条目直接丢弃，不让示例回显或幻觉 id 污染结果。
        留空（None/未传）时不做过滤，兼容测试与未传批次上下文的调用方。"""
        salvaged_mode = False
        try:
            # 尝试提取 JSON
            json_match = response
            if '```json' in response:
                json_match = response.split('```json')[1].split('```')[0]
            elif '```' in response:
                json_match = response.split('```')[1].split('```')[0]
            
            try:
                papers = json.loads(json_match.strip())
            except json.JSONDecodeError as je:
                # 2026-08-17 那轮三个独立批次各废 5 篇（batch=5），报错全在文末——
                # 是**响应被截断**，不是漏逗号（详见 json_tools.loads_lenient 的 docstring）。
                # 抢救前缀后，未覆盖到的论文走下方既有的 "Not found in LLM response"
                # 分支标 FAILED，与原行为一致，但前缀里的论文得以保住。
                # **这是第四道防线**：根因修复应在 _call_agent（接 --json-schema、查
                # stop_reason、真正传 max_tokens），其次是解析失败重试一次
                # （topics.py:synthesize_topic / qa.py 已有该范式，digest 是唯一漏掉的主链路）。
                salvaged = loads_lenient(json_match)
                if salvaged is None:
                    raise
                logger.warning("  ⚠️ 翻译 JSON 畸形，已抢救前缀（本批部分论文将标 FAILED）: {}".format(je))
                papers = salvaged
                salvaged_mode = True
                self._salvage_batches += 1

            # 支持两种格式：直接数组或 {papers: [...]}
            if isinstance(papers, dict):
                if 'papers' in papers:
                    papers = papers['papers']
                else:
                    # 包装键容错：顶层是对象但不含 'papers' 键（模型自造了别的包装键名，
                    # 如 {"results": [...]}），若恰好只有一个 list 型 value 就取它——
                    # 与 filter 侧 _parse_filter_response 的 verdicts/papers 双重兼容口径对齐。
                    list_values = [v for v in papers.values() if isinstance(v, list)]
                    if len(list_values) == 1:
                        papers = list_values[0]

            # 创建 ID 到结果的映射（与 filter 侧 _parse_filter_response 同款双保险：
            # 单条 id 非数字容错 + 本批合法 id 集合过滤，挡示例回显/幻觉 id）
            results_map: Dict[int, Dict[str, Any]] = {}
            for p in papers:
                if not (isinstance(p, dict) and 'id' in p):
                    continue
                try:
                    p_id = int(p['id'])
                except (TypeError, ValueError):
                    logger.warning("  响应含非数字 id，丢弃该条: {!r}".format(p.get('id')))
                    continue
                if valid_ids is not None and p_id not in valid_ids:
                    logger.warning("  响应 id={} 不在本批合法 id 集合内，丢弃该条（示例回显/幻觉 id）".format(p_id))
                    continue
                results_map[p_id] = p

            # 更新段落
            for seg in batch:
                if seg.segment_id not in results_map:
                    seg.status = DigestStatus.FAILED
                    seg.error_message = "Not found in LLM response"
                    logger.warning("    [MISS] ID {} not in response".format(seg.segment_id))
                    continue
                result = results_map[seg.segment_id]
                if salvaged_mode and not (result.get('translated_abstract') or '').strip():
                    # 漏逗号也可能落在**同一篇的字段之间**，抢救退到该篇内部的上一个逗号，
                    # 产出 {"id":N} 这种只剩 id 的残骸。此前唯一门槛是「id 在不在」，
                    # 于是它一路走到无条件 status=COMPLETED，计进 processed_papers、
                    # 不计进 failed_papers，札记再按 `translated_abstract or original_abstract`
                    # 静默回退成英文——一篇假成功比三篇诚实失败更难查。
                    seg.status = DigestStatus.FAILED
                    seg.error_message = "salvaged-incomplete: 抢救出的条目缺 translated_abstract"
                    self._salvage_dropped += 1
                    logger.warning("    [SALVAGE-INCOMPLETE] id={} 抢救残缺，标 FAILED".format(seg.segment_id))
                    continue
                try:
                    # 更新翻译内容。
                    # translated_abstract 先落地再算默认值（而非把 [:200] 塞进 .get 的默认实参）——
                    # Python 无条件先求值默认实参，LLM 返回 translated_abstract=null 时
                    # None[:200] 会当场 TypeError。
                    # summary：_build_batch_prompt 的输出契约里没有这个字段（prompt 从未
                    # 要求模型产出），LLM 恒不返回 —— 不再拿摘要前 200 字冒充「AI 归纳」，
                    # 留空则 notes.py/schema.py/docx_builder.py 的 AI 归纳/AI总结块按现有
                    # `if seg.summary` 判断自然不渲染，不再产出与摘要逐字重复的假总结。
                    abstract = result.get('translated_abstract') or ''
                    seg.translated_abstract = abstract
                    seg.summary = result.get('summary') or ''

                    # 更新元数据。以下每个字段都用 `or 默认值` 兜 LLM 返回 null 的情形——
                    # 同一批里一篇 relevance_score/priority_score/keywords=null 曾经在
                    # float(None)/None 上直接 TypeError，被本 try 外层的批级 except 吞掉后，
                    # 连带把「排在它之后、已解析正常」的论文也错杀成 FAILED；keywords=None
                    # 不兜底还会静默写进 metadata，让下游 ", ".join(keywords) 二次爆炸。
                    if 'translated_title' in result:
                        seg.metadata.translated_title = result['translated_title'] or seg.metadata.translated_title
                    if 'keywords' in result:
                        seg.metadata.keywords = result['keywords'] or []
                    if 'relevance_score' in result:
                        # 钳制到 [0,1]：越界值（模型按 0-10/0-100 标度打分）不能直接
                        # 决定排序与选篇，理由见 _clamp_unit_score 的说明
                        seg.metadata.relevance_score = self._clamp_unit_score(result['relevance_score'])
                    if 'source_type' in result:
                        seg.metadata.source_type = result['source_type'] or seg.metadata.source_type
                    if 'paper_type' in result:
                        seg.metadata.paper_type = result['paper_type'] or seg.metadata.paper_type
                    if 'priority_score' in result:
                        # 使用 LLM 返回的优先级覆盖规则计算的值；同样钳制到 [0,1]，
                        # 否则 9.5 这类幻觉分会挤掉全部合规分数进精读 top-N/thesis 证据池
                        seg.priority_score = self._clamp_unit_score(result['priority_score'])
                    if 'priority_breakdown' in result:
                        breakdown = result['priority_breakdown'] or {}
                        seg.metadata.priority_reason = "source={:.1f}, field={:.1f}, recency={:.1f}, type={:.1f}".format(
                            breakdown.get('source_score', 0) or 0,
                            breakdown.get('field_score', 0) or 0,
                            breakdown.get('recency_score', 0) or 0,
                            breakdown.get('type_score', 0) or 0
                        )
                    if result.get('relevance_reason'):
                        seg.metadata.priority_reason = (seg.metadata.priority_reason or "") + " | " + result['relevance_reason']

                    seg.status = DigestStatus.COMPLETED
                    seg.processed_at = datetime.now()
                    logger.info("    [OK] {} (priority={:.2f}, relevance={:.2f})".format(
                        seg.metadata.title[:40], seg.priority_score, seg.metadata.relevance_score or 0
                    ))
                except Exception as exc:
                    # 单篇脏数据（某字段被 LLM 返回了意外类型）只废这一篇——绝不能让
                    # 异常冒出这层 for 循环，否则外层的批级 except 会把本批「排在它
                    # 之后」的所有论文（包括已经解析正常的）全部错杀成 FAILED。
                    seg.status = DigestStatus.FAILED
                    seg.error_message = "Field update failed: {}".format(exc)
                    logger.warning("    [FIELD-ERROR] {}: {}".format(seg.segment_id, exc))

        except json.JSONDecodeError as e:
            logger.error("  [ERROR] JSON parse failed: {}".format(str(e)))
            # 只把尚未成功的论文标记失败，不回写已 COMPLETED 的
            for seg in batch:
                if seg.status != DigestStatus.COMPLETED:
                    seg.status = DigestStatus.FAILED
                    seg.error_message = "JSON parse error: {}".format(str(e))
        except Exception as e:
            logger.error("  [ERROR] Response processing failed: {}".format(str(e)))
            for seg in batch:
                if seg.status != DigestStatus.COMPLETED:
                    seg.status = DigestStatus.FAILED
                    seg.error_message = str(e)
    
    def _step_generate_output(self) -> DigestOutput:
        """Step 4: 生成输出文件"""
        logger.info("\n💾 Step 4: 生成输出文件")
        logger.info("-" * 40)
        
        # 创建输出对象
        output = DigestOutput(
            digest_id=self.run_id,
            title=f"Scholar Digest - {datetime.now().strftime('%Y-%m-%d')}",
            description=f"从 {len(self.processed_emails)} 封邮件中提取的 {len(self.segments)} 篇论文摘要",
            segments=self.segments,
            undecided_segments=self.undecided_segments,
            emails_processed=self.processed_emails,
            status=DigestStatus.COMPLETED,
            created_at=datetime.now()
        )
        # 有未决论文说明本次运行经历过 LLM 裁决失败——不显式喊出来就等于回到静默丢弃
        if self.undecided_segments:
            logger.warning("  ⚠️ {} 篇论文 LLM 裁决失败且未命中白名单，已列入 digest『待人工复核』小节".format(
                len(self.undecided_segments)))
        
        # 保存 JSON 文件
        json_path = self.output_dir / f"{self.run_id}.json"
        output.save_to_file(json_path)
        logger.info(f"  📄 JSON: {json_path}")
        # 记住输出对象/路径：精读在 Step 4.5 才写入 segment，需在其后重写 JSON 才能固化精读
        self._digest_output = output
        self._digest_json_path = json_path
        
        # 生成 Markdown 文件
        md_path = self.output_dir / f"{self.run_id}.md"
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(output.to_markdown())
        logger.info(f"  📝 Markdown: {md_path}")

        # 按月切分 Markdown（跨月时额外输出每月一份，便于渲染）
        self._write_monthly_markdown()

        # 固化被排除论文与过滤裁决（可审计性：每次裁决的判定+理由+模型+prompt 版本）
        excluded_path = self._write_excluded_sidecar()

        # 生成统计信息
        stats_path = self.output_dir / f"{self.run_id}_stats.json"
        stage_counts: Dict[str, int] = {}
        for rec in self.excluded:
            stage_counts[rec['stage']] = stage_counts.get(rec['stage'], 0) + 1
        stats = {
            'run_id': self.run_id,
            'timestamp': datetime.now().isoformat(),
            'total_emails': len(self.processed_emails),
            'total_papers': len(self.segments),
            'processed_papers': sum(1 for s in self.segments if s.is_processed),
            'failed_papers': sum(1 for s in self.segments if s.status == DigestStatus.FAILED),
            'fields_distribution': output.fields_distribution,
            'filter_mode': self.settings.processing.filter_mode,
            'excluded_papers': len(self.excluded),
            'excluded_by_stage': stage_counts,
            'filter_fallback_count': self._filter_fallback_count,
            'undecided_count': len(self.undecided_segments),
            'salvage_batches': self._salvage_batches,
            'salvage_dropped_items': self._salvage_dropped,
            'excluded_file': str(excluded_path) if excluded_path else None,
        }
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        logger.info(f"  📊 统计: {stats_path}")

        return output

    def _step_sync_zotero(self) -> Optional[Dict[str, Any]]:
        """Step 4.5: 把入选论文写入 Zotero，回查 citekey，生成 pandoc 科研札记。

        延迟导入，避免未用 Zotero 联动时也强依赖该模块。Zotero 不可用只 warning、不中断。
        """
        logger.info("\n📥 Step 4.5: Zotero 联动（写库 + citekey + 札记）")
        logger.info("-" * 40)
        if not self.segments:
            logger.info("  无入选论文，跳过")
            return None
        try:
            from .zotero_sync import sync_segments_to_zotero
            from .notes import write_notes
        except Exception as e:
            logger.warning("  ⚠️ Zotero 模块导入失败，跳过: {}".format(e))
            return None

        proc = self.settings.processing
        email = proc.zotero_email or proc.external_email or ""
        results = sync_segments_to_zotero(
            self.segments,
            base_url=proc.zotero_base_url,
            email=email,
            attach_pdf=proc.zotero_attach_pdf,
            enrich_crossref=proc.zotero_enrich_crossref,
            require_collection=proc.zotero_collection,
            translation_server_url=proc.zotero_translation_server_url,
        )
        citekeys = {pid: r.citekey for pid, r in results.items()}

        # 全文精读（高优先级 top-N；从正文归纳 + 句级三色联想）
        if proc.closeread_enabled:
            try:
                from .closereading import close_read_segments
                done = close_read_segments(
                    self.segments, proc.research_interests, self.llm_client,
                    top_n=proc.closeread_top_n, email=email,
                    model=(self.settings.llm.closeread_model or self.settings.llm.model),
                    deep=proc.closeread_deep, max_chars=proc.closeread_max_chars,
                    max_chunks=proc.closeread_max_chunks,
                )
                # 精读就地写入 segment，重写固化 JSON 使审计文件也带精读（否则 Step 4 的 JSON 为空）
                if done and getattr(self, "_digest_output", None) is not None:
                    try:
                        self._digest_output.save_to_file(self._digest_json_path)
                        logger.info("  🔁 已将全文精读回写固化 JSON: {}".format(self._digest_json_path))
                    except Exception as e:
                        logger.warning("  ⚠️ 精读回写 JSON 失败: {}".format(e))
            except Exception as e:
                logger.warning("  ⚠️ 全文精读步骤失败（跳过）: {}".format(e))

        # 语义清晰的标题/文件名：月份 + 是否全文精读
        cr_title = "（全文精读）" if proc.closeread_enabled else ""
        cr_fname = "_全文精读" if proc.closeread_enabled else ""
        label = self.date_range[0].strftime('%Y-%m') if self.date_range \
            else datetime.now().strftime('%Y-%m-%d')
        note_title = "科研札记 · {}{}".format(label, cr_title)
        note_fname = "科研札记_{}{}".format(label, cr_fname)
        summary = write_notes(
            self.segments,
            citekeys,
            out_dir=proc.notes_dir,
            instruction=proc.notes_instruction,
            digest_title=note_title,
            filename=note_fname,
            emit_docx=proc.notes_emit_docx,
            cjk_font=proc.notes_docx_cjk_font,
        )
        # 固化同步结果，便于审计/重跑
        sync_path = self.output_dir / f"{self.run_id}_zotero.json"
        try:
            with open(sync_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'run_id': self.run_id,
                    'saved': sum(1 for r in results.values() if r.saved),
                    'resolved_citekey': sum(1 for r in results.values() if r.citekey),
                    'oa_hit': sum(1 for r in results.values() if r.pdf_url),
                    'notes': summary,
                    'papers': [
                        {'paper_id': r.paper_id, 'citekey': r.citekey,
                         'saved': r.saved, 'oa_status': r.oa_status}
                        for r in results.values()
                    ],
                }, f, ensure_ascii=False, indent=2)
            logger.info("  🗂️ 同步记录: {}".format(sync_path))
        except Exception as e:
            logger.warning("  ⚠️ 写同步记录失败: {}".format(e))
        return summary

    def _write_excluded_sidecar(self) -> Optional[Path]:
        """写 {run_id}_excluded.json：被排除论文（元数据+摘要+完整裁决）与入选裁决记录"""
        if not self.excluded and not self.included_decisions:
            return None

        stage_counts: Dict[str, int] = {}
        for rec in self.excluded:
            stage_counts[rec['stage']] = stage_counts.get(rec['stage'], 0) + 1

        mode = self.settings.processing.filter_mode
        sidecar = {
            'run_id': self.run_id,
            'description': '被过滤论文固化与过滤裁决审计记录（含黑名单预过滤与白名单裁决）',
            'filter_mode': mode,
            'model': (self.settings.llm.filter_model or self.settings.llm.model) if mode == 'llm' else None,
            'prompt_version': self._filter_prompt_version,
            'blacklist_count': stage_counts.get('blacklist_keyword', 0),
            'llm_excluded_count': stage_counts.get('llm_judge', 0),
            'whitelist_miss_count': stage_counts.get('whitelist_keyword', 0) + stage_counts.get('keyword_fallback', 0),
            'fallback_count': self._filter_fallback_count,
            'unique_excluded': len(self.excluded),
            'included_decisions': [
                {'paper_id': d.paper_id, 'title': d.title, 'stage': d.stage, 'reason': d.reason}
                for d in self.included_decisions
            ],
            'papers': self.excluded,
        }

        excluded_path = self.output_dir / f"{self.run_id}_excluded.json"
        with open(excluded_path, 'w', encoding='utf-8') as f:
            json.dump(sidecar, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"  🗂️ 排除固化: {excluded_path} ({len(self.excluded)} 篇)")
        return excluded_path

    def _month_key(self, segment: PaperSegment) -> str:
        """取论文的归属月份（优先邮件接收时间，回退发布日期）"""
        dt = segment.metadata.email_received_at or segment.metadata.publication_date
        return dt.strftime('%Y-%m') if dt else 'unknown'

    def _write_monthly_markdown(self) -> List[Path]:
        """跨月时按月份拆分输出 Markdown，返回生成的文件路径列表"""
        monthly: Dict[str, List[PaperSegment]] = {}
        for seg in self.segments:
            monthly.setdefault(self._month_key(seg), []).append(seg)

        if len(monthly) <= 1:
            return []

        written = []
        for key in sorted(monthly):
            segs = monthly[key]
            emails_month = [
                e for e in self.processed_emails
                if e.received_at and e.received_at.strftime('%Y-%m') == key
            ]
            month_output = DigestOutput(
                digest_id=f"{self.run_id}_{key}",
                title=f"Scholar Digest {key}",
                description=f"{key}: {len(segs)} 篇论文（来自 {len(emails_month)} 封邮件）",
                segments=segs,
                emails_processed=emails_month,
                status=DigestStatus.COMPLETED,
                created_at=datetime.now(),
            )
            month_md = self.output_dir / f"{self.run_id}_{key}.md"
            month_md.write_text(month_output.to_markdown(), encoding='utf-8')
            logger.info(f"  📆 月度 Markdown: {month_md} ({len(segs)} 篇)")
            written.append(month_md)
        return written

    def _step_mark_emails_read(self):
        """Step 5: 标记邮件为已读"""
        logger.info("\n✅ Step 5: 标记邮件为已读")
        logger.info("-" * 40)
        
        # 如果设置了获取历史所有邮件，或者显式开启了全量清理
        if self.settings.processing.days_to_fetch == 0:
            count = self.gmail_client.mark_all_scholar_read()
            if count > 0:
                logger.info(f"  ✅ 已将所有 {count} 封历史 Scholar 邮件标记为已读")
        else:
            # 仅标记本次处理过的邮件 ID
            all_ids = [
                email['metadata'].email_id 
                for email in self.emails
            ]
            
            if all_ids:
                success = self.gmail_client.mark_as_read(all_ids)
                if success:
                    logger.info(f"  ✅ 已将 {len(all_ids)} 封处理过的邮件标记为已读")
                else:
                    logger.warning(f"  ⚠️ 标记已读失败")
            else:
                logger.info("  ℹ️ 没有需要标记的邮件")
    
    # ==================== 辅助方法 ====================
    
    def load_previous_digest(self, digest_id: str) -> Optional[DigestOutput]:
        """加载之前的摘要结果"""
        json_path = self.output_dir / f"{digest_id}.json"
        if json_path.exists():
            return DigestOutput.load_from_file(json_path)
        return None
    
    def get_paper_by_id(self, paper_id: str) -> Optional[PaperSegment]:
        """根据 ID 获取论文"""
        for seg in self.segments:
            if seg.paper_id == paper_id:
                return seg
        return None
    
    def export_to_csv(self, output_path: Optional[Path] = None) -> Path:
        """导出为 CSV 格式"""
        import csv
        
        if output_path is None:
            output_path = self.output_dir / f"{self.run_id}.csv"
        
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            
            # 表头
            writer.writerow([
                'Paper ID', 'Title', 'Authors', 'Field', 'DOI',
                'Abstract (Original)', 'Abstract (Translated)', 'Summary',
                'URL', 'Publication Date', 'Status'
            ])
            
            # 数据行
            for seg in self.segments:
                meta = seg.metadata
                writer.writerow([
                    meta.paper_id,
                    meta.title,
                    ', '.join(meta.authors),
                    meta.field,
                    meta.doi or '',
                    seg.original_abstract[:500] if seg.original_abstract else '',
                    seg.translated_abstract[:500] if seg.translated_abstract else '',
                    seg.summary[:500] if seg.summary else '',
                    meta.url or '',
                    meta.publication_date.isoformat() if meta.publication_date else '',
                    seg.status
                ])
        
        logger.info(f"📄 CSV 导出: {output_path}")
        return output_path
