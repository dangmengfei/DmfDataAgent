"""Skill 自带脚本的沙箱执行器（`run_skill_script` 工具的落地实现）。

这是对「Harness 不执行任意代码」原则的**一次有意放宽**：skill 包可以带脚本，
并由声明式步骤显式调用。因此执行侧自带多层纵深防御：

- **路径围栏**：脚本必须位于该 skill 的包目录内（`../` 逃逸直接拒绝）；
- **解释器白名单**：仅 `.py`（当前解释器）与 `.sh`（`/bin/sh`）；绝不 `shell=True`；
- **环境剥离**：只透传最小环境变量集，**显式剔除全部 `DMF_*`**
  （进程环境里有真实 LLM API key 与数据库口令）；
- **资源限制**（POSIX）：CPU 时间 / 地址空间 / 单文件大小 / 子进程数；
- **超时**：到点即 kill；
- **输出上限**：stdout/stderr 边读边截断，超限部分丢弃但继续排空（避免子进程写管道阻塞）。

**残余风险（如实说明）**：本模块不做内核级网络隔离（需要容器 / network namespace 才能做到），
脚本仍可能发起网络请求。压制手段是**上传期静态扫描**（`harness/sandbox.SandboxCheck` 的文本层
已覆盖 `curl|wget|requests.get/post|urllib.request` 等外联模式）+ 上述运行时约束。
若部署环境要求强隔离，应把 skill 执行放进独立容器，本模块保持为进程内兜底。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 解释器白名单：后缀 -> 取解释器路径的方式
_ALLOWED_SUFFIXES: Tuple[str, ...] = (".py", ".sh")

_READ_CHUNK = 8192

# 子进程资源上限（POSIX rlimit）
_RLIMIT_CPU_SECONDS = 60
_RLIMIT_ADDRESS_SPACE = 512 * 1024 * 1024
_RLIMIT_FILE_SIZE = 16 * 1024 * 1024
_RLIMIT_PROCESSES = 32

# 透传给子进程的环境变量（其余一律不透传，尤其 DMF_* 里的密钥）
_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TZ")


def _interpreter_for(path: Path) -> Optional[List[str]]:
    """按后缀给出解释器 argv 前缀；不支持的后缀返回 None。"""
    suffix = path.suffix.lower()
    if suffix == ".py":
        return [sys.executable or "python3"]
    if suffix == ".sh":
        return [shutil.which("sh") or "/bin/sh"]
    return None


def _scrubbed_env(home: Path) -> Dict[str, str]:
    """最小环境：白名单透传 + 固定 HOME/TMPDIR 指向本次运行的临时目录。"""
    env: Dict[str, str] = {}
    for key in _ENV_ALLOWLIST:
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = str(home)
    env["TMPDIR"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _apply_rlimits() -> None:  # pragma: no cover - 在子进程内执行，无法直接断言
    """子进程启动前设置资源上限（仅在 POSIX 生效，各平台缺失的项静默跳过）。"""
    try:
        import resource
    except ImportError:
        return

    limits = (
        (resource.RLIMIT_CPU, _RLIMIT_CPU_SECONDS),
        (resource.RLIMIT_AS, _RLIMIT_ADDRESS_SPACE),
        (resource.RLIMIT_FSIZE, _RLIMIT_FILE_SIZE),
        (getattr(resource, "RLIMIT_NPROC", None), _RLIMIT_PROCESSES),
    )
    for name, value in limits:
        if name is None:
            continue
        try:
            resource.setrlimit(name, (value, value))
        except (ValueError, OSError):
            continue


async def _read_capped(stream: Any, limit: int) -> Tuple[bytes, int]:
    """读到 EOF，但最多保留 ``limit`` 字节；超出部分丢弃并继续排空管道。"""
    if stream is None:
        return b"", 0
    kept: List[bytes] = []
    kept_size = 0
    total = 0
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if kept_size < limit:
            take = chunk[: limit - kept_size]
            kept.append(take)
            kept_size += len(take)
    return b"".join(kept), total


def _decode(raw: bytes, total: int) -> str:
    text = raw.decode("utf-8", "replace")
    if total > len(raw):
        text += f"\n…（输出已截断，共 {total} 字节）"
    return text


class ScriptRunner:
    """在 skill 包目录内执行 `.py` / `.sh` 脚本（见模块 docstring 的安全边界）。"""

    def __init__(
        self,
        *,
        timeout_seconds: int = 30,
        max_output_bytes: int = 65536,
        allowed_suffixes: Tuple[str, ...] = _ALLOWED_SUFFIXES,
    ) -> None:
        self._timeout = max(1, int(timeout_seconds or 30))
        self._max_output = max(1024, int(max_output_bytes or 65536))
        self._allowed_suffixes = tuple(allowed_suffixes)

    async def run(self, script: str, args: Optional[List[Any]] = None, cwd: Path | str = ".") -> Dict[str, Any]:
        """执行包内脚本；任何失败都回 `{ok: False, message}`，不冒泡。"""
        root = Path(cwd).resolve()
        if not root.is_dir():
            return self._fail(f"skill 包目录不存在：{root}")

        script_path = self._resolve_script(root, script)
        if isinstance(script_path, str):  # 校验失败，返回的是原因
            return self._fail(script_path)

        if script_path.suffix.lower() not in self._allowed_suffixes:
            return self._fail(
                f"不支持的脚本类型：{script_path.suffix or '（无后缀）'}"
                f"（仅支持 {'、'.join(self._allowed_suffixes)}）"
            )
        interpreter = _interpreter_for(script_path)
        if interpreter is None:  # pragma: no cover - 与上面的后缀检查一致
            return self._fail(f"无法确定 {script_path.name} 的解释器")

        argv_args, err = self._normalize_args(args)
        if err:
            return self._fail(err)

        return await self._spawn(interpreter + [str(script_path)] + argv_args, root)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _resolve_script(self, root: Path, script: str) -> Path | str:
        """路径围栏：脚本必须落在包目录内，返回解析后的路径或失败原因字符串。"""
        name = str(script or "").strip()
        if not name:
            return "缺少脚本路径（script）"
        if "\x00" in name:
            return "脚本路径含非法字符"
        candidate = (root / name).resolve()
        if not candidate.is_relative_to(root):
            return f"脚本路径越出 skill 包目录，已拒绝：{name}"
        if not candidate.is_file():
            return f"包内不存在脚本：{name}"
        return candidate

    @staticmethod
    def _normalize_args(args: Optional[List[Any]]) -> Tuple[List[str], str]:
        """把 args 规范成字符串数组。

        非字符串项（上一步 `query_result` 等结构化结果经 `${steps.N.result}` 替换而来）
        用 `json.dumps` 序列化，脚本侧可直接 `json.loads` 还原；若用 `str()` 会得到
        Python repr（单引号、`True`/`None` 等），不是合法 JSON，脚本无法解析。
        """
        if args is None:
            return [], ""
        if not isinstance(args, list):
            return [], "args 需为字符串数组"
        out: List[str] = []
        for item in args:
            if isinstance(item, str):
                text = item
            else:
                try:
                    text = json.dumps(item, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    text = str(item)
            if "\x00" in text:
                return [], "args 含非法字符"
            out.append(text)
        return out, ""

    async def _spawn(self, argv: List[str], cwd: Path) -> Dict[str, Any]:
        scratch = Path(tempfile.mkdtemp(prefix="dmf-skill-run-"))
        started = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=_scrubbed_env(scratch),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=_apply_rlimits if os.name == "posix" else None,
            )
        except Exception as exc:  # noqa: BLE001 - 启动失败需回传原因，不冒泡
            shutil.rmtree(scratch, ignore_errors=True)
            logger.warning("skill 脚本启动失败：%s", argv, exc_info=True)
            return self._fail(f"脚本启动失败：{exc}")

        out_task = asyncio.create_task(_read_capped(proc.stdout, self._max_output))
        err_task = asyncio.create_task(_read_capped(proc.stderr, self._max_output))

        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._timeout)
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):  # pragma: no cover
                logger.warning("skill 脚本超时后未能回收：%s", argv)

        (stdout_raw, stdout_total) = await out_task
        (stderr_raw, stderr_total) = await err_task
        shutil.rmtree(scratch, ignore_errors=True)

        elapsed = int((time.time() - started) * 1000)
        exit_code = proc.returncode if proc.returncode is not None else -1
        result: Dict[str, Any] = {
            "ok": (exit_code == 0) and not timed_out,
            "script": Path(argv[1]).name if len(argv) > 1 else Path(argv[0]).name,
            "exit_code": exit_code,
            "stdout": _decode(stdout_raw, stdout_total),
            "stderr": _decode(stderr_raw, stderr_total),
            "elapsed_ms": elapsed,
            "timed_out": timed_out,
        }
        if timed_out:
            result["message"] = f"脚本执行超时（上限 {self._timeout}s），已终止"
        elif exit_code != 0:
            result["message"] = f"脚本退出码 {exit_code}"
        return result

    @staticmethod
    def _fail(message: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "message": message,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "elapsed_ms": 0,
            "timed_out": False,
        }
