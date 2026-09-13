"""knowledge_service — query the Mangrove knowledge graph, offline.

Backed by the ``mangrove-kb`` package, which bundles the graph (every
indicator and signal in the library, what each computes, consumes and
produces, joined to the trading knowledge-base prose). Loading it needs no
network and no API call, so it is loaded once per process and reused.

The op surface mirrors MangroveAI's copilot tool of the same name
(``stats``, ``find``, ``ask``, ``get``, ``neighbors``, ``outputs``,
``path``) so the synced Michael skills read correctly here.

``ask`` searches by meaning. The package ships two indices for it: an LSA
index built from the corpus (works with the base install) and a
pretrained-encoder index that needs the ``mangrove-kb[semantic]`` extra
(sentence-transformers). This agent installs the base package only, so
``ask`` runs on whatever index is usable and says so in ``note``; with
neither it falls back to word search.
"""
from __future__ import annotations

import math
from functools import lru_cache
from importlib import metadata
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.shared.errors import KnowledgeQueryInvalid
from src.shared.logging import get_logger

_log = get_logger(__name__)

OPS = ("stats", "find", "ask", "get", "neighbors", "outputs", "path")

# Keys of stats() that describe where the graph file lives on this machine,
# not anything about trading.
_MACHINE_FACTS = ("source", "version")

_MAX_LIMIT = 200


class KnowledgeQuery(BaseModel):
    """One question about the knowledge graph. Which fields apply depends on ``op``."""

    op: Literal["stats", "find", "ask", "get", "neighbors", "outputs", "path"]
    q: str = Field("", description="Search text (find/ask/outputs) or node id/name (get/neighbors/path).")
    to: str = Field("", description="path: the destination node.")
    kind: str | None = Field(None, description="Class: what a computation measures (find/outputs).")
    role: str | None = Field(None, description="Part a signal plays: trigger | filter (find).")
    status: str | None = Field(None, description="find: e.g. ratified | deprecated.")
    requires: str | None = Field(None, description="find: an input column, e.g. volume.")
    param: str | None = Field(None, description="find: every signal taking a parameter of this name.")
    relation: str | None = Field(None, description="neighbors: follow only this relation.")
    direction: Literal["in", "out", "both"] = Field("both", description="neighbors: in = what reads this.")
    units: str | None = Field(None, description="outputs: exact unit match.")
    bounded: bool | None = Field(None, description="outputs: only values with a fixed range.")
    hops: int = Field(1, ge=0, le=3, description="ask: how far to walk from each match; 0 = retrieval only.")
    limit: int = Field(25, ge=1, le=_MAX_LIMIT, description="Maximum results.")


@lru_cache(maxsize=1)
def _graph():
    from mangrove_kb.graph import KnowledgeGraph

    graph = KnowledgeGraph.load()
    _log.info("knowledge.graph_loaded", nodes=len(graph.nodes), edges=len(graph.edges))
    return graph


def _package_version() -> str | None:
    try:
        return metadata.version("mangrove-kb")
    except metadata.PackageNotFoundError:  # pragma: no cover - dependency is pinned
        return None


def _json_safe(value: Any) -> Any:
    """Replace non-finite floats: the graph writes unbounded ranges as [-inf, inf],
    which strict JSON encoders (Starlette's included) refuse to serialize."""
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return None
        return "inf" if value > 0 else "-inf"
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _ask_note(graph) -> str | None:
    lsa = graph.semantic_index() is not None
    dense = graph.dense_index() is not None
    if lsa and dense:
        return None
    if lsa:
        return (
            "Meaning search ran on the corpus index only: the pretrained-encoder index needs "
            "the mangrove-kb[semantic] extra, which this agent does not install. Paraphrased "
            "questions match less well; if the rows look off-topic, ask again with a domain "
            "term or use op=find."
        )
    return (
        "No meaning-search index is available, so ask fell back to word search (the same "
        "matching as op=find). Phrase the question with the terms the answer would use."
    )


def _require_q(op: str, q: str, what: str) -> None:
    if not q.strip():
        raise KnowledgeQueryInvalid(f"op={op} needs `q`: {what}.")


def query_knowledge(req: KnowledgeQuery) -> dict[str, Any]:
    """Answer one question about the knowledge graph.

    Returns ``{op, result, source, [note]}``. Raises ``KnowledgeQueryInvalid``
    for a call that cannot be answered as asked (missing ``q``, a node or filter
    value the graph does not have) — the message carries the graph's own
    suggestions, so the next call can be right.
    """
    from mangrove_kb.graph import GraphError

    graph = _graph()
    note: str | None = None
    try:
        if req.op == "stats":
            result: Any = {k: v for k, v in graph.stats().items() if k not in _MACHINE_FACTS}
        elif req.op == "find":
            result = graph.find(req.q, kind=req.kind, role=req.role, status=req.status,
                                requires=req.requires, param=req.param,
                                limit=req.limit).as_dict()
        elif req.op == "ask":
            _require_q("ask", req.q, "the question, in ordinary words")
            result = graph.ask(req.q, hops=req.hops, limit=req.limit).as_dict()
            note = _ask_note(graph)
        elif req.op == "get":
            _require_q("get", req.q, "the node, by id or name")
            result = graph.get(req.q)
        elif req.op == "neighbors":
            _require_q("neighbors", req.q, "the node, by id or name")
            result = graph.neighbors(req.q, direction=req.direction, relation=req.relation,
                                     limit=req.limit).as_dict()
        elif req.op == "outputs":
            result = graph.outputs(req.q, units=req.units, bounded=req.bounded,
                                   kind=req.kind, limit=req.limit).as_dict()
        else:  # path
            if not req.q.strip() or not req.to.strip():
                raise KnowledgeQueryInvalid("op=path needs both `q` and `to`.")
            path = graph.path(req.q, req.to)
            result = {"path": path}
            if path is None:
                note = "Nothing connects these two nodes within the search depth."
    except GraphError as exc:
        raise KnowledgeQueryInvalid(
            str(exc),
            suggestion="Call op=stats for the vocabulary every filter accepts, or op=find to locate a node.",
        ) from exc

    out: dict[str, Any] = {
        "op": req.op,
        "result": _json_safe(result),
        "source": f"mangrove-kb {_package_version() or 'unknown'} (bundled graph, offline)",
    }
    if note:
        out["note"] = note
    return out
