"""The ports the memory core depends on. The core imports this module and nothing vendor specific."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


# ------------------------------------------------------------------ errors
class PortError(Exception):
    """Base class: every adapter failure is one of these, never a raw subprocess or urllib error."""


class InputTooLong(PortError):
    """Raised before any backend call when the request exceeds the adapter's declared limits."""


class CallCapExceeded(PortError):
    """The hard call cap for this backend is used up; nothing was sent."""


class CallTimeout(PortError):
    """The backend did not answer in time; the child process group was killed and nothing was cached."""


class BackendError(PortError):
    """The backend ran and failed (non-zero exit, is_error, HTTP error)."""


class SchemaError(PortError):
    """JSON-schema output was requested and the answer (after one repair retry) did not validate."""


class UnsupportedParameter(PortError):
    """A parameter was passed that this adapter cannot honour and strict mode is on."""


# ------------------------------------------------------------------ chat model
@dataclass(frozen=True)
class Capabilities:
    adapter: str
    supports_json_schema: bool          # True = enforced natively by the backend; see json_schema_mode
    json_schema_mode: str               # "native" | "prompt+validate" | "none"
    supports_system: bool
    reports_usage: bool                 # False => Usage fields are None, never 0
    max_context_tokens: Optional[int]   # None = unknown (no preflight check possible)
    supports_temperature: bool = False
    supports_max_output_tokens: bool = False
    streaming: str = "none"             # "native" | "simulated" | "none"


@dataclass(frozen=True)
class Usage:
    input_tokens: Optional[int] = None          # all prompt tokens, including cached ones
    cached_input_tokens: Optional[int] = None   # subset of input_tokens served from a prompt cache
    output_tokens: Optional[int] = None


@dataclass
class ChatResult:
    text: str
    parsed: Any                       # parsed JSON when a schema was given, else None
    usage: Optional[Usage]            # None when the backend does not report usage at all
    model: str
    latency_s: float                  # wall time of the original backend call (cache hits keep the original value)
    request_hash: str
    cached: bool = False
    adapter: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


Message = Dict[str, str]  # {"role": "user"|"assistant", "content": "..."}


class ChatModel:
    capabilities: Capabilities
    model: str

    def complete(self, messages: Sequence[Message], system: Optional[str] = None, json_schema: Optional[dict] = None,
                 max_output_tokens: Optional[int] = None, temperature: Optional[float] = None,
                 timeout_s: Optional[float] = None) -> ChatResult:
        raise NotImplementedError

    def request_hash(self, messages: Sequence[Message], system: Optional[str] = None, json_schema: Optional[dict] = None,
                     max_output_tokens: Optional[int] = None, temperature: Optional[float] = None) -> str:
        raise NotImplementedError


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ embedder
@dataclass
class EmbedResult:
    vectors: List[List[float]]
    model: str
    dimensions: int
    kind: str


class Embedder:
    """The port owns the query/passage distinction: callers pass `kind`, never a prefix.

    Subclasses implement `_embed(texts)` and declare `query_prefix` / `passage_prefix`
    (E5 models need "query: " / "passage: "; a measured pitfall: omitting them dropped top-10 overlap 1.0 -> 0.72).
    """
    model = "unset"
    dimensions = 0
    query_prefix = ""
    passage_prefix = ""

    def embed(self, texts: Sequence[str], kind: str = "query") -> EmbedResult:
        if kind not in ("query", "passage"):
            raise ValueError("kind must be 'query' or 'passage', got %r" % (kind,))
        if isinstance(texts, str):
            raise TypeError("texts must be a sequence of strings, not one string")
        prefix = self.query_prefix if kind == "query" else self.passage_prefix
        vecs = self._embed([prefix + t for t in texts], kind)
        return EmbedResult(vectors=vecs, model=self.model, dimensions=self.dimensions, kind=kind)

    def _embed(self, texts: List[str], kind: str) -> List[List[float]]:
        raise NotImplementedError


# ------------------------------------------------------------------ reranker
@dataclass
class RerankHit:
    index: int
    score: float


class Reranker:
    model = "unset"

    def rerank(self, query: str, documents: Sequence[str], top_n: Optional[int] = None) -> List[RerankHit]:
        """Hits ordered best first; `index` refers to `documents`."""
        raise NotImplementedError


# ------------------------------------------------------------------ search
@dataclass
class Hit:
    id: str
    score: float
    source: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResult:
    hits: List[Hit]
    mode: str          # "lexical" | "vector" | "fused"
    fusion: str       # "none" | "backend" | "client": where fusion happened (decided by the adapter)


class SearchBackend:
    fusion_location = "client"  # adapter declares "backend" when the engine fuses (licensed retrievers), else "client"

    def lexical(self, index: str, query: str, filter: Optional[dict] = None, k: int = 10) -> SearchResult:
        raise NotImplementedError

    def vector(self, index: str, vector: Sequence[float], filter: Optional[dict] = None, k: int = 10) -> SearchResult:
        raise NotImplementedError

    def fused(self, index: str, query: str, vector: Sequence[float], filter: Optional[dict] = None, k: int = 10) -> SearchResult:
        raise NotImplementedError


# ------------------------------------------------------------------ agent tool surface
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Any]

    def describe(self) -> dict:
        """The surface-neutral description (MCP tools/list, OpenAI tools, Agent Builder all map from this)."""
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema}


class AgentToolSurface:
    """Holds ToolSpecs; a transport (MCP stdio, HTTP, in-process) only maps list/call, it never redefines a tool."""

    def __init__(self, tools: Sequence[ToolSpec], egress_guard="default"):
        self._tools = {t.name: t for t in tools}
        self._guard = egress_guard

    def list_tools(self) -> List[dict]:
        return [t.describe() for t in self._tools.values()]

    def call_tool(self, name: str, arguments: Optional[dict]) -> Any:
        from .jsonschema_min import validate
        if name not in self._tools:
            raise KeyError("unknown tool %r" % name)
        arguments = arguments or {}
        errs = validate(arguments, self._tools[name].input_schema)
        if errs:
            raise ValueError("invalid arguments for %s: %s" % (name, "; ".join(errs)))
        result = self._tools[name].handler(arguments)
        guard = self._guard
        if guard == "default":
            from .guard import DEFAULT_GUARD as guard
        if guard is not None:
            guard.check(result, "tool result of %s" % name)   # model-facing: no dataset session ids
        return result


# ------------------------------------------------------------------ context injection
@dataclass
class Turn:
    session_id: str
    user_text: str
    time: str  # ISO 8601, supplied by the host so the injector never reads a clock


@dataclass
class Injection:
    block: str
    used_ids: List[str]


class ContextInjector:
    """Given a turn, return the block to inject and the ids it used (only ids are logged). Host adapters deliver it."""

    def build(self, turn: Turn) -> Injection:
        raise NotImplementedError
