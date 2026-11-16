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
    """Works on older trees too (attributes they lack are left alone), so base-tree runs fail for the tested reason."""
    from memcore.guard import DEFAULT_GUARD
    saved = {k: getattr(DEFAULT_GUARD, k) for k in ("real_ids", "require_real_ids", "load_error") if hasattr(DEFAULT_GUARD, k)}
    saved["real_ids"] = set(saved.get("real_ids") or ())
    try:
        yield DEFAULT_GUARD
    finally:
        if hasattr(DEFAULT_GUARD, "set_real_ids"):
            DEFAULT_GUARD.set_real_ids(saved.pop("real_ids"))
        else:
            saved.pop("real_ids")
        for k, v in saved.items():
            setattr(DEFAULT_GUARD, k, v)


def hook_main(argv, **kw):
    """memcore.hosts.prompt_hook.main with a placeholder id file unless the test names its own. The file is passed
    through MEMCORE_REAL_IDS_FILE (not the flag), so a tree without --real-ids still parses the same argv."""
    from memcore.hosts.prompt_hook import main
    old = os.environ.get("MEMCORE_REAL_IDS_FILE")
    if "--real-ids" not in argv:
        os.environ["MEMCORE_REAL_IDS_FILE"] = ids_file()
    try:
        with restored_default_guard():
            return main(list(argv), **kw)
    finally:
        if old is None:
            os.environ.pop("MEMCORE_REAL_IDS_FILE", None)
        else:
            os.environ["MEMCORE_REAL_IDS_FILE"] = old
