# Pinned upstream code (read-only copies)

| What | Upstream | Commit | Files |
|---|---|---|---|
| memcore (model port, budgeted injector, inbox with fencing token, egress guard, jsonschema_min, CallBudget) | worktree elastic-memory-lab-adapters-fix, branch fix/adapters-delta-16096 | d71da85 (this branch is cut from it; memcore/ is unmodified) | memcore/ |
| policy point (labels, pdp, enforce) | worktree elastic-memory-lab-policy, branch feat/policy-decisions | 515e594d63ecb2bbf9ad848d0df9625f82beb7dc | ctxrt/vendor/pol/{labels,pdp,enforce}.py copied byte-for-byte with `git show 515e594:policy/pol/<f>.py` |
| quote-and-speaker rule | elastic-memory-lab-phasef, phasef/PROVENANCE_CHECK.md (rule text; re-implemented in ctxrt/hostcheck.py) | read at worktree HEAD 19b74f1 | rule only |
| decision core (pure reducer) | same | 515e594 | ctxrt/vendor/pol/core.py byte copy (imports .labels join_dicts only) |
