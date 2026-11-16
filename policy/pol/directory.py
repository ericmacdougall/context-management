"""Directory of principals (the identity provider's view) and authenticated sessions.

Trust boundary. A request handler holds a Session and nothing else; every attribute a decision uses (tenant, groups, clearance,
restricted flag, kind, region, model) is read from the Directory by the session's principal id at the moment of use. A Session is
only valid if it is the very object the SessionAuthority issued (identity check against the authority's registry), so a hand-built
Session, or a Principal object in its place, is refused. Issuing a session (`SessionAuthority.issue`) and editing the directory
(`Directory.put` / `remove`) are the authentication and administration paths; request handlers never call them.

Directory entries are immutable Principal values; `put` replaces an entry atomically under a lock, so a lookup returns either the old
or the new entry, never a mix. Ids are canonical (pol.ids); an entry whose id looks like an existing different id (partial skeleton)
is refused."""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from typing import FrozenSet, Iterable, Optional

from .ids import IdError, canon, skeleton

KINDS = ("human", "agent", "analyst")
OWNER_CHANNELS = ("owner_ui", "owner_cli")
AGENT_CHANNELS = ("agent_api",)


class DirectoryError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    id: str
    tenant: str
    groups: FrozenSet[str]
    clearance: int
    restricted_ok: bool
    kind: str
    region: Optional[str]
    model: Optional[str]          # destination id this principal's prompts go to (None: a human reading a screen)

    def __post_init__(self):
        try:
            object.__setattr__(self, "id", canon(self.id))
            object.__setattr__(self, "tenant", canon(self.tenant))
            if isinstance(self.groups, (str, bytes)) or not isinstance(self.groups, (set, frozenset, list, tuple)):
                raise DirectoryError("groups")
            object.__setattr__(self, "groups", frozenset(canon(g) for g in self.groups))
        except IdError as e:
            raise DirectoryError(str(e))
        if not isinstance(self.clearance, int) or isinstance(self.clearance, bool) or not 0 <= self.clearance <= 3:
            raise DirectoryError("clearance")
        if not isinstance(self.restricted_ok, bool) or self.kind not in KINDS:
            raise DirectoryError("restricted_ok / kind")
        if self.region is not None and not isinstance(self.region, str):
            raise DirectoryError("region")
        if self.model is not None and not isinstance(self.model, str):
            raise DirectoryError("model")


class Directory:
    def __init__(self, entries: Iterable[Principal]):
        self._lock = threading.Lock()
        self._by_id = {}
        self._version = 0
        for e in entries:
            self.put(e)

    def put(self, entry: Principal):
        if not isinstance(entry, Principal):
            raise DirectoryError("entry type")
        with self._lock:
            sk = skeleton(entry.id)
            for other in self._by_id:
                if other != entry.id and skeleton(other) == sk:
                    raise DirectoryError("id %r looks like existing id %r" % (entry.id, other))
            self._by_id[entry.id] = entry
            self._version += 1

    def remove(self, pid: str):
        with self._lock:
            self._by_id.pop(canon(pid), None)
            self._version += 1

    def lookup(self, pid) -> Optional[Principal]:
        """The current entry, or None. Never raises for a malformed id (it simply is not there)."""
        try:
            k = canon(pid)
        except IdError:
            return None
        with self._lock:
            return self._by_id.get(k)

    def version(self) -> int:
        with self._lock:
            return self._version


@dataclass(frozen=True, eq=False)
class Session:
    tenant: str
    principal_id: str
    channel: str
    token: str


class SessionAuthority:
    """Issues sessions after authentication (the write path's lease and session binding in a deployment) and verifies them by identity."""

    def __init__(self, directory: Directory):
        if not isinstance(directory, Directory):
            raise TypeError("SessionAuthority needs a Directory")
        self._dir = directory
        self._live = {}
        self._lock = threading.Lock()

    def issue(self, principal_id: str, channel: str) -> Session:
        p = self._dir.lookup(principal_id)
        if p is None:
            raise DirectoryError("unknown principal")
        if channel not in OWNER_CHANNELS + AGENT_CHANNELS:
            raise DirectoryError("unknown channel")
        if (p.kind == "human") != (channel in OWNER_CHANNELS):
            raise DirectoryError("channel does not match the principal's kind")
        s = Session(p.tenant, p.id, channel, secrets.token_hex(16))
        with self._lock:
            self._live[s.token] = s
        return s

    def end(self, session: Session):
        with self._lock:
            if self._live.get(getattr(session, "token", None)) is session:
                del self._live[session.token]

    def verify(self, session) -> bool:
        tok = getattr(session, "token", None)
        if not isinstance(tok, str):
            return False
        with self._lock:
            return self._live.get(tok) is session
