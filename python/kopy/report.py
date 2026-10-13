from __future__ import annotations

from .ctx import Ctx
from .util import human


def summary(ctx: Ctx, elapsed: float) -> int:
    """Print the summary table and return a robocopy-compatible exit code:
    1 = files copied, 2 = extras found, 4 = mismatches, 8 = failures, 16 = fatal."""
    s = ctx.stats
    secs = max(elapsed, 0.001)
    row = "{:>10} {:>11} {:>11} {:>11} {:>11} {:>11}"
    lines = [
        "-" * 78,
        row.format("", "Total", "Copied", "Skipped", "Failed", "Extras"),
        row.format("Dirs :", s.dirs_total, s.dirs_created, max(0, s.dirs_total - s.dirs_created), "-", s.extras_dirs),
        row.format("Files :", s.files_total, s.files_copied, s.files_skipped, s.files_failed, s.extras_files),
        row.format("Bytes :", human(s.bytes_total), human(s.bytes_copied), human(s.bytes_skipped), "-", "-"),
        f"{'Time :':>10} {secs:.1f}s   throughput {human(s.bytes_copied / secs)}/s   retries {s.retries}   "
        f"vanished {s.files_vanished}   extras deleted {s.extras_deleted}   verify mismatches {s.mismatches}",
    ]
    if s.scan_errors:
        lines.append(f"{'Scan :':>10} {s.scan_errors} source entries could not be read")
    if ctx.fatal.is_set():
        lines.append("    RESULT: ABORTED - destination became unusable (see errors above)")
    elif ctx.stopped():
        lines.append("    RESULT: INTERRUPTED")
    if ctx.args.dry_run:
        lines.append("    (dry run - nothing was changed)")
    lines.append("-" * 78)

    text = "\n".join(lines)
    ctx.log.console(text)
    for line in lines:
        ctx.log.file_only("INFO", line)

    code = 0
    if s.files_copied:
        code |= 1
    if s.extras_files + s.extras_dirs:
        code |= 2
    if s.mismatches and s.files_failed:
        code |= 4
    if s.files_failed or s.scan_errors or (ctx.stopped() and not ctx.fatal.is_set()):
        code |= 8
    if ctx.fatal.is_set():
        code |= 16
    return code
