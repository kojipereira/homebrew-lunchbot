"""Single-instance guards for the two GUI processes.

The menu-bar app has three legitimate starting points — the com.lunchbot.gui
LaunchAgent at login, a double-click on Lunchbot.app, and `lunchbot gui` — and a
second copy would put a second sandwich in the menu bar. Same for the
preferences window, which is spawned as its own process every time.

An advisory `flock` on a file under the state dir is the whole mechanism. The
kernel drops the lock when the owning process exits *however* it exits (quit,
crash, SIGKILL, logout), so there is no stale lock to clean up and no pid to
babysit. The pid is written into the file anyway, purely so a losing process can
bring the winner's window forward.

Stdlib only, so this stays importable on the plain interpreter the ordering path
uses.
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path

from . import paths


def lock_path(name: str) -> Path:
    return paths.STATE_DIR / f"{name}.lock"


class InstanceLock:
    """Own the name `name` for as long as this process lives.

    Keep a reference for the lifetime of the process: the lock is released when
    the file object is garbage-collected, so a dropped reference silently opens
    the door to a second instance.
    """

    def __init__(self, name: str):
        self.name = name
        self.path = lock_path(name)
        self._fh = None

    def acquire(self) -> bool:
        """True if we now hold the lock, False if a live process already does.

        A filesystem problem counts as "held by nobody" — a broken state dir
        must never be the reason the menu-bar app refuses to start.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+")  # noqa: SIM115 — held for the process lifetime
        except OSError as e:
            logging.info("instance lock %s unavailable (%s); starting anyway",
                         self.path, e)
            return True
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh
        return True

    def owner_pid(self) -> int | None:
        """Pid recorded in the lock file — the live owner when acquire() failed."""
        try:
            return int(self.path.read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return None

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover — closing drops the lock regardless
            pass
        finally:
            self._fh.close()
            self._fh = None


def is_held(name: str) -> bool:
    """True when some live process holds `name`. For reporting (doctor) only —
    a caller that wants the name should acquire() it and keep the lock."""
    probe = InstanceLock(name)
    if not probe.acquire():
        return True
    probe.release()
    return False
