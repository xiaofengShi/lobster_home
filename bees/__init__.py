"""
🐝 LobsterHive Bees — 蜜蜂模块

向后兼容：旧代码中的 send_feishu / speak / check_emergency
现在统一由蜜蜂处理，这里提供别名函数。

蜜蜂清单：
- scout: 侦查蜂（感知层）
- guard: 守卫蜂（安全层）
- nurse: 哺育蜂（关怀层）
- dancer: 舞蹈蜂（通知层）
- builder: 筑巢蜂（记忆层）
- scout_guard: 侦察守卫蜂（合并版，可选）
"""

from bees.dancer import dancer
from bees.guard import guard
from bees.scout import scout
from bees.nurse import nurse
from bees.builder import builder

# 可选：合并的侦察守卫蜂
try:
    from bees.scout_guard import scout_guard
except ImportError:
    scout_guard = None


def send_feishu(text, target=None, skip_dedup=False):
    """向后兼容：发飞书消息 → 舞蹈蜂"""
    return dancer.notify_feishu(text, target=target, skip_dedup=skip_dedup)


def speak(text, force=False):
    """向后兼容：音箱播报 → 舞蹈蜂"""
    return dancer.speak(text, force=force)


def check_emergency(report_text):
    """向后兼容：紧急事件检测 → 守卫蜂"""
    return guard.check(report_text)
