"""Red Postgres reproducers pinned to the 77ea895 pgcore API. Run on t2-dev against the rescue's own container (emlab-pg-polr-16096)
with POL_PG_PORT=25490 and PGPASSFILE pointing at the 0600 passfile (libpq reads it; the old code needs no change to connect).
Each test asserts the property the rescue must establish and fails at 77ea895 with the stated reason."""
import os
import time
import unittest

from pol import pgcore
from pol.testkit import LAB

PR = {"u1": "human", "u2": "human"}
AU = {"r": ["u1", "u2"], "big": ["u1"]}


def ev(type, id, **kw):
    d = {"type": type, "id": id, "idem": kw.pop("idem", id), "tenant": "T1"}
    if type in ("propose", "supersede"):
        d["label"] = dict(LAB)
    d.update(kw)
    return d


class PG(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert os.environ.get("PGPASSFILE"), "set PGPASSFILE (never a password in the environment)"
        cls.c = pgcore.connect()
        pgcore.setup(cls.c, {"T1": (PR, AU, ["u1"])})

    def test_m2_retry_returns_original_outcome(self):
        e = ev("supersede", "s1", req="r", principal="u1", value="V", channel="owner_ui", based_on=0)
        first = pgcore.submit(self.c, e)
        again = pgcore.submit(self.c, e)
        self.assertIn(first, str(again), "M2: retry after a lost response returned %r; the original outcome %r is not surfaced" % (again, first))

    def test_events_append_only_for_the_application_connection(self):
        pgcore.submit(self.c, ev("propose", "p1", req="r", principal="u1", value="A", based_on=0))
        mutated = []
        for sql in ("update dc_events set code='tampered'", "delete from dc_events where false or true", "truncate dc_events"):
            try:
                with self.c.transaction():
                    self.c.execute(sql)
                    mutated.append(sql.split()[0])
                    raise RuntimeError("rollback")
            except RuntimeError:
                pass
            except Exception:       # noqa: BLE001  refused by the database: the property holds for this statement
                pass
        self.assertEqual(mutated, [], "append-only: the application connection could %s the events table" % mutated)

    def test_events_hash_chained(self):
        cols = {r[0] for r in self.c.execute("select column_name from information_schema.columns where table_name='dc_events'").fetchall()}
        self.assertTrue({"prev_hash", "hash"} <= cols, "append-only: dc_events has no hash chain columns (%s)" % sorted(cols))

    def test_n2_pg_cost_per_event_does_not_grow_with_history(self):
        blk = []
        n, step = 1200, 200
        for b in range(n // step):
            t0 = time.perf_counter()
            for i in range(b * step, (b + 1) * step):
                pgcore.submit(self.c, ev("propose", "q%d" % i, req="big", principal="u1", value="v" * 50, based_on=0))
            blk.append(time.perf_counter() - t0)
        ratio = blk[-1] / blk[0]
        self.assertLess(ratio, 3.0, "N2 (Postgres): the head document is rewritten per event: last block %.2fs vs first %.2fs (x%.1f)" % (blk[-1], blk[0], ratio))


if __name__ == "__main__":
    unittest.main()
