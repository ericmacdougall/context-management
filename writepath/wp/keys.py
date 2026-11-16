"""Per-subject keys and app-side authenticated encryption (AES-256-GCM).

The AAD binds every ciphertext to its tenant, subject and row identity, so a ciphertext cannot be moved between rows
(review N10). A decryption failure is an ERROR, never 'erased': only a missing key with a `destroyed` record means
erased. No key is cached across calls (an in-process key cache would be an unregistered content-bearing store)."""
import hashlib
import hmac
import os
import time

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import db


class SubjectErased(Exception):
    pass


class KeyStore:
    """The ONE interface the write path uses for subject keys. A client deployment implements it over its KMS with real
    key deletion (e.g. one KMS key or one wrapped data key per subject, destroyed through the KMS deletion API, with
    the KMS's own deletion-window and backup semantics stated in the receipt). Nothing else in wp/ touches key storage.

      keys(tenant, subjects, create=False) -> {subject: key bytes | None}   (create refuses a destroyed subject)
      tenant_hmac(tenant, text) / tenant_hmac_many(tenant, texts)          keyed hashes under the tenant key
      chain_key(tenant) -> bytes                                            log-chain MAC key (row 6); a KMS would
                                                                            run the MAC itself (HMAC key, no export)
      is_destroyed(tenant, subject), key_exists(tenant, subject)
      destroy(tenant, subject)                                              idempotent logical delete
      purge(timeout_s) -> {"purged": bool, ...}                          physical removal; False = not proven gone
      describe() -> {"kind", "stand_in": bool, "deletion"}                  copied into every result and receipt"""
    KIND = "abstract"
    STAND_IN = True
    DELETION = "unspecified"

    def describe(self):
        return {"kind": self.KIND, "stand_in": self.STAND_IN, "deletion": self.DELETION}


class KeyService(KeyStore):
    """LAB STAND-IN for a KMS: keys in a separate Postgres database (wp_keys) owned by wp_kms. Deletion = row delete +
    VACUUM + WAL switch + checkpoint, which removes the key from heap and current WAL in this lab (no WAL archive, no
    replica, no backup). It is NOT a KMS: no HSM, no deletion window, no audited key lifecycle."""
    KIND = "lab-postgres-keystore"
    STAND_IN = True
    DELETION = "row delete; purge waits for the cluster xid/xmin horizon to pass the delete, then VACUUM FULL + pg_switch_wal x2 + CHECKPOINT (lab: no archive, replica or backup; freed filesystem blocks not overwritten)"

    def __init__(self, conn=None):
        self.conn = conn or db.keys_connect()

    def keys(self, tenant, subjects, create=False):
        """{subject: key bytes | None}. create=True makes missing keys, refusing destroyed subjects."""
        out = {}
        for s in sorted(set(subjects)):
            with self.conn.transaction():
                self.conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"key:{tenant}/{s}",))
                r = self.conn.execute("select key_hex from subject_keys where tenant_id=%s and subject_id=%s", (tenant, s)).fetchone()
                if r is None and create:
                    if self.conn.execute("select 1 from destroyed where tenant_id=%s and subject_id=%s", (tenant, s)).fetchone():
                        raise SubjectErased(s)
                    k = os.urandom(32).hex()
                    self.conn.execute("insert into subject_keys values (%s,%s,%s)", (tenant, s, k))
                    r = (k,)
            out[s] = bytes.fromhex(r[0]) if r else None
        return out

    def tenant_hmac(self, tenant, text):
        with self.conn.transaction():
            self.conn.execute("insert into tenant_keys values (%s,%s) on conflict do nothing", (tenant, os.urandom(32).hex()))
            k = bytes.fromhex(self.conn.execute("select hkey_hex from tenant_keys where tenant_id=%s", (tenant,)).fetchone()[0])
        return hmac.new(k, text.encode(), hashlib.sha256).hexdigest()

    def tenant_hmac_many(self, tenant, texts):
        with self.conn.transaction():
            self.conn.execute("insert into tenant_keys values (%s,%s) on conflict do nothing", (tenant, os.urandom(32).hex()))
            k = bytes.fromhex(self.conn.execute("select hkey_hex from tenant_keys where tenant_id=%s", (tenant,)).fetchone()[0])
        return [hmac.new(k, x.encode(), hashlib.sha256).hexdigest() for x in texts]

    def chain_key(self, tenant):
        """Per-tenant chain MAC key, derived from the tenant key (domain-separated). Not a subject key: erasure does not
        destroy it, so the chain stays verifiable after an erasure."""
        return bytes.fromhex(self.tenant_hmac(tenant, "chain-key:v1"))

    def is_destroyed(self, tenant, subject):
        return self.conn.execute("select 1 from destroyed where tenant_id=%s and subject_id=%s", (tenant, subject)).fetchone() is not None

    def key_exists(self, tenant, subject):
        return self.conn.execute("select 1 from subject_keys where tenant_id=%s and subject_id=%s", (tenant, subject)).fetchone() is not None

    def destroy(self, tenant, subject):
        """Logical delete: the key row goes and `destroyed` is recorded, in one transaction (callers may wrap it in their
        own hold-check transaction). Idempotent. The bytes stay on disk until purge()."""
        with self.conn.transaction():
            self.conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"key:{tenant}/{subject}",))
            self.conn.execute("delete from subject_keys where tenant_id=%s and subject_id=%s", (tenant, subject))
            self.conn.execute("insert into destroyed(tenant_id, subject_id) values (%s,%s) on conflict do nothing", (tenant, subject))

    def purge(self, timeout_s=60.0, poll_s=0.25):
        """Physical purge after destroy(), run with NO transaction of the caller open. VACUUM FULL copies a deleted tuple
        into the rewritten file while any backend in the cluster still holds an xid or snapshot older than the delete
        (exp/probe_keyresidue.py `held`: 4/4 runs kept the key hex in the new relfilenode). So: take a fresh xid as the
        mark, wait until every other backend's backend_xid and backend_xmin is newer than it (wp_kms has
        pg_read_all_stats), then VACUUM FULL + pg_switch_wal x2 + CHECKPOINT. Returns {"waited_s", "purged"};
        purged=False (timeout) means the key bytes may remain on disk and the caller must not report the erasure done.
        Lab limits: prepared transactions and replication slots are not checked (none exist in this lab)."""
        t0 = time.time()
        mark = self.conn.execute("select txid_current()").fetchone()[0]       # autocommit: own short transaction
        # age() orders xids modulo wraparound: an older xid has a larger age than the mark
        q = ("with m as (select age(((%s::bigint) %% 4294967296)::text::xid) a) select count(*) from pg_stat_activity, m"
             " where pid <> pg_backend_pid() and ((backend_xid is not null and age(backend_xid) >= m.a) or"
             " (backend_xmin is not null and age(backend_xmin) >= m.a))")
        while True:
            old = self.conn.execute(q, (mark,)).fetchone()[0]
            if old == 0:
                break
            if time.time() - t0 > timeout_s:
                return {"waited_s": round(time.time() - t0, 3), "purged": False, "blocking_backends": old}
            time.sleep(poll_s)
        self.conn.execute("vacuum full subject_keys")
        for _ in range(2):
            self.conn.execute("select pg_switch_wal()")
            self.conn.execute("checkpoint")
        return {"waited_s": round(time.time() - t0, 3), "purged": True}


def aad(tenant, subject, row_id):
    return f"{tenant}\x1f{subject}\x1f{row_id}".encode()


def seal(key, tenant, subject, row_id, plaintext: str) -> bytes:
    n = os.urandom(12)
    return n + AESGCM(key).encrypt(n, plaintext.encode(), aad(tenant, subject, row_id))


def open_(key, tenant, subject, row_id, blob: bytes):
    """Plaintext, or None when the key is gone. Raises InvalidTag on tampering or a swapped ciphertext."""
    if key is None:
        return None
    blob = bytes(blob)
    return AESGCM(key).decrypt(blob[:12], blob[12:], aad(tenant, subject, row_id)).decode()


def req_hmac(key, canonical: str):
    """Request fingerprint for idempotency, keyed by the SUBJECT key: after the key is destroyed nobody can confirm a
    guessed request against it (it is not a tenant-key hash that survives erasure)."""
    return hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()


def derive_id(*parts):
    """Derivation-addressed id (a function of inputs, never of plaintext)."""
    return hashlib.sha256("\x1f".join(str(x) for x in parts).encode()).hexdigest()[:32]
