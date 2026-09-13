"""Unit tests for knowledge_service against the real bundled graph (offline, no network)."""
from __future__ import annotations

import json
import os

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402

from src.services.knowledge_service import KnowledgeQuery, _graph, query_knowledge  # noqa: E402
from src.shared.errors import KnowledgeQueryInvalid  # noqa: E402


def _q(**kw) -> dict:
    return query_knowledge(KnowledgeQuery(**kw))


def test_stats_hides_machine_facts_and_lists_vocabulary():
    out = _q(op="stats")
    assert out["op"] == "stats"
    assert out["source"].startswith("mangrove-kb ")
    result = out["result"]
    assert "source" not in result and "version" not in result
    assert result["nodes"] > 0
    # Roles are node ids (e.g. property:role-trigger); find(role="trigger") accepts the short form.
    assert any(r.endswith("trigger") for r in result["roles"])
    assert any(r.endswith("filter") for r in result["roles"])
    assert result["classes"]


def test_find_by_words_with_role_filter():
    out = _q(op="find", q="divergence", limit=5)
    assert out["result"]["returned"] >= 1
    triggers = _q(op="find", role="trigger", limit=3)["result"]
    assert triggers["returned"] == 3
    assert triggers["truncated"] is True  # capped results say so


def test_get_node_carries_params():
    node = _q(op="get", q="rsi")["result"]
    assert node["name"] == "RSI"
    assert "window" in node["params"]


def test_neighbors_in_uses():
    out = _q(op="neighbors", q="rsi", relation="uses", direction="in", limit=5)
    assert out["result"]["returned"] >= 1
    assert all(item["relation"] == "uses" for item in out["result"]["items"])


def test_path_between_signal_and_class():
    out = _q(op="path", q="adosc_bearish", to="momentum")
    assert isinstance(out["result"]["path"], list) and out["result"]["path"]


def test_outputs_are_json_safe():
    """Unbounded ranges are [-inf, inf] in the graph; strict JSON must still encode them."""
    out = _q(op="outputs", q="histogram", limit=5)
    json.dumps(out, allow_nan=False)
    ranges = [item.get("range") for item in out["result"]["items"]]
    assert ["-inf", "inf"] in ranges


def test_ask_degrades_and_says_so_without_semantic_extra():
    out = _q(op="ask", q="what are the odds I wipe out the account", limit=5)
    assert out["result"]["returned"] >= 1
    graph = _graph()
    if graph.dense_index() is None:
        assert "note" in out
        assert "mangrove-kb[semantic]" in out["note"] or "word search" in out["note"]


@pytest.mark.parametrize("kw", [
    {"op": "ask"},
    {"op": "get"},
    {"op": "neighbors"},
    {"op": "path", "q": "rsi"},
])
def test_missing_q_is_a_correctable_error(kw):
    with pytest.raises(KnowledgeQueryInvalid):
        _q(**kw)


def test_unknown_node_raises_with_guidance():
    with pytest.raises(KnowledgeQueryInvalid) as exc:
        _q(op="get", q="definitely_not_a_node_zzz")
    assert "no node matching" in exc.value.message
    assert exc.value.suggestion
