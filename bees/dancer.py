#!/usr/bin/env python3
"""
💃 舞蹈蜂 (Dancer Bee) — 所有对外通知的唯一出口

生物学原型：蜜蜂发现花蜜后回巢跳"摇摆舞"（waggle dance），
舞蹈方向=食物方向，持续时长=距离。这是自然界最优雅的消息传递协议。

职责：
- 统一管理所有通知渠道（飞书私信、飞书群、小爱音箱）
- 消息去重（同一事件30分钟内不重复）
- 时段控制（22:00后音箱静音，紧急事件除外）
- 优先级排序（urgent > high > normal）
- 渠道适配（飞书=富文本，音箱=纯语音）

核心原则：单一出口。所有蜜蜂不直接发消息，统一经舞蹈蜂。
"""

import hashlib
import json
import os
import re
import requests
import threading
from datetime import datetime
from pathlib import Path

# 导入蜂巢基类 + 统一配置
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from hive.bee_base import BeeAgent
from hive.event_bus import event_bus
from hive.config import (HA_URL, HA_TOKEN, FEISHU_APP_ID, FEISHU_APP_SECRET,
                          XIAOFENG_OPEN_ID, SPEAKER_ENTITIES, DATA_DIR)
from hive.safe_io import safe_write_json, safe_read_json, safe_append_jsonl

# 音箱实体（从统一配置）
SPEAKER_EXECUTE = SPEAKER_ENTITIES["execute"]
SPEAKER_PLAY = SPEAKER_ENTITIES["play_text"]

# 状态文件
STATE_FILE = DATA_DIR / ".dancer_state.json"


class DancerBee(BeeAgent):
    """💃 舞蹈蜂 — 所有对外通知的唯一出口"""

    def __init__(self):
        super().__init__(name="dancer", trigger_type="event")
        self._sent_hashes = {}  # {content_hash: timestamp}
        self._state_lock = threading.Lock()
        self._load_state()

    def _load_state(self):
        """加载状态（去重记录）"""
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text())
                self._sent_hashes = data.get("sent_hashes", {})
                # 清理30分钟前的记录
                now = datetime.now().timestamp()
                self._sent_hashes = {
                    k: v for k, v in self._sent_hashes.items()
                    if now - v < 1800  # 30分钟
                }
        except (json.JSONDecodeError, ValueError, KeyError):
            pass

    def _save_state(self):
        """保存状态（线程安全）"""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with self._state_lock:
                STATE_FILE.write_text(json.dumps({
                    "sent_hashes": self._sent_hashes,
                    "last_save": datetime.now().isoformat(),
                }, ensure_ascii=False))
        except (json.JSONDecodeError, ValueError, KeyError):
            pass

    def _is_duplicate(self, content, window_sec=1800):
        """检查是否重复（30分钟内，线程安全）"""
        h = hashlib.md5(content.encode()).hexdigest()[:16]
        now = datetime.now().timestamp()
        with self._state_lock:
            if h in self._sent_hashes and (now - self._sent_hashes[h]) < window_sec:
                return True
            self._sent_hashes[h] = now
        self._save_state()
        return False

    # ===== 飞书相关 =====

    _token_cache = None
    _token_expire = 0
    _token_lock = threading.Lock()

    def _get_feishu_token(self):
        """获取飞书 tenant access token（带2小时缓存，线程安全）"""
        import time as _time
        with DancerBee._token_lock:
            now = _time.time()
            if self._token_cache and now < self._token_expire:
                return self._token_cache
            try:
                resp = requests.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
                    timeout=10,
                )
                token = resp.json().get("tenant_access_token")
                if token:
                    DancerBee._token_cache = token
                    DancerBee._token_expire = now + 7000  # 提前200秒刷新
                    self._log("info", "飞书token已缓存")
                return token
            except (KeyError, IndexError, TypeError, ValueError) as e:
                self._log("error", f"获取飞书token失败: {e}")
                return None

    def notify_feishu(self, text, target=None, skip_dedup=False):
        """发送飞书私信（含降级链：飞书→音箱→纯日志）

        Args:
            text: 消息内容
            target: 接收者 open_id，默认晓峰
            skip_dedup: 是否跳过去重检查
        Returns:
            bool: 是否发送成功
        """
        target = target or XIAOFENG_OPEN_ID

        # 去重检查
        if not skip_dedup and self._is_duplicate(text):
            self._log("info", "消息重复，跳过发送")
            return True

        token = self._get_feishu_token()
        if not token:
            # 🔻 降级1：飞书不可用 → 尝试音箱播报
            return self._fallback_to_speaker(text)

        try:
            from hive.retry import resilient_request
            resp = resilient_request(
                "post",
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                max_retries=2,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "receive_id": target,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}),
                },
                timeout=10,
            )
            ok = resp.json().get("code") == 0
            msg_id = resp.json().get("data", {}).get("message_id", "")
            if ok:
                self._log("info", f"飞书发送成功: {text[:50]}...")
                # 发布事件
                event_bus.publish({
                    "source": "dancer",
                    "type": "feishu_sent",
                    "intensity": "normal",
                    "payload": {"text": text[:100], "target": target},
                })
                # 📊 关怀效果追踪
                self._track_notification(text, msg_id, target)
            else:
                self._log("error", f"飞书发送失败: {resp.text[:100]}")
                # 🔻 降级1：飞书 API 返回错误 → 尝试音箱
                return self._fallback_to_speaker(text)
            return ok
        except Exception as e:
            self._log("error", f"飞书发送异常，启动降级链: {e}")
            # 🔻 降级1：飞书异常 → 尝试音箱
            return self._fallback_to_speaker(text)

    def _fallback_to_speaker(self, text):
        """🔻 降级链：飞书失败 → 音箱播报 → 纯日志

        确保消息不会因为渠道故障而完全丢失。
        """
        self._log("warn", "飞书不可用，降级为音箱播报")
        try:
            # 音箱只能播报短文本
            speak_text = text[:200].replace("\n", "。")
            ok = self.speak(speak_text)
            if ok:
                self._log("info", "降级音箱播报成功")
                return True
        except Exception as e:
            self._log("error", f"音箱播报也失败: {e}")

        # 🔻 降级2：音箱也失败 → 纯日志记录（确保消息不丢失）
        self._log("warn", "所有通知渠道均不可用，降级为纯日志")
        try:
            fallback_file = DATA_DIR / "notification_fallback.jsonl"
            safe_append_jsonl(fallback_file, {
                "ts": datetime.now().isoformat(),
                "text": text[:500],
                "reason": "all_channels_down",
            })
        except Exception:
            pass
        return False

    def _track_notification(self, text, msg_id, target):
        """📊 追踪通知效果（类型分类 + 发送记录）"""
        # 分类通知类型
        if any(k in text for k in ["天气", "穿", "降温", "花粉", "下雨"]):
            ntype = "weather_care"
        elif any(k in text for k in ["到家", "回来", "回家"]):
            ntype = "arrival"
        elif any(k in text for k in ["课", "提醒", "准备"]):
            ntype = "schedule"
        elif any(k in text for k in ["🚨", "紧急", "异常", "摔"]):
            ntype = "emergency"
        elif any(k in text for k in ["日报", "播报", "巡查"]):
            ntype = "report"
        else:
            ntype = "general"
        
        record = {
            "ts": datetime.now().isoformat(),
            "type": ntype,
            "msg_id": msg_id,
            "target": target[-6:] if target else "",  # 脱敏
            "text_len": len(text),
            "text_preview": text[:40],
        }
        
        tracker_file = DATA_DIR / "notification_tracker.jsonl"
        try:
            safe_append_jsonl(tracker_file, record)
        except (OSError, IOError):
            pass

    def notify_feishu_with_image(self, image_path, text=None, target=None):
        """发送飞书图文消息（先发图再发文）
        
        Args:
            image_path: 图片路径
            text: 可选的文字说明
            target: 接收者
        """
        target = target or XIAOFENG_OPEN_ID
        token = self._get_feishu_token()
        if not token:
            return False

        # 上传图片
        try:
            with open(image_path, "rb") as f:
                img_resp = requests.post(
                    "https://open.feishu.cn/open-apis/im/v1/images",
                    headers={"Authorization": f"Bearer {token}"},
                    files={"image": f},
                    data={"image_type": "message"},
                    timeout=30,
                )
            if img_resp.status_code != 200 or img_resp.json().get("code") != 0:
                self._log("error", f"图片上传失败: {img_resp.text[:100]}")
                return False
            image_key = img_resp.json()["data"]["image_key"]

            # 发送图片消息
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            requests.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                headers=headers,
                json={
                    "receive_id": target,
                    "msg_type": "image",
                    "content": json.dumps({"image_key": image_key}),
                },
                timeout=10,
            )
            self._log("info", "图片发送成功")

            # 如果有文字，再发文字
            if text:
                self.notify_feishu(text, target, skip_dedup=True)

            return True
        except Exception as e:
            self._log("error", f"图文发送异常: {e}")
            return False

    def send_patrol_report(self, report_text, image_path=None, snap_age_note="", motion_time=None):
        """发送巡查报告（含发送前自检）
        
        从 home_patrol.py send_feishu_report() 迁移过来
        """
        # 发送前自检：画面时间与当前时间差距过大时拦截
        if motion_time:
            try:
                mt = datetime.strptime(motion_time[:19], "%Y-%m-%d %H:%M:%S")
                age_min = (datetime.now() - mt).total_seconds() / 60
                if age_min > 15:
                    self._log("warn", f"画面已过时{int(age_min)}分钟，拦截不发送")
                    return False
                elif age_min > 5 and not snap_age_note:
                    snap_age_note = f"（画面来自{int(age_min)}分钟前）"
            except (KeyError, IndexError, TypeError, ValueError):
                pass

        # 用画面时间而不是当前时间
        if motion_time:
            try:
                mt = datetime.strptime(motion_time[:19], "%Y-%m-%d %H:%M:%S")
                report_time = mt.strftime("%H:%M")
            except (KeyError, IndexError, TypeError, ValueError):
                report_time = datetime.now().strftime("%H:%M")
        else:
            report_time = datetime.now().strftime("%H:%M")

        # 压缩换行
        compact = report_text.strip()
        compact = "\n".join(line for line in compact.split("\n") if line.strip())
        full_text = f"🦞 {report_time} 巡查报告{snap_age_note}\n{compact}"

        # 先发图片
        if image_path and os.path.exists(image_path):
            self.notify_feishu_with_image(image_path, target=XIAOFENG_OPEN_ID)
            self._log("info", "摄像头截图已发送")

        # 再发文字报告
        return self.notify_feishu(full_text, skip_dedup=True)

    # ===== 音箱相关 =====

    def speak(self, text, force=False):
        """音箱播报
        
        Args:
            text: 播报内容（最长250字符）
            force: 是否强制播报（忽略夜间静音）
        Returns:
            bool: 是否成功
            
        小冰睡眠轻，22:00后不播报（除非 force=True 紧急情况）
        双重保险：先 execute_text_directive，失败用 play_text
        """
        hour = datetime.now().hour
        weekday = datetime.now().weekday()
        if not force and weekday >= 5:
            self._log("info", f"周末不音箱播报（减少打扰）")
            return True
        if not force and hour >= 22:
            self._log("info", f"{hour}:00 已过22点，小冰睡眠轻，跳过音箱播报")
            return True  # 返回 True 表示"按预期处理了"

        text = text[:250]  # 字符限制
        headers = {
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json",
        }

        # 方法1: execute_text_directive（主力）
        try:
            resp = requests.post(
                f"{HA_URL}/api/services/text/set_value",
                headers=headers,
                json={"entity_id": SPEAKER_EXECUTE, "value": text},
                timeout=10,
            )
            if resp.status_code == 200:
                self._log("info", f"音箱播报成功: {text[:30]}...")
                event_bus.publish({
                    "source": "dancer",
                    "type": "speaker_played",
                    "intensity": "normal",
                    "payload": {"text": text[:50]},
                })
                return True
        except (requests.RequestException, ConnectionError, TimeoutError, OSError):
            pass

        # 方法2: play_text（备份）
        try:
            resp = requests.post(
                f"{HA_URL}/api/services/text/set_value",
                headers=headers,
                json={"entity_id": SPEAKER_PLAY, "value": text},
                timeout=10,
            )
            return resp.status_code == 200
        except (requests.RequestException, ConnectionError, TimeoutError, OSError) as e:
            self._log("error", f"音箱播报失败: {e}")
            return False

    def set_volume(self, volume_pct):
        """设置音箱音量（0-100）"""
        try:
            headers = {
                "Authorization": f"Bearer {HA_TOKEN}",
                "Content-Type": "application/json",
            }
            requests.post(
                f"{HA_URL}/api/services/media_player/volume_set",
                headers=headers,
                json={
                    "entity_id": "media_player.xiaomi_lx06_ef64_play_control",
                    "volume_level": volume_pct / 100.0,
                },
                timeout=10,
            )
            self._log("info", f"音量设置: {volume_pct}%")
        except Exception as e:
            self._log("error", f"音量设置失败: {e}")

    def speak_reminder(self, report):
        """从巡查报告中提取提醒事项并播报
        
        从 home_patrol.py auto_speak_reminder() 迁移过来
        """
        # 提取【提醒事项】内容
        match = re.search(r"【提醒事项】(.*?)(?=【|$)", report, re.DOTALL)
        if not match:
            self._log("info", "无提醒事项，跳过播报")
            return

        reminder_text = match.group(1).strip()
        # 去掉 emoji 和特殊符号
        reminder_text = re.sub(r"[⚠️📌🔴]", "", reminder_text).strip()
        if not reminder_text:
            return

        if len(reminder_text) > 200:
            reminder_text = reminder_text[:200]

        speak_text = f"温馨提醒，{reminder_text}"

        # 先调低音量
        self.set_volume(20)
        import time
        time.sleep(0.5)

        self._log("info", f"播报提醒: {speak_text[:60]}...")
        self.speak(speak_text)

    def get_token(self):
        """获取飞书 token（公开方法）"""
        return self._get_feishu_token()

    # ===== BeeAgent 接口实现 =====

    def process(self, event):
        """处理通知事件"""
        event_type = event.get("type", "")
        payload = event.get("payload", {})

        if event_type == "notify_feishu":
            return self.notify_feishu(
                payload.get("text", ""),
                payload.get("target"),
            )
        elif event_type == "speak":
            return self.speak(
                payload.get("text", ""),
                payload.get("force", False),
            )
        elif event_type == "patrol_report":
            return self.send_patrol_report(
                payload.get("report_text", ""),
                payload.get("image_path"),
                payload.get("snap_age_note", ""),
                payload.get("motion_time"),
            )
        else:
            self._log("warn", f"未知事件类型: {event_type}")
            return None


# ===== 模块级单例 =====
dancer = DancerBee()

# 向后兼容的函数别名（供旧代码直接 import）
def send_feishu(text):
    """向后兼容：发飞书"""
    return dancer.notify_feishu(text)

def speak(text, force=False):
    """向后兼容：音箱播报"""
    return dancer.speak(text, force)

def send_feishu_report(report_text, image_path=None, snap_age_note="", motion_time=None):
    """向后兼容：发巡查报告"""
    return dancer.send_patrol_report(report_text, image_path, snap_age_note, motion_time)

def speak_on_speaker(text, force=False):
    """向后兼容：音箱播报"""
    return dancer.speak(text, force)

def set_speaker_volume(volume_pct):
    """向后兼容：设音量"""
    return dancer.set_volume(volume_pct)

def get_feishu_token():
    """向后兼容：获取飞书token"""
    return dancer.get_token()

def auto_speak_reminder(report):
    """向后兼容：播报提醒"""
    return dancer.speak_reminder(report)


# ===== 语音消息相关（从 home_patrol.py 搬入） =====

# 常见术语映射（用于 TTS 中文化）
_TERM_MAP = {
    'RLHF': '人类反馈强化学习', 'LLM': '大语言模型', 'RAG': '检索增强生成',
    'GPT': '生成式预训练模型', 'LoRA': '低秩适配微调', 'fine-tuning': '微调',
    'Fine-tuning': '微调', 'pre-training': '预训练', 'post-training': '后训练',
    'multi-modal': '多模态', 'multimodal': '多模态', 'open source': '开源',
    'text-to-image': '文生图', 'text-to-video': '文生视频',
    'text-to-speech': '文转语音', 'speech-to-text': '语音转文字',
    'AI Agent': '智能体', 'AI agent': '智能体', 'benchmark': '基准测试',
    'inference': '推理', 'Inference': '推理', 'transformer': '变换器架构',
    'Transformer': '变换器架构', 'diffusion model': '扩散模型',
    'DPO': '直接偏好优化', 'SFT': '监督微调', 'PPO': '近端策略优化',
    'MoE': '混合专家模型', 'CoT': '思维链', 'chain-of-thought': '思维链',
    'GitHub': '代码托管平台', 'HuggingFace': '模型社区',
    'Hugging Face': '模型社区', 'Product Hunt': '产品发布平台',
    'arXiv': '学术', 'ModelScope': '魔搭社区',
}


def chinese_ify_for_tts(text):
    """将文本中的英文内容转为中文，用于语音播报"""
    import re
    result = text
    for en, zh in _TERM_MAP.items():
        result = result.replace(en, zh)
    result = re.sub(r'https?://\S+', '', result)
    result = re.sub(r'\s{2,}', ' ', result).strip()

    en_chars = sum(1 for c in result if c.isascii() and c.isalpha())
    total_chars = len(result.replace(' ', ''))
    if total_chars > 0 and en_chars / total_chars > 0.1:
        try:
            llm_result = llm_translate_to_chinese(result)
            if llm_result:
                result = llm_result
        except Exception:
            pass
    return result


def llm_translate_to_chinese(text):
    """用 LLM 将混合文本翻译成纯中文口语"""
    api_key = os.getenv('KSC_API_KEY', '')
    if not api_key:
        return None
    prompt = f"""请将以下AI日报摘要完全翻译成中文口语，用于语音播报。
要求：所有英文翻译成中文，口语化，去掉emoji和符号。
原文：
{text[:1200]}"""
    try:
        resp = requests.post(
            'https://kspmas.ksyun.com/v1/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'model': 'claude-haiku', 'messages': [{'role': 'user', 'content': prompt}],
                  'max_tokens': 1500, 'temperature': 0.3},
            timeout=30
        )
        if resp.status_code == 200:
            content = resp.json().get('choices', [{}])[0].get('message', {}).get('content', '')
            return content.strip() if content else None
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return None


def send_feishu_voice(text, target=None):
    """生成语音并通过飞书发送"""
    import subprocess, tempfile
    text = text[:800]
    token = dancer.get_token()
    if not token:
        return False

    mp3_fd, mp3_path = tempfile.mkstemp(suffix='.mp3')
    os.close(mp3_fd)
    opus_fd, opus_path = tempfile.mkstemp(suffix='.opus')
    os.close(opus_fd)

    try:
        tts_script = f"""
import asyncio, edge_tts
async def gen():
    communicate = edge_tts.Communicate({repr(text)}, "zh-CN-YunxiaNeural")
    await communicate.save({repr(mp3_path)})
asyncio.run(gen())
"""
        subprocess.run(['/opt/miniconda3/envs/voice-copilot/bin/python3', '-c', tts_script],
                       timeout=60, capture_output=True)
        if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) < 100:
            return False

        subprocess.run(['ffmpeg', '-y', '-i', mp3_path, '-c:a', 'libopus', '-b:a', '24k', opus_path],
                       timeout=30, capture_output=True)
        if not os.path.exists(opus_path) or os.path.getsize(opus_path) < 100:
            return False

        with open(opus_path, 'rb') as opus_file:
            upload_resp = requests.post(
                'https://open.feishu.cn/open-apis/im/v1/files',
                headers={'Authorization': f'Bearer {token}'},
                files={'file': ('voice.opus', opus_file)},
                data={'file_type': 'opus'}, timeout=30
            )
        if upload_resp.status_code != 200 or upload_resp.json().get('code') != 0:
            return False

        file_key = upload_resp.json()['data']['file_key']
        target_id = target or XIAOFENG_OPEN_ID
        send_resp = requests.post(
            'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'receive_id': target_id, 'msg_type': 'audio',
                  'content': json.dumps({'file_key': file_key})},
            timeout=10
        )
        return send_resp.status_code == 200 and send_resp.json().get('code') == 0
    finally:
        for f in [mp3_path, opus_path]:
            try: os.unlink(f)
            except: pass
