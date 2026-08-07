#!/usr/bin/env python3
"""第二步精筛：用 Ollama 读每篇 title+abstract，判断是否涉及 EHR/多机构/因果/missingness。

对 narrow_keyword_hits.json 中的每篇论文，LLM 输出一个四维标记：
  {ehr, multi_site, causal, missingness}
每维 0/1，总分≥2 → 保留。

批量送审（10 篇/批），输出精筛结果。
"""
import json, sys, time
from pathlib import Path
from typing import List, Dict, Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.scholar.llm_client import LLMClient
from src.scholar.schema import ScholarSettings, LLMSettings

BATCH_SIZE = 10
MIN_SCORE = 2  # 至少命中 2 个维度

PROMPT_TEMPLATE = """You are screening papers for a research project on:
- EHR (electronic health records, tabular clinical data)
- Multi-institutional / multi-site / cross-site studies
- Causal inference (causal discovery, causal effects, causal structure)
- Missing data / missingness (MNAR, MAR, imputation, missing mechanisms)

For each paper below (title + abstract), output a JSON array. Each element:
{"id": <number>, "ehr": 0|1, "multi_site": 0|1, "causal": 0|1, "missingness": 0|1, "brief": "<10-word why>"}

RULES:
- ehr=1: paper uses tabular EHR/EMR data (not just images, wearables, genomics, NLP on notes)
- multi_site=1: paper uses data from ≥2 hospitals/sites/databases, or studies cross-site generalization
- causal=1: paper does causal inference/discovery (not just correlation/prediction)
- missingness=1: paper addresses missing data mechanism, imputation methodology, or missingness-aware modeling
- Be conservative: only mark 1 if the abstract CLEARLY indicates that dimension
- Output ONLY the JSON array, no markdown, no explanation

Papers:
```json
{{PAPERS_JSON}}
```"""


def build_prompt(batch: List[Dict]) -> str:
    papers = []
    for it in batch:
        papers.append({
            "id": it["_id"],
            "title": it.get("title", ""),
            "abstract": (it.get("abstract") or "")[:600],
        })
    return PROMPT_TEMPLATE.replace("{{PAPERS_JSON}}", json.dumps(papers, ensure_ascii=False))


def parse_response(response: str) -> List[Dict]:
    text = response.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    return json.loads(text)


def main():
    input_path = Path("output/journal_screen/narrow_keyword_hits.json")
    papers = json.loads(input_path.read_text(encoding="utf-8"))
    for i, p in enumerate(papers):
        p["_id"] = i + 1

    total = len(papers)
    batches = [papers[i:i+BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    print(f"论文总数: {total}, 批次: {len(batches)} (每批 {BATCH_SIZE} 篇)")
    print(f"LLM: ollama / qwen3.5:35b, 维度阈值: ≥{MIN_SCORE}")
    print()

    settings = ScholarSettings()
    # 强制用 ollama
    settings.llm.provider = "ollama"
    settings.llm.model = "qwen3.5:35b"
    settings.llm.fallback_providers = []

    client = LLMClient(settings.llm)
    kept, excluded = [], []
    stats = {"ehr": 0, "multi_site": 0, "causal": 0, "missingness": 0}

    try:
        for bi, batch in enumerate(batches):
            prompt = build_prompt(batch)
            batch_num = bi + 1
            sys.stdout.write(f"\r批次 {batch_num}/{len(batches)}...")
            sys.stdout.flush()

            try:
                response = client.call(prompt, model="qwen3.5:35b")
                verdicts = parse_response(response)
            except Exception as e:
                print(f"\n  批次 {batch_num} 失败: {e}, 跳过")
                continue

            for paper in batch:
                v = next((v for v in verdicts if v.get("id") == paper["_id"]), None)
                if v is None:
                    continue
                score = (int(v.get("ehr", 0)) + int(v.get("multi_site", 0)) +
                         int(v.get("causal", 0)) + int(v.get("missingness", 0)))
                paper["_verdict"] = v
                paper["_score"] = score
                if score >= MIN_SCORE:
                    kept.append(paper)
                    for dim in ["ehr", "multi_site", "causal", "missingness"]:
                        if v.get(dim):
                            stats[dim] += 1
                else:
                    excluded.append(paper)

            time.sleep(0.3)  # breathe

    finally:
        client.close()

    print(f"\n\n=== 精筛完成 ===")
    print(f"输入: {total} 篇")
    print(f"保留: {len(kept)} 篇 (≥{MIN_SCORE} 维)")
    print(f"排除: {len(excluded)} 篇")
    print(f"维度分布: {stats}")

    # 按 score 降序排列
    kept.sort(key=lambda p: (-p["_score"], p.get("title", "")))

    out = {
        "filter": "ollama-4dim",
        "min_score": MIN_SCORE,
        "total_input": total,
        "kept": len(kept),
        "excluded": len(excluded),
        "dimension_stats": stats,
        "papers": kept,
    }
    out_path = Path("output/journal_screen/fine_screened.json")
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n结果已保存: {out_path}")

    # 打印 top 预览
    print("\n=== Top 20 候选 ===")
    for i, p in enumerate(kept[:20]):
        v = p["_verdict"]
        dims = "".join([
            "E" if v.get("ehr") else " ",
            "M" if v.get("multi_site") else " ",
            "C" if v.get("causal") else " ",
            "X" if v.get("missingness") else " ",
        ])
        print(f"  {i+1:2d}. [{p['_score']}][{dims}] {p['title'][:80]}")
        print(f"       {v.get('brief', '')[:60]}")


if __name__ == "__main__":
    main()
