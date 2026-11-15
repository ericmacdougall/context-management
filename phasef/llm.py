"""Cached, rate-limited, call-capped wrapper around `claude -p` (user's subscription; no API key is used).

Every call is keyed by sha256(model, recipe, system, prompt). A hit costs nothing and does not count against the
cap. A miss increments a persistent counter BEFORE the subprocess starts (so a crash or failure still counts) and
is refused once the counter reaches the cap. Concurrency is bounded by a semaphore. Usage goes to a JSONL ledger.
"""
import fcntl
import hashlib
import json
import os
import subprocess
import threading
import time

DEFAULT_CAP = 120
DEFAULT_CONCURRENCY = 3


_SHARED = {}  # per cache dir: lock, semaphore and per-key locks shared by every LLM instance in this process
_SHARED_GUARD = threading.Lock()


class CallCapExceeded(RuntimeError):
    pass


def request_hash(model, recipe, system, prompt):
    h = hashlib.sha256()
    for part in (model, recipe, system, prompt):
        b = part.encode("utf-8")
        h.update(str(len(b)).encode() + b":" + b)
    return h.hexdigest()


def claude_cli_runner(model, system, prompt, timeout=1500, cwd=None):
    cmd = ["claude", "-p", "--model", model, "--tools", "", "--strict-mcp-config",
           "--system-prompt", system, "--no-session-persistence", "--output-format", "json"]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if p.returncode != 0:
        raise RuntimeError("claude exited %d: %s" % (p.returncode, p.stderr[-400:]))
    return json.loads(p.stdout)


class LLM:
    def __init__(self, cache_dir, ledger_path, cap=DEFAULT_CAP, concurrency=DEFAULT_CONCURRENCY,
                 recipe="v1", runner=None, cwd=None):
        self.cache_dir, self.ledger_path = cache_dir, ledger_path
        self.cap, self.recipe = cap, recipe
        # Run from a neutral, non-git directory: the user's Stop hook otherwise reacts to the caller's dirty checkout and
        # injects extra turns and hook chatter into the answer (observed on the first 10 ORACLE calls).
        self.runner = runner or (lambda m, s, p: claude_cli_runner(m, s, p, cwd=cwd))
        with _SHARED_GUARD:
            sh = _SHARED.setdefault(os.path.abspath(cache_dir), {"sem": threading.Semaphore(concurrency),
                                                                  "lock": threading.Lock(), "keys": {}})
        self._sem, self._lock, self._key_locks = sh["sem"], sh["lock"], sh["keys"]
        os.makedirs(cache_dir, exist_ok=True)
        self._counter = os.path.join(cache_dir, "calls.count")

    def calls_used(self):
        try:
            with open(self._counter) as fh:
                return int(fh.read().strip() or 0)
        except FileNotFoundError:
            return 0

    def _reserve(self):
        with self._lock:
            with open(self._counter + ".lock", "w") as lk:  # cross-process exclusion
                fcntl.flock(lk, fcntl.LOCK_EX)
                n = self.calls_used()
                if n >= self.cap:
                    raise CallCapExceeded("call cap %d reached" % self.cap)
                with open(self._counter, "w") as fh:
                    fh.write(str(n + 1))
                return n + 1

    def _path(self, key):
        return os.path.join(self.cache_dir, key + ".json")

    def call(self, prompt, system, model, arm, qid, stage):
        key = request_hash(model, self.recipe, system, prompt)
        path = self._path(key)
        if os.path.exists(path):
            with open(path) as fh:
                rec = json.load(fh)
            rec["cached"] = True
            return rec
        with self._lock:
            klock = self._key_locks.setdefault(key, threading.Lock())
        with klock, self._sem:
            if os.path.exists(path):  # another thread finished the same request
                with open(path) as fh:
                    rec = json.load(fh)
                rec["cached"] = True
                return rec
            self._reserve()
            t0 = time.time()
            out = self.runner(model, system, prompt)
            secs = time.time() - t0
            if out.get("is_error"):
                raise RuntimeError("claude reported is_error: %s" % str(out.get("result"))[:300])
            u = out.get("usage", {}) or {}
            rec = {"key": key, "model": model, "recipe": self.recipe, "arm": arm, "qid": qid, "stage": stage,
                   "result": out.get("result", ""), "is_error": bool(out.get("is_error", False)),
                   "in_tokens": u.get("input_tokens", 0), "cache_creation": u.get("cache_creation_input_tokens", 0),
                   "cache_read": u.get("cache_read_input_tokens", 0), "out_tokens": u.get("output_tokens", 0),
                   "cost_usd": out.get("total_cost_usd"), "seconds": round(secs, 2), "prompt_chars": len(prompt)}
            tmp = path + ".tmp%d" % threading.get_ident()
            with open(tmp, "w") as fh:
                json.dump(rec, fh)
            os.replace(tmp, path)
            with self._lock, open(self.ledger_path, "a") as fh:
                fh.write(json.dumps({k: rec[k] for k in ("arm", "qid", "stage", "model", "in_tokens", "cache_creation",
                                                         "cache_read", "out_tokens", "cost_usd", "seconds", "key")}) + "\n")
        rec["cached"] = False
        return rec
