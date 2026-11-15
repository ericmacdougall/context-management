"""Commit-time fencing predicates (pure). The SQL and advisory-lock fences in
derive.py / erasure.py implement the same rules against the database."""


def commit_allowed(input_event_ids, tombstoned) -> bool:
    """A derived row may be committed only if none of its inputs is tombstoned."""
    return not (set(input_event_ids) & set(tombstoned))


def touches(input_event_ids, tombstoned) -> bool:
    return bool(set(input_event_ids) & set(tombstoned))


def claim_may_commit(fold_token, current_token) -> bool:
    """A fold result commits only with the claim token it was started with
    (fencing token): a worker whose lease expired and was re-claimed loses."""
    return fold_token is not None and fold_token == current_token


def next_fold_at(now, dirty_since, debounce, max_wait):
    """Debounce with an upper bound: each activity pushes the fold out by
    `debounce` but never beyond dirty_since + max_wait."""
    return min(now + debounce, dirty_since + max_wait)
