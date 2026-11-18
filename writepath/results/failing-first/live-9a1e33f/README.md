# Live window 1 on the lab host, tree pinned at 9a1e33f (2026-11-18, 17:53:15Z to 18:11:09Z)

Unstamped. This window is superseded by `../live-27094c9/` and is kept for its red runs and for the defects it found.

| step | result |
|---|---|
| G1 | 78 OK |
| P0 | ok |
| G2 | 37 run, failures=1, errors=2. These are three test defects (B4, B8 and S6, not code defects), fixed in 6187a4b |
| G3 | 3 OK |
| P1 | FAIL, failed only on the positive control: database emlab is empty. The PC seed was added in 6187a4b |
| P2 | pass |
| RR | `['pg:seal_keys']` with registry.py at 179412e, `[]` at 9a1e33f |
| RS | failures=8, errors=3. The F14 red: plant and forged-MAC rows returned `ok` where WPS40 or WPS41 was required |
| RH | failures=12, errors=7. The F16 red: H16a/b got `'42883' != '57014'` and `None != 'pending'` |
| D6 | licence basic active, then 401. The password reset raced the security index; fixed in 27094c9 |
| LS | 33 candidates, 451 files, 0 hits |
