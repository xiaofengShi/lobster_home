#!/usr/bin/env python3
"""
👑 蜂后 (Queen) — LobsterHive 编排器

蜂后不干活，只调度。所有功能由蜜蜂完成。

⚠️ 架构说明（2026-03-22 改进后）：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
主巡查流程采用【直接函数调用链】模式（Anthropic Handoff 推荐）：
  scout.patrol() → guard.check() → dancer.send_patrol_report() → builder.log

不经过事件总线。简单、可调试、零序列化开销。

事件总线仅用于：
1. 异步联动（门锁事件、天气预警）
2. 审计日志（所有事件持久化）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

巡查流程：
  侦查蜂截图+VLM → 守卫蜂安全检测 → 舞蹈蜂通知 → 筑巢蜂记录

附加任务（按时段调度）：
  07:00-09:00  早间播报
  09:30+       日报播报
  19:00-22:00  到家检测
  23:00-01:00  晚间收尾
  全天          异常检测
"""

import json, os, sys, time, re, requests
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from bees.scout import scout
from bees.guard import guard
from bees.dancer import dancer, chinese_ify_for_tts, send_feishu_voice
from bees.nurse import nurse
from bees.builder import builder
from hive.hive_health import hive_health
from hive.event_bus import event_bus
from hive.config import XIAOFENG_OPEN_ID, DATA_DIR, KIDS_SCHEDULE, WEEKDAY_NAMES, DOOR_SENSOR, MOTION_SENSOR
from hive.safe_io import safe_write_json, safe_read_json
from hive.logger import get_logger

logger = get_logger("queen")

# ====================================================================
# 🎛️ 配置开关
# ====================================================================

# 是否使用合并的侦察守卫蜂（改进方案 9.2.4）
# True: 使用 scout_guard 一体化蜜蜂（减少一个进程，降低延迟 5-10ms）
# False: 使用分离的 scout + guard（向后兼容模式）
USE_MERGED_SCOUT_GUARD = False  # 默认关闭，稳定后再开启

# 注册所有蜜蜂
for bee in [dancer, guard, scout, nurse, builder]:
    hive_health.register_bee(bee)

# 如果启用合并蜜蜂，额外注册
if USE_MERGED_SCOUT_GUARD:
    try:
        from bees.scout_guard import scout_guard
        hive_health.register_bee(scout_guard)
        logger.info("✅ 已启用合并的侦察守卫蜂 (scout_guard)")
    except ImportError:
        USE_MERGED_SCOUT_GUARD = False
        logger.warning("⚠️ scout_guard 导入失败，回退到分离模式")

# ====================================================================
# 🐝 事件订阅 — 让蜜蜂通过摇摆舞协议联动（异步场景）
# ====================================================================

def _on_scene_report(event):
    """侦查蜂报告 → 守卫蜂检测 + 筑巢蜂记录"""
    payload = event.get("payload", {})
    report_text = payload.get("report", "")
    if report_text:
        emergency = guard.check(report_text)
        guard.track_run()
        if emergency:
            guard.handle(emergency)
        builder.log_observation("patrol_event", report_text[:200])

def _on_door_unlock(event):
    """门锁事件 → 守卫蜂+哺育蜂+筑巢蜂联动"""
    payload = event.get("payload", {})
    person = payload.get("person", "未知")
    key_id = payload.get("key_id")
    hour = datetime.now().hour

    # 守卫蜂：凌晨未知指纹 = 高危
    if 0 <= hour < 6 and person == "未知":
        dancer.notify_feishu(
            f"🔴 凌晨异常开锁！时间{datetime.now().strftime('%H:%M')} 未知指纹 key={key_id}",
            skip_dedup=True
        )
        dancer.speak("检测到异常开门！请注意安全！", force=True)

    # 哺育蜂：到家检测
    if 17 <= hour <= 22:
        nurse.check_family_arrivals()

    # 筑巢蜂：记录
    builder.log_observation("door_unlock", f"{person} key={key_id}")

def _on_weather_alert(event):
    """天气预警 → 哺育蜂+舞蹈蜂三蜂联动（暴雨场景）"""
    from hive.hive_state import set_hive_mode
    payload = event.get("payload", {})
    alert_type = payload.get("alert", "")

    if "暴雨" in alert_type or "storm" in alert_type.lower():
        set_hive_mode("storm_alert", {"alert": alert_type})
        # 哺育蜂生成关怀消息
        care_msg = nurse.generate_storm_care(payload)
        if care_msg:
            dancer.notify_feishu(care_msg, skip_dedup=True)
            dancer.speak(care_msg[:200])

def _on_mode_change(event):
    """全局模式切换 → 通知所有蜜蜂"""
    payload = event.get("payload", {})
    mode = payload.get("mode", "normal")
    who = payload.get("who", "")
    dancer.notify_feishu(
        f"🐝 蜂巢模式切换 → {mode}" + (f"（{who}）" if who else ""),
        skip_dedup=True
    )

# 注册订阅
event_bus.subscribe("scene_report", _on_scene_report)
event_bus.subscribe("door_unlock", _on_door_unlock)
event_bus.subscribe("weather_alert", _on_weather_alert)
event_bus.subscribe("mode_change", _on_mode_change)

DIGEST_ASK_STATE = DATA_DIR / ".digest_ask_state.json"
MORNING_STATE_FILE = DATA_DIR / ".morning_state.json"
EVENING_STATE_FILE = DATA_DIR / ".evening_state.json"


# ====================================================================
# 核心巡查
# ====================================================================

def full_patrol():
    """完整巡查流程

    当 USE_MERGED_SCOUT_GUARD=True 时，使用合并的侦查守卫蜂一体化模式：
      ScoutGuardBee.patrol_and_check() → 舞蹈蜂通知 → 筑巢蜂记录
    否则使用传统四步调度：
      侦查蜂 → 守卫蜂 → 舞蹈蜂 → 筑巢蜂
    """
    print("=" * 60)
    print("👑 蜂后启动 — LobsterHive 完整巡查")
    print("=" * 60)

    t0 = time.time()

    if USE_MERGED_SCOUT_GUARD:
        # ===== 合并模式：ScoutGuardBee 一体化 =====
        print("\n🔭⚔️ 合并模式: 侦查守卫蜂出动...")
        from bees.scout_guard import scout_guard
        sg_result = scout_guard.patrol_and_check()

        result = sg_result.get("patrol_result")
        emergency = sg_result.get("emergency")
        handled = sg_result.get("handled", False)

        if not result:
            print("  📷 摄像头休眠/关闭，跳过视觉巡查")
            env_data = scout.get_environment()
            print(f"  🌡️ 环境: {env_data}")
            weather = scout.get_weather()
            if weather:
                print(f"  ⛅ 天气: {weather['weather']} {weather['current_temp']}°C")
            report = None
        else:
            report = result["report"]
            image_path = result["image_path"]
            motion_time = result["motion_time"]
            vlm_skipped = result.get("vlm_skipped", False)

            if vlm_skipped:
                print(f"  ⏭️ VLM 跳过（帧差无变化）")
                from hive.frame_diff import get_stats
                stats = get_stats()
                print(f"  📊 帧差统计: 跳过率{stats['skip_rate_pct']}%，已省{stats['skipped']}次VLM")
            else:
                if emergency and handled:
                    print(f"  🚨 紧急事件已自动处理: {emergency['keyword']}")
                else:
                    print("  ✅ 安全检查通过")

                # 舞蹈蜂通知
                print("\n💃 舞蹈蜂通知...")
                dancer.send_patrol_report(report, image_path, motion_time=motion_time)
                dancer.speak_reminder(report)
                dancer.track_run()
                print("  ✅ 通知完成")

                # 筑巢蜂记录
                print("\n📖 筑巢蜂记录...")
                builder.log_observation("patrol", report[:200])
                builder.track_run()
                print("  ✅ 记录完成")
    else:
        # ===== 传统模式：四步调度 =====
        report = _full_patrol_classic()

    total = time.time() - t0
    print(f"\n⏱️ 总耗时: {total:.1f}s")
    print(hive_health.summary())

    # 刷新仪表盘数据
    try:
        from dashboard.collect_data import collect_dashboard_data
        import json
        dash_data = collect_dashboard_data()
        dash_file = Path(__file__).parent / "dashboard" / "data.json"
        dash_file.write_text(json.dumps(dash_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 每小时同步一次到 GitHub Pages
        sync_state_file = DATA_DIR / ".github_sync_state.json"
        last_sync = 0
        try:
            if sync_state_file.exists():
                last_sync = json.loads(sync_state_file.read_text()).get("last_sync", 0)
        except (json.JSONDecodeError, ValueError):
            pass

        if time.time() - last_sync > 3600:  # 1小时
            from dashboard.sync_github import sync_to_github
            if sync_to_github():
                sync_state_file.write_text(json.dumps({"last_sync": time.time()}))
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        print(f"  ⚠️ 仪表盘数据刷新失败: {e}")

    return report


def _full_patrol_classic():
    """传统四步巡查（USE_MERGED_SCOUT_GUARD=False 时使用）"""

    # Step 1: 侦查蜂
    print("\n🔭 Step 1: 侦查蜂出动...")
    result = scout.patrol()
    scout.track_run()

    if not result:
        print("  📷 摄像头休眠/关闭，跳过视觉巡查")
        env_data = scout.get_environment()
        print(f"  🌡️ 环境: {env_data}")
        weather = scout.get_weather()
        if weather:
            print(f"  ⛅ 天气: {weather['weather']} {weather['current_temp']}°C")
        report = None
    else:
        report = result["report"]
        image_path = result["image_path"]
        env_data = result["env_data"]
        weather = result["weather"]
        motion_time = result["motion_time"]
        vlm_skipped = result.get("vlm_skipped", False)
        
        if vlm_skipped:
            print(f"  ⏭️ VLM 跳过（帧差无变化）")
            # VLM 跳过时：不做守卫/舞蹈/筑巢（没有 VLM 报告可分析）
            # 但仍然发布环境数据事件
            from hive.frame_diff import get_stats
            stats = get_stats()
            print(f"  📊 帧差统计: 跳过率{stats['skip_rate_pct']}%，已省{stats['skipped']}次VLM")
        else:
            print(f"  ✅ 侦查完成")

            # Step 2: 守卫蜂
            print("\n⚔️ Step 2: 守卫蜂巡检...")
            emergency = guard.check(report)
            guard.track_run()
            if emergency:
                print(f"  🚨 紧急: {emergency['keyword']}")
                guard.handle(emergency)
            else:
                print("  ✅ 安全")

            # Step 3: 舞蹈蜂
            print("\n💃 Step 3: 舞蹈蜂通知...")
            dancer.send_patrol_report(report, image_path, motion_time=motion_time)
            dancer.speak_reminder(report)
            dancer.track_run()
            print("  ✅ 通知完成")

            # Step 4: 筑巢蜂
            print("\n📖 Step 4: 筑巢蜂记录...")
            builder.log_observation("patrol", report[:200])
            builder.track_run()
            print("  ✅ 记录完成")

    return report


# ====================================================================
# 日报播报
# ====================================================================

def _check_today_digest(token, today):
    """查今日日报是否已生成"""
    folder_token = "RTR0fTZ75le5oWdMwhVcObD0nqd"
    try:
        resp = requests.get(
            f"https://open.feishu.cn/open-apis/drive/v1/files?folder_token={folder_token}&page_size=10&order_by=EditedTime&direction=DESC",
            headers={"Authorization": f"Bearer {token}"}, timeout=10
        )
        if resp.status_code == 200:
            for f_item in resp.json().get("data", {}).get("files", []):
                if today in f_item.get("name", ""):
                    return True, f_item.get("name", "")
    except (requests.RequestException, ConnectionError, TimeoutError, OSError):
        pass
    return False, None


def _get_digest_summary(token, today):
    """获取日报口语化摘要"""
    folder_token = "RTR0fTZ75le5oWdMwhVcObD0nqd"
    try:
        resp = requests.get(
            f"https://open.feishu.cn/open-apis/drive/v1/files?folder_token={folder_token}&page_size=10&order_by=EditedTime&direction=DESC",
            headers={"Authorization": f"Bearer {token}"}, timeout=10
        )
        if resp.status_code != 200:
            return None
        doc_token = None
        for f_item in resp.json().get("data", {}).get("files", []):
            if today in f_item.get("name", ""):
                doc_token = f_item.get("token", "")
                break
        if not doc_token:
            return None

        resp2 = requests.get(
            f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_token}/raw_content",
            headers={"Authorization": f"Bearer {token}"}, timeout=10
        )
        if resp2.status_code != 200:
            return None
        content = resp2.json().get("data", {}).get("content", "")
        lines = [l.strip() for l in content.split("\n")
                 if l.strip() and not l.strip().startswith("🔗") and not l.strip().startswith("http") and len(l.strip()) > 5]
        full_text = "。".join(lines[:15])[:1500]
        return chinese_ify_for_tts(full_text)
    except (requests.RequestException, ConnectionError, TimeoutError, OSError):
        return None


def check_daily_digest_broadcast(report):
    """日报播报：晓峰在家→音箱问，不在家→飞书私信"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    try:
        if DIGEST_ASK_STATE.exists():
            state = json.loads(DIGEST_ASK_STATE.read_text())
            if state.get("last_ask_date") == today:
                return
    except (json.JSONDecodeError, ValueError, KeyError):
        pass

    token = dancer.get_token()
    if not token:
        return

    digest_found, doc_name = _check_today_digest(token, today)
    if not digest_found:
        print("  📰 日报尚未生成")
        return

    print(f"  📰 找到日报: {doc_name}")

    xiaofeng_home = False
    if report:
        home_section = re.search(r"【在家人员】(.*?)(?=【|$)", report, re.DOTALL)
        if home_section:
            home_text = home_section.group(1)
            home_lines = [l for l in home_text.split("\n")
                          if l.strip() and not l.strip().startswith("⚠️") and "PERSON_FEATURE" not in l]
            xiaofeng_home = any("晓峰" in l for l in home_lines)

    if xiaofeng_home:
        print("  📰 晓峰在家 → 🔊 音箱询问")
        dancer.speak("晓峰，今天的AI日报已经准备好了，要我给你读一下吗？")
        digest_text = _get_digest_summary(token, today)
        if digest_text:
            pending_file = DATA_DIR / "pending_briefing.json"
            pending_file.write_text(json.dumps({
                "text": digest_text, "type": "daily_digest",
                "expires": (now + timedelta(hours=2)).isoformat()
            }, ensure_ascii=False))
    else:
        print("  📰 晓峰不在家 → 📱 飞书发送")
        msg = f"📰 今天的 AI 日报已生成：{doc_name}\n\n🦞 感知到你不在家，飞书发你方便查看。下面还有语音摘要～"
        dancer.notify_feishu(msg, skip_dedup=True)
        digest_text = _get_digest_summary(token, today)
        if digest_text:
            send_feishu_voice(f"晓峰，飞书给你发日报了。简单说一下：{digest_text}")

    try:
        DIGEST_ASK_STATE.write_text(json.dumps({
            "last_ask_date": today, "asked_at": now.isoformat(),
            "method": "speaker" if xiaofeng_home else "feishu"
        }))
    except (requests.RequestException, ConnectionError, TimeoutError, OSError):
        pass


# ====================================================================
# 早间播报
# ====================================================================

def check_morning_broadcast():
    """早间智能播报（7:00-9:00，工作日，检测到活动后触发）"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周六日跳过
        return False
    if now.hour < 7 or now.hour >= 9:
        return False

    today = now.strftime("%Y-%m-%d")
    try:
        if MORNING_STATE_FILE.exists():
            state = json.loads(MORNING_STATE_FILE.read_text())
            if state.get("last_broadcast_date") == today:
                return False
    except (json.JSONDecodeError, ValueError, KeyError):
        pass

    # 检查最近活动
    try:
        resp = scout.ha_get(f"states/{scout.CAMERA_ENTITY}")
        attrs = resp.json().get("attributes", {})
        motion_time = attrs.get("motion_time", "")
        if not motion_time:
            return False
        mt = datetime.strptime(motion_time[:19], "%Y-%m-%d %H:%M:%S")
        if (now - mt).total_seconds() / 60 > 15:
            return False
    except (KeyError, IndexError, TypeError, ValueError):
        return False

    print("🌅 检测到早间活动...")
    weather = scout.get_weather()
    if not weather:
        return False

    text = f"早上好晓峰！今天{weather['weather']}，{weather['current_temp']}度"
    if weather["wind_speed"] > 20:
        text += f"，风比较大"
    if weather.get("rain_alerts"):
        text += "。注意今天有雨，记得带伞"

    # 音箱先问
    dancer.set_volume(20)
    time.sleep(1)
    dancer.speak("早上好，今天有简报，要听吗？")
    pending_file = DATA_DIR / "pending_briefing.json"
    pending_file.write_text(json.dumps({
        "text": text, "created": now.isoformat(),
        "expires": (now + timedelta(minutes=30)).isoformat()
    }, ensure_ascii=False))

    # 飞书也推一份
    token = dancer.get_token()
    if token:
        feishu_msg = f"🌅 早间播报\n\n{weather['weather']} {weather['current_temp']}°C 湿度{weather['humidity']}% 风速{weather['wind_speed']}km/h"
        if weather.get("rain_alerts"):
            feishu_msg += f"\n⚠️ {'，'.join(weather['rain_alerts'])}"
        dancer.notify_feishu(feishu_msg, skip_dedup=True)

    try:
        MORNING_STATE_FILE.write_text(json.dumps({
            "last_broadcast_date": today, "broadcast_time": now.isoformat()
        }))
    except (json.JSONDecodeError, ValueError, KeyError):
        pass
    return True


# ====================================================================
# 异常检测
# ====================================================================

def check_anomalies():
    """异常检测（凌晨活动、温度、湿度、门锁）"""
    now = datetime.now()
    alerts = []

    # 凌晨异常
    if 2 <= now.hour < 5:
        try:
            motion_resp = scout.ha_get(f"states/{MOTION_SENSOR}")
            if "Motion" in motion_resp.json().get("state", ""):
                alerts.append(f"🚨 凌晨{now.hour}点检测到异常运动")
        except (KeyError, IndexError, TypeError, ValueError):
            pass

    # 温湿度
    rooms = nurse.get_room_env()
    for room, d in rooms.items():
        if d["temp"] < 12:
            alerts.append(f"🥶 {room}温度{d['temp']:.0f}°C，太冷")
        elif d["temp"] > 35:
            alerts.append(f"🥵 {room}温度{d['temp']:.0f}°C，太热")
        if d["humidity"] and d["humidity"] < 15:
            alerts.append(f"💨 {room}湿度{d['humidity']:.0f}%，太干")
        elif d["humidity"] and d["humidity"] > 85:
            alerts.append(f"💧 {room}湿度{d['humidity']:.0f}%，太湿")

    # 门锁
    try:
        door_resp = scout.ha_get(f"states/{DOOR_SENSOR}")
        door_state = door_resp.json().get("state", "")
        if door_state == "open":
            last_changed = door_resp.json().get("last_changed", "")
            if last_changed:
                from dateutil.parser import parse as parse_dt
                changed_time = parse_dt(last_changed)
                open_min = (datetime.now(changed_time.tzinfo) - changed_time).total_seconds() / 60
                if open_min > 30:
                    alerts.append(f"🚪 门已开{int(open_min)}分钟")
    except (KeyError, IndexError, TypeError, ValueError):
        pass

    if alerts:
        msg = "🚨 龙虾管家异常报警\n\n" + "\n".join(alerts)
        dancer.notify_feishu(msg, skip_dedup=True)
    return alerts


# ====================================================================
# 晚间收尾
# ====================================================================

def next_day_label():
    """返回 (label, target_date)"""
    now = datetime.now()
    if now.hour < 5:
        return "今天", now
    else:
        return "明天", now + timedelta(days=1)


def _check_active_conversation():
    """检查是否在活跃对话中"""
    for fname in ["voice_inbox.json", "voice_bridge_state.json"]:
        fpath = DATA_DIR / fname
        if fpath.exists():
            if (datetime.now().timestamp() - fpath.stat().st_mtime) < 1800:
                return True
    return False


def check_evening_wrapup(force=False):
    """晚间收尾（23:00-01:00）"""
    now = datetime.now()
    hour = now.hour
    today = now.strftime("%Y-%m-%d")

    try:
        if EVENING_STATE_FILE.exists():
            state = json.loads(EVENING_STATE_FILE.read_text())
            if state.get("last_date") == today:
                return False
    except (json.JSONDecodeError, ValueError, KeyError):
        pass

    if not force and hour == 23:
        if _check_active_conversation():
            print("🌙 主人还在对话中，延后")
            return False

    msg_parts = [f"🌙 晚间收尾 | {now.strftime('%H:%M')}\n"]

    # 摄像头
    try:
        r = scout.ha_get(f"states/{scout.CAMERA_ENTITY}")
        cam_state = r.json().get("state", "unknown")
        if cam_state in ("unavailable", "off", "unknown"):
            msg_parts.append("📷 摄像头：已关闭 ✅")
        else:
            msg_parts.append(f"📷 摄像头：{cam_state}")
    except (KeyError, IndexError, TypeError, ValueError):
        msg_parts.append("📷 摄像头：查询失败")

    # 提醒
    label, target_date = next_day_label()
    target_str = target_date.strftime("%Y-%m-%d")
    reminder_file = DATA_DIR / "reminders.json"
    try:
        if reminder_file.exists():
            reminders = json.loads(reminder_file.read_text())
            items = [r for r in reminders if r.get("date", "").startswith(target_str)]
            if items:
                msg_parts.append(f"📋 {label}提醒：{'、'.join([r.get('text', '?') for r in items[:3]])}")
            else:
                msg_parts.append(f"📋 {label}提醒：无待办")
        else:
            msg_parts.append(f"📋 {label}提醒：无待办")
    except (json.JSONDecodeError, ValueError, KeyError):
        msg_parts.append(f"📋 {label}提醒：读取失败")

    # 课外课
    target_wd = target_date.weekday()
    target_class = KIDS_SCHEDULE.get(target_wd)
    msg_parts.append(f"📚 {label}{WEEKDAY_NAMES[target_wd]}：{'小宝' + target_class if target_class else '没课'}")

    # 设备状态
    try:
        r = scout.ha_get("states")
        all_states = r.json()
        lights = [s for s in all_states if s["entity_id"].startswith("light.")]
        on_lights = [s for s in lights if s["state"] == "on"]
        msg_parts.append(f"💡 灯：{len(on_lights)}/{len(lights)} 亮着")
    except (KeyError, IndexError, TypeError, ValueError):
        pass

    # 天气
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast?latitude=39.9&longitude=116.4&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode&timezone=Asia/Shanghai&forecast_days=2",
            timeout=10
        )
        w = r.json()["daily"]
        idx = 0 if label == "今天" else 1
        wmap = {0: "晴", 1: "基本晴", 2: "多云", 3: "阴", 45: "雾", 51: "小雨", 61: "雨", 71: "雪", 95: "雷暴"}
        wname = wmap.get(w["weathercode"][idx], f"code={w['weathercode'][idx]}")
        msg_parts.append(f"🌡️ {label}：{wname} {w['temperature_2m_min'][idx]}°~{w['temperature_2m_max'][idx]}°C 降水{w['precipitation_probability_max'][idx]}%")
    except (requests.RequestException, ConnectionError, TimeoutError, OSError):
        pass

    msg_parts.append("\n晚安 🌙")
    dancer.notify_feishu("\n".join(msg_parts), skip_dedup=True)

    # JSONL 轮转（每天晚间收尾时执行）
    try:
        from hive.rotate import rotate_all
        rot = rotate_all()
        if rot:
            for fname, (before, after) in rot.items():
                if before != after:
                    print(f"  🔄 轮转 {fname}: {before}→{after}")
    except Exception as e:
        logger.warning(f"JSONL 轮转失败: {e}")

    try:
        EVENING_STATE_FILE.write_text(json.dumps({"last_date": today, "time": now.isoformat()}))
    except (requests.RequestException, ConnectionError, TimeoutError, OSError):
        pass
    return True


# ====================================================================
# 主入口
# ====================================================================

def hive_self_heal(dry_run=False):
    """🏥 蜂巢自愈 — 检查异常蜜蜂并自动恢复
    
    Args:
        dry_run: True时只重置不发飞书通知（用于测试）
    """
    issues = hive_health.check_all()
    if not issues:
        return

    for issue in issues:
        bee_name = issue["bee"]
        errors = issue.get("consecutive_errors", 0)
        bee = hive_health.get_bee(bee_name)
        if not bee:
            continue

        if errors >= 3:
            # 3次连续失败 → 重置错误计数（"重启"）+ 通知晓峰
            bee.consecutive_errors = 0
            bee.state = "ready"
            msg = f"⚠️ 蜂巢自愈：{bee_name} 连续失败{errors}次，已自动重置"
            print(msg)
            if not dry_run:
                dancer.notify_feishu(f"🏥 {msg}", skip_dedup=True)
            event_bus.publish({
                "source": "queen", "type": "bee_healed",
                "payload": {"bee": bee_name, "errors": errors}
            })

    # 蜜蜂休眠检测（30天未运行 → 自动标记休眠）
    for name, bee in hive_health._bees.items():
        if bee.state == "ready" and bee.last_run:
            try:
                last = datetime.fromisoformat(bee.last_run)
                if (datetime.now() - last).days >= 30:
                    bee.sleep()
                    print(f"💤 {name} 30天未运行，自动休眠")
            except (ValueError, TypeError):
                pass


def run_all():
    """完整运行：自愈 + (异常检测 ∥ 早间播报) + 巡查 + (日报 ∥ 到家 ∥ 晚间)

    真正的并行编排：独立任务用 ThreadPoolExecutor 并行。
    每一步独立 try-except，单步失败不影响后续步骤。
    含生存模式检测：多只蜜蜂连续失败时进入最小化运行。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    now = datetime.now()
    hour = now.hour
    step_results = {}

    # 0. 蜂巢自愈检查（必须先跑）
    try:
        hive_self_heal()
        step_results["self_heal"] = "ok"
    except Exception as e:
        logger.error(f"自愈检查失败: {e}")
        step_results["self_heal"] = f"error: {e}"

    # 0.5 生存模式检测：检查各蜜蜂健康状态
    survival_mode = False
    try:
        sick_bees = []
        for bee_obj in [scout, guard, dancer, nurse, builder]:
            health = bee_obj.health_check()
            if health.get("consecutive_errors", 0) >= 3:
                sick_bees.append(health["name"])
        if len(sick_bees) >= 2:
            survival_mode = True
            logger.warning(f"⚠️ 生存模式激活！{len(sick_bees)} 只蜜蜂异常: {sick_bees}")
            # 生存模式下只做最基本的巡查和异常检测，跳过非关键任务
            try:
                dancer.notify_feishu(
                    f"🆘 蜂巢进入生存模式\n异常蜜蜂: {'、'.join(sick_bees)}\n"
                    f"仅保留核心巡查和异常检测，非关键任务暂停。",
                    skip_dedup=True
                )
            except Exception:
                pass
    except Exception as e:
        logger.error(f"生存模式检测失败: {e}")

    # 1+2. 异常检测 + 早间播报 — 并行（互不依赖）
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="hive") as pool:
        future_anomaly = pool.submit(check_anomalies)
        future_morning = pool.submit(check_morning_broadcast)
        
        try:
            future_anomaly.result(timeout=30)
            step_results["anomalies"] = "ok"
        except Exception as e:
            logger.error(f"异常检测失败: {e}")
            step_results["anomalies"] = f"error: {e}"
        
        try:
            future_morning.result(timeout=30)
            step_results["morning"] = "ok"
        except Exception as e:
            logger.error(f"早间播报失败: {e}")
            step_results["morning"] = f"error: {e}"

    # 3. 核心巡查（串行，因为后续步骤依赖 report）
    report = None
    try:
        report = full_patrol()
        step_results["patrol"] = "ok"
    except Exception as e:
        logger.error(f"核心巡查失败: {e}")
        step_results["patrol"] = f"error: {e}"

    # 4+5+6. 日报 + 到家 + 晚间 — 并行（互不依赖）
    # 生存模式下跳过非关键任务
    if survival_mode:
        logger.warning("⚠️ 生存模式：跳过日报/到家/晚间等非关键任务")
        step_results["digest"] = "skipped(survival)"
        step_results["arrivals"] = "skipped(survival)"
        step_results["evening"] = "skipped(survival)"
    else:
        def _digest_task():
            if hour >= 9:
                check_daily_digest_broadcast(report)

        def _arrival_task():
            nurse.check_family_arrivals()

        def _evening_task():
            if hour in (23, 0):
                check_evening_wrapup()
            elif hour == 1:
                check_evening_wrapup(force=True)

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="hive") as pool:
            futures = {
                pool.submit(_digest_task): "digest",
                pool.submit(_arrival_task): "arrivals",
                pool.submit(_evening_task): "evening",
            }
            for future in as_completed(futures, timeout=60):
                name = futures[future]
                try:
                    future.result()
                    step_results[name] = "ok"
                except Exception as e:
                    logger.error(f"{name} 失败: {e}")
                    step_results[name] = f"error: {e}"

    # 输出步骤摘要
    errors = [k for k, v in step_results.items() if v != "ok"]
    if errors:
        logger.warning(f"run_all 完成，{len(errors)}步失败: {errors}")
    else:
        logger.info("run_all 完成，全部正常")


# ====================================================================
# 🍯 蜂蜜报告 — 数据驱动的家庭洞察
# ====================================================================

HONEY_DIR = DATA_DIR.parent / "honey"
HONEY_DIR.mkdir(parents=True, exist_ok=True)

def generate_weekly_honey():
    """生成本周蜂蜜报告（家庭健康周报+系统进化记录）"""
    from datetime import timedelta
    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).strftime("%m.%d")
    week_end = now.strftime("%m.%d")

    lines = [f"# 📊 本周蜂蜜报告（{week_start}-{week_end}）\n"]

    # 1. 蜂巢运行统计
    lines.append("## 🐝 蜂巢运行")
    status = hive_health.get_hive_status()
    for name, info in status["bees"].items():
        emoji = "✅" if info["ok"] else "⚠️"
        lines.append(f"- {emoji} **{name}**: 运行 {info['total_runs']} 次, "
                      f"错误 {info['error_count']} 次")

    # 2. 家庭活动摘要（从 family_activity_log 统计）
    lines.append("\n## 👨‍👩‍👦 家庭活动")
    activity_log = DATA_DIR / "family_activity_log.jsonl"
    if activity_log.exists():
        from collections import Counter
        member_counts = Counter()
        week_entries = 0
        try:
            for line in activity_log.read_text().strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    entry_date = entry.get("timestamp", "")[:10]
                    if entry_date >= (now - timedelta(days=7)).strftime("%Y-%m-%d"):
                        week_entries += 1
                        for person in entry.get("people", []):
                            member_counts[person] += 1
                except json.JSONDecodeError:
                    pass
        except (json.JSONDecodeError, ValueError, KeyError):
            pass

        lines.append(f"本周共 {week_entries} 条活动记录")
        for person, count in member_counts.most_common():
            lines.append(f"- **{person}**: 被识别 {count} 次")

    # 3. 安全态势
    lines.append("\n## 🛡️ 安全态势")
    recent_events = event_bus.get_recent(500, "emergency")
    week_emergencies = [e for e in recent_events
                        if e.get("timestamp", "")[:10] >= (now - timedelta(days=7)).strftime("%Y-%m-%d")]
    if week_emergencies:
        lines.append(f"⚠️ 本周 {len(week_emergencies)} 次告警")
        for e in week_emergencies:
            lines.append(f"  - {e.get('timestamp', '')[:16]}: {e.get('payload', {}).get('keyword', '?')}")
    else:
        lines.append("🟢 本周无安全告警")

    # 4. 知识进化
    lines.append("\n## 🧬 知识进化")
    brain_status = builder.get_brain_status()
    lines.append(f"- 认识 {brain_status['known_people']} 人，学习中 {brain_status['learning_people']} 人")
    lines.append(f"- 习惯 {brain_status['habits_discovered']} 个")
    lines.append(f"- 观察记录 {brain_status['log_entries']} 条")

    # 5. 帧差检测统计
    lines.append("\n## 🔭 视觉效率")
    try:
        from hive.frame_diff import get_stats
        fs = get_stats()
        lines.append(f"- 总检查 {fs['total_checks']} 次")
        lines.append(f"- VLM 调用 {fs['vlm_calls']} 次，跳过 {fs['skipped']} 次")
        lines.append(f"- **跳过率 {fs['skip_rate_pct']}%**")
        if fs['vlm_calls'] > 0:
            est_saved = fs['skipped'] * 0.03  # 按 ¥0.03/次估算
            lines.append(f"- 预估节省 ¥{est_saved:.1f}")
    except Exception:
        lines.append("- 帧差统计暂无数据")

    # 6. 通知效果统计
    lines.append("\n## 💃 通知统计")
    tracker_file = DATA_DIR / "notification_tracker.jsonl"
    if tracker_file.exists():
        from collections import Counter
        type_counts = Counter()
        try:
            for line in tracker_file.read_text().strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                    rts = r.get("ts", "")[:10]
                    if rts >= (now - timedelta(days=7)).strftime("%Y-%m-%d"):
                        type_counts[r.get("type", "unknown")] += 1
                except json.JSONDecodeError:
                    pass
        except (OSError, IOError):
            pass
        total_notif = sum(type_counts.values())
        lines.append(f"本周发送 {total_notif} 条通知")
        for ntype, count in type_counts.most_common():
            lines.append(f"- {ntype}: {count} 条")
    else:
        lines.append("- 通知追踪暂无数据")

    report = "\n".join(lines)

    # 保存
    report_file = HONEY_DIR / f"weekly_{now.strftime('%Y-%m-%d')}.md"
    report_file.write_text(report, encoding="utf-8")
    print(f"🍯 周报已生成: {report_file}")

    return report


# ====================================================================
# 🍯 8.4 蜂蜜产出 — 月报 + 门锁分析 + 成本核算
# ====================================================================

def generate_monthly_honey():
    """生成月度蜂蜜报告（安全态势+成本分析+进化日志）
    
    对应文档 8.4：
    - 蜂蜜一：家庭健康周报（已有 weekly）
    - 蜂蜜二：安全态势报告
    - 蜂蜜三：能耗与成本分析
    - 蜂蜜四：进化日志
    """
    now = datetime.now()
    month_start = now.replace(day=1).strftime("%Y-%m-%d")
    month_label = now.strftime("%Y年%m月")
    days_in_month = now.day

    lines = [f"# 🍯 月度蜂蜜报告 — {month_label}\n"]

    # ===== 蜂蜜二：安全态势报告 =====
    lines.append("## 🛡️ 安全态势\n")
    
    events_file = DATA_DIR / "events.jsonl"
    emergencies = []
    total_events = 0
    if events_file.exists():
        try:
            for line in events_file.read_text(encoding="utf-8").strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    ts = e.get("timestamp", "")[:10]
                    if ts >= month_start:
                        total_events += 1
                        if e.get("type") == "emergency":
                            emergencies.append(e)
                except json.JSONDecodeError:
                    pass
        except (OSError, IOError):
            pass
    
    lines.append(f"- 事件总线事件总数: **{total_events}**")
    lines.append(f"- 紧急告警次数: **{len(emergencies)}**")
    
    if emergencies:
        false_alarms = sum(1 for e in emergencies 
                          if e.get("payload", {}).get("confidence_level") == "confirm"
                          or e.get("payload", {}).get("confidence", 0) < 0.5)
        lines.append(f"- 低置信度告警: {false_alarms}")
        if len(emergencies) > 0:
            lines.append(f"- 告警详情:")
            for e in emergencies[-10:]:  # 最多显示10条
                payload = e.get("payload", {})
                lines.append(f"  - {e.get('timestamp', '')[:16]}: "
                            f"{payload.get('keyword', '?')} "
                            f"(置信度: {payload.get('confidence', 'N/A')})")
    else:
        lines.append("- **🟢 本月无安全告警**")

    # ===== 门锁指纹学习进度 =====
    lines.append("\n## 🔑 门锁指纹\n")
    door_map_file = DATA_DIR / "door_key_mapping.json"
    if door_map_file.exists():
        try:
            door_data = json.loads(door_map_file.read_text())
            # 平铺结构：key_id → 描述/dict
            known = {}
            ambiguous = []
            for kid, info in door_data.items():
                if kid.startswith("_"):
                    continue  # 跳过说明字段
                if isinstance(info, str):
                    if "公共" in info or "内部" in info or "所有" in info:
                        ambiguous.append(kid)
                    else:
                        known[kid] = info
                elif isinstance(info, dict):
                    name = info.get("name", info.get("description", "?"))
                    if info.get("ambiguous") or "公共" in name:
                        ambiguous.append(kid)
                    else:
                        known[kid] = name
            
            lines.append(f"- 已确认指纹: **{len(known)}** 个")
            for kid, name in known.items():
                lines.append(f"  - `{kid}` → {name}")
            if ambiguous:
                lines.append(f"- 公共/模糊: {len(ambiguous)} 个")
        except (json.JSONDecodeError, OSError):
            lines.append("- 门锁数据读取失败")
    
    # ===== 蜂蜜三：成本分析 =====
    lines.append("\n## 💰 成本分析\n")
    
    # 从帧差统计估算 VLM 成本
    vlm_cost = 0
    try:
        from hive.frame_diff import get_stats
        fs = get_stats()
        vlm_calls = fs.get("vlm_calls", 0)
        vlm_cost = vlm_calls * 0.03
        lines.append(f"- VLM 调用: {vlm_calls} 次 → ¥{vlm_cost:.1f}")
        lines.append(f"- VLM 跳过（帧差节省）: {fs.get('skipped', 0)} 次")
        saved = fs.get("skipped", 0) * 0.03
        lines.append(f"- 帧差节省: ¥{saved:.1f}")
    except Exception:
        lines.append("- VLM 成本数据暂无")
    
    # 其他成本估算
    other_cost = days_in_month * 0.05  # LLM（关怀+记忆）≈ ¥0.05/天
    total_cost = vlm_cost + other_cost
    daily_avg = total_cost / max(days_in_month, 1)
    
    lines.append(f"- LLM 其他: ≈ ¥{other_cost:.1f}")
    lines.append(f"- **本月总计: ¥{total_cost:.1f}**")
    lines.append(f"- **日均: ¥{daily_avg:.2f}**")
    
    if daily_avg > 10:
        lines.append("- ⚠️ 日均成本 >¥10，建议降低巡查频率")
    elif daily_avg < 5:
        lines.append("- ✅ 成本控制良好")

    # ===== 蜂蜜四：进化日志 =====
    lines.append("\n## 🧬 系统进化\n")
    
    brain_status = builder.get_brain_status()
    lines.append(f"- 认识 {brain_status['known_people']} 人，学习中 {brain_status['learning_people']} 人")
    lines.append(f"- 发现习惯 {brain_status['habits_discovered']} 个")
    lines.append(f"- 累计观察 {brain_status['log_entries']} 条")
    lines.append(f"- 已知设备 {brain_status['devices']} 个")
    
    # 反馈分析（如果有）
    feedback = builder.analyze_feedback(days=days_in_month)
    if feedback.get("notifications", {}).get("total", 0) > 0:
        lines.append(f"\n### 📊 通知效果分析")
        lines.append(f"- 本月通知: {feedback['notifications']['total']} 条")
        for ntype, count in feedback["notifications"].get("by_type", {}).items():
            lines.append(f"  - {ntype}: {count}")
        if feedback.get("dead_letters", {}).get("total", 0) > 0:
            lines.append(f"- 消费失败: {feedback['dead_letters']['total']} 条 "
                        f"({feedback['dead_letters'].get('error_rate', 'N/A')})")
        for suggestion in feedback.get("suggestions", []):
            lines.append(f"- {suggestion}")

    # ===== 蜜蜂生命周期 =====
    lines.append("\n## 🥚 蜜蜂生命周期\n")
    lines.append(hive_health.lifecycle_report())

    report = "\n".join(lines)

    # 保存
    report_file = HONEY_DIR / f"monthly_{now.strftime('%Y-%m')}.md"
    report_file.write_text(report, encoding="utf-8")
    print(f"🍯 月报已生成: {report_file}")

    return report


def generate_honey_all():
    """一键生成所有蜂蜜报告"""
    print("🍯 生成全部蜂蜜报告...")
    weekly = generate_weekly_honey()
    monthly = generate_monthly_honey()
    print(f"\n📁 报告目录: {HONEY_DIR}")
    for f in sorted(HONEY_DIR.glob("*.md")):
        print(f"  📄 {f.name}")
    return {"weekly": weekly, "monthly": monthly}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "--morning":
            check_morning_broadcast()
        elif cmd == "--evening":
            check_evening_wrapup(force=True)
        elif cmd == "--anomaly":
            check_anomalies()
        elif cmd == "--honey":
            report = generate_weekly_honey()
            print(report)
        elif cmd == "--honey-monthly":
            report = generate_monthly_honey()
            print(report)
        elif cmd == "--honey-all":
            generate_honey_all()
        elif cmd == "--lifecycle":
            print(hive_health.lifecycle_report())
        elif cmd == "--mode":
            # 模式切换: queen.py --mode away '{"who":"晓峰"}'
            from hive.hive_state import set_hive_mode
            mode_name = sys.argv[2] if len(sys.argv) > 2 else "normal"
            mode_params = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
            state = set_hive_mode(mode_name, mode_params)
            print(f"🐝 模式已切换: {state['mode']}")
            event_bus.publish({"type": "mode_change", "payload": state})
        elif cmd == "--patrol":
            full_patrol()
        else:
            run_all()
    else:
        run_all()
