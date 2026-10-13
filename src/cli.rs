use clap::Parser;
use std::path::PathBuf;

/// Robust, fast robocopy-style copier tuned for network share -> USB secure drive.
#[derive(Parser, Debug, Clone)]
#[command(name = "kopy", version, about)]
pub struct Args {
    /// Source directory (local path or UNC share, e.g. \\server\share\data)
    pub source: PathBuf,

    /// Destination directory (e.g. E:\Backup on the Kanguru drive)
    pub dest: PathBuf,

    // ---------------------------------------------------------------- what to copy
    /// Mirror: copy changes AND delete dest files/dirs no longer in source (robocopy /MIR)
    #[arg(long)]
    pub mirror: bool,

    /// Delete dest files/dirs that no longer exist in source (robocopy /PURGE)
    #[arg(long)]
    pub purge: bool,

    /// Only copy the top level of source, no subdirectories
    #[arg(long)]
    pub no_recurse: bool,

    /// Don't pre-create directories that contain no copied files (robocopy /S instead of /E)
    #[arg(long)]
    pub skip_empty_dirs: bool,

    /// Exclude files whose name or relative path matches GLOB; repeatable (robocopy /XF)
    #[arg(long = "xf", value_name = "GLOB")]
    pub exclude_files: Vec<String>,

    /// Exclude directories whose name or relative path matches GLOB; repeatable (robocopy /XD)
    #[arg(long = "xd", value_name = "GLOB")]
    pub exclude_dirs: Vec<String>,

    /// Only copy files whose name or relative path matches GLOB; repeatable
    #[arg(long = "include", value_name = "GLOB")]
    pub include: Vec<String>,

    // ---------------------------------------------------------------- change detection
    /// Timestamp tolerance in seconds; 2 absorbs FAT/exFAT granularity (robocopy /FFT)
    #[arg(long, default_value_t = 2)]
    pub time_tolerance: u64,

    /// Treat exact one-hour timestamp shifts as equal; FAT stores local time (robocopy /DST)
    #[arg(long)]
    pub dst_compensate: bool,

    /// Copy every file even if the destination looks up to date (robocopy /IS /IT)
    #[arg(long)]
    pub force: bool,

    /// Destination is FAT32: reject files of 4 GiB or more up front
    #[arg(long)]
    pub fat32: bool,

    // ---------------------------------------------------------------- performance
    /// Concurrent files. Many threads hide network latency for small files (robocopy /MT)
    #[arg(short = 't', long, default_value_t = 8)]
    pub threads: usize,

    /// Max large files written at once. 1 keeps writes sequential, which USB flash prefers
    #[arg(long, default_value_t = 1)]
    pub large_slots: usize,

    /// Files of this many MiB or more count as large
    #[arg(long, default_value_t = 64)]
    pub large_mb: u64,

    /// I/O chunk size in MiB; 3 chunks are in flight per file (read ahead while writing)
    #[arg(long, default_value_t = 4)]
    pub buffer_mb: usize,

    /// Preallocate each file's full size first (fewer fragments on exFAT/NTFS; avoid on FAT32)
    #[arg(long)]
    pub prealloc: bool,

    // ---------------------------------------------------------------- safety
    /// Retries per file on transient errors (robocopy /R)
    #[arg(short = 'r', long, default_value_t = 5)]
    pub retries: u32,

    /// Base seconds between retries; doubles each time, capped at 60 (robocopy /W)
    #[arg(short = 'w', long, default_value_t = 2)]
    pub wait: u64,

    /// Re-read each file from the destination and compare BLAKE3 hashes before committing it
    #[arg(long)]
    pub verify: bool,

    /// Don't check free space on the destination before starting
    #[arg(long)]
    pub skip_space_check: bool,

    /// Skip flushing each file to the device (faster; unsafe if the drive is pulled or auto-locks)
    #[arg(long)]
    pub no_fsync: bool,

    // ---------------------------------------------------------------- output
    /// Show what would happen without changing anything (robocopy /L)
    #[arg(short = 'n', long)]
    pub dry_run: bool,

    /// Append a detailed log to FILE (robocopy /LOG+)
    #[arg(long, value_name = "FILE")]
    pub log: Option<PathBuf>,

    /// Print every copied file to the console
    #[arg(short, long)]
    pub verbose: bool,

    /// Hide the progress bar
    #[arg(short, long)]
    pub quiet: bool,
}

impl Args {
    pub fn purge_enabled(&self) -> bool {
        self.mirror || self.purge
    }
}
