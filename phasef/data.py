"""Pilot data access. Only the 10 micro-pilot records are ever materialised after selection."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "eval"))
from emlab.dataset import LongMemEvalLoader, build_rounds, parse_date  # noqa: E402  (eval/ is read-only)

PILOT_IDS = ["078150f1", "09ba9854_abs", "6e984301", "6e984302", "07741c44", "07741c45",
             "0862e8bf", "311778f1", "0e5e2d1a", "2bf43736"]  # from select_pilot.py, fixed rule
FACT_QIDS = ["6e984301", "6e984302", "07741c44", "07741c45"]  # the temporal and knowledge-update ones


def extract_records(path, ids):
    with open(path, "r", encoding="utf-8") as fh:
        return [r for r in json.load(fh) if str(r["question_id"]) in set(ids)]


def write_pilot_files(s_path, oracle_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for src, name in ((s_path, "pilot10_s.json"), (oracle_path, "pilot10_oracle.json")):
        recs = extract_records(src, PILOT_IDS)
        assert len(recs) == len(PILOT_IDS), (name, len(recs))
        with open(os.path.join(out_dir, name), "w") as fh:
            json.dump(recs, fh)


def load(path):
    with open(path) as fh:
        return {str(r["question_id"]): r for r in json.load(fh)}


def sessions_of(rec):
    """[(session_id, date_str, turns)] with a repeated id inside one haystack dropped (first kept), as the eval loader does."""
    seen, out = set(), []
    for sid, d, s in zip(rec["haystack_session_ids"], rec["haystack_dates"], rec["haystack_sessions"]):
        if sid in seen:
            continue
        seen.add(sid)
        out.append((sid, d, s))
    return out


def render_session(sid, date_str, turns):
    lines = ["### Session %s | date: %s" % (sid, date_str)]
    for t in turns:
        lines.append("%s: %s" % (t["role"], t["content"]))
    return "\n".join(lines)


def chrono(sessions):
    return sorted(sessions, key=lambda x: parse_date(x[1]))  # stable: ties keep haystack order


def render_sessions(sessions):
    return "\n\n".join(render_session(*s) for s in sessions)


def est_tokens(text):
    return int(len(text) / 3.5)  # conservative chars-per-token estimate; replaced by measured usage after a call


def rounds_of(sid, turns, date_str):
    from emlab.dataset import Turn
    d = parse_date(date_str)
    ts = [Turn(sid, i, t["role"], t["content"], bool(t.get("has_answer", False)), d) for i, t in enumerate(turns)]
    return build_rounds(sid, ts)
