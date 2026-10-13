"""Walks the source once and decides, per file, whether it needs copying.

Each source folder is compared against ONE listing of the matching destination
folder, instead of a separate lookup per file. On Windows, folder listings
already carry size and timestamp, so a resumed run over 500k files costs one
listing per folder on each side rather than 500k individual file lookups.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field

from .ctx import Ctx
from .util import FAT32_MAX, display, human

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(slots=True)
class Job:
    rel: str
    src: str
    dst: str
    size: int
    reason: str


@dataclass
class Plan:
    jobs: list[Job] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)  # relative, parents before children
    bytes_needed: int = 0  # net extra space the copy will consume
    bytes_to_copy: int = 0


def _join(rel_dir: str, name: str) -> str:
    return os.path.join(rel_dir, name) if rel_dir else name


def _is_link_dir(entry: os.DirEntry) -> bool:
    """Symlinks and junctions to directories are skipped (robocopy /XJ). File
    reparse points are NOT skipped: Windows Server deduplication uses them."""
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    return stat.S_ISDIR(st.st_mode) and bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def _dest_inside_rel(ctx: Ctx) -> str | None:
    """If the destination lives inside the source, its relative path (so the walk
    never recurses into its own output)."""
    try:
        s = os.path.normcase(os.path.realpath(ctx.src_root))
        d = os.path.normcase(os.path.realpath(ctx.dst_root))
        if os.path.commonpath([s, d]) == s and d != s:
            return os.path.relpath(d, s)
    except (OSError, ValueError):
        pass
    return None


def _list_dest(ctx: Ctx, path: str) -> dict[str, os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return {os.path.normcase(e.name): e for e in it}
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except OSError as e:
        # Can't tell; everything copies and the copy path reports real trouble.
        ctx.log.warn(f"cannot list destination {display(path)}: {e}")
        return {}


def _needs_copy(ctx: Ctx, src: os.stat_result, dst: os.stat_result) -> str | None:
    if src.st_size != dst.st_size:
        return "Changed"
    diff = src.st_mtime_ns - dst.st_mtime_ns
    tol = ctx.args.time_tolerance * 1_000_000_000
    if abs(diff) <= tol:
        return None
    if ctx.args.dst_compensate and abs(abs(diff) - 3_600_000_000_000) <= tol:
        return None
    return "Newer" if diff > 0 else "Older"


def scan(ctx: Ctx) -> Plan:
    a, s = ctx.args, ctx.stats
    plan = Plan()
    skip_rel = _dest_inside_rel(ctx)
    skip_rel = os.path.normcase(skip_rel) if skip_rel else None

    stack = [""]
    while stack and not ctx.stopped():
        rel_dir = stack.pop()
        src_dir = os.path.join(ctx.src_root, rel_dir) if rel_dir else ctx.src_root
        try:
            with os.scandir(src_dir) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as e:
            s.add("scan_errors")
            ctx.log.warn(f"cannot read source directory {display(src_dir)}: {e}")
            continue

        dst_dir = os.path.join(ctx.dst_root, rel_dir) if rel_dir else ctx.dst_root
        dest_index = _list_dest(ctx, dst_dir)
        subdirs: list[str] = []

        for ent in entries:
            if ctx.stopped():
                break
            rel = _join(rel_dir, ent.name)
            try:
                if _is_link_dir(ent):
                    ctx.log.info(f"skipping symlink/junction {rel}")
                    continue
                if ent.is_dir(follow_symlinks=False):
                    if a.no_recurse or ctx.filters.dir_excluded(rel) or os.path.normcase(rel) == skip_rel:
                        continue
                    s.add("dirs_total")
                    if not a.skip_empty_dirs:
                        plan.dirs.append(rel)
                    subdirs.append(rel)
                    continue
                if not ent.is_file(follow_symlinks=False) or ctx.filters.file_excluded(rel):
                    continue
                st = ent.stat(follow_symlinks=False)  # free on Windows: comes with the listing
            except OSError as e:
                s.add("scan_errors")
                ctx.log.warn(f"cannot stat {rel}: {e}")
                continue

            s.add("files_total")
            s.add("bytes_total", st.st_size)
            if s.files_total % 500 == 0:
                ctx.progress.set_message(f"scanning... {s.files_total} files, {len(plan.jobs)} to copy")

            if a.fat32 and st.st_size > FAT32_MAX:
                s.add("files_failed")
                ctx.log.error(f"{rel} is {human(st.st_size)} - too large for FAT32 (max 4 GiB); skipped")
                continue

            reason, dst_size = "New File", 0
            d = dest_index.get(os.path.normcase(ent.name))
            if d is not None:
                try:
                    if d.is_dir(follow_symlinks=False):
                        s.add("files_failed")
                        ctx.log.error(f"{rel}: destination has a directory where the source has a file")
                        continue
                    dst_st = d.stat(follow_symlinks=False)
                    r = _needs_copy(ctx, st, dst_st) or ("Forced" if a.force else None)
                    if r is None:
                        s.add("files_skipped")
                        s.add("bytes_skipped", st.st_size)
                        continue
                    reason, dst_size = r, dst_st.st_size
                except OSError as e:
                    ctx.log.warn(f"cannot stat destination for {rel}: {e}")

            plan.bytes_needed += max(0, st.st_size - dst_size)
            plan.bytes_to_copy += st.st_size
            plan.jobs.append(Job(rel, ent.path, os.path.join(ctx.dst_root, rel), st.st_size, reason))

        stack.extend(reversed(subdirs))  # depth-first, in name order
    return plan
