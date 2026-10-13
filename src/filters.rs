use crate::cli::Args;
use anyhow::{Context, Result};
use globset::{GlobBuilder, GlobSet, GlobSetBuilder};
use std::path::Path;

/// System folders that exist at a drive root and must never be copied or purged.
const PROTECTED_DIRS: &[&str] = &["System Volume Information", "$RECYCLE.BIN"];

pub struct Filters {
    xf: GlobSet,
    xd: GlobSet,
    include: Option<GlobSet>,
}

impl Filters {
    pub fn new(args: &Args) -> Result<Self> {
        Ok(Self {
            xf: build(&args.exclude_files)?,
            xd: build(&args.exclude_dirs)?,
            include: if args.include.is_empty() { None } else { Some(build(&args.include)?) },
        })
    }

    /// `rel` is relative to the source/dest root. Excluded files are neither copied
    /// nor purged from the destination, matching robocopy semantics.
    pub fn file_excluded(&self, rel: &Path) -> bool {
        if hit(&self.xf, rel) {
            return true;
        }
        match &self.include {
            Some(inc) => !hit(inc, rel),
            None => false,
        }
    }

    pub fn dir_excluded(&self, rel: &Path) -> bool {
        if let Some(name) = rel.file_name() {
            if PROTECTED_DIRS.iter().any(|p| name.eq_ignore_ascii_case(p)) {
                return true;
            }
        }
        hit(&self.xd, rel)
    }
}

fn hit(set: &GlobSet, rel: &Path) -> bool {
    !set.is_empty() && (set.is_match(rel) || rel.file_name().is_some_and(|n| set.is_match(n)))
}

fn build(patterns: &[String]) -> Result<GlobSet> {
    let mut b = GlobSetBuilder::new();
    for p in patterns {
        let glob = GlobBuilder::new(p)
            .case_insensitive(cfg!(windows))
            .literal_separator(false)
            .build()
            .with_context(|| format!("invalid glob pattern: {p}"))?;
        b.add(glob);
    }
    Ok(b.build()?)
}
