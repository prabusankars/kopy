"""Single-line console progress: elapsed, bar, bytes, rate, ETA, file count.
Log lines are printed above it without tearing. Hidden when not a terminal."""

from __future__ import annotations

import shutil
import sys
import threading
import time
from collections import deque

from .util import human

_SPIN = "|/-\\"


class Progress:
    def __init__(self, enabled: bool):
        self.enabled = enabled and sys.stderr.isatty()
        # Re-entrant: the Ctrl-C handler runs on the main thread and may print
        # while the main thread already holds the lock.
        self._lock = threading.RLock()
        self._total = 0
        self._pos = 0
        self._msg = ""
        self._start = time.monotonic()
        self._samples: deque[tuple[float, int]] = deque()
        self._tick = 0
        self._last_len = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- state
    def set_total(self, n: int) -> None:
        with self._lock:
            self._total = n

    def inc(self, n: int) -> None:
        with self._lock:
            self._pos += n

    def rollback(self, n: int) -> None:
        with self._lock:
            self._pos = max(0, self._pos - n)

    def set_message(self, msg: str) -> None:
        with self._lock:
            self._msg = msg

    # ---------------------------------------------------------------- output
    def start(self) -> None:
        if self.enabled and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="kopy-progress", daemon=True)
            self._thread.start()

    def write(self, line: str) -> None:
        with self._lock:
            if self.enabled:
                self._clear()
            sys.stderr.write(line + "\n")
            if self.enabled:
                self._render()
            sys.stderr.flush()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        with self._lock:
            if self.enabled:
                self._clear()
                sys.stderr.flush()

    def _run(self) -> None:
        while not self._stop.wait(0.2):
            with self._lock:
                self._render()
                sys.stderr.flush()

    def _clear(self) -> None:
        sys.stderr.write("\r" + " " * self._last_len + "\r")
        self._last_len = 0

    def _render(self) -> None:
        now = time.monotonic()
        self._samples.append((now, self._pos))
        while len(self._samples) > 2 and now - self._samples[0][0] > 5:
            self._samples.popleft()
        t0, p0 = self._samples[0]
        rate = (self._pos - p0) / (now - t0) if now - t0 > 0.5 else 0.0

        el = int(now - self._start)
        elapsed = f"{el // 3600:02}:{el % 3600 // 60:02}:{el % 60:02}"
        width = 30
        frac = min(1.0, self._pos / self._total) if self._total else 0.0
        done = int(frac * width)
        bar = "=" * done + (">" if done < width else "") + " " * max(0, width - done - 1)
        remaining = self._total - self._pos
        eta = f"{int(remaining / rate)}s" if rate > 0 and remaining > 0 else "-"
        self._tick += 1

        line = (
            f"{_SPIN[self._tick % 4]} {elapsed} [{bar}] {human(self._pos)}/{human(self._total)} "
            f"{human(rate)}/s eta {eta} {self._msg}"
        )
        cols = shutil.get_terminal_size((120, 20)).columns - 1
        line = line[:cols]
        pad = max(0, self._last_len - len(line))
        sys.stderr.write("\r" + line + " " * pad)
        self._last_len = len(line)
