"""Idempotency key derivation (pure). A key must be a function of the LOGICAL step only (the input event id, the
rollup ordinal, ...) and never of the attempt, run id, worker identity, process or time: a retried or replayed
activity, or a session rebuilt in a fresh engine, must derive the same key and so cannot repeat the effect."""


def effect_key(input_event_id: str) -> str:
    return f"eff/{input_event_id}"


def result_key(input_event_id: str) -> str:
    return f"res/{input_event_id}"


def rollup_key(session_id: str, ordinal: int) -> str:
    return f"rollup/{session_id}/{int(ordinal)}"


def approval_key(session_id: str, what: str) -> str:
    if what not in ("request", "grant", "resolve"):
        raise ValueError(what)
    return f"approval/{session_id}/{what}"


def finish_key(session_id: str) -> str:
    return f"finish/{session_id}"


def start_key(session_id: str) -> str:
    return f"start/{session_id}"
