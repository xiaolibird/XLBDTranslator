# -*- coding: utf-8 -*-
"""
Scholar Digest 工作流
完整的论文摘要处理流程
"""
import json
import hashlib
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional

from .schema import (
    ScholarSettings,
    PaperSegment,
    PaperSegmentList,
    EmailMetadata,
    DigestOutput,
    DigestBatch,
    DigestStatus,
)
from .gmail_client import GmailClient
from .paper_extractor import ScholarEmailParser
from ..utils.logger import get_logger

logger = get_logger(__name__)


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
        """创建 LLM 客户端"""
        provider = self.settings.llm.provider.lower()
        
        if provider == 'gemini':
            from google import genai
            from google.genai import types
            
            client = genai.Client(api_key=self.settings.llm.api_key)
            return {
                'client': client,
                'model': self.settings.llm.model,
                'provider': 'gemini'
            }
        else:
            # OpenAI 兼容接口
            import httpx
            return {
                'client': httpx.Client(timeout=60.0),
                'base_url': self.settings.llm.api_key,  # 假设存储了 base_url
                'model': self.settings.llm.model,
                'provider': 'openai-compatible'
            }
    
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
            
            # Step 2: 解析邮件提取论文
            self._step_parse_emails()
            
            # Step 3: 批量处理（翻译和摘要）
            if self.settings.processing.translate_abstracts or self.settings.processing.generate_summary:
                self._step_process_papers()
            
            # Step 4: 生成输出
            output = self._step_generate_output()
            
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
        
        # 获取邮件
        self.emails = self.gmail_client.fetch_scholar_emails(
            days=self.settings.processing.days_to_fetch,
            max_results=self.settings.processing.max_emails,
            unread_only=False
        )
        
        logger.info("获取到 {} 封 Scholar 邮件".format(len(self.emails)))
    
    def _step_parse_emails(self):
        """Step 2: 解析邮件提取论文"""
        logger.info("\nStep 2: 解析邮件提取论文")
        logger.info("-" * 40)
        
        self.parser.reset_counter()
        seen_paper_ids = set()
        seen_dois = set()
        
        whitelist = self.settings.processing.whitelist
        blacklist = self.settings.processing.blacklist
        
        for email_data in self.emails:
            metadata = email_data['metadata']
            body = email_data['body']
            
            logger.info("  处理邮件: {}".format(metadata.subject[:50]))
            
            # 解析邮件
            papers = self.parser.parse_email(body, metadata)
            
            extracted_count = 0
            # 过滤与去重
            for paper in papers:
                # 1. 检查黑白名单
                if not self._filter_paper(paper, whitelist, blacklist):
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
        
        logger.info("共提取 {} 篇唯一论文（经过黑白名单筛选与 DOI/ID 去重）".format(len(self.segments)))

    def _filter_paper(self, paper: PaperSegment, whitelist: List[str], blacklist: List[str]) -> bool:
        """
        根据黑白名单过滤论文
        
        Args:
            paper: 论文对象
            whitelist: 白名单关键词
            blacklist: 黑名单关键词
            
        Returns:
            bool: 是否保留
        """
        # 合并标题和摘要进行搜索
        content = (paper.metadata.title + " " + (paper.original_abstract or "")).lower()
        
        # 1. 黑名单优先：只要命中任何一个黑名单关键词，直接剔除
        for word in blacklist:
            if word.lower() in content:
                logger.debug("  跳过论文 (黑名单: {}): {}".format(word, paper.metadata.title[:50]))
                return False
        
        # 2. 白名单检查：如果设置了白名单，则必须命中其中之一
        if whitelist:
            found_in_white = False
            for word in whitelist:
                if word.lower() in content:
                    found_in_white = True
                    break
            
            if not found_in_white:
                logger.debug("  跳过论文 (未命中白名单): {}".format(paper.metadata.title[:50]))
                return False
        
        return True
    
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
        
        self._sort_by_priority()
    
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
{}
```

请严格按照JSON格式输出，ID必须与输入一一对应。""".format(json.dumps(papers_data, ensure_ascii=False, indent=2))
        
        return prompt
    
    def _call_llm(self, prompt: str) -> str:
        """调用 LLM API"""
        provider = self.llm_client.get('provider')
        
        if provider == 'gemini':
            from google.genai import types
            
            client = self.llm_client['client']
            model = self.llm_client['model']
            
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=self.settings.llm.temperature,
                    max_output_tokens=self.settings.llm.max_output_tokens,
                )
            )
            
            return response.text
        else:
            # OpenAI 兼容接口
            # TODO: 实现 OpenAI 兼容调用
            raise NotImplementedError("OpenAI compatible API not implemented yet")
    
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
            # 标记批次中所有论文为失败
            for seg in batch:
                seg.status = DigestStatus.FAILED
                seg.error_message = "JSON parse error: {}".format(str(e))
        except Exception as e:
            logger.error("  [ERROR] Response processing failed: {}".format(str(e)))
            for seg in batch:
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
        
        # 生成 Markdown 文件
        md_path = self.output_dir / f"{self.run_id}.md"
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(output.to_markdown())
        logger.info(f"  📝 Markdown: {md_path}")
        
        # 生成统计信息
        stats_path = self.output_dir / f"{self.run_id}_stats.json"
        stats = {
            'run_id': self.run_id,
            'timestamp': datetime.now().isoformat(),
            'total_emails': len(self.processed_emails),
            'total_papers': len(self.segments),
            'processed_papers': sum(1 for s in self.segments if s.is_processed),
            'failed_papers': sum(1 for s in self.segments if s.status == DigestStatus.FAILED),
            'fields_distribution': output.fields_distribution,
        }
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        logger.info(f"  📊 统计: {stats_path}")
        
        return output
    
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
