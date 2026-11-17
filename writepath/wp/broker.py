"""Key broker (delta review D6, design path item 4; main's ruling after the H1 probe).

The fence rests on alias-name scoping plus the broker-held alias compare-and-set, NOT on key revocation (H1 at
606f457: PB, PC and PE held 3/3; PD, how fast an invalidated key stops working, was not measured there). A writer key
for tenant t and projector epoch E carries, for every registered family f, exactly:
    create_doc, index                          on  wp16096-<t>-<f>-w<E>        (the epoch write alias, by NAME)
    read, view_index_metadata, maintenance     on  wp16096-<t>-<f>             (the read alias: search and refresh)
and nothing else: no manage, no base-index name, no other tenant, no other epoch, no erase alias. Elasticsearch
authorises a request by the name it names (PB), so the key cannot write by concrete index name, through another
tenant's or another epoch's alias, or through -x, and cannot move aliases (PC1). Only the broker moves aliases (PC5:
whoever holds manage on the base index and an alias can re-add it, so the CAS is never delegated). When the epoch
moves on, the broker's CAS removes -w<E>, and the old key's writes are refused with no revocation at all (PE).
Invalidating older keys is hygiene, and receipts state the revocation delay as measured by H1a, or "not measured".

  writer_key(t, E, holder)   only while (t, 'projector', E, holder) is the LIVE lease (Postgres, wp_lease_holder_current,
                             checked before and after the CAS); then the family indices exist, the CAS moves every
                             family's write alias to -w<E> (families.fence_es: StaleEpoch if a newer epoch is visible),
                             a key with the descriptors above is minted (expiring after KEY_TTL), and the tenant's
                             writer keys of older epochs are invalidated (hygiene; failures are recorded, not fatal).
  eraser_key(t)              per tenant: index and view_index_metadata on the tenant's -x aliases only (placeholders
                             at external version 2^62); the family indices are created first.

The broker is its own process and service identity: it authenticates to Elasticsearch as the native user
wp16096_broker (password in its own 0600 file, never printed) and to Postgres as wp_broker (execute on
wp_lease_holder_current only). It serves the application over a unix socket with the key service's transport
(wp/keysvc.py: kernel peer uid, fail-closed allow list WP_BROKER_ALLOW_UIDS). It logs operation names, tenants,
epochs and key ids, never a key value.

NOT shown by this design: tenant authority (the broker trusts the peer uid for every tenant: design path item 2);
cross-node API-key cache behaviour (one node here)."""
import json
import os
import socket
import sys

from . import config, families as F, keysvc, lease as L
from .es import ES

BROKER_USER, BROKER_ROLE = "wp16096_broker", "wp16096_broker_role"
PWFILE_NAME = "es_broker_password"
KEY_TTL = "1h"
OPS = ("writer_key", "eraser_key")


class Refused(Exception):
    pass


def writer_descriptors(t, epoch):
    fam = sorted(F.FAMILIES)
    return {"wp16096_writer": {"cluster": [], "indices": [
        {"names": [F.write_alias(t, f, epoch) for f in fam], "privileges": ["create_doc", "index"]},
        {"names": [F.read_alias(t, f) for f in fam], "privileges": ["read", "view_index_metadata", "maintenance"]}]}}


def eraser_descriptors(t):
    return {"wp16096_eraser": {"cluster": [], "indices": [
        {"names": [F.erase_alias(t, f) for f in sorted(F.FAMILIES)], "privileges": ["index", "view_index_metadata"]}]}}


def broker_role_descriptor():
    """The broker user's own role (superuser-created): what it needs to create family indices, run the alias CAS,
    and delegate the writer and eraser privileges (an API key gets the intersection with its owner's privileges)."""
    p = config.INDEX_PREFIX
    return {"cluster": ["manage_own_api_key", "monitor"],
            "indices": [{"names": [f"{p}*"], "privileges": ["create_index", "manage", "view_index_metadata", "read",
                                                            "maintenance", "create_doc", "index"]}]}


class Broker:
    def __init__(self, es, lease_ok, log=None):
        """es: an ES client authenticated as the broker user; lease_ok(t, scope, epoch, holder) -> bool."""
        self.es, self.lease_ok, self.log = es, lease_ok, log
        self.conn = None                     # the key-service transport closes .conn when a client disconnects

    def note(self, s):
        if self.log:
            self.log(s)

    def _mint(self, name, descriptors, meta):
        s, j = self.es.req("POST", "/_security/api_key", {"name": name, "role_descriptors": descriptors,
                                                          "expiration": KEY_TTL, "metadata": meta}, ok=None)
        if s != 200:
            et = (j.get("error") or {}).get("type") if isinstance(j, dict) and isinstance(j.get("error"), dict) else None
            raise Refused(f"mint refused: {s} {et}")
        return {"id": j["id"], "encoded": j["encoded"], "expiration": j.get("expiration")}

    def _invalidate_older(self, t, epoch):
        """Hygiene only: the fence does not depend on it (PE, and the generation invariant in families.py). Returns
        (invalidated ids, error or None). Never raises (review B5): it runs AFTER the mint, and an exception here would
        withhold a key that already exists; the failure is recorded as a sanitized type name instead."""
        try:
            return self._invalidate_older_once(t, epoch)
        except Exception as e:  # noqa: BLE001  (transport, protocol or parse failure: recorded, never fatal)
            return [], f"hygiene {type(e).__name__}"

    def _invalidate_older_once(self, t, epoch):
        s, j = self.es.req("GET", "/_security/api_key", ok=None, owner="true", name=f"wp16096-w-{t}-e*", active_only="true")
        if s != 200:
            return [], f"list {s}"
        old = [k["id"] for k in j.get("api_keys", [])
               if (k.get("metadata") or {}).get("tenant") == t and int((k.get("metadata") or {}).get("epoch", 0)) < epoch]
        if not old:
            return [], None
        s, j = self.es.req("DELETE", "/_security/api_key", {"ids": old, "owner": True}, ok=None)
        return (j.get("invalidated_api_keys", []) if s == 200 else []), (None if s == 200 else f"invalidate {s}")

    def writer_key(self, t, epoch, holder):
        L.check_tenant(t)
        epoch = int(epoch)
        if epoch <= F.GENERATION0:
            raise Refused("bad_epoch")                     # -w0 is the generation alias: no key for it, ever
        if not holder or not self.lease_ok(t, L.PROJECTOR, epoch, holder):
            raise Refused("lease_not_current")
        F.fence_es(self.es, t, epoch)                      # broker-held CAS; StaleEpoch if a newer epoch is visible
        if not self.lease_ok(t, L.PROJECTOR, epoch, holder):
            raise Refused("lease_not_current")              # lost between the check and the CAS: no key for it
        k = self._mint(f"wp16096-w-{t}-e{epoch}", writer_descriptors(t, epoch),
                       {"stream": "goodqa-16096", "kind": "writer", "tenant": t, "epoch": epoch})
        inv, err = self._invalidate_older(t, epoch)
        self.note(f"writer_key tenant={t} epoch={epoch} key_id={k['id']} invalidated_older={len(inv)} err={err}")
        return dict(k, invalidated_older=len(inv), invalidate_error=err)

    def eraser_key(self, t):
        L.check_tenant(t)
        for f in F.FAMILIES:
            F.ensure_family(self.es, t, f)
        k = self._mint(f"wp16096-x-{t}", eraser_descriptors(t), {"stream": "goodqa-16096", "kind": "eraser", "tenant": t})
        self.note(f"eraser_key tenant={t} key_id={k['id']}")
        return k


def dispatch(b, op, a):
    try:
        if op == "writer_key":
            return {"ok": b.writer_key(a["tenant"], a["epoch"], a["holder"])}
        if op == "eraser_key":
            return {"ok": b.eraser_key(a["tenant"])}
    except Refused as e:
        return {"error": "Refused", "why": str(e)}
    except F.StaleEpoch:
        return {"error": "StaleEpoch"}
    return {"error": "unknown_op"}


class Client:
    """The application's side: asks the broker for keys over its socket (WP_BROKER)."""

    def __init__(self, path=None):
        self.path = path or os.environ["WP_BROKER"]
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.path)
        self.f = self.sock.makefile("rwb")

    def close(self):
        try:
            self.f.close()
        finally:
            self.sock.close()

    def _req(self, op, **a):
        self.f.write((json.dumps({"op": op, "args": a}) + "\n").encode())
        self.f.flush()
        line = self.f.readline()
        if not line:
            raise keysvc.ServiceError("broker closed the connection")
        rep = json.loads(line)
        if "ok" in rep:
            return rep["ok"]
        if rep.get("error") == "StaleEpoch":
            raise F.StaleEpoch("broker: a newer epoch is visible")
        if rep.get("error") == "Refused":
            raise Refused(rep.get("why") or "refused")
        raise keysvc.ServiceError(rep.get("error") or "unknown")

    def writer_key(self, tenant, epoch, holder):
        return self._req("writer_key", tenant=tenant, epoch=int(epoch), holder=holder)

    def eraser_key(self, tenant):
        return self._req("eraser_key", tenant=tenant)


def es_with_key(k, role):
    """An ES client that authenticates with a broker-minted key (never the admin key, never a role-key file)."""
    return ES(role=role, auth="ApiKey " + k["encoded"])


def eraser_es(t):
    """Broker mode: an ES client holding a fresh eraser key for tenant t (one broker round trip)."""
    c = Client()
    try:
        return es_with_key(c.eraser_key(t), "eraser")
    finally:
        c.close()


REVOCATION_DELAY = "measured: first refusal at most 0.055 s after invalidation, 20/20 trials, one node (H1a at c8e0a46); cross-node not shown"


def fence_statement():
    """What a receipt may claim about the Elasticsearch fence in this process's mode (main's D6 ruling)."""
    if broker_mode():
        return {"mode": "broker",
                "basis": "alias-name scoping (H1 PB) plus the alias compare-and-set held by the broker (H1 PC, PE); "
                         "not key revocation",
                "revocation": "hygiene only; " + REVOCATION_DELAY,
                "derived_keys": "not used (not demonstrated on Basic: H1a PA)",
                "issued_key_window": "a key already issued for epoch E keeps writing through -w<E> until a newer "
                                     "epoch's CAS (which needs the broker) or its expiry (" + KEY_TTL + "); while the "
                                     "broker is down no new epoch gets a key",
                "time_bound": "none claimed under concurrent appends"}
    return {"mode": "role_keys" if any((config.SECRETS / f"es_api_key.{r}.json").exists() for r in config.ES_ROLES)
            else "single_key",
            "basis": "require_alias request flag and the alias CAS run by the projector itself; the projector's key "
                     "holds manage on its aliases, so a projector that lost its epoch can re-add one (H1 PC5)",
            "revocation": "not used",
            "time_bound": "none claimed under concurrent appends"}


def broker_mode():
    """True when this process must take its Elasticsearch keys from the broker (the hardened build): then the
    writer and eraser paths never fall back to a role-key file or the admin key."""
    return bool(os.environ.get("WP_BROKER"))


def _broker_auth():
    import base64
    pw = (config.SECRETS / PWFILE_NAME).read_text().strip()
    return "Basic " + base64.b64encode(f"{BROKER_USER}:{pw}".encode()).decode()


def broker_factory(es_factory, pg_factory, log=None):
    """make_store for the transport: ONE Broker per client connection, with its own ES client and its own Postgres
    connection (autocommit), so concurrent peers never share an HTTP or database connection; the transport closes
    .conn when the client disconnects. A dead connection fails that client's requests closed until it reconnects."""
    def make():
        pg = pg_factory()

        def lease_ok(t, sc, e, who):
            return bool(pg.execute("select wp_lease_holder_current(%s, %s, %s, %s)", (t, sc, e, who)).fetchone()[0])
        b = Broker(es_factory(), lease_ok, log=log)
        b.conn = pg
        return b
    return make


def serve(path):
    from . import db
    allow = frozenset(int(x) for x in os.environ.get("WP_BROKER_ALLOW_UIDS", "").split(",") if x.strip())
    if not allow:
        raise SystemExit("refusing to serve: WP_BROKER_ALLOW_UIDS lists no uid")
    auth = _broker_auth()
    log = lambda s: print("broker", s, flush=True)          # noqa: E731
    make = broker_factory(lambda: ES(auth=auth), db.broker, log=log)
    srv = keysvc.Server(path, allow, make_store=make, log=log, ops=OPS, dispatch_fn=dispatch)
    print(f"broker serving uids={sorted(allow)}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"] and len(sys.argv) == 3:
        serve(sys.argv[2])
    else:
        raise SystemExit(__doc__)
