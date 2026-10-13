"""Error model. Every OS error is classified once, where it happens, into
"retry it", "skip this file", or "stop the whole run" (the drive is unusable)."""

from __future__ import annotations

import errno

from .util import display

SOURCE = "source"
DEST = "dest"


class CopyError(Exception):
    retryable = False  # worth trying the same file again after a pause
    fatal = False  # once retries are exhausted, no further file can succeed

    def __init__(self, message: str, path: str = ""):
        super().__init__(message)
        self.path = path


class SourceVanished(CopyError):
    def __init__(self, path: str):
        super().__init__(f"source file vanished before it could be copied: {display(path)}", path)


class DiskFull(CopyError):
    fatal = True

    def __init__(self, path: str):
        super().__init__(f"destination is full while writing {display(path)}", path)


class WriteProtected(CopyError):
    fatal = True

    def __init__(self, path: str):
        super().__init__(
            f"destination is read-only or write-protected ({display(path)}) - check the drive's "
            "write-protect switch and that it is unlocked",
            path,
        )


class DeviceGone(CopyError):
    fatal = True
    retryable = True

    def __init__(self, path: str):
        super().__init__(
            f"destination device disconnected or not ready ({display(path)}) - was the drive "
            "removed, or did it auto-lock?",
            path,
        )


class TooLarge(CopyError):
    def __init__(self, path: str, size: int):
        super().__init__(
            f"file too large for the destination filesystem ({size} bytes): {display(path)} - FAT32 "
            "cannot hold files of 4 GiB or more; reformat the drive as exFAT or NTFS",
            path,
        )


class VerifyMismatch(CopyError):
    retryable = True

    def __init__(self, path: str):
        super().__init__(f"verification failed, destination content differs from source: {display(path)}", path)


class SourceChanged(CopyError):
    retryable = True

    def __init__(self, path: str):
        super().__init__(f"source changed while it was being copied: {display(path)}", path)


class Cancelled(CopyError):
    def __init__(self):
        super().__init__("cancelled")


class IoFailure(CopyError):
    def __init__(self, op: str, path: str, err: OSError, retryable: bool):
        detail = err.strerror or str(err)
        code = getattr(err, "winerror", None) or err.errno
        super().__init__(f"{op} failed for {display(path)}: {detail} (os error {code})", path)
        self.retryable = retryable


# ---------------------------------------------------------------- classification

_DISK_FULL, _WRITE_PROTECTED, _DEVICE_GONE, _TRANSIENT, _PERMANENT = range(5)

_WINERROR = {
    39: _DISK_FULL,          # ERROR_HANDLE_DISK_FULL
    112: _DISK_FULL,         # ERROR_DISK_FULL
    19: _WRITE_PROTECTED,    # ERROR_WRITE_PROTECT (Kanguru physical switch)
    21: _DEVICE_GONE,        # ERROR_NOT_READY
    433: _DEVICE_GONE,       # ERROR_NO_SUCH_DEVICE
    1167: _DEVICE_GONE,      # ERROR_DEVICE_NOT_CONNECTED
    32: _TRANSIENT,          # ERROR_SHARING_VIOLATION (file open on the server)
    33: _TRANSIENT,          # ERROR_LOCK_VIOLATION
    51: _TRANSIENT,          # ERROR_REM_NOT_LIST
    53: _TRANSIENT,          # ERROR_BAD_NETPATH
    59: _TRANSIENT,          # ERROR_UNEXP_NET_ERR
    64: _TRANSIENT,          # ERROR_NETNAME_DELETED (SMB session dropped)
    67: _TRANSIENT,          # ERROR_BAD_NET_NAME
    121: _TRANSIENT,         # ERROR_SEM_TIMEOUT
    1117: _TRANSIENT,        # ERROR_IO_DEVICE
    1231: _TRANSIENT,        # ERROR_NETWORK_UNREACHABLE
    1236: _TRANSIENT,        # ERROR_CONNECTION_ABORTED
    1450: _TRANSIENT,        # ERROR_NO_SYSTEM_RESOURCES (large SMB reads)
}


def _errnos(*names: str) -> frozenset[int]:
    return frozenset(getattr(errno, n) for n in names if hasattr(errno, n))


_E_DISK_FULL = _errnos("ENOSPC", "EDQUOT")
_E_WRITE_PROTECTED = _errnos("EROFS")
_E_DEVICE_GONE = _errnos("ENODEV", "ENXIO")
_E_TRANSIENT = _errnos(
    "EIO", "ETIMEDOUT", "ESTALE", "EAGAIN", "EINTR", "EBUSY", "EHOSTDOWN", "EHOSTUNREACH",
    "ENETDOWN", "ENETUNREACH", "ECONNRESET", "ECONNABORTED", "ENOTCONN",
)


def _class_of(err: OSError, side: str) -> int:
    win = getattr(err, "winerror", None)
    if win in _WINERROR:
        return _WINERROR[win]
    e = err.errno
    if e in _E_DISK_FULL:
        return _DISK_FULL
    if e in _E_WRITE_PROTECTED:
        return _WRITE_PROTECTED
    if e in _E_DEVICE_GONE:
        return _DEVICE_GONE
    if e in _E_TRANSIENT or isinstance(err, (TimeoutError, ConnectionError, InterruptedError, BlockingIOError)):
        return _TRANSIENT
    if isinstance(err, FileNotFoundError) and side == DEST:
        return _TRANSIENT  # parent dir removed underneath us; recreated on retry
    return _PERMANENT


def classify(op: str, path: str, side: str, err: OSError) -> CopyError:
    cls = _class_of(err, side)
    if side == SOURCE and isinstance(err, FileNotFoundError) and cls != _TRANSIENT:
        return SourceVanished(path)
    if side == DEST:
        if cls == _DISK_FULL:
            return DiskFull(path)
        if cls == _WRITE_PROTECTED:
            return WriteProtected(path)
        if cls == _DEVICE_GONE:
            return DeviceGone(path)
    # A dropped share or busy source file is worth waiting for.
    return IoFailure(op, path, err, retryable=cls in (_TRANSIENT, _DEVICE_GONE))
