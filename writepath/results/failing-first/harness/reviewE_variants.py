"""Broken variants for review E (F4 hold class, F12 order): each removes ONE lock from wp/schema.sql of a COPY of the
tree, and the named live test must then fail. Refuses (exit 3) unless the pattern occurs exactly once.
usage: python3 -I reviewE_variants.py <tree copy> <variant>      (variants: python3 -I reviewE_variants.py --list)"""
import pathlib
import sys

VARIANTS = {
    # name: (tests that must fail, old text, new text)
    "apply_no_event_lock": ("E1", "  perform 1 from events e where e.tenant_id = t and e.event_id = any(cand) order by e.event_id for no key update;\n", ""),
    "link_no_event_lock": ("E1", "  perform 1 from events e where e.tenant_id = j.tenant_id and e.event_id = any(ids) order by e.event_id for share;\n", ""),
    "apply_no_subject_lock": ("E2 E3 E4", "    order by sj.subject_id for share;\n", "    order by sj.subject_id;\n"),
    "footprint_no_rescan_links": ("E3", "      union select m.subject_id from erasure_mentions m where m.tenant_id = t and m.event_id = any(cand))\n", ")\n"),
    "hold_place_no_lock": ("E2", "  perform 1 from subjects where tenant_id = t and subject_id = s for update;\n  insert into legal_holds", "  insert into legal_holds"),
    "mark_job_first": ("E6", "  select tenant_id, subject_id into j from erasure_jobs where job_id = jid;\n  if not found then raise exception 'no erasure job %'",
                       "  select tenant_id, subject_id into j from erasure_jobs where job_id = jid for update;\n  if not found then raise exception 'no erasure job %'"),
}

if __name__ == "__main__":
    if sys.argv[1:] == ["--list"]:
        for k, (t, _, _) in VARIANTS.items():
            print(k, t.replace(" ", ","))
        sys.exit(0)
    tree, name = pathlib.Path(sys.argv[1]), sys.argv[2]
    _, old, new = VARIANTS[name]
    p = tree / "wp" / "schema.sql"
    s = p.read_text()
    n = s.count(old)
    if n != 1:
        print(f"variant {name}: pattern found {n} times, not 1")
        sys.exit(3)
    p.write_text(s.replace(old, new))
    print(f"variant {name}: applied")
