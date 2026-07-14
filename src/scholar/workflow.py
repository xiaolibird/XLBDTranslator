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
from typing import List, Dict, Any, Optional, Tuple

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
from .paper_extractor import ScholarEmailParser
from ..utils.logger import get_logger

logger = get_logger(__name__)

# LLM 白名单裁决：每次调用送审的论文数（flash 级模型，单批成本极低）
FILTER_BATCH_SIZE = 20

# 裁决 prompt 模板文件；缺失时使用内嵌兜底模板（保证 cron 无人值守可跑）
FILTER_PROMPT_FILE = Path("config/prompts/whitelist_filter_prompt.md")

FILTER_PROMPT_FALLBACK = """<!-- version: filter-v2-embedded -->
# ROLE
你是一名方法学审稿助手，为一篇投稿的论文做文献筛选。

## 论文主题与定位
{{RESEARCH_INTERESTS}}

# TASK
对下面每条记录（title + abstract）输出筛选判定，只依据给定文本，不得脑补。

# 纳入维度（命中任一 → 进入候选池）
A 缺失机制方法学；B 缺失感知建模；C 缺失×因果；D 跨域/跨中心迁移；
E 对抗性证据（结论与本文相反也**必须捕获**）；F 多库 ICU 基准；G 临床落地场景。

# 排除维度（命中 → drop，除非同时强命中 A/C/E）
X1 纯影像/基因组无表格EHR；X2 纯填补增量无机制；X3 无定量证据的综述；
X4 通用LLM未涉缺失/迁移；X5 无方法贡献的RCT/流调；X6 纯因果理论无实证；X7 无实验短文。

# RULES
- 宁可 MAYBE 不可 EXCLUDE：与 C 或 E 沾边的一律不低于 MAYBE。
- 结论与本文相左 ≠ 排除理由；THREAT 类 role 设为 MUST_ENGAGE。
- 摘要不足以判定则 decision=MAYBE 且 confidence≤0.4。

# OUTPUT（严格 JSON 数组，无 markdown，无前后缀）
```json
[
  {"id": 1, "decision": "INCLUDE|MAYBE|EXCLUDE", "bucket": ["A"], "exclude_reason": null,
   "flags": [], "role": "BACKGROUND", "one_line": "≤30字用处", "confidence": 0.8}
]
```

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
        self._filter_prompt_template: Optional[str] = None
        self._filter_prompt_version: Optional[str] = None
        self._filter_fallback_count = 0

        # 跨来源去重（邮件路径与外部检索共用，保证 Gmail/PubMed/arXiv 不重复入库）
        self._seen_paper_ids: set = set()
        self._seen_dois: set = set()

        # 显式日期区间 [start,end]（按月回填时由 CLI 设置；None=用相对 days_to_fetch）
        self.date_range: Optional[Tuple[date, date]] = None
        
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
        """关键词白名单过滤，为每篇论文产出裁决记录"""
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
                response = self._call_llm(prompt, model=model)
                verdicts = self._parse_filter_response(response)
            except Exception as e:
                logger.warning("  批次 {} LLM 裁决失败，回退关键词白名单: {}".format(batch_num, e))
                self._filter_fallback_count += len(batch)
                kept.extend(self._filter_by_whitelist(batch, stage="keyword_fallback"))
                continue

            for paper in batch:
                verdict = verdicts.get(paper.segment_id)
                if verdict is None:
                    # LLM 响应缺失该 id：单篇回退关键词判断
                    logger.warning("  [MISS] 裁决结果缺失 id={}，回退关键词".format(paper.segment_id))
                    self._filter_fallback_count += 1
                    kept.extend(self._filter_by_whitelist([paper], stage="keyword_fallback"))
                    continue

                # 单篇裁决构造独立容错：flash 级模型常把 confidence 返回成 "high"、
                # bucket/flags 返回非 list 等，绝不能因一篇脏字段让整批 digest 崩溃。
                try:
                    decision = self._build_filter_decision(paper, verdict, model, prompt_version)
                except Exception as e:
                    logger.warning("  裁决字段异常 id={}，回退关键词: {}".format(paper.segment_id, e))
                    self._filter_fallback_count += 1
                    kept.extend(self._filter_by_whitelist([paper], stage="keyword_fallback"))
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
        """裁决 prompt 版本标识：模板声明的版本标签 + 模板内容 md5 前 8 位"""
        if self._filter_prompt_version is None:
            template = self._get_filter_prompt_template()
            m = re.search(r'<!--\s*version:\s*(.+?)\s*-->', template)
            label = m.group(1) if m else "unversioned"
            digest = hashlib.md5(template.encode('utf-8')).hexdigest()[:8]
            self._filter_prompt_version = "{}@{}".format(label, digest)
        return self._filter_prompt_version

    def _build_filter_prompt(self, batch: List[PaperSegment]) -> str:
        """构建批量裁决 prompt"""
        papers_data = []
        for seg in batch:
            papers_data.append({
                "id": seg.segment_id,
                "title": seg.metadata.title,
                "abstract": (seg.original_abstract or "")[:800],
                "journal": seg.metadata.journal,
            })

        template = self._get_filter_prompt_template()
        prompt = template.replace("{{RESEARCH_INTERESTS}}", self.settings.processing.research_interests)
        prompt = prompt.replace("{{PAPERS_JSON}}", json.dumps(papers_data, ensure_ascii=False, indent=2))
        return prompt

    def _parse_filter_response(self, response: str) -> Dict[int, Dict[str, Any]]:
        """解析方法学审稿裁决响应为 {id: item} 映射（item 含 decision/bucket/flags/role/one_line/confidence）；
        解析失败抛异常由调用方回退。"""
        json_text = response
        if '```json' in response:
            json_text = response.split('```json')[1].split('```')[0]
        elif '```' in response:
            json_text = response.split('```')[1].split('```')[0]

        verdicts = json.loads(json_text.strip())
        if isinstance(verdicts, dict) and 'papers' in verdicts:
            verdicts = verdicts['papers']
        if not isinstance(verdicts, list):
            raise ValueError("裁决响应不是 JSON 数组")

        result = {}
        for item in verdicts:
            if isinstance(item, dict) and 'id' in item and 'decision' in item:
                result[int(item['id'])] = item
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
        
        # 分批处理
        for i in range(0, total, batch_size):
            batch_segments = self.segments[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (total + batch_size - 1) // batch_size
            
            logger.info("  处理批次 {}/{} ({} 篇)".format(batch_num, total_batches, len(batch_segments)))
            
            try:
                self._process_batch(batch_segments)
            except Exception as e:
                logger.error("  批次 {} 处理失败: {}".format(batch_num, e))
                # 继续处理下一批
                continue
        
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

            # 方法学审稿加成：THREAT/MUST_ENGAGE 置顶（对抗性证据最需优先阅读）
            bonus, bonus_reason = self._filter_priority_bonus(seg)
            if bonus:
                seg.priority_score += bonus
                seg.priority_reason += " " + bonus_reason

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
        """计算时效性评分"""
        if not pub_date:
            return 0.5
        
        from datetime import date
        current_year = date.today().year
        
        if hasattr(pub_date, 'year'):
            year = pub_date.year
        else:
            return 0.5
        
        if year >= current_year:
            return 1.0
        elif year == current_year - 1:
            return 0.9
        elif year == current_year - 2:
            return 0.7
        elif year == current_year - 3:
            return 0.5
        else:
            return 0.3
    
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
    
    def _sort_by_priority(self):
        """按优先级对论文排序"""
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
        
        # 调用 LLM
        response = self._call_llm(prompt)
        
        # 解析响应并更新段落
        self._parse_llm_response(response, batch)
    
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
        
        prompt = """你是一位医学人工智能领域的博士生导师，正在帮助学生筛选和分析论文。

## 研究背景
学生研究方向：电子健康记录(EHR)数据挖掘、临床预测模型、图神经网络(GNN)、半监督学习、大语言模型(LLM)在医学中的应用

## 任务
对以下论文进行：
1. 标题和摘要翻译（首次出现的术语需标注英文原文）
2. 提取5个关键词
3. 评估与研究方向的相关度(0-1)
4. 计算综合优先级(0-1)

## 评分规则
- source_score: 顶级期刊(Lancet/NEJM/Nature)=1.0, 专业期刊(JAMIA/JBI)=0.9, 一般期刊=0.7, 会议=0.6, 预印本=0.4
- field_score: 医学信息学/EHR/临床预测=1.0, 医学AI=0.9, 通用ML=0.6, 其他=0.3
- recency_score: 2026年=1.0, 2025年=0.9, 2024年=0.7, 2023年=0.5
- type_score: 综述/Meta分析=1.0, 方法论=0.9, 原创研究=0.7, 应用研究=0.5
- citation_score: 引用量 > 1000 = 1.0, > 100 = 0.7, > 10 = 0.4, 0 = 0.0
- priority_score = 0.25*source + 0.25*field + 0.15*recency + 0.15*type + 0.2*citation

## 输出格式（严格JSON数组）
```json
[
  {
    "id": 1,
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
```

## 输入论文
```json
__PAPERS_JSON__
```

请严格按照JSON格式输出，ID必须与输入一一对应。"""
        # 不能用 str.format()：模板中的 JSON 示例花括号会被当成格式占位符（KeyError）
        prompt = prompt.replace("__PAPERS_JSON__", json.dumps(papers_data, ensure_ascii=False, indent=2))
        
        return prompt
    
    def _call_llm(self, prompt: str, model: Optional[str] = None) -> str:
        """调用 LLM API（委托 LLMClient）

        Args:
            prompt: 提示词
            model: 覆盖默认模型（用于过滤裁决等使用不同档位模型的场景）
        """
        return self.llm_client.call(prompt, model=model)
    
    def _parse_llm_response(self, response: str, batch: List[PaperSegment]):
        """解析 LLM 响应并更新段落（新JSON格式）"""
        try:
            # 尝试提取 JSON
            json_match = response
            if '```json' in response:
                json_match = response.split('```json')[1].split('```')[0]
            elif '```' in response:
                json_match = response.split('```')[1].split('```')[0]
            
            papers = json.loads(json_match.strip())
            
            # 支持两种格式：直接数组或 {papers: [...]}
            if isinstance(papers, dict) and 'papers' in papers:
                papers = papers['papers']
            
            # 创建 ID 到结果的映射
            results_map = {p['id']: p for p in papers}
            
            # 更新段落
            for seg in batch:
                if seg.segment_id in results_map:
                    result = results_map[seg.segment_id]
                    
                    # 更新翻译内容
                    seg.translated_abstract = result.get('translated_abstract', '')
                    seg.summary = result.get('summary', seg.translated_abstract[:200])
                    
                    # 更新元数据
                    if 'translated_title' in result:
                        seg.metadata.translated_title = result['translated_title']
                    if 'keywords' in result:
                        seg.metadata.keywords = result['keywords']
                    if 'relevance_score' in result:
                        seg.metadata.relevance_score = float(result['relevance_score'])
                    if 'source_type' in result:
                        seg.metadata.source_type = result['source_type']
                    if 'paper_type' in result:
                        seg.metadata.paper_type = result['paper_type']
                    if 'priority_score' in result:
                        # 使用 LLM 返回的优先级覆盖规则计算的值
                        seg.priority_score = float(result['priority_score'])
                    if 'priority_breakdown' in result:
                        breakdown = result['priority_breakdown']
                        seg.metadata.priority_reason = "source={:.1f}, field={:.1f}, recency={:.1f}, type={:.1f}".format(
                            breakdown.get('source_score', 0),
                            breakdown.get('field_score', 0),
                            breakdown.get('recency_score', 0),
                            breakdown.get('type_score', 0)
                        )
                    if 'relevance_reason' in result:
                        seg.metadata.priority_reason = (seg.metadata.priority_reason or "") + " | " + result['relevance_reason']
                    
                    seg.status = DigestStatus.COMPLETED
                    seg.processed_at = datetime.now()
                    logger.info("    [OK] {} (priority={:.2f}, relevance={:.2f})".format(
                        seg.metadata.title[:40], seg.priority_score, seg.metadata.relevance_score or 0
                    ))
                else:
                    seg.status = DigestStatus.FAILED
                    seg.error_message = "Not found in LLM response"
                    logger.warning("    [MISS] ID {} not in response".format(seg.segment_id))
                    
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
            emails_processed=self.processed_emails,
            status=DigestStatus.COMPLETED,
            created_at=datetime.now()
        )
        
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
