"""Decision service: the only public way to change or read decision heads.

- submit(session, body): the session is verified by identity (pol.directory.SessionAuthority) and the principal read from the
  Directory. Tenant, principal and channel are STAMPED from the session; a body that names any of them, or an `id`, is refused
  `identity_in_body` (M1). Event ids are namespaced `<principal>/<client_id>`, so one principal cannot take another's id (H6). A body
  carries value TEXT, never a value_ref: the text goes to the value vault, which assigns the reference; the decision core and its event
  log only ever hold references (H7), so erasing a subject's values from the vault erases them from what the core can show.
- Every refusal (by the service or by the core) is written to the audit log with its code (the experiments count them).
- Reads are viewer-scoped only: explain(session, req) and table(session). A value, proposal or contested candidate is shown only when
  the enforcer allows `read` of its label for the viewer; otherwise it is left out, and the `hidden_candidates` flag exists only when the
  policy opts in to existence-only notices (conflict_notice == "existence_only"). There is no unscoped read on the public surface.
- Engines: MemoryEngine here, pol.pgcore.PgEngine for Postgres. Both run pol._core.reduce; the engine owns atomicity (a lock here, a
  transaction with an explicit isolation level there).

Value encryption is an INTERFACE only (_ValueCipher, implemented by the write path): in the composed system the write path's KeyStore owns per-subject keys, so this
module does not ship a cipher. Without one the vault stores plaintext in its own table/dict and erasure is deletion."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from typing import Dict, List, Mapping, Optional, Protocol

from . import _core
from .enforce import Enforcer, PolicyDenied
from .labels import Label, LabelError

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
IDENTITY_KEYS = frozenset(["tenant", "principal", "channel", "id"])
BODY_KEYS = frozenset(["type", "client_id", "idem", "req", "value", "label", "based_on", "proposal", "proposal_hash", "pending", "verb",
                       "choose", "target", "role"])


class _ValueCipher(Protocol):           # interface only (see module docstring)
    def seal(self, tenant: str, subjects: List[str], plaintext: bytes) -> bytes: ...

    def open(self, tenant: str, sealed: bytes) -> bytes: ...

    def shred(self, tenant: str, subject: str) -> None: ...


def _value_ref(key: bytes, tenant: str, principal: str, idem: str, text: str, label: Label) -> str:
    """Deterministic per (tenant, principal, idempotency key, value, label), so a retried submission carries the same reference and is
    recognised as a duplicate, and keyed (HMAC), so a reference left in the append-only log after erasure is not a plain hash of the value."""
    msg = json.dumps([tenant, principal, idem, text, label.to_dict()], sort_keys=True, separators=(",", ":")).encode()
    return "val-" + hmac.new(key, msg, hashlib.sha256).hexdigest()[:40]


class Vault:
    """Referenced value storage. Refs are assigned here. Each value is tagged with the subjects of its label, so erasure of a subject
    covers every value ABOUT that subject, whoever wrote it."""

    def __init__(self):
        self._vals: Dict[str, dict] = {}
        self._key = secrets.token_bytes(32)
        self._lock = threading.Lock()

    def _ref(self, tenant: str, principal: str, idem: str, text: str, label: Label) -> str:
        return _value_ref(self._key, tenant, principal, idem, text, label)

    def _put(self, tenant: str, ref: str, text: str, label: Label) -> bool:
        """Store under `ref` unless present; True when this call created it."""
        with self._lock:
            if ref in self._vals:
                return False
            self._vals[ref] = {"tenant": tenant, "text": text, "label": label, "subjects": frozenset(label.subjects)}
        return True

    def _get(self, tenant: str, ref) -> Optional[dict]:
        with self._lock:
            v = self._vals.get(ref) if isinstance(ref, str) else None
            return dict(v) if v is not None and v["tenant"] == tenant else None

    def _delete(self, tenant: str, ref: str) -> bool:
        with self._lock:
            v = self._vals.get(ref)
            if v is None or v["tenant"] != tenant:
                return False
            del self._vals[ref]
            return True

    def _refs_about(self, tenant: str, subject: str) -> List[str]:
        with self._lock:
            return sorted(r for r, v in self._vals.items() if v["tenant"] == tenant and subject in v["subjects"])


class MemoryEngine:
    """One tenant's decision state in memory; submit is atomic under a lock."""

    def __init__(self, config: Mapping):
        self._st = _core.MemoryState(config)
        self.tenant = self._st.tenant
        self._lock = threading.Lock()
        self._log: List[dict] = []

    def _submit(self, ev: dict):
        with self._lock:
            code, orig, delta = _core.reduce(self._st, ev)
            self._st.apply_delta(delta)
            if code not in _core.REFUSED:
                self._log.append(ev)
            return code, orig

    def _participant(self, pid: str, req: str) -> bool:
        with self._lock:
            s = self._st
            return s.req_exists(req) and (s.has_authority(req, pid) or s.is_proposer(req, pid) or s.is_admin(pid))

    def _is_admin(self, pid: str) -> bool:
        with self._lock:
            return self._st.is_admin(pid)

    def _reqs(self) -> List[str]:
        with self._lock:
            return sorted(self._st.heads)

    def _head(self, req: str) -> Optional[dict]:
        with self._lock:
            h = self._st.heads.get(req)
            return None if h is None else _core.copy.deepcopy(h)

    def _open_items(self, req: str) -> List[dict]:
        """Open items of `req` through the per-author open index (cost bounded by the open caps, not by history)."""
        with self._lock:
            st = self._st
            ids = [i for (r, _a), s in st.open_by.items() if r == req for i in s]
            return [dict(st.props[req][i]) for i in ids]


class DecisionService:
    def __init__(self, enforcer: Enforcer, engines: Mapping[str, object], vault):
        if not isinstance(enforcer, Enforcer) or not isinstance(engines, Mapping) or not engines or vault is None:
            raise TypeError("DecisionService(enforcer, engines, vault)")
        self._enf, self._engines, self._vault = enforcer, dict(engines), vault

    # ------------------------------------------------------------------ writes
    def _refuse(self, pid, code, body, orig=None):
        t = body.get("type") if isinstance(body, dict) else None
        self._enf._audit.add({"action": "decision", "principal": pid, "allow": False, "reason": code, "type": t,
                              "req": body.get("req") if isinstance(body, dict) else None, "policy_version": self._enf._ver()})
        return {"code": code, "original": orig, "id": None, "value_ref": None}

    def submit(self, session, body) -> dict:
        try:
            p = self._enf.principal_of(session)
        except PolicyDenied as e:
            return {"code": "unauthenticated:" + e.decision.reason, "original": None, "id": None, "value_ref": None}
        if not isinstance(body, dict):
            return self._refuse(p.id, "bad_event", {})
        if IDENTITY_KEYS & set(body):
            return self._refuse(p.id, "identity_in_body", body)
        if set(body) - BODY_KEYS:
            return self._refuse(p.id, "bad_event", body)
        cid = body.get("client_id")
        if not isinstance(cid, str) or not CLIENT_ID_RE.match(cid):
            return self._refuse(p.id, "bad_event", body)
        eng = self._engines.get(session.tenant)
        if eng is None:
            return self._refuse(p.id, "unknown_tenant", body)
        ev = {k: v for k, v in body.items() if k not in ("client_id", "value")}
        ev.update(id=p.id + "/" + cid, tenant=session.tenant, principal=p.id, channel=session.channel)
        ref, created = None, False
        if "value" in body:
            if not isinstance(body["value"], str) or "label" not in body:
                return self._refuse(p.id, "bad_event", body)
            try:
                lab = Label.from_dict(body["label"])
            except (LabelError, TypeError, ValueError, AttributeError):
                return self._refuse(p.id, "bad_event", body)
            ref = self._vault._ref(session.tenant, p.id, str(body.get("idem")), body["value"], lab)
            created = self._vault._put(session.tenant, ref, body["value"], lab)
            ev["value_ref"] = ref
        code, orig = eng._submit(ev)
        if code in _core.REFUSED:
            if ref is not None and created:
                self._vault._delete(session.tenant, ref)
            out = self._refuse(p.id, code, body, orig)
            out["id"] = ev["id"]
            return out
        self._enf._audit.add({"action": "decision", "principal": p.id, "allow": True, "reason": code, "type": ev.get("type"), "req": ev.get("req"),
                              "policy_version": self._enf._ver()})
        return {"code": code, "original": orig, "id": ev["id"], "value_ref": ref}

    def erase_subject(self, session, subject: str) -> dict:
        """Erase every vault value about `subject` (whoever said it). A value under a recorded legal hold is queued, not erased."""
        p = self._enf.principal_of(session)
        eng = self._engines.get(session.tenant)
        if eng is None or not eng._is_admin(p.id):
            self._refuse(p.id, "unauthorized", {"type": "erase"})
            return {"erased": 0, "queued": 0, "denied": 0, "refused": "unauthorized"}
        out = {"erased": 0, "queued": 0, "denied": 0, "refused": None}
        for ref in self._vault._refs_about(session.tenant, subject):
            v = self._vault._get(session.tenant, ref)
            if v is None:
                continue
            try:
                self._enf._check(session, "erase", [v["label"]], {})
            except PolicyDenied as e:
                out["queued" if e.decision.reason == "erasure_queued" else "denied"] += 1
                continue
            self._vault._delete(session.tenant, ref)
            out["erased"] += 1
        return out

    # ------------------------------------------------------------------ viewer-scoped reads
    def _readable(self, session, label) -> bool:
        if label is None:
            return True
        try:
            lab = label if isinstance(label, Label) else Label.from_dict(label)
            self._enf._check(session, "read", [lab], {})
            return True
        except (PolicyDenied, LabelError, TypeError, ValueError):
            return False

    def _value(self, session, ref):
        v = self._vault._get(session.tenant, ref) if ref is not None else None
        if ref is not None and v is None:
            return {"ref": ref, "text": None, "erased": True}
        if v is None:
            return None
        return {"ref": ref, "text": v["text"], "erased": False} if self._readable(session, v["label"]) else None

    def explain(self, session, req: str) -> Optional[dict]:
        """The head of `req` as THIS viewer may see it, or None (not a participant, unknown req, invalid session: indistinguishable)."""
        try:
            p = self._enf.principal_of(session)
        except PolicyDenied:
            return None
        eng = self._engines.get(session.tenant)
        if eng is None or not isinstance(req, str) or not eng._participant(p.id, req):
            return None
        h = eng._head(req)
        notice = self._enf.policy().conflict_notice == "existence_only"
        hidden = False
        out = {"req": req, "version": h["version"], "status": h["status"], "value": None, "value_by": None, "value_version": h["value_version"]}
        if h["value_ref"] is not None:
            if self._readable(session, h["value_label"]):
                out["value"], out["value_by"] = self._value(session, h["value_ref"]), h["value_by"]
            else:
                hidden = True
        cands = []
        if h["contested"] is not None:
            for c in h["contested"]["candidates"]:
                if self._readable(session, c["label"]):
                    eff = c["effect"]
                    cands.append({"event": c["event"], "by": c["by"], "verb": c["verb"], "effect": eff[0],
                                  "value": self._value(session, eff[1]) if eff[0] == "set" else None})
                else:
                    hidden = True
        out["candidates"] = cands
        items = []
        for d in eng._open_items(req):
            if self._readable(session, d["label"]):
                items.append({"id": d["id"], "kind": d["kind"], "author": d["author"], "value": self._value(session, d["value_ref"]),
                              "proposal_hash": _core.proposal_hash(d["id"], d["value_ref"], d["author"]), "channel": d["channel"]})
            else:
                hidden = True
        out["open"] = sorted(items, key=lambda x: x["id"])
        if notice:
            out["hidden_candidates"] = hidden
        return out

    def table(self, session) -> List[dict]:
        try:
            p = self._enf.principal_of(session)
        except PolicyDenied:
            return []
        eng = self._engines.get(session.tenant)
        if eng is None:
            return []
        return [x for x in (self.explain(session, r) for r in eng._reqs() if eng._participant(p.id, r)) if x is not None]
