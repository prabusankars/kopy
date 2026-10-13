use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};

/// Largest file FAT32 can store.
pub const FAT32_MAX: u64 = 4 * 1024 * 1024 * 1024 - 1;

const TEMP_SUFFIX: &str = ".kopy-tmp";

/// Hidden sibling the file is written to before an atomic rename, so an interrupted
/// copy never leaves a truncated file under the real name.
pub fn temp_path(dst: &Path) -> PathBuf {
    let mut name = OsString::from(".");
    name.push(dst.file_name().unwrap_or_else(|| OsStr::new("file")));
    name.push(TEMP_SUFFIX);
    dst.with_file_name(name)
}

pub fn is_temp_name(name: &OsStr) -> bool {
    let s = name.to_string_lossy();
    s.starts_with('.') && s.ends_with(TEMP_SUFFIX)
}

pub fn human(bytes: u64) -> String {
    const UNITS: [&str; 5] = ["B", "KiB", "MiB", "GiB", "TiB"];
    let mut v = bytes as f64;
    let mut u = 0;
    while v >= 1024.0 && u < UNITS.len() - 1 {
        v /= 1024.0;
        u += 1;
    }
    if u == 0 {
        format!("{bytes} B")
    } else {
        format!("{v:.1} {}", UNITS[u])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn temp_names_round_trip() {
        let t = temp_path(Path::new("dir/report.pdf"));
        assert_eq!(t, Path::new("dir/.report.pdf.kopy-tmp"));
        assert!(is_temp_name(t.file_name().unwrap()));
        assert!(!is_temp_name(OsStr::new("report.pdf")));
    }

    #[test]
    fn human_sizes() {
        assert_eq!(human(512), "512 B");
        assert_eq!(human(1536), "1.5 KiB");
        assert_eq!(human(5 * 1024 * 1024 * 1024), "5.0 GiB");
    }
}
