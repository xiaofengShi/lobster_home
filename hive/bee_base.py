#!/usr/bin/env python3
"""
🐝 BeeAgent — 蜜蜂基类

所有蜜蜂的父类，定义统一的生命周期和接口。
蜜蜂是 LobsterHive 的基本工作单元，每只蜜蜂有独立的职责、节奏和状态。
"""

from datetime import datetime


class BeeAgent:
    """蜜蜂基类 — 所有蜜蜂继承此类"""

    def __init__(self, name, trigger_type="event"):
        """
        Args:
            name: 蜜蜂代号 (scout/guard/nurse/dancer/builder)
            trigger_type: 触发方式 (cron/event/request)
        """
        self.name = name
        self.trigger_type = trigger_type
        self.state = "ready"  # ready / running / sleeping / retired
        self.last_run = None
        self.last_run_duration = None
        self.error_count = 0
        self.consecutive_errors = 0
        self.total_runs = 0

    def on_event(self, event):
        """事件驱动入口 — 所有蜜蜂通过此方法接收事件"""
        if self.state == "retired":
            return None
        if not self.should_handle(event):
            return None

        self.state = "running"
        start = datetime.now()
        try:
            result = self.process(event)
            self.consecutive_errors = 0
            self.total_runs += 1
            return result
        except Exception as e:
            self.error_count += 1
            self.consecutive_errors += 1
            self._log("error", f"处理失败: {e}")
            raise
        finally:
            self.state = "ready"
            self.last_run = datetime.now().isoformat()
            self.last_run_duration = (datetime.now() - start).total_seconds()

    def should_handle(self, event):
        """是否应该处理此事件（子类可覆盖）"""
        return True

    def process(self, event):
        """处理事件（子类必须实现）"""
        raise NotImplementedError(f"{self.name} 蜜蜂未实现 process()")

    def health_check(self):
        """蜂巢定期检查蜜蜂健康"""
        return {
            "name": self.name,
            "state": self.state,
            "trigger_type": self.trigger_type,
            "last_run": self.last_run,
            "last_run_duration": self.last_run_duration,
            "total_runs": self.total_runs,
            "error_count": self.error_count,
            "consecutive_errors": self.consecutive_errors,
            "ok": self.consecutive_errors < 3,
        }

    def retire(self):
        """退役"""
        self.state = "retired"
        self._log("info", "已退役")

    def wake(self):
        """从休眠唤醒"""
        if self.state == "sleeping":
            self.state = "ready"
            self._log("info", "已唤醒")

    def sleep(self):
        """休眠"""
        self.state = "sleeping"
        self._log("info", "已休眠")

    def track_run(self):
        """标记一次运行（直接调用时使用，on_event 会自动跟踪）"""
        from datetime import datetime
        self.total_runs += 1
        self.last_run = datetime.now().isoformat()

    def _log(self, level, message):
        """统一日志 — 使用 hive.logger"""
        from hive.logger import get_logger
        logger = get_logger(self.name)
        log_method = getattr(logger, level if level != "warn" else "warning", logger.info)
        log_method(message)

    def __repr__(self):
        return f"<{self.__class__.__name__} name={self.name} state={self.state}>"
