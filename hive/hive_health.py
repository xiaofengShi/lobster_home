#!/usr/bin/env python3
"""
🏥 蜂巢健康监控 (Hive Health)

跟踪每只蜜蜂的状态，提供整体蜂巢健康视图。
"""

from datetime import datetime


class HiveHealth:
    """蜂巢健康监控"""

    def __init__(self):
        self._bees = {}  # {name: bee_instance}

    def register_bee(self, bee):
        """注册蜜蜂"""
        self._bees[bee.name] = bee

    def unregister_bee(self, name):
        """注销蜜蜂"""
        self._bees.pop(name, None)

    def get_bee(self, name):
        """获取蜜蜂实例"""
        return self._bees.get(name)

    def get_hive_status(self):
        """获取蜂巢整体状态"""
        statuses = {}
        all_ok = True
        for name, bee in self._bees.items():
            health = bee.health_check()
            statuses[name] = health
            if not health.get("ok"):
                all_ok = False

        return {
            "timestamp": datetime.now().isoformat(),
            "total_bees": len(self._bees),
            "all_ok": all_ok,
            "bees": statuses,
        }

    def check_all(self):
        """检查所有蜜蜂，返回异常列表"""
        issues = []
        for name, bee in self._bees.items():
            health = bee.health_check()
            if not health.get("ok"):
                issues.append({
                    "bee": name,
                    "state": health.get("state"),
                    "consecutive_errors": health.get("consecutive_errors"),
                    "last_run": health.get("last_run"),
                })
        return issues

    def summary(self):
        """简洁的蜂巢摘要"""
        status = self.get_hive_status()
        lines = [f"🐝 蜂巢状态: {'✅ 全部正常' if status['all_ok'] else '⚠️ 有异常'}"]
        lines.append(f"   蜜蜂数量: {status['total_bees']}")
        for name, info in status["bees"].items():
            emoji = "✅" if info["ok"] else "❌"
            lines.append(f"   {emoji} {name}: {info['state']} (跑了{info['total_runs']}次)")
        return "\n".join(lines)


# 全局单例
hive_health = HiveHealth()
