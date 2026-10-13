from __future__ import annotations

import os
import sys

FAT32_MAX = 4 * 1024 * 1024 * 1024 - 1
TEMP_SUFFIX = ".kopy-tmp"
IS_WINDOWS = sys.platform == "win32"


def temp_path(dst: str) -> str:
    """Hidden sibling the file is written to before an atomic rename, so an
    interrupted copy never leaves a truncated file under the real name."""
    head, tail = os.path.split(dst)
    return os.path.join(head, f".{tail}{TEMP_SUFFIX}")


def is_temp_name(name: str) -> bool:
    return name.startswith(".") and name.endswith(TEMP_SUFFIX)


def long_path(p: str) -> str:
    r"""Absolute path; on Windows with the \\?\ prefix so paths over 260
    characters work without the LongPathsEnabled registry setting."""
    p = os.path.abspath(p)
    if not IS_WINDOWS or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def display(p: str) -> str:
    r"""Strip the \\?\ prefix for human-facing messages."""
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def human(n: float) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    v, u = float(n), 0
    while v >= 1024 and u < len(units) - 1:
        v /= 1024
        u += 1
    return f"{int(n)} B" if u == 0 else f"{v:.1f} {units[u]}"
