# racefree-filelock

A tiny (~220 LOC, zero required deps, Python 3.8+) **cross-process** PID-file lock that fixes the check-then-write race in naive implementations.

Extracted from a private long-running system after an adversarial review found that a naive PID-file lock was admitting 5+ concurrent processes under load.

> **Status (v0.1.0):** Source-only release. Not yet published to PyPI — install from GitHub (see [Install](#install) below). PyPI publish is planned.

---

## What this is

A way for **N processes** racing to acquire a single named resource (a watchdog cycle, a cron job, a singleton service) to guarantee that **exactly one wins**, even when they start within microseconds of each other. The "lock" is a PID file on disk. If the recorded holder dies without releasing, the next caller takes over safely.

## What this is NOT — read this first

| If you need... | Use... |
|---|---|
| **Locking inside one Python process** (between threads or asyncio tasks) | `threading.Lock`, `asyncio.Lock`. *This library is a no-op within one process — PIDs match.* |
| **Blocking acquire with timeout** ("wait up to N seconds for the lock") | [`filelock`](https://pypi.org/project/filelock/) or [`portalocker`](https://pypi.org/project/portalocker/) |
| **Kernel-enforced advisory locking** (`fcntl.flock`, `LockFileEx`) | `filelock` or `portalocker`. *PID files can be deleted by anyone with write permission; kernel locks can't.* |
| **Distributed locking** across machines | Redis (`SETNX` + TTL), etcd, ZooKeeper. *PID files on a shared NFS mount are NOT safe — see Limitations.* |
| **A lock that survives the host machine restarting and prevents the next boot from starting too soon** | A database row or an externally-managed lock service. *This lock is process-bound; PID 1234 from yesterday and PID 1234 from today are indistinguishable to a stale check.* |

**Use this when**: a small number of processes on **one machine** with a **local filesystem** need to agree on "only one of us runs at a time" and you want fail-fast semantics.

---

## The vulnerability this fixes

The naive PID-file lock looks safe:

```python
# BROKEN — race window between the check and the write.
if not lock_path.exists():
    lock_path.write_text(str(os.getpid()))
    proceed()
else:
    holder = int(lock_path.read_text())
    if not is_alive(holder):
        lock_path.write_text(str(os.getpid()))  # take over
        proceed()
    else:
        refuse()
```

Two processes that start within microseconds of each other both see "file doesn't exist," both write their PID, the second writer clobbers the first, **both think they hold the lock**. The same race exists in the stale-takeover branch.

This was observed in production: a Windows Scheduled Task was firing its watchdog 4 times in 64 seconds on logon, and the naive lock let 2 of 4 spawning processes through.

## The fix

```python
# Atomic — the kernel guarantees exactly one winner.
fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
```

`O_CREAT | O_EXCL` is the POSIX-standard atomic create-or-fail. Windows honors it. If the file exists, the call raises `FileExistsError` no matter what other process is mid-write.

Stale-holder takeover uses `tempfile.NamedTemporaryFile` + `os.replace`, which is atomic on POSIX and atomic-since-Vista on Windows. A re-check after the replace catches the rare case where two processes raced the same takeover — only one wins, the loser refuses.

---

## Install

Until v0.1.0 lands on PyPI, install directly from GitHub:

```bash
pip install git+https://github.com/aiton08255/racefree-filelock

# Optional: psutil enables identity-aware liveness checks (recommended)
pip install psutil
```

Or clone and install editable for local hacking:

```bash
git clone https://github.com/aiton08255/racefree-filelock
pip install -e "./racefree-filelock[dev]"
```

Once published to PyPI (planned), the install will simplify to:

```bash
pip install racefree-filelock[psutil]
```

## Use

```python
from racefree_filelock import FileLock, LockHeld

# Context manager (recommended)
with FileLock("/tmp/myapp.pid") as lock:
    do_exclusive_work()

# Manual
lock = FileLock("/tmp/myapp.pid")
try:
    lock.acquire()
    do_exclusive_work()
except LockHeld as e:
    print(f"another instance is running (PID {e.holder_pid})")
finally:
    lock.release()
```

### Identity-aware liveness (recommended for shared lock paths)

If a recorded PID has been recycled by an unrelated process — a real risk on Windows where PIDs are aggressively reused — pass an `identity` substring. The holder counts as alive only if its command line contains it.

```python
# Requires psutil
lock = FileLock("/var/lock/myservice.pid", identity="myservice-worker")
```

Without psutil, this falls back to existence-only checks (still correct in the common case, fragile under heavy PID reuse).

---

## Limitations — the honest list

These are things this lock does NOT do. They are not bugs; they are scope decisions. Read before adopting.

1. **Same-process reentrancy is allowed.** Two `FileLock` instances on the same path inside one Python process will both succeed — they share the same PID, so the lock can't tell them apart. **This is a cross-process lock, not a thread lock.** For in-process serialization, use `threading.Lock`. The 6th test in this repo demonstrates the cross-process guarantee with 8 real subprocesses.

2. **No blocking acquire / no timeout.** `acquire()` either succeeds immediately or raises `LockHeld`. If you need "wait up to N seconds," wrap it in your own retry loop, or use [`filelock`](https://pypi.org/project/filelock/).

3. **PID reuse on long-uptime systems.** If the recorded PID was recycled by an unrelated process between recording and check, `is_pid_alive` returns `True` for a totally unrelated process and `acquire()` refuses. The `identity` parameter (with `psutil`) fixes this; without `psutil` it's a real (if rare) failure mode.

4. **Windows-without-psutil falls back to `tasklist`.** This is a subprocess call (~tens of ms latency) and only checks PID existence — not identity. Install the `[psutil]` extra in production.

5. **NFS / network filesystems are NOT safe.** `O_EXCL` is documented as broken on NFSv2 and unreliable on some NFSv3 implementations. SMB/CIFS likewise. **Use this only on local filesystems.** For shared-storage locking, use a proper distributed lock service.

6. **`os.replace` atomicity has edge cases.** Atomic on POSIX. Atomic on NTFS since Vista. **Not guaranteed on FAT32, exFAT, or remote filesystems.** Same advice as above — local NTFS / ext4 / APFS only.

7. **File permissions matter.** Anyone with write access to the lock path can delete it and bypass the lock. This is an advisory lock, not a security mechanism. If you need privilege boundaries, use OS-level mechanisms (Linux capabilities, Windows ACLs, kernel `flock`).

8. **No deadlock detection.** If process A acquires lock X and waits on lock Y, while process B holds Y and waits on X, neither will ever release. This library can't help — design your acquisition order.

9. **Crash safety is partial.** If the holder process is killed (SIGKILL, power loss), the lock file stays on disk. The next acquirer detects the dead PID and takes over — **assuming the OS hasn't recycled that PID yet**. See limitation #3.

10. **The 6 tests in this repo are not exhaustive.** They cover acquire, release, takeover, idempotency, context manager, and the cross-process race. They do NOT cover: NFS, Windows without psutil under heavy PID reuse, signal handling during release, or behavior with read-only filesystems. Report bugs at [issues](https://github.com/aiton08255/racefree-filelock/issues).

---

## Alternatives — when NOT to use this

| Library | Use it instead of this when |
|---|---|
| [`filelock`](https://pypi.org/project/filelock/) | You need blocking acquire with timeout, OR you need real OS-level advisory locking (`fcntl`/`msvcrt`). Bigger, more mature, well-maintained. |
| [`portalocker`](https://pypi.org/project/portalocker/) | You need fine-grained shared/exclusive lock modes (multiple readers / single writer) on a file's contents, not on a lock-name. |
| `fcntl.flock` (stdlib, POSIX only) | You only need POSIX, you don't care about Windows, and you can tolerate the lock vanishing on `close()`. |
| `msvcrt.locking` (stdlib, Windows only) | Mirror of the above for Windows-only deployments. |
| Redis `SETNX` / etcd / ZooKeeper | Multiple machines need to coordinate. |
| `threading.Lock` / `asyncio.Lock` | Coordination is within a single Python process. |

**Why this library exists when `filelock` already does locking**: `filelock` uses fcntl/msvcrt and has timeout support, but it doesn't expose **who** holds the lock, and the lock vanishes if the holder's file handle closes unexpectedly. This library records the holder's PID (so you can log "PID 1234 is blocking us") and persists across handle-close events. Different tradeoffs for different jobs.

---

## API

### `FileLock(path, identity=None, register_atexit=True)`

- **`path`** — filesystem path of the PID file. Parent directory is created if missing.
- **`identity`** — optional substring; a recorded PID counts as alive only if its command line contains it. Requires `psutil` to take effect.
- **`register_atexit`** — when `True` (default), `release()` runs on interpreter shutdown via `atexit`. Disable if you manage your own shutdown.

### `lock.acquire() -> int`

Returns this process's PID. Raises `LockHeld(holder_pid, lock_path)` if a different live process holds the lock. Raises `OSError` on filesystem errors (permission denied, missing parent after a concurrent rmdir, etc).

### `lock.release() -> None`

Idempotent. Only deletes the file if it still records this process's PID — protects a successor that has already taken over via the stale-PID path.

### `is_pid_alive(pid, identity=None) -> bool`

Cross-platform PID liveness check. Returns `False` on any error — never raises. Exposed for callers that want the same semantics without taking a lock.

---

## Run the tests

```bash
git clone https://github.com/aiton08255/racefree-filelock
cd racefree-filelock
pip install -e ".[dev]"
python -m pytest tests/ -v
```

The key test is `test_exactly_one_winner_under_concurrent_race`: 8 real subprocesses race for the lock. Exactly one wins; the other 7 hit `LockHeld`. **This test fails** under any check-then-write implementation — the property is what the library exists to guarantee. If you fork this and modify `acquire()`, this is the test that catches regressions.

Current status: `6 passed in 3.40s` on Python 3.12 / Windows 11.

---

## Provenance

Built and battle-tested across 100+ continuous research cycles in a private long-running system. The race bug was found in an adversarial code-review round; the atomic fix shipped one round later. Since then, the lock has caught every double-spawn attempt by the host's Scheduled Task watchdog.

The private system this came from is not open-source. This file lock is published standalone because the underlying technique (atomic O_EXCL + tempfile takeover) is generally useful and most PID-file lock implementations get it wrong.

---

## License

MIT. See `LICENSE`.

## Contributing

Bug reports welcome — especially edge cases on platforms / filesystems I don't test (Linux with weird filesystems, macOS, BSD, NFS scenarios you can demonstrate are broken). Open an [issue](https://github.com/aiton08255/racefree-filelock/issues) with a reproducer.

For pull requests: keep the LOC count low. The whole point of this library is that it's small enough to read and verify in one sitting.
