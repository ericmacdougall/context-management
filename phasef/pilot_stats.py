"""Size statistics of the 102 PILOT-split questions only (never test-split content): unique sessions and characters.
Usage: python3 -m phasef.pilot_stats <dataset path> <out json>"""
import json
import sys

from emlab.dataset import LongMemEvalLoader, select_split


def main(path, out):
    qs = list(LongMemEvalLoader(path).questions())
    pilot = select_split(qs, "pilot")
    del qs
    seen, per_q, refs = {}, [], 0
    for q in pilot:
        chars = {}
        for t in q.turns:
            chars[t.session_id] = chars.get(t.session_id, 0) + len(t.text)
        refs += len(chars)
        per_q.append(sum(chars.values()))
        for s, c in chars.items():
            seen.setdefault(s, c)
    res = {"pilot_questions": len(pilot), "session_refs": refs, "unique_sessions": len(seen), "unique_chars": sum(seen.values()),
           "mean_chars_per_unique_session": sum(seen.values()) / float(len(seen)),
           "mean_haystack_chars_per_question": sum(per_q) / float(len(per_q)), "min_max_haystack_chars": [min(per_q), max(per_q)]}
    json.dump(res, open(out, "w"), indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
