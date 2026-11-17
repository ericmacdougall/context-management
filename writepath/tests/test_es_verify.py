"""Offline unit test (no stores): the strict placeholder check (wp/es_verify.py, D4) fails each residue the previous
check passed. A fake Elasticsearch serves _cat/shards and per-copy _search (preference _shards:<n>|_only_nodes:<id>).

Control in every residue case: old_count() models the fd5d2a3 check (count of documents among the ids that lack
erased:true, as one search over the index would see them through one copy) and must report 0 there, so each case
shows a residue the old check passed. usage (from writepath/): python3 -m unittest tests.test_es_verify -v"""
import importlib
import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
GOOD = {"stream": "emlab2-wphard", "pg_port": "25660", "es_url": "http://127.0.0.1:19460",
        "pg_container": "emlab2-wphard-pg", "es_container": "emlab2-wphard-es"}
V62 = 2 ** 62
PH = {"erased": True}


class FakeES:
    """copies: {(index, shard, prirep, node): {"state": str, "docs": {id: (source, version)}}}"""

    def __init__(self, copies):
        self.copies = copies
        self.searches = []
        self.params = []

    def req(self, method, path, body=None, ok=(200,), **params):
        if path.startswith("/_cat/shards/"):
            pat = path[len("/_cat/shards/"):].rstrip("*")
            rows = [{"index": i, "shard": sh, "prirep": pr, "state": c["state"], "id": n}
                    for (i, sh, pr, n), c in self.copies.items() if i.startswith(pat)]
            return (200, rows) if rows else (404, {})
        if path.endswith("/_search"):
            index = path.strip("/").split("/")[0]
            pref = params["preference"]
            shard = pref.split("|")[0].split(":")[1]
            node = pref.split("|")[1].split(":")[1]
            self.searches.append((index, shard, node))
            self.params.append(params)
            c = [v for (i, sh, pr, n), v in self.copies.items() if i == index and sh == shard and n == node][0]
            ids = body["query"]["ids"]["values"]
            hits = [{"_id": i, "_source": c["docs"][i][0], "_version": c["docs"][i][1]} for i in ids if i in c["docs"]]
            # a complete single-copy response; c["partial"] overrides its metadata and drops every hit (review C F9)
            j = {"timed_out": False, "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0}, "hits": {"hits": hits}}
            if "partial" in c:
                j.update(c["partial"], hits={"hits": []})
                j = {k: v for k, v in j.items() if v is not None}
            return 200, j
        raise AssertionError(path)


def old_count(es, index, ids):
    """fd5d2a3: count(ids filter, must_not erased:true) over the index, answered by its primary copy."""
    prim = [c for (i, sh, pr, n), c in es.copies.items() if i == index and pr == "p" and c["state"] == "STARTED"]
    return sum(1 for c in prim for i in ids if i in c["docs"] and not c["docs"][i][0].get("erased"))


class StrictPlaceholders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        home = pathlib.Path(tempfile.mkdtemp()) / "home"
        (home / "conf").mkdir(parents=True)
        for k, v in GOOD.items():
            (home / "conf" / k).write_text(v + "\n")
        os.environ["WP_HOME"] = str(home)
        sys.path.insert(0, str(HERE))
        cls.EV = importlib.import_module("wp.es_verify")
        cls.F = importlib.import_module("wp.families")
        cls.base = cls.F.base_index("t1", "events")

    def es(self, primary, replica=None, rstate="STARTED", extra=None):
        c = {(self.base, "0", "p", "n1"): {"state": "STARTED", "docs": primary}}
        if replica is not None:
            c[(self.base, "0", "r", "n2")] = {"state": rstate, "docs": replica}
        c.update(extra or {})
        return FakeES(c)

    def run_page(self, es, ids=("a", "b")):
        return self.EV.check_page(es, "t1", "events", list(ids))

    def test_1_clean_passes_on_every_copy(self):
        es = self.es({"a": (PH, V62), "b": (PH, V62)}, {"a": (PH, V62), "b": (PH, V62)})
        self.assertEqual(sum(self.run_page(es).values()), 0)
        self.assertEqual(len(es.searches), 2, "each copy must be searched alone")

    def test_2_placeholder_still_carrying_content(self):
        es = self.es({"a": ({"erased": True, "content": "lunch with yolanda"}, V62), "b": (PH, V62)})
        self.assertEqual(old_count(es, self.base, ["a", "b"]), 0, "control: the old check passes it")
        self.assertEqual(self.run_page(es)["es:placeholder_content"], 1)

    def test_3_missing_placeholder(self):
        es = self.es({"b": (PH, V62)})
        self.assertEqual(old_count(es, self.base, ["a", "b"]), 0, "control: the old check passes it")
        self.assertEqual(self.run_page(es)["es:placeholder_missing"], 1)

    def test_4_version_below_2_62(self):
        es = self.es({"a": (PH, 7), "b": (PH, V62)})
        self.assertEqual(old_count(es, self.base, ["a", "b"]), 0, "control: the old check passes it")
        self.assertEqual(self.run_page(es)["es:placeholder_version"], 1)

    def test_5_stale_replica_copy(self):
        es = self.es({"a": (PH, V62), "b": (PH, V62)}, {"a": ({"content": "lunch with yolanda", "subject_id": "Z"}, 1), "b": (PH, V62)})
        self.assertEqual(old_count(es, self.base, ["a", "b"]), 0, "control: the old check passes it")
        r = self.run_page(es)
        self.assertEqual(r["es:placeholder_content"], 1)
        self.assertEqual(r["es:placeholder_version"], 1)
        self.assertEqual(r["es:placeholder_missing"], 0)

    def test_6_replica_missing_the_placeholder(self):
        es = self.es({"a": (PH, V62), "b": (PH, V62)}, {"b": (PH, V62)})
        self.assertEqual(self.run_page(es)["es:placeholder_missing"], 1)

    def test_7_copy_not_started_is_unverified(self):
        es = self.es({"a": (PH, V62), "b": (PH, V62)}, {}, rstate="INITIALIZING")
        r = self.run_page(es)
        self.assertEqual(r["es:copy_unverified"], 1)
        self.assertEqual(r["es:placeholder_missing"], 0, "an INITIALIZING copy is not searched")

    def test_8_no_started_base_copy_means_every_id_missing(self):
        es = FakeES({(self.base, "0", "p", "n1"): {"state": "RELOCATING", "docs": {}}})
        r = self.run_page(es)
        self.assertEqual(r["es:placeholder_missing"], 2)
        self.assertEqual(r["es:copy_unverified"], 1)

    def test_9_content_in_another_index_of_the_family(self):
        other = self.F.P + "t1-events-old"
        es = self.es({"a": (PH, V62), "b": (PH, V62)}, extra={(other, "0", "p", "n1"): {"state": "STARTED", "docs": {"a": ({"content": "x"}, 1)}}})
        self.assertEqual(self.run_page(es)["es:placeholder_content"], 1)

    def test_10_facts_only_for_fact_events(self):
        fb = self.F.base_index("t1", "facts")
        derive = lambda t, e: "f-" + e  # noqa: E731
        es = FakeES({(self.base, "0", "p", "n1"): {"state": "STARTED", "docs": {"a": (PH, V62), "b": (PH, V62)}},
                     (fb, "0", "p", "n1"): {"state": "STARTED", "docs": {"f-b": (PH, V62)}}})
        r = self.EV.check(es, "t1", [[("a", "message"), ("b", "fact")]], derive)
        self.assertEqual(sum(r.values()), 0)
        es.copies[(fb, "0", "p", "n1")]["docs"].clear()
        r = self.EV.check(es, "t1", [[("a", "message"), ("b", "fact")]], derive)
        self.assertEqual(r["es:placeholder_missing"], 1)

    def test_11_detail_holds_ids_only_and_is_bounded(self):
        ids = [f"i{n}" for n in range(30)]
        es = self.es({})
        detail = {}
        self.EV.check_page(es, "t1", "events", ids, detail=detail)
        self.assertEqual(len(detail["es:placeholder_missing"]), 20)

    # review C F9: a partial or incomplete search is not evidence of absence (red on 4fe3ac1)
    PARTIAL = {
        "timed_out": {"timed_out": True},
        "failed_shard": {"_shards": {"total": 1, "successful": 0, "skipped": 0, "failed": 1}},
        "terminated_early": {"terminated_early": True},
        "skipped_shard": {"_shards": {"total": 1, "successful": 1, "skipped": 1, "failed": 0}},
        "unallocated_shard": {"_shards": {"total": 1, "successful": 0, "skipped": 0, "failed": 0}},
        "no_shard_searched": {"_shards": {"total": 0, "successful": 0, "skipped": 0, "failed": 0}},
        "two_shards_answered": {"_shards": {"total": 2, "successful": 2, "skipped": 0, "failed": 0}},
        "no_shard_metadata": {"_shards": None},
    }

    def test_12_partial_search_of_another_family_index_is_unverified(self):
        other = self.F.P + "t1-events-old"
        for name, meta in self.PARTIAL.items():
            with self.subTest(name):
                es = self.es({"a": (PH, V62), "b": (PH, V62)}, extra={(other, "0", "p", "n1"): {
                    "state": "STARTED", "docs": {"a": ({"content": "x"}, 1)}, "partial": meta}})
                r = self.run_page(es)
                self.assertEqual(r["es:copy_unverified"], 1, f"{name}: an incomplete search counted as absence")
                self.assertEqual(r["es:placeholder_content"], 0)

    def test_13_partial_search_of_the_only_base_copy_proves_nothing(self):
        for name, meta in self.PARTIAL.items():
            with self.subTest(name):
                es = FakeES({(self.base, "0", "p", "n1"): {"state": "STARTED", "docs": {"a": (PH, V62), "b": (PH, V62)}, "partial": meta}})
                r = self.run_page(es)
                self.assertEqual(r["es:copy_unverified"], 1, name)
                self.assertEqual(r["es:placeholder_missing"], 2, f"{name}: no complete base copy shows any id placed")

    def test_14_every_search_refuses_partial_results(self):
        es = self.es({"a": (PH, V62), "b": (PH, V62)}, {"a": (PH, V62), "b": (PH, V62)})
        self.assertEqual(sum(self.run_page(es).values()), 0)
        self.assertEqual([p.get("allow_partial_search_results") for p in es.params], ["false", "false"])


if __name__ == "__main__":
    unittest.main()
