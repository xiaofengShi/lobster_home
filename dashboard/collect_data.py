#!/usr/bin/env python3
"""
📊 蜂巢仪表盘数据 API
生成 dashboard/data.json，供前端页面读取。
由 queen.py 每次巡查后调用，或独立运行。
"""

import json, os, sys, time
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter

# 项目路径
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
HONEY_DIR = BASE_DIR / "honey"
DASHBOARD_DIR = BASE_DIR / "dashboard"

sys.path.insert(0, str(BASE_DIR))


def collect_dashboard_data():
    """收集所有蜂巢数据"""
    now = datetime.now()
    data = {
        "generated_at": now.isoformat(),
        "hive": {},
        "bees": {},
        "events": {},
        "family": {},
        "system": {},
    }

    # 1. 蜂巢健康
    try:
        from hive.hive_health import hive_health
        # 确保蜜蜂已注册（独立运行时需要手动注册）
        if not hive_health._bees:
            from bees.scout import scout
            from bees.guard import guard
            from bees.dancer import dancer
            from bees.nurse import nurse
            from bees.builder import builder
            for bee in [dancer, guard, scout, nurse, builder]:
                hive_health.register_bee(bee)
        status = hive_health.get_hive_status()
        data["hive"] = {
            "all_ok": status["all_ok"],
            "bee_count": len(status["bees"]),
            "bees_detail": status["bees"],
        }
    except Exception as e:
        data["hive"] = {"error": str(e)}

    # 2. 全局状态
    try:
        from hive.hive_state import get_hive_state
        data["hive"]["state"] = get_hive_state()
    except Exception:
        data["hive"]["state"] = {"mode": "unknown"}

    # 3. 最近事件统计
    try:
        events_file = DATA_DIR / "events.jsonl"
        if events_file.exists():
            lines = events_file.read_text().strip().split("\n")
            recent_lines = lines[-500:] if len(lines) > 500 else lines
            events = []
            type_counter = Counter()
            dancer_counter = Counter()
            for line in recent_lines:
                if line.strip():
                    try:
                        e = json.loads(line)
                        events.append(e)
                        type_counter[e.get("type", "unknown")] += 1
                        dancer_counter[e.get("dancer", "unknown")] += 1
                    except json.JSONDecodeError:
                        pass

            # 今日事件
            today = now.strftime("%Y-%m-%d")
            today_events = [e for e in events if e.get("timestamp", "")[:10] == today]

            data["events"] = {
                "total": len(lines),
                "recent_500": len(events),
                "today": len(today_events),
                "by_type": dict(type_counter.most_common(20)),
                "by_dancer": dict(dancer_counter.most_common(10)),
                "last_10": events[-10:][::-1],  # 最新的在前
            }
    except Exception as e:
        data["events"] = {"error": str(e)}

    # 4. 家庭活动统计
    try:
        activity_file = DATA_DIR / "family_activity_log.jsonl"
        if activity_file.exists():
            lines = activity_file.read_text().strip().split("\n")
            week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
            today = now.strftime("%Y-%m-%d")

            member_today = Counter()
            member_week = Counter()
            daily_counts = Counter()

            for line in lines[-2000:]:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    ts = entry.get("timestamp", "")[:10]
                    people = entry.get("people", [])
                    if ts == today:
                        for p in people:
                            member_today[p] += 1
                    if ts >= week_ago:
                        daily_counts[ts] += 1
                        for p in people:
                            member_week[p] += 1
                except json.JSONDecodeError:
                    pass

            data["family"] = {
                "today_sightings": dict(member_today.most_common()),
                "week_sightings": dict(member_week.most_common()),
                "daily_activity": dict(sorted(daily_counts.items())),
            }
    except Exception as e:
        data["family"] = {"error": str(e)}

    # 5. 系统信息
    try:
        # 日志文件大小
        log_file = DATA_DIR / "logs" / "hive.log"
        log_size = log_file.stat().st_size if log_file.exists() else 0

        # 最近巡查日志（最后5行）
        patrol_log = Path(os.path.expanduser("~/.openclaw/workspace/logs/lobster-patrol.log"))
        last_patrol_lines = []
        if patrol_log.exists():
            all_lines = patrol_log.read_text().strip().split("\n")
            last_patrol_lines = all_lines[-5:]

        # 知识库
        from bees.builder import builder
        brain = builder.get_brain_status()

        data["system"] = {
            "log_size_kb": round(log_size / 1024, 1),
            "brain": brain,
            "last_patrol_lines": last_patrol_lines,
            "uptime_note": "LaunchAgent 每5分钟触发",
        }
    except Exception as e:
        data["system"] = {"error": str(e)}

    # 6. 巡查时间线（从日志提取）
    try:
        log_file = DATA_DIR / "logs" / "hive.log"
        if log_file.exists():
            log_lines = log_file.read_text().strip().split("\n")
            # 提取今天的巡查记录
            today = now.strftime("%Y-%m-%d")
            patrols = []
            errors_by_day = Counter()
            for line in log_lines[-2000:]:
                try:
                    # 格式: [2026-03-21 18:11:34] [hive.scout] [info] ...
                    if "开始巡查" in line or "巡查完成" in line or "error" in line.lower():
                        ts = line[1:20] if line.startswith("[") else ""
                        level = "error" if "error" in line.lower() else "info"
                        if ts[:10] == today:
                            patrols.append({"time": ts, "level": level, "msg": line[line.rfind("]")+2:].strip()[:100]})
                        if level == "error" and len(ts) >= 10:
                            errors_by_day[ts[:10]] += 1
                except Exception:
                    pass
            
            data["patrol_timeline"] = patrols[-20:]  # 最近20条
            
            # 7天错误趋势
            week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
            data["error_trend"] = {k: v for k, v in sorted(errors_by_day.items()) if k >= week_ago}
        else:
            data["patrol_timeline"] = []
            data["error_trend"] = {}
    except Exception:
        data["patrol_timeline"] = []
        data["error_trend"] = {}

    # 7. 蜂蜜报告列表
    try:
        if HONEY_DIR.exists():
            reports = sorted(HONEY_DIR.glob("*.md"), reverse=True)
            data["honey"] = [{"name": r.name, "size": r.stat().st_size} for r in reports[:10]]
        else:
            data["honey"] = []
    except Exception:
        data["honey"] = []

    return data


def main():
    data = collect_dashboard_data()
    out = DASHBOARD_DIR / "data.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📊 仪表盘数据已更新: {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
