from __future__ import annotations

from fnmatch import fnmatchcase

from .util import IS_WINDOWS

# System folders at a drive root that must never be copied or purged.
PROTECTED_DIRS = frozenset({"system volume information", "$recycle.bin"})


def _norm(s: str) -> str:
    s = s.replace("\\", "/")
    return s.lower() if IS_WINDOWS else s


class Filters:
    """Globs match a file/dir name or its path relative to the root. Excluded
    files are neither copied nor purged, matching robocopy semantics."""

    def __init__(self, exclude_files: list[str], exclude_dirs: list[str], include: list[str]):
        self.xf = [_norm(p) for p in exclude_files]
        self.xd = [_norm(p) for p in exclude_dirs]
        self.include = [_norm(p) for p in include]

    @staticmethod
    def _hit(patterns: list[str], rel: str) -> bool:
        if not patterns:
            return False
        r = _norm(rel)
        name = r.rsplit("/", 1)[-1]
        return any(fnmatchcase(r, p) or fnmatchcase(name, p) for p in patterns)

    def file_excluded(self, rel: str) -> bool:
        if self._hit(self.xf, rel):
            return True
        return bool(self.include) and not self._hit(self.include, rel)

    def dir_excluded(self, rel: str) -> bool:
        name = rel.replace("\\", "/").rsplit("/", 1)[-1].lower()
        return name in PROTECTED_DIRS or self._hit(self.xd, rel)
