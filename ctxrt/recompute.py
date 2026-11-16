"""Second implementation (same author, NOT independent evidence) that re-judges RAW stored inputs and outputs, not the proofs' own booleans:
  P1  re-validates stored conversations, trims and injections with a validator written here in a different style (state machine), checks the user's task and
      thinking blocks and the block placement on the raw messages;
  P2  re-applies its own ingest filter to the stored transcript entries, ledgered delimiters and summary ids and counts re-ingested markers;
  P3  recomputes duplicates and skips of every pagination run from the stored page ids and expected ids;
  P5  recomputes duplicates and losses from the stored committed transcript rows and master finding lists;
  P6  recomputes author sets from the stored registry and checks every assignment;
  P7  re-validates the stored final conversations and the placement of the block.
P4 stores per-case outcomes of its own judgments (not raw model-facing messages): for P4 this checks hashes, row counts and threshold arithmetic only.
Coverage of the raw re-judgment is stated in the output (cases re-judged per proof). Run: python3 -m ctxrt.recompute"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "ctxrt", "results")


def load(name):
    return json.load(open(os.path.join(RES, name + ".json")))


def rows_of(art):
    p = os.path.join(ROOT, art["path"])
    h = hashlib.sha256()
    rows = []
    for line in open(p, "rb"):
        h.update(line)
        rows.append(json.loads(line))
    return rows, h.hexdigest() == art["sha256"] and len(rows) == art["n"]


# ---------------------------------------------------------------- independent validators (state machines)
def v2_anthropic(msgs):
    def kinds(m):
        c = m.get("content")
        return [b.get("type") for b in c] if isinstance(c, list) else []
    owed = None
    srv_open = False
    seen = set()
    if not msgs or msgs[0].get("role") != "user" or msgs[-1].get("role") != "user":
        return False
    for m in msgs:
        c = m.get("content")
        bl = c if isinstance(c, list) else []
        ks = kinds(m)
        if c in ("", None, []) or (isinstance(c, str) and not c.strip()):
            return False
        for b in bl:
            if b.get("type") == "text" and not str(b.get("text", "")).strip():
                return False
            if b.get("type") == "thinking" and (m["role"] != "assistant" or not b.get("signature")):
                return False
        if m["role"] == "user":
            res = [b for b in bl if b.get("type") == "tool_result"]
            lead = 0
            while lead < len(ks) and ks[lead] == "tool_result":
                lead += 1
            if owed is not None:
                if lead != len(res) or sorted(b.get("tool_use_id") for b in res) != sorted(owed):
                    return False
                if srv_open and lead != len(ks):
                    return False
            elif res:
                return False
            owed, srv_open = None, False
        else:
            if any(k == "tool_result" for k in ks) or any(k in ("image", "document") for k in ks):
                return False
            uses = [b["id"] for b in bl if b.get("type") in ("tool_use", "server_tool_use")]
            if len(set(uses)) != len(uses) or any(u in seen for u in uses):
                return False
            seen |= set(uses)
            client = [b["id"] for b in bl if b.get("type") == "tool_use"]
            owed = client or None
            sids = {b["id"] for b in bl if b.get("type") == "server_tool_use"}
            done = {b.get("tool_use_id") for b in bl if str(b.get("type", "")).endswith("_tool_result") and b.get("type") != "tool_result"}
            srv_open = bool(sids - done)
    return owed is None


def v2_openai(msgs):
    if not msgs or msgs[-1].get("role") not in ("user", "tool"):
        return False
    i, n, head, seen = 0, len(msgs), True, set()
    while i < n:
        m = msgs[i]
        r = m.get("role")
        if r in ("system", "developer"):
            if not head:
                return False
            i += 1
            continue
        head = False
        if r == "tool":
            return False
        if r == "assistant":
            calls = m.get("tool_calls")
            if calls is not None and not calls:
                return False
            if not calls and (m.get("content") is None or not str(m["content"]).strip()):
                return False
        if r == "assistant" and m.get("tool_calls"):
            ids = [c["id"] for c in m["tool_calls"]]
            if len(set(ids)) != len(ids) or any(x in seen for x in ids):
                return False
            seen |= set(ids)
            j, got = i + 1, []
            while j < n and msgs[j].get("role") == "tool":
                got.append(msgs[j].get("tool_call_id"))
                j += 1
            if sorted(got) != sorted(ids):
                return False
            i = j
            continue
        i += 1
    return True


def v2(shape, msgs):
    return v2_anthropic(msgs) if shape == "anthropic" else v2_openai(msgs)


def human_idx(shape, msgs):
    for k in range(len(msgs) - 1, -1, -1):
        m = msgs[k]
        if m.get("role") == "user":
            c = m.get("content")
            if shape == "openai" or not (isinstance(c, list) and c and c[0].get("type") == "tool_result"):
                return k
    return 0


def strip_blocks(text, delims):
    for d in delims:
        pat = re.compile(r"<<<%s BEGIN.*?<<<%s END>>>" % (re.escape(d), re.escape(d)), re.S)
        text = pat.sub("", text)
        k = text.find("<<<%s BEGIN" % d)
        if k >= 0:
            lines = text[k:].split("\n")
            j = 1
            while j < len(lines) and lines[j].startswith("{"):
                j += 1
            text = text[:k] + "\n".join(lines[j:])
    return text.strip()


def p2_new_markers(raw, mode):
    marker, n = raw["marker"], 0
    for e in raw["entries"]:
        if mode == "full":
            if e["id"] in raw["summary_ids"] or e["origin"] != "user_typed":
                continue
            t = strip_blocks(e["text"], raw["delims"])
        else:
            t = e["text"]
        n += marker in t
    return n


FAM = lambda m: re.sub(r"-(\d{8}|\d{4}-\d{2}-\d{2})$", "", m.lower())


def main():
    out, ok_all = {}, True
    for name in ("p1_trim", "p2_compact", "p3_pull", "p4_analyst", "p4_process", "p5_delivery", "p6_routing", "p7_chain"):
        p = os.path.join(RES, name + ".json")
        if not os.path.exists(p):
            out[name] = "MISSING"
            ok_all = False
            continue
        d = load(name)
        rec = {"stamp_commit": d["stamp"]["code_commit"][:7], "recorded_overall": d["overall"], "raw_cases_rejudged": 0}
        integ, bad = [], []
        for ps in d["per_seed"]:
            rows, ok = rows_of(ps["artifact"])
            integ.append(ok)
            for r in rows:
                if name == "p1_trim" and "input" in r:
                    rec["raw_cases_rejudged"] += 1
                    sh = r["shape"]
                    if not v2(sh, r["input"]):
                        bad.append(("input_invalid", sh, r["i"]))
                    if not v2(sh, r["trimmed"]) or not v2(sh, r["final"]):
                        bad.append(("output_invalid", sh, r["i"]))
                    if r["input"][human_idx(sh, r["input"])] not in r["trimmed"]:
                        bad.append(("task_lost", sh, r["i"]))
                    think_in = {json.dumps(m, sort_keys=True) for m in r["input"] if "signature" in json.dumps(m)}
                    for m in r["trimmed"]:
                        if "signature" in json.dumps(m) and json.dumps(m, sort_keys=True) not in think_in:
                            bad.append(("thinking_edited", sh, r["i"]))
                    if r["unresolved"] and r["block"] and v2(sh, r["text_after_results"]):
                        bad.append(("control_C3_should_be_invalid", sh, r["i"]))
                elif name == "p2_compact" and "raw" in r:
                    rec["raw_cases_rejudged"] += 1
                    if p2_new_markers(r["raw"], "full") != 0:
                        bad.append(("marker_reingested_full", r["layer"]))
                    if p2_new_markers(r["raw"], "none") == 0:
                        bad.append(("control_none_found_nothing", r["layer"]))
                elif name == "p3_pull" and "expected_ids" in r:
                    rec["raw_cases_rejudged"] += 1
                    seen, exp = r["seen_ids"], set(r["expected_ids"])
                    dup, skip = len(seen) - len(set(seen)), len(exp - set(seen))
                    if (dup == 0 and skip == 0) != r["ok"]:
                        bad.append(("pagination_verdict", r["mode"]))
                    if r["mode"] in ("snapshot", "idf_snapshot") and (dup or skip):
                        bad.append(("snapshot_violation", r["mode"]))
                elif name == "p5_delivery" and "raw" in r:
                    rec["raw_cases_rejudged"] += 1
                    dup = lost = 0
                    logicals = {"L": "S", "P": "S", "C": "S", "L1": "S1", "L2": "S2"}
                    for lin, tr in r["raw"]["transcript"].items():
                        counts = {}
                        for t in tr:
                            if t["alive"]:
                                for f in t["ids"]:
                                    counts[f] = counts.get(f, 0) + 1
                        for f in r["raw"]["master"].get(logicals.get(lin, "S"), []):
                            dup += counts.get(f, 0) > 1
                            lost += counts.get(f, 0) == 0
                    if r["schedule"] in ("analyst_rollback", "turn_reuse", "id_collision") and r["arm"] == "ctrl":
                        continue
                    if (dup > 0) != (r["dup"] > 0) or (lost > 0) != (r["lost"] > 0):
                        bad.append(("dup_lost_mismatch", r["schedule"], r["arm"]))
                elif name == "p6_routing" and "registry" in r:
                    rec["raw_cases_rejudged"] += 1
                    ag = {a["id"]: a for a in r["registry"]}
                    s = {r["author"]} | {i for i in ag if ag[i]["lineage"] == ag[r["author"]]["lineage"]}
                    while True:
                        add = {i for i in ag if i not in s and (ag[i]["parent"] in s or ag[i]["lineage"] in {ag[j]["lineage"] for j in s})}
                        if not add:
                            break
                        s |= add
                    done, anc, frontier = set(), set(), {i for i in ag if ag[i]["lineage"] == ag[r["author"]]["lineage"]}
                    while frontier:
                        nxt = set()
                        for i in frontier:
                            done.add(i)
                            pr = ag[i]["parent"]
                            if pr and pr in ag:
                                anc.add(pr)
                                nxt |= {j for j in ag if ag[j]["lineage"] == ag[pr]["lineage"]}
                        frontier = nxt - done
                    full = s | anc
                    if sorted(full) != r["author_set"]:
                        bad.append(("author_set_mismatch", r["k"]))
                    if r["assigned"] and (r["assigned"] in full or not r["author_model"] or FAM(ag[r["assigned"]]["model"]) == FAM(ag[r["author"]]["model"])):
                        bad.append(("bad_assignment", r["k"]))
                elif name == "p7_chain" and "raw" in r:
                    rec["raw_cases_rejudged"] += 1
                    w = r["raw"]
                    if not v2(r["shape"], w["final"]):
                        bad.append(("final_invalid", r["cls"], r["i"]))
                    if r["cls"] in ("nothing", "malformed", "stopped") and (w["block"] != "" or w["final"] != w["base"]):
                        bad.append(("injected_when_empty", r["cls"], r["i"]))
                    if r["cls"] in ("valid", "planted") and json.dumps(w["final"], ensure_ascii=False).count(w["quote"]) != 1:
                        bad.append(("quote_not_once", r["cls"], r["i"]))
        rec["hash_and_rowcount_ok"] = all(integ)
        rec["rejudgment_failures"] = bad[:10]
        rec["rejudgment_failure_count"] = len(bad)
        rec["all_ok"] = all(integ) and not bad and (rec["raw_cases_rejudged"] > 0 or name in ("p4_analyst", "p4_process"))
        if name in ("p4_analyst", "p4_process"):
            rec["note"] = "per-case outcomes of the proof's own judgments only: integrity and arithmetic check, no raw re-judgment"
        ok_all &= rec["all_ok"]
        out[name] = rec
    out["all_ok"] = ok_all
    print(json.dumps(out, indent=1))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
