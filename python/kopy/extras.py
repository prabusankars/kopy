"""Finds (and with --purge/--mirror deletes) destination entries that no longer
exist in the source. Each destination folder is checked against ONE listing of
the matching source folder, so no per-file round trips to the share.
Deliberately conservative: anything uncertain is kept."""

from __future__ import annotations

import os
from contextlib import suppress

from .copier import clear_readonly
from .ctx import Ctx
from .util import display, is_temp_name

_UNKNOWN = object()  # source folder unreadable: keep everything beneath


def process(ctx: Ctx) -> None:
    a, s = ctx.args, ctx.stats
    if not os.path.isdir(ctx.dst_root):
        return

    delete = a.purge_enabled and not a.dry_run
    if delete and (s.files_failed or s.scan_errors or ctx.stopped()):
        ctx.log.warn("purge skipped because this run had errors; extra files are only reported. "
                     "Re-run once the errors are fixed.")
        delete = False
    # If the share dropped, every file would look "extra". Never purge blind.
    if delete and not os.path.isdir(ctx.src_root):
        ctx.log.warn("purge skipped: source is no longer reachable")
        delete = False

    log_path = os.path.normcase(os.path.abspath(a.log)) if a.log else None
    _process_dir(ctx, "", delete, log_path, src_missing=False)


def _source_index(ctx: Ctx, rel: str):
    path = os.path.join(ctx.src_root, rel) if rel else ctx.src_root
    try:
        with os.scandir(path) as it:
            return {os.path.normcase(e.name): e.is_dir(follow_symlinks=False) for e in it}
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except OSError as e:
        ctx.log.warn(f"cannot list source {display(path)}: {e}; keeping its destination contents")
        return _UNKNOWN


def _process_dir(ctx: Ctx, rel: str, delete: bool, log_path: str | None, src_missing: bool) -> None:
    a, s = ctx.args, ctx.stats
    dst_dir = os.path.join(ctx.dst_root, rel) if rel else ctx.dst_root
    try:
        with os.scandir(dst_dir) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError as e:
        ctx.log.warn(f"cannot list destination {display(dst_dir)}: {e}")
        return
    index = {} if src_missing else _source_index(ctx, rel)

    for ent in entries:
        if ctx.stopped():
            return
        r = os.path.join(rel, ent.name) if rel else ent.name
        try:
            is_dir = ent.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False

        if not is_dir and is_temp_name(ent.name):
            # Leftover from an interrupted run.
            if not a.dry_run:
                with suppress(OSError):
                    os.unlink(ent.path)
                    ctx.log.info(f"removed stale temp file {r}")
            continue
        if is_dir and (ctx.filters.dir_excluded(r) or a.no_recurse):
            continue
        if not is_dir and ctx.filters.file_excluded(r):
            continue
        if log_path and os.path.normcase(os.path.abspath(ent.path)) == log_path:
            continue
        if index is _UNKNOWN:
            continue

        src_is_dir = index.get(os.path.normcase(ent.name))
        extra = src_is_dir is None or src_is_dir != is_dir
        if is_dir:
            # Children of an extra folder are all extra too.
            _process_dir(ctx, r, delete, log_path, src_missing=extra)
        if not extra:
            continue

        s.add("extras_dirs" if is_dir else "extras_files")
        kind = "dir " if is_dir else "file"
        if not delete:
            if a.dry_run and a.purge_enabled:
                ctx.log.console(f"  would delete {kind} {r}")
            ctx.log.info(f"*EXTRA {kind} {r}")
            continue
        try:
            if is_dir:
                os.rmdir(ent.path)
            else:
                _remove_file_force(ent.path)
            s.add("extras_deleted")
            ctx.log.info(f"*EXTRA {kind} deleted {r}")
        except OSError as e:
            ctx.log.warn(f"could not delete extra {r}: {e}")


def _remove_file_force(path: str) -> None:
    try:
        os.unlink(path)
    except PermissionError:
        if not clear_readonly(path):
            raise
        os.unlink(path)
