"""
文件操作工具
"""
import os
import re
from pathlib import Path
from typing import Optional
from datetime import datetime
import hashlib

from ..core.schema import Settings


def get_file_hash(file_path: Path, algorithm: str = 'md5') -> str:
    """
    计算文件的哈希值 (MD5 or SHA256).
    """
    hash_func = hashlib.new(algorithm)
    with open(file_path, 'rb') as f:
        #逐块读取以处理大文件
        for chunk in iter(lambda: f.read(4096), b""):
            hash_func.update(chunk)
    return hash_func.hexdigest()


def create_output_directory(
    output_base_dir: str,
    project_name: str
) -> Path:
    """
    基于项目唯一标识 (如 MD5-hash) 创建并返回项目目录。
    """
    # 确保基础输出目录存在
    base_dir = Path(output_base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    
    # 创建项目目录
    project_dir = base_dir / project_name
    project_dir.mkdir(parents=True, exist_ok=True)
    
    return project_dir

