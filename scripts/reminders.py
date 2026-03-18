#!/usr/bin/env python3
"""
🦞 龙虾管家 - 口述提醒系统
用户口述"几点干什么"，到点提醒。

数据文件: ../data/reminders.json
格式:
[
  {
    "id": "uuid",
    "text": "7点开会",
    "remind_at": "2026-03-18T18:45:00",  # 提前15分钟
    "event_at": "2026-03-18T19:00:00",
    "created": "2026-03-18T15:30:00",
    "status": "pending",  # pending / reminded / done
    "repeat": null  # null / "daily" / "weekly" / "weekdays"
  }
]
"""

import json, os, uuid, requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.expanduser('~/.openclaw/workspace/.env.local')))

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
REMINDERS_FILE = os.path.join(DATA_DIR, 'reminders.json')
FEISHU_APP_ID = os.getenv('FEISHU_APP_ID')
FEISHU_APP_SECRET = os.getenv('FEISHU_APP_SECRET')
XIAOFENG_OPEN_ID = 'ou_a9ca7506b002b1be36148b97d622d551'


def load_reminders():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(REMINDERS_FILE):
        with open(REMINDERS_FILE) as f:
            return json.load(f)
    return []


def save_reminders(reminders):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(REMINDERS_FILE, 'w') as f:
        json.dump(reminders, f, ensure_ascii=False, indent=2)


def add_reminder(text, event_time, advance_minutes=15, repeat=None):
    """
    添加提醒
    text: 提醒内容，如 "7点开会"
    event_time: datetime 对象，事件时间
    advance_minutes: 提前多少分钟提醒（默认15）
    repeat: 重复类型 null/daily/weekly/weekdays
    """
    reminders = load_reminders()
    reminder = {
        'id': str(uuid.uuid4())[:8],
        'text': text,
        'remind_at': (event_time - timedelta(minutes=advance_minutes)).isoformat(),
        'event_at': event_time.isoformat(),
        'created': datetime.now().isoformat(),
        'status': 'pending',
        'repeat': repeat,
        'advance_minutes': advance_minutes,
    }
    reminders.append(reminder)
    save_reminders(reminders)
    return reminder


def get_pending_reminders():
    """获取所有待提醒的项目"""
    return [r for r in load_reminders() if r['status'] == 'pending']


def check_and_fire():
    """
    检查是否有到期的提醒，触发飞书通知。
    返回已触发的提醒列表。
    """
    reminders = load_reminders()
    now = datetime.now()
    fired = []
    
    for r in reminders:
        if r['status'] != 'pending':
            continue
        
        remind_at = datetime.fromisoformat(r['remind_at'])
        event_at = datetime.fromisoformat(r['event_at'])
        
        if now >= remind_at:
            # 触发提醒
            minutes_left = max(0, int((event_at - now).total_seconds() / 60))
            
            if minutes_left > 0:
                msg = f"⏰ 提醒：{r['text']}\n还有 {minutes_left} 分钟"
            else:
                msg = f"⏰ 提醒：{r['text']}\n时间到了！"
            
            # 飞书通知
            try:
                token = get_feishu_token()
                requests.post(
                    f'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id',
                    headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                    json={
                        'receive_id': XIAOFENG_OPEN_ID,
                        'msg_type': 'text',
                        'content': json.dumps({'text': msg})
                    }
                )
            except Exception as e:
                print(f"⚠️ 飞书通知失败: {e}")
            
            r['status'] = 'reminded'
            fired.append(r)
            
            # 处理重复提醒
            if r.get('repeat'):
                next_event = event_at
                if r['repeat'] == 'daily':
                    next_event += timedelta(days=1)
                elif r['repeat'] == 'weekly':
                    next_event += timedelta(weeks=1)
                elif r['repeat'] == 'weekdays':
                    next_event += timedelta(days=1)
                    while next_event.weekday() >= 5:  # 跳过周末
                        next_event += timedelta(days=1)
                
                # 创建下一次提醒
                add_reminder(
                    r['text'], next_event,
                    advance_minutes=r.get('advance_minutes', 15),
                    repeat=r['repeat']
                )
    
    save_reminders(reminders)
    return fired


def clear_old_reminders(days=7):
    """清理N天前已完成的提醒"""
    reminders = load_reminders()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    reminders = [r for r in reminders if r['status'] == 'pending' or r.get('event_at', '') > cutoff]
    save_reminders(reminders)


def list_today():
    """列出今天的所有提醒"""
    today = datetime.now().strftime('%Y-%m-%d')
    return [r for r in load_reminders() if r['event_at'].startswith(today)]


def get_feishu_token():
    resp = requests.post(
        'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
        json={'app_id': FEISHU_APP_ID, 'app_secret': FEISHU_APP_SECRET}
    )
    return resp.json()['tenant_access_token']


def format_reminder_list(reminders):
    """格式化提醒列表为可读文本"""
    if not reminders:
        return "没有待处理的提醒"
    lines = []
    for r in sorted(reminders, key=lambda x: x['event_at']):
        t = datetime.fromisoformat(r['event_at']).strftime('%m/%d %H:%M')
        status = '⏳' if r['status'] == 'pending' else '✅'
        repeat = f" (每{'天' if r.get('repeat')=='daily' else '周' if r.get('repeat')=='weekly' else '工作日'})" if r.get('repeat') else ""
        lines.append(f"  {status} {t} {r['text']}{repeat}")
    return '\n'.join(lines)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--check':
        fired = check_and_fire()
        if fired:
            for r in fired:
                print(f"🔔 已提醒: {r['text']}")
        else:
            print("无到期提醒")
    elif len(sys.argv) > 1 and sys.argv[1] == '--list':
        for r in get_pending_reminders():
            t = datetime.fromisoformat(r['event_at']).strftime('%m/%d %H:%M')
            print(f"  ⏳ {t} {r['text']}")
    elif len(sys.argv) > 1 and sys.argv[1] == '--clean':
        clear_old_reminders()
        print("✅ 已清理旧提醒")
    else:
        print("用法: --check (检查并触发) | --list (列出) | --clean (清理)")
