//! The per-file copy engine.
//!
//! Each file is written to a hidden temp sibling, flushed to the device, optionally
//! verified, then atomically renamed over the target. Large files are pipelined:
//! a reader thread pulls chunks from the network share (hashing them as it goes)
//! while the calling thread writes the previous chunk to the USB drive, so network
//! latency and flash write time overlap instead of adding up.

use crate::ctx::{add, Ctx};
use crate::error::{classify, CopyError, Side};
use crate::scan::Job;
use crate::util::{temp_path, FAT32_MAX};
use crossbeam_channel::bounded;
use filetime::FileTime;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::time::Duration;

/// Chunks in flight per pipelined file: one being read, one queued, one being written.
const PIPELINE_DEPTH: usize = 3;

type CopyResult = Result<(u64, Option<blake3::Hash>), CopyError>;

pub fn copy_with_retry(ctx: &Ctx, job: &Job) -> Result<u64, CopyError> {
    let mut attempt = 0u32;
    loop {
        let mut written = 0u64;
        match attempt_copy(ctx, job, &mut written) {
            Ok(n) => return Ok(n),
            Err(e) => {
                // Undo this attempt's progress so the bar stays honest.
                ctx.pb.set_position(ctx.pb.position().saturating_sub(written));
                if e.is_retryable() && attempt < ctx.args.retries && !ctx.stopped() {
                    attempt += 1;
                    add(&ctx.stats.retries, 1);
                    let wait = backoff(ctx.args.wait, attempt);
                    ctx.log.warn(&format!(
                        "retry {attempt}/{} in {}s: {e}",
                        ctx.args.retries,
                        wait.as_secs()
                    ));
                    if !sleep_unless_cancelled(wait, &ctx.cancel) {
                        return Err(CopyError::Cancelled);
                    }
                    continue;
                }
                return Err(e);
            }
        }
    }
}

fn backoff(base_secs: u64, attempt: u32) -> Duration {
    let factor = 1u64 << (attempt.saturating_sub(1)).min(6);
    Duration::from_secs(base_secs.saturating_mul(factor).min(60))
}

/// Returns false if cancelled while waiting.
fn sleep_unless_cancelled(total: Duration, cancel: &AtomicBool) -> bool {
    let step = Duration::from_millis(100);
    let mut waited = Duration::ZERO;
    while waited < total {
        if cancel.load(Relaxed) {
            return false;
        }
        std::thread::sleep(step);
        waited += step;
    }
    !cancel.load(Relaxed)
}

fn attempt_copy(ctx: &Ctx, job: &Job, written: &mut u64) -> Result<u64, CopyError> {
    if ctx.stopped() {
        return Err(CopyError::Cancelled);
    }
    if let Some(parent) = job.dst.parent() {
        fs::create_dir_all(parent).map_err(|e| classify("create directory", parent, Side::Dest, e))?;
    }
    let tmp = temp_path(&job.dst);
    let res = write_and_commit(ctx, job, &tmp, written);
    if res.is_err() {
        // Never leave half-written data behind; ignore failure (drive may be gone).
        let _ = fs::remove_file(&tmp);
    }
    res
}

fn write_and_commit(ctx: &Ctx, job: &Job, tmp: &Path, written: &mut u64) -> Result<u64, CopyError> {
    let a = &ctx.args;
    let src_err = |op| move |e| classify(op, &job.src, Side::Source, e);
    let dst_err = |op| move |e| classify(op, &job.dst, Side::Dest, e);

    let mut src = File::open(&job.src).map_err(src_err("open"))?;
    let src_meta = src.metadata().map_err(src_err("stat"))?;
    let size = src_meta.len();
    let src_mtime = FileTime::from_last_modification_time(&src_meta);

    let mut out = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(tmp)
        .map_err(dst_err("create"))?;
    if a.prealloc && size > 0 {
        out.set_len(size).map_err(|e| write_error(e, job))?;
    }

    let buf_size = a.buffer_mb * 1024 * 1024;
    let (n, src_hash) = if size <= buf_size as u64 {
        copy_small(ctx, job, &mut src, &mut out, written)?
    } else {
        copy_pipelined(ctx, job, src, &mut out, buf_size, written)?
    };

    // Someone wrote to the file on the server while we were reading it.
    let after = fs::metadata(&job.src).map_err(src_err("stat"))?;
    if n != size || after.len() != size || FileTime::from_last_modification_time(&after) != src_mtime {
        return Err(CopyError::SourceChanged(job.src.clone()));
    }

    if a.prealloc && n != size {
        out.set_len(n).map_err(dst_err("truncate"))?;
    }
    filetime::set_file_handle_times(&out, None, Some(src_mtime)).map_err(dst_err("set timestamp"))?;
    if !a.no_fsync {
        // Make sure the bytes are on the flash, not in the OS cache, before we
        // call the file done: secure drives auto-lock and get yanked.
        out.sync_all().map_err(dst_err("flush to device"))?;
        drop_page_cache(&out);
    }
    drop(out);

    if let Some(expected) = src_hash {
        let actual = hash_file_uncached(tmp, buf_size).map_err(dst_err("verify read"))?;
        if actual != expected {
            add(&ctx.stats.mismatches, 1);
            return Err(CopyError::VerifyMismatch(job.dst.clone()));
        }
    }

    replace(tmp, &job.dst)?;
    // Carry the read-only attribute (Windows) / mode bits (Unix); best effort.
    let _ = fs::set_permissions(&job.dst, src_meta.permissions());
    Ok(n)
}

/// A write that fails past the 4 GiB mark on a "full" drive is really FAT32's file
/// size limit; reporting it as disk-full would wrongly abort the whole run.
fn write_error(e: io::Error, job: &Job) -> CopyError {
    match classify("write", &job.dst, Side::Dest, e) {
        CopyError::DiskFull(path) if job.size > FAT32_MAX => CopyError::TooLarge { path, size: job.size },
        other => other,
    }
}

fn copy_small(ctx: &Ctx, job: &Job, src: &mut File, out: &mut File, written: &mut u64) -> CopyResult {
    let mut data = Vec::with_capacity(job.size as usize);
    src.read_to_end(&mut data)
        .map_err(|e| classify("read", &job.src, Side::Source, e))?;
    if ctx.stopped() {
        return Err(CopyError::Cancelled);
    }
    out.write_all(&data).map_err(|e| write_error(e, job))?;
    let n = data.len() as u64;
    *written += n;
    ctx.pb.inc(n);
    Ok((n, ctx.args.verify.then(|| blake3::hash(&data))))
}

fn copy_pipelined(
    ctx: &Ctx,
    job: &Job,
    src: File,
    out: &mut File,
    buf_size: usize,
    written: &mut u64,
) -> CopyResult {
    let (free_tx, free_rx) = bounded::<Vec<u8>>(PIPELINE_DEPTH);
    let (full_tx, full_rx) = bounded::<Result<(Vec<u8>, usize), CopyError>>(PIPELINE_DEPTH);
    for _ in 0..PIPELINE_DEPTH {
        free_tx.send(vec![0u8; buf_size]).expect("channel has capacity");
    }
    let cancel: &AtomicBool = &ctx.cancel;
    let want_hash = ctx.args.verify;
    let src_path = &job.src;

    std::thread::scope(|s| {
        // Reader: network -> buffers (+ hash). Returns Some(hash) only on clean EOF.
        let reader = s.spawn(move || -> Option<Option<blake3::Hash>> {
            let mut src = src;
            let mut hasher = want_hash.then(blake3::Hasher::new);
            while let Ok(mut buf) = free_rx.recv() {
                if cancel.load(Relaxed) {
                    let _ = full_tx.send(Err(CopyError::Cancelled));
                    return None;
                }
                match read_full(&mut src, &mut buf) {
                    Ok(0) => return Some(hasher.map(|h| h.finalize())),
                    Ok(n) => {
                        if let Some(h) = hasher.as_mut() {
                            h.update(&buf[..n]);
                        }
                        if full_tx.send(Ok((buf, n))).is_err() {
                            return None; // writer gave up
                        }
                    }
                    Err(e) => {
                        let _ = full_tx.send(Err(classify("read", src_path, Side::Source, e)));
                        return None;
                    }
                }
            }
            None
        });

        // Writer: buffers -> USB drive, on this thread.
        let mut total = 0u64;
        let mut failure = None;
        for item in full_rx.iter() {
            match item {
                Ok((buf, n)) => {
                    if let Err(e) = out.write_all(&buf[..n]) {
                        failure = Some(write_error(e, job));
                        break;
                    }
                    total += n as u64;
                    *written += n as u64;
                    ctx.pb.inc(n as u64);
                    let _ = free_tx.send(buf);
                }
                Err(e) => {
                    failure = Some(e);
                    break;
                }
            }
        }
        // Unblock the reader whichever way it is waiting.
        drop(full_rx);
        drop(free_tx);

        let outcome = reader.join().map_err(|_| CopyError::Io {
            op: "read",
            path: job.src.clone(),
            retryable: true,
            source: io::Error::other("reader thread panicked"),
        })?;
        if let Some(e) = failure {
            return Err(e);
        }
        match outcome {
            Some(hash) => Ok((total, hash)),
            None => Err(CopyError::Cancelled),
        }
    })
}

/// Fill `buf` completely unless EOF; network reads often return short.
fn read_full(r: &mut impl Read, buf: &mut [u8]) -> io::Result<usize> {
    let mut filled = 0;
    while filled < buf.len() {
        match r.read(&mut buf[filled..]) {
            Ok(0) => break,
            Ok(n) => filled += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }
    Ok(filled)
}

/// Atomic replace. Windows refuses to rename over a read-only file, so clear the
/// attribute and try once more.
fn replace(tmp: &Path, dst: &Path) -> Result<(), CopyError> {
    match fs::rename(tmp, dst) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == io::ErrorKind::PermissionDenied && clear_readonly(dst) => {
            fs::rename(tmp, dst).map_err(|e| classify("replace", dst, Side::Dest, e))
        }
        Err(e) => Err(classify("replace", dst, Side::Dest, e)),
    }
}

/// Returns true if the file was read-only and the attribute was cleared.
pub fn clear_readonly(path: &Path) -> bool {
    match fs::metadata(path) {
        Ok(m) if m.permissions().readonly() => {
            let mut p = m.permissions();
            #[allow(clippy::permissions_set_readonly_false)]
            p.set_readonly(false);
            fs::set_permissions(path, p).is_ok()
        }
        _ => false,
    }
}

// ------------------------------------------------------------------ verification I/O
//
// Verification must read what is on the flash, not what is still in RAM. After
// fsync we evict the file from the page cache (Linux) or read with caching
// disabled (Windows).

#[cfg(target_os = "linux")]
fn drop_page_cache(f: &File) {
    use std::os::unix::io::AsRawFd;
    // SAFETY: valid open fd; advisory call with no memory effects.
    unsafe {
        libc::posix_fadvise(f.as_raw_fd(), 0, 0, libc::POSIX_FADV_DONTNEED);
    }
}

#[cfg(not(target_os = "linux"))]
fn drop_page_cache(_f: &File) {}

fn hash_reader(mut f: File, buf_size: usize) -> io::Result<blake3::Hash> {
    let mut hasher = blake3::Hasher::new();
    let mut buf = vec![0u8; buf_size];
    loop {
        let n = read_full(&mut f, &mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hasher.finalize())
}

#[cfg(not(windows))]
fn hash_file_uncached(path: &Path, buf_size: usize) -> io::Result<blake3::Hash> {
    hash_reader(File::open(path)?, buf_size)
}

#[cfg(windows)]
fn hash_file_uncached(path: &Path, buf_size: usize) -> io::Result<blake3::Hash> {
    use std::os::windows::fs::OpenOptionsExt;
    const FILE_FLAG_NO_BUFFERING: u32 = 0x2000_0000;
    const FILE_FLAG_SEQUENTIAL_SCAN: u32 = 0x0800_0000;
    const ALIGN: usize = 4096; // covers 512-byte and 4K-sector devices

    let uncached = OpenOptions::new()
        .read(true)
        .custom_flags(FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN)
        .open(path);
    let Ok(mut f) = uncached else {
        return hash_reader(File::open(path)?, buf_size);
    };

    // NO_BUFFERING needs a sector-aligned buffer and sector-multiple read lengths.
    let chunk = buf_size.div_ceil(ALIGN) * ALIGN;
    let mut raw = vec![0u8; chunk + ALIGN];
    let offset = raw.as_ptr().align_offset(ALIGN);
    let buf = &mut raw[offset..offset + chunk];

    let mut hasher = blake3::Hasher::new();
    loop {
        let n = match f.read(buf) {
            Ok(n) => n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            // Some filesystems/drivers reject unbuffered I/O; fall back to cached.
            Err(e) if e.raw_os_error() == Some(87) => return hash_reader(File::open(path)?, buf_size),
            Err(e) => return Err(e),
        };
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        if n < chunk {
            break; // short read only happens at EOF with unbuffered I/O
        }
    }
    Ok(hasher.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_doubles_and_caps() {
        assert_eq!(backoff(2, 1), Duration::from_secs(2));
        assert_eq!(backoff(2, 2), Duration::from_secs(4));
        assert_eq!(backoff(2, 3), Duration::from_secs(8));
        assert_eq!(backoff(2, 20), Duration::from_secs(60));
    }

    #[test]
    fn read_full_handles_short_reads() {
        struct Trickle(Vec<u8>);
        impl Read for Trickle {
            fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
                if self.0.is_empty() || buf.is_empty() {
                    return Ok(0);
                }
                buf[0] = self.0.remove(0);
                Ok(1)
            }
        }
        let mut r = Trickle(vec![1, 2, 3, 4, 5]);
        let mut buf = [0u8; 4];
        assert_eq!(read_full(&mut r, &mut buf).unwrap(), 4);
        assert_eq!(buf, [1, 2, 3, 4]);
        assert_eq!(read_full(&mut r, &mut buf).unwrap(), 1);
    }
}
