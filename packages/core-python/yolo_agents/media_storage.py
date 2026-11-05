"""Private content-addressed media files for one Store."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from uuid import uuid4


_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_CANONICAL = re.compile(r"^(\d{4})/(0[1-9]|1[0-2])/([0-9a-f]{64})\.(jpg|png|webp)$")
_TEMP = re.compile(r"^\.([0-9a-f]{64})\.(jpg|png|webp)\.tmp-[0-9a-f]{32}$")


class MediaFileError(RuntimeError):
    """A managed media path or file failed closed validation."""


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class MediaStorage:
    def __init__(self, root: str | Path):
        # abspath keeps the caller's path spelling and therefore cannot hide a
        # pre-existing root symlink the way Path.resolve() would.
        self.root = Path(os.path.abspath(Path(root).expanduser()))
        self._ensure_directory(self.root)

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        try:
            path.lstat()
            created = False
        except FileNotFoundError:
            path.mkdir(mode=0o700, parents=True, exist_ok=False)
            created = True
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise MediaFileError("Managed media directory is not a private directory.")
        os.chmod(path, 0o700)
        if created:
            _fsync_directory(path)
            _fsync_directory(path.parent)

    @staticmethod
    def storage_path(created_at: str, digest: str, media_type: str) -> str:
        if not isinstance(created_at, str) or not created_at.endswith("Z"):
            raise MediaFileError("Media creation timestamp is not canonical UTC.")
        try:
            timestamp = datetime.fromisoformat(created_at[:-1] + "+00:00")
        except ValueError:
            raise MediaFileError("Media creation timestamp is invalid.") from None
        if timestamp.utcoffset() != UTC.utcoffset(timestamp):
            raise MediaFileError("Media creation timestamp is not UTC.")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise MediaFileError("Media digest is invalid.")
        extension = _EXTENSIONS.get(media_type)
        if extension is None:
            raise MediaFileError("Media type has no managed extension.")
        return f"{timestamp:%Y/%m}/{digest}.{extension}"

    @classmethod
    def validate_storage_path(
        cls, storage_path: str, *, created_at: str, digest: str, media_type: str,
    ) -> str:
        if (not isinstance(storage_path, str) or not storage_path
                or PurePosixPath(storage_path).is_absolute()
                or ".." in PurePosixPath(storage_path).parts
                or _CANONICAL.fullmatch(storage_path) is None
                or storage_path != cls.storage_path(created_at, digest, media_type)):
            raise MediaFileError("Stored media path is not canonical.")
        return storage_path

    def _managed_path(self, storage_path: str) -> Path:
        match = _CANONICAL.fullmatch(storage_path)
        if match is None:
            raise MediaFileError("Stored media path is not canonical.")
        current = self.root
        root_info = current.lstat()
        if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
                or stat.S_IMODE(root_info.st_mode) != 0o700):
            raise MediaFileError("Managed media root is unsafe.")
        for component in (match.group(1), match.group(2)):
            current = current / component
            info = current.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise MediaFileError("Managed media path crosses an unsafe directory.")
        return current / PurePosixPath(storage_path).name

    def _month_directory(self, storage_path: str) -> Path:
        match = _CANONICAL.fullmatch(storage_path)
        if match is None:
            raise MediaFileError("Stored media path is not canonical.")
        root_info = self.root.lstat()
        if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
                or stat.S_IMODE(root_info.st_mode) != 0o700):
            raise MediaFileError("Managed media root is unsafe.")
        year = self.root / match.group(1)
        month = year / match.group(2)
        self._ensure_directory(year)
        self._ensure_directory(month)
        return month

    @staticmethod
    def _verify_descriptor(descriptor: int, *, byte_count: int, digest: str) -> bytes:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != byte_count):
            raise MediaFileError("Managed media file has an invalid type or size.")
        chunks, remaining = [], byte_count + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != byte_count or hashlib.sha256(data).hexdigest() != digest:
            raise MediaFileError("Managed media file failed integrity verification.")
        return data

    def read(self, storage_path: str, *, created_at: str, digest: str,
             media_type: str, byte_count: int) -> bytes:
        self.validate_storage_path(
            storage_path, created_at=created_at, digest=digest, media_type=media_type,
        )
        try:
            target = self._managed_path(storage_path)
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except (OSError, FileNotFoundError):
            raise MediaFileError("Managed media file cannot be safely opened.") from None
        try:
            return self._verify_descriptor(descriptor, byte_count=byte_count, digest=digest)
        finally:
            os.close(descriptor)

    def write(self, data: bytes, *, storage_path: str, created_at: str,
              digest: str, media_type: str) -> bool:
        self.validate_storage_path(
            storage_path, created_at=created_at, digest=digest, media_type=media_type,
        )
        if hashlib.sha256(data).hexdigest() != digest:
            raise MediaFileError("Media bytes do not match their digest.")
        month = self._month_directory(storage_path)
        target = month / PurePosixPath(storage_path).name
        if target.exists() or target.is_symlink():
            self.read(storage_path, created_at=created_at, digest=digest,
                      media_type=media_type, byte_count=len(data))
            return False

        temporary = month / f".{target.name}.tmp-{uuid4().hex}"
        descriptor = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise MediaFileError("Managed media file write did not progress.")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.chmod(temporary, 0o600, follow_symlinks=False)
            try:
                # Publish atomically without replacing a concurrent or corrupt target.
                os.link(temporary, target, follow_symlinks=False)
                created = True
            except FileExistsError:
                self.read(storage_path, created_at=created_at, digest=digest,
                          media_type=media_type, byte_count=len(data))
                created = False
            finally:
                temporary.unlink(missing_ok=True)
            _fsync_directory(month)
            return created
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def remove(self, storage_path: str) -> None:
        if _CANONICAL.fullmatch(storage_path) is None:
            raise MediaFileError("Stored media path is not canonical.")
        try:
            target = self._managed_path(storage_path)
            info = target.lstat()
        except FileNotFoundError:
            return
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise MediaFileError("Managed media deletion target is unsafe.")
        target.unlink()
        _fsync_directory(target.parent)

    def cleanup_residue(self, referenced: set[str]) -> None:
        """Remove only canonical unreferenced files and recognizable temp files."""
        try:
            root_info = self.root.lstat()
            if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
                    or stat.S_IMODE(root_info.st_mode) != 0o700):
                raise MediaFileError("Managed media root is unsafe.")
            years = list(self.root.iterdir())
        except OSError:
            raise MediaFileError("Managed media root cannot be scanned.") from None
        for year in years:
            year_info = year.lstat()
            if (year.is_symlink() or not stat.S_ISDIR(year_info.st_mode)
                    or stat.S_IMODE(year_info.st_mode) != 0o700
                    or re.fullmatch(r"\d{4}", year.name) is None):
                continue
            for month in list(year.iterdir()):
                month_info = month.lstat()
                if (month.is_symlink() or not stat.S_ISDIR(month_info.st_mode)
                        or stat.S_IMODE(month_info.st_mode) != 0o700
                        or re.fullmatch(r"0[1-9]|1[0-2]", month.name) is None):
                    continue
                for target in list(month.iterdir()):
                    if target.is_symlink():
                        continue
                    relative = f"{year.name}/{month.name}/{target.name}"
                    managed = _CANONICAL.fullmatch(relative) is not None
                    temporary = _TEMP.fullmatch(target.name) is not None
                    if target.is_file() and ((managed and relative not in referenced) or temporary):
                        target.unlink()
                _fsync_directory(month)


class TemporaryMediaStorage:
    """Owned temporary root used by an in-memory Store."""

    def __init__(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="yolo-agents-media-")
        self.storage = MediaStorage(Path(self._temporary.name) / "media")

    def cleanup(self) -> None:
        self._temporary.cleanup()
