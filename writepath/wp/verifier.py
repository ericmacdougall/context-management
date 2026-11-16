"""Erasure verifier: the only code that can complete an erasure job.

It runs as role wp_verifier (read-only everywhere except EXECUTE on wp_erasure_complete) with its own connections
and its own ES client, so a job is never completed on the runner's word. It
  1. runs the ES expunge itself (families.expunge) after the runner's placeholders, so the overwritten bytes are gone;
  2. counts the subject's documents in every registered ES family (every index of the family, aliased or not),
     by subject id (no id lists: no terms-query limit at any subject size);
  3. checks the key service for the subject's key (column privilege: it sees that a key exists, never the key);
  4. runs the unregistered-store gate (registry.unregistered) and counts what it finds;
  5. calls wp_erasure_complete, which recomputes the Postgres residue inside the store and writes 'done' only when
     every registered verifiable store is present and zero.
Cross-store counts (ES, key service) are ATTESTED by this role; Postgres counts are ENFORCED by the store."""
import json

from . import config, db, families as F, registry
from .es import ES

SCOPE = ("verifier: ES counted by subject_id OR mentions over every index of every family; key presence in the key store "
         "(see key_store in the receipt: the lab key store is a STAND-IN for a KMS). The stated erasure scope (speaker plus "
         "mentions found by the matcher) is computed by the store in wp_erasure_complete.")


class Verifier:
    def __init__(self, es=None):
        self.conn = db.verifier()
        self.kconn = db.verifier(db=config.KEYS_DB)
        self.es = es or ES()

    def ext_counts(self, t, s, expunge=True):
        out, info = {}, {}
        if expunge:
            try:
                info["expunge"] = F.expunge(self.es, t)
                out["es:soft_deleted"] = 0
            except TimeoutError:
                out["es:soft_deleted"] = 1
        else:
            out["es:soft_deleted"] = 1
        for f in F.FAMILIES:
            pat = f"{F.P}{t}-{f}-*"
            self.es.req("POST", f"/{pat}/_refresh", ok=(200, 404), expand_wildcards="all", ignore_unavailable=True, allow_no_indices=True)
            q = {"bool": {"should": [{"term": {"subject_id": s}}, {"term": {"mentions": s}}], "minimum_should_match": 1}}
            out[f"es:{f}"] = self.es.count(pat, q)
        out["keys:subject_keys"] = self.kconn.execute("select count(*) from subject_keys where tenant_id=%s and subject_id=%s", (t, s)).fetchone()[0]
        unreg = registry.unregistered(self.conn, self.kconn, self.es)
        out["gate:unregistered"] = len(unreg)
        info["unregistered"] = unreg[:20]
        return out, info

    def complete(self, jid, expunge=True):
        t, s = self.conn.execute("select tenant_id, subject_id from erasure_jobs where job_id=%s", (jid,)).fetchone()
        ext, info = self.ext_counts(t, s, expunge=expunge)
        res = self.conn.execute("select wp_erasure_complete(%s, %s, %s)", (jid, json.dumps(ext), SCOPE)).fetchone()[0]
        res.update(info)
        return res
