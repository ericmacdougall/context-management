"""Row 19: secret and credential scanning before append (wp/secrets.py), thresholds in THRESHOLDS.md amendment 5.

Planted corpus: 11 detector families x 8 in-scope encodings x 10 instances. Every planted value is SYNTHETIC (random
characters in the provider's format); none is a real credential. Out-of-scope variants (reversed, ROT13, split across
two writes) are generated and reported separately as documented misses, never counted.
Clean corpus: 40-line chunks of the Python standard library (top-level modules) plus synthetic look-alikes.
Store part: a planted secret through log.append is refused (SecretDetected) and nothing is stored; an app that skips
the scanner (secret_scan '') is refused by the store (WPS21). Controls: ctl_nodecode, ctl_noscan (scanner bypass AND
the store check removed: the secret is stored). No model calls; results never contain a planted value, only counts and
family/encoding names. usage: python -m exp.row19_secrets"""
import base64
import codecs
import glob
import json
import math
import random
import string
import sys
import urllib.parse
import uuid

from exp.common import write_result
from exp.patches import variant, patched
from exp.util import tenant, msg, session, env
from wp import db, log, secrets as SEC

RNG = random.Random(19019)
A, AN, UP, HEXC = string.ascii_letters, string.ascii_letters + string.digits, string.ascii_uppercase + string.digits, "0123456789abcdef"


def r(chars, n):
    return "".join(RNG.choice(chars) for _ in range(n))


def b64url(n):
    return r(AN + "-_", n)


FAMILIES = {
    "aws_access_key_id": lambda: "AKIA" + r(UP, 16),
    "aws_secret_key": lambda: "aws_secret_access_key = " + r(AN + "/+", 40),
    "github_token": lambda: "ghp_" + r(AN, 36),
    "slack_token": lambda: "xoxb-" + r(string.digits, 12) + "-" + r(string.digits, 12) + "-" + r(AN, 24),
    "stripe_key": lambda: "sk_live_" + r(AN, 24),
    "llm_api_key": lambda: "sk-ant-api03-" + b64url(60),
    "google_api_key": lambda: "AIza" + b64url(35),
    "private_key_block": lambda: "-----BEGIN RSA PRIVATE KEY-----\n" + r(AN + "+/", 64) + "\n-----END RSA PRIVATE KEY-----",
    "jwt": lambda: "eyJ" + b64url(20) + ".eyJ" + b64url(40) + "." + b64url(43),
    "url_credentials": lambda: "postgres://svc_user:" + r(AN, 10) + "9!x" + r(AN, 6) + "@db.internal.example:5432/app",
    "generic_assignment": lambda: 'client_secret: "' + r(AN, 10) + r(string.digits, 4) + r("!@#%^&", 2) + r(AN, 8) + '"',
}


def zw(sec):
    return "​".join(sec[i:i + 4] for i in range(0, len(sec), 4))


def split(sec):
    return "\n".join(sec[i:i + 10] for i in range(0, len(sec), 10))


def uesc(sec):
    return "".join("\\u%04x" % ord(c) for c in sec)


def wrap(sec):
    return f"note for later: {sec} (from the setup doc)"


ENCODINGS = {
    "plain": lambda s: wrap(s),
    "base64": lambda s: "blob " + base64.b64encode(wrap(s).encode()).decode(),
    "hex": lambda s: "dump " + wrap(s).encode().hex(),
    "percent": lambda s: "q=" + urllib.parse.quote(wrap(s), safe=""),
    "base64x2": lambda s: "blob " + base64.b64encode(base64.b64encode(wrap(s).encode())).decode(),
    "zero_width": lambda s: wrap(zw(s)),
    "line_split": lambda s: wrap(split(s)),
    "unicode_escape": lambda s: '{"v": "' + uesc(s) + '"}',
}
OUT_OF_SCOPE = {
    "reversed": lambda s: wrap(s[::-1]),
    "rot13": lambda s: wrap(codecs.encode(s, "rot13")),
    "split_two_writes_first_half": lambda s: wrap(s[: len(s) // 2]),
}


def wilson(k, n, z=1.96):
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round((c - m) / d, 4), round((c + m) / d, 4))


def planted(decode=True, n=10):
    per, total, caught, oos = {}, 0, 0, {}
    for fam, gen in FAMILIES.items():
        for enc, f in ENCODINGS.items():
            k = sum(1 for _ in range(n) if SEC.scan(f(gen()), decode=decode))
            per[f"{fam}/{enc}"] = k
            total += n
            caught += k
        for enc, f in OUT_OF_SCOPE.items():
            oos[f"{fam}/{enc}"] = sum(1 for _ in range(n) if SEC.scan(f(gen()), decode=decode))
    return {"items": total, "caught": caught, "wilson95": wilson(caught, total), "missed_cells": {k: v for k, v in per.items() if v < n},
            "out_of_scope_caught": oos}


def clean():
    chunks = []
    for fn in sorted(glob.glob("/usr/lib/python3.12/*.py")):
        lines = open(fn, encoding="utf-8", errors="replace").read().splitlines()
        for i in range(0, len(lines), 40):
            chunks.append((fn + f":{i + 1}", "\n".join(lines[i:i + 40])))
    syn = []
    for i in range(100):
        syn.append(("uuid", str(uuid.UUID(int=RNG.getrandbits(128)))))
        syn.append(("git_sha", r(HEXC, 40)))
        syn.append(("sha256", r(HEXC, 64)))
        syn.append(("b64_prose", base64.b64encode(f"the meeting moved to room {i} on tuesday afternoon".encode()).decode()))
        syn.append(("url", f"https://example.com/search?q=item{i}&page={i % 7}&sort=desc"))
    syn += [("placeholder", 'password = "<your-password-here>"'), ("placeholder", "api_key: ${API_KEY}"), ("placeholder", 'secret = "xxxxxxxxxxxxxxxx"'),
            ("aws_doc_example", "AKIAIOSFODNN7EXAMPLE"), ("aws_doc_example", "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")]
    blocked = []
    for name, text in chunks + syn:
        f = SEC.scan(text)
        if f:
            blocked.append({"where": name, "detectors": sorted(f)})
    n = len(chunks) + len(syn)
    return {"chunks_stdlib": len(chunks), "synthetic": len(syn), "items": n, "blocked": len(blocked), "wilson95": wilson(len(blocked), n),
            "blocked_where": blocked[:50]}


def store_part():
    conn, ks, es = env()
    adm = db.admin()
    out = {}
    secret = FAMILIES["github_token"]()

    def counts(t):
        return adm.execute("select (select count(*) from events where tenant_id=%s), (select count(*) from quarantine where tenant_id=%s)", (t, t)).fetchone()

    # fenced: the scanner refuses
    t = tenant("r19a"); s = session(conn, t)
    try:
        log.append(conn, ks, s, [msg(0, wrap(secret), "U")]); out["fenced_scanner"] = "stored"
    except SEC.SecretDetected as e:
        out["fenced_scanner"] = "refused:" + str(e).split(" detectors ")[-1]
    out["fenced_scanner_rows"] = counts(t)
    # fenced: an app without the scanner (secret_scan '') is refused by the store
    real = SEC.check
    SEC.check = lambda texts, where="event": ""
    try:
        t = tenant("r19b"); s = session(conn, t)
        try:
            log.append(conn, ks, s, [msg(0, wrap(secret), "U")]); out["fenced_store"] = "stored"
        except Exception as e:  # noqa: BLE001
            out["fenced_store"] = "refused:" + str(getattr(e, "sqlstate", type(e).__name__))
        out["fenced_store_rows"] = counts(t)
        # control: scanner bypassed AND the store check removed
        ap, rv = variant("events_guard", "if NEW.secret_scan is distinct from wp_secret_scan_version() then", "if false then")
        with patched(ap, rv):
            t = tenant("r19c"); s = session(conn, t)
            try:
                log.append(conn, ks, s, [msg(0, wrap(secret), "U")]); out["ctl_noscan"] = "stored"
            except Exception as e:  # noqa: BLE001
                out["ctl_noscan"] = "refused:" + str(getattr(e, "sqlstate", type(e).__name__))
            out["ctl_noscan_rows"] = counts(t)
    finally:
        SEC.check = real
    del secret
    return out


def main():
    res = {"scanner_version": SEC.VERSION, "planted": planted(), "clean": clean(), "ctl_nodecode": planted(decode=False), "store": store_part()}
    p, c, nd, st = res["planted"], res["clean"], res["ctl_nodecode"], res["store"]
    checks = {
        "catch_lb_ge_0.98": p["wilson95"][0] >= 0.98,
        "false_block_ub_le_0.01": c["wilson95"][1] <= 0.01,
        "ctl_nodecode_violated": nd["wilson95"][0] < 0.98,
        "fenced_scanner_refused": st["fenced_scanner"].startswith("refused") and tuple(st["fenced_scanner_rows"]) == (0, 0),
        "fenced_store_refused_WPS21": st["fenced_store"] == "refused:WPS21" and tuple(st["fenced_store_rows"]) == (0, 0),
        "ctl_noscan_violated": st["ctl_noscan"] == "stored",
    }
    res["checks"] = checks
    res["pass"] = all(checks.values())
    print("row19", "PASS" if res["pass"] else "FAIL", json.dumps(checks))
    print("  planted", p["caught"], "/", p["items"], p["wilson95"], "missed", json.dumps(p["missed_cells"])[:600])
    print("  out_of_scope", json.dumps({k: v for k, v in p["out_of_scope_caught"].items() if v})[:400])
    print("  clean", c["blocked"], "/", c["items"], c["wilson95"], json.dumps(c["blocked_where"])[:800])
    print("  ctl_nodecode", nd["caught"], "/", nd["items"], nd["wilson95"])
    print("  store", json.dumps(st, default=str))
    write_result("row19_secrets", res)


if __name__ == "__main__":
    sys.exit(main())
