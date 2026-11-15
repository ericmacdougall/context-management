"""Independent oracle for step classification. Deliberately simpler than task.py: it never looks at events or
operations, only at the explicit snapshot of the definition at every version that the scenario generator kept."""


def oracle(truth):
    """truth: {snaps:[{item_id:text}...] (index 0 = version 1), steps:{sid:{serves, basis (version) or None, invalid}}}
    Returns {sid: class or None}."""
    now = truth["snaps"][-1]
    out = {}
    for sid, st in truth["steps"].items():
        if st["invalid"]:
            out[sid] = "invalidated"
        elif st["basis"] is None:
            out[sid] = None
        else:
            then = truth["snaps"][st["basis"] - 1]
            alive = [i for i in st["serves"] if i in now]
            if st["serves"] and not alive:
                out[sid] = "obsolete"
            elif any(then.get(i) != now[i] for i in alive):
                out[sid] = "needs_recheck"
            else:
                out[sid] = "still_valid"
    return out
