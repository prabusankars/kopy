use crate::cli::Args;
use crate::copy::copy_with_retry;
use crate::ctx::{add, get, Ctx, Logger, Stats};
use crate::error::{classify, CopyError, Side};
use crate::filters::Filters;
use crate::scan::{scan, Job};
use crate::util::human;
use crate::{extras, report};
use anyhow::{bail, ensure, Context, Result};
use crossbeam_channel::{bounded, unbounded};
use indicatif::{ProgressBar, ProgressStyle};
use std::fs;
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::Arc;
use std::time::Instant;

pub fn run(args: Args) -> Result<u8> {
    let started = Instant::now();
    ensure!(args.threads > 0, "--threads must be at least 1");
    ensure!(args.large_slots > 0, "--large-slots must be at least 1");
    ensure!(args.buffer_mb > 0 && args.buffer_mb <= 256, "--buffer-mb must be 1..=256");

    let src_meta = fs::metadata(&args.source)
        .with_context(|| format!("cannot access source {}", args.source.display()))?;
    ensure!(src_meta.is_dir(), "source is not a directory: {}", args.source.display());
    if !args.dry_run {
        fs::create_dir_all(&args.dest)
            .with_context(|| format!("cannot create destination {}", args.dest.display()))?;
    }

    let pb = if args.quiet { ProgressBar::hidden() } else { ProgressBar::new(0) };
    pb.set_style(
        ProgressStyle::with_template(
            "{spinner} {elapsed_precise} [{bar:30}] {bytes}/{total_bytes} {binary_bytes_per_sec} eta {eta} {msg}",
        )?
        .progress_chars("=> "),
    );

    let cancel = Arc::new(AtomicBool::new(false));
    {
        let c = cancel.clone();
        // Best effort: a second handler install (e.g. in tests) is not an error.
        let _ = ctrlc::set_handler(move || {
            if c.swap(true, Relaxed) {
                std::process::exit(16);
            }
            eprintln!("\nkopy: interrupted - cleaning up (Ctrl-C again to force quit)");
        });
    }

    let ctx = Ctx {
        filters: Filters::new(&args)?,
        log: Logger::new(args.log.as_deref(), pb.clone(), args.verbose)?,
        src_root: args.source.clone(),
        dst_root: args.dest.clone(),
        stats: Stats::default(),
        pb: pb.clone(),
        cancel,
        fatal: AtomicBool::new(false),
        args,
    };
    ctx.log.file_only("INFO", &format!("kopy {} start", env!("CARGO_PKG_VERSION")));
    ctx.log.file_only("INFO", &format!("source: {}", ctx.src_root.display()));
    ctx.log.file_only("INFO", &format!("dest:   {}", ctx.dst_root.display()));
    ctx.log.file_only("INFO", &format!("options: {:?}", ctx.args));

    pb.enable_steady_tick(std::time::Duration::from_millis(200));
    pb.set_message("scanning...");
    let plan = scan(&ctx);
    ctx.log.info(&format!(
        "scan: {} files, {} to copy ({})",
        get(&ctx.stats.files_total),
        plan.jobs.len(),
        human(plan.bytes_to_copy)
    ));

    if !ctx.args.dry_run && !ctx.stopped() {
        if !ctx.args.skip_space_check {
            preflight_space(&ctx, plan.bytes_needed)?;
        }
        create_dirs(&ctx, &plan.dirs);
    }

    if ctx.args.dry_run {
        for job in &plan.jobs {
            ctx.log.console(&format!(
                "  would copy [{}] {} ({})",
                job.reason.label(),
                job.rel.display(),
                human(job.size)
            ));
        }
    } else if !ctx.stopped() {
        pb.set_length(plan.bytes_to_copy);
        pb.set_message(format!("0/{} files", plan.jobs.len()));
        run_jobs(&ctx, plan.jobs);
    }

    if !ctx.stopped() {
        pb.set_message("checking extras");
        extras::process(&ctx);
    }
    pb.finish_and_clear();

    let code = report::summary(&ctx, started.elapsed());
    ctx.log.flush();
    Ok(code)
}

fn preflight_space(ctx: &Ctx, needed: u64) -> Result<()> {
    match fs4::available_space(&ctx.dst_root) {
        Ok(avail) if avail < needed => {
            let msg = format!(
                "not enough space on destination: need {} more, {} free",
                human(needed),
                human(avail)
            );
            ctx.log.error(&msg);
            bail!(msg);
        }
        Ok(_) => Ok(()),
        Err(e) => {
            ctx.log.warn(&format!("could not determine free space: {e}"));
            Ok(())
        }
    }
}

fn create_dirs(ctx: &Ctx, dirs: &[std::path::PathBuf]) {
    for rel in dirs {
        if ctx.stopped() {
            return;
        }
        let d = ctx.dst_root.join(rel);
        if d.is_dir() {
            continue;
        }
        match fs::create_dir_all(&d) {
            Ok(()) => add(&ctx.stats.dirs_created, 1),
            Err(e) => {
                let err = classify("create directory", &d, Side::Dest, e);
                ctx.log.error(&format!("{err}"));
                if err.is_fatal() {
                    ctx.abort();
                }
            }
        }
    }
}

/// Fan files out to worker threads. Large files additionally need one of a small
/// number of "slots", so big sequential writes don't interleave on the flash.
fn run_jobs(ctx: &Ctx, jobs: Vec<Job>) {
    let large = ctx.args.large_mb.saturating_mul(1024 * 1024);
    let total = jobs.len();
    let (job_tx, job_rx) = unbounded::<Job>();
    for j in jobs {
        job_tx.send(j).expect("receiver alive");
    }
    drop(job_tx);

    let (slot_tx, slot_rx) = bounded::<()>(ctx.args.large_slots);
    for _ in 0..ctx.args.large_slots {
        slot_tx.send(()).expect("channel has capacity");
    }

    std::thread::scope(|s| {
        for _ in 0..ctx.args.threads {
            let (job_rx, slot_rx, slot_tx) = (job_rx.clone(), slot_rx.clone(), slot_tx.clone());
            s.spawn(move || {
                for job in job_rx.iter() {
                    if ctx.stopped() {
                        break;
                    }
                    let slot = (job.size >= large).then(|| slot_rx.recv().ok()).flatten();
                    process_job(ctx, &job);
                    let st = &ctx.stats;
                    let done = get(&st.files_copied) + get(&st.files_failed) + get(&st.files_vanished);
                    let failed = get(&st.files_failed);
                    ctx.pb.set_message(if failed > 0 {
                        format!("{done}/{total} files ({failed} failed)")
                    } else {
                        format!("{done}/{total} files")
                    });
                    if slot.is_some() {
                        let _ = slot_tx.send(());
                    }
                }
            });
        }
    });
}

fn process_job(ctx: &Ctx, job: &Job) {
    let s = &ctx.stats;
    match copy_with_retry(ctx, job) {
        Ok(n) => {
            add(&s.files_copied, 1);
            add(&s.bytes_copied, n);
            ctx.log.info(&format!("{:<9} {:>10}  {}", job.reason.label(), human(n), job.rel.display()));
        }
        Err(CopyError::Cancelled) => {}
        Err(CopyError::SourceVanished(_)) => {
            add(&s.files_vanished, 1);
            ctx.log.warn(&format!("{} vanished from source; skipped", job.rel.display()));
        }
        Err(e) => {
            add(&s.files_failed, 1);
            ctx.log.error(&format!("FAILED {}: {e}", job.rel.display()));
            if e.is_fatal() {
                ctx.log.error("aborting: destination drive is unusable");
                ctx.abort();
            }
        }
    }
}
