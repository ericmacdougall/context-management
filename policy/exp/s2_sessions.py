"""S2: authenticated sessions as the single root of identity (step 2, BRIEF-policy.md item 2), on the lab Postgres.

Real arm (current pol/_pgcore.sql), per seed, one install of tenants tenant-a and tenant-b:
  mint      raw SQL as dc_app and as dc_idp: the pre-step-2 bare-principal open_session, writing, altering or reading dc.sessions,
            reading or altering dc.factors / dc.used_proofs, enrolling a factor, dc_app opening or ending a session; and the in-process
            SessionAuthority asked for a session from a bare principal id. Every one must be refused.
  proof     N trials per class, principals and factors drawn by the seed: valid (must open a session that authenticates on dc.submit),
            forged MAC, body edited, another factor's key, another factor's id, look-alike principal, another principal, another
            channel, another tenant, expired, beyond the 300 s window, replay (the first use must open), revoked factor, malformed
            (one of eight shapes), expired inside a transaction (begun while the proof was valid; delta review M1), and, last, a
            revoked principal. Every class but valid and replay_first must be refused.
  replay_concurrent  8 dc_idp connections present ONE proof at once: exactly one opens a session.
  token     valid, forged, expired, revoked, ended (dc.end_session), empty and other-tenant admin tokens on all five dc_app calls
            (submit, explain, read_table, erase_candidates, erase_values), with a value about `dana` in tenant-a: the valid token
            gets data, every bad token gets the refusal, the other tenant's token sees and erases nothing of tenant-a. Delta review
            M1 adds expired_in_txn (a 1 s token, all five calls in a transaction begun while it was valid, made after it expired) and
            repeatable_read (a REPEATABLE READ transaction takes its snapshot, dc_idp then ends the token, then the five calls).
  origin    events by every kind and channel carry actor and trust_origin derived from the session; a body naming tenant, principal,
            channel, id, actor or trust_origin is refused identity_in_body.
  install_lookalike  a tenant config with look-alike principal ids is refused.
Controls, per seed, each on its own install (each must CATCH, i.e. show the attack its probe targets succeeding):
  ctl_bare_mint_80bb6a7   the 80bb6a7 SessionAuthority (exp/fixtures/directory_80bb6a7.py, verbatim) issues from a bare principal id.
  ctl_bare_mint_0a20b64   the 0a20b64 dc.open_session (exp/fixtures/open_session_0a20b64.sql, verbatim) granted to dc_idp: a bare mint
                          opens a session that authenticates.
  ctl_no_replay           variant without the replay ledger check: a replayed proof opens a second session.
  ctl_no_mac              variant without the MAC check: a proof with a forged MAC or another factor's key opens a session.
  ctl_identity_from_body  variant that skips the binding check and stamps the session from the proof BODY: a proof naming another or
                          look-alike principal opens a session as that principal.
  ctl_origin_from_body    variant (the e15241c submit plus body keys actor and trust_origin accepted, origin not stamped): a non-human
                          body naming actor "user" writes an event with actor "user".
  ctl_txn_clock           variant with d05d731's clock (now(), fixed at transaction start) and no isolation guard: a proof that
                          expires inside a transaction begun while it was valid opens a session.
Rows carry factor ids and outcomes only: no key, proof, MAC or token is written. Each proof trial and concurrent round also carries
its index and nonce_sha256, the first 16 hex digits of sha256 of the nonce drawn for it (final review P2: verify/s2.py counts DISTINCT
trials; the nonce is the presentation's identity, already stored in clear in dc.used_proofs, and is not a credential).
usage: python -m exp.s2_sessions (lab host; Postgres env as file paths)."""
import hashlib
import json
import os
import random
import secrets
import sys
import threading
import time
import types

from exp import common  # noqa: F401
from exp.common import write_items, write_result
from exp.seeds import seeds
from pol import pgcore as PG
from psycopg.types.json import Jsonb
from tests.kit import enroll, proof

import psycopg

T, TB = "tenant-a", "tenant-b"
CFG = {"tenant": T, "principals": {"alice": "human", "bob": "human", "carol": "human", "bot": "agent", "x": "analyst", "admin": "human"},
       "authority": {"r1": ["alice", "bob", "bot"], "r2": ["alice", "carol"]}, "proposers": {"r1": ["x"]}, "admins": ["admin"]}
CFG_B = dict(CFG, tenant=TB)
LAB = {"tenants": [T], "acl": [], "regions": None, "sensitivity": 0, "models": None, "banned": [], "local_only": False, "restricted": False,
       "holds": [], "subjects": []}
N = int(os.environ.get("S2_TRIALS") or 12)
CLASSES = ("valid", "forged_mac", "body_edited", "other_key", "other_factor_id", "look_alike", "other_principal", "other_channel",
           "other_tenant", "expired", "far_future", "replay", "revoked_factor", "malformed", "expired_in_txn")
MALFORMED = ("no_mac", "not_json", "extra_field", "nonce_not_hex", "expires_string", "empty", "oversized", "null")
CALLS = ("submit", "explain", "read_table", "erase_candidates", "erase_values")
TOKENS = ("valid", "forged", "expired", "revoked", "ended", "empty", "other_tenant", "expired_in_txn", "repeatable_read")
HERE = os.path.dirname(os.path.abspath(__file__))
SQL0 = PG._SQL_PATH
ROWS = []

REPLAY_CHECK = "  if n = 0 then raise exception 'proof refused: replayed' using errcode = 'P0001'; end if;\n"
MAC_CHECK = ("  if fr.factor is null or dc_crypto.digest(mac, 'sha256')\n"
             "       <> dc_crypto.digest(encode(dc_crypto.hmac(convert_to(body, 'UTF8'), fr.key, 'sha256'), 'hex'), 'sha256') then\n")
BINDING_CHECK = "  if b->>'tenant' <> fr.tenant or b->>'principal' <> fr.principal or b->>'channel' <> fr.channel then\n"
STAMP_SESSION = ("  insert into dc.sessions values (encode(sha256(convert_to(tok, 'UTF8')), 'hex'), fr.tenant, fr.principal, fr.channel, fr.factor,\n")
STAMP_RETURN = "  return jsonb_build_object('token', tok, 'tenant', fr.tenant, 'principal', fr.principal, 'channel', fr.channel, 'factor', fr.factor);\n"
ID_KEYS = "  if p_body ?| array['tenant', 'principal', 'channel', 'id', 'actor', 'trust_origin'] then\n"
ORIGIN_STAMP = "        || dc._origin((select p.kind from dc.principal p where p.tenant = s.tenant and p.pid = s.principal), s.channel);\n"
BODY_KEYS = "                                  'choose','target','role'];\n"
# delta review M1: the wall-clock checks and the isolation guards of the fix, and what d05d731 had instead (ctl_txn_clock)
TIME_CHECK = ("  if x <= extract(epoch from clock_timestamp()) or x > extract(epoch from clock_timestamp()) + 300 then\n"
              "    raise exception 'proof refused: time' using errcode = 'P0001';\n  end if;\n")
TIME_CHECK_OLD = ("  if x <= extract(epoch from now()) or x > extract(epoch from now()) + 300 then raise exception 'proof refused: time' "
                  "using errcode = 'P0001'; end if;\n")
TIME_RECHECK = "  if x <= extract(epoch from clock_timestamp()) then raise exception 'proof refused: time' using errcode = 'P0001'; end if;\n"
OPEN_ISOLATION = ("  if current_setting('transaction_isolation') <> 'read committed' then\n"
                  "    raise exception 'proof refused: isolation' using errcode = 'P0001';\n  end if;\n")
SESSION_ISOLATION = "  if current_setting('transaction_isolation') <> 'read committed' then return s; end if;\n"
SESSION_CLOCK = "     and not x.revoked and x.expires_at > clock_timestamp();\n"
VARIANTS = {
    "ctl_no_replay": [(REPLAY_CHECK, "")],
    "ctl_no_mac": [(MAC_CHECK, "  if fr.factor is null then\n")],
    "ctl_identity_from_body": [(BINDING_CHECK, "  if false then\n"),
                               (STAMP_SESSION, STAMP_SESSION.replace("fr.tenant, fr.principal, fr.channel", "b->>'tenant', b->>'principal', b->>'channel'")),
                               (STAMP_RETURN, STAMP_RETURN.replace("'tenant', fr.tenant, 'principal', fr.principal, 'channel', fr.channel",
                                                                   "'tenant', b->>'tenant', 'principal', b->>'principal', 'channel', b->>'channel'"))],
    "ctl_origin_from_body": [(ID_KEYS, "  if p_body ?| array['tenant', 'principal', 'channel', 'id'] then\n"), (ORIGIN_STAMP, ";\n"),
                             (BODY_KEYS, "                                  'choose','target','role','actor','trust_origin'];\n")],
    "ctl_txn_clock": [(TIME_CHECK, TIME_CHECK_OLD), (TIME_RECHECK, ""), (OPEN_ISOLATION, ""), (SESSION_ISOLATION, ""),
                      (SESSION_CLOCK, SESSION_CLOCK.replace("clock_timestamp()", "now()"))],
}


def env():
    return (int(os.environ["POL_PG_PORT"]), os.environ["POL_PG_SUPER_PASSFILE"], os.environ["POL_PG_APP_PASSWORD_FILE"],
            os.environ["POL_PG_APP_PASSFILE"], os.environ["POL_PG_IDP_PASSWORD_FILE"], os.environ["POL_PG_IDP_PASSFILE"])


def owner(name):
    port, sp = env()[:2]
    return PG._connect("postgres", sp, port, name)


def variant(name):
    src = open(SQL0, encoding="utf-8").read()
    for old, new in VARIANTS[name]:
        assert src.count(old) == 1, (name, old)
        src = src.replace(old, new)
    d = os.path.join(os.path.dirname(HERE), "results", "tmp")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "pgcore_s2_%s.sql" % name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(src)
    return p


def install(sql_path=None, tenants=None):
    port, sp, apw, _ap, ipw, _ip = env()
    PG._SQL_PATH = sql_path or SQL0
    try:
        PG.install(port, sp, apw, tenants or {T: CFG, TB: CFG_B}, idp_password_file=ipw)
    finally:
        PG._SQL_PATH = SQL0


class Lab:
    def __init__(self):
        port, _sp, _apw, ap, _ipw, ip = env()
        self.o = owner("s2_owner")
        self.idp = PG.connect_idp(port, ip, "s2_idp")
        self.app = PG.connect_app(port, ap, "s2_app")
        self.idp_t = PG.connect_idp(port, ip, "s2_idp_txn")     # explicit transactions (expired_in_txn)
        self.app_t = PG.connect_app(port, ap, "s2_app_txn")

    def close(self):
        for c in (self.o, self.idp, self.app, self.idp_t, self.app_t):
            c.close()

    def open(self, p, conn=None):
        """('accepted', result) or ('refused', reason): the reason is the P0001 message tail or the SQLSTATE, never the proof."""
        try:
            return "accepted", (conn or self.idp).execute("select dc.open_session(%s, %s)", (p, 60)).fetchone()[0]
        except psycopg.Error as e:
            msg = (e.diag.message_primary or "") if e.sqlstate == "P0001" else ""
            return "refused", msg.replace("proof refused: ", "") if msg.startswith("proof refused: ") else str(e.sqlstate)

    def submit(self, tok, body):
        return self.app.execute("select dc.submit(%s, %s)", (tok, Jsonb(body))).fetchone()[0]

    def authenticated(self, tok):
        """True when dc.submit treats the token as a session (any decision code), False when it answers unauthenticated."""
        code = self.submit(tok, {"type": "retract", "client_id": "a" + secrets.token_hex(4), "idem": "a" + secrets.token_hex(4), "req": "r1",
                                 "based_on": 0})["code"]
        return not code.startswith("unauthenticated")


def open_in_txn(lab, p):
    """delta review M1: begin a transaction while the proof is still valid, read once (now() is fixed from here), wait until the proof
    has expired by the wall clock, then present it inside that transaction. The transaction is rolled back either way."""
    x = json.loads(p.rpartition(".")[0])["expires"]
    c = lab.idp_t
    c.execute("begin")
    try:
        c.execute("select now()")
        time.sleep(max(0.0, x + 0.3 - time.time()))
        return lab.open(p, c)
    finally:
        c.execute("rollback")


def channels(kind):
    return ("owner_ui", "owner_cli") if kind == "human" else ("agent_api",)


def look_alike(pid, rng):
    """A canonical id with the same pol.ids.skeleton as pid (only substitutions the skeleton folds, or a separator suffix)."""
    from pol.ids import canon, skeleton
    opts = [pid.replace(a, b, 1) for a, b in (("l", "1"), ("i", "l"), ("o", "0"), ("s", "5"), ("m", "rn"), ("w", "vv")) if a in pid]
    opts += [pid + "." , pid + "-", pid + "_"]
    la = rng.choice(opts)
    assert la != pid and canon(la) == la and skeleton(la) == skeleton(pid), (pid, la)
    return la


def malformed(f, shape, nonce):
    good = proof(f, nonce=nonce)
    body, _, mac = good.rpartition(".")
    return {"no_mac": body, "not_json": "x." + mac, "extra_field": proof(f, admin=True, nonce=nonce), "nonce_not_hex": proof(f, nonce="zz" * 16),
            "expires_string": proof(f, expires="soon", nonce=nonce), "empty": "", "oversized": proof(f, pad="p" * 5000, nonce=nonce),
            "null": None}[shape]


def nonce_id(nonce):
    """A trial's identity in the rows (final review P2): the first 16 hex digits of sha256 of the nonce drawn for it."""
    return hashlib.sha256(nonce.encode()).hexdigest()[:16]


def trial_nonce(p, nonce):
    """The identity of the trial whose drawn nonce is `nonce` and whose presented text is `p`. Where the presented text carries a
    parseable nonce it must BE the drawn one (only the nonce_not_hex shape presents a fixed invalid nonce), so the identity names the
    presentation and a bug that presents another proof stops the run instead of writing a row."""
    try:
        got = json.loads(p.rpartition(".")[0]).get("nonce") if isinstance(p, str) else None
    except (ValueError, AttributeError):
        got = None
    assert got in (None, nonce, "zz" * 16), "the presented proof is not the trial's"
    return nonce_id(nonce)


def factors(lab, rng):
    """One factor per (tenant, principal, channel its kind allows); alice also on owner_cli."""
    out = {}
    for t in (T, TB):
        for pid, kind in sorted(CFG["principals"].items()):
            for ch in channels(kind):
                out[(t, pid, ch)] = enroll(lab.o, t, pid, ch)
    return out


def make(cls, f, fs, rng, lab, nonce):
    """A proof of class `cls` built on factor f (tenant-a) with the trial's nonce. Returns the proof text and, for the record, the
    claimed principal."""
    same_t = [g for k, g in fs.items() if k[0] == T and g.factor_id != f.factor_id]
    other = rng.choice(same_t)
    if cls == "valid":
        return proof(f, nonce=nonce), f.principal
    if cls == "forged_mac":
        p = proof(f, nonce=nonce)
        return p[:-1] + ("0" if p[-1] != "0" else "1"), f.principal
    if cls == "body_edited":
        o = rng.choice([g for g in same_t if g.principal != f.principal])           # another PRINCIPAL, never the same one's other factor
        p = proof(f, nonce=nonce)
        body, _, mac = p.rpartition(".")
        edited = body.replace('"principal":"%s"' % f.principal, '"principal":"%s"' % o.principal)
        assert edited != body
        return edited + "." + mac, o.principal
    if cls == "other_key":
        return proof(f, key=other.key, nonce=nonce), f.principal
    if cls == "other_factor_id":
        return proof(f, factor=other.factor_id, nonce=nonce), f.principal
    if cls == "look_alike":
        la = look_alike(f.principal, rng)
        return proof(f, principal=la, nonce=nonce), la
    if cls == "other_principal":
        o = rng.choice([g for g in same_t if g.principal != f.principal])
        return proof(f, principal=o.principal, nonce=nonce), o.principal
    if cls == "other_channel":
        ch = rng.choice([c for c in ("owner_ui", "owner_cli", "agent_api") if c != f.channel])
        return proof(f, channel=ch, nonce=nonce), f.principal
    if cls == "other_tenant":
        return proof(f, tenant=TB, nonce=nonce), f.principal
    if cls == "expired":
        return proof(f, expires_in=-rng.randint(1, 3600), nonce=nonce), f.principal
    if cls == "far_future":
        return proof(f, expires_in=rng.randint(302, 86400), nonce=nonce), f.principal
    if cls == "revoked_factor":
        g = enroll(lab.o, T, f.principal, f.channel)
        lab.o.execute("update dc.factors set revoked = true where factor = %s", (g.factor_id,))
        return proof(g, nonce=nonce), f.principal
    if cls == "malformed":
        return malformed(f, rng.choice(MALFORMED), nonce), f.principal
    raise ValueError(cls)


def proof_rows(arm, seed, lab, fs, rng, classes, trials):
    keys = sorted(k for k in fs if k[0] == T and not (k[1] == "carol"))     # carol is revoked last
    for cls in classes:
        for i in range(trials):
            f = fs[rng.choice(keys)]
            n = secrets.token_hex(16)                    # the trial's own nonce (final review P2: one presentation per trial)
            if cls == "expired_in_txn":
                p, claimed = proof(f, expires_in=1.2, nonce=n), f.principal
                st, d = open_in_txn(lab, p)
            elif cls == "replay":
                p = proof(f, nonce=n)
                st1, d1 = lab.open(p)
                ROWS.append({"arm": arm, "seed": seed, "kind": "proof", "cls": "replay_first", "trial": i, "nonce_sha256": trial_nonce(p, n),
                             "outcome": st1, "factor": f.factor_id,
                             "principal": f.principal, "stamped": d1.get("principal") if st1 == "accepted" else None,
                             "authenticated": lab.authenticated(d1["token"]) if st1 == "accepted" else None})
                st, d = lab.open(p)
                claimed = f.principal
            else:
                p, claimed = make(cls, f, fs, rng, lab, n)
                st, d = lab.open(p)
            ROWS.append({"arm": arm, "seed": seed, "kind": "proof", "cls": cls, "trial": i, "nonce_sha256": trial_nonce(p, n),
                         "outcome": st, "reason": None if st == "accepted" else d,
                         "factor": f.factor_id, "factor_principal": f.principal, "claimed": claimed,
                         "stamped": d.get("principal") if st == "accepted" else None,
                         "authenticated": lab.authenticated(d["token"]) if st == "accepted" else None})


def concurrent(arm, seed, lab, fs, rng, rounds=3, n=8):
    port, _sp, _a, _ap, _ipw, ip = env()
    conns = [PG.connect_idp(port, ip, "s2_c%d" % i) for i in range(n)]
    try:
        for r in range(rounds):
            f = fs[rng.choice(sorted(k for k in fs if k[0] == T and k[1] != "carol"))]
            nonce = secrets.token_hex(16)
            p, bar, out = proof(f, nonce=nonce), threading.Barrier(n), [None] * n

            def go(i):
                bar.wait()
                out[i] = lab.open(p, conns[i])[0]
            ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            ROWS.append({"arm": arm, "seed": seed, "kind": "replay_concurrent", "round": r, "presented": n, "winners": out.count("accepted"),
                         "factor": f.factor_id, "nonce_sha256": trial_nonce(p, nonce)})
    finally:
        for c in conns:
            c.close()


MINT = (("dc_idp", "bare_principal_open_session", "select dc.open_session('tenant-a','admin','owner_ui','f',60)"),
        ("dc_app", "bare_principal_open_session", "select dc.open_session('tenant-a','admin','owner_ui','f',60)"),
        ("dc_app", "open_session_with_proof", None),
        ("dc_app", "end_session", "select dc.end_session('x')"),
        ("dc_app", "enroll_factor", "select dc.enroll_factor('tenant-a','admin','owner_ui')"),
        ("dc_idp", "enroll_factor", "select dc.enroll_factor('tenant-a','admin','owner_ui')"),
        ("dc_app", "insert_session", "insert into dc.sessions values ('h','tenant-a','admin','owner_ui','f', now() + interval '1 hour', false)"),
        ("dc_idp", "insert_session", "insert into dc.sessions values ('h','tenant-a','admin','owner_ui','f', now() + interval '1 hour', false)"),
        ("dc_app", "update_session", "update dc.sessions set principal = 'admin'"),
        ("dc_idp", "update_session", "update dc.sessions set principal = 'admin'"),
        ("dc_app", "read_sessions", "select count(*) from dc.sessions"),
        ("dc_idp", "read_sessions", "select count(*) from dc.sessions"),
        ("dc_app", "read_factors", "select count(*) from dc.factors"),
        ("dc_idp", "read_factors", "select count(*) from dc.factors"),
        ("dc_idp", "alter_factors", "update dc.factors set revoked = false"),
        ("dc_idp", "clear_used_proofs", "delete from dc.used_proofs"))


def mint_rows(arm, seed, lab, fs):
    for role, what, q in MINT:
        conn = lab.idp if role == "dc_idp" else lab.app
        args = ()
        if q is None:
            q, args = "select dc.open_session(%s, 60)", (proof(fs[(T, "alice", "owner_ui")]),)
        try:
            with conn.transaction():
                conn.execute(q, args)
            ROWS.append({"arm": arm, "seed": seed, "kind": "mint", "role": role, "what": what, "outcome": "accepted"})
        except psycopg.Error as e:
            ROWS.append({"arm": arm, "seed": seed, "kind": "mint", "role": role, "what": what, "outcome": "refused", "reason": e.sqlstate})
    from pol.directory import Directory, Principal, SessionAuthority
    from tests.kit import LabIdP
    sa = SessionAuthority(Directory([Principal("alice", T, frozenset(["staff"]), 3, False, "human", "eu", None)]), LabIdP())
    try:
        sa.issue("alice", "owner_ui")
        out = "accepted"
    except TypeError:
        out = "refused"
    except Exception as e:                       # noqa: BLE001  any refusal of a bare principal id
        out = "refused:" + type(e).__name__
    ROWS.append({"arm": arm, "seed": seed, "kind": "mint", "role": "in_process", "what": "bare_principal_issue", "outcome": out})


def _obs(name, v, marker):
    """What a call returned, reduced: `seen` counts the seeded tenant-a proposal (its id) in the answer."""
    if name == "submit":
        return v
    if name == "explain":
        return None if v is None else {"tenant": v.get("tenant"), "seen": json.dumps(v).count(marker)}
    if name == "read_table":
        return {"n": len(v), "seen": json.dumps(v).count(marker)}
    if name == "erase_candidates":
        return {"refused": v["refused"], "values": len(v["values"])}
    return {"refused": v["refused"], "erased": v.get("erased")}


def token_rows(arm, seed, lab, fs):
    a = lab.open(proof(fs[(T, "alice", "owner_ui")]))[1]["token"]
    lab.submit(a, {"type": "propose", "client_id": "seed-%s" % seed, "idem": "seed-%s" % seed, "req": "r1", "value": "about dana",
                   "label": dict(LAB, subjects=["dana"]), "based_on": 0})
    adm = lambda t=T: lab.open(proof(fs[(t, "admin", "owner_ui")]))[1]["token"]     # noqa: E731
    h = lambda t: hashlib.sha256(t.encode()).hexdigest()                               # noqa: E731
    toks = {"valid": adm(), "forged": secrets.token_hex(32), "expired": adm(), "revoked": adm(), "ended": adm(), "empty": "",
            "other_tenant": adm(TB)}
    lab.o.execute("update dc.sessions set expires_at = now() - interval '1 second' where token_hash = %s", (h(toks["expired"]),))
    lab.o.execute("update dc.sessions set revoked = true where token_hash = %s", (h(toks["revoked"]),))
    ended = lab.idp.execute("select dc.end_session(%s)", (toks["ended"],)).fetchone()[0]
    def calls(c, tok):
        return {"submit": c.execute("select dc.submit(%s, %s)", (tok, Jsonb({"type": "propose", "client_id": "t" + secrets.token_hex(4),
                                                                            "idem": "t" + secrets.token_hex(4), "req": "r1", "value": "w",
                                                                            "label": LAB, "based_on": 0}))).fetchone()[0]["code"],
                "explain": c.execute("select dc.explain(%s, 'r1')", (tok,)).fetchone()[0],
                "read_table": c.execute("select dc.read_table(%s)", (tok,)).fetchone()[0],
                "erase_candidates": c.execute("select dc.erase_candidates(%s, 'dana')", (tok,)).fetchone()[0],
                "erase_values": c.execute("select dc.erase_values(%s, 'dana', %s)", (tok, ["nothing"])).fetchone()[0]}

    def in_txn(cls):
        """delta review M1. expired_in_txn: an admin token opened with a 1 s lifetime, a transaction begun while it is valid (now() fixed
        by a first read), the five calls made after it has expired by the wall clock. repeatable_read: a valid admin token, a
        REPEATABLE READ transaction that takes its snapshot, dc.end_session committed by dc_idp, then the five calls. Rolled back."""
        c = lab.app_t
        if cls == "expired_in_txn":
            tok = lab.idp.execute("select dc.open_session(%s, 1)", (proof(fs[(T, "admin", "owner_ui")]),)).fetchone()[0]["token"]
            c.execute("begin")
            try:
                c.execute("select now()")
                time.sleep(1.3)
                return calls(c, tok)
            finally:
                c.execute("rollback")
        tok = adm()
        c.execute("begin isolation level repeatable read")
        try:
            c.execute("select 1")
            assert lab.idp.execute("select dc.end_session(%s)", (tok,)).fetchone()[0] is True
            return calls(c, tok)
        finally:
            c.execute("rollback")

    for cls in TOKENS:
        if cls in ("expired_in_txn", "repeatable_read"):
            got = in_txn(cls)
        else:
            got = calls(lab.app, toks[cls])
        for call in CALLS:
            ROWS.append({"arm": arm, "seed": seed, "kind": "token", "cls": cls, "call": call, "obs": _obs(call, got[call], "alice/seed-%s" % seed)})
    ROWS.append({"arm": arm, "seed": seed, "kind": "token_after", "ended_returned": ended,
                 "dana_values": lab.o.execute("select count(*) from dc.vals where tenant = %s and 'dana' = any(subjects)", (T,)).fetchone()[0]})


def origin_rows(arm, seed, lab, fs, bodies=True):
    cases = [("alice", "owner_ui", "r1"), ("alice", "owner_cli", "r1"), ("bob", "owner_ui", "r1"), ("carol", "owner_ui", "r2"),
             ("bot", "agent_api", "r1"), ("x", "agent_api", "r1")]
    for pid, ch, req in cases:
        tok = lab.open(proof(fs[(T, pid, ch)]))[1]["token"]
        cid = "o-%s-%s-%s" % (pid, ch, seed)
        out = lab.submit(tok, {"type": "propose", "client_id": cid, "idem": cid, "req": req, "value": "v", "label": LAB, "based_on": 0})
        ev = lab.o.execute("select ev->>'actor', ev->>'trust_origin' from dc.events where tenant = %s and evid = %s", (T, pid + "/" + cid)).fetchone()
        ROWS.append({"arm": arm, "seed": seed, "kind": "origin", "principal": pid, "principal_kind": CFG["principals"][pid], "channel": ch,
                     "code": out["code"], "actor": ev[0] if ev else None, "trust_origin": ev[1] if ev else None})
    if not bodies:
        return
    tok = lab.open(proof(fs[(T, "bot", "agent_api")]))[1]["token"]
    for k, v in (("tenant", TB), ("principal", "alice"), ("channel", "owner_ui"), ("id", "alice/x"), ("actor", "user"), ("trust_origin", "user_typed")):
        cid = "b-%s-%s" % (k, seed)
        out = lab.submit(tok, {"type": "propose", "client_id": cid, "idem": cid, "req": "r1", "value": "v", "label": LAB, "based_on": 0, k: v})
        ev = lab.o.execute("select ev->>'actor' from dc.events where tenant = %s and evid = %s", (T, "bot/" + cid)).fetchone()
        ROWS.append({"arm": arm, "seed": seed, "kind": "body_identity", "key": k, "code": out["code"], "event_actor": ev[0] if ev else None})


def revoked_principal_rows(arm, seed, lab, fs, rng):
    lab.o.execute("update dc.principal set revoked = true where tenant = %s and pid = 'carol'", (T,))
    for i in range(N):
        f = fs[(T, "carol", rng.choice(channels("human")))]
        n = secrets.token_hex(16)
        p = proof(f, nonce=n)
        st, d = lab.open(p)
        ROWS.append({"arm": arm, "seed": seed, "kind": "proof", "cls": "revoked_principal", "trial": i, "nonce_sha256": trial_nonce(p, n),
                     "outcome": st, "reason": None if st == "accepted" else d,
                     "factor": f.factor_id, "factor_principal": "carol", "claimed": "carol", "stamped": d.get("principal") if st == "accepted" else None,
                     "authenticated": None})


def lookalike_install_row(arm, seed, rng):
    base = rng.choice(sorted(CFG["principals"]))
    la = look_alike(base, rng)
    cfg = dict(CFG, principals=dict(CFG["principals"], **{la: CFG["principals"][base]}))
    try:
        install(tenants={T: cfg})
        out = "accepted"
    except Exception as e:                       # noqa: BLE001  the config is refused before or inside the install transaction
        out = "refused:" + type(e).__name__
    ROWS.append({"arm": arm, "seed": seed, "kind": "install_lookalike", "base": base, "look_alike": la, "outcome": out})


def real_arm(seed):
    rng = random.Random("s2-real-%s" % seed)
    install()
    lab = Lab()
    try:
        fs = factors(lab, rng)
        mint_rows("real", seed, lab, fs)
        proof_rows("real", seed, lab, fs, rng, CLASSES, N)
        concurrent("real", seed, lab, fs, rng)
        token_rows("real", seed, lab, fs)
        origin_rows("real", seed, lab, fs)
        revoked_principal_rows("real", seed, lab, fs, rng)
    finally:
        lab.close()
    lookalike_install_row("real", seed, rng)


def old_directory():
    F = os.path.join(HERE, "fixtures", "directory_80bb6a7.py")
    mod = types.ModuleType("pol.directory_80bb6a7")
    mod.__file__, mod.__package__ = F, "pol"
    sys.modules[mod.__name__] = mod
    exec(compile(open(F, encoding="utf-8").read(), F, "exec"), mod.__dict__)
    return mod


def controls(seed):
    rng = random.Random("s2-ctl-%s" % seed)
    m = old_directory()
    sa = m.SessionAuthority(m.Directory([m.Principal("alice", T, frozenset(["staff"]), 3, False, "human", "eu", None)]))
    try:
        s = sa.issue("alice", "owner_ui")
        out, who = "accepted", s.principal_id
    except Exception as e:                       # noqa: BLE001
        out, who = "refused:" + type(e).__name__, None
    ROWS.append({"arm": "ctl_bare_mint_80bb6a7", "seed": seed, "kind": "mint", "role": "in_process", "what": "bare_principal_issue", "outcome": out,
                 "stamped": who})
    # the 0a20b64 bare-principal open_session, added next to the current one
    install()
    lab = Lab()
    try:
        fn = open(os.path.join(HERE, "fixtures", "open_session_0a20b64.sql"), encoding="utf-8").read()
        lab.o.execute("set role dc_owner")
        lab.o.execute(fn)
        lab.o.execute("reset role")
        lab.o.execute("grant execute on function dc.open_session(text, text, text, text, integer) to dc_idp")
        try:
            tok = lab.idp.execute("select dc.open_session('tenant-a','admin','owner_ui','ctl',60)").fetchone()[0]
            out, auth = "accepted", lab.authenticated(tok)
        except psycopg.Error as e:
            out, auth = "refused:" + str(e.sqlstate), None
        ROWS.append({"arm": "ctl_bare_mint_0a20b64", "seed": seed, "kind": "mint", "role": "dc_idp", "what": "bare_principal_open_session",
                     "outcome": out, "authenticated": auth})
    finally:
        lab.close()
    plan = {"ctl_no_replay": ("replay",), "ctl_no_mac": ("forged_mac", "other_key"), "ctl_identity_from_body": ("other_principal", "look_alike")}
    for name, classes in plan.items():
        install(variant(name))
        lab = Lab()
        try:
            proof_rows(name, seed, lab, factors(lab, rng), rng, classes, N)
        finally:
            lab.close()
    install(variant("ctl_origin_from_body"))
    lab = Lab()
    try:
        origin_rows("ctl_origin_from_body", seed, lab, factors(lab, rng))
    finally:
        lab.close()
    install(variant("ctl_txn_clock"))
    lab = Lab()
    try:
        proof_rows("ctl_txn_clock", seed, lab, factors(lab, rng), rng, ("expired_in_txn",), 3)
    finally:
        lab.close()


def main():
    t0 = time.time()
    sd = seeds("s2")
    for s in sd:
        real_arm(s)
        controls(s)
    install()                                    # leave the lab on the production core
    items = write_items("s2_sessions", ROWS)
    real = [r for r in ROWS if r["arm"] == "real"]
    bad = [r for r in real if r["kind"] in ("proof", "mint") and r.get("cls") not in ("valid", "replay_first") and r["outcome"] == "accepted"]
    data = {"verdict": "PASS" if not bad else "FAIL", "rows": len(ROWS), "accepted_attacks": len(bad), "trials_per_class": N,
            "seconds": round(time.time() - t0, 1), "items": dict(zip(("file", "sha256"), items)),
            "not_proven": "one Postgres 16 instance; lab factors are HMAC keys stored in the database (the owner can forge proofs); the proof "
                          "window trusts the database clock; the identity adapter's own authentication of a person or device (enrolment) is "
                          "out of scope; the verdict here is the experiment's own, verify/s2.py re-judges every row"}
    write_result("s2_sessions", {"seeds": sd, "trials_per_class": N, "classes": list(CLASSES), "tokens": list(TOKENS), "calls": list(CALLS),
                                 "controls": ["ctl_bare_mint_80bb6a7", "ctl_bare_mint_0a20b64"] + list(VARIANTS)}, data,
                 extra_env={"postgres": "16", "roles": "owner (enrol, setup), dc_idp (open/end), dc_app (API)"})
    print(data["verdict"], "rows", len(ROWS), "accepted attacks", len(bad))


if __name__ == "__main__":
    main()
