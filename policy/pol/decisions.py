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

import dataclasses
import re
import secrets
import threading
from typing import Dict, List, Mapping, Optional, Protocol

from . import _core
from .enforce import Enforcer, PolicyDenied
from .labels import Label, LabelError, canon_subject

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
IDENTITY_KEYS = frozenset(["tenant", "principal", "channel", "id", "actor", "trust_origin"])     # step 2: origin is derived, never sent
BODY_KEYS = frozenset(["type", "client_id", "idem", "req", "value", "label", "based_on", "proposal", "proposal_hash", "pending", "verb",
                       "choose", "target", "role"])


class _ValueCipher(Protocol):           # interface only (see module docstring)
    def seal(self, tenant: str, subjects: List[str], plaintext: bytes) -> bytes: ...

    def open(self, tenant: str, sealed: bytes) -> bytes: ...

    def shred(self, tenant: str, subject: str) -> None: ...


def _random_ref() -> str:
    return "val-" + secrets.token_hex(20)


def _tokenised(label: Label, tokens) -> Label:
    """The label as the append-only log and core state keep it: each subject replaced by its token (R3-3)."""
    return dataclasses.replace(label, subjects=frozenset(tokens))


@dataclasses.dataclass(frozen=True)
class _Staged:
    ref: str
    created: bool           # False: the stored value of an earlier submission with this key (a retry); nothing to settle
    key: tuple


class Vault:
    """Referenced value storage. Refs are assigned here. Each value is tagged with the subjects of its label, so erasure of a subject
    covers every value ABOUT that subject, whoever wrote it."""

    def __init__(self, ref_factory=None):
        """ref_factory: a zero-argument callable returning a fresh reference. The default is random; an experiment that compares two
        replays of one script passes a counter (exp/e5_threads.py). It must never be derived from the value, its label or its subjects."""
        self._vals: Dict[str, dict] = {}
        self._subj: Dict[tuple, str] = {}
        self._idem: Dict[tuple, str] = {}           # (tenant, principal, idem) -> ref, only while that value is stored
        self._new_ref = ref_factory or _random_ref
        self._lock = threading.Lock()
        self._key_locks = [threading.Lock() for _ in range(64)]

    def _key_lock(self, tenant: str, principal: str, idem: str) -> threading.Lock:
        """The lock serialising submissions with one (tenant, principal, idempotency key) from allocation to cleanup (delta review M3).
        Striped over 64 locks, so memory is bounded; unrelated keys may share a stripe and then only wait for each other. Lock order:
        a key lock is taken first and never while holding the vault's or an engine's lock."""
        return self._key_locks[hash((tenant, principal, idem)) % len(self._key_locks)]

    def _stage(self, tenant: str, principal: str, idem: str, text: str, label: Label) -> "_Staged":
        """Allocate the reference for one submission; `_settle` must follow on every exit path (final review P3). A retry by the same
        principal with the same idempotency key, value and label, while that value is stored, gets the stored reference (so the core
        sees the same event and answers `duplicate`); anything else gets a FRESH reference from the factory, and the value is stored
        WITHOUT a retry mapping: the mapping is published by `_settle` only once the engine has accepted the event (at a2d65fd it was
        published here, and an exception before the engine's decision left it naming a value no event had). No reference is computed
        from the value, the label or a subject name, so a reference left in the append-only log after erasure cannot be confirmed by
        guessing a name or a text, by anyone, the vault included (review-3 N5). After erasure a retry gets a fresh reference and the
        core answers `idem_conflict`."""
        k = (tenant, principal, idem)
        with self._lock:
            r = self._idem.get(k)
            v = self._vals.get(r) if r is not None else None
            if v is not None and v["text"] == text and v["label"].to_dict() == label.to_dict():
                return _Staged(r, False, k)
            ref = self._new_ref()
            if ref in self._vals:
                raise RuntimeError("value reference collision")
            self._vals[ref] = {"tenant": tenant, "text": text, "label": label, "subjects": frozenset(label.subjects), "key": k}
            return _Staged(ref, True, k)

    def _settle(self, staged: "_Staged", outcome: str) -> None:
        """outcome: "accepted" (the engine applied the event: publish the retry mapping; a mapping left for another value of this key
        names a value no applied event has, because the engine would have answered duplicate or idem_conflict had one been applied, so
        that value goes), "kept" (the engine answered duplicate, or whether it applied the event is unknown: keep the value and publish
        the mapping unless one exists, so the exact retry converges), "refused" (not applied: a value created by `_stage` is deleted)."""
        if not staged.created:
            return
        with self._lock:
            k, ref = staged.key, staged.ref
            if outcome == "refused":
                v = self._vals.pop(ref, None)
                if v is not None and self._idem.get(k) == ref:
                    del self._idem[k]
                return
            if ref not in self._vals:                # erased meanwhile (erasure needs the subject's admin; the key lock is not held)
                return
            prior = self._idem.get(k)
            if outcome == "accepted":
                if prior is not None and prior != ref:
                    self._vals.pop(prior, None)
                self._idem[k] = ref
            elif prior is None:
                self._idem[k] = ref

    def _event_label(self, tenant: str, label: Label) -> Label:
        """Subjects replaced by a RANDOM token per (tenant, canonical subject), minted on first use and held only here. Erasure deletes
        the mapping (`_forget_subject`), after which a token left in the append-only log is unlinkable and no party, including the vault
        key holder, can confirm a guessed name; a re-appearing subject gets a fresh token (R3-3)."""
        with self._lock:
            toks = [self._subj.setdefault((tenant, x), "subj-" + secrets.token_hex(16)) for x in sorted(label.subjects)]
        return _tokenised(label, toks)

    def _forget_subject(self, tenant: str, subject: str) -> None:
        with self._lock:
            self._subj.pop((tenant, subject), None)

    def _get(self, tenant: str, ref) -> Optional[dict]:
        with self._lock:
            v = self._vals.get(ref) if isinstance(ref, str) else None
            return {k: x for k, x in v.items() if k != "key"} if v is not None and v["tenant"] == tenant else None

    def _delete(self, tenant: str, ref: str, judge=None) -> bool:
        """Delete the value and its retry mapping: nothing the vault keeps mentions `ref` afterwards. judge(value), when given, is the
        caller's authorization; it runs INSIDE the vault lock that the deletion holds, so no wait comes between the judgment and the
        deletion (final review P1), and whatever it raises propagates with nothing deleted."""
        with self._lock:
            v = self._vals.get(ref)
            if v is None or v["tenant"] != tenant:
                return False
            if judge is not None:
                judge({k: x for k, x in v.items() if k != "key"})
            del self._vals[ref]
            if self._idem.get(v["key"]) == ref:
                del self._idem[v["key"]]
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

    def _submit(self, ev: dict, gate=None):
        """gate: the caller's final authorization (final review P1). It runs inside the engine lock, after every wait and immediately
        before the reduction; a non-None answer is returned as the code and nothing is applied."""
        with self._lock:
            if gate is not None:
                refusal = gate()
                if refusal is not None:
                    return refusal, None
            code, orig, delta = _core.reduce(self._st, ev)
            self._st.apply_delta(delta)
            if code not in _core.REFUSED:
                self._log.append(ev)
            return code, orig

    def _applied(self, ev) -> bool:
        """True when the engine's idempotency record for ev's (principal, key) holds ev's own fingerprint, i.e. ev itself was applied
        (the lost-response check after a failure: final review P3)."""
        try:
            fp = _core.sha(ev)
            key = (ev["principal"], ev["idem"])
            hash(key)
        except Exception:       # noqa: BLE001  an event that cannot be fingerprinted or keyed was refused bad_event, never applied
            return False
        with self._lock:
            rec = self._st.idem(*key)
        return rec is not None and rec[0] == fp

    def _revoked(self):
        with self._lock:
            return sorted(self._st.revoked)

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
        for eng in self._engines.values():          # R3-10: revocations already in a core log reach the enforcer at start-up
            for pid in eng._revoked():
                enforcer._revocations._add(pid)

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
        if self._enf._revocations._is_revoked(p.id):                  # R3-10: one revocation source for core and enforcer
            return self._refuse(p.id, "principal_revoked", body)
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
        t, idem = body.get("type"), body.get("idem")
        if not isinstance(t, str) or t not in _core.REQUIRED or not isinstance(idem, str) or not idem:
            out = self._refuse(p.id, "bad_event", body)     # final review P3: refused before anything is allocated (the core's answer)
            out["id"] = p.id + "/" + cid
            return out
        ev = {k: v for k, v in body.items() if k not in ("client_id", "value", "label")}     # the label enters the event only tokenised
        ev.update(id=p.id + "/" + cid, tenant=session.tenant, principal=p.id, channel=session.channel)
        ev["actor"], ev["trust_origin"] = _core.origin(eng._st.kind(p.id), session.channel)       # from the session, as dc.submit
        # delta review M3: allocating the reference, the engine's decision and the loser's cleanup for one (tenant, principal,
        # idempotency key) run under one lock, so two overlapping submissions with that key are decided one after the other and the
        # retry mapping left behind is always the accepted submission's (dc.submit gets the same from its per-principal advisory lock)
        with self._vault._key_lock(session.tenant, p.id, idem):
            return self._submit_keyed(session, p, body, eng, ev)

    def _gate(self, session, pid: str) -> Optional[str]:
        """The final authorization (final review P1): the entry judgment again (live session, current directory entry of the same
        principal and tenant, not revoked). MemoryEngine._submit runs it inside its lock, after the key-lock and engine-lock waits and
        immediately before the reduction, so nothing is applied for a session that ended, or a principal revoked or removed, while
        the submission waited. None: allowed; otherwise the refusal code."""
        try:
            q = self._enf.principal_of(session)
        except PolicyDenied as e:
            return "unauthenticated:" + e.decision.reason
        if q.id != pid:
            return "unauthenticated:principal_changed"
        if self._enf._revocations._is_revoked(pid):
            return "principal_revoked"
        return None

    def _submit_keyed(self, session, p, body, eng, ev) -> dict:
        lab = None
        if "value" in body:
            if not isinstance(body["value"], str) or "label" not in body:
                return self._refuse(p.id, "bad_event", body)
            try:
                lab = Label.from_dict(body["label"])
            except (LabelError, TypeError, ValueError, AttributeError):
                return self._refuse(p.id, "bad_event", body)
        elif "label" in body:
            return self._refuse(p.id, "bad_event", body)  # a label belongs to a value; never let an untokenised label into the log
        # final review P3: from here every exit settles the staged value. Before the engine has accepted the event the vault holds the
        # value without a retry mapping; the mapping is published only on acceptance, and a value the engine did not apply is deleted,
        # whether the engine refused it or anything raised (on an exception, acceptance is read back from the engine's idempotency
        # record, so a lost response after the engine applied the event keeps its value and stays retryable).
        staged = None
        try:
            if lab is not None:
                staged = self._vault._stage(session.tenant, p.id, ev["idem"], body["value"], lab)
                ev["value_ref"] = staged.ref
                ev["label"] = self._vault._event_label(session.tenant, lab).to_dict()
            code, orig = eng._submit(ev, gate=lambda: self._gate(session, p.id))
        except BaseException:
            if staged is not None:
                self._vault._settle(staged, self._after_fault(eng, ev))
            raise
        ref = staged.ref if staged is not None else None
        if staged is not None:
            self._vault._settle(staged, "accepted" if code not in _core.REFUSED and not code.startswith("unauthenticated:")
                                else "kept" if code == "duplicate" else "refused")
        if code.startswith("unauthenticated:"):           # the final judgment found the session gone (as at entry: not audited here)
            return {"code": code, "original": None, "id": None, "value_ref": None}
        if code in _core.REFUSED:
            out = self._refuse(p.id, code, body, orig)
            out["id"] = ev["id"]
            if code == "duplicate" and ref is not None:
                out["value_ref"] = ref                   # M2: a retry gets the original outcome; same body -> same stored ref
            return out
        if code == "revoked":
            self._enf._revocations._add(ev["target"])
        self._enf._audit.add({"action": "decision", "principal": p.id, "allow": True, "reason": code, "type": ev.get("type"), "req": ev.get("req"),
                              "policy_version": self._enf._ver()})
        return {"code": code, "original": orig, "id": ev["id"], "value_ref": ref}

    @staticmethod
    def _after_fault(eng, ev) -> str:
        """How to settle a staged value after an exception: "accepted" when the engine's record shows ev applied, "refused" when it
        shows it was not, "kept" when the engine cannot say (no record lookup, or the lookup fails): keeping the value and its mapping
        lets the exact retry converge (duplicate if applied, applied once if not)."""
        f = getattr(eng, "_applied", None)
        if f is None:
            return "kept"
        try:
            return "accepted" if f(ev) else "refused"
        except Exception:       # noqa: BLE001
            return "kept"

    def erase_subject(self, session, subject: str) -> dict:
        """Erase every vault value about `subject` (whoever said it), matched by THE subject canonicalisation (pol.labels.canon_subject).
        A value under a recorded legal hold is queued, not erased. `result` is "no_values_found" when nothing is about the subject (never
        reported as success), "erased" when every value went, "incomplete" when some were queued or denied. When no value about the
        subject remains, its token mapping is deleted, so tokens left in the log become unlinkable (R3-2, R3-3)."""
        p = self._enf.principal_of(session)
        eng = self._engines.get(session.tenant)
        if eng is None or not eng._is_admin(p.id):
            self._refuse(p.id, "unauthorized", {"type": "erase"})
            return {"erased": 0, "queued": 0, "denied": 0, "refused": "unauthorized", "result": None}
        try:
            subj = canon_subject(subject)
        except LabelError:
            self._refuse(p.id, "bad_event", {"type": "erase"})
            return {"erased": 0, "queued": 0, "denied": 0, "refused": "bad_subject", "result": None}
        out = {"erased": 0, "queued": 0, "denied": 0, "refused": None, "result": None}
        refs = self._vault._refs_about(session.tenant, subj)
        for ref in refs:
            # final review P1: each value is judged inside the vault lock its deletion holds (no wait between judgment and deletion)
            try:
                gone = self._vault._delete(session.tenant, ref, judge=lambda v: self._enf._check(session, "erase", [v["label"]], {}))
            except PolicyDenied as e:
                out["queued" if e.decision.reason == "erasure_queued" else "denied"] += 1
                continue
            if gone:
                out["erased"] += 1
        if not self._vault._refs_about(session.tenant, subj):
            self._vault._forget_subject(session.tenant, subj)
        out["result"] = "no_values_found" if not refs else ("erased" if out["queued"] == out["denied"] == 0 else "incomplete")
        self._enf._audit.add({"action": "erase_subject", "principal": p.id, "allow": True, "reason": out["result"], "erased": out["erased"],
                              "queued": out["queued"], "denied": out["denied"], "policy_version": self._enf._ver()})
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
        return out if self._still(session, p.id) else None      # final review P1: judged again after the engine-lock waits

    def _still(self, session, pid: str) -> bool:
        """The read calls' final judgment: the session still names the same current principal (the entry judgment of a read)."""
        try:
            return self._enf.principal_of(session).id == pid
        except PolicyDenied:
            return False

    def table(self, session) -> List[dict]:
        try:
            p = self._enf.principal_of(session)
        except PolicyDenied:
            return []
        eng = self._engines.get(session.tenant)
        if eng is None:
            return []
        rows = [x for x in (self.explain(session, r) for r in eng._reqs() if eng._participant(p.id, r)) if x is not None]
        return rows if self._still(session, p.id) else []
