"""Test helpers for the fail-closed real-id requirement of the memcore entry points (phase 2, goodqa#16096).
The entry points load the id file into memcore.guard.DEFAULT_GUARD IN PLACE; these helpers restore it afterwards so one
test's id file never changes what another test's guard does."""
import contextlib
import json
import os
import tempfile

PLACEHOLDER_ID = "conv-test-0000"     # a stand-in real id; matches nothing the tests inject


def ids_file(ids=(PLACEHOLDER_ID,)) -> str:
    p = os.path.join(tempfile.mkdtemp(), "real_ids.json")
    with open(p, "w") as fh:
        json.dump(list(ids), fh)
    return p


@contextlib.contextmanager
def restored_default_guard():
    from memcore.guard import DEFAULT_GUARD
    saved = (set(DEFAULT_GUARD.real_ids), DEFAULT_GUARD.require_real_ids, DEFAULT_GUARD.load_error)
    try:
        yield DEFAULT_GUARD
    finally:
        DEFAULT_GUARD.set_real_ids(saved[0])
        DEFAULT_GUARD.require_real_ids, DEFAULT_GUARD.load_error = saved[1], saved[2]


def hook_main(argv, **kw):
    """memcore.hosts.prompt_hook.main with a placeholder id file unless the test names its own."""
    from memcore.hosts.prompt_hook import main
    argv = list(argv) if "--real-ids" in argv else list(argv) + ["--real-ids", ids_file()]
    with restored_default_guard():
        return main(argv, **kw)
