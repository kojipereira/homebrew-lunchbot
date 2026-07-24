"""Centralized filesystem locations + logging setup.

Everything that touches config/state/log paths imports from here so the
wizard, the daily run, and doctor never disagree about where things live.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

HOME = Path.home()

# XDG-ish layout so uninstall is a clean rm and nothing squats in $HOME.
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "lunchbot"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local" / "state")) / "lunchbot"
DATA_DIR = HOME / ".local" / "share" / "lunchbot"

CONFIG_PATH = CONFIG_DIR / "config.toml"
STATE_PATH = STATE_DIR / "state.json"
LOG_PATH = HOME / "Library" / "Logs" / "lunchbot.log"

# Legacy single-file install (the original author's ~/lunchbot/).
LEGACY_DIR = HOME / "lunchbot"
LEGACY_CONFIG = LEGACY_DIR / "config.yaml"
LEGACY_STATE = LEGACY_DIR / "state.json"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, STATE_DIR, LOG_PATH.parent):
        d.mkdir(parents=True, exist_ok=True)


_LOGGING_READY = False


def setup_logging() -> None:
    global _LOGGING_READY
    if _LOGGING_READY:
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # Console shows only warnings/errors so interactive commands stay clean;
    # full INFO detail always goes to the file (see `lunchbot logs`).
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    root.addHandler(console)
    _LOGGING_READY = True
