"""Secrets come from configuration, never from source: a 0600 file (or an environment variable naming one). No default value exists."""
from __future__ import annotations

import os
import stat


class SecretConfigError(Exception):
    pass


def load_secret(path: str) -> bytes:
    """Read a secret from a regular file that only its owner can read. Refuses a missing, empty, group/other-readable or symlinked file."""
    try:
        st = os.lstat(path)
    except OSError as e:
        raise SecretConfigError("secret file unreadable: %s" % type(e).__name__)
    if not stat.S_ISREG(st.st_mode):
        raise SecretConfigError("secret file must be a regular file")
    if st.st_mode & 0o077:
        raise SecretConfigError("secret file must be mode 0600")
    data = open(path, "rb").read().strip()
    if len(data) < 16:
        raise SecretConfigError("secret too short (need at least 16 bytes)")
    return data


def secret_from_env(var: str = "CTXRT_SECRET_FILE") -> bytes:
    p = os.environ.get(var)
    if not p:
        raise SecretConfigError("%s is not set" % var)
    return load_secret(p)
