#!/usr/bin/env python3
"""
💃 摇摆舞协议 — 事件总线 (Event Bus)

⚠️ 架构说明（2026-03-22 改进后）：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
事件总线已降级为【审计日志 + 异步联动】用途。

主调度路径（full_patrol）使用【直接函数调用链】模式：
  scout.patrol() → guard.check() → dancer.send_patrol_report()

这是 Anthropic "Building Effective Agents" 推荐的 Handoff 模式：
- 简单、可调试、零序列化开销
- 适合家庭场景 QPS < 1

事件总线保留的价值：
1. 审计日志：所有事件持久化到 events.jsonl，便于排查问题
2. 异步联动：门锁事件、天气预警等非主流程场景
3. 可观测性：蜂蜜报告可以从事件日志统计
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

蜜蜂之间不直接对话，而是通过摇摆舞（事件总线）传递结构化信息。
基于 JSONL 文件持久化 + 内存缓存热查询。

设计选型理由：
- 家庭场景 QPS < 1，不需要高并发
- JSONL 天然可审计（grep 直接排查）
- 零外部依赖（不需要 Redis）
- 持久化（重启不丢事件）
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path

# 默认事件日志路径
_DEFAULT_EVENTS_FILE = Path(__file__).parent.parent / "data" / "events.jsonl"

# 内存缓存大小
_CACHE_SIZE = 200


class EventBus:
    """摇摆舞事件总线"""

    def __init__(self, events_file=None):
        self.events_file = Path(events_file) if events_file else _DEFAULT_EVENTS_FILE
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        self._subscribers = {}  # {event_type: [callback, ...]}
        self._cache = []  # 最近 N 条事件的内存缓存
        self._lock = threading.Lock()

        # 启动时加载最近事件到缓存
        self._load_cache()

    def _load_cache(self):
        """启动时从文件加载最近事件"""
        if not self.events_file.exists():
            return
        try:
            lines = self.events_file.read_text().strip().split("\n")
            recent = lines[-_CACHE_SIZE:] if len(lines) > _CACHE_SIZE else lines
            for line in recent:
                if line.strip():
                    try:
                        self._cache.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except (json.JSONDecodeError, ValueError, KeyError):
            pass

    def publish(self, event):
        """发布事件（摇摆舞）

        Args:
            event: dict，必须包含 type 字段。
                   会自动添加 dance, timestamp 等元数据。
        """
        # 补充元数据
        if "dance" not in event:
            event["dance"] = "waggle"
        if "timestamp" not in event:
            event["timestamp"] = datetime.now().isoformat()
        if "intensity" not in event:
            event["intensity"] = "normal"

        with self._lock:
            # 写入文件
            try:
                with open(self.events_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                print(f"[EventBus] 写入事件失败: {e}")

            # 更新缓存
            self._cache.append(event)
            if len(self._cache) > _CACHE_SIZE:
                self._cache = self._cache[-_CACHE_SIZE:]

        # 通知订阅者（带消费确认 + dead-letter）
        event_type = event.get("type", "")
        callbacks = self._subscribers.get(event_type, []) + self._subscribers.get("*", [])
        for cb in callbacks:
            try:
                cb(event)
            except Exception as e:
                # Dead-letter: 记录失败的消费
                self._dead_letter(event, cb, e)

    def _dead_letter(self, event, callback, error):
        """记录消费失败的事件（dead-letter queue）"""
        dl_entry = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event.get("type", "unknown"),
            "event_ts": event.get("timestamp", ""),
            "subscriber": getattr(callback, "__name__", str(callback)),
            "error": str(error)[:200],
        }
        dl_file = self.events_file.parent / "dead_letters.jsonl"
        try:
            with open(dl_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(dl_entry, ensure_ascii=False) + "\n")
        except (OSError, IOError):
            pass
        print(f"[EventBus] ⚠️ 订阅者 {dl_entry['subscriber']} 消费失败: {error}")

    def subscribe(self, event_type, callback):
        """订阅事件

        Args:
            event_type: 事件类型字符串，"*" 表示订阅所有
            callback: 回调函数 callback(event)
        """
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)

    def get_recent(self, n=100, event_type=None):
        """获取最近 n 条事件

        Args:
            n: 数量
            event_type: 可选过滤
        """
        with self._lock:
            events = self._cache[-n:] if not event_type else [
                e for e in self._cache if e.get("type") == event_type
            ][-n:]
        return events

    def get_by_source(self, source_name, n=50):
        """获取某只蜜蜂发出的最近事件"""
        with self._lock:
            return [e for e in self._cache if e.get("source") == source_name][-n:]

    def get_by_dancer(self, dancer_name, n=50):
        """向后兼容：获取某只蜜蜂发出的最近事件"""
        return self.get_by_source(dancer_name, n)

    def clear_old(self, keep_days=7):
        """清理超过指定天数的旧事件，委托给 rotate.py"""
        try:
            from hive.rotate import rotate_jsonl
            before, after = rotate_jsonl(self.events_file, keep_days)
            # 重新加载缓存
            if before != after:
                with self._lock:
                    self._cache.clear()
                self._load_cache()
            return before - after
        except Exception:
            return 0


# 全局单例
event_bus = EventBus()
