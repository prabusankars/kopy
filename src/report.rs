use crate::ctx::{get, Ctx};
use crate::util::human;
use std::time::Duration;

/// Robocopy-compatible exit code bits, so existing batch scripts keep working:
/// 1 = files copied, 2 = extra files/dirs found, 4 = mismatches,
/// 8 = failures, 16 = fatal (nothing more could be copied).
pub fn summary(ctx: &Ctx, elapsed: Duration) -> u8 {
    let s = &ctx.stats;
    let (ft, fc, fs, ff, fv) = (
        get(&s.files_total),
        get(&s.files_copied),
        get(&s.files_skipped),
        get(&s.files_failed),
        get(&s.files_vanished),
    );
    let (dt, dc) = (get(&s.dirs_total), get(&s.dirs_created));
    let (bt, bc, bs) = (get(&s.bytes_total), get(&s.bytes_copied), get(&s.bytes_skipped));
    let (xf, xd, xdel) = (get(&s.extras_files), get(&s.extras_dirs), get(&s.extras_deleted));
    let secs = elapsed.as_secs_f64().max(0.001);

    let mut out = String::new();
    out.push_str(&"-".repeat(78));
    out.push_str(&format!(
        "\n{:>10} {:>11} {:>11} {:>11} {:>11} {:>11}\n",
        "", "Total", "Copied", "Skipped", "Failed", "Extras"
    ));
    out.push_str(&format!(
        "{:>10} {:>11} {:>11} {:>11} {:>11} {:>11}\n",
        "Dirs :", dt, dc, dt.saturating_sub(dc), "-", xd
    ));
    out.push_str(&format!(
        "{:>10} {:>11} {:>11} {:>11} {:>11} {:>11}\n",
        "Files :", ft, fc, fs, ff, xf
    ));
    out.push_str(&format!(
        "{:>10} {:>11} {:>11} {:>11} {:>11} {:>11}\n",
        "Bytes :",
        human(bt),
        human(bc),
        human(bs),
        "-",
        "-"
    ));
    out.push_str(&format!(
        "{:>10} {:.1}s   throughput {}/s   retries {}   vanished {}   extras deleted {}   verify mismatches {}\n",
        "Time :",
        secs,
        human((bc as f64 / secs) as u64),
        get(&s.retries),
        fv,
        xdel,
        get(&s.mismatches),
    ));
    if get(&s.scan_errors) > 0 {
        out.push_str(&format!("{:>10} {} source entries could not be read\n", "Scan :", get(&s.scan_errors)));
    }
    if ctx.is_fatal() {
        out.push_str("    RESULT: ABORTED - destination became unusable (see errors above)\n");
    } else if ctx.stopped() {
        out.push_str("    RESULT: INTERRUPTED\n");
    }
    if ctx.args.dry_run {
        out.push_str("    (dry run - nothing was changed)\n");
    }
    out.push_str(&"-".repeat(78));

    ctx.log.console(&out);
    for line in out.lines() {
        ctx.log.file_only("INFO", line);
    }

    let mut code = 0u8;
    if fc > 0 {
        code |= 1;
    }
    if xf + xd > 0 {
        code |= 2;
    }
    if get(&s.mismatches) > 0 && ff > 0 {
        code |= 4;
    }
    if ff > 0 || get(&s.scan_errors) > 0 || (ctx.stopped() && !ctx.is_fatal()) {
        code |= 8;
    }
    if ctx.is_fatal() {
        code |= 16;
    }
    code
}
