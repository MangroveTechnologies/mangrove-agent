"""Unit tests for shared/clients/mangrove.py — SDK singletons."""
from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

from mangrove_ai import MangroveAI
from mangrove_markets import MangroveMarkets

from src.shared.clients.mangrove import (
    mangrove_ai_client,
    mangrove_markets_client,
    reset_clients,
)


def test_mangroveai_singleton_returns_same_instance():
    reset_clients()
    a = mangrove_ai_client()
    b = mangrove_ai_client()
    assert a is b
    assert isinstance(a, MangroveAI)


def test_mangrovemarkets_singleton_returns_same_instance():
    reset_clients()
    a = mangrove_markets_client()
    b = mangrove_markets_client()
    assert a is b
    assert isinstance(a, MangroveMarkets)


def test_reset_clients_creates_new_instance():
    reset_clients()
    a = mangrove_ai_client()
    reset_clients()
    b = mangrove_ai_client()
    assert a is not b  # reset caused re-instantiation
