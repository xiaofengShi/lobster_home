#!/usr/bin/env python3
"""
🔭 侦查蜂 (Scout Bee) — 感知层，看见世界

生物学原型：侦查蜂是蜂群中最勇敢的5%，它们飞出蜂巢探索未知区域，
发现食物和危险后回巢通过摇摆舞报告方位和距离。

职责：
- 摄像头截图获取（含隐私保护）
- VLM 视觉分析（图像→结构化报告）
- 环境感知（温湿度、门锁、运动传感器）
- 天气获取
- 门锁事实采集
- 家庭活动记录
- 门锁指纹自动学习

核心原则：
- 🔴 隐私铁律：用户关闭的设备，绝不唤醒
- 🔴 摄像头 19:00-05:00 休眠
- 事实优先：传感器数据 > 画面猜测
"""

import base64
import json
import os
import re
import requests
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 蜂巢基类 + 统一配置
sys.path.insert(0, str(Path(__file__).parent.parent))
from hive.bee_base import BeeAgent
from hive.event_bus import event_bus
from hive.config import (HA_URL, HA_TOKEN, KSC_API_KEY, VLM_MODEL, VLM_URL,
                          CAMERA_ENTITY, CAMERA_SWITCH, DOOR_SENSOR, MOTION_SENSOR,
                          AC_SENSORS, DATA_DIR, XIAOFENG_OPEN_ID)
from hive.safe_io import safe_write_json, safe_read_json, safe_append_jsonl

# 大脑（向后兼容，后续改为 builder）
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from bees.builder import learn_person, get_known_people_context, observe_event, log_observation

FAMILY_ACTIVITY_LOG = DATA_DIR / "family_activity_log.jsonl"
KEY_MAP_FILE = DATA_DIR / "door_key_mapping.json"

# 家庭成员信息（VLM prompt 用）— 动态生成，区分工作日/周末
def get_family_info():
    """根据当前是工作日还是周末，生成不同的家庭推断规则"""
    now = datetime.now()
    weekday = now.weekday()  # 0=周一...6=周日
    is_weekend = weekday >= 5  # 周六/周日
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][weekday]

    base = f"""家庭成员（5人）：
- 晓峰：男主人，36岁男性，AI研究员
- 小冰：女主人，30多岁年轻女性，晓峰的老婆、小宝的妈妈
- 岳父（姥爷）：50-60岁中老年男性
- 岳母（姥姥）：50-60岁中老年女性，体型通常比小冰偏胖/壮实
- 小宝：5岁男孩，身高矮

🔴🔴🔴 小冰 vs 姥姥 vs 晓峰 区分要点（反复出错，必须注意！）：
- 小冰 = 年轻女性（30多岁），皮肤、体态、穿着偏年轻
- 姥姥 = 中老年女性（50-60岁），体型偏胖/壮实，面部有明显年龄感
- 晓峰 = 36岁男性
- 小冰和晓峰年龄相近，都是30多岁，注意区分性别特征
- 🚫 不要因为"有人坐在桌前"就默认是晓峰！小冰也经常坐桌前
- 🚫 不要因为"有女性在带小宝"就默认是姥姥！小冰（妈妈）也经常带小宝
- 关键判断：先看性别，再看年龄特征（面部、皮肤、体态），最后才看活动

课外课程：
- 周一、周三 18:30 体能课
- 周二、周四、周六 19:30 英语课

📅 今天是 {weekday_cn}"""

    if is_weekend:
        base += """（周末）

⚠️ 根据时间推断谁在家（周末）：
- 周末不上班不上幼儿园，全家人可能全天在家
- 晓峰、小冰、姥爷都不需要上班，在家是完全正常的
- 小宝不上幼儿园，全天在家
- 可能会有外出活动（购物、公园等），有人不在也正常
- 🔴 周末看到年轻女性坐桌前/带小孩 → 大概率是小冰（妈妈），不是姥姥也不是晓峰
- 必须根据性别+年龄来判断，不要根据活动（坐桌前≠晓峰，带孩子≠姥姥）"""
    else:
        base += """（工作日）

小宝时间表（工作日）：
- 7:30 出门上幼儿园
- 17:00 姥姥去学校接他
- 17:30 到家

⚠️ 根据时间推断谁在家（工作日）：
- 06:30 前：全家可能都在
- 06:30-07:30：岳父已出门，其他人可能在
- 07:30-17:00：大概率只有姥姥一个人在家
- 17:00-17:30：姥姥出门接小宝，可能家里没人
- 17:30-19:00：姥姥 + 小宝
- 19:00-20:00：姥姥 + 小宝 + 晓峰/小冰可能到家
- 20:00 后：全家可能都回来了"""

    base += """

🔴🔴🔴 最重要的约束（反复出错，必须死记！）：
0. 🔴【首要原则】先数人数！画面中有几个真实的活人？家具、靠垫、衣物、玩具、阴影都不是人！不确定是不是人的 → 不算人！
1. 背影判断不可靠：仅凭"短发、深色上衣"不能确认是谁。
2. 坐桌前的人不一定是晓峰！小冰也会坐桌前。必须根据性别和年龄判断。
3. 带小宝的女性不一定是姥姥！小冰（妈妈）也带小宝。必须根据年龄判断（30多岁=小冰，50-60岁=姥姥）。
4. 如果当前时间某人不太可能在家，看到疑似该人时，标注 ⚠️ 并说明原因。
5. 如果今天是周末，全家在家是正常情况，不要标注⚠️。
6. 🔴【严禁幻觉】如果画面中只有一个小孩在玩，就只报告一个小孩。不要脑补出一个"看护的大人"。沙发上的靠垫不是人，椅子上的衣服不是人。
"""
    return base


class ScoutBee(BeeAgent):
    """🔭 侦查蜂 — 感知层"""

    def __init__(self):
        super().__init__(name="scout", trigger_type="cron")

    # ===== HA 通信 =====

    def ha_get(self, endpoint):
        """调用 HA API（带自动重试）"""
        from hive.retry import resilient_request
        return resilient_request(
            "get", f"{HA_URL}/api/{endpoint}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            timeout=10, max_retries=2,
        )

    def ha_history(self, entity_id, hours=24):
        """获取 HA 历史记录"""
        start = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            r = requests.get(
                f"{HA_URL}/api/history/period/{start}",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
                params={"filter_entity_id": entity_id},
                timeout=15,
            )
            if r.status_code == 200 and r.json() and r.json()[0]:
                return [e for e in r.json()[0] if e.get("state") not in ("unavailable", "unknown", "")]
        except (requests.RequestException, ConnectionError, TimeoutError, OSError):
            pass
        return []

    # ===== 摄像头 =====

    def get_camera_snapshot(self):
        """获取摄像头截图

        🔴 隐私铁律：用户关闭 → 不唤醒；19:00-05:00 → 休眠
        
        Returns:
            (image_path, motion_time) 或 (None, None)
        """
        # 检查摄像头状态
        state_resp = self.ha_get(f"states/{CAMERA_ENTITY}")
        motion_time = None
        if state_resp.status_code == 200:
            cam_state = state_resp.json()
            cam_status = cam_state.get("state", "unavailable")
            attrs = cam_state.get("attributes", {})
            motion_time = attrs.get("motion_video_time", "")

            # 检查摄像机开关
            switch_resp = self.ha_get("states/switch.chuangmi_069a01_a9d3_switch_status")
            switch_on = True
            if switch_resp.status_code == 200:
                switch_on = switch_resp.json().get("state", "off") == "on"

            if not switch_on or cam_status in ("unavailable", "off", "unknown"):
                self._log("info", f"摄像头 {cam_status}, 开关 {'on' if switch_on else 'off'} — 已被用户关闭，跳过")
                return None, None
        else:
            self._log("warn", f"无法获取摄像头状态 ({state_resp.status_code})")
            return None, None

        # 时间检查（19:00-05:00 休眠）
        now = datetime.now()
        if now.hour >= 19 or now.hour < 5:
            self._log("info", f"当前 {now.strftime('%H:%M')}，摄像头休眠时段")
            return None, None

        # 截图
        resp = self.ha_get(f"camera_proxy/{CAMERA_ENTITY}")
        if resp.status_code == 200 and len(resp.content) > 1000:
            path = "/tmp/home_patrol_snapshot.jpg"
            with open(path, "wb") as f:
                f.write(resp.content)
            self._log("info", f"截图成功: {len(resp.content)} bytes")
            return path, motion_time
        else:
            self._log("warn", f"截图失败 ({resp.status_code})")
            return None, None

    # ===== 环境感知 =====

    def get_environment(self):
        """获取室内环境数据"""
        env = {}
        for room, sensors in AC_SENSORS.items():
            try:
                temp = self.ha_get(f'states/{sensors["temp"]}').json().get("state", "?")
                hum = self.ha_get(f'states/{sensors["humidity"]}').json().get("state", "?")
                env[room] = f"{temp}°C, 湿度{hum}%"
            except (KeyError, IndexError, TypeError, ValueError):
                env[room] = "获取失败"

        try:
            door = self.ha_get(f"states/{DOOR_SENSOR}").json().get("state", "?")
            env["门锁"] = door
        except (KeyError, IndexError, TypeError, ValueError):
            env["门锁"] = "获取失败"

        try:
            motion = self.ha_get(f"states/{MOTION_SENSOR}").json().get("state", "?")
            env["最近移动"] = motion
        except (KeyError, IndexError, TypeError, ValueError):
            env["最近移动"] = "获取失败"

        return env

    def get_weather(self):
        """获取北京当前天气"""
        try:
            resp = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": 39.9, "longitude": 116.4,
                    "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                    "hourly": "precipitation_probability,temperature_2m,weather_code",
                    "forecast_days": 1,
                    "timezone": "Asia/Shanghai",
                },
                timeout=10,
            )
            data = resp.json()
            current = data["current"]

            weather_names = {
                0: "晴", 1: "大部晴", 2: "多云", 3: "阴天",
                45: "雾", 48: "霜雾", 51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
                61: "小雨", 63: "中雨", 65: "大雨", 71: "小雪", 73: "中雪", 75: "大雪",
                80: "阵雨", 81: "中阵雨", 82: "大阵雨", 95: "雷暴", 96: "冰雹雷暴",
            }

            weather = {
                "current_temp": current["temperature_2m"],
                "humidity": current["relative_humidity_2m"],
                "wind_speed": current["wind_speed_10m"],
                "weather": weather_names.get(current["weather_code"], f"天气码{current['weather_code']}"),
                "weather_code": current["weather_code"],
            }

            # 未来6小时降水预警
            hourly = data.get("hourly", {})
            now_hour = datetime.now().hour
            rain_alerts = []
            for t, prob, code in zip(
                hourly.get("time", []),
                hourly.get("precipitation_probability", []),
                hourly.get("weather_code", []),
            ):
                hour = int(t[11:13])
                if now_hour <= hour <= now_hour + 6:
                    if prob and prob > 40:
                        rain_alerts.append(f"{hour}点降水概率{prob}%")

            weather["rain_alerts"] = rain_alerts
            return weather
        except (KeyError, IndexError, TypeError, ValueError) as e:
            self._log("warn", f"天气获取失败: {e}")
            return None

    # ===== 门锁事实 =====

    def get_recent_door_facts(self):
        """从门锁历史获取已确认事实"""
        facts = []
        try:
            now = datetime.now()
            mapping_file = DATA_DIR / "door_key_mapping.json"
            mapping = {}
            if mapping_file.exists():
                mapping = json.loads(mapping_file.read_text())

            today_start_utc = now.replace(hour=0, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%S")
            r = requests.get(
                f"{HA_URL}/api/history/period/{today_start_utc}",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
                params={"filter_entity_id": "sensor.loock_fvl109_559c_lock_action,sensor.loock_fvl109_559c_lock_key_id"},
                timeout=10,
            )

            if r.status_code == 200:
                events = []
                for entity_data in r.json():
                    if not entity_data:
                        continue
                    eid = entity_data[0].get("entity_id", "")
                    for e in entity_data:
                        s = e.get("state", "")
                        if s in ("unavailable", "unknown", ""):
                            continue
                        try:
                            t = datetime.fromisoformat(e["last_changed"].replace("Z", "+00:00"))
                            import datetime as dt_mod
                            t_local = t + dt_mod.timedelta(hours=8)
                            events.append({
                                "time": t_local,
                                "type": "action" if "action" in eid else "key_id",
                                "value": s,
                            })
                        except (KeyError, IndexError, TypeError, ValueError):
                            pass

                events.sort(key=lambda x: x["time"])
                recent_arrivals = []
                # 公共密码/无法区分身份的 key_id 列表
                ambiguous_keys = {"0", "100", "2147942403"}  # 内部开锁、临时密码、公共数字密码
                for i, evt in enumerate(events):
                    if evt["type"] == "action" and evt["value"] == "outside_unlock":
                        key_id = None
                        for j in range(max(0, i - 2), min(len(events), i + 3)):
                            if events[j]["type"] == "key_id" and abs((events[j]["time"] - evt["time"]).total_seconds()) < 30:
                                key_id = events[j]["value"]
                                break
                        time_str = evt['time'].strftime('%H:%M')
                        if key_id and key_id not in ambiguous_keys:
                            who = mapping.get(key_id, f"指纹{key_id}")
                            recent_arrivals.append(f"{time_str} {who}从外面开门回家（指纹确认）")
                        else:
                            desc = mapping.get(key_id, "未知方式") if key_id else "未知"
                            recent_arrivals.append(f"{time_str} 有人从外面开门回家（{desc}，无法确认身份）")

                if recent_arrivals:
                    facts.append("今天已确认的到家记录（门锁数据）：")
                    for a in recent_arrivals:
                        facts.append(f"  - {a}")

            # 活动日志中今天的记录
            activity_log = DATA_DIR / "family_activity_log.jsonl"
            if activity_log.exists():
                today_str = now.strftime("%Y-%m-%d")
                seen_today = set()
                for line in activity_log.read_text().splitlines():
                    try:
                        entry = json.loads(line.strip())
                        if entry.get("date") == today_str and entry.get("seen"):
                            for person in entry["seen"]:
                                seen_today.add(person)
                    except (json.JSONDecodeError, ValueError, KeyError):
                        pass
                if seen_today:
                    facts.append(f"今天巡查已确认看到的家庭成员：{'、'.join(seen_today)}")
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            facts.append(f"(门锁数据获取失败: {e})")

        return "\n".join(facts) if facts else ""

    # ===== VLM 分析 =====

    def analyze_with_vlm(self, image_path, env_data, motion_time=None, weather=None):
        """用 VLM 分析摄像头画面"""
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        now = datetime.now()
        weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

        if motion_time:
            try:
                mt = datetime.strptime(motion_time[:19], "%Y-%m-%d %H:%M:%S")
                time_str = mt.strftime("%H:%M")
            except (KeyError, IndexError, TypeError, ValueError):
                time_str = now.strftime("%H:%M")
        else:
            time_str = now.strftime("%H:%M")

        env_text = "\n".join([f"  {k}: {v}" for k, v in env_data.items()])

        weather_text = ""
        if weather:
            weather_text = f"\n室外天气：{weather['weather']}，{weather['current_temp']}°C，湿度{weather['humidity']}%，风速{weather['wind_speed']}km/h"
            if weather.get("rain_alerts"):
                weather_text += f"\n⚠️ 降雨预警：{'，'.join(weather['rain_alerts'])}"

        people_context = get_known_people_context()
        door_facts = self.get_recent_door_facts()
        door_facts_text = f"\n\n📋 已确认的事实（来自门锁和历史巡查，请以此为准）：\n{door_facts}" if door_facts else ""

        # 接小宝时间计算（仅工作日！周末不上幼儿园）
        is_weekend = now.weekday() >= 5  # 周六=5, 周日=6
        if is_weekend:
            pickup_reminder = ""
        else:
            pickup_time = now.replace(hour=17, minute=0, second=0, microsecond=0)
            minutes_to_pickup = int((pickup_time - now).total_seconds() / 60)
            if 0 < minutes_to_pickup <= 120:
                pickup_reminder = f"- 距离17:00接小宝还有{minutes_to_pickup}分钟，请提醒姥姥提前准备出门"
            elif minutes_to_pickup <= 0:
                pickup_reminder = "- 已过17:00，姥姥应该已出门接小宝或已回来"
            else:
                pickup_reminder = f"- 17:00姥姥需出门接小宝，距今还有{minutes_to_pickup // 60}小时{minutes_to_pickup % 60}分钟，暂无需提醒"

        family_info = get_family_info()

        prompt = f"""你是一个智能家居AI管家"龙虾管家🦞"。

画面拍摄时间：{weekday_cn} {time_str}
室内环境：
{env_text}{weather_text}

{family_info}

{people_context}{door_facts_text}

请分析这张家庭摄像头画面，给出一份简洁的巡查报告。

巡查报告内容：
1. 【在家人员】只报告你在画面中**确定看到的真实活人**。
2. 【状态评估】他们在做什么？状态是否正常？
3. 【环境检查】结合温湿度数据，环境是否舒适？需要调整吗？
4. 【提醒事项】根据当前时间和家庭日程，有什么需要提醒的？
   {pickup_reminder}
   - 如果今天有课外课，提前30分钟提醒
   - 如果老人长时间没动，建议关注
   - 如果有降雨预警且无人在家，提醒关窗收衣服
   - 如果室外温度骤降/骤升，建议调整空调
   - 如果大风，提醒关窗
5. 【建议】有什么建议？

用中文回答，控制在300字以内。

⚠️【强制要求——严格遵守，违反即错误】
1. 对于每个**确定是真实活人**的人输出：PERSON_FEATURE: [外貌特征] | GUESS: [猜测]
2. 🔴🔴🔴 **严格判断"是不是人"：**
   - 家具（沙发、椅子、靠垫）不是人
   - 衣物、背包、毛绒玩具不是人
   - 墙上挂画、照片中的人物不是人
   - 阴影、模糊轮廓不是人
   - **如果不确定某个形状是不是人 → 不报告！宁可漏报也不要误报！**
3. 🔴 **画面中只有1个人就报告1个人，有0个人就说"画面中无人"。不要凑数！**
4. 🔴 **不要把小宝旁边的沙发/靠垫/玩具当成"看护的大人"。5岁孩子独自在客厅玩是正常的。**
5. 【时间常识约束】识别人物时必须结合当前时间判断合理性。
6. 【已确认事实优先】门锁已确认到家的人不需要在画面中再次确认。"""

        from hive.retry import resilient_request
        resp = resilient_request(
            "post", VLM_URL, max_retries=1,
            headers={"Authorization": f"Bearer {KSC_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": VLM_MODEL,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }],
                "max_tokens": 600,
            },
            timeout=60,
        )

        data = resp.json()
        if "choices" in data:
            text = data["choices"][0]["message"]["content"]
            if "<think>" in text:
                text = text.split("</think>")[-1].strip()

            # 发布侦查结果事件
            event_bus.publish({
                "source": "scout",
                "type": "scene_report",
                "intensity": "normal",
                "payload": {"report": text[:500], "image_path": image_path},
            })
            return text
        else:
            self._log("error", f"VLM 分析失败: {json.dumps(data, ensure_ascii=False)[:200]}")
            return f"VLM 分析失败: {json.dumps(data, ensure_ascii=False)[:200]}"

    # ===== 活动记录 =====

    def record_family_activity(self, report, env_data):
        """从巡查报告中提取家庭成员活动"""
        try:
            now = datetime.now()
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            log_file = DATA_DIR / "family_activity_log.jsonl"

            # 从【在家人员】段落提取
            person_section = ""
            person_match = re.search(r"【在家人员】(.*?)(?=【|$)", report, re.DOTALL)
            if person_match:
                person_section = person_match.group(1)

            feature_lines = [l for l in report.split("\n") if "PERSON_FEATURE:" in l]
            person_text = person_section + "\n".join(feature_lines)

            negative_patterns = ["不应在家", "本应在", "不可能", "与预期时间不符", "未见"]

            members = {
                "晓峰": ["晓峰", "男主人"],
                "小冰": ["小冰", "女主人", "妈妈"],
                "姥姥": ["姥姥", "岳母", "老年女性"],
                "岳父": ["岳父", "老年男性"],
                "小宝": ["小宝", "儿童", "小男孩", "孩子"],
            }

            seen = []
            for name, keywords in members.items():
                for kw in keywords:
                    if kw in person_text:
                        is_negative = False
                        for neg in negative_patterns:
                            idx = person_text.find(kw)
                            if idx >= 0:
                                context = person_text[max(0, idx - 50):idx + len(kw) + 50]
                                if neg in context:
                                    is_negative = True
                                    break
                        if not is_negative:
                            seen.append(name)
                        break

            if not seen:
                return

            activity = ""
            match = re.search(r"【状态评估】(.*?)(?=【|$)", report, re.DOTALL)
            if match:
                activity = match.group(1).strip()[:200]

            record = {
                "time": now.isoformat(),
                "hour": now.hour,
                "weekday": now.weekday(),
                "people": seen,
                "activity": activity,
                "temp": env_data.get("室内温度", ""),
                "humidity": env_data.get("室内湿度", ""),
            }

            safe_append_jsonl(log_file, record)

            self._log("info", f"家庭活动记录: {', '.join(seen)}")
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            self._log("warn", f"活动记录失败: {e}")

    def learn_door_key_mapping(self, seen_people):
        """通过 VLM + 门锁时间交叉学习指纹映射"""
        try:
            mapping_file = DATA_DIR / "door_key_mapping.json"
            if not mapping_file.exists():
                return

            mapping = json.loads(mapping_file.read_text())

            r_action = self.ha_get("states/sensor.loock_fvl109_559c_lock_action")
            r_key = self.ha_get("states/sensor.loock_fvl109_559c_lock_key_id")

            if r_action.status_code != 200 or r_key.status_code != 200:
                return

            action = r_action.json().get("state", "")
            key_id = str(r_key.json().get("state", ""))
            key_changed = r_key.json().get("last_changed", "")

            if not key_changed or key_id in ("0", "unavailable", "unknown", ""):
                return

            import datetime as dt_mod
            try:
                event_time = datetime.fromisoformat(key_changed.replace("Z", "+00:00"))
                now_utc = datetime.now(dt_mod.timezone.utc)
                if (now_utc - event_time).total_seconds() > 600:
                    return
            except Exception:
                return

            if action != "outside_unlock":
                return

            current = mapping.get(key_id, "待学习")
            if current != "待学习":
                return

            if len(seen_people) == 1:
                person = seen_people[0]
                mapping[key_id] = person
                mapping_file.write_text(json.dumps(mapping, ensure_ascii=False, indent=2))
                self._log("info", f"学习到: key_id {key_id} → {person}")
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            self._log("warn", f"门锁学习失败: {e}")

    # ===== 降级巡查 =====

    def patrol_degraded(self):
        """🔻 降级巡查 — VLM 失败时自动降级为纯传感器模式

        只读取门锁、温湿度、运动检测等传感器数据，不依赖 VLM。
        确保即使视觉分析完全不可用，蜂巢仍能感知基本环境。

        Returns:
            dict: 与 patrol() 相同结构，report 为传感器文本摘要，vlm_skipped=True
        """
        self._log("info", "⚠️ 降级模式：纯传感器巡查（VLM 不可用）")

        env_data = self.get_environment()
        weather = self.get_weather()

        # 组装纯传感器报告
        lines = ["[降级模式] VLM 不可用，以下为纯传感器数据：\n"]
        lines.append("【环境数据】")
        for k, v in env_data.items():
            lines.append(f"  {k}: {v}")

        if weather:
            lines.append(f"\n【天气】{weather['weather']} {weather['current_temp']}°C "
                        f"湿度{weather['humidity']}% 风速{weather['wind_speed']}km/h")
            if weather.get("rain_alerts"):
                lines.append(f"  ⚠️ 降雨预警：{'，'.join(weather['rain_alerts'])}")

        # 运动检测
        try:
            motion_resp = self.ha_get(f"states/{MOTION_SENSOR}")
            if motion_resp.status_code == 200:
                motion_state = motion_resp.json().get("state", "unknown")
                last_changed = motion_resp.json().get("last_changed", "")
                lines.append(f"\n【运动检测】状态: {motion_state}, 最后变化: {last_changed}")
        except Exception:
            lines.append("\n【运动检测】获取失败")

        # 门锁状态
        try:
            door_resp = self.ha_get(f"states/{DOOR_SENSOR}")
            if door_resp.status_code == 200:
                door_state = door_resp.json().get("state", "unknown")
                lines.append(f"【门锁】状态: {door_state}")
        except Exception:
            lines.append("【门锁】获取失败")

        report = "\n".join(lines)
        self._log("info", f"降级巡查完成: {len(report)} 字")

        return {
            "report": report,
            "image_path": None,
            "env_data": env_data,
            "weather": weather,
            "motion_time": None,
            "vlm_skipped": True,
        }

    # ===== 完整巡查流程 =====

    def patrol(self):
        """执行一次完整巡查（两级视觉策略）

        第一级：帧差检测（本地，¥0） → 判断画面是否有变化
        第二级：VLM 深度分析（云端） → 仅在有变化时调用
        VLM 失败时自动降级为 patrol_degraded() 纯传感器模式

        Returns:
            dict: {report, image_path, env_data, weather, motion_time, vlm_skipped} 或 None
        """
        self._log("info", "开始巡查...")

        # 1. 截图
        image_path, motion_time = self.get_camera_snapshot()
        if not image_path:
            self._log("info", "无法获取画面，跳过巡查")
            return None

        # 2. 环境
        env_data = self.get_environment()
        self._log("info", f"环境: {env_data}")

        # 3. 天气
        weather = self.get_weather()

        # 4. 两级视觉策略：帧差检测 → 按需 VLM
        from hive.frame_diff import should_call_vlm

        # 传感器事件检测：运动传感器最近5分钟有触发 或 门锁有变化
        sensor_triggered = False
        if motion_time:
            try:
                from datetime import datetime, timedelta
                mt = datetime.strptime(motion_time[:19], "%Y-%m-%d %H:%M:%S")
                if (datetime.now() - mt).total_seconds() < 300:  # 5分钟内
                    sensor_triggered = True
            except (ValueError, TypeError):
                pass

        door_state = env_data.get("门锁", "")
        if door_state == "open":
            sensor_triggered = True

        should_vlm, reason = should_call_vlm(image_path, sensor_triggered)

        if should_vlm:
            # 第二级：VLM 深度分析（失败时降级为纯传感器模式）
            try:
                report = self.analyze_with_vlm(image_path, env_data, motion_time, weather)
                self._log("info", f"VLM 报告 ({reason}): {report[:100]}...")
                vlm_skipped = False
            except Exception as e:
                self._log("error", f"VLM 调用失败，降级为纯传感器模式: {e}")
                return self.patrol_degraded()
        else:
            # 跳过 VLM，使用轻量报告
            report = f"[帧差检测] 画面无明显变化（{reason}），跳过 VLM 分析。环境数据正常。"
            self._log("info", f"跳过 VLM: {reason}")
            vlm_skipped = True

        # 5. 记录活动（仅 VLM 分析时记录详细活动）
        if not vlm_skipped:
            self.record_family_activity(report, env_data)

        return {
            "report": report,
            "image_path": image_path,
            "env_data": env_data,
            "weather": weather,
            "motion_time": motion_time,
            "vlm_skipped": vlm_skipped,
        }

    # ===== BeeAgent 接口 =====

    def process(self, event):
        """处理事件"""
        event_type = event.get("type", "")
        if event_type == "patrol_request":
            return self.patrol()
        elif event_type == "snapshot_request":
            return self.get_camera_snapshot()
        elif event_type == "env_request":
            return self.get_environment()
        else:
            self._log("warn", f"未知事件类型: {event_type}")
            return None


# ===== 模块级单例 =====
scout = ScoutBee()

# ===== 向后兼容 =====
def ha_get(endpoint):
    return scout.ha_get(endpoint)

def get_camera_snapshot():
    return scout.get_camera_snapshot()

def get_environment():
    return scout.get_environment()

def get_weather():
    return scout.get_weather()

def get_recent_door_facts():
    return scout.get_recent_door_facts()

def analyze_with_vlm(image_path, env_data, motion_time=None, weather=None):
    return scout.analyze_with_vlm(image_path, env_data, motion_time, weather)
