"""Tests for racefree_filelock.

Every test exercises real filesystem state — no mocks for the
under-test code path. The race test spawns real subprocesses so the
O_EXCL guarantee is exercised through a genuine kernel-level race, not
a simulated one.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
from pathlib import Path

import pytest

from racefree_filelock import FileLock, LockHeld, is_pid_alive


def test_acquire_release_roundtrip(tmp_path: Path) -> None:
    lock_path = tmp_path / "app.pid"
    lock = FileLock(lock_path, register_atexit=False)

    pid = lock.acquire()
    assert pid == os.getpid()
    assert lock_path.exists()
    assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())

    lock.release()
    assert not lock_path.exists()


def _holder_worker(
    lock_path: str, acquired_q: "mp.Queue[int]", release_q: "mp.Queue[bool]"
) -> None:
    """Acquire the lock, signal the parent with our PID, wait until
    parent signals us to release."""
    from racefree_filelock import FileLock

    lock = FileLock(lock_path, register_atexit=False)
    lock.acquire()
    acquired_q.put(os.getpid())
    release_q.get(timeout=30)
    lock.release()


def test_refuse_when_live_holder_present(tmp_path: Path) -> None:
    """A live foreign PID holding the lock must cause acquire() to raise
    LockHeld — not steal-takeover, not silently succeed."""
    lock_path = tmp_path / "app.pid"
    ctx = mp.get_context("spawn")
    acquired_q: "mp.Queue[int]" = ctx.Queue()
    release_q: "mp.Queue[bool]" = ctx.Queue()

    holder = ctx.Process(
        target=_holder_worker, args=(str(lock_path), acquired_q, release_q)
    )
    holder.start()
    try:
        holder_pid = acquired_q.get(timeout=15)
        assert holder_pid != os.getpid()
        assert is_pid_alive(holder_pid), "child must be alive while holding lock"

        intruder = FileLock(lock_path, register_atexit=False)
        with pytest.raises(LockHeld) as exc_info:
            intruder.acquire()
        assert exc_info.value.holder_pid == holder_pid
    finally:
        release_q.put(True)
        holder.join(timeout=15)
        assert not holder.is_alive(), "holder failed to exit"


def test_stale_pid_takeover(tmp_path: Path) -> None:
    lock_path = tmp_path / "app.pid"
    # PID 2**31 - 1 is the max int32. Vanishingly unlikely to be a real
    # running process on any sane system; if your CI has 2.1B processes
    # this test will be the least of your problems.
    dead_pid = 2_147_483_646
    assert not is_pid_alive(dead_pid)
    lock_path.write_text(str(dead_pid), encoding="utf-8")

    lock = FileLock(lock_path, register_atexit=False)
    pid = lock.acquire()
    assert pid == os.getpid()
    assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_idempotent_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "app.pid"
    lock = FileLock(lock_path, register_atexit=False)

    lock.acquire()
    lock.release()
    # Second release must not crash and must not blow up if the file
    # is now missing.
    lock.release()
    lock.release()


def test_context_manager_releases_on_exit(tmp_path: Path) -> None:
    lock_path = tmp_path / "app.pid"

    with FileLock(lock_path, register_atexit=False) as lock:
        assert lock_path.exists()
        assert lock._owner_pid == os.getpid()
    assert not lock_path.exists()

    # And on exception
    with pytest.raises(RuntimeError):
        with FileLock(lock_path, register_atexit=False):
            assert lock_path.exists()
            raise RuntimeError("boom")
    assert not lock_path.exists()


def _race_worker(lock_path: str, result_queue: "mp.Queue[str]") -> None:
    """Run in a subprocess. Try to acquire; report 'win' or 'lose'."""
    from racefree_filelock import FileLock, LockHeld

    lock = FileLock(lock_path, register_atexit=False)
    try:
        lock.acquire()
        result_queue.put(f"win:{os.getpid()}")
        # Hold the lock long enough that every sibling reaches the
        # FileExistsError branch and then the alive-holder check.
        import time

        time.sleep(2.0)
        lock.release()
    except LockHeld as e:
        result_queue.put(f"lose:{os.getpid()}:held_by={e.holder_pid}")
    except Exception as e:
        result_queue.put(f"error:{os.getpid()}:{type(e).__name__}:{e}")


@pytest.mark.skipif(
    sys.platform == "win32" and sys.version_info < (3, 8),
    reason="multiprocessing spawn semantics on old Windows Pythons",
)
def test_exactly_one_winner_under_concurrent_race(tmp_path: Path) -> None:
    """Spawn 8 concurrent acquirers. Exactly one wins; the rest must
    hit LockHeld (never crash, never both think they hold the lock).

    This is the test that would fail under the check-then-write
    implementation that motivated this library — multiple processes
    would have read 'no file' and all written their PIDs in sequence,
    each thinking it held the lock, the last writer's PID stuck. With
    O_EXCL the kernel guarantees a single winner."""
    lock_path = str(tmp_path / "race.pid")
    ctx = mp.get_context("spawn")
    result_queue: "mp.Queue[str]" = ctx.Queue()

    n_workers = 8
    procs = [
        ctx.Process(target=_race_worker, args=(lock_path, result_queue))
        for _ in range(n_workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=15)
        assert not p.is_alive(), "worker hung"

    results = []
    while not result_queue.empty():
        results.append(result_queue.get_nowait())

    wins = [r for r in results if r.startswith("win:")]
    losses = [r for r in results if r.startswith("lose:")]
    errors = [r for r in results if r.startswith("error:")]

    assert errors == [], f"unexpected worker errors: {errors}"
    assert len(results) == n_workers, (
        f"missing results: got {len(results)}, want {n_workers}"
    )
    assert len(wins) == 1, f"expected exactly 1 winner, got {len(wins)}: {wins}"
    assert len(losses) == n_workers - 1, (
        f"expected {n_workers - 1} losers, got {len(losses)}"
    )
