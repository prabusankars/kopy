use crate::cli::Args;
use crate::filters::Filters;
use anyhow::{Context, Result};
use indicatif::ProgressBar;
use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::SystemTime;

#[derive(Default)]
pub struct Stats {
    pub dirs_total: AtomicU64,
    pub dirs_created: AtomicU64,
    pub files_total: AtomicU64,
    pub files_copied: AtomicU64,
    pub files_skipped: AtomicU64,
    pub files_failed: AtomicU64,
    pub files_vanished: AtomicU64,
    pub bytes_total: AtomicU64,
    pub bytes_copied: AtomicU64,
    pub bytes_skipped: AtomicU64,
    pub extras_files: AtomicU64,
    pub extras_dirs: AtomicU64,
    pub extras_deleted: AtomicU64,
    pub mismatches: AtomicU64,
    pub retries: AtomicU64,
    pub scan_errors: AtomicU64,
}

pub fn add(counter: &AtomicU64, n: u64) {
    counter.fetch_add(n, Relaxed);
}

pub fn get(counter: &AtomicU64) -> u64 {
    counter.load(Relaxed)
}

/// Console + optional log file. Console output goes through the progress bar so
/// lines never tear through it.
pub struct Logger {
    file: Mutex<Option<BufWriter<File>>>,
    pb: ProgressBar,
    verbose: bool,
}

impl Logger {
    pub fn new(path: Option<&Path>, pb: ProgressBar, verbose: bool) -> Result<Self> {
        let file = match path {
            Some(p) => Some(BufWriter::new(
                OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(p)
                    .with_context(|| format!("cannot open log file {}", p.display()))?,
            )),
            None => None,
        };
        Ok(Self { file: Mutex::new(file), pb, verbose })
    }

    pub fn file_only(&self, level: &str, msg: &str) {
        if let Ok(mut guard) = self.file.lock() {
            if let Some(f) = guard.as_mut() {
                let ts = humantime::format_rfc3339_seconds(SystemTime::now());
                // A failing log must never fail the copy.
                let _ = writeln!(f, "{ts} {level:<5} {msg}");
            }
        }
    }

    pub fn console(&self, msg: &str) {
        self.pb.suspend(|| eprintln!("{msg}"));
    }

    pub fn info(&self, msg: &str) {
        self.file_only("INFO", msg);
        if self.verbose {
            self.console(msg);
        }
    }

    pub fn warn(&self, msg: &str) {
        self.file_only("WARN", msg);
        self.console(&format!("WARN  {msg}"));
    }

    pub fn error(&self, msg: &str) {
        self.file_only("ERROR", msg);
        self.console(&format!("ERROR {msg}"));
    }

    pub fn flush(&self) {
        if let Ok(mut guard) = self.file.lock() {
            if let Some(f) = guard.as_mut() {
                let _ = f.flush();
            }
        }
    }
}

pub struct Ctx {
    pub args: Args,
    pub src_root: PathBuf,
    pub dst_root: PathBuf,
    pub filters: Filters,
    pub stats: Stats,
    pub log: Logger,
    pub pb: ProgressBar,
    pub cancel: Arc<AtomicBool>,
    pub fatal: AtomicBool,
}

impl Ctx {
    pub fn stopped(&self) -> bool {
        self.cancel.load(Relaxed)
    }

    /// The destination is unusable: stop every worker.
    pub fn abort(&self) {
        self.fatal.store(true, Relaxed);
        self.cancel.store(true, Relaxed);
    }

    pub fn is_fatal(&self) -> bool {
        self.fatal.load(Relaxed)
    }
}
