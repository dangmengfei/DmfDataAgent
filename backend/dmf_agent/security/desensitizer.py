"""数据脱敏组件（PRD FR13）：客户号 / 手机号 / 身份证 / 银行卡 / 邮箱。

应用于 SQL 结果 `rows`（命中敏感列）与最终答复文本。脱敏为不可逆掩码，保留格式用于统计展示。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


# 列名特征：命中这些 key 的列会被脱敏
_SENSITIVE_COLUMN_HINTS = {
    "mobile": ["mobile", "phone", "手机号", "电话", "联系方式", "tel"],
    "id_card": ["id_card", "idcard", "身份证", "证件号", "cust_cert"],
    "bank_card": ["bank_card", "bankcard", "银行卡", "卡号", "account_no"],
    "customer_no": ["customer_no", "cust_no", "客户号", "客户编号", "cust_id"],
    "email": ["email", "邮箱", "邮件"],
}


class Desensitizer:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.patterns = {
            "mobile": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
            "id_card": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)|(?<!\d)\d{15}(?!\d)"),
            "bank_card": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
            "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
        }

    @staticmethod
    def _mask(value: str, kind: str) -> str:
        if not value:
            return value
        n = len(value)
        if kind == "mobile" and n == 11:
            return value[:3] + "****" + value[-4:]
        if kind == "id_card" and n >= 15:
            return value[:4] + "**********" + value[-4:]
        if kind == "bank_card" and n >= 16:
            return value[:6] + " **** **** " + value[-4:]
        if kind == "email":
            local, _, domain = value.rpartition("@")
            if len(local) > 2:
                return local[:2] + "***@" + domain
            return "***@" + domain
        # 通用：保留首尾各 2 位
        if n <= 4:
            return "*" * n
        return value[:2] + "*" * (n - 4) + value[-2:]

    def detect_column_kind(self, column_name: str) -> Optional[str]:
        col = column_name.lower()
        for kind, hints in _SENSITIVE_COLUMN_HINTS.items():
            if any(h in col for h in hints):
                return kind
        return None

    def mask_text(self, text: str) -> str:
        if not self.enabled or not isinstance(text, str):
            return text
        result = text
        for kind, pat in self.patterns.items():
            result = pat.sub(lambda m: self._mask(m.group(0), kind), result)
        return result

    def mask_row(
        self,
        row: List[Any],
        columns: List[str],
    ) -> List[Any]:
        if not self.enabled:
            return row
        out: List[Any] = []
        for col, val in zip(columns, row):
            kind = self.detect_column_kind(col)
            if kind and isinstance(val, str):
                out.append(self._mask(val, kind))
            else:
                out.append(val)
        return out

    def mask_rows(
        self,
        rows: List[List[Any]],
        columns: List[str],
    ) -> List[List[Any]]:
        if not self.enabled:
            return rows
        return [self.mask_row(r, columns) for r in rows]

    def mask_query_result(self, query_result: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled or not query_result:
            return query_result
        columns = query_result.get("columns") or []
        rows = query_result.get("rows") or []
        # 仅当结果确实含敏感列时才脱敏并打标，避免普通指标结果被误标 desensitized
        sensitive = [c for c in columns if self.detect_column_kind(str(c))]
        if not sensitive:
            return query_result
        masked_rows = self.mask_rows(rows, columns)
        result = dict(query_result)
        result["rows"] = masked_rows
        result["desensitized"] = True
        result["desensitized_columns"] = sensitive
        return result
