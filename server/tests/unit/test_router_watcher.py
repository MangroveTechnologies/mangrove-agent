"""The weekly 1inch router watcher reads the routers that signing reviews."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from src.services import transaction_intent

ROOT = Path(__file__).resolve().parents[3]


def test_watcher_reads_the_routers_transaction_intent_reviews(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "check_1inch_routers", ROOT / ".github/scripts/check_1inch_routers.py")
    watcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watcher)
    monkeypatch.chdir(ROOT)
    assert watcher.get_current_allowlist() == {
        transaction_intent.ROUTER_V5.lower(), transaction_intent.ROUTER_V6.lower()}
