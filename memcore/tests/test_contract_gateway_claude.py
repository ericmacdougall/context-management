"""RETIRED (decision 2026-11-16): subscription CLIs are direct development adapters only and are never served over HTTP
(an HTTP front is what the Elasticsearch inference API would call), so client -> gateway -> claude-cli is refused by
memcore.gateway.check_gate. The claude-cli contract suite runs directly: memcore/tests/test_contract_claude*.py."""
import unittest


@unittest.skip("retired: the gateway refuses subscription-backed backends (decision 2026-11-16)")
class GatewayClaudeContract(unittest.TestCase):
    def test_retired(self):
        pass


if __name__ == "__main__":
    unittest.main()
