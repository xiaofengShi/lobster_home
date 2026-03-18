#!/usr/bin/env python3
"""
🦞 龙虾管家 - 语音桥接守护进程
功能：轮询小爱音箱对话记录 → 过滤"龙虾"唤醒词 → 转发到 OpenClaw 处理 → TTS 回复

双人格共存：
- "小爱同学" → 原版小爱处理（设备控制、音乐等）
- "龙虾" → 龙虾管家处理（天气、日程、日报、智能对话）
"""

import asyncio, aiohttp, json, os, sys, time, subprocess
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.expanduser('~/.openclaw/workspace/.env.local')))

# ===== 配置 =====
MI_USER = os.getenv('MI_USER', '18810927533')
MI_PASS = os.getenv('MI_PASS', '1989Woaini!')
MI_DID = os.getenv('MI_DID', '936141368')
MI_HARDWARE = os.getenv('MI_HARDWARE', 'LX06')

WAKE_WORD = '龙虾'  # 唤醒词
POLL_INTERVAL = 2    # 轮询间隔（秒）
CONVERSATION_API = "https://userprofile.mina.mi.com/device_profile/v2/conversation?source=dialogu&hardware={hardware}&timestamp={timestamp}&limit=2"

# ===== 全局状态 =====
last_timestamp = 0
last_request_id = None


async def get_mina_session():
    """创建并登录 MiNA 会话"""
    from miservice import MiAccount, MiNAService
    session = aiohttp.ClientSession()
    account = MiAccount(session, MI_USER, MI_PASS)
    mina = MiNAService(account)
    
    # 获取设备列表触发登录
    devices = await mina.device_list()
    if not devices:
        print("❌ 未找到设备")
        await session.close()
        return None, None, None, None
    
    device_id = devices[0].get('deviceID')
    print(f"✅ 已连接: {devices[0].get('name')} (DID: {device_id})")
    
    return session, account, mina, device_id


async def get_latest_conversation(session, account, device_id, timestamp=0):
    """获取最新对话记录（用 userprofile API）"""
    token = account.token
    cookies = {
        'userId': str(token.get('userId', '')),
        'serviceToken': token.get('serviceToken', ''),
        'deviceId': device_id,
    }
    cookie_str = '; '.join(f'{k}={v}' for k, v in cookies.items())
    
    url = CONVERSATION_API.format(hardware=MI_HARDWARE, timestamp=timestamp)
    headers = {
        'Cookie': cookie_str,
        'User-Agent': 'MiHome/6.0',
    }
    
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get('code') == 0:
                    inner = json.loads(data.get('data', '{}'))
                    records = inner.get('records', [])
                    return records
    except Exception as e:
        print(f"⚠️ 获取对话失败: {e}")
    return []


async def tts_on_speaker(mina, device_id, text):
    """在音箱上播放 TTS"""
    try:
        # 用 micli 方式（更可靠）
        env = os.environ.copy()
        env['MI_USER'] = MI_USER
        env['MI_PASS'] = MI_PASS
        env['MI_DID'] = MI_DID
        
        result = subprocess.run(
            ['/opt/miniconda3/envs/openclaw/bin/micli', '5', text],
            env=env, capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0
    except Exception as e:
        print(f"⚠️ TTS 失败: {e}")
        return False


async def handle_lobster_query(query, mina, device_id):
    """处理龙虾唤醒的查询"""
    print(f"🦞 处理查询: {query}")
    
    # 去掉唤醒词
    clean_query = query
    for prefix in ['龙虾', '龙虾龙虾', '龙虾同学']:
        if clean_query.startswith(prefix):
            clean_query = clean_query[len(prefix):].strip()
            break
    
    if not clean_query:
        await tts_on_speaker(mina, device_id, "我在，请说")
        return
    
    # 发送到飞书（让主 session 的 AI 处理）
    # 同时也在音箱上给一个临时回复
    await tts_on_speaker(mina, device_id, "好的，让我想想")
    
    # 通过 OpenClaw 消息系统发送
    try:
        # 写入一个文件，让主 session 读取
        msg_file = os.path.join(os.path.dirname(__file__), '..', 'data', 'voice_inbox.json')
        os.makedirs(os.path.dirname(msg_file), exist_ok=True)
        
        msg = {
            'query': clean_query,
            'timestamp': datetime.now().isoformat(),
            'source': 'speaker',
            'status': 'pending'
        }
        
        # 追加到消息队列
        messages = []
        if os.path.exists(msg_file):
            try:
                with open(msg_file) as f:
                    messages = json.load(f)
            except:
                messages = []
        
        messages.append(msg)
        with open(msg_file, 'w') as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)
        
        print(f"📨 已写入 voice_inbox: {clean_query}")
        
    except Exception as e:
        print(f"⚠️ 消息转发失败: {e}")
        await tts_on_speaker(mina, device_id, "抱歉，我出了点问题")


async def poll_loop():
    """主轮询循环"""
    global last_timestamp, last_request_id
    
    session, account, mina, device_id = await get_mina_session()
    if not session:
        return
    
    print(f"\n🦞 龙虾管家语音桥接已启动")
    print(f"   唤醒词: \"{WAKE_WORD}\"")
    print(f"   轮询间隔: {POLL_INTERVAL}秒")
    print(f"   按 Ctrl+C 停止\n")
    
    try:
        while True:
            records = await get_latest_conversation(
                session, account, device_id, last_timestamp
            )
            
            for record in records:
                query = record.get('query', '')
                request_id = record.get('requestId', '')
                record_time = record.get('time', 0)
                
                # 跳过已处理的
                if request_id == last_request_id:
                    continue
                
                # 更新时间戳
                if record_time > last_timestamp:
                    last_timestamp = record_time
                last_request_id = request_id
                
                # 检查是否用了龙虾唤醒词
                if query.startswith(WAKE_WORD):
                    print(f"\n🎤 [{datetime.now().strftime('%H:%M:%S')}] 捕获: \"{query}\"")
                    await handle_lobster_query(query, mina, device_id)
                else:
                    # 不是给我的，忽略（让小爱处理）
                    print(f"   [{datetime.now().strftime('%H:%M:%S')}] 小爱处理: \"{query}\"")
            
            await asyncio.sleep(POLL_INTERVAL)
            
    except KeyboardInterrupt:
        print("\n🛑 语音桥接已停止")
    finally:
        await session.close()


def main():
    """入口"""
    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        # 测试模式：只查一次
        async def test():
            session, account, mina, device_id = await get_mina_session()
            if session:
                records = await get_latest_conversation(session, account, device_id, 0)
                print(f"最近对话: {len(records)} 条")
                for r in records:
                    print(f"  🎤 \"{r.get('query', '?')}\"")
                    answers = r.get('answers', [])
                    for a in answers:
                        llm = a.get('llm', {})
                        print(f"  🤖 \"{llm.get('text', '?')[:100]}\"")
                await session.close()
        asyncio.run(test())
    else:
        asyncio.run(poll_loop())


if __name__ == '__main__':
    main()
