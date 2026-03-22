#!/usr/bin/env python3
"""
🔒 安全文件 I/O

解决两个问题：
1. 非原子写入 → 写临时文件再 os.replace()
2. 并发写冲突 → 独立 .lock 文件作为锁标的（os.replace 后锁仍有效）
"""

import fcntl
import json
import os
import tempfile
from pathlib import Path


def _lock_path(path):
    """返回对应的 .lock 文件路径"""
    return Path(str(path) + ".lock")


def safe_write_json(path, data, indent=2):
    """原子写入 JSON，带文件锁（独立 .lock 文件）"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = _lock_path(path)

    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with open(lock_file, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, ensure_ascii=False, indent=indent)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(path))
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except (json.JSONDecodeError, ValueError, KeyError):
        # 清理临时文件
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def safe_read_json(path, default=None):
    """安全读取 JSON，损坏时返回 default"""
    path = Path(path)
    if not path.exists():
        return default if default is not None else {}
    lock_file = _lock_path(path)
    try:
        with open(lock_file, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_SH)
            try:
                with open(path) as f:
                    data = json.load(f)
                    return data
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except (json.JSONDecodeError, IOError):
        return default if default is not None else {}


def safe_append_jsonl(path, entry):
    """原子追加 JSONL 一行，带文件锁（独立 .lock 文件）"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = _lock_path(path)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(lock_file, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            with open(path, "a") as f:
                f.write(line)
                f.flush()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
