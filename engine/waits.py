"""Pure wait arithmetic used by the workflow (unit-testable anywhere)."""

APPROVAL_FLOOR_S = 1.0     # an approval poll is never shorter than this, whatever the clocks say


def approval_wait_s(remaining_s, current_poll_s, floor_s=APPROVAL_FLOOR_S):
    """How long to wait for the approval doorbell before asking Postgres again. `remaining_s` is computed from the
    workflow clock against a Postgres-time deadline; when the workflow clock LEADS Postgres it can be zero or negative
    while Postgres still says pending, and without a floor the loop would spin at one activity per iteration."""
    return max(floor_s, min(remaining_s, current_poll_s))
