//! Error model. Every I/O failure is classified once, at the point it happens, into
//! "retry it", "skip this file", or "stop the whole run" (the drive is unusable).

use std::io;
use std::path::{Path, PathBuf};
use thiserror::Error;

/// Which side of the copy an I/O error came from. A full or vanished *destination*
/// is a property of the drive; the same OS error on the source side is not.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Source,
    Dest,
}

#[derive(Debug, Error)]
pub enum CopyError {
    #[error("source file vanished before it could be copied: {}", .0.display())]
    SourceVanished(PathBuf),

    #[error("destination is full while writing {}", .0.display())]
    DiskFull(PathBuf),

    #[error(
        "destination is read-only or write-protected ({}) - check the drive's \
         write-protect switch and that it is unlocked",
        .0.display()
    )]
    WriteProtected(PathBuf),

    #[error(
        "destination device disconnected or not ready ({}) - was the drive \
         removed, or did it auto-lock?",
        .0.display()
    )]
    DeviceGone(PathBuf),

    #[error(
        "file too large for the destination filesystem ({size} bytes): {} - FAT32 \
         cannot hold files of 4 GiB or more; reformat the drive as exFAT or NTFS",
        .path.display()
    )]
    TooLarge { path: PathBuf, size: u64 },

    #[error("verification failed, destination content differs from source: {}", .0.display())]
    VerifyMismatch(PathBuf),

    #[error("source changed while it was being copied: {}", .0.display())]
    SourceChanged(PathBuf),

    #[error("cancelled")]
    Cancelled,

    #[error("{op} failed for {}: {source}", .path.display())]
    Io {
        op: &'static str,
        path: PathBuf,
        retryable: bool,
        #[source]
        source: io::Error,
    },
}

impl CopyError {
    /// Worth trying the same file again after a pause.
    pub fn is_retryable(&self) -> bool {
        match self {
            CopyError::Io { retryable, .. } => *retryable,
            CopyError::DeviceGone(_) | CopyError::VerifyMismatch(_) | CopyError::SourceChanged(_) => true,
            _ => false,
        }
    }

    /// Once retries are exhausted, does this error mean no further file can succeed?
    pub fn is_fatal(&self) -> bool {
        matches!(
            self,
            CopyError::DiskFull(_) | CopyError::WriteProtected(_) | CopyError::DeviceGone(_)
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Class {
    DiskFull,
    WriteProtected,
    DeviceGone,
    Transient,
    Permanent,
}

/// Turn a raw `io::Error` into a `CopyError` that knows whether to retry, skip or abort.
pub fn classify(op: &'static str, path: &Path, side: Side, err: io::Error) -> CopyError {
    let class = err
        .raw_os_error()
        .and_then(os_class)
        .unwrap_or_else(|| kind_class(&err, side));
    let p = path.to_path_buf();

    if side == Side::Source && err.kind() == io::ErrorKind::NotFound && class != Class::Transient {
        return CopyError::SourceVanished(p);
    }

    match (class, side) {
        (Class::DiskFull, Side::Dest) => CopyError::DiskFull(p),
        (Class::WriteProtected, Side::Dest) => CopyError::WriteProtected(p),
        (Class::DeviceGone, Side::Dest) => CopyError::DeviceGone(p),
        // A share that drops or a busy source file is worth waiting for.
        (Class::Transient | Class::DeviceGone, _) => CopyError::Io { op, path: p, retryable: true, source: err },
        _ => CopyError::Io { op, path: p, retryable: false, source: err },
    }
}

fn kind_class(e: &io::Error, side: Side) -> Class {
    use io::ErrorKind as K;
    match e.kind() {
        K::StorageFull => Class::DiskFull,
        K::ReadOnlyFilesystem => Class::WriteProtected,
        // Parent dir removed underneath us; it is recreated on the next attempt.
        K::NotFound if side == Side::Dest => Class::Transient,
        K::Interrupted
        | K::TimedOut
        | K::WouldBlock
        | K::ConnectionReset
        | K::ConnectionAborted
        | K::NotConnected
        | K::BrokenPipe
        | K::UnexpectedEof
        | K::NetworkDown
        | K::NetworkUnreachable
        | K::HostUnreachable
        | K::ResourceBusy
        | K::StaleNetworkFileHandle => Class::Transient,
        _ => Class::Permanent,
    }
}

#[cfg(windows)]
fn os_class(code: i32) -> Option<Class> {
    Some(match code {
        39 | 112 => Class::DiskFull,         // ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
        19 => Class::WriteProtected,          // ERROR_WRITE_PROTECT (Kanguru physical switch)
        21 | 433 | 1167 => Class::DeviceGone, // NOT_READY, NO_SUCH_DEVICE, DEVICE_NOT_CONNECTED
        32    // ERROR_SHARING_VIOLATION (file open on the server)
        | 33  // ERROR_LOCK_VIOLATION
        | 51  // ERROR_REM_NOT_LIST
        | 53  // ERROR_BAD_NETPATH
        | 59  // ERROR_UNEXP_NET_ERR
        | 64  // ERROR_NETNAME_DELETED (SMB session dropped)
        | 67  // ERROR_BAD_NET_NAME
        | 121 // ERROR_SEM_TIMEOUT
        | 1117 // ERROR_IO_DEVICE
        | 1231 // ERROR_NETWORK_UNREACHABLE
        | 1236 // ERROR_CONNECTION_ABORTED
        | 1450 // ERROR_NO_SYSTEM_RESOURCES (large SMB reads under memory pressure)
        => Class::Transient,
        _ => return None,
    })
}

#[cfg(unix)]
fn os_class(code: i32) -> Option<Class> {
    Some(match code {
        libc::ENOSPC | libc::EDQUOT => Class::DiskFull,
        libc::EROFS => Class::WriteProtected,
        libc::ENODEV | libc::ENXIO => Class::DeviceGone,
        libc::EIO
        | libc::ETIMEDOUT
        | libc::ESTALE
        | libc::EAGAIN
        | libc::EINTR
        | libc::EBUSY
        | libc::EHOSTDOWN
        | libc::EHOSTUNREACH
        | libc::ENETDOWN
        | libc::ENETUNREACH
        | libc::ECONNRESET
        | libc::ECONNABORTED
        | libc::ENOTCONN => Class::Transient,
        _ => return None,
    })
}

#[cfg(not(any(unix, windows)))]
fn os_class(_code: i32) -> Option<Class> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dest_disk_full_is_fatal_source_is_not() {
        let e = io::Error::from(io::ErrorKind::StorageFull);
        assert!(classify("write", Path::new("x"), Side::Dest, e).is_fatal());
        let e = io::Error::from(io::ErrorKind::StorageFull);
        assert!(!classify("read", Path::new("x"), Side::Source, e).is_fatal());
    }

    #[test]
    fn missing_source_is_vanished_missing_dest_is_retryable() {
        let e = io::Error::from(io::ErrorKind::NotFound);
        assert!(matches!(
            classify("open", Path::new("x"), Side::Source, e),
            CopyError::SourceVanished(_)
        ));
        let e = io::Error::from(io::ErrorKind::NotFound);
        assert!(classify("create", Path::new("x"), Side::Dest, e).is_retryable());
    }

    #[test]
    fn timeouts_retry_permission_denied_does_not() {
        let e = io::Error::from(io::ErrorKind::TimedOut);
        assert!(classify("read", Path::new("x"), Side::Source, e).is_retryable());
        let e = io::Error::from(io::ErrorKind::PermissionDenied);
        assert!(!classify("read", Path::new("x"), Side::Source, e).is_retryable());
    }
}
