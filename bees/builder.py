#!/usr/bin/env python3
"""
📖 筑巢蜂 (Builder Bee) — 记忆层，积累进化

生物学原型：筑巢蜂用蜂蜡建造六角形蜂巢，每一个蜂房都是
精确计算的最优结构。它们不断修缮、扩展蜂巢。

职责：
- 视觉记忆（认人：VLM 特征 → 确认身份）
- 行为学习（发现家人的日常习惯）
- 观察日志（持续记录）
- 设备自发现（扫描 HA 设备 + 能力边界感知）
- 进化报告（大脑状态总览）

核心原则：不是预设规则，是 观察 → 记住 → 学习 → 行动
"""

import json
import os
import requests
import sys
from datetime import datetime, timedelta
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from hive.bee_base import BeeAgent
from hive.event_bus import event_bus
from hive.config import HA_URL, HA_TOKEN, BRAIN_DIR, DATA_DIR
from hive.safe_io import safe_write_json, safe_read_json, safe_append_jsonl

# ===== 数据路径 =====
PEOPLE_DB = BRAIN_DIR / "people.json"
HABITS_DB = BRAIN_DIR / "habits.json"
OBSERVATIONS_LOG = BRAIN_DIR / "observations.jsonl"
KNOWN_DEVICES_FILE = BRAIN_DIR / "known_devices.json"


class BuilderBee(BeeAgent):
    """📖 筑巢蜂 — 记忆与进化"""

    def __init__(self):
        super().__init__(name="builder", trigger_type="event")

    # ==========================================
    # 1. 视觉记忆 — 认人
    # ==========================================

    def load_people(self):
        if PEOPLE_DB.exists():
            return json.loads(PEOPLE_DB.read_text())
        return {"known_people": [], "pending_identifications": []}

    def save_people(self, data):
        PEOPLE_DB.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def learn_person(self, description, vlm_guess, confidence="low"):
        """从 VLM 分析中学习人物特征
        
        贝叶斯置信度模型：累积证据直到后验概率 > 0.90
        （替代原来的固定"3次确认"）
        """
        db = self.load_people()
        now = datetime.now().isoformat()

        for person in db["known_people"]:
            if self._features_match(description, person["features"]):
                person["last_seen"] = now
                person["seen_count"] += 1
                self.save_people(db)
                return person["name"], True

        for pending in db["pending_identifications"]:
            if self._features_match(description, pending["features"]):
                pending["count"] += 1
                pending["last_seen"] = now
                pending["guesses"].append(vlm_guess)

                # 贝叶斯更新：计算当前置信度
                conf = self._bayesian_confidence(pending)
                pending["confidence_score"] = round(conf, 3)
                
                if conf > 0.90:
                    most_common_name = Counter(pending["guesses"]).most_common(1)[0][0]
                    db["known_people"].append({
                        "name": most_common_name,
                        "features": pending["features"],
                        "first_seen": pending["first_seen"],
                        "last_seen": now,
                        "seen_count": pending["count"],
                        "confidence": "confirmed",
                        "confidence_score": round(conf, 3),
                    })
                    db["pending_identifications"].remove(pending)
                    self.save_people(db)
                    self._log("info", f"确认身份: {most_common_name} (置信度{conf:.1%})")
                    event_bus.publish({
                        "source": "builder", "type": "person_confirmed",
                        "intensity": "normal",
                        "payload": {"name": most_common_name, "confidence": round(conf, 3)},
                    })
                    return most_common_name, True

                self.save_people(db)
                return vlm_guess, False

        db["pending_identifications"].append({
            "features": description,
            "guesses": [vlm_guess],
            "count": 1,
            "first_seen": now,
            "last_seen": now,
            "confidence_score": 0.5,
        })
        self.save_people(db)
        return vlm_guess, False

    def _bayesian_confidence(self, pending):
        """贝叶斯置信度计算
        
        因素：
        1. 观察次数（越多越确定）
        2. VLM 猜测一致性（都猜同一个人 → 更确定）
        3. 时间跨度（不同时段看到 → 更确定，不只是偶然）
        """
        count = pending.get("count", 1)
        guesses = pending.get("guesses", [])
        
        # 先验：0.5（完全不确定）
        prior = 0.5
        
        # 证据 1: 每次观察给一个 likelihood
        # VLM 猜对的概率约 0.7，猜错约 0.3
        if guesses:
            most_common = Counter(guesses).most_common(1)[0]
            consistency = most_common[1] / len(guesses)  # 一致性比例
        else:
            consistency = 0.5
        
        # 单次观察的似然比
        lr_per_obs = 1.5 + consistency  # 一致性高 → 更强的证据
        
        # 累积 N 次观察的后验
        odds = prior / (1 - prior)  # 先验赔率
        for _ in range(count):
            odds *= lr_per_obs
        
        posterior = odds / (1 + odds)  # 转回概率
        
        # 证据 2: 时间跨度奖励（不同日期看到加分）
        try:
            first = datetime.fromisoformat(pending["first_seen"])
            last = datetime.fromisoformat(pending["last_seen"])
            days_span = (last - first).days
            if days_span >= 1:
                posterior = min(posterior * 1.1, 0.99)  # 不同天加 10%
            if days_span >= 3:
                posterior = min(posterior * 1.05, 0.99)  # 3天以上再加 5%
        except (ValueError, TypeError, KeyError):
            pass
        
        return min(posterior, 0.99)

    def _features_match(self, desc1, desc2):
        """简单特征匹配（关键词重叠度>40%）"""
        keywords1 = set(desc1.lower().replace("，", " ").replace("、", " ").split())
        keywords2 = set(desc2.lower().replace("，", " ").replace("、", " ").split())
        if not keywords1 or not keywords2:
            return False
        overlap = len(keywords1 & keywords2) / min(len(keywords1), len(keywords2))
        return overlap > 0.4

    def get_known_people_context(self):
        """生成已认识的人的上下文（给 VLM prompt 用）"""
        db = self.load_people()
        if not db["known_people"]:
            return ""
        lines = ["我已经认识的家人："]
        for p in db["known_people"]:
            lines.append(f"- {p['name']}：{p['features']}（已确认，见过{p['seen_count']}次）")
        return "\n".join(lines)

    # ==========================================
    # 2. 行为学习 — 发现习惯
    # ==========================================

    def load_habits(self):
        if HABITS_DB.exists():
            return json.loads(HABITS_DB.read_text())
        return {"discovered_habits": [], "observations": []}

    def save_habits(self, data):
        HABITS_DB.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def observe_event(self, event_type, details, timestamp=None):
        """记录一次观察事件"""
        if timestamp is None:
            timestamp = datetime.now()

        db = self.load_habits()
        observation = {
            "event": event_type,
            "details": details,
            "time": timestamp.strftime("%H:%M"),
            "weekday": timestamp.weekday(),
            "date": timestamp.strftime("%Y-%m-%d"),
            "timestamp": timestamp.isoformat(),
        }
        db["observations"].append(observation)

        # 只保留最近30天
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        db["observations"] = [o for o in db["observations"] if o["timestamp"] > cutoff]

        self.save_habits(db)
        self._analyze_patterns(db)

    def _analyze_patterns(self, db):
        """分析观察数据，发现重复模式"""
        events = db["observations"]
        if len(events) < 3:
            return

        by_type = {}
        for e in events:
            by_type.setdefault(e["event"], []).append(e)

        for event_type, occurrences in by_type.items():
            if len(occurrences) < 3:
                continue
            existing = [h for h in db["discovered_habits"] if h["event"] == event_type]
            if existing:
                continue

            times = [o["time"] for o in occurrences]
            time_counter = Counter(times)
            most_common_time, count = time_counter.most_common(1)[0]

            if count >= 3:
                weekdays = [o["weekday"] for o in occurrences if o["time"] == most_common_time]
                habit = {
                    "event": event_type,
                    "usual_time": most_common_time,
                    "frequency": f"{count}次/{len(occurrences)}次观察",
                    "weekdays": list(set(weekdays)),
                    "discovered_at": datetime.now().isoformat(),
                    "status": "discovered",
                    "auto_action": None,
                }
                db["discovered_habits"].append(habit)
                self.save_habits(db)
                self._log("info", f"发现新习惯: {event_type} 通常在 {most_common_time}")

    def get_active_habits(self):
        """获取当前时间应触发的习惯"""
        db = self.load_habits()
        now = datetime.now()
        current_weekday = now.weekday()

        triggered = []
        for habit in db.get("discovered_habits", []):
            if habit["status"] in ("discovered", "active"):
                habit_h, habit_m = map(int, habit["usual_time"].split(":"))
                now_minutes = now.hour * 60 + now.minute
                habit_minutes = habit_h * 60 + habit_m
                if abs(now_minutes - habit_minutes) <= 5:
                    if not habit["weekdays"] or current_weekday in habit["weekdays"]:
                        triggered.append(habit)
        return triggered

    # ==========================================
    # 3. 观察日志
    # ==========================================

    def log_observation(self, category, content):
        """追加写入观察日志"""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "category": category,
            "content": content,
        }
        safe_append_jsonl(OBSERVATIONS_LOG, entry)

    # ==========================================
    # 4. 设备自发现
    # ==========================================

    def load_known_devices(self):
        if KNOWN_DEVICES_FILE.exists():
            return json.loads(KNOWN_DEVICES_FILE.read_text())
        return {"devices": {}, "last_scan": None}

    def save_known_devices(self, data):
        KNOWN_DEVICES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def scan_devices(self):
        """扫描 HA 上的所有设备"""
        try:
            resp = requests.get(
                f"{HA_URL}/api/states",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
                timeout=10,
            )
            if resp.status_code != 200:
                return [], []

            entities = resp.json()
            db = self.load_known_devices()

            new_devices = []
            current_ids = set()
            interesting = {"camera", "climate", "light", "switch", "fan",
                          "humidifier", "media_player", "sensor", "text", "remote", "binary_sensor"}

            for entity in entities:
                eid = entity["entity_id"]
                domain = eid.split(".")[0]
                if domain not in interesting:
                    continue

                current_ids.add(eid)
                name = entity.get("attributes", {}).get("friendly_name", eid)
                state = entity.get("state", "unknown")

                if eid not in db["devices"]:
                    caps = self._infer_capabilities(domain, entity)
                    db["devices"][eid] = {
                        "name": name, "domain": domain,
                        "first_seen": datetime.now().isoformat(),
                        "state": state, "capabilities": caps,
                    }
                    new_devices.append({"id": eid, "name": name, "domain": domain, "capabilities": caps})
                    self._log("info", f"发现新设备: {name} ({domain})")
                else:
                    db["devices"][eid]["state"] = state
                    db["devices"][eid]["last_seen"] = datetime.now().isoformat()

            removed = [db["devices"][eid] for eid in db["devices"] if eid not in current_ids]
            db["last_scan"] = datetime.now().isoformat()
            self.save_known_devices(db)
            return new_devices, removed

        except (KeyError, IndexError, TypeError, ValueError) as e:
            self._log("error", f"设备扫描失败: {e}")
            return [], []

    def _infer_capabilities(self, domain, entity):
        """推断设备能力"""
        caps = {
            "camera": ["截图", "视频流", "移动侦测"],
            "climate": ["温度控制", "模式切换", "温湿度读取"],
            "light": ["开关", "亮度调节"],
            "media_player": ["播放控制"],
            "fan": ["风速调节", "开关"],
            "humidifier": ["湿度调节", "开关"],
        }.get(domain, [])

        if domain == "light":
            attrs = entity.get("attributes", {})
            if "color_temp_kelvin" in attrs or "color_temp" in attrs:
                caps.append("色温调节")
        elif domain == "text" and "play_text" in entity["entity_id"]:
            caps = ["TTS语音播报"]
        elif domain == "sensor":
            name = entity.get("attributes", {}).get("friendly_name", "")
            if "温度" in name or "temperature" in name.lower():
                caps = ["温度读取"]
            elif "湿度" in name or "humidity" in name.lower():
                caps = ["湿度读取"]
            elif "门" in name or "door" in name.lower():
                caps = ["门窗状态"]
            elif "电量" in name:
                caps = ["电量读取"]
        return caps

    def assess_capability(self, request_text):
        """评估需求是否可实现"""
        db = self.load_known_devices()
        all_caps = {}
        for eid, dev in db.get("devices", {}).items():
            for cap in dev.get("capabilities", []):
                all_caps.setdefault(cap, []).append(dev["name"])

        requirement_map = {
            "浇水": ("土壤湿度读取", "需要土壤湿度传感器"),
            "窗帘": ("窗帘控制", "需要智能窗帘电机"),
            "烟雾": ("烟雾检测", "需要烟雾报警器"),
            "漏水": ("漏水检测", "需要水浸传感器"),
        }

        for keyword, (needed_cap, suggestion) in requirement_map.items():
            if keyword in request_text.lower():
                if needed_cap in all_caps:
                    return True, f"可以！有: {', '.join(all_caps[needed_cap])}", []
                else:
                    return False, f"目前无法实现", [suggestion]

        return None, "需要分析具体需求", []

    # ==========================================
    # 5. 状态总览
    # ==========================================

    def get_brain_status(self):
        """大脑状态"""
        people_db = self.load_people()
        habits_db = self.load_habits()
        devices_db = self.load_known_devices()

        obs_lines = 0
        if OBSERVATIONS_LOG.exists():
            obs_lines = sum(1 for _ in open(OBSERVATIONS_LOG))

        return {
            "known_people": len(people_db.get("known_people", [])),
            "learning_people": len(people_db.get("pending_identifications", [])),
            "habits_discovered": len(habits_db.get("discovered_habits", [])),
            "total_observations": len(habits_db.get("observations", [])),
            "log_entries": obs_lines,
            "devices": len(devices_db.get("devices", {})),
            "last_device_scan": devices_db.get("last_scan"),
        }

    # ==========================================
    # 6. 反馈回路 — Evaluator-Optimizer 模式
    # ==========================================

    def analyze_feedback(self, days=7):
        """分析通知效果，生成优化建议（每周运行一次）
        
        基于改进方案 9.2.3：增加 Evaluator-Optimizer 反馈回路
        
        分析内容：
        1. 通知发送统计（从 notification_tracker.jsonl）
        2. 消费失败率（从 dead_letters.jsonl）
        3. 高频低价值通知识别
        
        Args:
            days: 分析最近多少天的数据，默认7天
            
        Returns:
            dict: 反馈报告
        """
        from datetime import datetime, timedelta
        from collections import Counter
        import json
        
        now = datetime.now()
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        
        report = {
            "period": f"最近{days}天 ({cutoff} ~ {now.strftime('%Y-%m-%d')})",
            "notifications": {},
            "dead_letters": {},
            "suggestions": [],
            "generated_at": now.isoformat(),
        }
        
        # 1. 通知发送统计
        tracker_file = DATA_DIR / "notification_tracker.jsonl"
        if tracker_file.exists():
            type_counts = Counter()
            channel_counts = Counter()
            total = 0
            try:
                for line in tracker_file.read_text().strip().split("\n"):
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                        ts = r.get("ts", "")[:10]
                        if ts >= cutoff:
                            total += 1
                            type_counts[r.get("type", "unknown")] += 1
                            channel_counts[r.get("channel", "unknown")] += 1
                    except json.JSONDecodeError:
                        pass
            except (OSError, IOError):
                pass
            
            report["notifications"] = {
                "total": total,
                "by_type": dict(type_counts.most_common()),
                "by_channel": dict(channel_counts.most_common()),
            }
            
            # 高频低价值检测
            for ntype, count in type_counts.items():
                if count > 20:  # 一周超过20条
                    report["suggestions"].append(
                        f"⚠️ 「{ntype}」通知一周发送{count}条，考虑降低频率或合并"
                    )
        
        # 2. 消费失败统计
        dead_file = DATA_DIR / "dead_letters.jsonl"
        if dead_file.exists():
            error_counts = Counter()
            subscriber_errors = Counter()
            total_errors = 0
            try:
                for line in dead_file.read_text().strip().split("\n"):
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                        ts = r.get("timestamp", "")[:10]
                        if ts >= cutoff:
                            total_errors += 1
                            error_counts[r.get("event_type", "unknown")] += 1
                            subscriber_errors[r.get("subscriber", "unknown")] += 1
                    except json.JSONDecodeError:
                        pass
            except (OSError, IOError):
                pass
            
            report["dead_letters"] = {
                "total": total_errors,
                "by_event_type": dict(error_counts.most_common()),
                "by_subscriber": dict(subscriber_errors.most_common()),
            }
            
            # 误报率计算
            notif_total = report["notifications"].get("total", 1)
            if notif_total > 0:
                error_rate = total_errors / notif_total
                report["dead_letters"]["error_rate"] = f"{error_rate:.1%}"
                if error_rate > 0.15:
                    report["suggestions"].append(
                        f"🔴 消费失败率 {error_rate:.1%} 超过15%，需检查订阅者逻辑"
                    )
        
        # 3. 通知频率建议
        notif_total = report["notifications"].get("total", 0)
        daily_avg = notif_total / days
        if daily_avg > 10:
            report["suggestions"].append(
                f"📊 日均通知 {daily_avg:.1f} 条，考虑合并同类消息减少打扰"
            )
        
        # 4. 保存报告
        honey_dir = DATA_DIR.parent / "honey"
        honey_dir.mkdir(parents=True, exist_ok=True)
        report_file = honey_dir / f"feedback_{now.strftime('%Y-%m-%d')}.json"
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self._log("info", f"反馈报告已生成: {report_file}")
        
        return report

    # ===== BeeAgent 接口 =====

    def process(self, event):
        event_type = event.get("type", "")
        payload = event.get("payload", {})

        if event_type == "learn_person":
            return self.learn_person(payload.get("description", ""), payload.get("guess", ""))
        elif event_type == "observe":
            return self.observe_event(payload.get("event_type", ""), payload.get("details", ""))
        elif event_type == "scan_devices":
            return self.scan_devices()
        elif event_type == "brain_status":
            return self.get_brain_status()
        else:
            self._log("warn", f"未知事件: {event_type}")
            return None


# 模块单例
builder = BuilderBee()

# 向后兼容（lobster_brain.py 的原始接口）
def learn_person(description, vlm_guess, confidence="low"):
    return builder.learn_person(description, vlm_guess, confidence)

def get_known_people_context():
    return builder.get_known_people_context()

def observe_event(event_type, details, timestamp=None):
    return builder.observe_event(event_type, details, timestamp)

def log_observation(category, content):
    return builder.log_observation(category, content)

def get_active_habits():
    return builder.get_active_habits()

def scan_devices(ha_url=None, ha_token=None):
    return builder.scan_devices()

def get_brain_status():
    return builder.get_brain_status()

def assess_capability(request_text):
    return builder.assess_capability(request_text)

def get_capability_summary():
    db = builder.load_known_devices()
    all_caps = set()
    domains = {}
    for eid, dev in db.get("devices", {}).items():
        for cap in dev.get("capabilities", []):
            all_caps.add(cap)
        d = dev.get("domain", "unknown")
        domains[d] = domains.get(d, 0) + 1
    return {"total_devices": len(db.get("devices", {})), "domains": domains, "capabilities": sorted(list(all_caps)), "last_scan": db.get("last_scan")}
