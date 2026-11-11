"""系统提示词管理器：版本管理 + 运行时热更新 + FR12 5 层/灰度/门禁/A·B。

- 基线：仓库内 agents/prompts/*.yaml（版本 1.0.0，只读）；
- 变更：保存 / 回滚均生成**新版本**（历史不可变），回滚记录 source_version 保留审计链；
- 生效：每次变更后写运行时覆盖（prompt_loader.set_override），Agent 下一轮即用新内容；
- FR12：版本带 layer / rollout_status / gray_ratio / eval_passed；canary 版本按请求上下文灰度命中；
       canary→full 晋级需 eval_passed 门禁；A/B 对比取两版本渲染内容；
- 存储：prompt_versions 表（MySQL），不可用时退化进程内存。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from dmf_agent.agents import prompt_loader
from dmf_agent.agents import prompt_rollout
from dmf_agent.db.stores import PromptVersionStore

logger = logging.getLogger(__name__)


class PromptManager:
    def __init__(self, store: PromptVersionStore, gray_enabled: bool = True) -> None:
        self._store = store
        self._gray_enabled = gray_enabled

    # ---------- 生效加载 ----------
    async def reload(self) -> str:
        """从存储加载各模板最新 published 版本，注入运行时覆盖，并注册灰度 resolver。"""
        prompt_loader.clear_overrides()
        rows = await self._store.load_all()
        published = [r for r in rows if r.get("status") == "published"]
        latest: Dict[str, Dict[str, Any]] = {}
        for r in published:
            # 金丝雀版本只进金丝雀注册表，不当默认覆盖（默认覆盖给 full/稳定版本）
            if r.get("rollout_status") == "canary":
                continue
            cur = latest.get(r["name"])
            if cur is None or prompt_loader.version_key(r["version"]) > prompt_loader.version_key(cur["version"]):
                latest[r["name"]] = r
        for name, r in latest.items():
            if name not in prompt_loader.prompt_names():
                continue
            prompt_loader.set_override(
                name,
                r["version"],
                r["system"],
                r.get("human", ""),
                r.get("source", "mysql"),
                r.get("updated_at", 0),
            )
        # FR12 灰度：构建金丝雀注册表并注册 resolver（对 load_prompt 调用方透明）
        by_name: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            by_name.setdefault(r["name"], []).append(r)
        canary_registry: Dict[str, Dict[str, Any]] = {}
        for name, vs in by_name.items():
            canary = prompt_rollout.choose_canary(vs)
            if canary:
                canary_registry[name] = canary
        prompt_loader.set_rollout_resolver(
            prompt_rollout.build_resolver(canary_registry, self._gray_enabled)
        )
        mode = "mysql" if (self._store.persistent and published) else "memory"
        logger.info(
            "提示词覆盖加载完成：%d 个模板生效（来源 %s），金丝雀 %d 个",
            len(latest), mode, len(canary_registry),
        )
        return mode

    # ---------- 查询 ----------
    async def list(self) -> Dict[str, Any]:
        meta = prompt_loader.active_meta()
        items: List[Dict[str, Any]] = []
        for name in prompt_loader.prompt_names():
            history = await self._store.load_by_name(name)
            m = meta.get(name, {"version": prompt_loader.BASELINE_VERSION, "source": "builtin", "updated_at": 0})
            latest_row: Dict[str, Any] = {}
            for r in history:
                if r.get("rollout_status") == "disabled":
                    continue
                if not latest_row or prompt_loader.version_key(r["version"]) > prompt_loader.version_key(latest_row["version"]):
                    latest_row = r
            items.append(
                {
                    "name": name,
                    "description": prompt_loader.PROMPT_DESCRIPTIONS.get(name, ""),
                    "version": m["version"],
                    "source": m["source"],
                    "updated_at": m["updated_at"],
                    "history_count": len(history),
                    "layer": latest_row.get("layer", ""),
                    "rollout_status": latest_row.get("rollout_status", "full"),
                    "gray_ratio": latest_row.get("gray_ratio", 1.0),
                    "eval_passed": latest_row.get("eval_passed", False),
                }
            )
        return {
            "ok": True,
            "items": items,
            "builtin_version": prompt_loader.BASELINE_VERSION,
            "changelog": [],
        }

    async def history(self, name: str) -> Dict[str, Any]:
        if name not in prompt_loader.prompt_names():
            return {"ok": False, "message": f"未知模板：{name}"}
        rows = await self._store.load_by_name(name)
        baseline = prompt_loader.baseline_spec(name)
        if not any(r["version"] == baseline["version"] for r in rows):
            rows.append(baseline)
        for r in rows:
            r.setdefault("layer", "")
            r.setdefault("rollout_status", "full")
            r.setdefault("gray_ratio", 1.0)
            r.setdefault("eval_passed", False)
        rows.sort(key=lambda r: prompt_loader.version_key(r["version"]))
        meta = prompt_loader.active_meta().get(name, {})
        return {
            "ok": True,
            "name": name,
            "items": rows,
            "count": len(rows),
            "current": meta.get("version", prompt_loader.BASELINE_VERSION),
        }

    async def detail(self, name: str, version: str) -> Dict[str, Any]:
        if name not in prompt_loader.prompt_names():
            return {"ok": False, "message": f"未知模板：{name}"}
        rows = await self._store.load_by_name(name)
        hit = next((r for r in rows if r["version"] == version), None)
        if hit:
            return {
                "ok": True,
                "name": name,
                "version": version,
                "system": hit["system"],
                "human": hit.get("human", ""),
                "description": hit.get("description", ""),
                "source": hit.get("source", "mysql"),
                "created_at": hit.get("created_at", 0),
                "layer": hit.get("layer", ""),
                "rollout_status": hit.get("rollout_status", "full"),
                "gray_ratio": hit.get("gray_ratio", 1.0),
                "eval_passed": hit.get("eval_passed", False),
            }
        baseline = prompt_loader.baseline_spec(name)
        if baseline["version"] == version:
            return {
                "ok": True,
                "name": name,
                "version": version,
                "system": baseline["system"],
                "human": baseline["human"],
                "description": baseline["description"],
                "source": "builtin",
                "created_at": 0,
                "layer": "",
                "rollout_status": "full",
                "gray_ratio": 1.0,
                "eval_passed": False,
            }
        return {"ok": False, "message": f"模板 {name} 不存在版本 {version}"}

    # ---------- 变更 ----------
    async def update(
        self,
        name: str,
        content: str,
        description: str = "",
        layer: str = "",
        rollout_status: str = "full",
        gray_ratio: float = 1.0,
        eval_passed: bool = False,
    ) -> Dict[str, Any]:
        if name not in prompt_loader.prompt_names():
            return {"ok": False, "message": f"未知模板：{name}"}
        content = (content or "").strip()
        if not content:
            return {"ok": False, "message": "提示词内容不能为空"}
        rows = await self._store.load_by_name(name)
        versions = [r["version"] for r in rows]
        new_version = prompt_loader.next_patch_version(versions)
        baseline = prompt_loader.baseline_spec(name)
        now = int(time.time())
        row = {
            "name": name,
            "version": new_version,
            "system": content,
            "human": baseline["human"],
            "description": description or prompt_loader.PROMPT_DESCRIPTIONS.get(name, ""),
            "source_version": "",
            "status": "published",
            "layer": layer,
            "rollout_status": rollout_status,
            "gray_ratio": gray_ratio,
            "eval_passed": eval_passed,
            "created_at": now,
            "updated_at": now,
        }
        persisted = await self._store.save(row)
        prompt_loader.set_override(
            name, new_version, content, baseline["human"],
            "mysql" if persisted else "memory", now,
        )
        where = "MySQL 已持久化" if persisted else "内存兜底（配置 MySQL 后持久化）"
        return {"ok": True, "message": f"模板「{name}」新版本 {new_version} 已保存并生效（{where}）", "version": new_version}

    async def rollback(
        self,
        name: str,
        version: str,
        description: str = "",
        layer: str = "",
        rollout_status: str = "full",
        gray_ratio: float = 1.0,
        eval_passed: bool = False,
    ) -> Dict[str, Any]:
        if name not in prompt_loader.prompt_names():
            return {"ok": False, "message": f"未知模板：{name}"}
        detail = await self.detail(name, version)
        if not detail.get("ok"):
            return detail
        rows = await self._store.load_by_name(name)
        versions = [r["version"] for r in rows]
        new_version = prompt_loader.next_patch_version(versions)
        baseline = prompt_loader.baseline_spec(name)
        now = int(time.time())
        row = {
            "name": name,
            "version": new_version,
            "system": detail["system"],
            "human": baseline["human"],
            "description": description or f"回溯自 {version}（{detail.get('description') or ''}）",
            "source_version": version,
            "status": "published",
            "layer": layer or detail.get("layer", ""),
            "rollout_status": rollout_status,
            "gray_ratio": gray_ratio,
            "eval_passed": eval_passed,
            "created_at": now,
            "updated_at": now,
        }
        persisted = await self._store.save(row)
        prompt_loader.set_override(
            name, new_version, detail["system"], baseline["human"],
            "mysql" if persisted else "memory", now,
        )
        where = "MySQL 已持久化" if persisted else "内存兜底"
        return {
            "ok": True,
            "message": f"模板「{name}」已回溯到 {version}（新版本 {new_version}，{where}）",
            "version": new_version,
        }

    # ---------- FR12 评测门禁 / A·B ----------
    async def mark_eval(self, name: str, version: str, passed: bool) -> Dict[str, Any]:
        rows = await self._store.load_by_name(name)
        hit = next((r for r in rows if r["version"] == version), None)
        if not hit:
            return {"ok": False, "message": f"模板 {name} 不存在版本 {version}"}
        await self._store.update_version(name, version, eval_passed=bool(passed))
        return {"ok": True, "message": f"{name} {version} 评测结果已记录（passed={passed}）"}

    # ---------- P1-7：数据集评测联动发布门禁 ----------
    async def eval_by_dataset(
        self,
        name: str,
        version: str,
        answers: List[Dict[str, Any]],
        *,
        min_tcr: float = 0.8,
        require_no_hallucination: bool = True,
    ) -> Dict[str, Any]:
        """用一段数据集评测结果（逐用例断言）联动 FR12 发布门禁。

        :param answers: 逐用例断言结果（来自一次实时跑或离线录回），字段：
            ``name``（与数据集用例对齐）、``intent_ok``/``metrics_ok``/``skills_ok``/
            ``time_ok``/``attribution_ok``（bool）、``judge``（可选 JudgeResult dict）、
            ``answer``（可选）。
        :param min_tcr: 任务完成率门禁阈值（默认 0.8）
        :param require_no_hallucination: 是否要求幻觉率必须为 0（PRD FR 目标）
        :return: 聚合报告 + 是否通过门禁 + 已写入的 eval_passed
        """
        from dmf_agent.eval.judge import JudgeResult
        from dmf_agent.eval.metrics import EvalCaseResult, compute_metrics

        if not answers:
            return {"ok": False, "message": "answers 为空，无法评测"}
        results = []
        for a in answers:
            judge = None
            jd = a.get("judge")
            if isinstance(jd, dict):
                judge = JudgeResult(
                    faithfulness=jd.get("faithfulness", 1.0),
                    hallucination=jd.get("hallucination", False),
                    accuracy=jd.get("accuracy", 1.0),
                    rationale=jd.get("rationale", ""),
                    source=jd.get("source", "deterministic"),
                )
            results.append(
                EvalCaseResult(
                    name=a.get("name", ""),
                    query=a.get("query", ""),
                    category=a.get("category", ""),
                    intent_ok=bool(a.get("intent_ok", False)),
                    metrics_ok=bool(a.get("metrics_ok", False)),
                    skills_ok=bool(a.get("skills_ok", False)),
                    time_ok=bool(a.get("time_ok", True)),
                    attribution_ok=bool(a.get("attribution_ok", True)),
                    judge=judge,
                    answer=a.get("answer", ""),
                    is_attribution=(a.get("category") == "attribution"),
                )
            )
        report = compute_metrics(results)
        passed = report.tcr >= min_tcr and (
            not require_no_hallucination or report.hallucination_rate == 0.0
        )
        mark = await self.mark_eval(name, version, passed)
        return {
            "ok": True,
            "name": name,
            "version": version,
            "report": report.to_dict(),
            "eval_passed": passed,
            "min_tcr": min_tcr,
            "mark_eval": mark,
        }

    async def promote(self, name: str, version: str) -> Dict[str, Any]:
        """金丝雀晋级 full：评测门禁——未通过评测（eval_passed）不允许晋级。"""
        rows = await self._store.load_by_name(name)
        hit = next((r for r in rows if r["version"] == version), None)
        if not hit:
            return {"ok": False, "message": f"模板 {name} 不存在版本 {version}"}
        if hit.get("rollout_status") != "canary":
            return {"ok": False, "message": f"仅 canary 可晋级（当前 {hit.get('rollout_status')}）"}
        if not hit.get("eval_passed"):
            return {
                "ok": False,
                "code": "EVAL_GATE",
                "message": "评测门禁未通过：canary 晋级 full 前必须 mark_eval(passed=true)",
            }
        await self._store.update_version(name, version, rollout_status="full")
        await self.reload()
        return {"ok": True, "message": f"模板「{name}」{version} 已晋级 full（100% 流量）"}

    async def ab_compare(self, name: str, version_a: str, version_b: str) -> Dict[str, Any]:
        da = await self.detail(name, version_a)
        db = await self.detail(name, version_b)
        if not da.get("ok"):
            return {"ok": False, "message": f"版本不存在：{name} {version_a}"}
        if not db.get("ok"):
            return {"ok": False, "message": f"版本不存在：{name} {version_b}"}
        return {
            "ok": True,
            "name": name,
            "a": {"version": version_a, "system": da["system"], "human": da["human"],
                  "layer": da["layer"], "rollout_status": da["rollout_status"]},
            "b": {"version": version_b, "system": db["system"], "human": db["human"],
                  "layer": db["layer"], "rollout_status": db["rollout_status"]},
        }
