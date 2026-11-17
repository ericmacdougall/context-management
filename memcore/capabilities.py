"""Start-up capability probe: which Elasticsearch licence tier is active and therefore which memcore paths may run.

Decision in force (user, 2026-11-16, relayed by main, WAL wb7ed3da806f): Basic is the REQUIRED path. Agent Builder and the
Elasticsearch inference API are Enterprise-only optional features. memcore must therefore work with:
  - no ES inference API (embedders/rerankers run locally: fakes.HashEmbedder, adapters.ssh_models, or the client's own),
  - no retriever fusion (ElasticsearchSearch strategy "client": a lexical and a kNN query fused in memcore.fusion.rrf),
  - no Agent Builder (the client's agent runtime calls POST /v1/pre_exec itself: memcore.hosts.basic_hook).

The probe makes ONE call that Basic allows (GET /_license) and never probes a licensed API, so on a Basic node it cannot
produce a licence 403. Any status other than "active" or an unknown type is treated as Basic (fail towards the required
path). Feature mapping (ASSUMPTION from the decision above, not from Elastic): retriever fusion, inference API and Agent
Builder only on an active enterprise or trial licence.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Callable, Optional

ENTERPRISE_TYPES = {"enterprise", "trial"}
LICENSED_PATH_PREFIXES = ("/_inference",)          # ES APIs the Basic path must never call


def tier_from_licence(lic: Optional[dict]) -> dict:
    lic = (lic or {}).get("license") or {}
    typ, status = str(lic.get("type") or "unknown"), str(lic.get("status") or "unknown")
    enterprise = status == "active" and typ in ENTERPRISE_TYPES
    return {"licence_type": typ, "licence_status": status, "tier": "enterprise" if enterprise else "basic",
            "features": {"retriever_fusion": enterprise, "inference_api": enterprise, "agent_builder": enterprise},
            "search_strategy": "auto" if enterprise else "client", "injection_path": "basic_hook (POST /v1/pre_exec)"
            + (" or Agent Builder pre-execution workflow (optional)" if enterprise else "")}


def probe(get_json: Callable[[str], dict]) -> dict:
    """get_json(path) -> parsed JSON (raises on HTTP errors). Returns the tier report; on any error the report says
    tier basic with the error type (the Basic path needs nothing the probe could not confirm)."""
    try:
        return tier_from_licence(get_json("/_license"))
    except Exception as e:  # noqa: BLE001 - an unreachable or refusing node: assume the required (Basic) path
        rep = tier_from_licence(None)
        rep["probe_error"] = type(e).__name__
        return rep


def es_get_json(search) -> Callable[[str], dict]:
    """A get_json over an ElasticsearchSearch adapter's transport (same auth, TLS and timeout)."""
    return lambda path: search._req("GET", path)


def apply(report: dict, search) -> None:
    """Make an ElasticsearchSearch follow the tier: Basic never tries the licensed retriever (no probe, no 403)."""
    if report["tier"] == "basic":
        search.strategy = "client"
        search.fusion_location = "client"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Report the active Elasticsearch licence tier and the memcore paths it allows.")
    ap.add_argument("--es", required=True, help="Elasticsearch base URL")
    ap.add_argument("--auth-file", required=True, help="file holding the Authorization header value (e.g. 'ApiKey ...'), 0600")
    ap.add_argument("--ca", default=None)
    a = ap.parse_args(argv)
    from .adapters.elasticsearch_search import ElasticsearchSearch

    def auth():
        with open(a.auth_file) as fh:
            return fh.read().strip()
    s = ElasticsearchSearch(a.es, auth, tenant="probe", ca_file=a.ca)
    print(json.dumps(probe(es_get_json(s)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
