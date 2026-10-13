# kopy (Python)

Python port of [kopy](../README.md), a robocopy-style copier
tuned for copying from a Windows file server (SMB share) to a USB secure drive such
as a Kanguru Defender. Same flags, same safety behaviour, same exit codes as the
Rust version.

**Standard library only.** Runs on Python 3.10+ with nothing to install, which
suits locked-down servers without pip or internet access.

## Run

```powershell
# no install needed: from the folder containing the "kopy" package
python -m kopy \\fileserver\projects E:\Backup\projects
# or equivalently
python kopy_cli.py \\fileserver\projects E:\Backup\projects

# or install it as a "kopy" command
pip install .
kopy \\fileserver\projects E:\Backup\projects --mirror --verify --log C:\logs\kopy.log
```

Optional: `pip install blake3` makes `--verify` about 3x faster on large files.
It is picked up automatically; without it the built-in BLAKE2 hash is used.

Single .exe for machines without Python:

```powershell
pip install pyinstaller
build_exe.bat
# -> dist\kopy.exe, with the kopy icon and version details
```

`build_exe.bat` runs this, from the folder containing `kopy_cli.py`:

```powershell
pyinstaller --noconfirm --clean --onefile --console --name kopy `
    --icon assets\kopy.ico --version-file version_info.txt kopy_cli.py
```

- `assets\kopy.ico` shows documents moving into a locked USB drive. Sizes 16-32 px
  use a simplified drawing so it stays readable in lists and the taskbar. To change
  it, edit `tools\make_icon.py` and run `pip install pillow` then
  `python tools\make_icon.py`. Or pass `--icon` any `.ico` file of your own.
- `version_info.txt` sets the description shown in Explorer > Properties > Details
  and in Task Manager. Update its version numbers when you release.
- If Explorer still shows the old icon after a rebuild, that's Windows' icon
  cache: rename the exe or copy it to another folder to see the new one.

Tests: `pip install pytest` then `python -m pytest`.

## Usage

```powershell
kopy SRC DST                      # incremental copy (robocopy /E)
kopy SRC DST --mirror             # also delete extras (robocopy /MIR)
kopy SRC DST --mirror -n          # preview only (robocopy /L)
kopy SRC DST --xd .git --xf "*.tmp" --xf "~$*"
kopy SRC DST -t 16 --verify --log kopy.log
```

Exit codes use robocopy's bits: `1` copied, `2` extras found, `4` verify mismatch,
`8` failures, `16` fatal. Below 8 means success.

| robocopy        | kopy                         |
|-----------------|------------------------------|
| `/E`            | default (`--skip-empty-dirs` for `/S`) |
| `/MIR` `/PURGE` | `--mirror` `--purge`         |
| `/MT:n`         | `-t n` (default 8)           |
| `/R:n` `/W:n`   | `-r n` `-w n` (5, 2s; backoff doubles, max 60s) |
| `/XF` `/XD`     | `--xf GLOB` `--xd GLOB` (repeatable) |
| `/L`            | `-n` / `--dry-run`           |
| `/LOG+:file`    | `--log file`                 |
| `/FFT` `/DST`   | `--time-tolerance 2` (default), `--dst-compensate` |
| `/IS /IT`       | `--force`                    |
| `/V` `/NP`      | `-v` `-q`                    |

Run `kopy -h` for all options.

## How it works

- **Parallel files:** worker threads copy several files at once to hide per-file
  network round trips. File I/O and hashing release Python's GIL, so the threads
  really do run in parallel.
- **Read and write overlap:** for large files, a reader thread pulls the next chunk
  from the share while the worker writes the previous one to the drive.
- **Large-file slots:** only one big file is written at a time (`--large-slots`),
  because USB flash is fastest with one steady stream.
- **Atomic writes:** each file goes to a hidden `.<name>.kopy-tmp`, is flushed to the
  device, optionally verified, then renamed into place. A pulled or auto-locked
  drive never leaves a truncated file under the real name.
- **Resume:** just rerun the same command. Finished files match on size and
  timestamp and are skipped; leftover temp files are cleaned up.
- **Fast rescans:** each folder is compared against one listing of the matching
  destination folder (and, for `--mirror`, one listing of the source folder),
  instead of looking up files one at a time. On Windows a folder listing already
  includes sizes and dates, so this saves one round trip per file.
- **Error handling:**
  - transient problems (dropped SMB session, file open on the server, timeouts)
    are retried with backoff
  - a source file deleted mid-run is skipped with a warning
  - a source file modified mid-copy is retried
  - **drive full, write-protect switch on, drive removed or locked → the whole run
    stops at once** with a specific message
  - a file over 4 GiB on FAT32 is reported as a format limit, not "disk full"
- **Free-space check** before anything is copied.
- **Safe mirror:** nothing is deleted if the run had any errors or the source has
  become unreachable. Excluded paths, `System Volume Information` and
  `$RECYCLE.BIN` are never touched.
- **Long paths:** paths over 260 characters work without registry changes.
- **Ctrl-C** cleans up the in-progress temp file; press twice to force quit.

## Python vs Rust version

Measured on the same Linux machine and data (these numbers will differ on your
server and drive, where the network and USB speed usually dominate):

| test                           | Rust  | Python |
|--------------------------------|-------|--------|
| 50,000 small files             | 9.1s  | 14.7s  |
| rescan of 50,000 unchanged     | 0.37s | 0.74s  |
| 1 GiB file                     | 1.4s  | 1.7s   |
| 1 GiB file with `--verify`     | 2.0s  | 5.7s (without the optional blake3) |

Large files are close because both are limited by I/O. Many small files cost
Python more per file. Pick Python if you want to read and change the code
easily or can't ship a compiled binary; pick Rust for the fastest runs.

## Layout

| file | role |
|------|------|
| `cli.py`      | arguments |
| `scan.py`     | source walk, per-folder destination comparison, job list |
| `copier.py`   | per-file engine: pipeline, temp+rename, fsync, verify, retry |
| `winio.py`    | Windows uncached read for `--verify` |
| `errors.py`   | Windows/Unix error classification (retry / skip / abort) |
| `extras.py`   | extra-file detection and safe purge |
| `engine.py`   | orchestration, worker threads, large-file slots, Ctrl-C |
| `progress.py` | console progress line |
| `report.py`   | summary table and exit codes |

## Known limits / TODO

- Copies data, timestamps and the read-only attribute; not NTFS ACLs, owners,
  alternate data streams or hidden/system attributes (robocopy `/COPYALL`).
- Directory symlinks and junctions are skipped (like robocopy `/XJ`). File reparse
  points, such as Windows Server deduplicated files, are copied normally.
- A single large file restarts from zero after an interruption.
  TODO: restartable mode (`/Z`).
- `winio.py` (uncached verify read) is Windows-only and untested on Windows so
  far; any failure there falls back to a normal read.
