#!/usr/bin/env python3
"""
👁️‍🗨️ 侦察守卫蜂 (Scout-Guard Bee) — 感知+安全一体化

基于改进方案 9.2.4：合并守卫蜂和侦查蜂

设计理由：
- 守卫蜂和侦查蜂在执行链路上紧耦合——每次侦查蜂出报告，守卫蜂必须立即检查
- 拆成两个独立进程反而增加了通信开销和延迟
- 合并后减少一个进程，降低延迟约 5-10ms

功能：
- 采集数据（原侦查蜂职责）
- 立即检查安全（原守卫蜂职责）— 规则引擎 <1ms，几乎零开销
- 如果有紧急事件 → 直接调舞蹈蜂（保留紧急路径）
- 如果正常 → 返回报告给蜂后/哺育蜂

对外隐喻保留：对外讲故事时仍可说"侦查蜂负责看，守卫蜂负责判断"——只是内部实现合并了。
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hive.bee_base import BeeAgent
from hive.event_bus import event_bus
from hive.logger import get_logger

# 导入原有蜜蜂（组合而非继承）
from bees.scout import scout
from bees.guard import guard
from bees.dancer import dancer

logger = get_logger("scout_guard")


class ScoutGuardBee(BeeAgent):
    """👁️‍🗨️ 侦察守卫蜂 — 感知+安全一体化
    
    组合 scout 和 guard 的能力，在一次调用中完成：
    1. 摄像头截图 + VLM 分析
    2. 立即安全检测
    3. 紧急事件直达舞蹈蜂
    """

    def __init__(self):
        super().__init__(name="scout_guard", trigger_type="cron")
        self._scout = scout  # 组合侦查蜂
        self._guard = guard  # 组合守卫蜂
        self._last_emergency = None

    def patrol_and_check(self):
        """执行一次完整的侦察+安全检查（合并流程）
        
        这是 Anthropic 推荐的 Handoff 模式的实现：
        直接函数调用链，不经过事件总线。
        
        Returns:
            dict: {
                "report": VLM报告或降级报告,
                "image_path": 截图路径,
                "env_data": 环境数据,
                "weather": 天气数据,
                "motion_time": 最近运动时间,
                "vlm_skipped": 是否跳过VLM,
                "emergency": 紧急事件（如果有）,
                "emergency_handled": 是否已处理紧急事件,
            } 或 None（摄像头关闭时）
        """
        self._log("info", "👁️‍🗨️ 侦察守卫蜂出动...")
        start_time = datetime.now()
        
        # Step 1: 侦查蜂采集数据
        result = self._scout.patrol()
        self._scout.track_run()
        
        if not result:
            self._log("info", "摄像头休眠/关闭，跳过")
            return None
        
        report = result.get("report", "")
        vlm_skipped = result.get("vlm_skipped", False)
        
        # Step 2: 守卫蜂安全检测（仅在有 VLM 报告时）
        emergency = None
        emergency_handled = False
        
        if not vlm_skipped and report:
            emergency = self._guard.check(report)
            self._guard.track_run()
            
            if emergency:
                self._log("warn", f"🚨 检测到紧急事件: {emergency['keyword']}")
                self._last_emergency = emergency
                
                # 直达舞蹈蜂（紧急路径，绕过蜂后）
                self._guard.handle(emergency)
                emergency_handled = True
            else:
                self._log("info", "✅ 安全检查通过")
        else:
            self._log("info", "VLM跳过，安全检查跳过")
        
        # 发布合并事件
        event_bus.publish({
            "source": "scout_guard",
            "type": "patrol_complete",
            "intensity": "urgent" if emergency else "normal",
            "payload": {
                "report_summary": report[:200] if report else "",
                "has_emergency": bool(emergency),
                "emergency_keyword": emergency["keyword"] if emergency else None,
                "vlm_skipped": vlm_skipped,
                "duration_ms": int((datetime.now() - start_time).total_seconds() * 1000),
            },
        })
        
        # 返回完整结果
        result["emergency"] = emergency
        result["emergency_handled"] = emergency_handled
        
        self.track_run()
        return result

    def patrol_degraded(self):
        """降级巡查 — 仅采集传感器数据，不做 VLM 分析
        
        当 VLM 服务不可用时使用此方法。
        
        Returns:
            dict: 简化的环境数据报告
        """
        self._log("info", "👁️‍🗨️ 降级模式：仅传感器数据")
        
        env_data = self._scout.get_environment()
        weather = self._scout.get_weather()
        door_facts = self._scout.get_recent_door_facts()
        
        report = f"[降级模式] VLM不可用，仅采集传感器数据。\n\n"
        report += f"环境数据：\n"
        for k, v in env_data.items():
            report += f"  - {k}: {v}\n"
        
        if weather:
            report += f"\n天气：{weather['weather']} {weather['current_temp']}°C"
        
        if door_facts:
            report += f"\n\n门锁事实：\n{door_facts}"
        
        # 即使在降级模式下，仍然可以做基于传感器的安全检测
        sensor_alert = self._check_sensor_safety(env_data)
        emergency = None
        if sensor_alert:
            emergency = {
                "keyword": sensor_alert,
                "priority": "🟠",
                "action": "notify",
                "speak_text": "",
                "report": report[:200],
            }
            self._guard.handle(emergency)
        
        self.track_run()
        return {
            "report": report,
            "image_path": None,
            "env_data": env_data,
            "weather": weather,
            "motion_time": None,
            "vlm_skipped": True,
            "degraded": True,
            "emergency": emergency,
            "emergency_handled": bool(emergency),
        }

    def _check_sensor_safety(self, env_data):
        """基于传感器数据的安全检查（不依赖 VLM）
        
        Returns:
            str: 告警信息，None 表示安全
        """
        # 门锁状态
        door_state = env_data.get("门锁", "")
        if door_state == "open":
            hour = datetime.now().hour
            if 0 <= hour < 6:
                return f"凌晨{hour}点门锁处于打开状态"
        
        # 温度极端值
        for room, value in env_data.items():
            if "°C" in str(value):
                try:
                    temp = float(value.split("°C")[0])
                    if temp > 40:
                        return f"{room}温度过高({temp}°C)"
                    if temp < 5:
                        return f"{room}温度过低({temp}°C)"
                except (ValueError, IndexError):
                    pass
        
        return None

    def get_last_emergency(self):
        """获取最近一次紧急事件"""
        return self._last_emergency

    def process(self, event):
        """BeeAgent 接口：处理事件"""
        event_type = event.get("type", "")
        
        if event_type == "patrol_request":
            return self.patrol_and_check()
        elif event_type == "degraded_patrol_request":
            return self.patrol_degraded()
        else:
            self._log("warn", f"未知事件类型: {event_type}")
            return None


# ===== 模块级单例 =====
scout_guard = ScoutGuardBee()


# ===== 便捷函数 =====
def patrol_and_check():
    """执行一次完整的侦察+安全检查"""
    return scout_guard.patrol_and_check()


def patrol_degraded():
    """降级巡查"""
    return scout_guard.patrol_degraded()


# ===== 测试 =====
if __name__ == "__main__":
    print("👁️‍🗨️ 侦察守卫蜂测试")
    print("=" * 50)
    
    # 健康检查
    health = scout_guard.health_check()
    print(f"健康状态: {health}")
    
    # 实际巡查（需要 HA 连接）
    # result = scout_guard.patrol_and_check()
    # print(f"巡查结果: {result}")
