"""Skill 压缩包（ZIP）的安全解压与落盘。

Skill 包 = 一个 ZIP，根（或被唯一一层顶层目录包裹）含 `SKILL.md`，可附带脚本、文档、
数据等任意文件。本模块**只做安全解压与文件清单**，不做包解析（见 `package.py`）。

安全闸口（任一不过即抛 `ArchiveError`，临时目录整棵删除）::

    加密成员 / 符号链接等非普通文件 / 绝对路径 / `..` 路径穿越 / 反斜杠成员名
    解压总大小上限 / 单文件大小上限 / 文件数上限 / 压缩比（防 zip 炸弹）
    根目录必须能找到 SKILL.md

落盘是**两阶段**的：先解到 `<base>/.tmp-<uuid>`，全部校验通过后才由 `commit()` 原子替换
`<base>/<name>` —— 避免中途失败留下半成品包，也避免坏包覆盖掉已注册的同名 skill。
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Tuple

SKILL_MD_NAME = "SKILL.md"

# 解压临时目录前缀（`<base>/.tmp-<uuid>`）；启动清理按该前缀识别遗留目录
_TMP_PREFIX = ".tmp-"

# 解压时跳过、也不进清单的噪声（macOS 压缩常见）
_IGNORED_NAMES = {".DS_Store", "__MACOSX", "__pycache__", ".git"}

# 压缩比上限：解压总大小 / 压缩总大小超过该值视为 zip 炸弹
_MAX_COMPRESSION_RATIO = 100

_SCRIPT_SUFFIXES = (".py", ".sh", ".bash")
_DOC_SUFFIXES = (".md", ".markdown", ".txt", ".yaml", ".yml", ".json", ".rst", ".csv")


class ArchiveError(ValueError):
    """压缩包解压/校验失败（原因面向使用者，中文）。"""


@dataclass
class ExtractResult:
    """一次解压的结果。

    - ``root``：包根目录（含 `SKILL.md` 的那一层）
    - ``tmp``：临时解压目录（`commit` / `discard` 负责清理）
    - ``files``：包内文件清单（不含 SKILL.md 自身）
    """

    root: Path
    tmp: Path
    files: List[Dict[str, Any]] = field(default_factory=list)


def kind_of(rel_path: str) -> str:
    """按后缀判断包内文件类型：`script`（可执行）/ `doc`（说明文档）/ `asset`（其它）。"""
    suffix = PurePosixPath(rel_path).suffix.lower()
    if suffix in _SCRIPT_SUFFIXES:
        return "script"
    if suffix in _DOC_SUFFIXES:
        return "doc"
    return "asset"


def _is_ignored(part: str) -> bool:
    return part in _IGNORED_NAMES or part.startswith(".")


def _safe_member_path(dest: Path, name: str) -> Path:
    """把 zip 成员名映射到 dest 下的安全路径；不安全即抛 `ArchiveError`。"""
    if "\\" in name:
        raise ArchiveError(f"包内路径含反斜杠，已拒绝：{name}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts:
        raise ArchiveError(f"包内路径越界，已拒绝：{name}")
    # Windows 盘符形式（C:xxx）
    if pure.parts and ":" in pure.parts[0]:
        raise ArchiveError(f"包内路径含盘符，已拒绝：{name}")

    target = dest / name
    base = dest.resolve()
    if not target.resolve().is_relative_to(base):
        raise ArchiveError(f"包内路径越界，已拒绝：{name}")
    return target


def _is_plain_file(zinfo: zipfile.ZipInfo) -> bool:
    """仅允许普通文件（拒绝符号链接 / 设备 / FIFO 等）。"""
    mode = (zinfo.external_attr >> 16) & 0o170000
    return mode in (0, 0o100000)  # 0 = 未声明（如 DOS 打包）


def extract_skill_zip(
    raw: bytes,
    base: Path,
    *,
    max_bytes: int,
    max_files: int,
    max_file_bytes: int,
) -> ExtractResult:
    """把 skill 压缩包解压到 `<base>/.tmp-<uuid>` 并做全部安全校验。

    失败抛 `ArchiveError` 且临时目录已清理；成功后由调用方 `commit()` 或 `discard()`。
    """
    if not raw:
        raise ArchiveError("上传内容为空")
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        raise ArchiveError("不是有效的 ZIP 压缩包（仅支持 .zip）")

    base.mkdir(parents=True, exist_ok=True)
    tmp = base / f"{_TMP_PREFIX}{uuid.uuid4().hex}"
    tmp.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            infos = [z for z in zf.infolist() if not z.is_dir()]
            if not infos:
                raise ArchiveError("压缩包内没有任何文件")

            # 危险成员名先于「噪声忽略」显式拒绝：`../xx`、`..\\xx` 这类路径若先被
            # 点开头忽略过滤吞掉，就变成静默跳过而非拒绝（防御纵深要求显式报错）。
            for zinfo in infos:
                _safe_member_path(tmp, zinfo.filename)

            members = [z for z in infos if not any(_is_ignored(p) for p in PurePosixPath(z.filename).parts)]
            if len(members) > max_files:
                raise ArchiveError(f"包内文件数 {len(members)} 超过上限 {max_files}")

            declared_total = sum(z.file_size for z in members)
            compressed_total = sum(z.compress_size for z in members) or 1
            if declared_total / compressed_total > _MAX_COMPRESSION_RATIO:
                raise ArchiveError("压缩比异常（疑似 zip 炸弹），已拒绝")
            if max_bytes > 0 and declared_total > max_bytes:
                raise ArchiveError(f"解压后总大小 {declared_total} 字节超过上限 {max_bytes} 字节")

            written_total = 0
            for zinfo in members:
                if zinfo.flag_bits & 0x1:
                    raise ArchiveError(f"包内含加密文件，已拒绝：{zinfo.filename}")
                if not _is_plain_file(zinfo):
                    raise ArchiveError(f"包内含非普通文件（如符号链接），已拒绝：{zinfo.filename}")

                target = _safe_member_path(tmp, zinfo.filename)
                data = zf.read(zinfo)
                if max_file_bytes > 0 and len(data) > max_file_bytes:
                    raise ArchiveError(
                        f"包内文件过大：{zinfo.filename}（{len(data)} 字节，上限 {max_file_bytes} 字节）"
                    )
                written_total += len(data)
                if max_bytes > 0 and written_total > max_bytes:
                    raise ArchiveError(f"解压后总大小超过上限 {max_bytes} 字节")

                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
    except ArchiveError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ArchiveError(f"压缩包解压失败：{exc}") from exc

    try:
        root = _locate_root(tmp)
    except ArchiveError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    return ExtractResult(root=root, tmp=tmp, files=_manifest(root))


def _locate_root(tmp: Path) -> Path:
    """定位含 SKILL.md 的包根：允许在 zip 根，或由唯一一层顶层目录包裹。"""
    if (tmp / SKILL_MD_NAME).is_file():
        return tmp

    entries = [p for p in sorted(tmp.iterdir()) if not _is_ignored(p.name)]
    if len(entries) == 1 and entries[0].is_dir() and (entries[0] / SKILL_MD_NAME).is_file():
        return entries[0]

    raise ArchiveError(
        f"包内未找到 {SKILL_MD_NAME}（需位于压缩包根目录，或被唯一一层顶层目录包裹）"
    )


def _manifest(root: Path) -> List[Dict[str, Any]]:
    """包内文件清单（相对包根的 POSIX 路径 + 大小 + sha256 + 类别），不含 SKILL.md。"""
    files: List[Dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.name == SKILL_MD_NAME:
            continue
        if any(_is_ignored(part) for part in rel.parts):
            continue
        data = path.read_bytes()
        files.append(
            {
                "path": rel.as_posix(),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "kind": kind_of(rel.as_posix()),
            }
        )
    return files


def read_package_file(root: Path, rel_path: str) -> Tuple[bytes, str]:
    """读取包内文件（相对包根）；越界抛 `ArchiveError`。返回 (内容, 文本)。

    非 UTF-8 文件不抛错，`text` 回落为空串（脚本/二进制只做扫描与展示）。
    """
    target = _safe_member_path(root, rel_path)
    if not target.is_file():
        raise ArchiveError(f"包内不存在文件：{rel_path}")
    data = target.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    return data, text


def commit(result: ExtractResult, base: Path, name: str) -> Path:
    """把解压结果原子地安装为 `<base>/<name>`；同名旧包先整体删除。"""
    final = base / name
    if final.exists():
        shutil.rmtree(final, ignore_errors=True)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(result.root, final)
    shutil.rmtree(result.tmp, ignore_errors=True)
    return final


def discard(result: ExtractResult) -> None:
    """放弃解压结果（校验未通过时清理临时目录）。"""
    shutil.rmtree(result.tmp, ignore_errors=True)


def remove_package(base: Path, name: str) -> None:
    """删除某个 skill 的落盘包（删除插件时调用）；路径固定在 base 内，防越界。"""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return
    target = base / name
    if target.resolve().parent != base.resolve():
        return
    shutil.rmtree(target, ignore_errors=True)


def cleanup_stale_temp(base: Path | str, *, max_age_seconds: int = 3600) -> int:
    """清理 `base` 下**遗留**的 `.tmp-*` 解压目录，返回删除数量。

    正常流程中临时目录由 `commit()` / `discard()` 清理；但进程被 SIGKILL / OOM 杀掉时
    会残留（只占磁盘，不影响正确性）。启动时调用一次即可。

    只删 `mtime` 早于 `max_age_seconds` 的目录：多实例共享同一 `base` 时，别的实例**正在**
    解压的目录（刚创建、mtime 是现在）不会被误删。任何异常都不抛出——启动清理不应阻断启动。
    """
    root = Path(base)
    if not root.is_dir():
        return 0
    cutoff = time.time() - max(0, int(max_age_seconds))
    removed = 0
    try:
        entries = list(root.glob(f"{_TMP_PREFIX}*"))
    except OSError:
        return 0
    for path in entries:
        try:
            if not path.is_dir() or path.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed
