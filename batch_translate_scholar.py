# -*- coding: utf-8 -*-
"""
Scholar Batch Processor
独立脚本：读取已生成的 Scholar JSON 文件，利用 Gemini 批量翻译和归纳
"""
import os
import json
import argparse
import time
import logging
from pathlib import Path
from tqdm import tqdm
from datetime import datetime

# 设置简单的日志
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# 尝试导入 google-genai
try:
    from google import genai
    from google.genai import types
except ImportError:
    print("错误: 请先安装 google-genai: pip install google-genai")
    exit(1)

def build_prompt(batch_papers):
    """构建批量处理的 prompt"""
    papers_json = []
    for p in batch_papers:
        papers_json.append({
            "id": p.get('segment_id'),
            "title": p['metadata'].get('title'),
            "abstract": p.get('original_abstract', '')[:1500]
        })
    
    prompt = """你是一位医学人工智能领域的专家。请对以下论文批量进行中文翻译和摘要归纳。

## 任务要求
1. **中文标题**：准确翻译标题。
2. **核心归纳**：用 2-3 句话总结研究的核心贡献、方法和结论。
3. **关键词提取**：提取 3-5 个中文关键词。

## 输出格式 (严格 JSON 数组)
```json
[
  {
    "id": 1,
    "translated_title": "...",
    "summary": "...",
    "keywords": ["...", "..."]
  }
]
```

## 输入数据
```json
""" + json.dumps(papers_json, ensure_ascii=False, indent=2) + """
```
"""
    return prompt

def process_file(json_path, api_key, model_name="gemini-2.0-flash"):
    """处理 JSON 文件"""
    json_path = Path(json_path)
    if not json_path.exists():
        logger.error(f"文件不存在: {json_path}")
        return

    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    segments = data.get('segments', [])
    total = len(segments)
    logger.info(f"Loaded {total} papers from {json_path}")

    client = genai.Client(api_key=api_key)
    
    batch_size = 5
    results = {}
    
    # 进度条
    for i in tqdm(range(0, total, batch_size), desc="Processing Batches"):
        batch = segments[i:i+batch_size]
        prompt = build_prompt(batch)
        
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type="application/json"
                )
            )
            
            # 解析相应
            text = response.text
            # 清理可能的 markdown 标记
            if '```json' in text:
                text = text.split('```json')[1].split('```')[0]
            elif '```' in text:
                text = text.split('```')[1].split('```')[0]
                
            batch_results = json.loads(text.strip())
            for res in batch_results:
                results[res['id']] = res
                
        except Exception as e:
            logger.error(f"Error processing batch {i//batch_size}: {e}")
            # 等待一下再继续
            time.sleep(2)
            continue
            
    # 生成 Markdown 输出
    output_md_path = json_path.parent / f"{json_path.stem}_翻译归纳.md"
    
    markdown_lines = [
        f"# Scholar Digest 翻译归纳 - {datetime.now().strftime('%Y-%m-%d')}",
        f"\n**原始文件**: {json_path.name}",
        f"**总篇数**: {total}",
        "\n---\n"
    ]
    
    for seg in segments:
        res = results.get(seg['segment_id'], {})
        meta = seg['metadata']
        
        markdown_lines.append(f"### {meta.get('title')}")
        if res.get('translated_title'):
            markdown_lines.append(f"**中文标题**: {res['translated_title']}")
        
        markdown_lines.append(f"\n**作者**: {', '.join(meta.get('authors', [])[:3])}")
        markdown_lines.append(f"**期刊**: {meta.get('journal', 'N/A')}")
        
        if res.get('keywords'):
            markdown_lines.append(f"**关键词**: {', '.join(res['keywords'])}")
        
        markdown_lines.append("\n#### 翻译归纳")
        if res.get('summary'):
            markdown_lines.append(res['summary'])
        else:
            markdown_lines.append("*翻译/归纳失败*")
            
        markdown_lines.append("\n#### 英文摘要")
        markdown_lines.append(f"> {seg.get('original_abstract', 'No abstract')[:500]}...")
        markdown_lines.append("\n---\n")

    with open(output_md_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(markdown_lines))
    
    logger.info(f"Markdown output saved to: {output_md_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch translate scholar JSON results")
    parser.add_argument("--json", type=str, required=True, help="Path to the scholar JSON file")
    parser.add_argument("--key", type=str, help="Gemini API Key")
    parser.add_argument("--model", type=str, default="gemini-2.0-flash", help="Gemini model name")
    
    args = parser.parse_args()
    
    # 尝试多种方式获取 API Key
    api_key = args.key
    if not api_key:
        # 1. 尝试从环境变量获取
        api_key = os.environ.get("GEMINI_API_KEY")
    
    if not api_key:
        # 2. 尝试从 config/scholar.env 获取
        scholar_env = Path("config/scholar.env")
        if scholar_env.exists():
            with open(scholar_env, 'r', encoding='utf-8') as f:
                for line in f:
                    # 兼容嵌套命名（LLM__GEMINI_API_KEY，模板现行写法）与旧平铺命名
                    if line.startswith(("LLM__GEMINI_API_KEY=", "GEMINI_API_KEY=")):
                        api_key = line.split("=", 1)[1].strip()
                        break
    
    if not api_key:
        print("错误: 未找到 API Key。")
        print("请在 config/scholar.env 中设置，或通过 --key 提供，或设置 GEMINI_API_KEY 环境变量。")
        exit(1)
        
    process_file(args.json, api_key, args.model)
