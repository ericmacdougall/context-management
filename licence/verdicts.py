"""Verdicts over typed evidence only (DESIGN-VERDICTS.md). Each verdict function takes exactly one evidence object (evidence.py) and returns
Verdict(status, reason); it never sees a life dictionary, an integer HTTP status or a stored derived figure. PASS is returned only on a
branch that matched the intended observation. judge() parses first: evidence that does not parse is UNVERIFIED with the parse reason.
Thresholds are THRESHOLDS.md's as implemented, with six disclosed differences from the registered wording (summarize.LIMITS,
DESIGN-VERDICTS.md threshold-wording table); EXP3-T1/T4/T5 and EXP4 have no gate here."""
import collections

import evidence as E
from evidence import (Availability, Battery, Correctness, Dls, FlipDown, FlipUp, Latch, LicenceDown, LicenceUp, Migration, Moment, SemOnly,
                      WritesGate)

Verdict = collections.namedtuple("Verdict", "status reason")
PASS, FAIL, UNVERIFIED = "PASS", "FAIL", "UNVERIFIED"


def combine(verdicts):
    """FAIL if any failed; else UNVERIFIED if any is not PASS (or there are none); else PASS. Reasons of the deciding verdicts are kept."""
    vs = list(verdicts)
    for st in (FAIL, UNVERIFIED):
        bad = [v.reason for v in vs if v.status == st]
        if bad:
            return Verdict(st, "; ".join(bad))
    return Verdict(PASS, "all parts passed") if vs else Verdict(UNVERIFIED, "no evidence")


def ok(cond, reason_fail, reason_pass="as intended"):
    return Verdict(PASS, reason_pass) if cond else Verdict(FAIL, reason_fail)


def refusal(name, outcome, leaked=False):
    """After start_basic: an authorisation or licence refusal with nothing returned is fail-closed (PASS); success or a returned document
    is a FAIL; any other case (authentication failure, not found, client error, infrastructure, transport, unexpected) says nothing: UNVERIFIED."""
    if leaked or outcome.case == "SUCCESS":
        return Verdict(FAIL, "%s: served data after start_basic (%s)" % (name, outcome.case))
    if outcome.case in E.REFUSALS:
        return Verdict(PASS, "%s refused" % name)
    return Verdict(UNVERIFIED, "%s: %s is not a refusal" % (name, outcome.case))


def moment(e: Moment):
    # each first_refusal carries its Outcome, already enforced to the probe's required case (LICENCE_REFUSAL; dls: AUTHORISATION_REFUSAL)
    return combine(ok(getattr(e, p).first_refusal.at_s <= 5.0 and getattr(e, p).success_after_refusal == 0,
                      "%s: first %s %.3f s, %d successes after it" % (p, getattr(e, p).first_refusal.outcome.case,
                                                                      getattr(e, p).first_refusal.at_s, getattr(e, p).success_after_refusal))
                   for p in E.T2_PROBES)


def latch(e: Latch):
    # THRESHOLDS.md EXP1-T3: the adapter's _is_licence_error must be true for the message the adapter would build (checked in parsing)
    return combine(ok(getattr(e, c).detects, "%s: adapter detector %s on %r" % (c, getattr(e, c).detects, getattr(e, c).message[:60]))
                   for c in E.LATCH_CELLS)


_NO_DOC_WITNESS = ("no status-independent witness that no document was returned (docs_in_payload; recorded from review 12 on; "
                   "THRESHOLDS.md:26)")


def dls(e: Dls):
    a = e.after
    counts = {"in the answer": a.docs, "counted from 200 hits": a.returned, "in polled searches started after start_basic": e.polled_docs}
    shown = {k: v for k, v in counts.items() if v}
    if shown:   # THRESHOLDS.md:26: returning any document is a FAIL, whatever the status; positive evidence decides first
        return Verdict(FAIL, "dls_search_as_user: documents returned after start_basic (%s)" % ", ".join("%d %s" % (v, k) for k, v in shown.items()))
    if e.after.cell_class in ("UNVERIFIED_PREREQUISITE", "EMPTY_WITNESS", "SKIP"):   # matrix.NOT_OBSERVED
        return Verdict(UNVERIFIED, "after cell not established: %s" % e.after.cell_class)
    r = refusal("dls_search_as_user", e.after.outcome)
    if r.status == FAIL:   # a missing witness only withholds a PASS; it never softens a definite FAIL (a 2xx answer)
        return r
    missing = [k for k in ("in the answer", "in polled searches started after start_basic") if counts[k] is None]
    if missing:
        return Verdict(UNVERIFIED, "dls_search_as_user: %s: %s" % (_NO_DOC_WITNESS, ", ".join(missing)))
    return r


def battery(e: Battery):
    out = []
    for p in E.BATTERY:
        r = getattr(e.after, p)
        leaked = r.hidden or r.any_source or bool(r.docs)
        v = refusal(p, r.outcome, leaked)
        if v.status != FAIL and r.docs is None:   # a missing witness withholds a PASS, never a definite FAIL
            v = Verdict(UNVERIFIED, "%s: %s" % (p, _NO_DOC_WITNESS))
        out.append(v)
    return combine(out)


def correctness(e: Correctness):
    c = e.control
    return combine([ok(e.examined >= 20000, "examined %d < 20,000" % e.examined),
                    ok(e.mismatches == 0, "%d wrong answers" % e.mismatches),
                    ok(e.backend_without_oracle == 0, "%d backend answers without an oracle" % e.backend_without_oracle),
                    ok(c.flagged.rate >= 0.9, "control flagged %d of %d" % (c.flagged.numerator, c.flagged.denominator)),
                    ok(c.backend_wrong == 0, "control backend wrong %d" % c.backend_wrong)])


def availability(e: Availability):
    return combine([ok(e.failed_share.rate <= 0.001, "%d of %d reads failed" % (e.failed, e.requests)),
                    ok(e.longest_window_s <= 2.0, "longest failure window %.3f s" % e.longest_window_s),
                    ok(e.consecutive_auto_s <= 2.0, "longest run of consecutive failed auto requests %.3f s" % e.consecutive_auto_s)])


def flip_down(e: FlipDown):
    return combine([ok(e.flip_s <= 5.0, "flip %.3f s" % e.flip_s), ok(e.backend_after_client == 0, "%d backend answers after the flip" % e.backend_after_client)])


def flip_up(e: FlipUp):
    return combine([ok(e.flip_s <= e.ttl_s + 5.0, "flip %.3f s > ttl %.1f + 5" % (e.flip_s, e.ttl_s)),
                    ok(e.client_after_backend == 0, "%d client answers after the flip" % e.client_after_backend)])


def writes(e: WritesGate):
    w = e.writes
    lost = combine([ok(w.acked_absent == 0, "%d acknowledged writes absent" % w.acked_absent),
                    ok(w.sem_acked_absent == 0, "%d acknowledged semantic writes absent" % w.sem_acked_absent),
                    ok(w.sem_failed_present == 0, "%d failed semantic writes present" % w.sem_failed_present),
                    ok(w.loss_control is True, "injected-loss control not detected")])
    if lost.status != PASS:
        return lost
    if w.sem_failed == 0:
        return Verdict(PASS, "no write lost; no semantic write failed")
    if w.sem_failed_other is None:
        return Verdict(UNVERIFIED, "%d semantic write failures never classified (addendum D)" % w.sem_failed)
    return ok(w.sem_failed_other == 0 and w.sem_failed_loud == w.sem_failed,
              "%d of %d semantic failures not loud" % (w.sem_failed - w.sem_failed_loud, w.sem_failed))


def licence_down(e: LicenceDown):
    s = e.control.licence_share
    return ok(s.rate >= 0.95, "licence failures %d of %d" % (s.numerator, s.denominator))


def licence_up(e: LicenceUp):
    b, a = e.before.licence_share, e.after.success_share
    return combine([ok(b.rate >= 0.95, "licence failures before %d of %d" % (b.numerator, b.denominator)),
                    ok(a.numerator == a.denominator, "successes after %d of %d" % (a.numerator, a.denominator))])


def migration(e: Migration):
    b, bb, s, ind = e.bbq_to_hnsw, e.bbq_to_bbq_hnsw, e.semantic, e.independent
    parts = [
        ok(b.dst == b.src and b.mismatches == 0 and b.knn.rate >= 0.99 and b.src_vs_truth <= 1e-6, "bbq_disk_to_hnsw copy differs"),
        ok(bb.dst == bb.src and bb.mismatches == 0 and bb.knn.rate >= 0.99, "bbq_disk_to_bbq_hnsw copy differs"),
        ok(s.dst == s.docs and s.mismatches == 0 and s.abs_err <= 0.005 and s.cosine >= 0.9999 and s.knn.rate >= 0.99 and s.skipped == 0,
           "semantic_to_text_vector copy differs"),
        ok(e.plain_control.mismatches == 0 and e.plain_control.dst == e.plain_control.src, "plain control copy differs"),
        ok(e.corrupted_control.mismatches == 1, "corrupted control not detected"),
        ok(b.src == ind.bbq_disk and bb.src == ind.bbq_disk and s.docs + s.skipped == ind.sem and e.plain_control.src == ind.plain,
           "source counts differ from the independent counts"),
    ]
    for attr, f in E.MigratedOpsAll.SPEC.items():   # every declared index; the parse already required every operation of each
        for op in E.MIG_OPS:
            o = getattr(getattr(e.ops, attr), op)
            parts.append(ok(o.case == "SUCCESS", "%s %s: %s (%d)" % (f.path[0], op, o.case, o.status)))
    return combine(parts)


def semonly(e: SemOnly):
    return combine(ok(getattr(e, k).outcome.case == "SUCCESS", "%s: %s" % (k, getattr(e, k).outcome.case)) for k in ("semantic", "match", "match_all"))


Gate = collections.namedtuple("Gate", "scope report out evidence verdict doc")
GATES = {
    "EXP1-T2": Gate("down", "exp1", "T2", Moment, moment, "every mandatory probe's first refusal of its required Outcome case (licence probes LICENCE_REFUSAL, dls_search AUTHORISATION_REFUSAL), carried with its timing, within 5 s of start_basic; no success after it"),
    "EXP1-T3": Gate("down", "exp1", "T3_latch", Latch, latch, "the adapter's own _is_licence_error is true for the message it would build (HTTP <code>: first 400 bytes) on each latch cell"),
    "EXP1-T4": Gate("down", "exp1", "T4", Dls, dls, "positive restricted control on trial; after start_basic the DLS user's search is refused"),
    "EXP1-T4-battery": Gate("down", "exp1", "T4_battery", Battery, battery, "the same control and a trial battery control; every read path refused, nothing returned"),
    "EXP2-T1": Gate("both", "exp2", "T1", Correctness, correctness, "0 wrong answers over >= 20,000 examined; control flags >= 90% (computed from its counts)"),
    "EXP2-T2": Gate("both", "exp2", "T2", Availability, availability, "failed reads <= 0.1% (computed from counts); longest failure window <= 2 s"),
    "EXP2-T3-down": Gate("down", "exp2", "T3", FlipDown, flip_down, "first client-labelled answer within 5 s of start_basic; no backend label after it"),
    "EXP2-T3-up": Gate("up", "exp2", "T3", FlipUp, flip_up, "first backend-labelled answer within TTL + 5 s of start_trial; no client label after it"),
    "EXP2-T4": Gate("both", "exp2", "T4", WritesGate, writes, "no acknowledged write lost; every semantic write failure classified loud"),
    "EXP2-T5-down": Gate("down", "exp2", "T5", LicenceDown, licence_down, ">= 95% of control requests after 1 s are a LICENCE_REFUSAL (counted by Outcome case)"),
    "EXP2-T5-up": Gate("up", "exp2", "T5", LicenceUp, licence_up, ">= 95% LICENCE_REFUSAL (counted by Outcome case) before start_trial; every control request succeeds after it"),
    "EXP3-migration": Gate("down", "exp3", "migration", Migration, migration, "every migrated copy equal to its source; Basic operations on migrated indices succeed"),
    "EXP3-semonly-control": Gate("down", "exp3", "semonly_control", SemOnly, semonly, "semantic-only fixture answers semantic, match and match_all with hits"),
}


def judge(name, life):
    """Parse the life into the gate's evidence type, then judge it. Never raises for malformed input. Review 12: a registered clause of
    the gate that no field witnesses (a LIMIT row of clauses.CLAUSES) forces a PASS to UNVERIFIED, naming the clause; a FAIL stays."""
    g = GATES[name]
    ev, why = E.parse(g.evidence, life)
    if ev is None:
        return Verdict(UNVERIFIED, why)
    v = g.verdict(ev)
    if v.status == PASS:
        import clauses
        lim = clauses.limits_for(name)
        if lim:
            return Verdict(UNVERIFIED, "registered clause not witnessed: " + "; ".join("%s (%s)" % (c.cid, c.note) for c in lim))
    return v
