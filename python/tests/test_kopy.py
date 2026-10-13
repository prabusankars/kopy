import errno
import filecmp
import os
from pathlib import Path


from kopy.__main__ import main
from kopy.copier import backoff
from kopy.errors import (DEST, SOURCE, DiskFull, IoFailure, SourceVanished, WriteProtected,
                         classify)
from kopy.filters import Filters
from kopy.util import human, is_temp_name, temp_path


def make_tree(root: Path, files: int = 30) -> None:
    for i in range(files):
        d = root / f"d{i % 3}" / "sub"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"f{i}.dat").write_bytes(os.urandom(1000 + i * 37))
    (root / "empty").mkdir(exist_ok=True)
    (root / "readme.txt").write_text("hello")


def same_tree(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b)

    def ok(c):
        if c.left_only or c.right_only or c.diff_files or c.funny_files:
            return False
        return all(ok(sub) for sub in c.subdirs.values())

    return ok(cmp)


def run(*args) -> int:
    return main([str(a) for a in args] + ["-q"])


# ---------------------------------------------------------------- behaviour

def test_copy_then_noop(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_tree(src)
    assert run(src, dst) == 1
    assert same_tree(src, dst)
    assert (dst / "empty").is_dir()
    assert run(src, dst) == 0  # nothing to do


def test_timestamps_preserved(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_tree(src, 3)
    f = src / "readme.txt"
    os.utime(f, (1_577_836_800, 1_577_836_800))
    run(src, dst)
    assert (dst / "readme.txt").stat().st_mtime == f.stat().st_mtime


def test_resume_after_interruption(tmp_path):
    """Half the files present, one half-written temp left behind: the rerun
    copies only what is missing and cleans up the temp file."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_tree(src, 40)
    run(src, dst)
    victims = sorted(dst.rglob("*.dat"))[:20]
    for v in victims:
        v.unlink()
    stale = Path(temp_path(str(victims[0])))
    stale.write_bytes(b"partial")

    out_log = tmp_path / "log.txt"
    assert run(src, dst, "--log", out_log) == 1
    assert same_tree(src, dst)
    assert not stale.exists()
    assert out_log.read_text().count("New File") == 20


def test_mirror_deletes_extras_but_keeps_excluded(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_tree(src, 6)
    run(src, dst)
    (dst / "extra.txt").write_text("x")
    (dst / "olddir" / "deep").mkdir(parents=True)
    (dst / "olddir" / "deep" / "z.txt").write_text("z")
    (dst / "keep.bak").write_text("protected by --xf")
    assert run(src, dst, "--mirror", "--xf", "*.bak") == 2
    assert not (dst / "extra.txt").exists()
    assert not (dst / "olddir").exists()
    assert (dst / "keep.bak").exists()


def test_extras_reported_not_deleted_without_purge(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_tree(src, 3)
    run(src, dst)
    (dst / "extra.txt").write_text("x")
    assert run(src, dst) == 2
    assert (dst / "extra.txt").exists()


def test_dry_run_changes_nothing(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_tree(src, 5)
    assert run(src, dst, "-n", "--mirror") == 0
    assert not dst.exists()


def test_changed_and_older_files_recopied(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_tree(src, 5)
    run(src, dst)
    (src / "readme.txt").write_text("changed content")
    old = src / "d0" / "sub" / "f0.dat"
    os.utime(old, (1_000_000_000, 1_000_000_000))
    assert run(src, dst) == 1
    assert same_tree(src, dst)


def test_large_file_pipelined_with_verify(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    (src / "big.bin").write_bytes(os.urandom(5 * 1024 * 1024 + 123))
    assert run(src, dst, "--verify", "--buffer-mb", "1", "--large-mb", "1") == 1
    assert same_tree(src, dst)


def test_readonly_destination_file_is_replaced(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_tree(src, 2)
    run(src, dst)
    (src / "readme.txt").write_text("new version")
    os.chmod(dst / "readme.txt", 0o444)
    assert run(src, dst) == 1
    assert (dst / "readme.txt").read_text() == "new version"


def test_excluded_dirs_and_include(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_tree(src, 6)
    (src / "node_modules").mkdir()
    (src / "node_modules" / "x.js").write_text("x")
    run(src, dst, "--xd", "node_modules", "--include", "*.dat")
    assert not (dst / "node_modules").exists()
    assert not (dst / "readme.txt").exists()
    assert len(list(dst.rglob("*.dat"))) == 6


def test_dest_inside_source_is_not_recursed(tmp_path):
    src = tmp_path / "src"
    make_tree(src, 3)
    dst = src / "backup"
    assert run(src, dst) == 1
    assert not (dst / "backup").exists()


def test_bad_source_is_fatal(tmp_path):
    assert run(tmp_path / "missing", tmp_path / "dst") == 16


# ---------------------------------------------------------------- units

def test_classify_dest_vs_source():
    full = OSError(errno.ENOSPC, "No space left")
    assert isinstance(classify("write", "x", DEST, full), DiskFull)
    assert not classify("read", "x", SOURCE, full).fatal
    ro = OSError(errno.EROFS, "Read-only file system")
    assert isinstance(classify("write", "x", DEST, ro), WriteProtected)


def test_classify_vanished_and_retryable():
    nf = FileNotFoundError(errno.ENOENT, "gone")
    assert isinstance(classify("open", "x", SOURCE, nf), SourceVanished)
    assert classify("create", "x", DEST, nf).retryable
    assert classify("read", "x", SOURCE, TimeoutError(errno.ETIMEDOUT, "t")).retryable
    denied = PermissionError(errno.EACCES, "denied")
    err = classify("read", "x", SOURCE, denied)
    assert isinstance(err, IoFailure) and not err.retryable


def test_windows_codes_classified():
    e = OSError(errno.EINVAL, "net name deleted")
    e.winerror = 64  # SMB session dropped
    assert classify("read", "x", SOURCE, e).retryable
    wp = OSError(errno.EACCES, "write protect")
    wp.winerror = 19
    assert isinstance(classify("write", "x", DEST, wp), WriteProtected)


def test_filters():
    f = Filters(["*.tmp"], [".git"], [])
    assert f.file_excluded("a/b/x.tmp")
    assert not f.file_excluded("a/b/x.txt")
    assert f.dir_excluded("proj/.git")
    assert f.dir_excluded("System Volume Information")


def test_helpers():
    assert backoff(2, 1) == 2 and backoff(2, 3) == 8 and backoff(2, 20) == 60
    t = temp_path(os.path.join("dir", "r.pdf"))
    assert is_temp_name(os.path.basename(t)) and not is_temp_name("r.pdf")
    assert human(512) == "512 B" and human(1536) == "1.5 KiB"
