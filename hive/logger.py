#!/usr/bin/env python3
"""
📋 蜂巢统一日志

替换所有 print()，提供：
- 日志级别（DEBUG/INFO/WARN/ERROR）
- 文件输出（自动按天轮转）
- 结构化格式：[时间] [蜜蜂名] [级别] 消息
- BeeAgent._log() 自动接入
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler

from hive.config import DATA_DIR

LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ===== 自定义格式 =====
class HiveFormatter(logging.Formatter):
    """[2026-03-21 16:00:00] [dancer] [info] 消息"""

    LEVEL_MAP = {
        "DEBUG": "debug",
        "INFO": "info",
        "WARNING": "warn",
        "ERROR": "error",
        "CRITICAL": "fatal",
    }

    def format(self, record):
        ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        level = self.LEVEL_MAP.get(record.levelname, record.levelname.lower())
        name = record.name
        return f"[{ts}] [{name}] [{level}] {record.getMessage()}"


# ===== 工厂函数 =====
_loggers = {}

def get_logger(name, level=None):
    """获取指定蜜蜂的 logger

    Args:
        name: 蜜蜂名（dancer/scout/nurse/guard/builder/queen）
        level: 日志级别，默认 INFO

    Returns:
        logging.Logger
    """
    if name in _loggers:
        return _loggers[name]

    if level is None:
        level_str = os.getenv("HIVE_LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_str, logging.INFO)

    logger = logging.getLogger(f"hive.{name}")
    logger.setLevel(level)
    logger.propagate = False

    # 控制台输出
    if not logger.handlers:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(level)
        console.setFormatter(HiveFormatter())
        logger.addHandler(console)

        # 文件输出（按天轮转，保留7天）
        log_file = LOG_DIR / "hive.log"
        file_handler = TimedRotatingFileHandler(
            str(log_file), when="midnight", backupCount=7,
            encoding="utf-8"
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(HiveFormatter())
        logger.addHandler(file_handler)

    _loggers[name] = logger
    return logger
