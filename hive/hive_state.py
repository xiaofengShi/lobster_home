#!/usr/bin/env python3
"""
🌐 蜂巢全局状态 — 多蜂协同的状态机

支持场景切换：正常/出差/生病/暴雨预警等
所有蜜蜂读取全局状态，调整自身行为。

线程安全：所有 read-modify-write 操作通过 _state_lock 保护。
"""

import json
import threading
from datetime import datetime
from pathlib import Path
from hive.config import DATA_DIR
from hive.safe_io import safe_write_json, safe_read_json

STATE_FILE = DATA_DIR / ".hive_global_state.json"
_state_lock = threading.RLock()

# 默认状态
DEFAULT_STATE = {
    "mode": "normal",           # normal / away / sick / storm_alert
    "mode_params": {},          # 模式参数（谁出差、谁生病等）
    "mode_set_at": None,        # 模式设置时间
    "mode_expires_at": None,    # 模式过期时间（自动恢复）
    "family_overrides": {},     # 家庭成员临时状态覆盖
}


def get_hive_state():
    """获取当前蜂巢全局状态（线程安全）"""
    with _state_lock:
        state = safe_read_json(STATE_FILE, DEFAULT_STATE.copy())

        # 检查过期 → 自动恢复正常
        if state.get("mode_expires_at"):
            try:
                exp = datetime.fromisoformat(state["mode_expires_at"])
                if datetime.now() > exp:
                    state["mode"] = "normal"
                    state["mode_params"] = {}
                    state["mode_expires_at"] = None
                    safe_write_json(STATE_FILE, state)
            except (ValueError, TypeError):
                pass

        return state


def set_hive_mode(mode, params=None, expires_at=None):
    """切换蜂巢模式（线程安全）"""
    with _state_lock:
        state = safe_read_json(STATE_FILE, DEFAULT_STATE.copy())
        state["mode"] = mode
        state["mode_params"] = params or {}
        state["mode_set_at"] = datetime.now().isoformat()
        state["mode_expires_at"] = expires_at
        safe_write_json(STATE_FILE, state)
        return state


def set_family_override(name, status, until=None):
    """设置家庭成员临时状态（线程安全）"""
    with _state_lock:
        state = safe_read_json(STATE_FILE, DEFAULT_STATE.copy())
        state.setdefault("family_overrides", {})
        state["family_overrides"][name] = {
            "status": status,
            "set_at": datetime.now().isoformat(),
            "until": until,
        }
        safe_write_json(STATE_FILE, state)
        return state


def clear_family_override(name):
    """清除家庭成员临时状态（线程安全）"""
    with _state_lock:
        state = safe_read_json(STATE_FILE, DEFAULT_STATE.copy())
        state.get("family_overrides", {}).pop(name, None)
        safe_write_json(STATE_FILE, state)
        return state


def get_family_status(name):
    """获取家庭成员当前状态（含临时覆盖）"""
    state = get_hive_state()
    override = state.get("family_overrides", {}).get(name)
    if override:
        # 检查是否过期
        until = override.get("until")
        if until:
            try:
                if datetime.now() > datetime.fromisoformat(until):
                    clear_family_override(name)
                    return {}
            except (ValueError, TypeError):
                pass
        return override.get("status", {})
    return {}


def is_mode(mode):
    """快速检查当前是否是某个模式"""
    return get_hive_state().get("mode") == mode
