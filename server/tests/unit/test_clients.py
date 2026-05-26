"""Unit tests for shared/clients/mangrove.py — SDK singletons."""
from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

from mangrove_ai import MangroveAI
from mangrove_markets import MangroveMarkets

from src.shared.clients.mangrove import (
    mangroveai_client,
    mangrovemarkets_client,
    reset_clients,
)


def test_mangroveai_singleton_returns_same_instance():
    reset_clients()
    a = mangroveai_client()
    b = mangroveai_client()
    assert a is b
    assert isinstance(a, MangroveAI)


def test_mangrovemarkets_singleton_returns_same_instance():
    reset_clients()
    a = mangrovemarkets_client()
    b = mangrovemarkets_client()
    assert a is b
    assert isinstance(a, MangroveMarkets)


def test_reset_clients_creates_new_instance():
    reset_clients()
    a = mangroveai_client()
    reset_clients()
    b = mangroveai_client()
    assert a is not b  # reset caused re-instantiation
