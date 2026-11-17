"""Cached, capped, limit-aware wrapper around the subscription CLIs (`claude -p`, `codex exec`) for the Stage 11 pilots.

Ported from the earlier lab's phasef/llm.py (same cache key and cap design) with these changes:
  * every call, hit or miss, is appended to a per-call log with model requested, model returned, prompt hash, token counts,
    latency and the cache flag;
  * the model id the CLI reports is recorded and a mismatch with the requested id raises ModelMismatch (the row is withheld);
  * a usage or rate limit stops the run: LimitStop is raised, STOPPED.json is written, nothing is retried and no other
    model or account is tried;
  * no API key is read or used anywhere: the CLIs run under the existing subscription sign-in.
A miss reserves the persistent call counter BEFORE the subprocess starts, so a crash still counts and an interrupted run never repeats
a paid call (the response is cached by sha256(model, recipe, system, prompt) as soon as it returns).
"""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import threading
import time

CODEX_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
_LIMIT_RE = re.compile(r"usage limit|rate.?limit|quota|too many requests|\b429\b|overloaded|limit reached|limit will reset|resets? (at|in)", re.I)
_SHARED = {}
_SHARED_GUARD = threading.Lock()


class CallCapExceeded(RuntimeError):
    pass


class LimitStop(RuntimeError):
    """A subscription usage or rate limit (or an overload) stopped the run. Stop, record, report; never switch model or account."""


class ModelMismatch(RuntimeError):
    pass


class PromptLeak(RuntimeError):
    pass


_LEAK_RE = re.compile(r"answer_[0-9a-fA-F]|sharegpt_|ultrachat_")


def check_prompt(prompt, system=""):
    for text in (prompt, system):
        m = _LEAK_RE.search(text)
        if m:
            raise PromptLeak("prompt shows a real session id pattern near %r" % text[max(0, m.start() - 20):m.end() + 20])


def request_hash(model, recipe, system, prompt):
    h = hashlib.sha256()
    for part in (model, recipe, system, prompt):
        b = part.encode("utf-8")
        h.update(str(len(b)).encode() + b":" + b)
    return h.hexdigest()


def claude_runner(model, system, prompt, cwd, timeout=1800):
    cmd = ["claude", "-p", "--model", model, "--tools", "", "--strict-mcp-config", "--system-prompt", system,
           "--no-session-persistence", "--output-format", "json"]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    out = None
    try:
        out = json.loads(p.stdout)
    except ValueError:
        pass
    if p.returncode != 0 or out is None:
        text = (p.stderr or "")[-400:] + (p.stdout or "")[-400:]
        if _LIMIT_RE.search(text):
            raise LimitStop("claude stopped on a limit: " + text[-300:].replace("\n", " "))
        raise RuntimeError("claude exited %d: %s" % (p.returncode, text[-300:]))
    if out.get("is_error"):
        if _LIMIT_RE.search(str(out.get("result"))):
            raise LimitStop("claude reported a limit: " + str(out.get("result"))[:300])
        raise RuntimeError("claude reported is_error: " + str(out.get("result"))[:300])
    u = out.get("usage", {}) or {}
    return {"result": out.get("result", ""), "model_returned": sorted((out.get("modelUsage") or {}).keys()),
            "in_tokens": u.get("input_tokens", 0), "cache_creation": u.get("cache_creation_input_tokens", 0),
            "cache_read": u.get("cache_read_input_tokens", 0), "out_tokens": u.get("output_tokens", 0),
            "cost_usd": out.get("total_cost_usd"), "num_turns": out.get("num_turns")}


def codex_runner(model, system, prompt, cwd, effort="medium", timeout=900):
    """One item per call under the existing ChatGPT sign-in; no key, no pool, no fallback."""
    text = system.strip() + "\n\n" + prompt
    cmd = [CODEX_BIN, "exec", "-m", model, "-c", "model_reasoning_effort=" + effort, "-s", "read-only", "--ephemeral",
           "--skip-git-repo-check", "--json", "-"]
    p = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    msgs, usage, err = [], {}, ""
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            msgs.append(ev["item"].get("text", ""))
        elif ev.get("type") == "turn.completed":
            usage = ev.get("usage", {}) or {}
        elif ev.get("type") in ("error", "turn.failed"):
            err += json.dumps(ev)[:300]
    if p.returncode != 0 or not msgs:
        blob = err + (p.stderr or "")[-300:]
        if _LIMIT_RE.search(blob):
            raise LimitStop("codex stopped on a limit: " + blob[-300:].replace("\n", " "))
        raise RuntimeError("codex exited %d without an answer: %s" % (p.returncode, blob[-300:]))
    return {"result": msgs[-1], "model_returned": [], "in_tokens": usage.get("input_tokens", 0), "cache_creation": usage.get("cache_write_input_tokens", 0),
            "cache_read": usage.get("cached_input_tokens", 0), "out_tokens": usage.get("output_tokens", 0), "cost_usd": None, "num_turns": 1}


class LLM:
    def __init__(self, cache_dir, run_dir, cap, concurrency=2, recipe="v1", cwd=None, runner=None, backend="claude"):
        self.cache_dir, self.run_dir, self.cap, self.recipe, self.backend = cache_dir, run_dir, cap, recipe, backend
        self.cwd = cwd
        base = claude_runner if backend == "claude" else codex_runner
        self.runner = runner or (lambda m, s, p: base(m, s, p, cwd))
        with _SHARED_GUARD:
            sh = _SHARED.setdefault(os.path.abspath(cache_dir), {"sem": threading.Semaphore(concurrency), "lock": threading.Lock(), "keys": {}})
        self._sem, self._lock, self._key_locks = sh["sem"], sh["lock"], sh["keys"]
        os.makedirs(cache_dir, exist_ok=True)
        os.makedirs(run_dir, exist_ok=True)
        self._counter = os.path.join(run_dir, "calls.count")
        self._calllog = os.path.join(run_dir, "calls.jsonl")
        self._stopfile = os.path.join(run_dir, "STOPPED.json")

    def calls_used(self):
        try:
            with open(self._counter) as fh:
                return int(fh.read().strip() or 0)
        except FileNotFoundError:
            return 0

    def stopped(self):
        return os.path.exists(self._stopfile)

    def _reserve(self):
        with self._lock:
            with open(self._counter + ".lock", "w") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                n = self.calls_used()
                if n >= self.cap:
                    raise CallCapExceeded("call cap %d reached" % self.cap)
                with open(self._counter, "w") as fh:
                    fh.write(str(n + 1))
                return n + 1

    def _log(self, row):
        with self._lock, open(self._calllog, "a") as fh:
            fh.write(json.dumps(row) + "\n")

    def call(self, prompt, system, model, tag, qid, stage):
        check_prompt(prompt, system)
        key = request_hash(model, self.recipe, system, prompt)
        path = os.path.join(self.cache_dir, key + ".json")

        def hit():
            with open(path) as fh:
                rec = json.load(fh)
            rec["cached"] = True
            self._log({"t": round(time.time(), 1), "stage": stage, "tag": tag, "qid": qid, "model": model, "key": key, "cached": True})
            return rec

        if os.path.exists(path):
            return hit()
        if self.stopped():
            raise LimitStop("run is stopped (STOPPED.json present); nothing more is called")
        with self._lock:
            klock = self._key_locks.setdefault(key, threading.Lock())
        with klock, self._sem:
            if os.path.exists(path):
                return hit()
            if self.stopped():
                raise LimitStop("run is stopped (STOPPED.json present)")
            n = self._reserve()
            t0 = time.time()
            try:
                out = self.runner(model, system, prompt)
            except LimitStop as e:
                with open(self._stopfile, "w") as fh:
                    json.dump({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "reason": str(e)[:400], "call_number": n, "key": key,
                               "qid": qid, "stage": stage, "model": model}, fh)
                raise
            secs = round(time.time() - t0, 2)
            if self.backend == "claude" and model not in out["model_returned"]:
                row = {"t": round(time.time(), 1), "stage": stage, "tag": tag, "qid": qid, "model": model, "key": key, "cached": False,
                       "model_returned": out["model_returned"], "mismatch": True}
                self._log(row)
                raise ModelMismatch("requested %s, CLI reports %s" % (model, out["model_returned"]))
            rec = dict(out, key=key, model=model, recipe=self.recipe, tag=tag, qid=qid, stage=stage, seconds=secs, prompt_chars=len(prompt),
                       prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(), call_number=n, backend=self.backend)
            tmp = path + ".tmp%d" % threading.get_ident()
            with open(tmp, "w") as fh:
                json.dump(rec, fh)
            os.replace(tmp, path)
            self._log({"t": round(time.time(), 1), "stage": stage, "tag": tag, "qid": qid, "model": model, "key": key, "cached": False,
                       "model_returned": out["model_returned"], "prompt_sha256": rec["prompt_sha256"], "in_tokens": out["in_tokens"],
                       "cache_creation": out["cache_creation"], "cache_read": out["cache_read"], "out_tokens": out["out_tokens"],
                       "seconds": secs, "call_number": n})
        rec["cached"] = False
        return rec
