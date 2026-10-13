"""Windows-only: read a file with the OS cache bypassed (FILE_FLAG_NO_BUFFERING),
so --verify checks what is actually on the flash, not what is still in RAM.

Unbuffered reads need a sector-aligned buffer and sector-multiple lengths; an
anonymous mmap is page-aligned, which satisfies both 512-byte and 4K devices.
"""

from __future__ import annotations

import ctypes
import mmap
from ctypes import wintypes
from typing import Callable

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x1
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
ERROR_HANDLE_EOF = 38
ALIGN = 4096

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
_CreateFileW = _k32.CreateFileW
_CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_CreateFileW.restype = wintypes.HANDLE
_ReadFile = _k32.ReadFile
_ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                      ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
_ReadFile.restype = wintypes.BOOL
_CloseHandle = _k32.CloseHandle
_CloseHandle.argtypes = [wintypes.HANDLE]
_CloseHandle.restype = wintypes.BOOL
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def hash_uncached(path: str, chunk: int, new_hasher: Callable) -> bytes:
    chunk = -(-chunk // ALIGN) * ALIGN
    handle = _CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING,
                          FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN, None)
    if handle is None or handle == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    buf = mmap.mmap(-1, chunk)
    cbuf = (ctypes.c_char * chunk).from_buffer(buf)
    try:
        hasher = new_hasher()
        got = wintypes.DWORD()
        while True:
            if not _ReadFile(handle, ctypes.addressof(cbuf), chunk, ctypes.byref(got), None):
                err = ctypes.get_last_error()
                if err == ERROR_HANDLE_EOF:
                    break
                raise ctypes.WinError(err)
            n = got.value
            if n == 0:
                break
            hasher.update(buf[:n])
            if n < chunk:
                break  # short read only happens at EOF with unbuffered I/O
        return hasher.digest()
    finally:
        _CloseHandle(handle)
        del cbuf
        buf.close()
