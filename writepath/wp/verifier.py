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
import time

from . import config, db, es_verify as EV, families as F, keys as K, registry
from .es import ES

SCOPE = ("verifier: ES counted by subject_id OR mentions over every index of every family; key presence in the key store "
         "(see key_store in the receipt: the lab key store is a STAND-IN for a KMS). The stated erasure scope (speaker plus "
         "mentions found by the matcher) is computed by the store in wp_erasure_complete.")


class Verifier:
    def __init__(self, es=None):
        self.conn = db.verifier()
        self.kconn = db.verifier(db=config.KEYS_DB)
        self.es = es or ES()

    def ext_counts(self, t, s, expunge=True, wait_s=180.0):
        out, info = {}, {}
        tm, t0 = {}, time.time()
        if expunge:
            try:
                info["expunge"] = F.expunge(self.es, t, wait_s=wait_s)
                out["es:soft_deleted"] = 0
            except TimeoutError:
                out["es:soft_deleted"] = 1
        else:
            out["es:soft_deleted"] = 1
        tm["expunge_s"], t0 = round(time.time() - t0, 3), time.time()
        for f in F.FAMILIES:
            pat = f"{F.P}{t}-{f}-*"
            self.es.req("POST", f"/{pat}/_refresh", ok=(200, 404), expand_wildcards="all", ignore_unavailable=True, allow_no_indices=True)
            q = {"bool": {"should": [{"term": {"subject_id": s}}, {"term": {"mentions": s}}], "minimum_should_match": 1}}
            out[f"es:{f}"] = self.es.count(pat, q)
        tm["es_counts_s"], t0 = round(time.time() - t0, 3), time.time()
        # D4 (strict, wp/es_verify.py): every tombstoned event (own, write-time mention, rescan hit) and every derived
        # fact of a tombstoned fact event must be PRESENT as an exact placeholder ({"erased": true} at version 2^62) on
        # every started copy; content, a lower version, a missing id or an unsearchable copy each count
        detail = {}
        out.update(EV.check(self.es, t, self._tombstoned(t, s), K.derive_id, detail))
        info["placeholder_detail"] = detail
        tm["placeholder_check_s"], t0 = round(time.time() - t0, 3), time.time()
        out["keys:subject_keys"] = self.kconn.execute("select count(*) from subject_keys where tenant_id=%s and subject_id=%s", (t, s)).fetchone()[0]
        # item 4: attested by the key service (purge() sets purged_at); a destroyed key never purged counts 1
        out["keys:physical_pending"] = self.kconn.execute("select count(*) from destroyed where tenant_id=%s and subject_id=%s and purged_at is null",
                                                          (t, s)).fetchone()[0]
        unreg = registry.unregistered(self.conn, self.kconn, self.es)
        out["gate:unregistered"] = len(unreg)
        info["unregistered"] = unreg[:20]
        tm["keys_and_gate_s"] = round(time.time() - t0, 3)
        info["timings"] = tm
        return out, info

    def _tombstoned(self, t, s, page=5000):
        """Pages of (event_id, kind), keyset-paged; the same ids the cascade placed (erasure.tombstoned_ids)."""
        last = ""
        while True:
            rows = self.conn.execute("select tb.event_id, e.kind from tombstones tb join events e on e.tenant_id = tb.tenant_id "
                                     "and e.event_id = tb.event_id where tb.tenant_id=%s and tb.subject_id=%s and tb.event_id > %s "
                                     "order by tb.event_id limit %s", (t, s, last, page)).fetchall()
            if not rows:
                return
            yield rows
            last = rows[-1][0]

    def complete(self, jid, expunge=True, wait_s=180.0):
        """Counts and submits; the store answers done, or 'logically_erased_physical_purge_pending' (every logical count
        zero, only es:soft_deleted / keys:physical_pending outstanding), or residue. wait_s bounds the expunge's wait for
        the ES retention leases (a short wait returns the logical receipt quickly; sweep() completes it later)."""
        t, s = self.conn.execute("select tenant_id, subject_id from erasure_jobs where job_id=%s", (jid,)).fetchone()
        ext, info = self.ext_counts(t, s, expunge=expunge, wait_s=wait_s)
        t0 = time.time()
        res = self.conn.execute("select wp_erasure_complete(%s, %s, %s)", (jid, json.dumps(ext), SCOPE)).fetchone()[0]
        info["timings"]["pg_complete_s"] = round(time.time() - t0, 3)
        res.update(info)
        return res
