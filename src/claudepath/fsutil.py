"""
Filesystem helpers for the automatic mode: atomic writes and locking.

Claude Code guards ~/.claude.json and ~/.claude/history.jsonl with the
proper-lockfile package: the lock is a directory named "<file>.lock", created
with mkdir (atomic), refreshed every 5 seconds while held, and treated as stale
once its mtime is more than 10 seconds old. file_lock() takes the very same
lock, so a running Claude Code session waits for our rewrite instead of
appending to a file that is about to be replaced.
"""

import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Generator, Optional, Union

# proper-lockfile's default, which Claude Code uses for both files.
STALE_LOCK_SECONDS = 10.0

PathLike = Union[str, Path]


class LockTimeout(OSError):
    """Raised when a lock is still held by someone else after the timeout."""


@contextmanager
def file_lock(
    target: PathLike,
    timeout: float = 3.0,
    lock_path: Optional[PathLike] = None,
    stale: float = STALE_LOCK_SECONDS,
) -> Generator[None, None, None]:
    """Hold a proper-lockfile compatible lock on `target`.

    The lock directory defaults to "<target>.lock". A lock whose mtime is older
    than `stale` seconds is considered abandoned and is taken over, exactly as
    proper-lockfile does.
    """
    lock = Path(lock_path) if lock_path is not None else Path(str(target) + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            pass
        try:
            age = time.time() - os.stat(lock).st_mtime
        except FileNotFoundError:
            age = None  # released between our mkdir and stat: just retry
        if age is not None and age > stale:
            try:
                os.rmdir(lock)
            except OSError:
                pass
            continue
        if time.monotonic() >= deadline:
            raise LockTimeout(f"{target} is locked by another process ({lock})")
        time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass


@contextmanager
def process_lock(lock_file: PathLike, timeout: float = 3.0) -> Generator[None, None, None]:
    """Hold claudepath's own lock: an exclusive flock() on `lock_file`.

    Unlike file_lock() this lock does not go stale on a timer -- a migration
    of a large project can legitimately take longer than 10 seconds -- and it
    needs no takeover logic: the kernel releases it when the holder exits or
    dies. The file itself is left in place (deleting lock files reintroduces
    races).
    """
    import fcntl

    fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"another claudepath process is running ({lock_file})")
                time.sleep(0.05)
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())  # informational only
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class AtomicFile:
    """Write a file through a temp file in the same directory + os.replace.

    Usage:
        with AtomicFile(path, keep_times=True) as f:
            f.write(data)          # or call discard() to leave `path` alone

    The new file keeps the old file's permission bits, and its atime/mtime when
    `keep_times` is set. If `path` is a symlink, the file it points to is
    replaced and the link itself survives. On an exception the temp file is
    removed and `path` is left untouched.
    """

    def __init__(self, path: PathLike, keep_times: bool = False):
        self.path = Path(os.path.realpath(path))
        self.keep_times = keep_times
        self._discarded = False
        self._tmp: Optional[str] = None
        self._file: Optional[BinaryIO] = None
        try:
            self._stat: Optional[os.stat_result] = os.stat(self.path)
        except FileNotFoundError:
            self._stat = None

    def __enter__(self) -> BinaryIO:
        import tempfile

        fd, self._tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".claudepath-tmp"
        )
        self._file = os.fdopen(fd, "wb", buffering=1024 * 1024)
        return self._file

    def discard(self) -> None:
        """Leave the original file untouched when the block exits."""
        self._discarded = True

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self._file is not None and self._tmp is not None
        try:
            if exc_type is None and not self._discarded:
                self._file.flush()
                os.fsync(self._file.fileno())
                self._file.close()
                if self._stat is not None:
                    os.chmod(self._tmp, stat.S_IMODE(self._stat.st_mode))
                os.replace(self._tmp, self.path)
                self._tmp = None
                if self.keep_times and self._stat is not None:
                    try:
                        os.utime(self.path, ns=(self._stat.st_atime_ns, self._stat.st_mtime_ns))
                    except OSError:
                        pass
        finally:
            if not self._file.closed:
                self._file.close()
            if self._tmp is not None:
                try:
                    os.unlink(self._tmp)
                except OSError:
                    pass


def atomic_write(path: PathLike, data: bytes, keep_times: bool = False) -> None:
    """Atomically replace `path` with `data` (see AtomicFile)."""
    with AtomicFile(path, keep_times=keep_times) as f:
        f.write(data)
