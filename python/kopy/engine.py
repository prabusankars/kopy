from __future__ import annotations

import argparse
import os
import queue
import shutil
import signal
import threading
import time
import traceback

from . import __version__, extras
from .copier import copy_with_retry
from .ctx import Ctx, Logger
from .errors import Cancelled, CopyError, SourceVanished, classify, DEST
from .filters import Filters
from .progress import Progress
from .report import summary
from .scan import Job, scan
from .util import display, human, long_path


class FatalError(Exception):
    """Nothing can be copied at all (bad source, no space, unusable destination)."""


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    src, dst = long_path(args.source), long_path(args.dest)
    if not os.path.isdir(src):
        raise FatalError(f"source is not an accessible directory: {args.source}")
    if not args.dry_run:
        try:
            os.makedirs(dst, exist_ok=True)
        except OSError as e:
            raise FatalError(f"cannot create destination {args.dest}: {e}") from e

    progress = Progress(enabled=not args.quiet)
    try:
        log = Logger(args.log, progress, args.verbose)
    except OSError as e:
        raise FatalError(f"cannot open log file {args.log}: {e}") from e
    ctx = Ctx(args=args, src_root=src, dst_root=dst,
              filters=Filters(args.exclude_files, args.exclude_dirs, args.include),
              log=log, progress=progress)
    _install_ctrl_c(ctx)

    log.file_only("INFO", f"kopy {__version__} (python) start")
    log.file_only("INFO", f"source: {display(src)}")
    log.file_only("INFO", f"dest:   {display(dst)}")
    log.file_only("INFO", f"options: {vars(args)}")

    progress.start()
    try:
        progress.set_message("scanning...")
        plan = scan(ctx)
        log.info(f"scan: {ctx.stats.files_total} files, {len(plan.jobs)} to copy ({human(plan.bytes_to_copy)})")

        if not args.dry_run and not ctx.stopped():
            if not args.skip_space_check:
                _preflight_space(ctx, plan.bytes_needed)
            _create_dirs(ctx, plan.dirs)

        if args.dry_run:
            for job in plan.jobs:
                log.console(f"  would copy [{job.reason}] {job.rel} ({human(job.size)})")
        elif not ctx.stopped():
            progress.set_total(plan.bytes_to_copy)
            progress.set_message(f"0/{len(plan.jobs)} files")
            _run_jobs(ctx, plan.jobs)

        if not ctx.stopped():
            progress.set_message("checking extras")
            extras.process(ctx)
    finally:
        progress.close()

    code = summary(ctx, time.monotonic() - started)
    log.close()
    return code


def _install_ctrl_c(ctx: Ctx) -> None:
    if threading.current_thread() is not threading.main_thread():
        return

    def handler(signum, frame):
        if ctx.cancel.is_set():
            os._exit(16)  # second Ctrl-C: force quit
        ctx.cancel.set()
        ctx.log.console("\nkopy: interrupted - cleaning up (Ctrl-C again to force quit)")

    signal.signal(signal.SIGINT, handler)


def _preflight_space(ctx: Ctx, needed: int) -> None:
    try:
        free = shutil.disk_usage(ctx.dst_root).free
    except OSError as e:
        ctx.log.warn(f"could not determine free space: {e}")
        return
    if free < needed:
        msg = f"not enough space on destination: need {human(needed)} more, {human(free)} free"
        ctx.log.error(msg)
        raise FatalError(msg)


def _create_dirs(ctx: Ctx, dirs: list[str]) -> None:
    for rel in dirs:
        if ctx.stopped():
            return
        d = os.path.join(ctx.dst_root, rel)
        if os.path.isdir(d):
            continue
        try:
            os.makedirs(d, exist_ok=True)
            ctx.stats.add("dirs_created")
        except OSError as e:
            err = classify("create directory", d, DEST, e)
            ctx.log.error(str(err))
            if err.fatal:
                ctx.abort()


def _run_jobs(ctx: Ctx, jobs: list[Job]) -> None:
    """Fan files out to worker threads. Large files also need one of a few
    "slots", so big sequential writes don't interleave on the flash."""
    large = ctx.args.large_mb * 1024 * 1024
    total = len(jobs)
    q: queue.SimpleQueue[Job] = queue.SimpleQueue()
    for j in jobs:
        q.put(j)
    slots = threading.Semaphore(ctx.args.large_slots)

    def worker() -> None:
        while not ctx.stopped():
            try:
                job = q.get_nowait()
            except queue.Empty:
                return
            big = job.size >= large
            if big:
                while not slots.acquire(timeout=0.2):
                    if ctx.stopped():
                        return
            try:
                _process_job(ctx, job)
            finally:
                if big:
                    slots.release()
            s = ctx.stats
            done = s.files_copied + s.files_failed + s.files_vanished
            failed = f" ({s.files_failed} failed)" if s.files_failed else ""
            ctx.progress.set_message(f"{done}/{total} files{failed}")

    threads = [threading.Thread(target=worker, name=f"kopy-worker-{i}", daemon=True)
               for i in range(ctx.args.threads)]
    for t in threads:
        t.start()
    for t in threads:
        while t.is_alive():  # short joins keep the main thread responsive to Ctrl-C
            t.join(0.2)


def _process_job(ctx: Ctx, job: Job) -> None:
    s = ctx.stats
    try:
        n = copy_with_retry(ctx, job)
    except Cancelled:
        return
    except SourceVanished:
        s.add("files_vanished")
        ctx.log.warn(f"{job.rel} vanished from source; skipped")
        return
    except CopyError as e:
        s.add("files_failed")
        ctx.log.error(f"FAILED {job.rel}: {e}")
        if e.fatal:
            ctx.log.error("aborting: destination drive is unusable")
            ctx.abort()
        return
    except Exception as e:  # a bug must fail one file, not the run
        s.add("files_failed")
        ctx.log.error(f"FAILED {job.rel}: unexpected error: {e!r}")
        ctx.log.file_only("ERROR", traceback.format_exc())
        return
    s.add("files_copied")
    s.add("bytes_copied", n)
    ctx.log.info(f"{job.reason:<9} {human(n):>10}  {job.rel}")
