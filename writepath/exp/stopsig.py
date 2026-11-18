"""A stop signal runs the orchestrator's cleanup (review D2 of f36766c).

Without a handler, SIGTERM ends Python without running `finally:`, so a live-chain stop (results/failing-first/harness/
live_chain.sh, the 8 GB stop line of amendment H6d) left service containers running, ES security on and 0600 secret
copies under /h/svc. install() makes the FIRST SIGTERM or SIGINT raise SystemExit(143) in the main thread, so every
`finally:` runs; later signals are ignored while that cleanup runs (a second TERM must not interrupt it)."""
import signal

STOPPED = {"signal": None}


def _handler(signum, frame):
    STOPPED["signal"] = signum
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, signal.SIG_IGN)
    raise SystemExit(143)


def install():
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, _handler)
