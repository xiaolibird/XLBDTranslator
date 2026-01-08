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
        
        logger.info(f"📊 ScholarWorkflow 初始化完成")
        logger.info(f"   运行ID: {self.run_id}")
        logger.info(f"   输出目录: {self.output_dir}")
    
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
            logger.info(f"   处理邮件: {len(self.processed_emails)}")
            logger.info(f"   提取论文: {len(self.segments)}")
            logger.info("=" * 60)
            
            return output
            
        except Exception as e:
            logger.error(f"❌ 工作流执行失败: {e}")
            raise
    
    def _step_fetch_emails(self):
        """Step 1: 获取 Google Scholar 邮件"""
        logger.info("\n📥 Step 1: 获取 Google Scholar 邮件")
        logger.info("-" * 40)
        
        # 获取用户信息（同时触发认证）
        profile = self.gmail_client.get_user_profile()
        logger.info(f"✅ 已登录: {profile.get('emailAddress')}")
        
        # 获取邮件
        self.emails = self.gmail_client.fetch_scholar_emails(
            days=self.settings.processing.days_to_fetch,
            max_results=self.settings.processing.max_emails,
            unread_only=False
        )
        
        logger.info(f"📧 获取到 {len(self.emails)} 封 Scholar 邮件")
    
    def _step_parse_emails(self):
        """Step 2: 解析邮件提取论文"""
        logger.info("\n📄 Step 2: 解析邮件提取论文")
        logger.info("-" * 40)
        
        self.parser.reset_counter()
        seen_paper_ids = set()
        
        for email_data in self.emails:
            metadata = email_data['metadata']
            body = email_data['body']
            
            logger.info(f"  📧 处理: {metadata.subject[:50]}...")
            
            # 解析邮件
            papers = self.parser.parse_email(body, metadata)
            
            # 去重
            for paper in papers:
                if paper.paper_id not in seen_paper_ids:
                    seen_paper_ids.add(paper.paper_id)
                    self.segments.append(paper)
            
            # 更新邮件元数据
            metadata.papers_extracted = len(papers)
            metadata.is_processed = True
            self.processed_emails.append(metadata)
        
        logger.info(f"✅ 共提取 {len(self.segments)} 篇唯一论文（去重后）")
    
    def _step_process_papers(self):
        """Step 3: 批量处理论文（翻译和摘要）"""
        logger.info("\n🤖 Step 3: LLM 处理论文摘要")
        logger.info("-" * 40)
        
        batch_size = self.settings.processing.batch_size
        total = len(self.segments)
        
        # 分批处理
        for i in range(0, total, batch_size):
            batch_segments = self.segments[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (total + batch_size - 1) // batch_size
            
            logger.info(f"  📦 处理批次 {batch_num}/{total_batches} ({len(batch_segments)} 篇)")
            
            try:
                self._process_batch(batch_segments)
            except Exception as e:
                logger.error(f"  ❌ 批次 {batch_num} 处理失败: {e}")
                # 继续处理下一批
                continue
        
        # 统计
        processed = sum(1 for s in self.segments if s.is_processed)
        logger.info(f"✅ 处理完成: {processed}/{total} 篇论文")
    
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
        """构建批量处理的 prompt"""
        papers_data = []
        for seg in batch:
            papers_data.append({
                "id": seg.segment_id,
                "title": seg.metadata.title,
                "authors": ", ".join(seg.metadata.authors[:3]) if seg.metadata.authors else "Unknown",
                "abstract": seg.original_abstract[:1000] if seg.original_abstract else "No abstract available",
                "field": seg.metadata.field,
            })
        
        prompt = f"""你是一个学术论文摘要助手。请对以下论文进行处理：

1. 将每篇论文的标题和摘要翻译成中文
2. 为每篇论文生成一个简洁的总结（100-200字）

输入论文列表（JSON格式）：
```json
{json.dumps(papers_data, ensure_ascii=False, indent=2)}
```

请按以下 JSON 格式输出结果（必须是有效的 JSON）：
```json
{{
  "papers": [
    {{
      "id": <segment_id>,
      "translated_title": "<中文标题>",
      "translated_abstract": "<中文摘要>",
      "summary": "<简洁总结，突出研究的主要贡献和发现>"
    }}
  ]
}}
```

注意：
- 翻译要准确、学术化
- 总结要简洁，突出核心贡献
- 保持 JSON 格式正确
- 如果没有摘要，请根据标题推测可能的研究方向
"""
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
        """解析 LLM 响应并更新段落"""
        try:
            # 尝试提取 JSON
            json_match = response
            if '```json' in response:
                json_match = response.split('```json')[1].split('```')[0]
            elif '```' in response:
                json_match = response.split('```')[1].split('```')[0]
            
            data = json.loads(json_match.strip())
            papers = data.get('papers', [])
            
            # 创建 ID 到结果的映射
            results_map = {p['id']: p for p in papers}
            
            # 更新段落
            for seg in batch:
                if seg.segment_id in results_map:
                    result = results_map[seg.segment_id]
                    seg.translated_abstract = result.get('translated_abstract', '')
                    seg.summary = result.get('summary', '')
                    seg.status = DigestStatus.COMPLETED
                    seg.processed_at = datetime.now()
                    
                    # 更新元数据中的标题
                    if 'translated_title' in result:
                        # 可以添加一个 translated_title 字段
                        pass
                else:
                    seg.status = DigestStatus.FAILED
                    seg.error_message = "Not found in LLM response"
                    
        except json.JSONDecodeError as e:
            logger.error(f"  ❌ JSON 解析失败: {e}")
            # 标记批次中所有论文为失败
            for seg in batch:
                seg.status = DigestStatus.FAILED
                seg.error_message = f"JSON parse error: {str(e)}"
        except Exception as e:
            logger.error(f"  ❌ 响应处理失败: {e}")
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
        
        unread_ids = [
            email['metadata'].email_id 
            for email in self.emails 
            if not email['metadata'].is_read
        ]
        
        if unread_ids:
            success = self.gmail_client.mark_as_read(unread_ids)
            if success:
                logger.info(f"  ✅ 已标记 {len(unread_ids)} 封邮件为已读")
            else:
                logger.warning(f"  ⚠️ 标记已读失败")
        else:
            logger.info("  ℹ️ 没有需要标记的未读邮件")
    
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
