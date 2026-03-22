#!/usr/bin/env python3
"""
💊 哺育蜂 (Nurse Bee) — 关怀层，照顾家人

生物学原型：哺育蜂负责喂养幼虫和照顾蜂后，是蜂巢的"保姆"。
它们根据幼虫的年龄调整食物配比，体现个性化关怀。

职责：
- 天气关怀（提前预警，个性化建议）
- 家庭成员画像（健康状况、作息时间）
- 行为统计分析（门锁/灯光/运动检测）
- 每日关怀报告生成
- 实时环境检查

核心原则：
- 每个家庭成员都是独立个体，关怀要个性化
- 提前预判 > 事后通知
- 季节感知（花粉、换季、雾霾等）
"""

import json
import os
import requests
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from hive.bee_base import BeeAgent
from hive.event_bus import event_bus
from hive.config import HA_URL, HA_TOKEN, DATA_DIR
from hive.safe_io import safe_write_json, safe_read_json
from bees.dancer import dancer

# ===== 配置 =====
HA_HEADERS = {"Authorization": f"Bearer {HA_TOKEN}"}
CARE_STATE = DATA_DIR / ".family_care_state.json"
KEY_MAP_FILE = DATA_DIR / "door_key_mapping.json"

# ===== 关怀模板（LLM 降级时使用固定文案） =====
CARE_TEMPLATES = {
    "storm": "⛈️ 暴雨预警！姥姥接小宝记得带伞穿雨衣，晓峰小冰注意路上安全，检查阳台窗户。",
    "cold_snap": "🥶 明天大降温！全家注意添衣，姥爷明早出门穿厚外套，小宝多穿一层。",
    "heat_wave": "🥵 明天高温！老人小孩少户外，多喝水，接小宝避开最热时段。",
    "rain_tomorrow": "🌂 明天有雨，出门记得带伞。姥姥接小宝备好雨具。",
    "pollen_alert": "🤧 花粉季提醒：晓峰出门戴口罩，回家换衣洗手，关好门窗。",
    "dry_indoor": "💧 室内湿度偏低，建议开启加湿器，多喝水润燥。",
    "default_care": "🦞 龙虾管家温馨提醒：注意家人健康，关注天气变化。",
}

# 家庭成员画像
FAMILY = {
    "晓峰": {
        "role": "男主人", "age_group": "adult", "works": True,
        "health": ["鼻炎", "花粉过敏"],
        "care_notes": "春天花粉季注意提醒关窗、备药；空气质量差时提醒戴口罩",
    },
    "小冰": {
        "role": "女主人", "age_group": "adult", "works": True,
        "health": ["睡眠轻"],
        "schedule": "晚上8-9点到家",
        "care_notes": "睡眠轻容易被吵醒，晚间注意控制音量",
    },
    "姥爷": {
        "role": "岳父", "age_group": "elder", "works": True,
        "schedule": "早出晚归约06:30",
        "care_notes": "早上出门最早，需要头天晚上提醒明早天气和穿衣",
    },
    "姥姥": {
        "role": "岳母", "age_group": "elder", "works": False,
        "home_duty": "接送小宝+家务",
        "care_notes": "下午接小宝注意天气；如降温/下雨需提前提醒",
    },
    "小宝": {
        "role": "儿子", "age_group": "child", "age": 5,
        "care_notes": "5岁幼童，抵抗力较弱，换季容易感冒",
    },
}

# 季节常识库
SEASONAL_AWARENESS = {
    3: {"season": "早春", "risks": ["花粉开始飘散", "早晚温差大", "倒春寒"], "tips": "春捂秋冻，别急着减衣"},
    4: {"season": "仲春", "risks": ["花粉高峰", "柳絮杨絮", "过敏高发"], "tips": "过敏人群减少外出，回家换衣洗手"},
    5: {"season": "晚春", "risks": ["花粉持续", "紫外线增强", "雷阵雨多发"], "tips": "注意防晒，出门带伞"},
    6: {"season": "初夏", "risks": ["高温开始", "空调病", "食物易变质"], "tips": "空调温度别太低，注意饮食卫生"},
    7: {"season": "盛夏", "risks": ["高温中暑", "暴雨", "蚊虫"], "tips": "老人小孩少户外，多喝水"},
    8: {"season": "盛夏", "risks": ["持续高温", "秋老虎"], "tips": "立秋不等于凉快，防暑仍要继续"},
    9: {"season": "初秋", "risks": ["早晚转凉", "秋燥", "感冒多发"], "tips": "早晚加外套，多喝水润燥"},
    10: {"season": "仲秋", "risks": ["大幅降温", "雾霾开始", "供暖前最冷"], "tips": "关注空气质量，适时添衣"},
    11: {"season": "深秋", "risks": ["寒潮", "雾霾加重", "供暖初期干燥"], "tips": "开加湿器，注意保暖"},
    12: {"season": "冬季", "risks": ["严寒", "路面结冰", "流感高发"], "tips": "老人出行注意防滑，流感疫苗"},
    1: {"season": "深冬", "risks": ["极寒", "冰雪", "心脑血管风险"], "tips": "老人早起不要太急，注意保暖"},
    2: {"season": "冬末", "risks": ["倒春寒", "流感尾声", "干燥"], "tips": "别急着脱冬装，注意润肺"},
}


class NurseBee(BeeAgent):
    """💊 哺育蜂 — 关怀层"""

    def __init__(self):
        super().__init__(name="nurse", trigger_type="cron")

    # ===== HA 工具 =====

    def ha_state(self, entity_id):
        """获取 HA 实体状态（带重试）"""
        try:
            from hive.retry import resilient_request
            r = resilient_request("get", f"{HA_URL}/api/states/{entity_id}",
                                  headers=HA_HEADERS, timeout=5, max_retries=2)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def ha_history(self, entity_id, hours=24, today_only=False):
        """获取 HA 历史记录"""
        if today_only:
            now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
            today_start_bj = now_bj.replace(hour=0, minute=0, second=0, microsecond=0)
            today_start_utc = today_start_bj - timedelta(hours=8)
            start = today_start_utc.strftime("%Y-%m-%dT%H:%M:%S")
        else:
            start = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            r = requests.get(
                f"{HA_URL}/api/history/period/{start}",
                headers=HA_HEADERS,
                params={"filter_entity_id": entity_id},
                timeout=15,
            )
            if r.status_code == 200 and r.json() and r.json()[0]:
                results = [e for e in r.json()[0] if e.get("state") not in ("unavailable", "unknown", "")]
                if today_only:
                    today_str = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
                    filtered = []
                    for e in results:
                        try:
                            t = datetime.fromisoformat(e["last_changed"].replace("Z", "+00:00")) + timedelta(hours=8)
                            if t.strftime("%Y-%m-%d") == today_str:
                                filtered.append(e)
                        except (KeyError, IndexError, TypeError, ValueError):
                            continue
                    return filtered
                return results
        except (KeyError, IndexError, TypeError, ValueError):
            pass
        return []

    def load_key_map(self):
        if KEY_MAP_FILE.exists():
            return json.loads(KEY_MAP_FILE.read_text())
        return {}

    def load_state(self):
        if CARE_STATE.exists():
            return json.loads(CARE_STATE.read_text())
        return {}

    def save_state(self, state):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CARE_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    # ===== 天气 =====

    def get_tomorrow_weather(self):
        """获取明天天气预报"""
        try:
            r = requests.get("https://wttr.in/Beijing?format=j1&lang=zh", timeout=10)
            if r.status_code == 200:
                raw = r.json()
                data = raw.get("data", raw)
                today_w = data["current_condition"][0]
                tomorrow = data["weather"][1] if len(data.get("weather", [])) > 1 else None

                result = {
                    "today": {
                        "temp": int(today_w.get("temp_C", 20)),
                        "feels_like": int(today_w.get("FeelsLikeC", 20)),
                        "humidity": int(today_w.get("humidity", 50)),
                        "desc": (today_w.get("lang_zh", [{}]) or [{}])[0].get("value", ""),
                        "uv": int(today_w.get("uvIndex", 0)),
                        "wind_km": int(today_w.get("windspeedKmph", 0)),
                    },
                }

                if tomorrow:
                    hourly = tomorrow.get("hourly", [])
                    has_rain = any(int(h.get("chanceofrain", 0)) > 50 for h in hourly)
                    has_snow = any(int(h.get("chanceofsnow", 0)) > 30 for h in hourly)
                    max_temp = int(tomorrow.get("maxtempC", 20))
                    min_temp = int(tomorrow.get("mintempC", 10))
                    noon_desc = ""
                    for h in hourly:
                        if h.get("time") in ("1200", "900", "1500"):
                            noon_desc = (h.get("lang_zh", [{}]) or [{}])[0].get("value", "")
                            break
                    result["tomorrow"] = {
                        "max_temp": max_temp,
                        "min_temp": min_temp,
                        "has_rain": has_rain,
                        "has_snow": has_snow,
                        "desc": noon_desc,
                        "rain_hours": [h.get("time", "") for h in hourly if int(h.get("chanceofrain", 0)) > 50],
                    }
                return result
        except (KeyError, IndexError, TypeError, ValueError) as e:
            self._log("warn", f"天气获取失败: {e}")
        return None

    def generate_weather_care(self, weather):
        """生成个性化天气关怀"""
        tips = []
        if not weather:
            return tips

        tomorrow = weather.get("tomorrow")
        today = weather.get("today", {})
        now = datetime.now()
        month = now.month
        seasonal = SEASONAL_AWARENESS.get(month, {})

        # 花粉季（3-5月）— 晓峰有鼻炎
        if month in (3, 4, 5):
            xiaofeng = FAMILY.get("晓峰", {})
            if "花粉过敏" in xiaofeng.get("health", []):
                wind = today.get("wind_km", 0)
                if wind > 10:
                    tips.append({
                        "icon": "🤧", "priority": "high", "target": "晓峰",
                        "text": f"晓峰注意：{seasonal.get('season', '春天')}花粉期，今天风速{wind}km/h，出门戴口罩",
                    })

        if tomorrow:
            # 姥爷明早提醒
            if tomorrow["min_temp"] <= 5:
                tips.append({
                    "icon": "🧥", "priority": "high", "target": "姥爷",
                    "text": f"姥爷明早出门注意：最低{tomorrow['min_temp']}°C，穿厚外套",
                })

            # 明天下雨
            if tomorrow.get("has_rain"):
                rain_hours = tomorrow.get("rain_hours", [])
                early_rain = any(h in ("600", "700", "800", "900") for h in rain_hours)
                afternoon_rain = any(h in ("1500", "1600", "1700") for h in rain_hours)
                if early_rain:
                    tips.append({"icon": "🌂", "priority": "high", "target": "全家", "text": "明早有雨，出门带伞"})
                tomorrow_weekday = (now.weekday() + 1) % 7
                if afternoon_rain and tomorrow_weekday < 5:  # 只有明天是工作日才提醒接小宝
                    tips.append({"icon": "☔", "priority": "high", "target": "姥姥", "text": "明天下午可能下雨，接小宝记得带伞"})

            # 大降温
            temp_drop = today.get("temp", 20) - tomorrow["min_temp"]
            if temp_drop >= 8:
                tips.append({
                    "icon": "🥶", "priority": "high", "target": "全家",
                    "text": f"明天大降温{temp_drop}°C！最低{tomorrow['min_temp']}°C，全家添衣",
                })

            # 明天很热（只有工作日才提醒接小宝）
            if tomorrow["max_temp"] >= 33:
                tomorrow_wd = (now.weekday() + 1) % 7
                if tomorrow_wd < 5:  # 工作日
                    tips.append({
                        "icon": "🥵", "priority": "medium", "target": "姥姥",
                        "text": f"明天最高{tomorrow['max_temp']}°C，接小宝避开最热时段",
                    })
                else:
                    tips.append({
                        "icon": "🥵", "priority": "medium", "target": "全家",
                        "text": f"明天最高{tomorrow['max_temp']}°C，外出注意防暑",
                    })

        # 室内湿度检查
        for eid, room in [
            ("sensor.xiaomi_mt7_ecac_relative_humidity", "客厅"),
            ("sensor.xiaomi_c13_8713_relative_humidity", "主卧"),
        ]:
            state = self.ha_state(eid)
            if state:
                try:
                    humidity = float(state["state"])
                    if humidity < 30:
                        tips.append({"icon": "💧", "priority": "high", "text": f"{room}湿度{humidity:.0f}%很干，开加湿器"})
                except (ValueError, TypeError):
                    pass

        return tips

    # ===== 行为分析 =====

    def analyze_door_activity(self):
        """分析门锁出入"""
        key_map = self.load_key_map()
        lock_history = self.ha_history("sensor.loock_fvl109_559c_lock_action", today_only=True)
        key_history = self.ha_history("sensor.loock_fvl109_559c_lock_key_id", today_only=True)

        events = []
        for e in lock_history:
            try:
                t = datetime.fromisoformat(e["last_changed"].replace("Z", "+00:00")) + timedelta(hours=8)
            except (KeyError, IndexError, TypeError, ValueError):
                continue

            kid = ""
            for k in key_history:
                try:
                    kt = datetime.fromisoformat(k["last_changed"].replace("Z", "+00:00"))
                    et = datetime.fromisoformat(e["last_changed"].replace("Z", "+00:00"))
                    if abs((kt - et).total_seconds()) < 60:
                        kid = k["state"]
                        break
                except (KeyError, IndexError, TypeError, ValueError):
                    continue

            who = key_map.get(kid, "") if kid else ""
            who_name = who.split("—")[0].split("（")[0].strip() if who and "待学习" not in who else ""

            events.append({
                "time": t.strftime("%H:%M"),
                "hour": t.hour,
                "action": e["state"],
                "key_id": kid,
                "who": who_name,
            })
        return events

    def analyze_light_activity(self):
        """分析灯光活动"""
        lights = [
            ("light.yeelink_ceiling18_c57b_light", "客厅"),
            ("light.yeelink_ceil26_80a2_light", "卧室"),
            ("light.yeelink_ceil26_cb7b_light", "次卧"),
        ]
        events = []
        for eid, room in lights:
            history = self.ha_history(eid, today_only=True)
            for h in history:
                if h.get("state") in ("on", "off"):
                    try:
                        t = datetime.fromisoformat(h["last_changed"].replace("Z", "+00:00")) + timedelta(hours=8)
                        events.append({"time": t.strftime("%H:%M"), "hour": t.hour, "room": room, "state": h["state"]})
                    except (KeyError, IndexError, TypeError, ValueError):
                        continue
        return sorted(events, key=lambda x: x["time"])

    def analyze_behavior_patterns(self, door_events, light_events):
        """推断行为模式"""
        insights = []
        now = datetime.now()

        morning_lights = [e for e in light_events if e["state"] == "on" and 5 <= e["hour"] <= 9]
        if morning_lights:
            insights.append(f"最早 {morning_lights[0]['time']} {morning_lights[0]['room']}灯亮（起床时间）")

        daytime_lights = [e for e in light_events if 8 <= e["hour"] <= 17]
        if len(daytime_lights) <= 2 and now.hour >= 14:
            insights.append("⚠️ 白天灯光变化很少，家里可能活动不多")

        out_events = [e for e in door_events if "inside_unlock" in e["action"]]
        if len(out_events) == 0 and now.hour >= 17:
            insights.append("⚠️ 今天到现在没人出过门")

        motion = self.ha_state("sensor.mi_95329875_message")
        if motion and 9 <= now.hour <= 18:
            last_changed = motion.get("last_changed", "")
            try:
                mt = datetime.fromisoformat(last_changed.replace("Z", "+00:00")) + timedelta(hours=8)
                hours_ago = (now - mt).total_seconds() / 3600
                if hours_ago > 3:
                    insights.append(f"⚠️ 距上次运动检测已{hours_ago:.0f}小时")
            except (KeyError, IndexError, TypeError, ValueError):
                pass

        return insights

    def get_room_env(self):
        """获取各房间环境"""
        rooms = {}
        for t_eid, h_eid, room in [
            ("sensor.xiaomi_mt7_ecac_temperature", "sensor.xiaomi_mt7_ecac_relative_humidity", "客厅"),
            ("sensor.xiaomi_mt7_5657_temperature", "sensor.xiaomi_mt7_5657_relative_humidity", "卧室"),
            ("sensor.xiaomi_c13_8713_temperature", "sensor.xiaomi_c13_8713_relative_humidity", "主卧"),
        ]:
            t_state = self.ha_state(t_eid)
            h_state = self.ha_state(h_eid)
            if t_state:
                rooms[room] = {
                    "temp": float(t_state["state"]),
                    "humidity": float(h_state["state"]) if h_state else None,
                }
        return rooms

    # ===== 报告生成 =====

    def generate_daily_report(self):
        """生成每日关怀报告"""
        state = self.load_state()
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

        if state.get("last_daily_report") == today_str:
            self._log("info", "今天已发过关怀报告")
            return None

        weather = self.get_tomorrow_weather()
        door_events = self.analyze_door_activity()
        light_events = self.analyze_light_activity()
        behavior = self.analyze_behavior_patterns(door_events, light_events)
        rooms = self.get_room_env()
        weather_tips = self.generate_weather_care(weather)

        lines = [f"🦞 家庭关怀日报｜{today_str} {weekday_cn}\n"]

        # 时间线
        lines.append("📋 今日家里发生了什么\n")
        timeline = []
        for e in door_events:
            if "outside_unlock" in e["action"]:
                who = e["who"] or "有人"
                timeline.append((e["time"], f"🚪 {who}到家"))
            elif "inside_unlock" in e["action"]:
                timeline.append((e["time"], "🚪 有人出门"))
        for e in light_events:
            if e["state"] == "on":
                timeline.append((e["time"], f"💡 {e['room']}灯亮了"))
        timeline.sort(key=lambda x: x[0])
        for t, desc in timeline:
            lines.append(f"  {t}  {desc}")
        if not timeline:
            lines.append("  （今日传感器数据较少）")

        # 行为分析
        if behavior:
            lines.append("\n🔍 行为分析\n")
            for b in behavior:
                lines.append(f"  {b}")

        # 环境
        lines.append("\n🌡️ 家里环境\n")
        for room, d in rooms.items():
            h_str = f"湿度{d['humidity']:.0f}%" if d["humidity"] else ""
            warn = " ⚠️ 偏干" if d["humidity"] and d["humidity"] < 35 else ""
            lines.append(f"  {room} {d['temp']:.0f}°C {h_str}{warn}")
        if weather and weather.get("today"):
            w = weather["today"]
            lines.append(f"  室外 {w.get('desc', '')} {w.get('temp', '')}°C")

        # 天气关怀
        high_tips = [t for t in weather_tips if t["priority"] == "high"]
        if high_tips:
            lines.append("\n📣 提前关怀\n")
            for t in high_tips:
                lines.append(f"  {t['icon']} {t['text']}")

        # 小宝
        lines.append("\n👦 小宝\n")
        wd = now.weekday()
        if wd in (0, 2):
            lines.append("  今晚 18:30 体能课")
        elif wd in (1, 3, 5):
            lines.append("  今晚 19:30 英语课")
        else:
            lines.append("  今天没有课外课")

        # 门锁电量
        battery_state = self.ha_state("sensor.loock_fvl109_559c_battery_level")
        if battery_state:
            bat = float(battery_state["state"])
            bat_icon = "✅" if bat > 50 else ("🟡" if bat > 20 else "🔴 需换电池！")
            lines.append(f"\n🔋 门锁电量 {bat:.0f}% {bat_icon}")

        report = "\n".join(lines)
        self._log("info", f"生成关怀报告: {len(report)} 字")

        if dancer.notify_feishu(report):
            state["last_daily_report"] = today_str
            self.save_state(state)
            event_bus.publish({
                "source": "nurse",
                "type": "daily_care_report",
                "intensity": "normal",
                "payload": {"date": today_str},
            })

        return report

    def realtime_check(self):
        """实时环境检查"""
        now = datetime.now()
        alerts = []

        # 运动检测异常
        motion = self.ha_state("sensor.mi_95329875_message")
        if motion and 9 <= now.hour <= 18:
            last_changed = motion.get("last_changed", "")
            try:
                mt = datetime.fromisoformat(last_changed.replace("Z", "+00:00")) + timedelta(hours=8)
                hours_since = (now - mt).total_seconds() / 3600
                if hours_since > 4:
                    alerts.append(f"🟡 家里已{hours_since:.0f}小时没有运动检测")
            except (KeyError, IndexError, TypeError, ValueError):
                pass

        # 温度异常
        for t_eid, room in [
            ("sensor.xiaomi_mt7_ecac_temperature", "客厅"),
            ("sensor.xiaomi_mt7_5657_temperature", "卧室"),
        ]:
            state = self.ha_state(t_eid)
            if state:
                temp = float(state["state"])
                if temp < 10:
                    alerts.append(f"🔴 {room}温度{temp}°C，太冷了！")
                elif temp > 35:
                    alerts.append(f"🔴 {room}温度{temp}°C，太热了！")

        # 门锁电量
        bat = self.ha_state("sensor.loock_fvl109_559c_battery_level")
        if bat and float(bat["state"]) < 15:
            alerts.append(f"🔴 门锁电量{bat['state']}%，需要换电池！")

        return alerts

    # ===== BeeAgent 接口 =====

    def check_family_arrivals(self):
        """检查门锁记录，检测家人到家并通知"""
        now = datetime.now()
        hour = now.hour
        today_str = now.strftime("%Y-%m-%d")

        if hour < 19 or hour >= 22:
            return

        arrival_state = DATA_DIR / ".arrival_notify_state.json"
        notified = {}
        try:
            if arrival_state.exists():
                state = json.loads(arrival_state.read_text())
                if state.get("date") == today_str:
                    notified = state.get("notified", {})
        except (json.JSONDecodeError, ValueError, KeyError):
            pass

        lock_history = self.ha_history("sensor.loock_fvl109_559c_lock_action", hours=1)
        key_history = self.ha_history("sensor.loock_fvl109_559c_lock_key_id", hours=1)
        key_map = self.load_key_map()

        if not lock_history or not key_history:
            return

        for event in lock_history:
            action = event.get("state", "")
            if action != "outside_unlock":
                continue
            try:
                t = datetime.fromisoformat(event["last_changed"].replace("Z", "+00:00")) + timedelta(hours=8)
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if (now - t).total_seconds() > 1800:
                continue

            key_id = None
            for k in key_history:
                if k.get("last_changed") == event.get("last_changed"):
                    key_id = str(k.get("state", ""))
                    break
            if not key_id:
                continue

            person = key_map.get(key_id, "")
            if person in ("姥姥", "岳父（姥爷）— 早出晚归，06:30出门/20:19回家"):
                continue

            time_str = t.strftime("%H:%M")
            if person and "待学习" not in person:
                who = person
            elif 20 <= t.hour <= 21:
                who = "小冰(推测)"
            elif 19 <= t.hour <= 22:
                who = "晓峰或小冰"
            else:
                who = f"未知(key={key_id})"

            if who in notified:
                continue

            if "小冰" in who:
                msg = f"🏠 小冰 {time_str} 到家了\n\n辛苦了一天，可以问问她今天累不累、想吃什么 ❤️"
                dancer.notify_feishu(msg)
                notified["小冰"] = time_str
            elif "晓峰" in who:
                notified["晓峰"] = time_str
            else:
                dancer.notify_feishu(f"🏠 {who} {time_str} 到家了")
                notified[who] = time_str

        try:
            arrival_state.parent.mkdir(parents=True, exist_ok=True)
            arrival_state.write_text(json.dumps({"date": today_str, "notified": notified}, ensure_ascii=False, indent=2))
        except (json.JSONDecodeError, ValueError, KeyError):
            pass

    def generate_storm_care(self, weather_payload):
        """暴雨预警 → 结合家庭状态生成个性化提醒

        LLM/API 失败时降级为 CARE_TEMPLATES 固定模板。
        """
        alert = weather_payload.get("alert", "暴雨")
        now = datetime.now()
        hour = now.hour

        try:
            parts = [f"⛈️ {alert}预警"]

            # 结合家庭状态
            from hive.hive_state import get_family_status
            FAMILY_CARE = {
                "姥姥": "接小宝时带伞穿雨衣",
                "姥爷": "下班回家注意安全",
                "晓峰": "建议提前回家避开暴雨",
                "小冰": "注意路上安全",
                "小宝": "放学时记得带雨具",
            }
            for name, tip in FAMILY_CARE.items():
                status = get_family_status(name)
                if status.get("away") or status.get("sick"):
                    continue  # 不在家/生病的跳过出行提醒
                parts.append(f"• {name}：{tip}")

            # 阳台/窗户提醒
            if 8 <= hour <= 20:
                parts.append("• 检查阳台衣服是否收了，窗户是否关好")

            return "\n".join(parts)
        except Exception as e:
            # 🔻 降级：LLM/API 失败时使用固定模板
            self._log("warn", f"暴雨关怀生成失败，降级为模板: {e}")
            return CARE_TEMPLATES["storm"]

    def process(self, event):
        event_type = event.get("type", "")
        if event_type == "daily_report":
            return self.generate_daily_report()
        elif event_type == "realtime_check":
            return self.realtime_check()
        elif event_type == "weather_care":
            weather = self.get_tomorrow_weather()
            return self.generate_weather_care(weather)
        else:
            self._log("warn", f"未知事件: {event_type}")
            return None


# 模块单例
nurse = NurseBee()

# 向后兼容
def get_tomorrow_weather():
    return nurse.get_tomorrow_weather()

def generate_weather_care(weather):
    return nurse.generate_weather_care(weather)

def generate_daily_report():
    return nurse.generate_daily_report()

def realtime_check():
    return nurse.realtime_check()

def check_family_arrivals():
    return nurse.check_family_arrivals()
