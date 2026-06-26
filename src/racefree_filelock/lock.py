"""Atomic cross-process file lock.

Solves the check-then-write race that plagues naive PID-file locks:

    # BROKEN — both processes can read "no file" and both write their PID.
    if not lock.exists():
        lock.write_text(str(os.getpid()))

Atomic ``os.open(O_CREAT|O_EXCL|O_WRONLY)`` guarantees exactly one winner
per race. Stale-holder takeover uses ``tempfile + os.replace`` — atomic
on POSIX, atomic-since-Vista on Windows — to avoid the same race during
recovery.
"""

from __future__ import annotations

import atexit
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Union


class LockHeld(Exception):
    """Raised when another live process holds the lock.

    The attribute ``holder_pid`` carries the PID currently recorded in
    the lock file, so callers can log or report it.
    """

    def __init__(self, holder_pid: int, lock_path: Path) -> None:
        super().__init__(f"lock at {lock_path} held by PID {holder_pid}")
        self.holder_pid = holder_pid
        self.lock_path = lock_path


def is_pid_alive(pid: int, identity: Optional[str] = None) -> bool:
    """Cross-platform PID liveness check.

    Args:
        pid: PID to check. Non-positive PIDs return False.
        identity: If given, the process's command line must contain this
            substring to count as alive. Lets you distinguish a stale
            PID re-used by an unrelated process from a real lock holder.
            Requires ``psutil`` to be effective; without psutil the
            check degrades to existence-only and a warning is silent.

    Returns False on any error — never raises.
    """
    if pid <= 0:
        return False
    try:
        try:
            import psutil
        except ImportError:
            psutil = None  # type: ignore[assignment]

        if psutil is not None:
            try:
                p = psutil.Process(pid)
                if identity is None:
                    return True
                cmd = " ".join(p.cmdline()) if p.cmdline() else ""
                return identity in cmd
            except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
                return False

        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return f" {pid} " in result.stdout

        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


class FileLock:
    """Atomic cross-process file lock.

    Usage as context manager (recommended):

        with FileLock("/tmp/myapp.pid") as lock:
            ...  # exclusive section

    Or manually:

        lock = FileLock("/tmp/myapp.pid")
        try:
            lock.acquire()  # raises LockHeld if another live PID holds it
            ...
        finally:
            lock.release()

    Args:
        path: Filesystem path of the lock/PID file. Parent directory is
            created if missing.
        identity: Optional substring that must appear in a holder's
            command line for it to count as alive. Use when the lock
            file lives in a shared directory and a stale PID might be
            re-used by an unrelated process. Requires ``psutil``.
        register_atexit: When True (default), release() is registered
            with atexit so the lock cleans up on normal interpreter
            shutdown. Disable if you manage shutdown yourself.
    """

    def __init__(
        self,
        path: Union[str, os.PathLike],
        identity: Optional[str] = None,
        register_atexit: bool = True,
    ) -> None:
        self.path = Path(path)
        self.identity = identity
        self._register_atexit = register_atexit
        self._owner_pid: Optional[int] = None

    def acquire(self) -> int:
        """Acquire the lock.

        Returns this process's PID on success. Raises ``LockHeld`` if
        another live process holds the lock. Raises ``OSError`` on
        filesystem errors (permissions, missing parent after concurrent
        rmdir, etc).
        """
        my_pid = os.getpid()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        try:
            fd = os.open(
                str(self.path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
            try:
                os.write(fd, str(my_pid).encode("utf-8"))
            finally:
                os.close(fd)
            self._owner_pid = my_pid
            if self._register_atexit:
                atexit.register(self._atexit_release)
            return my_pid
        except FileExistsError:
            pass

        try:
            existing = int(self.path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            existing = -1

        if (
            existing > 0
            and existing != my_pid
            and is_pid_alive(existing, self.identity)
        ):
            raise LockHeld(existing, self.path)

        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(self.path.parent),
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tf:
            tf.write(str(my_pid))
            tmp_path = tf.name
        os.replace(tmp_path, str(self.path))

        try:
            recorded_after = int(self.path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            recorded_after = -1
        if recorded_after != my_pid:
            if recorded_after > 0 and is_pid_alive(recorded_after, self.identity):
                raise LockHeld(recorded_after, self.path)

        self._owner_pid = my_pid
        if self._register_atexit:
            atexit.register(self._atexit_release)
        return my_pid

    def release(self) -> None:
        """Release the lock. Idempotent. Only deletes the file if it
        still records this process's PID — a successor that took over
        via stale-PID rules must not have its lock yanked."""
        if self._owner_pid is None:
            return
        try:
            if not self.path.exists():
                self._owner_pid = None
                return
            recorded = int(self.path.read_text(encoding="utf-8").strip())
            if recorded == self._owner_pid:
                self.path.unlink()
        except (ValueError, OSError):
            pass
        finally:
            self._owner_pid = None

    def _atexit_release(self) -> None:
        try:
            self.release()
        except Exception:
            pass

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
