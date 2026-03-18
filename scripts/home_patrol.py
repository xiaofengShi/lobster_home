#!/usr/bin/env python3
"""
🦞 龙虾管家 - 主动看护巡查脚本
功能：摄像头截图 → VLM分析谁在家 → 结合家庭信息判断 → 飞书汇报

🔴 隐私铁律（2026-03-18 晓峰明确要求）：
   - 用户关闭的设备（摄像头/音箱等），绝对不能自己打开
   - 摄像头休眠 → 跳过摄像头，只读被动传感器
   - 20:00后停止摄像头巡查
   - 不允许外人实时调用摄像头
"""

import base64, json, os, sys, requests, subprocess
from datetime import datetime
from dotenv import load_dotenv

# 加载大脑
sys.path.insert(0, os.path.dirname(__file__))
from lobster_brain import learn_person, get_known_people_context, observe_event, log_observation, get_active_habits
from reminders import check_and_fire as check_reminders, get_pending_reminders

load_dotenv(os.path.join(os.path.expanduser('~/.openclaw/workspace/.env.local')))

# ===== 配置 =====
HA_URL = os.getenv('HA_URL', 'http://localhost:8123')
HA_TOKEN = os.getenv('HA_TOKEN')
VLM_API_KEY = os.getenv('VLM_API_KEY')
FEISHU_APP_ID = os.getenv('FEISHU_APP_ID')
FEISHU_APP_SECRET = os.getenv('FEISHU_APP_SECRET')
XIAOFENG_OPEN_ID = 'ou_a9ca7506b002b1be36148b97d622d551'

CAMERA_ENTITY = 'camera.chuangmi_069a01_a9d3_camera_control'

# 家庭成员信息
FAMILY_INFO = """
家庭成员（5人）：
- 晓峰：男主人，AI研究员，工作日上班
- 晓峰的老婆：女主人，工作日上班
- 岳父：老人，白天通常在家
- 岳母（姥姥）：老人，负责接送小宝
- 小宝：5岁男孩，上幼儿园

小宝时间表（工作日）：
- 7:30 出门上幼儿园
- 17:00 姥姥去学校接他
- 17:30 到家

课外课程：
- 周一、周三 18:30 体能课
- 周二、周四、周六 19:30 英语课
"""

# 空调传感器
AC_SENSORS = {
    '主卧': {
        'temp': 'sensor.xiaomi_c13_8713_temperature',
        'humidity': 'sensor.xiaomi_c13_8713_relative_humidity',
    },
    '次卧': {
        'temp': 'sensor.xiaomi_mt7_5657_temperature',
        'humidity': 'sensor.xiaomi_mt7_5657_relative_humidity',
    },
    '另一间': {
        'temp': 'sensor.xiaomi_mt7_ecac_temperature',
        'humidity': 'sensor.xiaomi_mt7_ecac_relative_humidity',
    }
}

DOOR_SENSOR = 'sensor.loock_fvl109_559c_door_state'
MOTION_SENSOR = 'sensor.mi_95329875_message'


def ha_get(endpoint):
    """调用 HA API"""
    resp = requests.get(
        f'{HA_URL}/api/{endpoint}',
        headers={'Authorization': f'Bearer {HA_TOKEN}'},
        timeout=10
    )
    return resp


def get_camera_snapshot():
    """获取摄像头截图，同时返回截图实际时间
    
    🔴 隐私铁律：如果摄像头休眠/关闭/不可用，直接跳过，绝不尝试唤醒
    """
    # 先检查摄像头状态
    state_resp = ha_get(f'states/{CAMERA_ENTITY}')
    motion_time = None
    if state_resp.status_code == 200:
        cam_state = state_resp.json()
        cam_status = cam_state.get('state', 'unavailable')
        attrs = cam_state.get('attributes', {})
        motion_time = attrs.get('motion_video_time', '')
        
        # 🔴 检查摄像头是否被用户关闭/休眠
        if cam_status in ('unavailable', 'off', 'idle', 'unknown'):
            print(f"📷 摄像头状态: {cam_status} — 已被用户关闭或休眠，跳过截图")
            print(f"   🔴 尊重用户设置，不尝试唤醒")
            return None, None
    else:
        print(f"📷 无法获取摄像头状态 ({state_resp.status_code})，跳过截图")
        return None, None
    
    # 检查时间（20:00后不使用摄像头）
    from datetime import datetime
    now = datetime.now()
    if now.hour >= 20 or now.hour < 7:
        print(f"📷 当前 {now.strftime('%H:%M')}，隐私保护时段，跳过摄像头")
        return None, None
    
    resp = ha_get(f'camera_proxy/{CAMERA_ENTITY}')
    if resp.status_code == 200 and len(resp.content) > 1000:
        path = '/tmp/home_patrol_snapshot.jpg'
        with open(path, 'wb') as f:
            f.write(resp.content)
        print(f"📷 截图成功: {len(resp.content)} bytes")
        if motion_time:
            print(f"  📸 画面时间: {motion_time}")
        return path, motion_time
    else:
        print(f"📷 截图失败 ({resp.status_code})，可能摄像头已休眠")
        return None, None


def get_environment():
    """获取环境数据"""
    env = {}
    for room, sensors in AC_SENSORS.items():
        try:
            temp_resp = ha_get(f'states/{sensors["temp"]}')
            hum_resp = ha_get(f'states/{sensors["humidity"]}')
            temp = temp_resp.json().get('state', '?')
            hum = hum_resp.json().get('state', '?')
            env[room] = f'{temp}°C, 湿度{hum}%'
        except:
            env[room] = '获取失败'
    
    try:
        door = ha_get(f'states/{DOOR_SENSOR}').json().get('state', '?')
        env['门锁'] = door
    except:
        env['门锁'] = '获取失败'
    
    try:
        motion = ha_get(f'states/{MOTION_SENSOR}').json().get('state', '?')
        env['最近移动'] = motion
    except:
        env['最近移动'] = '获取失败'
    
    return env


def get_weather():
    """获取北京当前天气和未来几小时预报"""
    try:
        resp = requests.get(
            'https://api.open-meteo.com/v1/forecast',
            params={
                'latitude': 39.9, 'longitude': 116.4,
                'current': 'temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m',
                'hourly': 'precipitation_probability,temperature_2m,weather_code',
                'forecast_days': 1,
                'timezone': 'Asia/Shanghai'
            },
            timeout=10
        )
        data = resp.json()
        current = data['current']
        
        # 天气码对照
        weather_names = {
            0: '晴', 1: '大部晴', 2: '多云', 3: '阴天',
            45: '雾', 48: '霜雾', 51: '小毛毛雨', 53: '毛毛雨', 55: '大毛毛雨',
            61: '小雨', 63: '中雨', 65: '大雨', 71: '小雪', 73: '中雪', 75: '大雪',
            80: '阵雨', 81: '中阵雨', 82: '大阵雨', 95: '雷暴', 96: '冰雹雷暴'
        }
        weather_desc = weather_names.get(current['weather_code'], f"天气码{current['weather_code']}")
        
        weather = {
            'current_temp': current['temperature_2m'],
            'humidity': current['relative_humidity_2m'],
            'wind_speed': current['wind_speed_10m'],
            'weather': weather_desc,
            'weather_code': current['weather_code'],
        }
        
        # 检查未来6小时降水概率
        hourly = data.get('hourly', {})
        now_hour = datetime.now().hour
        rain_alerts = []
        for i, (t, prob, code) in enumerate(zip(
            hourly.get('time', []),
            hourly.get('precipitation_probability', []),
            hourly.get('weather_code', [])
        )):
            hour = int(t[11:13])
            if hour >= now_hour and hour <= now_hour + 6:
                if prob and prob > 40:
                    rain_alerts.append(f"{hour}点降水概率{prob}%")
        
        weather['rain_alerts'] = rain_alerts
        return weather
    except Exception as e:
        print(f"  ⚠️ 天气获取失败: {e}")
        return None


def analyze_with_vlm(image_path, env_data, motion_time=None, weather=None):
    """用 VLM 分析摄像头画面"""
    with open(image_path, 'rb') as f:
        img_b64 = base64.b64encode(f.read()).decode()
    
    now = datetime.now()
    weekday_cn = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]
    
    # 使用画面实际时间（motion_time），而不是当前系统时间
    if motion_time:
        try:
            mt = datetime.strptime(motion_time[:19], '%Y-%m-%d %H:%M:%S')
            time_str = mt.strftime('%H:%M')
        except:
            time_str = now.strftime('%H:%M')
    else:
        time_str = now.strftime('%H:%M')
    
    env_text = '\n'.join([f'  {k}: {v}' for k, v in env_data.items()])
    
    # 天气信息
    weather_text = ""
    if weather:
        weather_text = f"\n室外天气：{weather['weather']}，{weather['current_temp']}°C，湿度{weather['humidity']}%，风速{weather['wind_speed']}km/h"
        if weather.get('rain_alerts'):
            weather_text += f"\n⚠️ 降雨预警：{'，'.join(weather['rain_alerts'])}"
    
    # 获取已认识的人的上下文
    people_context = get_known_people_context()
    
    prompt = f"""你是一个智能家居AI管家"龙虾管家🦞"。

画面拍摄时间：{weekday_cn} {time_str}
室内环境：
{env_text}{weather_text}

{FAMILY_INFO}

{people_context}

请分析这张家庭摄像头画面，给出一份简洁的巡查报告。

巡查报告内容：
1. 【在家人员】描述画面中每个人的外貌特征，并推测是谁。
2. 【状态评估】他们在做什么？状态是否正常？
3. 【环境检查】结合温湿度数据，环境是否舒适？需要调整吗？
4. 【提醒事项】根据当前时间和家庭日程，有什么需要提醒的？
   - 如果快到17:00，提醒姥姥去接小宝
   - 如果今天有课外课，提前30分钟提醒
   - 如果老人长时间没动，建议关注
   - 如果有降雨预警且无人在家，提醒关窗收衣服
   - 如果室外温度骤降/骤升，建议调整空调
   - 如果大风，提醒关窗
5. 【建议】有什么建议？

用中文回答，控制在300字以内。

⚠️【强制要求】
1. 【在家人员】一栏中，禁止只写"1人"或"2人"。必须写成类似"姥姥（粉色家居服、短发）"的格式，即：名字（外貌特征）。
2. 对于画面中的每个人，必须额外输出一行：
PERSON_FEATURE: [衣服颜色 性别 年龄段 发型等特征] | GUESS: [猜测是家里的谁]"""

    resp = requests.post(
        'https://kspmas.ksyun.com/v1/chat/completions',
        headers={'Authorization': f'Bearer {VLM_API_KEY}', 'Content-Type': 'application/json'},
        json={
            'model': 'qwen3-vl-235b-a22b-instruct',
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{img_b64}'}},
                    {'type': 'text', 'text': prompt}
                ]
            }],
            'max_tokens': 600
        },
        timeout=60
    )
    
    data = resp.json()
    if 'choices' in data:
        text = data['choices'][0]['message']['content']
        # 去掉 think 标签
        if '<think>' in text:
            text = text.split('</think>')[-1].strip()
        return text
    else:
        return f"VLM 分析失败: {json.dumps(data, ensure_ascii=False)[:200]}"


def get_feishu_token():
    """获取飞书 tenant access token"""
    resp = requests.post(
        'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
        json={'app_id': FEISHU_APP_ID, 'app_secret': FEISHU_APP_SECRET}
    )
    return resp.json().get('tenant_access_token')


def set_speaker_volume(volume_pct):
    """设置客厅小爱音箱音量（0-100）"""
    headers = {'Authorization': f'Bearer {HA_TOKEN}', 'Content-Type': 'application/json'}
    requests.post(
        f'{HA_URL}/api/services/media_player/volume_set',
        headers=headers,
        json={
            'entity_id': 'media_player.xiaomi_lx06_ef64_play_control',
            'volume_level': volume_pct / 100.0
        },
        timeout=10
    )


def speak_on_speaker(text):
    """让客厅小爱音箱播报文字"""
    headers = {'Authorization': f'Bearer {HA_TOKEN}', 'Content-Type': 'application/json'}
    # 255字符限制
    text = text[:250]
    requests.post(
        f'{HA_URL}/api/services/text/set_value',
        headers=headers,
        json={
            'entity_id': 'text.xiaomi_lx06_ef64_play_text',
            'value': text
        },
        timeout=10
    )


def auto_speak_reminder(report):
    """从巡查报告中提取提醒事项，用音箱口语化播报"""
    import re
    
    # 提取【提醒事项】内容
    match = re.search(r'【提醒事项】(.*?)(?=【|$)', report, re.DOTALL)
    if not match:
        print("  无提醒事项，跳过播报")
        return
    
    reminder_text = match.group(1).strip()
    # 去掉 emoji 和特殊符号，口语化
    reminder_text = re.sub(r'[⚠️📌🔴]', '', reminder_text).strip()
    
    # 控制长度（音箱播报不要太长）
    if len(reminder_text) > 200:
        reminder_text = reminder_text[:200]
    
    speak_text = f"温馨提醒，{reminder_text}"
    
    # 先把音量调到20%（避免太响吓人，晓峰确认）
    print(f"  📢 音量设置: 20%")
    set_speaker_volume(20)
    
    import time
    time.sleep(0.5)
    
    print(f"  🔊 播报: {speak_text[:60]}...")
    speak_on_speaker(speak_text)


def send_feishu_report(report_text, image_path=None, snap_age_note="", motion_time=None):
    """发送飞书汇报"""
    token = get_feishu_token()
    if not token:
        print("❌ 获取飞书 token 失败")
        return
    
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    
    # 发送图片
    if image_path:
        img_resp = requests.post(
            'https://open.feishu.cn/open-apis/im/v1/images',
            headers={'Authorization': f'Bearer {token}'},
            files={'image': open(image_path, 'rb')},
            data={'image_type': 'message'}
        )
        if img_resp.status_code == 200 and img_resp.json().get('code') == 0:
            image_key = img_resp.json()['data']['image_key']
            requests.post(
                f'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id',
                headers=headers,
                json={
                    'receive_id': XIAOFENG_OPEN_ID,
                    'msg_type': 'image',
                    'content': json.dumps({'image_key': image_key})
                }
            )
            print(f"📸 摄像头截图已发送")
    
    # 发送文字报告（压缩换行，紧凑显示）
    # 用画面时间而不是当前时间
    if motion_time:
        try:
            mt = datetime.strptime(motion_time[:19], '%Y-%m-%d %H:%M:%S')
            report_time = mt.strftime('%H:%M')
        except:
            report_time = datetime.now().strftime('%H:%M')
    else:
        report_time = datetime.now().strftime('%H:%M')
    # 把连续换行压成单个，去掉多余空行
    compact = report_text.strip()
    compact = '\n'.join(line for line in compact.split('\n') if line.strip())
    full_text = f"🦞 {report_time} 巡查报告{snap_age_note}\n{compact}"
    
    resp = requests.post(
        f'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id',
        headers=headers,
        json={
            'receive_id': XIAOFENG_OPEN_ID,
            'msg_type': 'text',
            'content': json.dumps({'text': full_text})
        }
    )
    
    if resp.status_code == 200 and resp.json().get('code') == 0:
        print(f"✅ 巡查报告已发送到飞书")
    else:
        print(f"❌ 发送失败: {resp.json()}")


def main():
    print("🦞 龙虾管家 — 主动看护巡查")
    print("=" * 50)
    
    # Step 1: 截图
    print("\n📷 Step 1: 获取摄像头截图...")
    image_path, motion_time = get_camera_snapshot()
    if not image_path:
        print("❌ 无法获取摄像头画面，跳过本次巡查")
        return
    
    # 检查画面是否有变动（与上次巡查对比 motion_time）
    state_file = os.path.join(os.path.dirname(__file__), '..', 'data', '.patrol_state.json')
    last_motion_time = None
    try:
        if os.path.exists(state_file):
            with open(state_file) as f:
                state = json.load(f)
                last_motion_time = state.get('last_motion_time')
    except:
        pass
    
    if motion_time and motion_time == last_motion_time:
        print(f"  📸 画面与上次相同（{motion_time}），无新变动，跳过本次巡查")
        return
    
    # 保存本次 motion_time
    try:
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, 'w') as f:
            json.dump({'last_motion_time': motion_time, 'last_patrol': datetime.now().isoformat()}, f)
    except:
        pass
    
    # 标注截图时效
    snap_age_note = ""
    if motion_time:
        try:
            mt = datetime.strptime(motion_time[:19], '%Y-%m-%d %H:%M:%S')
            age_min = (datetime.now() - mt).total_seconds() / 60
            if age_min > 5:
                snap_age_note = f"（⚠️画面为{int(age_min)}分钟前，非实时）"
                print(f"  ⚠️ 截图已过时 {int(age_min)} 分钟")
        except:
            pass
    
    # Step 2: 获取环境数据 + 天气
    print("🌡️ Step 2: 获取环境数据...")
    env_data = get_environment()
    for k, v in env_data.items():
        print(f"  {k}: {v}")
    
    print("🌤️ Step 2.5: 获取室外天气...")
    weather = get_weather()
    if weather:
        print(f"  室外: {weather['weather']} {weather['current_temp']}°C 风速{weather['wind_speed']}km/h")
        if weather.get('rain_alerts'):
            for alert in weather['rain_alerts']:
                print(f"  ⚠️ {alert}")
    
    # Step 3: VLM 分析
    print("\n🧠 Step 3: VLM 分析画面...")
    report = analyze_with_vlm(image_path, env_data, motion_time, weather)
    print(f"\n--- 分析结果 ---\n{report}\n---")
    
    # Step 3.5: 从 VLM 结果中学习
    print("\n🧠 Step 3.5: 学习...")
    for line in report.split('\n'):
        if 'PERSON_FEATURE:' in line:
            try:
                parts = line.split('PERSON_FEATURE:')[1]
                feature_part, guess_part = parts.split('| GUESS:')
                feature = feature_part.strip()
                guess = guess_part.strip()
                name, is_known = learn_person(feature, guess)
                status = "✅ 已认识" if is_known else "📝 学习中"
                print(f"  {status}: {name} ({feature})")
            except:
                pass
    
    # 记录观察
    log_observation('patrol', f"环境:{env_data}, VLM分析:{report[:200]}")
    
    # 去掉 PERSON_FEATURE 行（不发给用户看）
    clean_report = '\n'.join([l for l in report.split('\n') if 'PERSON_FEATURE:' not in l]).strip()
    report = clean_report
    
    # Step 3.7: 音箱播报（已改为交互式：由主 session 询问用户后再触发）
    # 巡检脚本不再自动播报，保留函数供外部调用
    # auto_speak_reminder() 仍然可用，但不在这里自动触发
    
    # Step 4: 飞书汇报
    print("\n📱 Step 4: 飞书汇报...")
    send_feishu_report(report, image_path, snap_age_note, motion_time)
    
    print("\n✅ 巡查完成！")


# ====================================================================
# 🌅 早间智能播报（摄像头检测到起床活动后触发）
# ====================================================================

MORNING_STATE_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', '.morning_state.json')

def check_morning_broadcast():
    """
    早间智能播报：检测到有人活动后自动播报天气+日程。
    - 7:00-9:00 之间触发
    - 每天只播一次
    - 通过摄像头 motion_time 判断有人活动
    """
    now = datetime.now()
    
    # 只在 7:00-9:00 之间触发
    if now.hour < 7 or now.hour >= 9:
        return False
    
    # 检查今天是否已播报
    today = now.strftime('%Y-%m-%d')
    try:
        if os.path.exists(MORNING_STATE_FILE):
            with open(MORNING_STATE_FILE) as f:
                state = json.load(f)
                if state.get('last_broadcast_date') == today:
                    return False
    except:
        pass
    
    # 检查摄像头是否有最近活动
    try:
        resp = ha_get(f'states/{CAMERA_ENTITY}')
        attrs = resp.json().get('attributes', {})
        motion_time = attrs.get('motion_time', '')
        if not motion_time:
            return False
        
        mt = datetime.strptime(motion_time[:19], '%Y-%m-%d %H:%M:%S')
        age_min = (now - mt).total_seconds() / 60
        
        # 活动时间在15分钟内 = 有人刚起来
        if age_min > 15:
            return False
    except:
        return False
    
    print("🌅 检测到早间活动，触发智能播报...")
    
    # 获取天气
    weather = get_weather()
    if not weather:
        return False
    
    # 构建播报内容
    text = f"早上好晓峰！今天{weather['weather']}，{weather['current_temp']}度"
    
    if weather['wind_speed'] > 20:
        text += f"，风比较大有{weather['wind_speed']}公里每小时"
    
    if weather.get('rain_alerts'):
        text += "。注意今天有雨，记得带伞"
    
    if weather['current_temp'] < 5:
        text += "。今天很冷，多穿点"
    elif weather['current_temp'] > 35:
        text += "。今天很热，注意防暑"
    
    # 空气质量提醒（简单判断：湿度极低=干燥）
    if weather['humidity'] < 20:
        text += "。空气很干燥，多喝水"
    
    # 播报
    try:
        set_speaker_volume(20)
        import time; time.sleep(1)
        speak_on_speaker(text)
        print(f"  🔊 早间播报: {text}")
    except Exception as e:
        print(f"  ⚠️ 音箱播报失败: {e}")
    
    # 同时飞书推送
    try:
        token = get_feishu_token()
        feishu_msg = f"🌅 早间播报\n\n{weather['weather']} {weather['current_temp']}°C 湿度{weather['humidity']}% 风速{weather['wind_speed']}km/h"
        if weather.get('rain_alerts'):
            feishu_msg += f"\n⚠️ {'，'.join(weather['rain_alerts'])}"
        
        requests.post(
            f'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'receive_id': XIAOFENG_OPEN_ID, 'msg_type': 'text',
                  'content': json.dumps({'text': feishu_msg})}
        )
    except:
        pass
    
    # 标记今天已播报
    try:
        os.makedirs(os.path.dirname(MORNING_STATE_FILE), exist_ok=True)
        with open(MORNING_STATE_FILE, 'w') as f:
            json.dump({'last_broadcast_date': today, 'broadcast_time': now.isoformat()}, f)
    except:
        pass
    
    return True


# ====================================================================
# 🚨 异常检测规则
# ====================================================================

def check_anomalies():
    """
    异常检测：检查各种异常情况并通过飞书报警。
    - 凌晨异常活动（2:00-5:00有运动检测）
    - 室温过低/过高
    - 老人长时间无活动
    """
    now = datetime.now()
    alerts = []
    
    # 1. 凌晨异常运动（2:00-5:00）
    if 2 <= now.hour < 5:
        try:
            motion_resp = ha_get(f'states/{MOTION_SENSOR}')
            motion_state = motion_resp.json().get('state', '')
            if 'Motion' in motion_state:
                motion_time = motion_resp.json().get('last_changed', '')
                alerts.append(f"🚨 凌晨{now.hour}点检测到异常运动！运动状态: {motion_state}")
        except:
            pass
    
    # 2. 室温异常
    for room, sensors in AC_SENSORS.items():
        try:
            temp_resp = ha_get(f'states/{sensors["temp"]}')
            temp = float(temp_resp.json().get('state', '0'))
            if temp < 12:
                alerts.append(f"🥶 {room}温度过低: {temp}°C，建议开暖气")
            elif temp > 35:
                alerts.append(f"🥵 {room}温度过高: {temp}°C，建议开空调")
        except:
            pass
    
    # 3. 湿度异常（太干燥影响健康）
    for room, sensors in AC_SENSORS.items():
        try:
            hum_resp = ha_get(f'states/{sensors["humidity"]}')
            hum = float(hum_resp.json().get('state', '0'))
            if hum < 15:
                alerts.append(f"💨 {room}湿度极低: {hum}%，建议开加湿器")
            elif hum > 85:
                alerts.append(f"💧 {room}湿度过高: {hum}%，建议通风")
        except:
            pass
    
    # 4. 门锁状态异常（长时间未锁）
    try:
        door_resp = ha_get(f'states/{DOOR_SENSOR}')
        door_state = door_resp.json().get('state', '')
        if door_state == 'open':
            last_changed = door_resp.json().get('last_changed', '')
            if last_changed:
                from dateutil.parser import parse as parse_dt
                changed_time = parse_dt(last_changed)
                open_min = (datetime.now(changed_time.tzinfo) - changed_time).total_seconds() / 60
                if open_min > 30:
                    alerts.append(f"🚪 门已开启{int(open_min)}分钟，请确认是否正常")
    except:
        pass
    
    # 发送报警
    if alerts:
        print(f"\n🚨 检测到 {len(alerts)} 条异常：")
        for a in alerts:
            print(f"  {a}")
        
        try:
            token = get_feishu_token()
            msg = "🚨 龙虾管家异常报警\n\n" + "\n".join(alerts)
            requests.post(
                f'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id',
                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                json={'receive_id': XIAOFENG_OPEN_ID, 'msg_type': 'text',
                      'content': json.dumps({'text': msg})}
            )
            print("  📱 已通过飞书报警")
        except Exception as e:
            print(f"  ⚠️ 飞书报警失败: {e}")
    
    return alerts


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--morning':
        check_morning_broadcast()
    elif len(sys.argv) > 1 and sys.argv[1] == '--anomaly':
        check_anomalies()
    elif len(sys.argv) > 1 and sys.argv[1] == '--reminders':
        fired = check_reminders()
        for r in fired:
            print(f"🔔 已提醒: {r['text']}")
    else:
        # 每次巡查都顺带检查异常、提醒和早间播报
        check_anomalies()
        fired = check_reminders()
        if fired:
            for r in fired:
                print(f"🔔 已提醒: {r['text']}")
        check_morning_broadcast()
        main()
