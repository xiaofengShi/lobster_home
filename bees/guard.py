#!/usr/bin/env python3
"""
⚔️ 守卫蜂 (Guard Bee) — 安全检测，纯规则引擎

生物学原型：守卫蜂守在巢门口，通过气味识别"自己人"和"入侵者"，
面对威胁立刻蛰。

职责：
- 从侦查蜂的感知报告中检测异常（摔倒/火灾/入侵/争吵）
- 规则引擎判断（不用LLM，零成本、零延迟、100%确定性）
- 否定语境排除（"无跌倒风险"不触发告警）
- 紧急事件直达舞蹈蜂（绕过蜂后）

核心原则：安全这件事不交给概率模型。规则引擎简单但可靠。
成本：¥0 / 延迟 <1ms / 确定性 100%
"""

from datetime import datetime
from pathlib import Path
import sys

# 导入蜂巢基类
sys.path.insert(0, str(Path(__file__).parent.parent))
from hive.bee_base import BeeAgent
from hive.event_bus import event_bus

# 导入舞蹈蜂（紧急路径直达）
from bees.dancer import dancer


# ===== 突发事件关键词配置 =====
EMERGENCY_KEYWORDS = {
    "摔倒": {
        "priority": "🔴",
        "action": "notify+speak",
        "speak": "检测到有人可能摔倒了，请注意安全，需要帮助吗？",
    },
    "跌倒": {
        "priority": "🔴",
        "action": "notify+speak",
        "speak": "检测到有人可能跌倒了，请注意安全，需要帮助吗？",
    },
    "躺在地上": {
        "priority": "🔴",
        "action": "notify+speak",
        "speak": "检测到有人躺在地上，请注意，需要帮助吗？",
    },
    "吵架": {
        "priority": "🟠",
        "action": "notify",
        "speak": "",
    },
    "争吵": {
        "priority": "🟠",
        "action": "notify",
        "speak": "",
    },
    "哭": {
        "priority": "🟡",
        "action": "notify",
        "speak": "",
    },
    "烟": {
        "priority": "🔴",
        "action": "notify+speak",
        "speak": "检测到可能有烟雾，请注意安全！",
    },
    "火": {
        "priority": "🔴",
        "action": "notify+speak",
        "speak": "检测到可能有火情，请注意安全！",
    },
}

# 否定前缀：如果关键词前面紧跟这些词，说明是否定语境
NEGATION_PREFIXES = ["无", "没有", "没", "不", "未", "非", "无明显", "排除"]

# 否定短语模式：关键词出现在这些模式中也应跳过
NEGATION_PATTERNS = ["正常", "安全", "无异常", "无风险", "不存在"]


class GuardBee(BeeAgent):
    """⚔️ 守卫蜂 — 安全检测，纯规则引擎"""

    def __init__(self):
        super().__init__(name="guard", trigger_type="event")

    def check(self, vlm_report_text):
        """检查 VLM 报告中是否有突发事件
        
        Args:
            vlm_report_text: VLM 分析报告文本
            
        Returns:
            dict: 检测到的紧急事件，None 表示安全
            
        关键逻辑：否定语境排除
        - "无跌倒风险" → 跳过
        - "没有摔倒" → 跳过
        - "老人躺在地上" → 触发
        """
        if not vlm_report_text:
            return None

        text = vlm_report_text.lower()

        for keyword, config in EMERGENCY_KEYWORDS.items():
            if keyword not in text:
                continue

            # 找到关键词位置
            idx = text.index(keyword)
            
            # 取关键词前面 10 个字符作为前缀上下文
            prefix_ctx = text[max(0, idx - 10):idx]
            
            # 取关键词所在句子
            sentence_start = max(text.rfind("\n", 0, idx), text.rfind("。", 0, idx), 0)
            sentence = text[sentence_start:idx + len(keyword) + 10]

            # 检查是否为否定语境
            is_negated = False
            
            # 检查前缀否定词
            for neg in NEGATION_PREFIXES:
                if prefix_ctx.rstrip().endswith(neg):
                    is_negated = True
                    break
            
            # 检查句子中的否定模式
            if not is_negated:
                for pat in NEGATION_PATTERNS:
                    if pat in sentence:
                        is_negated = True
                        break

            if is_negated:
                self._log("info", f"关键词「{keyword}」在否定语境中，跳过")
                continue

            # 触发告警！
            self._log("warn", f"检测到紧急事件: {keyword}")
            return {
                "keyword": keyword,
                "priority": config["priority"],
                "action": config["action"],
                "speak_text": config["speak"],
                "report": vlm_report_text[:200],
            }

        # 第一层关键词未命中 → 第二层语义检测兜底
        return self._semantic_check(text)

    def _semantic_check(self, text):
        """第二层：语义相似度检测（关键词漏检的兜底）
        
        使用简单的字符级 n-gram 相似度（不依赖外部模型），
        将 VLM 报告与预定义的紧急句子做模糊匹配。
        
        Returns:
            dict: 检测到的紧急事件，None 表示安全
        """
        # 紧急句子模板（关键词可能漏掉的表述）
        EMERGENCY_SENTENCES = [
            ("老人趴在地上", "fall", "urgent"),
            ("有人瘫坐在地上", "fall", "urgent"),
            ("老人不动了", "fall", "urgent"),
            ("地上躺着一个人", "fall", "urgent"),
            ("浓烟滚滚", "fire", "urgent"),
            ("着火了", "fire", "urgent"),
            ("厨房冒烟", "fire", "high"),
            ("有人闯入", "intrusion", "urgent"),
            ("不认识的人进来了", "intrusion", "high"),
            ("小孩在哭喊求救", "cry", "high"),
            ("激烈争吵", "fight", "high"),
        ]
        
        SIMILARITY_THRESHOLD = 0.65
        
        for sentence, category, priority in EMERGENCY_SENTENCES:
            sim = self._ngram_similarity(text, sentence, n=2)
            if sim > SIMILARITY_THRESHOLD:
                # 再做一次否定语境检查
                idx = 0
                for i in range(len(text) - len(sentence) + 1):
                    chunk = text[i:i+len(sentence)+10]
                    if self._ngram_similarity(chunk, sentence, n=2) > SIMILARITY_THRESHOLD:
                        idx = i
                        break
                
                prefix = text[max(0, idx-10):idx]
                is_negated = any(prefix.rstrip().endswith(neg) for neg in NEGATION_PREFIXES)
                if is_negated:
                    continue
                
                self._log("warn", f"语义检测命中: '{sentence}' (sim={sim:.2f})")
                return {
                    "keyword": f"[语义]{sentence}",
                    "priority": priority,
                    "action": "alert",
                    "speak_text": f"注意！检测到异常情况：{sentence}",
                    "report": text[:200],
                }
        
        return None
    
    @staticmethod
    def _ngram_similarity(text, pattern, n=2):
        """字符级 n-gram 相似度（Jaccard）"""
        def ngrams(s, n):
            return set(s[i:i+n] for i in range(len(s)-n+1)) if len(s) >= n else {s}
        
        ng_text = ngrams(text.lower(), n)
        ng_pat = ngrams(pattern.lower(), n)
        
        if not ng_pat:
            return 0.0
        
        intersection = ng_text & ng_pat
        return len(intersection) / len(ng_pat)  # 以 pattern 为基准

    def handle(self, event):
        """处理紧急事件 — 直达舞蹈蜂（绕过蜂后）
        
        集成置信度评估（改进方案 9.2.5）：
        - auto: 全自动执行通知
        - semi: 执行 + 请求确认
        - confirm: 只通知，等待确认
        
        Args:
            event: check() 返回的紧急事件 dict
        """
        if not event:
            return

        # 置信度评估
        try:
            from hive.confidence import assess_confidence, get_action_for_level
            confidence, level, reason = assess_confidence("emergency", {
                "keyword": event.get("keyword", ""),
                "source": "semantic" if "[语义]" in event.get("keyword", "") else "rule",
                "priority": event.get("priority", "🟡"),
            })
            event["confidence"] = confidence
            event["confidence_level"] = level
            event["confidence_reason"] = reason
        except ImportError:
            confidence, level, reason = 0.7, "semi", "置信度模块不可用"

        self._log("warn", f"🚨 处理紧急事件: {event['keyword']} ({event['priority']}) "
                         f"置信度: {confidence:.0%} → {level}")

        # 发布事件到总线
        event_bus.publish({
            "source": "guard",
            "type": "emergency",
            "intensity": "urgent",
            "payload": event,
        })

        # 根据置信度级别决定动作
        # 1. 飞书紧急通知（直达舞蹈蜂）
        msg = f"🚨 【紧急】家庭突发事件\n\n"
        msg += f"事件：{event['keyword']}\n"
        msg += f"优先级：{event['priority']}\n"
        msg += f"置信度：{confidence:.0%} ({level})\n"
        msg += f"时间：{datetime.now().strftime('%H:%M')}\n"
        msg += f"\n报告摘要：\n{event.get('report', '')[:200]}"
        
        dancer.notify_feishu(msg, skip_dedup=True)

        # 2. 音箱播报（紧急 force=True，忽略夜间静音）
        if "+speak" in event.get("action", "") and event.get("speak_text"):
            dancer.speak(event["speak_text"], force=True)

        self._log("info", f"紧急事件已处理完成")

    def process(self, event):
        """BeeAgent 接口：处理事件"""
        event_type = event.get("type", "")
        payload = event.get("payload", {})

        if event_type == "check_report":
            # 检查 VLM 报告
            result = self.check(payload.get("report_text", ""))
            if result:
                self.handle(result)
            return result
        else:
            self._log("warn", f"未知事件类型: {event_type}")
            return None


# ===== 模块级单例 =====
guard = GuardBee()


# ===== 向后兼容的函数别名 =====
def check_emergency(vlm_report_text):
    """向后兼容：检查紧急事件"""
    return guard.check(vlm_report_text)

def handle_emergency(event):
    """向后兼容：处理紧急事件"""
    return guard.handle(event)
