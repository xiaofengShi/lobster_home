#!/usr/bin/env python3
"""
🏥 蜂巢健康监控 (Hive Health)

跟踪每只蜜蜂的状态，提供整体蜂巢健康视图。

8.1 蜜蜂生命周期管理：
- 孵化规则：独立职责 + 独立节奏 + 标准接口
- 休眠规则：30天未触发 → 自动休眠
- 淘汰规则：高成本低价值 / 功能重叠 → 退役
"""

import json
from datetime import datetime, timedelta
from pathlib import Path


# 蜂巢生命周期数据文件
_LIFECYCLE_FILE = Path(__file__).parent.parent / "data" / "bee_lifecycle.json"


class HiveHealth:
    """蜂巢健康监控 + 蜜蜂生命周期管理"""

    def __init__(self):
        self._bees = {}  # {name: bee_instance}
        self._lifecycle = self._load_lifecycle()

    def _load_lifecycle(self):
        """加载蜜蜂生命周期数据"""
        if _LIFECYCLE_FILE.exists():
            try:
                return json.loads(_LIFECYCLE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_lifecycle(self):
        """持久化生命周期数据"""
        try:
            _LIFECYCLE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _LIFECYCLE_FILE.write_text(
                json.dumps(self._lifecycle, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except OSError:
            pass

    def register_bee(self, bee):
        """注册蜜蜂"""
        self._bees[bee.name] = bee
        if bee.name not in self._lifecycle:
            self._lifecycle[bee.name] = {
                "hatched": datetime.now().isoformat(),
                "last_active": None,
                "status": "active",
                "dormant_since": None,
                "daily_cost_estimate": 0.0,
            }
            self._save_lifecycle()

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

    # ====================================================================
    # 🥚 8.1 蜜蜂孵化/淘汰规则
    # ====================================================================

    def can_hatch(self, name, responsibilities, trigger_type, overlaps_with=None):
        """检查新蜜蜂是否满足孵化三条件
        
        孵化规则（文档 8.1）：
        1. 独立职责：不能跟现有蜜蜂职责重叠
        2. 独立节奏：必须有自己的触发方式和频率
        3. 标准化接口：必须通过事件总线/函数调用通信
        
        Args:
            name: 新蜜蜂名称
            responsibilities: 职责描述列表
            trigger_type: 触发方式 (cron/event/request)
            overlaps_with: 如果跟某只现有蜜蜂有功能重叠，指定名称
            
        Returns:
            (bool, str): (是否允许孵化, 原因)
        """
        # 条件1：名称不重复
        if name in self._bees:
            return False, f"蜜蜂 '{name}' 已存在，不能重复孵化"
        
        # 条件2：不能与现有蜜蜂功能完全重叠
        if overlaps_with and overlaps_with in self._bees:
            return False, (f"与 '{overlaps_with}' 职责重叠，"
                          f"应增强现有蜜蜂而非新建。"
                          f"违反孵化规则第1条：独立职责")
        
        # 条件3：必须有触发方式
        if trigger_type not in ("cron", "event", "request"):
            return False, f"触发方式 '{trigger_type}' 无效，必须是 cron/event/request"
        
        # 条件4：检查蜂巢容量（合理上限）
        if len(self._bees) >= 10:
            return False, "蜂巢已有10只蜜蜂，超出合理上限。考虑合并现有蜜蜂"
        
        return True, f"✅ 满足孵化条件：职责={responsibilities}, 触发={trigger_type}"

    def check_dormancy(self, days_threshold=30):
        """检查哪些蜜蜂应该休眠
        
        淘汰机制（文档 8.1）：
        - 连续 N 天未触发 → 标记为休眠蜜蜂，不占资源
        
        Args:
            days_threshold: 多少天无活动后进入休眠
            
        Returns:
            list: 应该休眠的蜜蜂列表
        """
        now = datetime.now()
        should_sleep = []
        
        for name, bee in self._bees.items():
            if bee.state == "retired" or bee.state == "sleeping":
                continue
            
            # 核心蜜蜂永不休眠
            if name in ("scout", "guard", "dancer", "nurse", "builder"):
                continue
            
            last_run = bee.last_run
            if last_run:
                try:
                    last_dt = datetime.fromisoformat(last_run)
                    days_idle = (now - last_dt).days
                    if days_idle >= days_threshold:
                        should_sleep.append({
                            "name": name,
                            "days_idle": days_idle,
                            "last_run": last_run,
                        })
                except (ValueError, TypeError):
                    pass
            elif name in self._lifecycle:
                # 从未运行过
                hatched = self._lifecycle[name].get("hatched", "")
                if hatched:
                    try:
                        hatched_dt = datetime.fromisoformat(hatched)
                        days_since = (now - hatched_dt).days
                        if days_since >= days_threshold:
                            should_sleep.append({
                                "name": name,
                                "days_idle": days_since,
                                "last_run": "从未运行",
                            })
                    except (ValueError, TypeError):
                        pass
        
        return should_sleep

    def evaluate_bee(self, name, daily_cost=0.0, user_satisfaction="unknown"):
        """评估蜜蜂价值（用于淘汰决策）
        
        淘汰规则（文档 8.1）：
        - 高成本低价值：日成本 > ¥1 但用户满意度低 → 降级或合并
        - 功能重叠：两只蜜蜂干同一件事 → 保强去弱
        
        Args:
            name: 蜜蜂名称
            daily_cost: 预估日成本（元）
            user_satisfaction: "high" / "medium" / "low" / "unknown"
            
        Returns:
            dict: 评估结果
        """
        bee = self._bees.get(name)
        if not bee:
            return {"recommendation": "not_found"}
        
        health = bee.health_check()
        result = {
            "name": name,
            "daily_cost": daily_cost,
            "user_satisfaction": user_satisfaction,
            "total_runs": health.get("total_runs", 0),
            "error_rate": (health.get("error_count", 0) / max(health.get("total_runs", 1), 1)),
            "recommendation": "keep",
            "reasons": [],
        }
        
        # 高成本低价值
        if daily_cost > 1.0 and user_satisfaction == "low":
            result["recommendation"] = "retire_or_merge"
            result["reasons"].append(f"日成本¥{daily_cost:.2f}>¥1 但满意度低")
        
        # 高错误率
        if result["error_rate"] > 0.3:
            result["recommendation"] = "fix_or_retire"
            result["reasons"].append(f"错误率{result['error_rate']:.0%}过高")
        
        # 从未运行
        if health.get("total_runs", 0) == 0:
            result["reasons"].append("从未运行过，考虑是否需要")
        
        # 更新生命周期
        if name in self._lifecycle:
            self._lifecycle[name]["daily_cost_estimate"] = daily_cost
            self._save_lifecycle()
        
        return result

    def lifecycle_report(self):
        """蜜蜂生命周期报告"""
        lines = ["🥚 蜜蜂生命周期报告\n"]
        
        for name, bee in self._bees.items():
            health = bee.health_check()
            lc = self._lifecycle.get(name, {})
            hatched = lc.get("hatched", "未知")[:10]
            cost = lc.get("daily_cost_estimate", 0)
            
            status_emoji = {"ready": "🟢", "running": "🔵", "sleeping": "💤", "retired": "⚫"}.get(
                health.get("state", ""), "❓"
            )
            
            lines.append(
                f"{status_emoji} **{name}** | 孵化:{hatched} | "
                f"运行:{health.get('total_runs', 0)}次 | "
                f"错误:{health.get('error_count', 0)}次 | "
                f"日成本:¥{cost:.2f}"
            )
        
        # 休眠建议
        sleepy = self.check_dormancy()
        if sleepy:
            lines.append("\n💤 建议休眠的蜜蜂：")
            for s in sleepy:
                lines.append(f"  - {s['name']}: {s['days_idle']}天无活动")
        
        return "\n".join(lines)


# 全局单例
hive_health = HiveHealth()
