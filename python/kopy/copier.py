"""The per-file copy engine.

Each file is written to a hidden temp sibling, flushed to the device, optionally
verified, then atomically renamed over the target. Large files are pipelined: a
reader thread pulls chunks from the network share (hashing them as it goes) while
the worker thread writes the previous chunk to the USB drive, so network latency
and flash write time overlap. File I/O and hashing release the GIL, so these
threads genuinely run in parallel.
"""

from __future__ import annotations

import hashlib
import os
import queue
import stat
import threading
from contextlib import suppress

from .ctx import Ctx
from .errors import (DEST, SOURCE, Cancelled, CopyError, DiskFull, SourceChanged, TooLarge,
                     VerifyMismatch, classify)
from .scan import Job
from .util import FAT32_MAX, IS_WINDOWS, temp_path

try:  # optional, faster on big files
    import blake3 as _blake3

    new_hasher = _blake3.blake3
except ImportError:
    new_hasher = hashlib.blake2b

PIPELINE_DEPTH = 3  # chunks in flight per file: being read, queued, being written
_EOF = object()


def copy_with_retry(ctx: Ctx, job: Job) -> int:
    attempt = 0
    while True:
        written = [0]
        try:
            return _attempt(ctx, job, written)
        except CopyError as e:
            ctx.progress.rollback(written[0])  # keep the bar honest
            if e.retryable and attempt < ctx.args.retries and not ctx.stopped():
                attempt += 1
                ctx.stats.add("retries")
                wait = backoff(ctx.args.wait, attempt)
                ctx.log.warn(f"retry {attempt}/{ctx.args.retries} in {wait}s: {e}")
                if ctx.cancel.wait(wait):
                    raise Cancelled() from e
                continue
            raise


def backoff(base: int, attempt: int) -> int:
    return min(60, base * (1 << min(attempt - 1, 6)))


def _attempt(ctx: Ctx, job: Job, written: list[int]) -> int:
    if ctx.stopped():
        raise Cancelled()
    parent = os.path.dirname(job.dst)
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as e:
        raise classify("create directory", parent, DEST, e) from e
    tmp = temp_path(job.dst)
    try:
        return _write_and_commit(ctx, job, tmp, written)
    except BaseException:
        # Never leave half-written data behind; ignore failure (drive may be gone).
        with suppress(OSError):
            os.unlink(tmp)
        raise


def _write_and_commit(ctx: Ctx, job: Job, tmp: str, written: list[int]) -> int:
    a = ctx.args
    buf_size = a.buffer_mb * 1024 * 1024
    try:
        src = open(job.src, "rb", buffering=0)
    except OSError as e:
        raise classify("open", job.src, SOURCE, e) from e
    with src:
        try:
            st = os.fstat(src.fileno())
        except OSError as e:
            raise classify("stat", job.src, SOURCE, e) from e
        size = st.st_size
        try:
            out = open(tmp, "wb", buffering=0)
        except OSError as e:
            raise classify("create", job.dst, DEST, e) from e
        with out:
            if a.prealloc and size:
                try:
                    out.truncate(size)
                except OSError as e:
                    raise _write_error(e, job) from e
            if size <= buf_size:
                n, digest = _copy_small(ctx, job, src, out, written)
            else:
                n, digest = _copy_pipelined(ctx, job, src, out, buf_size, written)

            # Someone wrote to the file on the server while we were reading it.
            try:
                after = os.stat(job.src)
            except OSError as e:
                raise classify("stat", job.src, SOURCE, e) from e
            if n != size or after.st_size != size or after.st_mtime_ns != st.st_mtime_ns:
                raise SourceChanged(job.src)

            try:
                if a.prealloc and n != size:
                    out.truncate(n)
                if os.utime in os.supports_fd:  # Unix: stamp before fsync so it is durable too
                    os.utime(out.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
                if not a.no_fsync:
                    # Bytes must be on the flash, not in the OS cache, before the file
                    # counts as done: secure drives auto-lock and get yanked.
                    os.fsync(out.fileno())
                    _drop_page_cache(out.fileno())
            except OSError as e:
                raise classify("flush to device", job.dst, DEST, e) from e

    if os.utime not in os.supports_fd:  # Windows: set after close so close can't bump it
        try:
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError as e:
            raise classify("set timestamp", job.dst, DEST, e) from e

    if digest is not None:
        try:
            actual = _hash_uncached(tmp, buf_size)
        except OSError as e:
            raise classify("verify read", job.dst, DEST, e) from e
        if actual != digest:
            ctx.stats.add("mismatches")
            raise VerifyMismatch(job.dst)

    _replace(tmp, job.dst)
    # Carry the read-only attribute (Windows) / mode bits (Unix); best effort.
    with suppress(OSError):
        os.chmod(job.dst, stat.S_IMODE(st.st_mode))
    return n


def _write_error(e: OSError, job: Job) -> CopyError:
    """A write failing past 4 GiB on a "full" drive is really FAT32's file size
    limit; reporting it as disk-full would wrongly abort the whole run."""
    err = classify("write", job.dst, DEST, e)
    if isinstance(err, DiskFull) and job.size > FAT32_MAX:
        return TooLarge(job.dst, job.size)
    return err


def _write_all(out, mv: memoryview) -> None:
    while mv:
        n = out.write(mv)
        mv = mv[n:]


def _read_full(f, buf: bytearray) -> int:
    """Fill buf completely unless EOF; network reads often return short."""
    mv = memoryview(buf)
    filled = 0
    while filled < len(buf):
        n = f.readinto(mv[filled:])
        if not n:
            break
        filled += n
    return filled


def _copy_small(ctx: Ctx, job: Job, src, out, written: list[int]) -> tuple[int, bytes | None]:
    try:
        data = src.readall()
    except OSError as e:
        raise classify("read", job.src, SOURCE, e) from e
    if ctx.stopped():
        raise Cancelled()
    try:
        _write_all(out, memoryview(data))
    except OSError as e:
        raise _write_error(e, job) from e
    written[0] += len(data)
    ctx.progress.inc(len(data))
    return len(data), (new_hasher(data).digest() if ctx.args.verify else None)


def _copy_pipelined(ctx: Ctx, job: Job, src, out, buf_size: int,
                    written: list[int]) -> tuple[int, bytes | None]:
    free: queue.SimpleQueue = queue.SimpleQueue()
    full: queue.SimpleQueue = queue.SimpleQueue()
    for _ in range(PIPELINE_DEPTH):
        free.put(bytearray(buf_size))  # memory per file is capped at 3 buffers
    hasher = new_hasher() if ctx.args.verify else None
    result: dict[str, bytes | None] = {}

    def reader() -> None:
        try:
            while True:
                buf = free.get()
                if buf is None:  # writer gave up
                    return
                if ctx.stopped():
                    full.put(Cancelled())
                    return
                n = _read_full(src, buf)
                if n == 0:
                    result["digest"] = hasher.digest() if hasher else None
                    full.put(_EOF)
                    return
                if hasher:
                    hasher.update(memoryview(buf)[:n])
                full.put((buf, n))
        except OSError as e:
            full.put(classify("read", job.src, SOURCE, e))
        except BaseException as e:  # noqa: BLE001 - hand anything to the writer
            full.put(e)

    t = threading.Thread(target=reader, name="kopy-reader", daemon=True)
    t.start()
    total = 0
    failure: BaseException | None = None
    try:
        while True:
            item = full.get()
            if item is _EOF:
                break
            if isinstance(item, BaseException):
                failure = item
                break
            buf, n = item
            try:
                _write_all(out, memoryview(buf)[:n])
            except OSError as e:
                failure = _write_error(e, job)
                break
            total += n
            written[0] += n
            ctx.progress.inc(n)
            free.put(buf)
    finally:
        free.put(None)  # unblock the reader whichever way it is waiting
        t.join()
    if failure is not None:
        raise failure
    return total, result.get("digest")


def _replace(tmp: str, dst: str) -> None:
    """Atomic replace. Windows refuses to rename over a read-only file, so clear
    the attribute and try once more."""
    try:
        os.replace(tmp, dst)
    except PermissionError as e:
        if not clear_readonly(dst):
            raise classify("replace", dst, DEST, e) from e
        try:
            os.replace(tmp, dst)
        except OSError as e2:
            raise classify("replace", dst, DEST, e2) from e2
    except OSError as e:
        raise classify("replace", dst, DEST, e) from e


def clear_readonly(path: str) -> bool:
    try:
        mode = os.stat(path).st_mode
        if mode & stat.S_IWRITE:
            return False
        os.chmod(path, mode | stat.S_IWRITE)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- verification I/O
# Verify must read what is on the flash. After fsync, evict the file from the
# page cache (Linux) or read with caching disabled (Windows).

def _drop_page_cache(fd: int) -> None:
    if hasattr(os, "posix_fadvise"):
        with suppress(OSError):
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)


def _hash_cached(path: str, buf_size: int) -> bytes:
    h = new_hasher()
    buf = bytearray(buf_size)
    mv = memoryview(buf)
    with open(path, "rb", buffering=0) as f:
        while n := _read_full(f, buf):
            h.update(mv[:n])
    return h.digest()


def _hash_uncached(path: str, buf_size: int) -> bytes:
    if IS_WINDOWS:
        try:
            from .winio import hash_uncached

            return hash_uncached(path, buf_size, new_hasher)
        except OSError:
            pass  # some drivers reject unbuffered I/O; fall back to cached reads
    return _hash_cached(path, buf_size)
