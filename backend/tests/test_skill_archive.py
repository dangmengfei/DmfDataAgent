"""Skill 压缩包安全解压测试（`skills/archive.py`）。

零外部依赖：全部用内存 zip + pytest tmp_path，不连网、不碰 DB。
"""

from __future__ import annotations

import io
import os
import time
import zipfile

import pytest

from dmf_agent.skills.archive import (
    ArchiveError,
    cleanup_stale_temp,
    commit,
    extract_skill_zip,
    remove_package,
)

VALID_SKILL_MD = (
    "---\nname: demo\nsteps:\n  - tool: execute_query\n    arguments: {dsl: {}}\n---\n正文\n"
)


def _zip(entries: dict[str, bytes], *, symlinks=()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            if name in symlinks:
                info = zipfile.ZipInfo(name)
                info.external_attr = 0o120777 << 16  # 符号链接
                zf.writestr(info, data)
            else:
                zf.writestr(name, data)
    return buf.getvalue()


def _encrypted_zip(entries: dict[str, bytes]) -> bytes:
    """构造一个含「加密成员」的 zip：`writestr` 会重算 flag_bits，只能直接改中央目录。

    中央目录条目里 general purpose bit flag 是固定偏移：magic(4) + version(2) 之后，
    低 8 位在第 9 个字节、高 8 位在第 10 个字节——置 bit0 即标记加密。
    """
    raw = _zip(entries)
    pos = 0
    while True:
        pos = raw.find(b"PK\x01\x02", pos)
        if pos < 0:
            break
        flags = raw[pos + 8] | (raw[pos + 9] << 8)
        raw = raw[: pos + 8] + bytes([flags | 0x1]) + raw[pos + 9 :]
        break  # 只标第一个成员即足够（触发逐成员加密检查）
    return raw


def _extract(tmp_path, raw: bytes, **kwargs):
    base = tmp_path / "skills"
    opts = {"max_bytes": 5_000_000, "max_files": 100, "max_file_bytes": 1_000_000}
    opts.update(kwargs)
    return extract_skill_zip(raw, base, **opts)


def test_extract_flat_package(tmp_path):
    raw = _zip({"SKILL.md": VALID_SKILL_MD.encode(), "scripts/run.py": b"print(1)\n", "README.md": b"x"})
    result = _extract(tmp_path, raw)
    assert (result.root / "SKILL.md").is_file()
    paths = {f["path"] for f in result.files}
    assert paths == {"README.md", "scripts/run.py"}
    by_path = {f["path"]: f for f in result.files}
    assert by_path["scripts/run.py"]["kind"] == "script"
    assert by_path["README.md"]["kind"] == "doc"


def test_extract_nested_top_level_dir(tmp_path):
    """直接压缩一个文件夹时的常见形态：SKILL.md 被唯一一层顶层目录包裹。"""
    raw = _zip({"my_skill/SKILL.md": VALID_SKILL_MD.encode(), "my_skill/a.txt": b"1"})
    result = _extract(tmp_path, raw)
    assert (result.root / "SKILL.md").is_file()
    assert result.root.name == "my_skill"
    assert [f["path"] for f in result.files] == ["a.txt"]


def test_commit_installs_and_replaces(tmp_path):
    base = tmp_path / "skills"
    raw = _zip({"SKILL.md": VALID_SKILL_MD.encode(), "x.txt": b"new"})
    result = extract_skill_zip(raw, base, max_bytes=5_000_000, max_files=100, max_file_bytes=1_000_000)
    final = commit(result, base, "demo")
    assert (final / "SKILL.md").is_file()
    assert (base / "demo" / "x.txt").read_text() == "new"

    # 重传同名：整棵替换
    raw2 = _zip({"SKILL.md": VALID_SKILL_MD.encode(), "y.txt": b"v2"})
    result2 = extract_skill_zip(raw2, base, max_bytes=5_000_000, max_files=100, max_file_bytes=1_000_000)
    commit(result2, base, "demo")
    assert not (base / "demo" / "x.txt").exists()
    assert (base / "demo" / "y.txt").read_text() == "v2"


def test_rejects_path_traversal(tmp_path):
    raw = _zip({"../evil.txt": b"x", "SKILL.md": VALID_SKILL_MD.encode()})
    with pytest.raises(ArchiveError, match="越界|路径"):
        _extract(tmp_path, raw)


def test_rejects_absolute_path(tmp_path):
    raw = _zip({"/abs/evil.txt": b"x", "SKILL.md": VALID_SKILL_MD.encode()})
    with pytest.raises(ArchiveError):
        _extract(tmp_path, raw)


def test_rejects_backslash_name(tmp_path):
    raw = _zip({"..\\evil.txt": b"x", "SKILL.md": VALID_SKILL_MD.encode()})
    with pytest.raises(ArchiveError):
        _extract(tmp_path, raw)


def test_rejects_symlink(tmp_path):
    raw = _zip({"link": b"../etc/passwd", "SKILL.md": VALID_SKILL_MD.encode()}, symlinks=("link",))
    with pytest.raises(ArchiveError, match="符号链接|非普通文件"):
        _extract(tmp_path, raw)


def test_rejects_encrypted_member(tmp_path):
    raw = _encrypted_zip({"secret.txt": b"x", "SKILL.md": VALID_SKILL_MD.encode()})
    with pytest.raises(ArchiveError, match="加密"):
        _extract(tmp_path, raw)


def test_rejects_too_many_files(tmp_path):
    entries = {"SKILL.md": VALID_SKILL_MD.encode()}
    for i in range(10):
        entries[f"f{i}.txt"] = b"x"
    raw = _zip(entries)
    with pytest.raises(ArchiveError, match="文件数"):
        _extract(tmp_path, raw, max_files=5)


def test_rejects_total_size_over_limit(tmp_path):
    raw = _zip({"SKILL.md": VALID_SKILL_MD.encode(), "big.bin": b"0" * 5000})
    with pytest.raises(ArchiveError, match="总大小"):
        _extract(tmp_path, raw, max_bytes=1000)


def test_rejects_single_file_over_limit(tmp_path):
    raw = _zip({"SKILL.md": VALID_SKILL_MD.encode(), "big.bin": b"0" * 5000})
    with pytest.raises(ArchiveError, match="过大"):
        _extract(tmp_path, raw, max_file_bytes=100)


def test_rejects_zip_bomb_ratio(tmp_path):
    raw = _zip({"SKILL.md": VALID_SKILL_MD.encode(), "bomb.bin": b"0" * 200_000})
    with pytest.raises(ArchiveError, match="压缩比"):
        _extract(tmp_path, raw, max_bytes=50_000_000)


def test_rejects_missing_skill_md(tmp_path):
    raw = _zip({"only.txt": b"x"})
    with pytest.raises(ArchiveError, match="SKILL.md"):
        _extract(tmp_path, raw)


def test_rejects_not_a_zip(tmp_path):
    with pytest.raises(ArchiveError, match="ZIP"):
        _extract(tmp_path, b"this is not a zip at all")


def test_remove_package_stays_within_base(tmp_path):
    base = tmp_path / "skills"
    (base / "demo").mkdir(parents=True)
    (base / "demo" / "x.txt").write_text("x")
    remove_package(base, "demo")
    assert not (base / "demo").exists()
    # 越界名字静默不删
    (base / "keep").mkdir()
    remove_package(base, "../etc")
    assert (base / "keep").exists()


# ---------------------------------------------------------------------------
# 遗留临时目录的启动清理（进程被 SIGKILL/OOM 杀掉时的兜底）
# ---------------------------------------------------------------------------
def test_cleanup_stale_temp_removes_old_only(tmp_path):
    old = tmp_path / ".tmp-old"
    old.mkdir()
    (old / "x.txt").write_text("stale")
    fresh = tmp_path / ".tmp-fresh"
    fresh.mkdir()
    pkg = tmp_path / "deposit_topic"
    pkg.mkdir()

    past = time.time() - 7200
    os.utime(old, (past, past))

    removed = cleanup_stale_temp(tmp_path, max_age_seconds=3600)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()  # 未超龄：可能是别的实例正在解压，不能误删
    assert pkg.exists()  # 非 `.tmp-` 前缀：真实包目录，不动


def test_cleanup_stale_temp_missing_base(tmp_path):
    assert cleanup_stale_temp(tmp_path / "nope") == 0


def test_cleanup_stale_temp_ignores_files(tmp_path):
    """同前缀的普通文件不删（只处理目录）。"""
    f = tmp_path / ".tmp-file"
    f.write_text("x")
    assert cleanup_stale_temp(tmp_path) == 0
    assert f.exists()