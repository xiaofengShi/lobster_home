#!/usr/bin/env python3
"""
⚙️ 蜂巢统一配置

所有常量、凭证、实体ID在这里定义一次。
蜜蜂通过 `from hive.config import cfg` 获取。
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.openclaw/workspace/.env.local"))

# ===== 路径 =====
HIVE_ROOT = Path(__file__).parent.parent
DATA_DIR = HIVE_ROOT / "data"
BRAIN_DIR = DATA_DIR / "brain"
DATA_DIR.mkdir(parents=True, exist_ok=True)
BRAIN_DIR.mkdir(parents=True, exist_ok=True)

# ===== Home Assistant =====
HA_URL = os.getenv("HA_URL", "http://localhost:8123")
HA_TOKEN = os.getenv("HA_TOKEN", "")
HA_HEADERS = {"Authorization": f"Bearer {HA_TOKEN}"}

# ===== 飞书 =====
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
XIAOFENG_OPEN_ID = os.getenv("XIAOFENG_OPEN_ID", "")

# ===== 设备实体 =====
CAMERA_ENTITY = "camera.chuangmi_069a01_a9d3_camera_control"
CAMERA_SWITCH = "switch.chuangmi_069a01_a9d3_switch_status"
DOOR_SENSOR = "sensor.loock_fvl109_559c_door_state"
MOTION_SENSOR = "sensor.mi_95329875_message"

SPEAKER_ENTITIES = {
    "execute": "text.xiaomi_lx06_ef64_execute_text_directive",
    "play_text": "text.xiaomi_lx06_ef64_play_text",
}

AC_SENSORS = {
    "客厅": {"temp": "sensor.xiaomi_mt7_ecac_temperature", "humidity": "sensor.xiaomi_mt7_ecac_relative_humidity"},
    "卧室": {"temp": "sensor.xiaomi_mt7_5657_temperature", "humidity": "sensor.xiaomi_mt7_5657_relative_humidity"},
    "主卧": {"temp": "sensor.xiaomi_c13_8713_temperature", "humidity": "sensor.xiaomi_c13_8713_relative_humidity"},
}

# ===== VLM =====
KSC_API_KEY = os.getenv("KSC_API_KEY", "")
VLM_MODEL = "qwen3-vl-235b-a22b-instruct"
VLM_URL = "https://kspmas.ksyun.com/v1/chat/completions"

# ===== 小宝课程表 =====
KIDS_SCHEDULE = {
    0: "体能课 18:30", 1: "英语课 19:30", 2: "体能课 18:30",
    3: "英语课 19:30", 4: None, 5: "英语课 19:30", 6: None,
}
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


class _Config:
    """单例配置对象，方便引用"""
    def __getattr__(self, name):
        # 允许 cfg.HA_URL 等方式访问模块级变量
        g = globals()
        if name in g:
            return g[name]
        raise AttributeError(f"Config has no '{name}'")


cfg = _Config()
