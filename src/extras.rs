//! Finds (and with --purge/--mirror deletes) destination entries that no longer
//! exist in the source. Deliberately conservative: anything uncertain is kept.

use crate::copy::clear_readonly;
use crate::ctx::{add, get, Ctx};
use crate::util::is_temp_name;
use std::fs;
use std::io;
use std::path::Path;
use walkdir::WalkDir;

pub fn process(ctx: &Ctx) {
    let a = &ctx.args;
    let s = &ctx.stats;
    let dst_root = &ctx.dst_root;
    if !dst_root.is_dir() {
        return;
    }

    let mut delete = a.purge_enabled() && !a.dry_run;
    if delete && (get(&s.files_failed) > 0 || get(&s.scan_errors) > 0 || ctx.stopped()) {
        ctx.log.warn(
            "purge skipped because this run had errors; extra files are only reported. \
             Re-run once the errors are fixed.",
        );
        delete = false;
    }
    // If the share dropped, every file would look "extra". Never purge blind.
    if delete && !fs::metadata(&ctx.src_root).map(|m| m.is_dir()).unwrap_or(false) {
        ctx.log.warn("purge skipped: source is no longer reachable");
        delete = false;
    }

    let mut walker = WalkDir::new(dst_root).contents_first(true).follow_links(false);
    if a.no_recurse {
        walker = walker.max_depth(1);
    }
    let rel_of = |p: &Path| p.strip_prefix(dst_root).unwrap_or(p).to_path_buf();
    let iter = walker.into_iter().filter_entry(|e| {
        e.depth() == 0 || !e.file_type().is_dir() || !ctx.filters.dir_excluded(&rel_of(e.path()))
    });

    for entry in iter {
        if ctx.stopped() {
            break;
        }
        let entry = match entry {
            Ok(e) => e,
            Err(e) => {
                ctx.log.warn(&format!("cannot read destination entry: {e}"));
                continue;
            }
        };
        if entry.depth() == 0 {
            continue;
        }
        let path = entry.path();
        let rel = rel_of(path);
        let is_dir = entry.file_type().is_dir();

        if !is_dir && is_temp_name(entry.file_name()) {
            // Leftover from an interrupted run.
            if !a.dry_run && fs::remove_file(path).is_ok() {
                ctx.log.info(&format!("removed stale temp file {}", rel.display()));
            }
            continue;
        }
        if (!is_dir && ctx.filters.file_excluded(&rel))
            || (a.no_recurse && is_dir)
            || a.log.as_deref() == Some(path)
        {
            continue;
        }

        let extra = match fs::symlink_metadata(ctx.src_root.join(&rel)) {
            Ok(m) => m.is_dir() != is_dir,
            Err(e) if e.kind() == io::ErrorKind::NotFound => true,
            Err(e) => {
                ctx.log.warn(&format!("cannot check source for {}: {e}; keeping it", rel.display()));
                false
            }
        };
        if !extra {
            continue;
        }

        add(if is_dir { &s.extras_dirs } else { &s.extras_files }, 1);
        let kind = if is_dir { "dir " } else { "file" };
        if !delete {
            let msg = format!("*EXTRA {kind} {}", rel.display());
            if a.dry_run && a.purge_enabled() {
                ctx.log.console(&format!("  would delete {kind} {}", rel.display()));
            }
            ctx.log.info(&msg);
            continue;
        }
        let res = if is_dir { fs::remove_dir(path) } else { remove_file_force(path) };
        match res {
            Ok(()) => {
                add(&s.extras_deleted, 1);
                ctx.log.info(&format!("*EXTRA {kind} deleted {}", rel.display()));
            }
            Err(e) => ctx.log.warn(&format!("could not delete extra {}: {e}", rel.display())),
        }
    }
}

fn remove_file_force(path: &Path) -> io::Result<()> {
    match fs::remove_file(path) {
        Err(e) if e.kind() == io::ErrorKind::PermissionDenied && clear_readonly(path) => fs::remove_file(path),
        other => other,
    }
}
