#!/usr/bin/env python3
"""
🎯 置信度评估 — 三级自动化机制

根据事件类型和上下文评估置信度，决定自动化级别：
- 🟢 auto (>90%): 全自动执行，不打扰用户
- 🟡 semi (50-90%): 执行 + 通知确认
- 🔴 confirm (<50%): 只通知，等用户确认

设计原则（来自改进方案 9.2.5）：
- 已知指纹 + 合理时段 = 高置信度
- 未知指纹 + 白天 = 中置信度
- VLM 疑似摔倒 = 低置信度，需人工确认
"""

from datetime import datetime
from pathlib import Path
import json

# 数据目录
DATA_DIR = Path(__file__).parent.parent / "data"


def get_known_fingerprints():
    """获取已知指纹映射"""
    mapping_file = DATA_DIR / "door_key_mapping.json"
    if mapping_file.exists():
        try:
            return json.loads(mapping_file.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def is_reasonable_hour(hour, person=None):
    """判断当前时间对于该人是否合理
    
    Args:
        hour: 当前小时 (0-23)
        person: 人名，可选
        
    Returns:
        bool: 是否合理时段
    """
    # 凌晨 0-5 点 → 不合理（除非已知住户）
    if 0 <= hour < 5:
        return False
    
    # 白天 6-22 点 → 合理
    if 6 <= hour <= 22:
        return True
    
    # 晚上 23 点 → 边缘
    return False


def assess_confidence(event_type, payload):
    """评估事件置信度，决定自动化级别
    
    Args:
        event_type: 事件类型 (door_unlock/emergency/arrival/care)
        payload: 事件载荷 dict
        
    Returns:
        tuple: (confidence_score: float, level: str, reason: str)
            - confidence_score: 0.0-1.0
            - level: "auto" | "semi" | "confirm"
            - reason: 判断理由
    
    Examples:
        >>> assess_confidence("door_unlock", {"key_id": "2147942402", "hour": 17})
        (0.95, "auto", "已知指纹(姥姥)+合理时段")
        
        >>> assess_confidence("door_unlock", {"key_id": "unknown", "hour": 14})
        (0.70, "semi", "未知指纹+白天时段")
        
        >>> assess_confidence("emergency", {"keyword": "摔倒", "source": "vlm"})
        (0.40, "confirm", "VLM检测疑似摔倒，需人工确认")
    """
    now = datetime.now()
    hour = payload.get("hour", now.hour)
    
    # ====== 门锁事件 ======
    if event_type == "door_unlock":
        key_id = str(payload.get("key_id", ""))
        known_fps = get_known_fingerprints()
        person = known_fps.get(key_id)
        
        # 已知指纹 + 合理时段 → 高置信度
        if person and person != "待学习" and is_reasonable_hour(hour, person):
            return (0.95, "auto", f"已知指纹({person})+合理时段")
        
        # 已知指纹 + 不合理时段 → 中置信度
        if person and person != "待学习":
            return (0.75, "semi", f"已知指纹({person})+异常时段({hour}点)")
        
        # 未知指纹 + 白天 → 中置信度
        if 6 <= hour <= 22:
            return (0.70, "semi", f"未知指纹+白天时段")
        
        # 未知指纹 + 凌晨 → 低置信度
        return (0.30, "confirm", f"未知指纹+凌晨{hour}点，需确认")
    
    # ====== 紧急事件 ======
    if event_type == "emergency":
        keyword = payload.get("keyword", "")
        source = payload.get("source", "rule")  # rule/vlm/semantic
        priority = payload.get("priority", "🟡")
        
        # 规则引擎检测的明确关键词 → 较高置信度
        if source == "rule" and priority == "🔴":
            return (0.80, "semi", f"规则引擎检测到「{keyword}」，执行+确认")
        
        # VLM 语义检测 → 低置信度
        if source in ("vlm", "semantic") or "[语义]" in keyword:
            return (0.40, "confirm", f"VLM检测疑似{keyword}，需人工确认")
        
        # 一般紧急事件
        return (0.60, "semi", f"检测到{keyword}，执行+确认")
    
    # ====== 到家检测 ======
    if event_type == "arrival":
        person = payload.get("person", "")
        source = payload.get("source", "")  # door/vlm
        
        # 门锁确认 → 高置信度
        if source == "door":
            return (0.95, "auto", f"门锁确认{person}到家")
        
        # VLM 识别 → 中置信度
        if source == "vlm":
            return (0.70, "semi", f"VLM识别{person}到家，待确认")
        
        return (0.60, "semi", f"{person}可能到家")
    
    # ====== 关怀消息 ======
    if event_type == "care":
        care_type = payload.get("care_type", "")
        
        # 天气关怀 → 全自动（数据客观）
        if care_type in ("weather", "temperature", "rain"):
            return (0.95, "auto", "天气数据客观可靠")
        
        # 健康关怀 → 半自动
        if care_type in ("health", "reminder"):
            return (0.80, "auto", "定时关怀")
        
        return (0.85, "auto", "关怀消息")
    
    # ====== 默认 ======
    return (0.70, "semi", "未知事件类型，默认半自动")


def get_action_for_level(level, event_type=None):
    """根据置信度级别返回建议的动作
    
    Args:
        level: "auto" | "semi" | "confirm"
        event_type: 可选，事件类型
        
    Returns:
        dict: 建议的动作配置
    """
    actions = {
        "auto": {
            "execute": True,
            "notify": True,
            "ask_confirm": False,
            "description": "全自动执行，正常通知",
        },
        "semi": {
            "execute": True,
            "notify": True,
            "ask_confirm": True,
            "description": "执行 + 请求确认",
        },
        "confirm": {
            "execute": False,
            "notify": True,
            "ask_confirm": True,
            "description": "只通知，等待确认后执行",
        },
    }
    return actions.get(level, actions["semi"])


# ===== 测试 =====
if __name__ == "__main__":
    # 测试用例
    test_cases = [
        ("door_unlock", {"key_id": "2147942402", "hour": 17}),
        ("door_unlock", {"key_id": "unknown", "hour": 14}),
        ("door_unlock", {"key_id": "unknown", "hour": 3}),
        ("emergency", {"keyword": "摔倒", "source": "rule", "priority": "🔴"}),
        ("emergency", {"keyword": "[语义]老人躺在地上", "source": "semantic"}),
        ("arrival", {"person": "姥姥", "source": "door"}),
        ("arrival", {"person": "晓峰", "source": "vlm"}),
        ("care", {"care_type": "weather"}),
    ]
    
    print("🎯 置信度评估测试\n")
    for event_type, payload in test_cases:
        score, level, reason = assess_confidence(event_type, payload)
        action = get_action_for_level(level)
        print(f"事件: {event_type}")
        print(f"  载荷: {payload}")
        print(f"  置信度: {score:.2f} → {level}")
        print(f"  理由: {reason}")
        print(f"  动作: {action['description']}")
        print()
