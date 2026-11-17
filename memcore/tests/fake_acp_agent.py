"""A tiny ACP agent (JSON-RPC over stdio) for testing the client. Not a real agent."""
import json, sys

def out(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
n = 0
for line in sys.stdin:
    m = json.loads(line)
    method, rid = m.get("method"), m.get("id")
    if method == "initialize":
        out({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}, "authMethods": []}})
    elif method == "session/new":
        out({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "S1"}})
    elif method == "session/set_config_option":
        out({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "session/prompt":
        n += 1
        text = m["params"]["prompt"][0]["text"]
        out({"jsonrpc": "2.0", "id": 900 + n, "method": "session/request_permission", "params": {"sessionId": "S1", "toolCall": {"toolCallId": "t"}, "options": []}})
        resp = json.loads(sys.stdin.readline())  # the client must answer our permission request
        granted = resp.get("result", {}).get("outcome", {}).get("outcome")
        out({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "S1", "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "perm=%s;" % granted}}}})
        out({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "S1", "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "saw:" + text[-12:]}}}})
        out({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
