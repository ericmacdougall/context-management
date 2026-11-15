"""Versioned session state carried in the workflow input and across continue-as-new (ids, cursors, counters only).

v1: counters `rollups`, `turns_done`.
v2: `rollups` is renamed `rollup_count`; new field `policy_gen` (int, default 0) and `last_result` (id, default "").

migrate() is pure and total: it upgrades any known older shape one step at a time, is idempotent on current-shape
input, never drops an unknown field silently (they are kept under their own names), and refuses a state from the
future (a newer version than this code knows) so an old worker can never corrupt a newer session."""
import copy

CURRENT = 2


class StateFromFuture(ValueError):
    pass


def new_state(tenant, sid, ver=CURRENT, **kw):
    st = {"ver": 1, "tenant": tenant, "sid": sid, "expected_turns": 0, "rollup_s": 0, "approval_at": 0,
          "approval_to_s": 0, "approval": "none", "rollup_deadline_ms": 0, "rollups": 0, "turns_done": 0,
          "signals_seen": 0, "cans": 0, "hist_max": 0, "can_len": 0}
    st.update(kw)
    return migrate(st, to=ver) if ver > 1 else st


def _v1_to_v2(d):
    d = dict(d)
    d["rollup_count"] = d.pop("rollups", 0)
    d.setdefault("policy_gen", 0)
    d.setdefault("last_result", "")
    d["ver"] = 2
    return d


_STEPS = {1: _v1_to_v2}


def migrate(state, to=CURRENT):
    d = copy.deepcopy(state)
    v = d.get("ver", 1)
    if v > to:
        raise StateFromFuture(f"state ver {v} > supported {to}")
    while v < to:
        d = _STEPS[v](d)
        v = d["ver"]
    return d


def migrate_lossy(state, to=CURRENT):
    """BROKEN on purpose (test control): adds the new fields but resets the renamed counter."""
    d = migrate(state, to)
    d["rollup_count"] = 0
    return d
