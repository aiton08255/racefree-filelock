"""Atomic cross-process file lock with safe stale-holder takeover.

Public API:
    FileLock      — the lock object
    LockHeld      — raised by acquire() when another live process holds it
    is_pid_alive  — cross-platform liveness check (exposed for callers
                    that want the same semantics without taking a lock)
"""

from racefree_filelock.lock import FileLock, LockHeld, is_pid_alive

__all__ = ["FileLock", "LockHeld", "is_pid_alive"]
__version__ = "0.1.0"
