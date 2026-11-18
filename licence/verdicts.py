"""Verdicts over typed evidence only (DESIGN-VERDICTS.md). Each gate's verdict function takes exactly one evidence object (evidence.py)
and returns its CONJUNCTS: one Part(cid, state, reason) per separate condition of the registered requirement (review 13 audit,
licence/CLAUSE-AUDIT.md). A state is ESTABLISHED (positively shown by the evidence), VIOLATED (the evidence shows the condition false)
or NOT_ESTABLISHED (the evidence cannot show it: a witness is absent, a control is not met, the record cannot place an observation).
A gate is PASS only when every one of its conjuncts is ESTABLISHED, FAIL when any is VIOLATED, otherwise UNVERIFIED. There is no
"nothing seen, so nothing wrong" branch: each conjunct that quantifies over observations also requires the observations to exist.

judge() parses first: evidence that does not parse leaves every conjunct of the gate NOT_ESTABLISHED with the parse reason. A
registered clause no recorded field witnesses (a LIMIT row of clauses.CLAUSES) is a conjunct that is never established. Thresholds
are THRESHOLDS.md's as implemented, with the disclosed differences listed in summarize.LIMITS; EXP3-T1/T4/T5 and EXP4 have no gate."""
import collections

import clauses
import evidence as E
from evidence import (Availability, Battery, Correctness, Dls, FlipDown, FlipUp, Latch, LicenceDown, LicenceUp, Migration, Moment, SemOnly,
                      WritesGate)

Verdict = collections.namedtuple("Verdict", "status reason")
Part = collections.namedtuple("Part", "cid state reason")
PASS, FAIL, UNVERIFIED = "PASS", "FAIL", "UNVERIFIED"
ESTABLISHED, VIOLATED, NOT_ESTABLISHED = "ESTABLISHED", "VIOLATED", "NOT_ESTABLISHED"


def holds(cid, cond, bad):
    """A condition the evidence decides either way."""
    return Part(cid, ESTABLISHED, "established") if cond else Part(cid, VIOLATED, "%s: %s" % (cid, bad))


def shown(cid, cond, missing):
    """A witness or control: when it is not there, the condition is not established (never a pass, never a failure)."""
    return Part(cid, ESTABLISHED, "established") if cond else Part(cid, NOT_ESTABLISHED, "%s: %s" % (cid, missing))


def parsed(cid, what):
    """A condition the evidence type itself enforces: the object exists only if it holds (evidence.py, `enforce` and `pos`)."""
    return Part(cid, ESTABLISHED, "%s (enforced when the evidence parsed)" % what)


def worst(cid, parts):
    """One conjunct over several items (probes, paths, copies): VIOLATED if any item is, else NOT_ESTABLISHED if any is."""
    for st in (VIOLATED, NOT_ESTABLISHED):
        bad = [p.reason for p in parts if p.state == st]
        if bad:
            return Part(cid, st, "; ".join(bad))
    return Part(cid, ESTABLISHED, "established")


def combine(parts):
    """FAIL if any conjunct is VIOLATED; else UNVERIFIED if any is NOT_ESTABLISHED (or there are none); else PASS."""
    ps = list(parts)
    for state, status in ((VIOLATED, FAIL), (NOT_ESTABLISHED, UNVERIFIED)):
        bad = list(dict.fromkeys(p.reason for p in ps if p.state == state))   # one reason once (an unparsed record gives every part it)
        if bad:
            return Verdict(status, "; ".join(bad))
    return Verdict(PASS, "all conjuncts established") if ps else Verdict(UNVERIFIED, "no evidence")


# ---- EXP1 -----------------------------------------------------------------------------------------------------------------------
def moment(e: Moment):
    ps = [(p, getattr(e, p)) for p in E.T2_PROBES]
    return [
        parsed("EXP1-T2.refused-each", "every mandatory probe has a first refusal of its required Outcome case"),
        worst("EXP1-T2.within-5s", [holds(p, x.first_refusal.at_s <= 5.0, "first %s %.3f s after the response"
                                          % (x.first_refusal.outcome.case, x.first_refusal.at_s)) for p, x in ps]),
        worst("EXP1-T2.no-success-after", [holds(p, x.success_after_refusal == 0, "%d successes after its first refusal"
                                                 % x.success_after_refusal) for p, x in ps]),
        worst("EXP1-T2.observed-after", [shown(p, x.unobserved_after == 0, "%s polls after its first refusal did not observe it"
                                               % ("unknown" if x.unobserved_after is None else x.unobserved_after)) for p, x in ps]),
        worst("EXP1-T2.answered-before", [shown(p, bool(x.success_before) and x.refusals_before == 0,
                                                "answered before the call: %s, refused before the window: %s"
                                                % (x.success_before, x.refusals_before)) for p, x in ps]),
    ]


def latch(e: Latch):
    # THRESHOLDS.md EXP1-T3: the adapter's _is_licence_error must be true for the message the adapter would build (checked in parsing)
    return [parsed("EXP1-T3.message", "each latch cell carries the adapter message and the real detector's answer on it"),
            worst("EXP1-T3.detector", [holds(c, getattr(e, c).detects, "adapter detector %s on %r" % (getattr(e, c).detects,
                                                                                                      getattr(e, c).message[:60]))
                                       for c in E.LATCH_CELLS])]


def refusal(name, outcome):
    """After start_basic: a 2xx is a FAIL; an authorisation or licence refusal is established; any other case (authentication,
    not found, client error, infrastructure, transport, unexpected) says nothing."""
    if outcome.case == "SUCCESS":
        return Part(name, VIOLATED, "%s: served an answer after start_basic" % name)
    if outcome.case in E.REFUSALS:
        return Part(name, ESTABLISHED, "refused")
    return Part(name, NOT_ESTABLISHED, "%s: %s is not a refusal" % (name, outcome.case))


_NO_DOC_WITNESS = "no status-independent witness that no document was returned (docs_in_payload; THRESHOLDS.md:26)"


def dls(e: Dls):
    a = e.after
    counts = {"in the answer": a.docs, "counted from 200 hits": a.returned, "in polled searches started after start_basic": e.polled_docs}
    seen = {k: v for k, v in counts.items() if v}
    if seen:   # THRESHOLDS.md:26: returning any document is a FAIL, whatever the status; positive evidence decides first
        doc = Part("EXP1-T4.no-document", VIOLATED, "dls_search_as_user: documents returned after start_basic (%s)"
                   % ", ".join("%d %s" % (v, k) for k, v in seen.items()))
    else:
        missing = [k for k in ("in the answer", "in polled searches started after start_basic") if counts[k] is None]
        doc = shown("EXP1-T4.no-document", not missing, "dls_search_as_user: %s: %s" % (_NO_DOC_WITNESS, ", ".join(missing)))
    if a.cell_class in ("UNVERIFIED_PREREQUISITE", "EMPTY_WITNESS", "SKIP"):   # matrix.NOT_OBSERVED
        ref = Part("EXP1-T4.refused", NOT_ESTABLISHED, "after cell not established: %s" % a.cell_class)
    else:
        ref = refusal("EXP1-T4.refused", a.outcome)
    return [parsed("EXP1-T4.control", "the DLS user read 1..7 of 8 documents on trial, all permitted"), ref, doc]


def battery(e: Battery):
    paths = [(p, getattr(e.after, p)) for p in E.BATTERY]
    return [parsed("EXP1-T4-battery.control", "trial control: allowed document readable, hidden one never returned"),
            worst("EXP1-T4-battery.refused", [refusal(p, r.outcome) for p, r in paths]),
            worst("EXP1-T4-battery.no-document",
                  [holds(p, not (r.hidden or r.any_source or r.docs), "returned a document after start_basic") if (r.hidden or r.any_source or r.docs)
                   else shown(p, r.docs is not None, _NO_DOC_WITNESS) for p, r in paths])]


# ---- EXP2 -----------------------------------------------------------------------------------------------------------------------
def correctness(e: Correctness):
    if e.examined is not None:
        ex = holds("EXP2-T1.examined", e.examined >= 20000, "examined %d < 20,000" % e.examined)
    elif e.answers_bound is not None and e.answers_bound < 20000:   # the rows alone bound it: violated whatever the oracle says
        ex = Part("EXP2-T1.examined", VIOLATED, "EXP2-T1.examined: at most %d answers to examine (successful auto and auto+linear rows) "
                                                "< 20,000" % e.answers_bound)
    else:
        ex = Part("EXP2-T1.examined", NOT_ESTABLISHED, "EXP2-T1.examined: no oracle-derived count from raw rows")
    c, k = e.control, e.ctl
    return [
        ex,
        holds("EXP2-T1.no-mismatch", e.mismatches == 0, "%d wrong answers" % e.mismatches) if e.mismatches is not None
        else Part("EXP2-T1.no-mismatch", NOT_ESTABLISHED, "EXP2-T1.no-mismatch: answers not compared from raw rows and oracle"),
        shown("EXP2-T1.both-labels", e.by_label is not None and e.by_label.client >= 1 and e.by_label.backend >= 1,
              "client- and backend-labelled answers examined: %s" % ("not recorded" if e.by_label is None
                                                                      else "%d and %d" % (e.by_label.client, e.by_label.backend))),
        holds("EXP2-T1.oracle", e.backend_without_oracle == 0, "%d backend answers without an oracle" % e.backend_without_oracle)
        if e.backend_without_oracle is not None else Part("EXP2-T1.oracle", NOT_ESTABLISHED, "EXP2-T1.oracle: not derived from raw rows"),
        (holds("EXP2-T1.control-readers", k.wrong == 0 and k.without_oracle == 0, "control readers: %d of %d backend answers wrong, %d "
               "without an oracle" % (k.wrong, k.checked, k.without_oracle)) if k is not None
         else Part("EXP2-T1.control-readers", NOT_ESTABLISHED, "EXP2-T1.control-readers: control readers' answers not compared")),
        (Part("EXP2-T1.control-floor", NOT_ESTABLISHED, "EXP2-T1.control-floor: no broken-fallback control from raw rows") if c is None
         else holds("EXP2-T1.control-floor", c.backend_wrong == 0, "control backend wrong %d" % c.backend_wrong) if c.backend_wrong
         else shown("EXP2-T1.control-floor", c.flagged.rate >= 0.9, "control flagged %d of %d (< 90%%: T1 is CLAIMED, not proven)"
                    % (c.flagged.numerator, c.flagged.denominator))),
    ]


def _phases(cid, ph, groups):
    if ph is None:
        return Part(cid, NOT_ESTABLISHED, "%s: requests before and after the transition not recorded" % cid)
    thin = ["%s %d before, %d after" % (g, getattr(ph, g).before, getattr(ph, g).after) for g in groups
            if not (getattr(ph, g).before and getattr(ph, g).after)]
    return shown(cid, not thin, "no requests on one side of the transition: " + ", ".join(thin))


def availability(e: Availability):
    return [holds("EXP2-T2.share", e.failed_share.rate <= 0.001, "%d of %d reads failed" % (e.failed, e.requests)),
            holds("EXP2-T2.run", e.consecutive_auto_s <= 2.0, "longest run of consecutive failed auto requests %.3f s" % e.consecutive_auto_s),
            holds("EXP2-T2.window", e.longest_window_s <= 2.0, "longest failure window %.3f s" % e.longest_window_s),
            _phases("EXP2-T2.phases", e.phases, ("auto", "lex", "vec"))]


def _before(cid, b, label):
    return shown(cid, b is not None and b.n >= 1 and b.other == 0, "auto answers before the transition request all labelled %s: %s"
                 % (label, "not recorded" if b is None else "%d of %d are not" % (b.other, b.n)))


def flip_down(e: FlipDown):
    return [holds("EXP2-T3.down", e.flip_s <= 5.0, "flip %.3f s" % e.flip_s),
            holds("EXP2-T3.down-stays", e.backend_after_client == 0, "%d backend answers after the flip" % e.backend_after_client),
            _before("EXP2-T3.down-before", e.before, "backend")]


def flip_up(e: FlipUp):
    return [holds("EXP2-T3.up", e.flip_s <= e.ttl_s + 5.0, "flip %.3f s > ttl %.1f + 5" % (e.flip_s, e.ttl_s)),
            holds("EXP2-T3.up-stays", e.client_after_backend == 0, "%d client answers after the flip" % e.client_after_backend),
            _before("EXP2-T3.up-before", e.before, "client")]


def writes(e: WritesGate):
    w, b = e.writes, e.writes.basic
    nb = "the semantic writes issued on Basic are not recorded (review 13 F32)"

    def basic(cid, cond, bad):
        return Part(cid, NOT_ESTABLISHED, "%s: %s" % (cid, nb)) if b is None else holds(cid, cond(b), bad(b))
    if w.sem_failed and w.sem_failed_other is None:
        cls = Part("EXP2-T4.classified", NOT_ESTABLISHED, "%d semantic write failures never classified (addendum D)" % w.sem_failed)
    else:
        cls = holds("EXP2-T4.classified", not w.sem_failed_other and w.sem_failed_present == 0 and w.sem_failed_loud == w.sem_failed,
                    "%d of %d semantic failures not loud, %d failed writes present" % (w.sem_failed - w.sem_failed_loud, w.sem_failed,
                                                                                       w.sem_failed_present))
    return [holds("EXP2-T4.lost", w.acked_absent == 0 and w.sem_acked_absent == 0,
                  "%d acknowledged writes absent, %d acknowledged semantic writes absent" % (w.acked_absent, w.sem_acked_absent)),
            _phases("EXP2-T4.phases", None if w.by_phase is None else _OnePhase(w.by_phase), ("plain",)),
            shown("EXP2-T4.control", w.loss_control is True, "injected-loss control not detected"),
            Part("EXP2-T4.basic-attempted", NOT_ESTABLISHED, "EXP2-T4.basic-attempted: " + nb) if b is None
            else shown("EXP2-T4.basic-attempted", b.attempted >= 1, "no semantic write was issued on Basic"),
            basic("EXP2-T4.basic-loud", lambda x: x.failed_other == 0, lambda x: "%d of %d Basic failures not loud" % (x.failed_other, x.failed)),
            basic("EXP2-T4.basic-none-acked", lambda x: x.acked == 0, lambda x: "%d semantic writes acknowledged on Basic" % x.acked),
            basic("EXP2-T4.basic-none-present", lambda x: x.present == 0, lambda x: "%d semantic writes issued on Basic are present" % x.present),
            cls]


class _OnePhase(object):
    __slots__ = ("plain",)

    def __init__(self, p):
        self.plain = p


def licence_down(e: LicenceDown):
    s, st = e.control.licence_share, e.control.start
    return [holds("EXP2-T5.down", s.rate >= 0.95, "licence failures %d of %d" % (s.numerator, s.denominator)),
            shown("EXP2-T5.down-window", st == 1.0, "counted window starts at %s s after the response, registered 1 s" % (st,))]


def licence_up(e: LicenceUp):
    b, a, st = e.before.licence_share, e.after.success_share, e.after.start
    return [holds("EXP2-T5.up-before", b.rate >= 0.95, "licence failures before %d of %d" % (b.numerator, b.denominator)),
            holds("EXP2-T5.up-after", a.numerator == a.denominator, "successes after %d of %d" % (a.numerator, a.denominator)),
            shown("EXP2-T5.up-window", st == 0.0, "counted window starts at %s s after the response, registered 0 s (review 13 F31)" % (st,))]


# ---- EXP3 -----------------------------------------------------------------------------------------------------------------------
def migration(e: Migration):
    b, bb, s, ind, pc = e.bbq_to_hnsw, e.bbq_to_bbq_hnsw, e.semantic, e.independent, e.plain_control
    ops = [holds("%s %s" % (attr, op), getattr(getattr(e.ops, attr), op).case == "SUCCESS", "%s (%d)" % (
        getattr(getattr(e.ops, attr), op).case, getattr(getattr(e.ops, attr), op).status)) for attr in E.MigratedOpsAll.SPEC for op in E.MIG_OPS]
    return [
        holds("EXP3-T2.counts", b.dst == b.src and bb.dst == bb.src and s.dst == s.docs and b.src == ind.bbq_disk and bb.src == ind.bbq_disk
              and s.docs + s.skipped == ind.sem and pc.src == ind.plain, "a copy or source count differs from the independent count"),
        holds("EXP3-T2.vectors", b.mismatches == 0 and bb.mismatches == 0 and s.mismatches == 0 and s.abs_err <= 0.005 and s.cosine >= 0.9999
              and s.skipped == 0, "a migrated vector differs from its source"),
        holds("EXP3-T2.knn", min(b.knn.rate, bb.knn.rate, s.knn.rate) >= 0.99, "kNN self-hit top-1 below 99%"),
        worst("EXP3-T2.ops", ops),
        holds("EXP3.ground-truth", b.src_vs_truth <= 1e-6, "source vectors differ from the stub ground truth by %g" % b.src_vs_truth),
        holds("EXP3-T3.plain", pc.mismatches == 0 and pc.dst == pc.src, "plain control copy differs"),
        shown("EXP3-T3.corrupted", e.corrupted_control.mismatches == 1, "corrupted control not detected (the vector check is not shown sensitive)"),
    ]


def semonly(e: SemOnly):
    return [worst("EXP3-T4.control", [shown(k, getattr(e, k).outcome.case == "SUCCESS", getattr(e, k).outcome.case)
                                      for k in ("semantic", "match", "match_all")])]


Gate = collections.namedtuple("Gate", "scope report out evidence verdict doc conjuncts")
GATES = {
    "EXP1-T2": Gate("down", "exp1", "T2", Moment, moment, "every mandatory probe answered before the call, refused with its required Outcome case within 5 s of start_basic, observed and never successful after", ("EXP1-T2.refused-each", "EXP1-T2.within-5s", "EXP1-T2.no-success-after", "EXP1-T2.observed-after", "EXP1-T2.answered-before")),
    "EXP1-T3": Gate("down", "exp1", "T3_latch", Latch, latch, "the adapter's own _is_licence_error is true for the message it would build (HTTP <code>: first 400 bytes) on each latch cell", ("EXP1-T3.message", "EXP1-T3.detector")),
    "EXP1-T4": Gate("down", "exp1", "T4", Dls, dls, "positive restricted control on trial; after start_basic the DLS user's search is refused and no document is returned", ("EXP1-T4.control", "EXP1-T4.refused", "EXP1-T4.no-document")),
    "EXP1-T4-battery": Gate("down", "exp1", "T4_battery", Battery, battery, "the same control and a trial battery control; every read path refused, nothing returned", ("EXP1-T4-battery.control", "EXP1-T4-battery.refused", "EXP1-T4-battery.no-document")),
    "EXP2-T1": Gate("both", "exp2", "T1", Correctness, correctness, "0 wrong answers (auto, auto+linear and control readers) over >= 20,000 examined, client and backend both examined; control flags >= 90%", ("EXP2-T1.examined", "EXP2-T1.no-mismatch", "EXP2-T1.both-labels", "EXP2-T1.oracle", "EXP2-T1.control-readers", "EXP2-T1.control-floor")),
    "EXP2-T2": Gate("both", "exp2", "T2", Availability, availability, "failed reads <= 0.1%; consecutive failed auto run and failure window <= 2 s; reads on both sides of the transition", ("EXP2-T2.share", "EXP2-T2.run", "EXP2-T2.window", "EXP2-T2.phases")),
    "EXP2-T3-down": Gate("down", "exp2", "T3", FlipDown, flip_down, "backend-labelled before; first client-labelled answer within 5 s of start_basic; no backend label after it", ("EXP2-T3.down", "EXP2-T3.down-stays", "EXP2-T3.down-before")),
    "EXP2-T3-up": Gate("up", "exp2", "T3", FlipUp, flip_up, "client-labelled before; first backend-labelled answer within TTL + 5 s of start_trial; no client label after it", ("EXP2-T3.up", "EXP2-T3.up-stays", "EXP2-T3.up-before")),
    "EXP2-T4": Gate("both", "exp2", "T4", WritesGate, writes, "no acknowledged write lost (writes on both sides); semantic writes on Basic all loud failures, none acknowledged, none present; every failure classified", ("EXP2-T4.lost", "EXP2-T4.phases", "EXP2-T4.control", "EXP2-T4.basic-attempted", "EXP2-T4.basic-loud", "EXP2-T4.basic-none-acked", "EXP2-T4.basic-none-present", "EXP2-T4.classified")),
    "EXP2-T5-down": Gate("down", "exp2", "T5", LicenceDown, licence_down, ">= 95% of control requests started 1 s or more after start_basic are a LICENCE_REFUSAL (counted by Outcome case)", ("EXP2-T5.down", "EXP2-T5.down-window")),
    "EXP2-T5-up": Gate("up", "exp2", "T5", LicenceUp, licence_up, ">= 95% LICENCE_REFUSAL before start_trial; every control request started at or after its response succeeds", ("EXP2-T5.up-before", "EXP2-T5.up-after", "EXP2-T5.up-window")),
    "EXP3-migration": Gate("down", "exp3", "migration", Migration, migration, "every migrated copy equal to its source; Basic operations on migrated indices succeed; controls", ("EXP3-T2.counts", "EXP3-T2.vectors", "EXP3-T2.knn", "EXP3-T2.ops", "EXP3.ground-truth", "EXP3-T3.plain", "EXP3-T3.corrupted")),
    "EXP3-semonly-control": Gate("down", "exp3", "semonly_control", SemOnly, semonly, "semantic-only fixture answers semantic, match and match_all with hits", ("EXP3-T4.control",)),
}


def conjuncts_of(name):
    """Every conjunct of a gate: its verdict's own, then each LIMIT row (a registered clause no recorded field witnesses)."""
    return GATES[name].conjuncts + tuple(c.cid for c in clauses.limits_for(name))


def judge_parts(name, life):
    """-> (Verdict, [Part]) for one gate on one life. Never raises for malformed input."""
    g = GATES[name]
    ev, why = E.parse(g.evidence, life)
    if ev is None:
        parts = [Part(cid, NOT_ESTABLISHED, why) for cid in conjuncts_of(name)]
        return Verdict(UNVERIFIED, why), parts
    parts = list(g.verdict(ev))
    got = collections.Counter(p.cid for p in parts)
    # a declared conjunct the gate function did not judge (or judged twice, or an undeclared one) is never established
    parts = [p if got[p.cid] == 1 and p.cid in g.conjuncts else Part(p.cid, NOT_ESTABLISHED, "conjunct judged %d times or not declared: %s"
                                                                       % (got[p.cid], p.reason)) for p in parts]
    parts += [Part(cid, NOT_ESTABLISHED, "conjunct not judged by %s" % g.verdict.__name__) for cid in g.conjuncts if cid not in got]
    parts += [Part(c.cid, NOT_ESTABLISHED, "registered clause not witnessed: %s (%s)" % (c.cid, c.note)) for c in clauses.limits_for(name)]
    return combine(parts), parts


def judge(name, life):
    """Parse the life into the gate's evidence type, then judge every conjunct. Never raises for malformed input."""
    return judge_parts(name, life)[0]
