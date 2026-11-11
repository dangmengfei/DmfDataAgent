"""输入/输出内容安全守卫（PRD FR13：提示注入 / 越权意图 / 敏感词 / 输出过滤）。

- 输入侧：在问题进入 Agent 主图前做预检；命中高危模式直接返回拦截提示。
- 输出侧：在 `synthesize_answer` 最终输出前做内容过滤，防止 PII/敏感指令泄漏。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


# 提示注入 / 越狱模式（中英）
_PROMPT_INJECTION_PATTERNS = [
    r"ignore\s+(?:previous|above|prior|all)\s+(?:instructions?|prompt|system)",
    r"disregard\s+(?:any|all)\s+(?:constraints?|rules?|safety|guidelines?)",
    r"you\s+are\s+now\s+(?:in\s+)?(?:DAN|developer|admin|root)",
    r"jailbreak",
    r"DAN\s*mode",
    r"开发者模式",
    r"忽略\s*(?:以上|之前|前面|所有)\s*(?:指令|提示|设定|约束|规则)",
    r"忘记\s*(?:之前|上面|所有)\s*(?:指令|设定|要求)",
    r"现在\s*(?:你是|扮演|作为)\s*(?:没有任何限制|不受约束|管理员|开发者)",
    r"绕过\s*(?:权限|安全|审计|限制)",
    r"跳过\s*(?:权限|安全|审计|校验)",
]

# 越权 / 高危行为意图
_PRIVILEGE_ESCALATION_PATTERNS = [
    r"(?:查|看|导出|下载|拉取|获取|访问)\s*(?:所有|全部|人人|别人|其他用户|全行|全公司)",
    r"(?:绕过|跳过|关闭|禁用)\s*(?:权限|脱敏|审计)",
    # 删除/清空：允许"所有/全部"等修饰语；裸英文 DDL/DML
    r"(?:删除|清空|清除)\s*(?:所有|全部|任何|任意)?\s*(?:表|数据库|数据|记录|库表)",
    r"\b(?:drop|truncate)\s+(?:table|database|schema)\b",
    r"\bdelete\s+from\b",
    r"(?:导出|下载)\s*(?:全量|全部|百万|千万|所有)\s*(?:数据|明细)",
    r"(?:客户|用户)\s*(?:明细|名单|联系方式|手机号|身份证)",
    r" without\s+permission",
    r"bypass\s+(?:auth|audit|permission|masking)",
    r"dump\s+(?:all|full|entire)\s+(?:table|database)",
]

# 默认敏感词（可在 SecuritySettings.sensitive_words 追加）
_DEFAULT_SENSITIVE_WORDS = ["机密", "核心机密", "国家秘密", "涉密", "反洗钱", "反恐怖融资"]


class ContentGuard:
    """内容安全守卫。"""

    def __init__(self, extra_sensitive_words: Optional[str] = None) -> None:
        self.injection_re = re.compile(
            "|".join(f"({p})" for p in _PROMPT_INJECTION_PATTERNS), re.IGNORECASE
        )
        self.privilege_re = re.compile(
            "|".join(f"({p})" for p in _PRIVILEGE_ESCALATION_PATTERNS), re.IGNORECASE
        )
        words = list(_DEFAULT_SENSITIVE_WORDS)
        if extra_sensitive_words:
            words.extend([w.strip() for w in extra_sensitive_words.split(",") if w.strip()])
        self.sensitive_words = [w for w in words if w]
        # 输出侧 PII / 敏感信息模式
        self.pii_patterns = {
            "mobile": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
            "id_card": re.compile(r"(?<!\d)\d{15}(?:\d\d[\dXx])?(?!\d)"),
            "bank_card": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
            "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
        }

    def check_input(self, text: str, max_length: int = 4096) -> Dict[str, Any]:
        """输入侧预检。返回 {blocked, reason, risk_level, hits[]}。"""
        if not text:
            return {"blocked": False, "reason": "", "risk_level": "none", "hits": []}
        if len(text) > max_length:
            return {
                "blocked": True,
                "reason": f"输入超长（{len(text)} > {max_length}），已拦截",
                "risk_level": "high",
                "hits": [{"type": "input_too_long", "matched": str(len(text))}],
            }

        hits: List[Dict[str, Any]] = []
        for m in self.injection_re.finditer(text):
            hits.append({"type": "prompt_injection", "matched": m.group(0), "pos": m.start()})
        for m in self.privilege_re.finditer(text):
            hits.append({"type": "privilege_escalation", "matched": m.group(0), "pos": m.start()})
        for word in self.sensitive_words:
            idx = text.lower().find(word.lower())
            if idx >= 0:
                hits.append({"type": "sensitive_word", "matched": word, "pos": idx})

        if hits:
            injection = any(h["type"] == "prompt_injection" for h in hits)
            privilege = any(h["type"] == "privilege_escalation" for h in hits)
            risk_level = "high" if (injection or privilege) else "medium"
            reason_type = "prompt_injection" if injection else ("privilege_escalation" if privilege else "sensitive_word")
            return {
                "blocked": True,
                "reason": f"命中 {reason_type} 规则，已拦截",
                "risk_level": risk_level,
                "hits": hits,
            }
        return {"blocked": False, "reason": "", "risk_level": "none", "hits": []}

    def check_output(self, text: str) -> Dict[str, Any]:
        """输出侧过滤。返回 {safe, hits[], sanitized_text}。"""
        if not text:
            return {"safe": True, "hits": [], "sanitized_text": text}
        hits: List[Dict[str, Any]] = []
        sanitized = text
        for kind, pat in self.pii_patterns.items():
            for m in pat.finditer(text):
                hits.append({"type": f"pii:{kind}", "matched": m.group(0), "pos": m.start()})
                sanitized = sanitized.replace(m.group(0), f"[{kind.upper()}_REDACTED]")

        # 拦截含系统指令/越狱的模型输出
        for m in self.injection_re.finditer(text):
            hits.append({"type": "output_injection", "matched": m.group(0), "pos": m.start()})

        # 默认对输出只脱敏，不拦截；若命中 prompt_injection 则视为不安全
        unsafe = any(h["type"].startswith("output_") for h in hits)
        return {"safe": not unsafe, "hits": hits, "sanitized_text": sanitized}
