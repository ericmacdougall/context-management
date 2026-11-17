#!/usr/bin/env python3
"""A fake `claude -p --input-format stream-json` for tests: one result event per input line."""
import json, sys
print(json.dumps({"type": "system", "subtype": "init", "session_id": "fake-sess"}), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok:" + str(msg["message"]["content"])[-20:],
                      "usage": {"input_tokens": 1, "output_tokens": 1}, "session_id": "fake-sess", "total_cost_usd": 0.0}), flush=True)
