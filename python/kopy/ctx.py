from __future__ import annotations

import argparse
import threading
from dataclasses import dataclass, field
from datetime import datetime

from .filters import Filters
from .progress import Progress


class Stats:
    FIELDS = (
        "dirs_total", "dirs_created",
        "files_total", "files_copied", "files_skipped", "files_failed", "files_vanished",
        "bytes_total", "bytes_copied", "bytes_skipped",
        "extras_files", "extras_dirs", "extras_deleted",
        "mismatches", "retries", "scan_errors",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        for f in self.FIELDS:
            setattr(self, f, 0)

    def add(self, name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + n)


class Logger:
    """Console (through the progress line) plus an optional append-only log file.
    A failing log must never fail the copy."""

    def __init__(self, path: str | None, progress: Progress, verbose: bool):
        self._f = open(path, "a", encoding="utf-8") if path else None
        self._lock = threading.Lock()
        self.progress = progress
        self.verbose = verbose

    def file_only(self, level: str, msg: str) -> None:
        if self._f is None:
            return
        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            try:
                self._f.write(f"{ts} {level:<5} {msg}\n")
            except OSError:
                pass

    def console(self, msg: str) -> None:
        self.progress.write(msg)

    def info(self, msg: str) -> None:
        self.file_only("INFO", msg)
        if self.verbose:
            self.console(msg)

    def warn(self, msg: str) -> None:
        self.file_only("WARN", msg)
        self.console(f"WARN  {msg}")

    def error(self, msg: str) -> None:
        self.file_only("ERROR", msg)
        self.console(f"ERROR {msg}")

    def close(self) -> None:
        if self._f is not None:
            with self._lock:
                try:
                    self._f.close()
                except OSError:
                    pass


@dataclass
class Ctx:
    args: argparse.Namespace
    src_root: str
    dst_root: str
    filters: Filters
    log: Logger
    progress: Progress
    stats: Stats = field(default_factory=Stats)
    cancel: threading.Event = field(default_factory=threading.Event)
    fatal: threading.Event = field(default_factory=threading.Event)

    def stopped(self) -> bool:
        return self.cancel.is_set()

    def abort(self) -> None:
        """The destination is unusable: stop every worker."""
        self.fatal.set()
        self.cancel.set()
