//! Walks the source once and decides, per file, whether it needs copying.

use crate::ctx::{add, Ctx};
use crate::util::{human, FAT32_MAX};
use filetime::FileTime;
use std::fs::{self, Metadata};
use std::io;
use std::path::PathBuf;
use walkdir::WalkDir;

#[derive(Debug, Clone, Copy)]
pub enum Reason {
    New,
    Changed,
    Newer,
    Older,
    Forced,
}

impl Reason {
    pub fn label(self) -> &'static str {
        match self {
            Reason::New => "New File",
            Reason::Changed => "Changed",
            Reason::Newer => "Newer",
            Reason::Older => "Older",
            Reason::Forced => "Forced",
        }
    }
}

#[derive(Debug)]
pub struct Job {
    pub rel: PathBuf,
    pub src: PathBuf,
    pub dst: PathBuf,
    pub size: u64,
    pub reason: Reason,
}

#[derive(Default)]
pub struct Plan {
    pub jobs: Vec<Job>,
    /// Destination directories (relative) to create, parents before children.
    pub dirs: Vec<PathBuf>,
    /// Net extra space the copy will consume on the destination.
    pub bytes_needed: u64,
    pub bytes_to_copy: u64,
}

pub fn scan(ctx: &Ctx) -> Plan {
    let a = &ctx.args;
    let s = &ctx.stats;
    let src_root = &ctx.src_root;
    let dst_root = &ctx.dst_root;
    let mut plan = Plan::default();

    // If the destination lives inside the source, never walk into it (endless recursion).
    let dest_inside: Option<PathBuf> = match (fs::canonicalize(src_root), fs::canonicalize(dst_root)) {
        (Ok(s), Ok(d)) => d.strip_prefix(&s).ok().map(PathBuf::from),
        _ => None,
    };

    let mut walker = WalkDir::new(src_root).follow_links(false);
    if a.no_recurse {
        walker = walker.max_depth(1);
    }
    let iter = walker.into_iter().filter_entry(|e| {
        if e.depth() == 0 || !e.file_type().is_dir() {
            return true;
        }
        let rel = e.path().strip_prefix(src_root).unwrap_or(e.path());
        !ctx.filters.dir_excluded(rel) && dest_inside.as_deref() != Some(rel)
    });

    for entry in iter {
        if ctx.stopped() {
            break;
        }
        let entry = match entry {
            Ok(e) => e,
            Err(e) => {
                add(&s.scan_errors, 1);
                ctx.log.warn(&format!("cannot read source entry: {e}"));
                continue;
            }
        };
        if entry.depth() == 0 {
            continue;
        }
        let rel = entry
            .path()
            .strip_prefix(src_root)
            .expect("walkdir yields paths under its root")
            .to_path_buf();
        let ft = entry.file_type();

        if ft.is_symlink() {
            ctx.log.info(&format!("skipping symlink/junction {}", rel.display()));
            continue;
        }
        if ft.is_dir() {
            add(&s.dirs_total, 1);
            if !a.skip_empty_dirs {
                plan.dirs.push(rel);
            }
            continue;
        }
        if !ft.is_file() || ctx.filters.file_excluded(&rel) {
            continue;
        }

        add(&s.files_total, 1);
        let seen = crate::ctx::get(&s.files_total);
        if seen.is_multiple_of(500) {
            ctx.pb.set_message(format!("scanning... {seen} files, {} to copy", plan.jobs.len()));
        }
        let meta = match entry.metadata() {
            Ok(m) => m,
            Err(e) => {
                add(&s.scan_errors, 1);
                ctx.log.warn(&format!("cannot stat {}: {e}", rel.display()));
                continue;
            }
        };
        let size = meta.len();
        add(&s.bytes_total, size);

        if a.fat32 && size > FAT32_MAX {
            add(&s.files_failed, 1);
            ctx.log.error(&format!(
                "{} is {} - too large for FAT32 (max 4 GiB); skipped",
                rel.display(),
                human(size)
            ));
            continue;
        }

        let dst = dst_root.join(&rel);
        let (reason, dst_size) = match fs::metadata(&dst) {
            Ok(dm) if dm.is_dir() => {
                add(&s.files_failed, 1);
                ctx.log.error(&format!(
                    "{}: destination has a directory where the source has a file",
                    rel.display()
                ));
                continue;
            }
            Ok(dm) => match needs_copy(ctx, &meta, &dm).or(a.force.then_some(Reason::Forced)) {
                Some(r) => (r, dm.len()),
                None => {
                    add(&s.files_skipped, 1);
                    add(&s.bytes_skipped, size);
                    continue;
                }
            },
            Err(e) if e.kind() == io::ErrorKind::NotFound => (Reason::New, 0),
            Err(e) => {
                // Can't tell; copy it and let the copy path report real trouble.
                ctx.log.warn(&format!("cannot stat destination {}: {e}", dst.display()));
                (Reason::New, 0)
            }
        };

        plan.bytes_needed += size.saturating_sub(dst_size);
        plan.bytes_to_copy += size;
        plan.jobs.push(Job { rel, src: entry.into_path(), dst, size, reason });
    }
    plan
}

fn nanos(t: FileTime) -> i128 {
    t.unix_seconds() as i128 * 1_000_000_000 + t.nanoseconds() as i128
}

fn needs_copy(ctx: &Ctx, src: &Metadata, dst: &Metadata) -> Option<Reason> {
    if src.len() != dst.len() {
        return Some(Reason::Changed);
    }
    let diff = nanos(FileTime::from_last_modification_time(src))
        - nanos(FileTime::from_last_modification_time(dst));
    let tol = ctx.args.time_tolerance as i128 * 1_000_000_000;
    if diff.abs() <= tol {
        return None;
    }
    if ctx.args.dst_compensate && (diff.abs() - 3_600_000_000_000).abs() <= tol {
        return None;
    }
    Some(if diff > 0 { Reason::Newer } else { Reason::Older })
}
