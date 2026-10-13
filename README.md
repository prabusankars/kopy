# kopy

A robocopy-style file copier in Rust, tuned for copying from a Windows file server
(SMB share) to a USB secure drive such as a Kanguru Defender.

> **Python version:** [`python/`](python/) has a standard-library-only Python port
> with the same flags, safety behaviour and exit codes, plus a PyInstaller build
> script with an icon. See [python/README.md](python/README.md).

## Build (on the Windows machine)

```powershell
# one-time: install Rust from https://rustup.rs (MSVC toolchain)
cd kopy
cargo build --release
# binary: target\release\kopy.exe  (single static exe, copy it anywhere)
# build.rs embeds assets\kopy.ico and version info (needs the Windows SDK's rc.exe)
cargo test
```

## Usage

```powershell
# Incremental copy (robocopy src dst /E)
kopy \\fileserver\projects E:\Backup\projects

# Mirror with verification and a log (robocopy /MIR /LOG+:...)
kopy \\fileserver\projects E:\Backup\projects --mirror --verify --log C:\logs\kopy.log

# Preview only (robocopy /L)
kopy \\fileserver\projects E:\Backup\projects --mirror -n

# Exclusions (robocopy /XD /XF)
kopy \\fs\data E:\data --xd node_modules --xd .git --xf "*.tmp" --xf "~$*"
```

Exit codes use robocopy's bit layout, so existing batch/Task Scheduler checks keep
working: `1` copied, `2` extras found, `4` verify mismatch, `8` failures, `16` fatal.
`< 8` means success.

## robocopy flag mapping

| robocopy        | kopy                         | notes |
|-----------------|------------------------------|-------|
| `/E`            | default                      | `--skip-empty-dirs` for `/S` |
| `/MIR`          | `--mirror`                   | |
| `/PURGE`        | `--purge`                    | |
| `/MT:n`         | `-t n` (default 8)           | |
| `/R:n` `/W:n`   | `-r n` `-w n` (5, 2s)        | backoff doubles, capped at 60s |
| `/XF` `/XD`     | `--xf GLOB` `--xd GLOB`      | repeatable, globs on name or relative path |
| `/L`            | `-n` / `--dry-run`           | |
| `/LOG+:file`    | `--log file`                 | always appends |
| `/FFT`          | `--time-tolerance 2` (default) | |
| `/DST`          | `--dst-compensate`           | |
| `/IS /IT`       | `--force`                    | |
| `/V` `/NP`      | `-v` `-q`                    | |

## What it does differently from robocopy

**Faster for this specific path**
- *Pipelined I/O per file:* a reader thread pulls 4 MiB chunks from the share while
  the writer pushes the previous chunk to the USB drive, so network latency and
  flash write time overlap instead of adding up.
- *Large-file slots:* small files use all threads (hides SMB per-file latency), but
  only `--large-slots` (default 1) big files write at once. Interleaving several
  large sequential writes on USB flash fragments its write pattern and is slower.
- *Verification for free on the read side:* `--verify` hashes with BLAKE3 while
  reading the source, so only the destination is re-read. The re-read bypasses the
  OS cache (unbuffered I/O on Windows) so it checks what is actually on the flash.

**Safer**
- *Atomic writes:* each file goes to a hidden `.<name>.kopy-tmp`, is flushed to the
  device, optionally verified, then renamed into place. A pulled drive or auto-lock
  never leaves a truncated file under the real name. Stale temps are cleaned up on
  the next run.
- *Errors are classified, not just retried:*
  - transient (dropped SMB session, sharing violation, timeouts) → retry with backoff
  - source file deleted mid-run → skipped with a warning
  - source modified during copy → retried
  - **drive full, write-protect switch on, drive removed/locked → whole run stops
    immediately** with a specific message instead of failing 10,000 files one by one
  - >4 GiB file on FAT32 → reported as a filesystem limit, not "disk full"
- *Free-space preflight:* fails before copying anything if the drive can't hold it.
- *Conservative purge:* `--mirror` won't delete anything if the run had any
  errors, or if the source has become unreachable (so a dropped share can't make
  every file look "extra"). Excluded paths and `System Volume Information` /
  `$RECYCLE.BIN` are never touched.
- *Ctrl-C* finishes cleanly: in-progress temp files are removed; press twice to force.

## Kanguru tuning tips

- Format the drive **exFAT** (or NTFS) if you have files ≥ 4 GiB; use `--fat32` if
  it must stay FAT32 so oversized files are rejected up front.
- If the drive is FAT/FAT32 and timestamps shift by an hour after DST changes, add
  `--dst-compensate` to avoid re-copying everything.
- Leave fsync on (default). The drive can auto-lock on idle timeout; flushed files
  are safe, cached ones are not.
- For trees of mostly tiny files over a slow WAN, try `-t 16`. For a few huge files
  on a fast LAN, try `--buffer-mb 8`. `--prealloc` can help on exFAT/NTFS.

## Code layout

| file | role |
|------|------|
| `cli.rs`     | arguments |
| `scan.rs`    | single source walk, change detection, job list |
| `copy.rs`    | per-file engine: pipeline, temp+rename, fsync, verify, retry |
| `error.rs`   | Windows/Unix error code classification (retry / skip / abort) |
| `extras.rs`  | extra-file detection and safe purge |
| `engine.rs`  | orchestration, worker pool, large-file slots, preflight |
| `report.rs`  | summary table and exit codes |

## Known limits / TODO

- Copies data, timestamps and the read-only attribute. It does not copy NTFS ACLs,
  owners, ADS or hidden/system attributes (robocopy `/COPYALL`, `/SEC`). Kanguru
  drives are usually exFAT/FAT32, which can't store ACLs anyway.
- Symlinks and junctions are skipped (like robocopy `/XJ`).
- No resume within a single huge file after an interruption; the file restarts.
- TODO: restartable mode (`/Z`) for multi-GB files over unreliable WAN links.
