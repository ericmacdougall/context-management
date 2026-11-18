"""Review H F13 and F15 reproducers with the reviewer's exact inputs (owner a5bed869). usage:
WP_UCD_DIR=<ucd> python3 f13_f15_probe.py <writepath tree> <label>
Measures, in THIS process, against the matcher module of the given tree:
  F13  "I"*12 + "a"*65524 (one 65,536-byte token, 4,096 readings): admission verdict, then M.scan with a fake lookup
       (no identifiers) and a counting HMAC: tracemalloc peak, HMAC count, seconds.
  F13b 2,000 distinct long combining-mark clusters: bytes retained in M._CLUSTERS.
  F15  identifier "I"*n: M.capacity and M.check_identifiers seconds and verdict (refused before key construction?).
Prints numbers only; no content."""
import hashlib
import hmac
import sys
import time
import tracemalloc

tree, label = sys.argv[1], sys.argv[2]
sys.path.insert(0, tree)
from wp import mentions as M  # noqa: E402


class KS:
    def __init__(self):
        self.total = 0

    def tenant_hmac_many(self, tenant, texts):
        texts = list(texts)
        self.total += len(texts)
        return [hmac.new(b"probe", f"{tenant}|{x}".encode(), hashlib.sha256).hexdigest() for x in texts]


class Conn:
    def execute(self, sql, args=()):
        class R:
            def fetchone(self):
                return (1, None)

            def fetchall(self):
                return []
        return R()


M.key("warm up the tables")
print(f"[{label}] matcher {M.matcher_id()[:60]}")
s = "I" * 12 + "a" * 65524
try:
    M.admit([s])
    adm = "admitted"
except M.WriteRefused as e:
    adm = "refused " + e.code
ks = KS()
tracemalloc.start()
t0 = time.time()
try:
    M.scan(Conn(), ks, "d3t", [s])
    res = "scanned"
except Exception as e:  # noqa: BLE001
    res = type(e).__name__
cur, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
print(f"[{label}] F13 bytes={len(s.encode())} admission={adm} scan={res} hmacs={ks.total} peak_traced_mib={peak / 2**20:.1f} seconds={time.time() - t0:.2f}")

M._CLUSTERS.clear()
for i in range(2000):
    M.key_count(chr(0x100 + i % 128) + "".join(chr(0x300 + (j * 7 + i // 128) % 0x70) for j in range(29)))
size = 0
for c, v in M._CLUSTERS.items():
    parts = (v[0],) + tuple(v[1]) + tuple(v[2]) if isinstance(v, tuple) and len(v) == 3 else (v,)
    size += sys.getsizeof(c) + sum(sys.getsizeof(x) for x in parts)
print(f"[{label}] F13b cluster_cache_entries={len(M._CLUSTERS)} retained_bytes={size} limit_attr={getattr(M, 'CLUSTER_CACHE_BYTES', None)}")
M._CLUSTERS.clear()

for n in (13, 2000, 20000, 200000):
    x = "I" * n
    t0 = time.time()
    try:
        c = M.capacity(x)
        cap = f"bits={c.bit_length()}"
    except Exception as e:  # noqa: BLE001
        cap = type(e).__name__
    tc = time.time() - t0
    t0 = time.time()
    try:
        M.check_identifiers([x])
        ver = "accepted"
    except Exception as e:  # noqa: BLE001
        ver = type(e).__name__ + ": " + str(e)[:70]
    print(f"[{label}] F15 n={n} capacity {cap} {tc:.3f}s; check_identifiers {time.time() - t0:.3f}s -> {ver}")
