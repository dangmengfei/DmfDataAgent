"""Skill 包内脚本的沙箱执行测试（`skills/script_runner.py`）。

零外部依赖：脚本用 `sys.executable` 跑临时 `.py`，不连网、不碰 DB、不依赖具体平台命令。
"""

from __future__ import annotations

import os

import pytest

from dmf_agent.skills.script_runner import ScriptRunner, _scrubbed_env


def _write(tmp_path, name: str, content: str):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_run_python_script_ok(tmp_path):
    _write(tmp_path, "hello.py", "print('hello from skill')\n")
    runner = ScriptRunner(timeout_seconds=10, max_output_bytes=4096)
    out = await runner.run("hello.py", [], cwd=tmp_path)
    assert out["ok"] is True
    assert "hello from skill" in out["stdout"]
    assert out["exit_code"] == 0
    assert out["timed_out"] is False
    assert out["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_run_shell_script_ok(tmp_path):
    _write(tmp_path, "task.sh", "echo from-sh\n")
    runner = ScriptRunner(timeout_seconds=10, max_output_bytes=4096)
    out = await runner.run("task.sh", [], cwd=tmp_path)
    assert out["ok"] is True, out
    assert "from-sh" in out["stdout"]


@pytest.mark.asyncio
async def test_run_script_with_args(tmp_path):
    _write(tmp_path, "echo.py", "import sys\nprint(sys.argv[1:])\n")
    out = await ScriptRunner(timeout_seconds=10).run("echo.py", ["--top", "5"], cwd=tmp_path)
    assert out["ok"] is True
    assert "'--top', '5'" in out["stdout"]


@pytest.mark.asyncio
async def test_rejects_unknown_suffix(tmp_path):
    _write(tmp_path, "run.exe", "binary")
    out = await ScriptRunner(timeout_seconds=5).run("run.exe", [], cwd=tmp_path)
    assert out["ok"] is False
    assert "不支持的脚本类型" in out["message"]


@pytest.mark.asyncio
async def test_rejects_path_escape(tmp_path):
    """`../` 越出包目录：直接拒绝，不执行。"""
    outside = tmp_path.parent / "outside.py"
    _write(tmp_path.parent, "outside.py", "print('escaped')\n")
    try:
        out = await ScriptRunner(timeout_seconds=5).run("../outside.py", [], cwd=tmp_path)
        assert out["ok"] is False
        assert "越出" in out["message"]
    finally:
        outside.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_rejects_missing_script(tmp_path):
    out = await ScriptRunner(timeout_seconds=5).run("ghost.py", [], cwd=tmp_path)
    assert out["ok"] is False
    assert "不存在" in out["message"]


@pytest.mark.asyncio
async def test_rejects_empty_script_name(tmp_path):
    out = await ScriptRunner(timeout_seconds=5).run("", [], cwd=tmp_path)
    assert out["ok"] is False
    assert "缺少" in out["message"]


@pytest.mark.asyncio
async def test_nonzero_exit_code_reported(tmp_path):
    _write(tmp_path, "fail.py", "import sys\nsys.stderr.write('boom')\nsys.exit(3)\n")
    out = await ScriptRunner(timeout_seconds=10).run("fail.py", [], cwd=tmp_path)
    assert out["ok"] is False
    assert out["exit_code"] == 3
    assert "boom" in out["stderr"]
    assert "退出码" in out["message"]


@pytest.mark.asyncio
async def test_timeout_kills_script(tmp_path):
    _write(tmp_path, "hang.py", "import time\ntime.sleep(30)\n")
    runner = ScriptRunner(timeout_seconds=1, max_output_bytes=4096)
    out = await runner.run("hang.py", [], cwd=tmp_path)
    assert out["ok"] is False
    assert out["timed_out"] is True
    assert "超时" in out["message"]


@pytest.mark.asyncio
async def test_output_truncated_to_limit(tmp_path):
    _write(tmp_path, "spam.py", "print('x' * 10000)\n")
    out = await ScriptRunner(timeout_seconds=10, max_output_bytes=1024).run("spam.py", [], cwd=tmp_path)
    assert out["ok"] is True
    assert len(out["stdout"]) < 2000  # 截断到 1024 + 截断提示
    assert "已截断" in out["stdout"]


@pytest.mark.asyncio
async def test_env_scrubbed_no_dmf_secrets(tmp_path):
    """子进程里看不到任何 DMF_*（进程环境里有真实 LLM key 与 DB 口令）。"""
    _write(
        tmp_path,
        "env.py",
        "import os\nprint(sorted(k for k in os.environ if k.startswith('DMF_')))\n"
        "print('HOME=' + os.environ.get('HOME', ''))\n",
    )
    out = await ScriptRunner(timeout_seconds=10).run("env.py", [], cwd=tmp_path)
    assert out["ok"] is True, out
    lines = out["stdout"].splitlines()
    assert lines[0] == "[]"  # 无任何 DMF_* 泄漏
    assert lines[1].startswith("HOME=") and lines[1] != "HOME="  # HOME 被指到临时目录


def test_scrubbed_env_drops_dmf(monkeypatch):
    monkeypatch.setenv("DMF_LLM-API_KEY", "sk-secret")
    monkeypatch.setenv("DMF_DB-PASSWORD", "p@ss")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    env = _scrubbed_env(home=None)
    assert not any(k.startswith("DMF_") for k in env)
    assert env.get("PYTHONDONTWRITEBYTECODE") == "1"


def test_scrubbed_env_defaults(tmp_path):
    env = _scrubbed_env(home=tmp_path)
    assert env["HOME"] == str(tmp_path)
    assert env["TMPDIR"] == str(tmp_path)
    assert env["PYTHONIOENCODING"] == "utf-8"


@pytest.mark.asyncio
async def test_structured_args_serialized_as_json(tmp_path):
    """上一步的结构化结果（dict）经 args 传入：脚本侧可 json.loads 还原。

    回归护栏：若对非字符串项用 `str()`，脚本收到的是 Python repr（单引号、True/None），
    `json.loads` 会失败——这正是「取数结果喂给包内脚本」断链的根因。
    """
    _write(
        tmp_path,
        "consume.py",
        "import json, sys\n"
        "payload = json.loads(sys.argv[1])\n"
        "print('rows=' + str(len(payload['rows'])))\n",
    )
    result = {"columns": ["a"], "rows": [1, 2, 3]}
    out = await ScriptRunner(timeout_seconds=10).run("consume.py", [result], cwd=tmp_path)
    assert out["ok"] is True, out
    assert "rows=3" in out["stdout"]


def test_normalize_args_json_encodes_structures():
    """`_normalize_args`：字符串原样透传，结构化项走 JSON，标量走 JSON 字面量。"""
    args, err = ScriptRunner._normalize_args([{"a": 1}, "plain", 5, None])
    assert err == ""
    assert args == ['{"a": 1}', "plain", "5", "null"]


def test_normalize_args_rejects_nul():
    _, err = ScriptRunner._normalize_args(["ok", "bad\x00arg"])
    assert err == "args 含非法字符"
