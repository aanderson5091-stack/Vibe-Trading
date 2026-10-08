"""Start / Stop for the loop, driven from the dashboard.

Two small files in ~/.jev-loop/ (or $JEV_LOOP_HOME):

  control.json   {"state": "RUNNING"} or {"state": "PAUSED"}. The loop reads
                 it before every order, so a press on the dashboard takes
                 effect on the next tick, and it survives a page refresh or
                 a restart of either process. No file means RUNNING.
  control.token  a random per-install secret, made the first time the
                 dashboard server starts. The dashboard sends it with every
                 Start/Stop press; anything without it is refused.

PAUSED stops new orders only. Market data, Jev decisions, the log and the
dashboard all keep updating. It does not close a position: the KILL switch
(the hard risk limits) does that, and still does while paused.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

RUNNING = "RUNNING"
PAUSED = "PAUSED"
STATES = (RUNNING, PAUSED)


def _state_file(home: Path) -> Path:
    return Path(home) / "control.json"


def read_state(home: Path, attempts: int = 5, wait_s: float = 0.02) -> str:
    """RUNNING or PAUSED. A missing file is RUNNING. A file that can't be
    read after a few tries (a Windows lock that won't clear, or garbage) is
    treated as PAUSED: when in doubt, don't place the order."""
    path = _state_file(home)
    for _ in range(attempts):
        try:
            state = json.loads(path.read_bytes()).get("state")
            return state if state in STATES else PAUSED
        except FileNotFoundError:
            return RUNNING
        except (PermissionError, ValueError, AttributeError):
            time.sleep(wait_s)
    return PAUSED


def is_paused(home: Path) -> bool:
    return read_state(home) == PAUSED


def write_state(home: Path, state: str, attempts: int = 8) -> None:
    """Atomic swap with a short retry, so a reader never sees half a file
    and a Windows reader holding it open only delays the swap."""
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}, got {state!r}")
    path = _state_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"state": state, "changed_at": time.time()}))
    wait = 0.01
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == attempts - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(wait)
            wait *= 2


def load_or_create_token(home: Path) -> str:
    """The per-install token. Created once with owner-only permissions
    where the OS supports them (Mac, Linux); on Windows the file sits in
    the user's own profile folder, which other users can't read."""
    path = Path(home) / "control.token"
    try:
        token = path.read_text().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token
