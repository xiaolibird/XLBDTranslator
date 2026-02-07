# -*- coding: utf-8 -*-
"""
Scholar Digest 模块
用于处理 Google Scholar 邮件提醒，提取论文信息并生成摘要
"""

from .schema import (
    ScholarSettings,
    PaperMetadata,
    PaperSegment,
    PaperSegmentList,
    EmailMetadata,
    DigestOutput,
)
from .gmail_client import GmailClient
from .paper_extractor import ScholarEmailParser
from .workflow import ScholarWorkflow
from .deep_research import DeepResearchClient

__all__ = [
    # Schema
    "ScholarSettings",
    "PaperMetadata",
    "PaperSegment",
    "PaperSegmentList",
    "EmailMetadata",
    "DigestOutput",
    # Components
    "GmailClient",
    "ScholarEmailParser",
    "ScholarWorkflow",
    "DeepResearchClient",
]
