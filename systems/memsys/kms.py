"""Application-side access to the key service (a separate Postgres container: emlab-keys-16096).
The event database never sees a key; it stores only ciphertext, ciphertext hashes and key-free fences."""
import os
import psycopg
from .db import HOME

_KMS = {}


class SubjectErased(Exception):
    pass


def _connect():
    port = int((HOME / "kms_port").read_text().strip())
    os.environ.setdefault("PGPASSFILE", str(HOME / "pgpass"))
    return psycopg.connect(host="127.0.0.1", port=port, dbname="keys", user="kms", autocommit=True, application_name="memsys-kms")


def get():
    """One KMS client per process (fork-safe)."""
    k = _KMS.get(os.getpid())
    if k is None or k.conn.closed:
        k = _KMS[os.getpid()] = KMS(_connect())
    return k


def reset_schema():
    c = _connect()
    c.execute("drop schema public cascade; create schema public;")
    c.execute((__import__("pathlib").Path(__file__).with_name("kms.sql")).read_text())
    c.close(); _KMS.pop(os.getpid(), None)


class KMS:
    def __init__(self, conn):
        self.conn = conn

    def enc_many(self, tenant, subjects, texts):
        out = []
        try:
            for s, t in zip(subjects, texts):
                out.append(bytes(self.conn.execute("select kms_enc(%s,%s,%s)", (tenant, s, t.encode("utf-8"))).fetchone()[0]))
        except psycopg.errors.RaiseException as e:
            if "subject_erased" in str(e):
                raise SubjectErased(str(e)) from None
            raise
        return out

    def enc(self, tenant, subject, text):
        return self.enc_many(tenant, [subject], [text])[0]

    def dec_many(self, tenant, pairs):
        """pairs: [(subject, ciphertext)] -> [plaintext or None] in order (None = key destroyed / unreadable)."""
        out = [None] * len(pairs)
        by = {}
        for i, (s, c) in enumerate(pairs):
            if c is not None:
                by.setdefault(s, []).append(i)
        for s, idx in by.items():
            rows = self.conn.execute("select kms_dec(%s, %s, c) from unnest(%s::bytea[]) with ordinality as u(c, n) order by n",
                                     (tenant, s, [bytes(pairs[i][1]) for i in idx])).fetchall()
            for i, (b,) in zip(idx, rows):
                out[i] = bytes(b).decode("utf-8") if b is not None else None
        return out

    def hmac_many(self, tenant, texts):
        return [self.conn.execute("select kms_hmac(%s,%s)", (tenant, t)).fetchone()[0] for t in texts]

    def destroy(self, tenant, subject):
        """Real delete of the key, then VACUUM and CHECKPOINT so the old tuple is not left in heap or WAL."""
        self.conn.execute("select kms_destroy(%s,%s)", (tenant, subject))
        self.conn.execute("vacuum keys")
        self.conn.execute("checkpoint")

    def is_destroyed(self, tenant, subject):
        return self.conn.execute("select 1 from destroyed where tenant_id=%s and subject_id=%s", (tenant, subject)).fetchone() is not None

    def key_hex(self, tenant, subject):
        """For the byte-residue test only; never print or log the result."""
        r = self.conn.execute("select key from keys where tenant_id=%s and subject_id=%s", (tenant, subject)).fetchone()
        return r[0] if r else None
