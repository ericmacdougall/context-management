"""Experiment 4: start-up capability probe in every licence state, including a node that is still starting."""
import subprocess
import time

import exp2
import lib
from lib import req
from memcore import capabilities
from reprobe import ReprobingTier


class BackendErr(Exception):
    pass


def unit_cases():
    """Stub transport: the probe's answer for each malformed or unusual /_license answer."""
    L = lambda t, s: {"license": {"type": t, "status": s}}
    cases = {"basic_active": L("basic", "active"), "trial_active": L("trial", "active"), "trial_expired": L("trial", "expired"),
             "enterprise_active": L("enterprise", "active"), "enterprise_inactive": L("enterprise", "inactive"), "platinum_active": L("platinum", "active"),
             "gold_active": L("gold", "active"), "empty_dict": {}, "null_license": {"license": None}, "no_type": {"license": {"status": "active"}},
             "list_instead": {"license": []} if False else {"license": {"type": ["trial"], "status": "active"}}}
    out = {}
    for name, body in cases.items():
        paths = []

        def g(p, b=body):
            paths.append(p)
            return b
        r = capabilities.probe(g)
        out[name] = {"tier": r["tier"], "search_strategy": r["search_strategy"], "paths": paths}
    for name, exc in (("http_404", BackendErr("HTTP 404")), ("http_503", BackendErr("HTTP 503")), ("timeout", TimeoutError()), ("conn_refused", ConnectionRefusedError())):
        paths = []

        def g2(p, e=exc):
            paths.append(p)
            raise e
        r = capabilities.probe(g2)
        out[name] = {"tier": r["tier"], "probe_error": r.get("probe_error"), "search_strategy": r["search_strategy"], "paths": paths}
    return out


def state_probe():
    """Real node: the reviewed probe on the current licence, with the transport recorded."""
    s = exp2.make_adapter("auto", 5.0)
    paths = []

    def g(p):
        paths.append(p)
        return s._req("GET", p)
    rep = capabilities.probe(g)
    rb = lib.licence()
    enterprise_truth = rb["status"] == "active" and rb["type"] in ("trial", "enterprise")
    return {"report": {k: rep[k] for k in ("tier", "licence_type", "licence_status", "search_strategy")}, "probe_error": rep.get("probe_error"),
            "paths": paths, "readback": rb, "safe": (rep["tier"] == "enterprise") <= enterprise_truth, "exact": (rep["tier"] == "enterprise") == enterprise_truth}


def poll_ready(max_s=240, extra_s=12.0, sim=True, need_outage=False):
    """Poll GET /_license every ~100 ms until it answers 200, then extra_s more. At the first sight of each distinct status a 'service' starts
    (reviewed flow: probe once, capabilities.apply; and ReprobingTier). Reports the status sequence and each service's final strategy."""
    rows, sims, t_ok, seen_down = [], {}, None, False
    t0 = time.time()
    while time.time() - t0 < max_s:
        t = time.time()
        c, j, _ = req("GET", "/_license", timeout=1.5)
        typ = (j.get("license") or {}).get("type") if c == 200 and isinstance(j, dict) else None
        rows.append([round(t - t0, 3), c, typ])
        key = "refused" if c == 0 else ("ok" if c == 200 else "http%d" % c)
        if sim and key not in sims:
            rev = exp2.make_adapter("auto", 5.0)
            rep = capabilities.probe(capabilities.es_get_json(rev))
            capabilities.apply(rep, rev)
            rp = exp2.make_adapter("auto", 5.0)
            tr = ReprobingTier(rp, interval_s=3.0, backoff_s=1.0)
            tr.refresh()
            tr.start()
            sims[key] = {"t_start": t - t0, "reviewed": rev, "reprobe_search": rp, "tracker": tr, "first_tier": rep["tier"], "first_error": rep.get("probe_error"),
                         "reprobe_first_strategy": rp.strategy, "reviewed_first_strategy": rev.strategy, "t_reprobe_correct": None}
        if c != 200:
            seen_down = True
        if c == 200 and t_ok is None and (seen_down or not need_outage):
            t_ok = t
        if t_ok is not None:
            rb = lib.licence()
            want = "auto" if (rb["status"] == "active" and rb["type"] in ("trial", "enterprise")) else "client"
            for s in sims.values():
                if s["t_reprobe_correct"] is None and s["reprobe_search"].strategy == want:
                    s["t_reprobe_correct"] = t - t_ok
            if t - t_ok >= extra_s:
                break
        time.sleep(0.1)
    out = {"poll_rows_n": len(rows), "t_first_200_s": None if t_ok is None else round(t_ok - t0, 3)}
    seq, last = [], None
    for r in rows:
        k = (r[1], r[2])
        if k != last:
            seq.append(r)
            last = k
    out["status_sequence"] = seq[:40]
    rb = lib.licence()
    want = "auto" if (rb["status"] == "active" and rb["type"] in ("trial", "enterprise")) else "client"
    out["readback"] = rb
    out["expected_strategy"] = want
    out["services"] = {}
    for key, s in sims.items():
        s["tracker"].stop()
        out["services"][key] = {"t_start_s": round(s["t_start"], 3), "first_tier": s["first_tier"], "first_probe_error": s["first_error"],
                                "reviewed_final_strategy": s["reviewed"].strategy, "reviewed_correct": s["reviewed"].strategy == want,
                                "reprobe_final_strategy": s["reprobe_search"].strategy, "reprobe_correct": s["reprobe_search"].strategy == want,
                                "reprobe_seconds_to_correct_after_first_200": None if s["t_reprobe_correct"] is None else round(s["t_reprobe_correct"], 3),
                                "reprobe_history_n": len(s["tracker"].history)}
    return out


def restart_and_poll(container):
    p = subprocess.Popen(["docker", "restart", "-t", "20", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = poll_ready(need_outage=True)
    p.wait(timeout=120)
    return r
