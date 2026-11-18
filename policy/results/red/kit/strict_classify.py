"""Strict failing-first count of a unittest -v red log (review of 2262eaa F4). Parses FAIL/ERROR blocks as kit/o-classify.py does, then
puts each in ONE class by the first matching rule below (the rules are the whole method; a block no rule matches is UNCLASSIFIED and
must be read by hand):

  missing-api  the code under test lacks a function, role or attribute the test calls (an ERROR or a "does not exist" assertion)
  vacuous      the test's own precondition failed: the call did not wait, no lock trace, a count over vacuous subtests ("2 != 0" in
               the S1 points), "did not wait on the held lock"
  safety       an unsafe outcome was observed: a call answered, applied, opened, accepted, erased or kept a session/view when it
               should have refused, or a cross-tenant / revoked-principal effect happened
  liveness     the operation that should have completed did not (in time): a revocation gave up (55P03) instead of applying, a call
               still waited past expiry or XWAIT, was starved, or a stalled holder survived
  mechanism    a step of the fix's protocol is absent but no unsafe outcome or lost liveness is shown in that assertion: the mutation
               did not wait for the call, a fence did not refuse (None != '55P03'), a privilege was not refused
  availability a call that should not touch another backend ended it, stalled it or fenced it (cross-tenant, or a change the rules
               refuse)
  integrity    a fault or malformed input left partial state, raised instead of refusing, or the vault/rows came out inexact
  verifier     a verifier returned PASS on manipulated evidence
A few assertions carry no message ("False is not true"); OVERRIDES names their class by test. BY_TEST classes the tests of the
review of 2262eaa by test name (read by hand; their messages mix words the generic rules would misplace).

`safety`, `liveness`, `availability`, `integrity` and `verifier` are failing-first evidence of a defect of that kind; `mechanism`, `vacuous` and
`missing-api` are not.
usage: python3 strict_classify.py <log> [<log>...]   (prints one table per log and the totals)"""
import collections
import re
import sys

RULES = [
    ("missing-api", r"UndefinedFunction|UndefinedObject|UndefinedTable|does not exist|has no attribute|ImportError|ModuleNotFoundError"),
    ("vacuous", r"vacuous|no lock trace recorded|did not wait on the held lock|^AssertionError: 2 != 0$"),
    ("verifier", r"'PASS' != 'FAIL'"),
    ("integrity", r"raised:|Tuples differ: \(1, 1, 0\) != \(0, 0, 0\)|Lists differ: \[|Tuples differ: \(True, 1\) != \(False, 0\)"),
    ("liveness", r"evicted the holder|revoker was evicted|still waiting|starved|late \d|waited for A's prune|survived|gave up|None != 'revoked'|"
                 r"is not True : \{'error': '55P03'|took longer|the revoke needed more than one round|stuck"),
    ("mechanism", r"did not wait for|None != '55P03'|InsufficientPrivilege not raised|None != \(False, False, False, False\)|"
                  r"was not evicted|did not wait while"),
    ("safety", r"!= 'principal_revoked'|^AssertionError: \{'req'|answered|accepted|applied|opened|erased|'proposed' != 'unauth|!= 'unauthenticated|!= \('unauthorized'|"
               r"None != 'proof refused|Tuples differ: \(\{'req'|Tuples differ: \(None, 0\)|is not None|is not false : \{|"
               r"!= \(None, \[\]\)|ended a tenant-b|another tenant|evicted bob|evicted the revocation|re-took|"
               r"refused, emptied or stalled|end_session answered|holds a call's advisory locks|prepared|"
               r"can still execute|restore the advisory ACL|is not false : \[|is not false : None"),
]

OVERRIDES = {"test_44_rollback_gives_the_old_engine_its_advisory_lock": "liveness"}   # the old engine is refused its lock after rollback

BY_TEST = {
    "test_a_colliding_subject_never_stalls_another_tenant": "availability",
    "test_a_nonexistent_colliding_target_evicts_nobody": "availability",
    "test_colliding_principals_never_cross_evict_or_cross_fence": "availability",
    "test_refused_changes_evict_nobody": "availability",
    "test_refused_changes_never_wait_for_the_tenant_lock": "liveness",
    "test_a_prepared_transaction_cannot_hold_authority": "liveness",
    "test_a_member_login_role_holder_is_evicted": "liveness",
    "test_end_session_does_not_cancel_a_revoke_in_progress": "liveness",
    "test_a_reconnecting_adversary_never_re_takes_the_lock": "mechanism",
    "test_install_removes_explicit_advisory_grants": "mechanism",
    "test_rollback_restores_the_recorded_acl": "integrity",
}


def blocks(text):
    out = []
    for b in re.split(r"\n={50,}\n", text)[1:]:
        head, _, rest = b.partition("\n")
        m = re.match(r"(FAIL|ERROR): (\S+) \(([^)]+)\)(.*)", head)
        if not m:
            continue
        kind, name, cls, sub = m.groups()
        exc = "?"
        for line in reversed(rest.splitlines()):
            if re.match(r"^([A-Za-z_][\w.]*(Error|Exception|Warning|Exit|Failure|Interrupt|errors\.\w+))(:.*)?$", line.strip()):
                exc = line.strip()
                break
        out.append((kind, cls.split(".")[-1] + "." + name, sub.strip(), exc))
    return out


def klass(kind, exc, test=""):
    if kind == "FAIL" and exc.startswith("AssertionError"):
        for t, c in BY_TEST.items():
            if t in test:
                return c
    for t, c in OVERRIDES.items():
        if t in test and exc.strip() in ("AssertionError: False is not true", "AssertionError: True is not false"):
            return c
    if kind == "ERROR" and not exc.startswith("AssertionError"):
        return "missing-api" if re.search(RULES[0][1], exc) else "error"
    for name, rx in RULES:
        if re.search(rx, exc, re.M):
            return name
    return "UNCLASSIFIED"


def main(paths):
    for p in paths:
        text = open(p, encoding="utf-8", errors="replace").read()
        rows = [(klass(k, e, t), k, t, s, e) for k, t, s, e in blocks(text)]
        print("== %s" % p)
        for c, k, t, s, e in rows:
            print("%-12s %-5s %s %s | %s" % (c, k, t, s[:80], e[:200]))
        summary = re.findall(r"^(Ran \d+ tests? in [\d.]+s)\s*\n\s*\n(OK.*|FAILED.*)$", text, re.M)
        print("STRICT", dict(collections.Counter(c for c, *_ in rows)), summary)


if __name__ == "__main__":
    main(sys.argv[1:])
