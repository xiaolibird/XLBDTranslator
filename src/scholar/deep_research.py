# -*- coding: utf-8 -*-
"""
Deep Research API Integration
用于调用 Gemini Deep Research 生成博士论文绪论和文献综述
"""
import json
import time
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional

from .schema import PaperSegment, PaperMetadata, ScholarSettings
from ..utils.logger import get_logger

logger = get_logger(__name__)


class DeepResearchClient:
    """
    Deep Research API 客户端
    
    用于：
    1. 生成博士论文绪论框架
    2. 生成文献综述
    3. 分析研究趋势
    """
    
    def __init__(self, settings: ScholarSettings):
        """
        初始化客户端
        
        Args:
            settings: ScholarSettings 配置对象
        """
        self.settings = settings
        self._client = None
        
        # 输出目录
        self.output_dir = settings.processing.output_dir / "deep_research"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info("DeepResearchClient initialized")
        logger.info("   Output directory: {}".format(self.output_dir))
    
    @property
    def client(self):
        """懒加载 Gemini 客户端"""
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.settings.llm.api_key)
        return self._client
    
    def generate_thesis_introduction(
        self,
        papers: List[PaperSegment],
        research_topic: str = "EHR Data Mining and Clinical Prediction using GNN and LLM",
        target_words: int = 15000,
        save_output: bool = True
    ) -> Dict[str, Any]:
        """
        生成博士论文绪论
        
        Args:
            papers: 已处理的论文列表（按优先级排序）
            research_topic: 研究主题
            target_words: 目标字数
            save_output: 是否保存输出
            
        Returns:
            生成结果字典
        """
        logger.info("=" * 60)
        logger.info("Starting Thesis Introduction Generation")
        logger.info("=" * 60)
        logger.info("   Research topic: {}".format(research_topic))
        logger.info("   Target words: {}".format(target_words))
        logger.info("   Input papers: {}".format(len(papers)))
        
        # 筛选高优先级论文
        top_papers = self._select_top_papers(papers, max_papers=50)
        logger.info("   Selected top {} papers for analysis".format(len(top_papers)))
        
        # 构建 prompt
        prompt = self._build_thesis_prompt(top_papers, research_topic, target_words)
        
        # 加载 prompt 模板（如果存在）
        prompt_file = Path("config/prompts/thesis_introduction_prompt.md")
        if prompt_file.exists():
            template = prompt_file.read_text(encoding='utf-8')
            prompt = self._merge_with_template(template, top_papers, research_topic)
            logger.info("   Using prompt template from {}".format(prompt_file))
        
        # 调用 Deep Research API
        logger.info("\n[STEP 1] Calling Deep Research API...")
        result = self._call_deep_research(prompt)
        
        # 保存输出
        if save_output and result.get('success'):
            output_file = self._save_result(result, research_topic)
            result['output_file'] = str(output_file)
            logger.info("\n[DONE] Output saved to: {}".format(output_file))
        
        return result
    
    def _select_top_papers(
        self,
        papers: List[PaperSegment],
        max_papers: int = 50,
        min_priority: float = 0.5
    ) -> List[PaperSegment]:
        """
        筛选高优先级论文
        
        Args:
            papers: 所有论文
            max_papers: 最大数量
            min_priority: 最低优先级阈值
        """
        # 按优先级排序
        sorted_papers = sorted(papers, key=lambda x: x.priority_score, reverse=True)
        
        # 筛选
        selected = []
        for paper in sorted_papers:
            if paper.priority_score >= min_priority:
                selected.append(paper)
            if len(selected) >= max_papers:
                break
        
        return selected
    
    def _build_thesis_prompt(
        self,
        papers: List[PaperSegment],
        research_topic: str,
        target_words: int
    ) -> str:
        """构建论文绪论生成 prompt"""
        
        # 格式化论文列表
        papers_text = self._format_papers_for_prompt(papers)
        
        prompt = """# Task: Generate PhD Thesis Introduction

## Research Background
You are helping a PhD student in Medical Informatics write the Introduction chapter of their thesis.

**Research Topic**: {topic}

**Research Areas**:
- Electronic Health Record (EHR) data mining
- Clinical prediction models
- Graph Neural Networks (GNN) for healthcare
- Large Language Models (LLM) in medicine
- Semi-supervised learning for clinical applications

## Requirements

### 1. Structure (Target: {words} words)
1. Research Background (3000-4000 words)
   - Development of medical informatization
   - Value and challenges of EHR data
   - Rise of AI in healthcare

2. Literature Review (8000-10000 words)
   - Traditional clinical prediction methods
   - Deep learning approaches in EHR
   - Graph-based methods for medical data
   - LLM applications in clinical settings
   - Research gaps and opportunities

3. Research Questions (1500-2000 words)
   - Problem statement
   - Research objectives
   - Significance of the study

4. Thesis Structure Overview (1000-1500 words)
   - Chapter organization
   - Methodology preview
   - Expected contributions

### 2. Citation Requirements
- Use numbered citation format [1], [2], etc.
- Include DOI when available
- Prioritize recent publications (2022-2025)
- Include both seminal works and recent advances

### 3. Writing Style
- Academic and formal tone
- Clear logical flow
- Critical analysis, not just description
- Connect different research streams

## Input Papers (Priority Sorted)
{papers}

## Output Format
Please generate the thesis introduction in Markdown format with:
- Clear section headers
- Proper citations
- Reference list at the end

Begin generation:
""".format(
            topic=research_topic,
            words=target_words,
            papers=papers_text
        )
        
        return prompt
    
    def _format_papers_for_prompt(self, papers: List[PaperSegment]) -> str:
        """将论文格式化为 prompt 文本"""
        lines = []
        for i, paper in enumerate(papers, 1):
            meta = paper.metadata
            line = "[{}] {} ({}). {}".format(
                i,
                ", ".join(meta.authors[:3]) + (" et al." if len(meta.authors) > 3 else ""),
                meta.publication_date.year if meta.publication_date else "n.d.",
                meta.title
            )
            if meta.journal:
                line += ". {}".format(meta.journal)
            if meta.doi:
                line += ". DOI: {}".format(meta.doi)
            
            # 添加摘要摘要（如果有翻译）
            if paper.translated_abstract:
                abstract_preview = paper.translated_abstract[:200]
                line += "\n   Abstract: {}...".format(abstract_preview)
            elif paper.original_abstract:
                abstract_preview = paper.original_abstract[:200]
                line += "\n   Abstract: {}...".format(abstract_preview)
            
            # 添加优先级信息
            line += "\n   Priority: {:.2f} | Relevance: {:.2f}".format(
                paper.priority_score, paper.metadata.relevance_score
            )
            
            lines.append(line)
        
        return "\n\n".join(lines)
    
    def _merge_with_template(
        self,
        template: str,
        papers: List[PaperSegment],
        research_topic: str
    ) -> str:
        """将论文数据合并到模板中"""
        papers_text = self._format_papers_for_prompt(papers)
        
        # 替换占位符
        result = template.replace("{{RESEARCH_TOPIC}}", research_topic)
        result = result.replace("{{PAPERS_LIST}}", papers_text)
        result = result.replace("{{PAPER_COUNT}}", str(len(papers)))
        result = result.replace("{{GENERATION_DATE}}", datetime.now().strftime("%Y-%m-%d"))
        
        return result
    
    def _call_deep_research(self, prompt: str) -> Dict[str, Any]:
        """
        调用 Deep Research API
        
        Note: Gemini Deep Research 可能需要特殊的 API 调用方式
        这里提供一个基础实现，可能需要根据实际 API 调整
        """
        try:
            from google.genai import types
            
            # 使用更大的模型进行深度研究
            # 如果有 Deep Research 专用 API，在这里替换
            model = "gemini-2.5-pro-exp-03-25"  # 或 "gemini-2.0-flash-thinking-exp-01-21"
            
            logger.info("   Using model: {}".format(model))
            logger.info("   Prompt length: {} chars".format(len(prompt)))
            
            # 配置生成参数
            config = types.GenerateContentConfig(
                temperature=0.7,  # 稍微提高创造性
                max_output_tokens=65536,  # 最大输出
                # 如果支持的话，启用深度研究模式
                # thinking_config=types.ThinkingConfig(thinking_budget=24576)
            )
            
            logger.info("   Generating content (this may take several minutes)...")
            start_time = time.time()
            
            response = self.client.models.generate_content(
                model=model,
                contents=prompt,
                config=config
            )
            
            elapsed = time.time() - start_time
            logger.info("   Generation completed in {:.1f} seconds".format(elapsed))
            
            # 提取结果
            text = response.text if hasattr(response, 'text') else str(response)
            
            return {
                'success': True,
                'content': text,
                'model': model,
                'elapsed_seconds': elapsed,
                'prompt_length': len(prompt),
                'output_length': len(text),
                'generated_at': datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error("Deep Research API call failed: {}".format(str(e)))
            return {
                'success': False,
                'error': str(e),
                'generated_at': datetime.now().isoformat()
            }
    
    def _save_result(self, result: Dict[str, Any], topic: str) -> Path:
        """保存生成结果"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # 保存 Markdown
        md_file = self.output_dir / "thesis_intro_{}.md".format(timestamp)
        md_content = """# PhD Thesis Introduction Draft

**Generated**: {date}
**Topic**: {topic}
**Model**: {model}
**Generation Time**: {elapsed:.1f} seconds

---

{content}
""".format(
            date=result.get('generated_at', 'Unknown'),
            topic=topic,
            model=result.get('model', 'Unknown'),
            elapsed=result.get('elapsed_seconds', 0),
            content=result.get('content', '')
        )
        md_file.write_text(md_content, encoding='utf-8')
        
        # 保存元数据 JSON
        meta_file = self.output_dir / "thesis_intro_{}_meta.json".format(timestamp)
        meta = {
            'topic': topic,
            'model': result.get('model'),
            'elapsed_seconds': result.get('elapsed_seconds'),
            'prompt_length': result.get('prompt_length'),
            'output_length': result.get('output_length'),
            'generated_at': result.get('generated_at'),
            'md_file': str(md_file)
        }
        meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        
        return md_file
    
    def generate_literature_review(
        self,
        papers: List[PaperSegment],
        focus_area: str,
        review_type: str = "systematic"
    ) -> Dict[str, Any]:
        """
        生成文献综述
        
        Args:
            papers: 论文列表
            focus_area: 聚焦领域
            review_type: 综述类型 (systematic/narrative/scoping)
        """
        logger.info("Generating {} literature review for: {}".format(review_type, focus_area))
        
        prompt = self._build_review_prompt(papers, focus_area, review_type)
        result = self._call_deep_research(prompt)
        
        if result.get('success'):
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = self.output_dir / "lit_review_{}_{}.md".format(
                focus_area.replace(" ", "_")[:20], timestamp
            )
            output_file.write_text(result['content'], encoding='utf-8')
            result['output_file'] = str(output_file)
        
        return result
    
    def _build_review_prompt(
        self,
        papers: List[PaperSegment],
        focus_area: str,
        review_type: str
    ) -> str:
        """构建文献综述 prompt"""
        papers_text = self._format_papers_for_prompt(papers[:30])
        
        return """# Task: Generate {type} Literature Review

## Focus Area
{area}

## Review Requirements
1. Synthesize findings across papers
2. Identify research trends
3. Highlight methodological approaches
4. Discuss limitations and gaps
5. Suggest future directions

## Papers for Review
{papers}

## Output
Generate a comprehensive literature review in academic style.
""".format(
            type=review_type.title(),
            area=focus_area,
            papers=papers_text
        )


def run_deep_research_cli(settings: ScholarSettings, papers_file: str):
    """
    CLI 入口点
    
    Args:
        settings: 配置
        papers_file: 论文 JSON 文件路径
    """
    import json
    from pathlib import Path
    
    # 加载论文
    papers_path = Path(papers_file)
    if not papers_path.exists():
        logger.error("Papers file not found: {}".format(papers_file))
        return
    
    with open(papers_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 转换为 PaperSegment 对象
    segments = []
    for seg_data in data.get('segments', []):
        # 简化处理，实际使用时需要完整转换
        meta = PaperMetadata(**seg_data.get('metadata', {}))
        seg = PaperSegment(
            segment_id=seg_data.get('segment_id', 0),
            paper_id=seg_data.get('paper_id', ''),
            metadata=meta,
            original_abstract=seg_data.get('original_abstract', ''),
            translated_abstract=seg_data.get('translated_abstract', ''),
            priority_score=seg_data.get('priority_score', 0.0)
        )
        segments.append(seg)
    
    logger.info("Loaded {} papers from {}".format(len(segments), papers_file))
    
    # 运行 Deep Research
    client = DeepResearchClient(settings)
    result = client.generate_thesis_introduction(
        papers=segments,
        research_topic="EHR Data Mining and Clinical Prediction using Graph Neural Networks and Large Language Models"
    )
    
    if result.get('success'):
        logger.info("\n" + "=" * 60)
        logger.info("SUCCESS! Thesis introduction generated.")
        logger.info("Output file: {}".format(result.get('output_file')))
        logger.info("=" * 60)
    else:
        logger.error("Failed: {}".format(result.get('error')))
