"""安全沙箱：LLM 输出 / 生成 SQL 的危险模式检查（PRD FR15 安全沙箱）。

本项目不执行 LLM 生成的任意代码，但仍需拦截含系统指令/越权/外联/文件操作的输出，
以及拦截生成 SQL 里的破坏性 DDL / 数据外泄语句。

设计要点（解决"直接扫回答会误伤正常输出"的历史遗留问题）：
- **双层规则**：
  - SQL 层（`_SQL_PATTERNS`）：仅对**生成的取数 SQL** 扫描，覆盖 `drop/truncate/alter/
    delete from/insert into/...` 以及 `select ... into outfile`、`load_file()` 等数据外泄写法。
  - 文本层（`_TEXT_PATTERNS` = `_INJECTION_PATTERNS` + `_CODE_PATTERNS`）：对**最终答复文本**
    与**可执行脚本**扫描，覆盖提示注入回声、`os.system`/`eval`/`exec`、外联
    `curl/wget/requests`、文件系统越权（`/etc/passwd`、`../../`）等。
  - **文档层（`scan_doc`）**：只对**说明文档 / SKILL.md** 应用注入类规则（`_INJECTION_PATTERNS`）
    ——文档里出现 `subprocess.run`、`curl` 这类说明性代码示例属正常，不因此拒绝整包上传；
    但文档正文会喂给 LLM，注入类规则仍必须生效。
  - 关键：**文本层刻意不含** `drop table|delete from` 这类 SQL 关键字，因此像
    "请删除该过滤条件" 这样的正常中文答复不会被误伤。
- **规则路径可配置**（`rule_path` / `DMF_HARNESS-SANDBOX_RULE_PATH`）：
  支持 YAML / JSON / TXT 三种格式，可热插拔自定义危险模式而无需改代码；文件缺失只告警不阻断。
- 生产链路接入点：
  - 取数 SQL：`dmf_agent.semantic.starrocks.fetch`（scan_sql，纵深防御）
  - 最终答复：`dmf_agent.agents.metric_agent.nodes.synthesize_answer._finalize_answer`（scan_text）
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from dmf_agent.config.settings import HarnessSettings, get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 内置规则（双层）
# ---------------------------------------------------------------------------

# SQL 层：仅在「生成 SQL」上下文扫描。覆盖破坏性 DDL/DML + 数据外泄写法。
# 注：StarRocks 已有 assert_read_only 拦截非 SELECT 前缀与 insert/update/delete 等关键字，
# 这里额外兜住 select ... into outfile / load_file() 等「select 前缀但外泄」的缝隙。
_SQL_PATTERNS = [
    r"drop\s+table",
    r"drop\s+database",
    r"truncate\s+table",
    r"alter\s+table",
    r"alter\s+database",
    r"delete\s+from\s+\w+",          # 裸删行（带表名）
    r"insert\s+into",
    r"replace\s+into",
    r"update\s+\w+\s+set",
    r"merge\s+into",
    r"grant\s+",
    r"revoke\s+",
    r"create\s+(?:table|database|view|user|index)",
    r"set\s+password",
    r"into\s+outfile",               # 数据外泄：导出到文件
    r"into\s+dumpfile",
    r"load\s+data",
    r"load_file\s*\(",
]

# 文本层：仅在「答复文本」上下文扫描。刻意不含 drop/delete/truncate 等 SQL 关键字，
# 以避免对正常中文金融答复（如"请删除该条件""删除重复行"）误伤。
#
# 拆成「注入类 / 代码类」两组，是为了让**说明文档与 SKILL.md 只过注入类**：文档里出现
# `subprocess.run`、`curl` 这类**说明性示例**是正常的（skill 正文本来就要教人怎么写脚本），
# 不应因此拒绝整包上传；而文档正文会喂给 LLM，注入类规则仍必须生效。
_INJECTION_PATTERNS = [
    # 提示注入回声
    r"ignore\s+(?:previous|above|prior)\s+instructions",
    r"忽略\s*(?:以上|前面|之前)\s*(?:指令|提示|设定)",
    r"disregard\s+(?:any|all)\s+(?:constraints|rules|safety)",
]

# 代码类：代码执行 / 破坏性 shell / 网络外联 / 文件系统越权。
# 只对**最终答复**与**可执行脚本**扫描；说明文档不扫（见上）。
_CODE_PATTERNS = [
    # 代码执行
    r"(?:os\.system|subprocess\.call|subprocess\.run|eval\s*\(|exec\s*\()",
    # 破坏性 shell
    r"rm\s+-rf",
    # 网络外联 / 数据外发
    r"(?:curl|wget|requests\.(?:get|post)|urllib\.request)",
    # 文件系统越权
    r"(?:open\s*\(\s*['\"]/|/etc/passwd|/\.ssh/|\.\./\.\./)",
]

# 全量文本层（答复与脚本用）：注入类 + 代码类，保持历史行为不变。
_TEXT_PATTERNS = _INJECTION_PATTERNS + _CODE_PATTERNS


# ---------------------------------------------------------------------------
# 规则文件加载（路径可配置）
# ---------------------------------------------------------------------------

def load_rule_file(path: str) -> Dict[str, List[str]]:
    """加载规则文件，返回 {"sql": [...], "text": [...]}。

    支持三种格式：
    - JSON / YAML：顶层为对象时可含 `sql` 与 `text` 两个键（推荐）；顶层为列表则全部归入 text 层。
    - TXT：每行一个正则（# 开头为注释），全部归入 text 层。
    文件不存在或解析失败抛出 FileNotFoundError / ValueError（由调用方决定降级策略）。
    """
    import os

    if not os.path.exists(path):
        raise FileNotFoundError(f"沙箱规则文件不存在：{path}")

    text = ""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    sql: List[str] = []
    txt: List[str] = []

    lowered = path.lower()
    try:
        if lowered.endswith(".json"):
            data = json.loads(text)
        elif lowered.endswith((".yaml", ".yml")):
            try:
                import yaml  # 局部导入：未装 PyYAML 时不影响其它路径
            except ImportError as exc:  # pragma: no cover - 依赖缺失
                raise ValueError(
                    "解析 YAML 规则文件需要 PyYAML，请安装或改用 .json / .txt 格式"
                ) from exc
            data = yaml.safe_load(text)
        elif lowered.endswith(".txt"):
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                txt.append(line)
            return {"sql": [], "text": txt}
        else:
            # 未知后缀默认按 TXT 处理（逐行）
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                txt.append(line)
            return {"sql": [], "text": txt}

        if isinstance(data, dict):
            sql = list(data.get("sql", []) or [])
            txt = list(data.get("text", []) or [])
        elif isinstance(data, list):
            txt = list(data)
            logger.warning("沙箱规则文件 %s 为纯列表，全部归入 text 层", path)
        else:
            raise ValueError(f"沙箱规则文件格式不支持（需 dict 或 list）：{path}")
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"沙箱规则文件解析失败 {path}：{exc}") from exc

    return {"sql": sql, "text": txt}


def _compile(patterns: List[str]) -> List[re.Pattern[str]]:
    """编译正则列表，跳过非法正则并告警。"""
    out: List[re.Pattern[str]] = []
    for p in patterns:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error as exc:
            logger.warning("沙箱规则正则无效，已跳过：%r（%s）", p, exc)
    return out


# ---------------------------------------------------------------------------
# 沙箱检查器
# ---------------------------------------------------------------------------

class SandboxCheck:
    def __init__(
        self,
        rule_path: Optional[str] = None,
        extra_sql_patterns: Optional[List[str]] = None,
        extra_text_patterns: Optional[List[str]] = None,
    ) -> None:
        sql_patterns = list(_SQL_PATTERNS)
        injection_patterns = list(_INJECTION_PATTERNS)
        code_patterns = list(_CODE_PATTERNS)
        custom_text: List[str] = []  # 自定义文本规则：文档与脚本都扫（保守，宁多不漏）

        # 规则文件路径可配置：文件缺失 / 解析失败仅告警，回退内置规则，不阻断主链路
        if rule_path:
            try:
                loaded = load_rule_file(rule_path)
                sql_patterns.extend(loaded.get("sql", []))
                custom_text.extend(loaded.get("text", []))
                logger.info("沙箱规则已从 %s 加载（sql=%d, text=%d）", rule_path,
                            len(loaded.get("sql", [])), len(loaded.get("text", [])))
            except (FileNotFoundError, ValueError) as exc:
                logger.warning("沙箱规则文件不可用，回退内置规则：%s", exc)

        if extra_sql_patterns:
            sql_patterns.extend(extra_sql_patterns)
        if extra_text_patterns:
            custom_text.extend(extra_text_patterns)

        self.sql_patterns = _compile(sql_patterns)
        # 全量文本层（最终答复 / 可执行脚本）：注入类 + 代码类 + 自定义
        self.text_patterns = _compile(injection_patterns + code_patterns + custom_text)
        # 文档层（SKILL.md / 说明文档）：仅注入类 + 自定义，不误伤说明性代码示例
        self.doc_patterns = _compile(injection_patterns + custom_text)

    # ---- SQL 层 ----
    def scan_sql(self, sql: str) -> List[Dict[str, Any]]:
        return self._scan(sql, self.sql_patterns)

    def is_safe_sql(self, sql: str) -> bool:
        return not self.scan_sql(sql)

    def check_sql(self, sql: str) -> Dict[str, Any]:
        hits = self.scan_sql(sql)
        return {
            "ok": len(hits) == 0,
            "hits": hits,
            "summary": "safe" if not hits else f"SQL 命中 {len(hits)} 个危险模式",
        }

    # ---- 文本层 ----
    def scan_text(self, text: str) -> List[Dict[str, Any]]:
        return self._scan(text, self.text_patterns)

    def scan_doc(self, text: str) -> List[Dict[str, Any]]:
        """说明文档 / SKILL.md 专用：只扫注入类规则（+自定义规则）。

        不扫代码执行 / 外联类——文档里出现 `subprocess.run`、`curl` 这类**说明性示例**
        是正常的（skill 正文本来就要教人怎么写脚本），不应因此拒绝整包上传。
        """
        return self._scan(text, self.doc_patterns)

    def is_safe(self, text: str) -> bool:
        """文本层安全性（保持与历史接口一致：默认按文本层扫描）。"""
        return not self.scan_text(text)

    def check_text(self, text: str) -> Dict[str, Any]:
        hits = self.scan_text(text)
        return {
            "ok": len(hits) == 0,
            "hits": hits,
            "summary": "safe" if not hits else f"文本命中 {len(hits)} 个危险模式",
        }

    # ---- 通用 ----
    def _scan(self, text: str, patterns: List[re.Pattern[str]]) -> List[Dict[str, Any]]:
        hits: List[Dict[str, Any]] = []
        for idx, pat in enumerate(patterns):
            for m in pat.finditer(text or ""):
                hits.append(
                    {
                        "pattern_index": idx,
                        "matched": m.group(0),
                        "position": m.start(),
                    }
                )
        return hits


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def build_sandbox(settings: HarnessSettings) -> Optional[SandboxCheck]:
    """根据 HarnessSettings 构建沙箱；关闭时返回 None（调用方据此跳过扫描）。"""
    if not settings.sandbox_enabled:
        return None
    return SandboxCheck(
        rule_path=settings.sandbox_rule_path or None,
        extra_sql_patterns=list(settings.sandbox_extra_sql_patterns or []),
        extra_text_patterns=list(settings.sandbox_extra_text_patterns or []),
    )


_MISSING = object()
_SANDBOX_CACHE: Dict[tuple, Optional[SandboxCheck]] = {}


def get_default_sandbox() -> Optional[SandboxCheck]:
    """从全局 settings（DMF_HARNESS-*）构建沙箱，并按配置指纹缓存复用。

    返回 None 表示沙箱关闭（调用方跳过扫描）。
    """
    hs = get_settings().harness
    key = (
        hs.sandbox_enabled,
        hs.sandbox_rule_path,
        tuple(hs.sandbox_extra_sql_patterns or []),
        tuple(hs.sandbox_extra_text_patterns or []),
    )
    cached = _SANDBOX_CACHE.get(key, _MISSING)  # type: ignore[arg-type]
    if cached is _MISSING:
        cached = build_sandbox(hs)
        _SANDBOX_CACHE[key] = cached
    return cached
