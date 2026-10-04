"""Nonblocking host-wide LLM permits shared by application workers of one user.

POSIX uses flock, a private 0700 directory, and 0600 non-symlink slot files.
Windows uses one-byte msvcrt locks in the user's temporary directory and rejects
symlinks/reparse points. Windows directory privacy depends on inherited user
ACLs and requires verification on a Windows host; POSIX modes are not Windows
ACLs. Every worker must use the same directory on a local filesystem. Directory
and slot files must not be removed while workers are running.

Closing a lease or process termination releases its OS lock. A lower ``limit``
selects fewer slots for new admissions; it does not cancel existing leases.
"""

from __future__ import annotations

import errno
import getpass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time


MAX_HOST_CONCURRENCY = 50
_IS_WINDOWS = os.name == "nt"
_FENCE_ERROR = "LLM host capacity fence is unavailable"
_lease_fds: set[int] = set()
_lease_lock = threading.RLock()


def _after_fork_child() -> None:
    # Closing inherited descriptors (without LOCK_UN) preserves parent locks
    # and prevents a PDF child process from retaining them after parent death.
    global _lease_lock
    for fd in tuple(_lease_fds):
        try:
            os.close(fd)
        except OSError:
            pass
    _lease_fds.clear()
    _lease_lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=lambda: _lease_lock.acquire(),
        after_in_parent=lambda: _lease_lock.release(),
        after_in_child=_after_fork_child,
    )


def _default_directory() -> Path:
    if _IS_WINDOWS:
        user = hashlib.sha256(getpass.getuser().encode()).hexdigest()[:16]
        return Path(tempfile.gettempdir()) / f"smartai-llm-host-capacity-v1-{user}"
    # A fixed root also shares permits when workers have different TMPDIRs.
    return Path("/tmp") / f"smartai-llm-host-capacity-v1-{os.getuid()}"


def _reject_link(info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise OSError("unsafe capacity entry")


def _validate_entry(info: os.stat_result, *, directory: bool) -> None:
    _reject_link(info)
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode):
        raise OSError("invalid capacity entry")
    if not directory and info.st_nlink != 1:
        raise OSError("linked capacity entry")
    if not _IS_WINDOWS and (
        info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
    ):
        raise OSError("unsafe capacity permissions")


def _try_lock(fd: int) -> bool:
    try:
        if _IS_WINDOWS:
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        busy_errors = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}
        if _IS_WINDOWS:
            busy_errors.add(errno.EDEADLK)
        if exc.errno in busy_errors or (
            _IS_WINDOWS and getattr(exc, "winerror", None) == 33
        ):
            return False
        raise


class HostLease:
    """Own one slot until released; release is idempotent and thread-safe."""

    def __init__(self, fd: int):
        self._fd: int | None = fd
        self._pid = os.getpid()

    def release(self) -> None:
        with _lease_lock:
            fd, self._fd = self._fd, None
            if fd is None or self._pid != os.getpid():
                return
            _lease_fds.discard(fd)
            try:
                # close releases both flock and msvcrt byte-range locks.
                os.close(fd)
            except OSError:
                raise RuntimeError(_FENCE_ERROR) from None

    def __enter__(self) -> HostLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass


class HostCapacity:
    """Try an OS permit without waiting, clamping all callers to 50 slots."""

    def __init__(self, directory: str | os.PathLike[str] | None = None):
        self.directory = Path(directory) if directory is not None else _default_directory()
        self._directory_fd: int | None = None
        try:
            try:
                os.mkdir(self.directory, 0o700)
            except FileExistsError:
                pass
            info = os.lstat(self.directory)
            _validate_entry(info, directory=True)
            if not _IS_WINDOWS:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                flags |= getattr(os, "O_CLOEXEC", 0)
                self._directory_fd = os.open(self.directory, flags)
                opened = os.fstat(self._directory_fd)
                _validate_entry(opened, directory=True)
                if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                    raise OSError("capacity directory changed")
        except (OSError, ImportError):
            self.close()
            raise RuntimeError(_FENCE_ERROR) from None

    def _open_slot(self, slot: int) -> int:
        return self._open_name(f"slot-{slot:02d}.lock")

    def _open_name(self, name: str) -> int:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        fd: int | None = None
        try:
            if not _IS_WINDOWS:
                if self._directory_fd is None:
                    raise OSError("capacity directory is closed")
                flags |= os.O_NOFOLLOW
                fd = os.open(name, flags, 0o600, dir_fd=self._directory_fd)
            else:
                before = os.lstat(self.directory)
                _validate_entry(before, directory=True)
                path = self.directory / name
                try:
                    _validate_entry(os.lstat(path), directory=False)
                except FileNotFoundError:
                    pass
                fd = os.open(path, flags, 0o600)
                after = os.lstat(self.directory)
                _validate_entry(after, directory=True)
                path_info = os.lstat(path)
                _validate_entry(path_info, directory=False)
                opened = os.fstat(fd)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise OSError("capacity directory changed")
                if (path_info.st_dev, path_info.st_ino) != (opened.st_dev, opened.st_ino):
                    raise OSError("capacity file changed")
            _validate_entry(os.fstat(fd), directory=False)
            return fd
        except (OSError, ImportError):
            if fd is not None:
                os.close(fd)
            raise RuntimeError(_FENCE_ERROR) from None

    def try_start(self, key: str, rpm: int) -> bool:
        """Atomically pace actual requests across web and workflow processes.

        Store only a quota-key digest and monotonic timestamps. A rejected
        admission consumes no quota; cancellation before admission is free.
        """
        if rpm <= 0:
            return True
        name = "rpm-" + hashlib.sha256(key.encode()).hexdigest() + ".lock"
        fd = self._open_name(name)
        with _lease_lock:
            locked = False
            try:
                locked = _try_lock(fd)
                if not locked:
                    return False
                _lease_fds.add(fd)
                now = time.monotonic()
                os.lseek(fd, 0, os.SEEK_SET)
                raw = os.read(fd, 300_001)
                if len(raw) > 300_000:
                    raise ValueError("invalid quota window")
                starts = json.loads(raw) if raw and raw != b"\0" else []
                if not isinstance(starts, list) or any(type(t) not in (int, float) for t in starts):
                    raise ValueError("invalid quota window")
                starts = [t for t in starts if now - 60 < t <= now]
                if len(starts) >= rpm or (starts and now - starts[-1] < 60.0 / rpm):
                    return False
                starts.append(now)
                body = json.dumps(starts[-10_000:], separators=(",", ":")).encode()
                os.lseek(fd, 0, os.SEEK_SET)
                if os.write(fd, body) != len(body):
                    raise OSError("incomplete quota window write")
                os.ftruncate(fd, len(body))
                return True
            except (OSError, ValueError, TypeError):
                raise RuntimeError(_FENCE_ERROR) from None
            finally:
                _lease_fds.discard(fd)
                os.close(fd)

    def try_acquire(self, limit: int = MAX_HOST_CONCURRENCY) -> HostLease | None:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("host capacity limit must be an integer")
        for slot in range(max(0, min(limit, MAX_HOST_CONCURRENCY))):
            fd = self._open_slot(slot)
            try:
                with _lease_lock:
                    if _try_lock(fd):
                        _lease_fds.add(fd)
                        return HostLease(fd)
            except (OSError, ImportError):
                os.close(fd)
                raise RuntimeError(_FENCE_ERROR) from None
            os.close(fd)
        return None

    def close(self) -> None:
        fd, self._directory_fd = self._directory_fd, None
        if fd is not None:
            os.close(fd)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
