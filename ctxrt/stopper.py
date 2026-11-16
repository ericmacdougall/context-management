"""The audited out-of-band stop for a child process: the harness's own `ah.py` stop command (SIGTERM, then SIGKILL after a grace period, ownership
checked, logged). No signal is sent from this code."""
from __future__ import annotations

import os
import subprocess
import sys

AH = os.path.expanduser("~/.claude/hooks/ah.py")


def stop_pid(pid: int, reason: str, grace: float = 0.3, timeout: float = 60.0):
    p = subprocess.run([sys.executable, "-I", AH, "kill", "--pid", str(pid), "--reason", reason, "--grace", str(grace)], capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "")[-300:], (p.stderr or "")[-300:]
