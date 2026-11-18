"""Prevention at source (runkit/GATE-DESIGN.md 7.6; review F scanner half, option 2): a per-process registry of the
secret values this process created or loaded, and the one writer of results/ and logs/, which refuses to write
content holding a registered value in any representation and view runkit/leak_scan.py uses.

  register(value, label, complete=False)  add a value (str or bytes). label is static text naming the kind of secret,
                 never derived from the value. complete=True matches only the whole value (for values that begin with
                 a public part, such as an encoded API key or "id:key"); otherwise any 16 consecutive characters.
  find(data)     the labels of registered values present in data (str or bytes), in any view.
  write(path, data, mode=0o644)  write atomically (a temporary file beside it, fsync, rename), or raise
                 SecretInOutput (naming labels only) and write nothing; SinkError when the content cannot be searched
                 completely (a damaged or oversized gzip stream).
  RedactingFilter  a logging filter: a record whose message or exception text holds a registered value is replaced
                 whole by "<redacted: holds a registered secret (label, ...)>".
Limits: a process knows only the values it registered (hand-off H-1 to H-5 wires the calls into the stream's
creation sites and writers; tests/test_result_sink_fence.py fences both). What leak_scan's LIMITS v4 leave out, this
sink leaves out. Nothing here prints, logs or returns a value."""
import importlib.util
import logging
import os
import pathlib
import threading

_LOCK = threading.Lock()
_REG = []                                     # [(label, value bytes, complete)]
_LS = None


class SinkError(Exception):
    pass


class SecretInOutput(SinkError):
    pass


def _scanner():
    """runkit/leak_scan.py of this tree, loaded by path once (it also runs standalone on the host)."""
    global _LS
    if _LS is None:
        p = pathlib.Path(__file__).resolve().parents[1] / "runkit" / "leak_scan.py"
        spec = importlib.util.spec_from_file_location("wp_secret_sink_leak_scan", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _LS = m
    return _LS


def _bytes(x):
    return x.encode() if isinstance(x, str) else bytes(x)


def register(value, label, complete=False):
    if not isinstance(label, str) or not label.strip():
        raise ValueError("a label is static, non-empty text")
    v = _bytes(value)
    if not v:
        raise ValueError("an empty value cannot be registered")
    if find(label) or _hits_one(label.encode(), v, complete):
        raise ValueError("a label must not hold a secret value")
    with _LOCK:
        if not any(x[1] == v and x[2] == complete for x in _REG):
            _REG.append((label, v, complete))


def labels():
    with _LOCK:
        return sorted({x[0] for x in _REG})


def _index(items):
    ls = _scanner()
    return ls, ls.Index({"file": [(str(i), v, "full" if c else "frag") for i, (_, v, c) in enumerate(items)],
                         "keystore": []})


def _hits_one(data, v, complete):
    ls, idx = _index([("", v, complete)])
    try:
        return any(idx.find(x) for x in ls.views(data))
    except ls.ScanError:
        return True


def find(data):
    """Labels of registered values in data. Raises SinkError when data cannot be searched completely."""
    with _LOCK:
        items = list(_REG)
    if not items:
        return []
    ls, idx = _index(items)
    names = {iid: items[int(src)][0] for iid, src, _ in idx.ids}
    found = set()
    try:
        for x in ls.views(_bytes(data)):
            found |= {names[iid] for _, iid in idx.find(x)}
    except ls.ScanError as e:
        raise SinkError(f"content cannot be searched completely ({e}); nothing written") from None
    return sorted(found)


def write(path, data, mode=0o644):
    path = pathlib.Path(path)
    raw = _bytes(data)
    hit = find(raw)
    if hit:
        raise SecretInOutput(f"refused to write {path.name}: it holds a registered secret ({', '.join(hit)})")
    tmp = path.with_name(f".{path.name}.sink-{os.getpid()}-{threading.get_ident()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)                   # the umask does not apply to the stated mode
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


class RedactingFilter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:                    # a record whose arguments do not format: kept out whole
            msg = str(record.msg)
        exc = logging.Formatter().formatException(record.exc_info) if record.exc_info else (record.exc_text or "")
        try:
            hit = find(msg + "\n" + exc)
        except SinkError:
            hit = ["content that cannot be searched"]
        if hit:
            record.msg, record.args = f"<redacted: holds a registered secret ({', '.join(hit)})>", ()
            record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True
