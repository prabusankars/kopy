from __future__ import annotations

import argparse

from . import __version__


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="kopy",
        description="Robust, fast robocopy-style copier tuned for network share -> USB secure drive.",
    )
    p.add_argument("source", help=r"source directory (local path or UNC share, e.g. \\server\share\data)")
    p.add_argument("dest", help=r"destination directory (e.g. E:\Backup on the Kanguru drive)")
    p.add_argument("--version", action="version", version=f"kopy {__version__}")

    g = p.add_argument_group("what to copy")
    g.add_argument("--mirror", action="store_true", help="copy changes AND delete dest entries no longer in source (robocopy /MIR)")
    g.add_argument("--purge", action="store_true", help="delete dest entries no longer in source (robocopy /PURGE)")
    g.add_argument("--no-recurse", action="store_true", help="only copy the top level of source")
    g.add_argument("--skip-empty-dirs", action="store_true", help="don't pre-create directories without copied files (robocopy /S)")
    g.add_argument("--xf", dest="exclude_files", action="append", default=[], metavar="GLOB",
                   help="exclude files whose name or relative path matches GLOB; repeatable (robocopy /XF)")
    g.add_argument("--xd", dest="exclude_dirs", action="append", default=[], metavar="GLOB",
                   help="exclude directories whose name or relative path matches GLOB; repeatable (robocopy /XD)")
    g.add_argument("--include", action="append", default=[], metavar="GLOB",
                   help="only copy files whose name or relative path matches GLOB; repeatable")

    g = p.add_argument_group("change detection")
    g.add_argument("--time-tolerance", type=int, default=2, metavar="SECS",
                   help="timestamp tolerance; 2 absorbs FAT/exFAT granularity (robocopy /FFT) [2]")
    g.add_argument("--dst-compensate", action="store_true",
                   help="treat exact one-hour timestamp shifts as equal; FAT stores local time (robocopy /DST)")
    g.add_argument("--force", action="store_true", help="copy every file even if dest looks up to date (robocopy /IS /IT)")
    g.add_argument("--fat32", action="store_true", help="destination is FAT32: reject files of 4 GiB or more up front")

    g = p.add_argument_group("performance")
    g.add_argument("-t", "--threads", type=int, default=8, help="concurrent files (robocopy /MT) [8]")
    g.add_argument("--large-slots", type=int, default=1,
                   help="max large files written at once; 1 keeps USB writes sequential [1]")
    g.add_argument("--large-mb", type=int, default=64, help="files of this many MiB or more count as large [64]")
    g.add_argument("--buffer-mb", type=int, default=4,
                   help="I/O chunk size in MiB; 3 chunks in flight per large file [4]")
    g.add_argument("--prealloc", action="store_true",
                   help="preallocate each file's full size first (exFAT/NTFS; avoid on FAT32)")

    g = p.add_argument_group("safety")
    g.add_argument("-r", "--retries", type=int, default=5, help="retries per file on transient errors (robocopy /R) [5]")
    g.add_argument("-w", "--wait", type=int, default=2,
                   help="base seconds between retries; doubles each time, max 60 (robocopy /W) [2]")
    g.add_argument("--verify", action="store_true",
                   help="re-read each file from the destination and compare hashes before committing it")
    g.add_argument("--skip-space-check", action="store_true", help="don't check free space before starting")
    g.add_argument("--no-fsync", action="store_true",
                   help="skip flushing each file to the device (faster; unsafe if the drive is pulled or auto-locks)")

    g = p.add_argument_group("output")
    g.add_argument("-n", "--dry-run", action="store_true", help="show what would happen without changing anything (robocopy /L)")
    g.add_argument("--log", metavar="FILE", help="append a detailed log to FILE (robocopy /LOG+)")
    g.add_argument("-v", "--verbose", action="store_true", help="print every copied file")
    g.add_argument("-q", "--quiet", action="store_true", help="hide the progress line")

    args = p.parse_args(argv)
    if args.threads < 1:
        p.error("--threads must be at least 1")
    if args.large_slots < 1:
        p.error("--large-slots must be at least 1")
    if not 1 <= args.buffer_mb <= 256:
        p.error("--buffer-mb must be 1..256")
    args.purge_enabled = args.mirror or args.purge
    return args
