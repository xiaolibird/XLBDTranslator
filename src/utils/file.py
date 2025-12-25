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


def clean_filename(filename: str) -> str:
    """清理文件名，去除特殊字符"""
    return re.sub(r'[\\/*?:"<>|]', "", filename).replace(" ", "_")


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
    project_name: str, 
    output_base_dir: str
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


def get_last_checkpoint_id(md_path: Path) -> int:
    """读取 Markdown 文件，找到最后一个已完成的 Segment ID"""
    if not md_path.exists():
        return -1

    try:
        content = md_path.read_text(encoding='utf-8')

        # 尝试匹配新格式
        ids = re.findall(r'🔖 \*\*Segment (\d+)\*\*', content)
        if not ids:
            # 尝试匹配旧格式
            ids = re.findall(r'### Segment (\d+)', content)

        return int(ids[-1]) if ids else -1

    except Exception:
        return -1


def recover_context_from_file(md_path: Path, max_chars: int = 2000) -> str:
    """从文件末尾恢复上下文"""
    if not md_path.exists():
        return ""

    try:
        with open(md_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()

            if file_size == 0:
                return ""

            read_size = min(file_size, max_chars + 512)
            f.seek(-read_size, os.SEEK_END)

            tail_bytes = f.read(read_size)
            tail_text = tail_bytes.decode('utf-8', errors='ignore')

        return tail_text[-max_chars:]

    except Exception:
        return ""


def save_segments_cache(cache_path: Path, segments: list) -> None:
    """保存片段缓存"""
    import json

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = [seg.model_dump() for seg in segments]
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Failed to save cache: {e}")


def load_segments_cache(cache_path: Path) -> Optional[list]:
    """加载片段缓存"""
    import json
    from ..core.schema import ContentSegment

    if not cache_path.exists():
        return None

    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            raw_data = json.load(f)
            return [ContentSegment(**item) for item in raw_data]
    except Exception:
        return None
