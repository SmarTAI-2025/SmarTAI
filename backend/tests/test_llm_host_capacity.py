"""Real OS-lock tests; no providers, network access, or billed requests."""

import errno
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from backend.llm import host_capacity
from backend.llm.host_capacity import HostCapacity, MAX_HOST_CONCURRENCY


def test_rpm_is_paced_before_dispatch_across_processes(tmp_path):
    directory = tmp_path / "quota"
    first = HostCapacity(directory)
    assert first.try_start("same-model-and-key", 2)
    script = "from backend.llm.host_capacity import HostCapacity; import sys; c=HostCapacity(sys.argv[1]); assert not c.try_start('same-model-and-key',2); assert c.try_start('different-model',2)"
    child = subprocess.run([sys.executable, "-c", script, str(directory)], capture_output=True, timeout=10)
    assert child.returncode == 0, child.stderr.decode()


def test_rpm_recovers_at_interval_and_does_not_spend_on_denied_start(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(host_capacity.time, "monotonic", lambda: now[0])
    first, second = HostCapacity(tmp_path / "quota"), HostCapacity(tmp_path / "quota")
    assert first.try_start("key", 2)
    for _ in range(10):
        assert not second.try_start("key", 2)
    now[0] += 30.1
    assert second.try_start("key", 2)
    now[0] += 30.1
    assert first.try_start("key", 2)


def test_separate_instances_share_slots_and_release_is_idempotent(tmp_path):
    directory = tmp_path / "permits"
    first, second = HostCapacity(directory), HostCapacity(directory)
    a, b = first.try_acquire(2), second.try_acquire(2)
    assert a is not None and b is not None
    assert first.try_acquire(2) is None
    assert second.try_acquire(2) is None
    a.release()
    a.release()
    c = second.try_acquire(2)
    assert c is not None
    assert first.try_acquire(2) is None
    b.release()
    c.release()


def test_all_callers_are_fenced_at_fifty(tmp_path):
    capacity = HostCapacity(tmp_path / "permits")
    leases = [capacity.try_acquire(1000) for _ in range(MAX_HOST_CONCURRENCY)]
    try:
        assert all(lease is not None for lease in leases)
        assert HostCapacity(capacity.directory).try_acquire(1000) is None
        assert capacity.try_acquire(0) is None
    finally:
        for lease in leases:
            if lease is not None:
                lease.release()


def test_subprocess_shares_capacity_and_crash_releases_slots(tmp_path):
    directory = tmp_path / "permits"
    capacity = HostCapacity(directory)
    local = capacity.try_acquire(2)
    assert local is not None
    script = """
import os, sys
from backend.llm.host_capacity import HostCapacity
capacity = HostCapacity(sys.argv[1])
lease = capacity.try_acquire(2)
assert lease is not None
assert capacity.try_acquire(2) is None
print('locked', flush=True)
sys.stdin.readline()
os._exit(23)
"""
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(directory)],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        assert capacity.try_acquire(2) is None
        child.stdin.write("crash\n")
        child.stdin.flush()
        assert child.wait(timeout=10) == 23
        recovered = capacity.try_acquire(2)
        assert recovered is not None
        recovered.release()
    finally:
        local.release()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fork inheritance")
def test_fork_child_does_not_retain_parent_lease(tmp_path):
    directory = tmp_path / "permits"
    capacity = HostCapacity(directory)
    lease = capacity.try_acquire(1)
    assert lease is not None
    context = multiprocessing.get_context("fork")
    parent_pipe, child_pipe = context.Pipe()

    def check_after_parent_release():
        child_capacity = HostCapacity(directory)
        child_pipe.send(child_capacity.try_acquire(1) is None)
        child_pipe.recv()
        acquired = child_capacity.try_acquire(1)
        child_pipe.send(acquired is not None)
        if acquired is not None:
            acquired.release()

    child = context.Process(target=check_after_parent_release)
    child.start()
    try:
        assert parent_pipe.poll(10) and parent_pipe.recv()
        lease.release()
        parent_pipe.send("released")
        assert parent_pipe.poll(10) and parent_pipe.recv()
        child.join(timeout=10)
        assert child.exitcode == 0
    finally:
        lease.release()
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)
        parent_pipe.close()
        child_pipe.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_private_directory_and_slot_modes(tmp_path):
    capacity = HostCapacity(tmp_path / "permits")
    lease = capacity.try_acquire(1)
    assert lease is not None
    try:
        assert capacity.directory.stat().st_mode & 0o777 == 0o700
        assert (capacity.directory / "slot-00.lock").stat().st_mode & 0o777 == 0o600
    finally:
        lease.release()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_unsafe_directory_fails_closed_without_disclosing_path(tmp_path):
    directory = tmp_path / "private-looking-secret-path"
    directory.mkdir(mode=0o755)
    with pytest.raises(RuntimeError, match="^LLM host capacity fence is unavailable$"):
        HostCapacity(directory)


@pytest.mark.skipif(os.name == "nt", reason="symlink privileges vary on Windows")
def test_symlinked_directory_and_slot_fail_closed(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="^LLM host capacity fence is unavailable$"):
        HostCapacity(linked)
    capacity = HostCapacity(target)
    outside = tmp_path / "outside"
    outside.write_text("preserve")
    (target / "slot-00.lock").symlink_to(outside)
    with pytest.raises(RuntimeError, match="^LLM host capacity fence is unavailable$"):
        capacity.try_acquire(1)
    assert outside.read_text() == "preserve"


def test_windows_nonblocking_byte_lock_path(tmp_path, monkeypatch):
    operations = []

    def locking(fd, operation, count):
        operations.append((operation, count, os.lseek(fd, 0, os.SEEK_CUR)))

    monkeypatch.setattr(host_capacity, "_IS_WINDOWS", True)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=2, locking=locking))
    capacity = HostCapacity(tmp_path / "permits")
    lease = capacity.try_acquire(1)
    assert lease is not None
    assert operations == [(2, 1, 0)]
    assert (capacity.directory / "slot-00.lock").read_bytes() == b"\0"
    lease.release()


def test_windows_lock_contention_returns_none(tmp_path, monkeypatch):
    def locking(*_args):
        raise OSError(errno.EACCES, "busy")

    monkeypatch.setattr(host_capacity, "_IS_WINDOWS", True)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=2, locking=locking))
    assert HostCapacity(tmp_path / "permits").try_acquire(1) is None


def test_non_contention_lock_errors_fail_closed(tmp_path, monkeypatch):
    def broken(_fd):
        raise OSError(errno.EIO, "private host details")

    monkeypatch.setattr(host_capacity, "_try_lock", broken)
    capacity = HostCapacity(tmp_path / "permits")
    with pytest.raises(RuntimeError, match="^LLM host capacity fence is unavailable$"):
        capacity.try_acquire(1)
