"""Real file/OS-lock fault injection; no provider calls or wall-clock sleeps."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.llm import host_capacity
from backend.llm.host_capacity import HostCapacity


def state_path(capacity, kind):
    return capacity.directory / (kind + "-" + hashlib.sha256(b"key").hexdigest() + ".lock")


def test_reboot_clock_reset_is_persistent_and_cannot_resurrect_cooldown(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(host_capacity.time, "monotonic", lambda: now[0])
    capacity = HostCapacity(tmp_path / "permits")
    assert capacity.defer_start("key", 65) == 65
    now[0] = 10.0  # Restarted host, but retained temporary files.
    assert HostCapacity(capacity.directory).try_start("key", 0)
    now[0] = 1001.0
    restarted = HostCapacity(capacity.directory)
    assert restarted.cooldown_remaining("key") == 0
    assert restarted.try_start("key", 0)


def test_rpm_clock_reset_cannot_keep_part_of_the_old_clock_epoch(tmp_path, monkeypatch):
    now = [950.0]
    monkeypatch.setattr(host_capacity.time, "monotonic", lambda: now[0])
    capacity = HostCapacity(tmp_path / "permits")
    assert capacity.try_start("key", 2)
    now[0] = 1000
    assert capacity.try_start("key", 2)
    now[0] = 960
    assert HostCapacity(capacity.directory).try_start("key", 2)
    now[0] = 1001
    assert HostCapacity(capacity.directory).try_start("key", 2)


@pytest.mark.parametrize("kind", ["rpm", "cooldown"])
@pytest.mark.parametrize("raw", [b"[100,", b"[NaN]", b"{}", b"\xff"])
def test_existing_damaged_record_recovers_after_one_bounded_pause(tmp_path, monkeypatch, kind, raw):
    now = [100.0]
    monkeypatch.setattr(host_capacity.time, "monotonic", lambda: now[0])
    capacity = HostCapacity(tmp_path / "permits")
    path = state_path(capacity, kind)
    path.write_bytes(raw)
    path.chmod(0o600)
    assert not capacity.try_start("key", 6)
    assert capacity.cooldown_remaining("key") == 65
    for now[0] in (101, 150, 164.9):
        assert not HostCapacity(capacity.directory).try_start("key", 6)
    now[0] = 165
    assert HostCapacity(capacity.directory).try_start("key", 6)
    assert not capacity.try_start("key", 6)


@pytest.mark.parametrize("kind", ["rpm", "cooldown"])
@pytest.mark.parametrize("stage", ["temporary", "published", "primary", "truncate", "synced"])
@pytest.mark.parametrize("existing", [False, True])
def test_process_exit_during_write_recovers_complete_state(tmp_path, monkeypatch, kind, stage, existing):
    now = [100.0]
    monkeypatch.setattr(host_capacity.time, "monotonic", lambda: now[0])
    capacity = HostCapacity(tmp_path / "permits")
    path = state_path(capacity, kind)
    if not existing:
        path.touch(mode=0o600)
    elif kind == "rpm":
        assert capacity.try_start("key", 1)
    else:
        assert capacity.defer_start("key", 65) == 65
    inode = path.stat().st_ino
    # Kill an actual child inside os.write/ftruncate/fsync/replace, including
    # partial writes before truncation. Its locks must release automatically.
    script = r'''
import os, sys
from backend.llm import host_capacity as h
directory, kind, stage = sys.argv[1:]
h.time.monotonic = lambda: 200.0
capacity = h.HostCapacity(directory)
original_open, original_write = capacity._open_name, os.write
original_truncate, original_sync, original_replace = os.ftruncate, os.fsync, os.replace
names = {}
def opening(name):
    fd = original_open(name)
    names[fd] = name
    return fd
capacity._open_name = opening
def is_target(fd, suffix):
    name = names.get(fd, "")
    return name.startswith(kind + "-") and name.endswith(suffix)
def writing(fd, body):
    if (stage == "primary" and is_target(fd, ".lock")) or (stage == "temporary" and is_target(fd, ".tmp")):
        original_write(fd, body[:3])
        os._exit(23)
    return original_write(fd, body)
def truncating(fd, size):
    original_truncate(fd, size)
    if stage == "truncate" and is_target(fd, ".lock"):
        os._exit(23)
def syncing(fd):
    original_sync(fd)
    if stage == "synced" and is_target(fd, ".lock"):
        os._exit(23)
def replacing(src, dst, **kwargs):
    original_replace(src, dst, **kwargs)
    if stage == "published" and str(dst).split(os.sep)[-1].startswith(kind + "-"):
        os._exit(23)
os.write, os.ftruncate, os.fsync, os.replace = writing, truncating, syncing, replacing
if kind == "rpm":
    assert capacity.try_start("key", 1)
else:
    capacity.defer_start("key", 65)
raise AssertionError("fault injection did not run")
'''
    child = subprocess.run([sys.executable, "-c", script, str(capacity.directory), kind, stage],
                           capture_output=True, timeout=15)
    assert child.returncode == 23, child.stderr.decode()
    now[0] = 201
    recovered = HostCapacity(capacity.directory)
    if stage == "temporary":
        # No committed write-ahead record: keep the complete previous state.
        assert recovered.try_start("key", 1)
    else:
        assert not recovered.try_start("key", 1)
        now[0] = 265
        assert recovered.try_start("key", 1)
    assert path.stat().st_ino == inode  # Never replace/unlink the lock inode.
    raw = path.read_bytes()
    if raw:
        json.loads(raw)
    else:
        assert not existing and stage == "temporary" and kind == "cooldown"


def test_new_state_is_readable_in_original_rpm_format(tmp_path, monkeypatch):
    monkeypatch.setattr(host_capacity.time, "monotonic", lambda: 100.0)
    capacity = HostCapacity(tmp_path / "permits")
    assert capacity.try_start("key", 6)
    assert json.loads(state_path(capacity, "rpm").read_bytes()) == [100]
    assert (Path(str(state_path(capacity, "rpm")) + ".pending")).read_bytes() == b""


def test_repair_preserves_longer_existing_retry_after(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(host_capacity.time, "monotonic", lambda: now[0])
    capacity = HostCapacity(tmp_path / "permits")
    assert capacity.defer_start("key", 600) == 600
    damaged = state_path(capacity, "rpm")
    damaged.write_bytes(b"[")
    damaged.chmod(0o600)
    assert not capacity.try_start("key", 6)
    assert capacity.cooldown_remaining("key") == 600


def test_storage_error_still_fails_closed(tmp_path, monkeypatch):
    capacity = HostCapacity(tmp_path / "permits")
    def broken(*args, **kwargs):
        raise OSError("synthetic full disk")
    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(RuntimeError, match="^LLM host capacity fence is unavailable$"):
        capacity.defer_start("key", 65)


def test_journal_commit_does_not_release_the_group_lock(tmp_path, monkeypatch):
    capacity = HostCapacity(tmp_path / "permits")
    original_replace = os.replace
    checked = []
    def replacing(src, dst, **kwargs):
        original_replace(src, dst, **kwargs)
        script = """
import sys
from backend.llm.host_capacity import HostCapacity
c = HostCapacity(sys.argv[1])
assert not c.try_start('key', 0)
assert c.try_start('independent', 0)
"""
        child = subprocess.run([sys.executable, "-c", script, str(capacity.directory)],
                               capture_output=True, timeout=10)
        assert child.returncode == 0, child.stderr.decode()
        checked.append(True)
    monkeypatch.setattr(os, "replace", replacing)
    assert capacity.defer_start("key", 65) > 64
    assert checked == [True]
