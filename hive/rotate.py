#!/usr/bin/env python3
"""
🔄 JSONL 文件轮转 — 防止日志无限增长

保留最近 N 天的数据，旧数据自动清理。
由 queen.py 每天调用一次（晚间收尾时）。
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from hive.config import DATA_DIR
from hive.logger import get_logger

logger = get_logger("rotate")

# 需要轮转的 JSONL 文件及保留天数
ROTATION_CONFIG = {
    "events.jsonl": 7,
    "family_activity_log.jsonl": 7,
    "notification_tracker.jsonl": 14,
    "dead_letters.jsonl": 30,
}


def rotate_jsonl(filepath, keep_days=7):
    """轮转单个 JSONL 文件，只保留最近 N 天的记录
    
    Args:
        filepath: JSONL 文件路径
        keep_days: 保留天数
        
    Returns:
        tuple: (原行数, 保留行数)
    """
    filepath = Path(filepath)
    if not filepath.exists():
        return 0, 0
    
    cutoff = datetime.now() - timedelta(days=keep_days)
    cutoff_str = cutoff.isoformat()
    
    original_lines = 0
    kept_lines = []
    
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                original_lines += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    # 尝试多种时间戳字段名
                    ts = record.get("timestamp") or record.get("ts") or record.get("time") or ""
                    if ts and ts >= cutoff_str:
                        kept_lines.append(line)
                    elif not ts:
                        # 没有时间戳的记录保留
                        kept_lines.append(line)
                except json.JSONDecodeError:
                    # 格式错误的行跳过
                    pass
        
        if len(kept_lines) < original_lines:
            # 原子写入
            tmp = filepath.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for line in kept_lines:
                    f.write(line + "\n")
            tmp.replace(filepath)
            
            removed = original_lines - len(kept_lines)
            logger.info(f"轮转 {filepath.name}: {original_lines} → {len(kept_lines)} (清理{removed}行)")
        
        return original_lines, len(kept_lines)
    
    except (OSError, IOError) as e:
        logger.error(f"轮转失败 {filepath.name}: {e}")
        return original_lines, original_lines


def rotate_all():
    """轮转所有配置的 JSONL 文件
    
    Returns:
        dict: {filename: (before, after)}
    """
    results = {}
    for filename, keep_days in ROTATION_CONFIG.items():
        filepath = DATA_DIR / filename
        before, after = rotate_jsonl(filepath, keep_days)
        if before > 0:
            results[filename] = (before, after)
    
    # 清理旧日志文件
    _rotate_log_files()
    
    return results


def _rotate_log_files():
    """清理超过7天的日志文件"""
    log_dir = DATA_DIR / "logs"
    if not log_dir.exists():
        return
    
    cutoff = datetime.now() - timedelta(days=7)
    for f in log_dir.iterdir():
        if f.is_file():
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime < cutoff:
                    f.unlink()
                    logger.info(f"清理旧日志: {f.name}")
            except (OSError, IOError):
                pass
