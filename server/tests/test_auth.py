"""Tests for API key auth middleware."""
import os

os.environ.setdefault("ENVIRONMENT", "test")

from unittest.mock import MagicMock, patch

import pytest


def _make_mock_config(auth_enabled, api_keys="test-key-1,test-key-2"):
    mock = MagicMock()
    mock.AUTH_ENABLED = auth_enabled
    mock.API_KEYS = api_keys
    return mock


def test_valid_api_key_passes():
    mock_config = _make_mock_config(auth_enabled=True)
    with patch("src.shared.auth.middleware._get_config", return_value=mock_config):
        from src.shared.auth.middleware import validate_api_key
        result = validate_api_key("test-key-1")
        assert result == "test-key-1"


def test_invalid_api_key_rejected():
    mock_config = _make_mock_config(auth_enabled=True)
    with patch("src.shared.auth.middleware._get_config", return_value=mock_config):
        from src.shared.auth.middleware import validate_api_key
        with pytest.raises(ValueError, match="Invalid API key"):
            validate_api_key("wrong-key")


def test_missing_api_key_rejected():
    mock_config = _make_mock_config(auth_enabled=True)
    with patch("src.shared.auth.middleware._get_config", return_value=mock_config):
        from src.shared.auth.middleware import validate_api_key
        with pytest.raises(ValueError, match="Missing API key"):
            validate_api_key(None)


def test_auth_disabled_allows_all():
    mock_config = _make_mock_config(auth_enabled=False)
    with patch("src.shared.auth.middleware._get_config", return_value=mock_config):
        from src.shared.auth.middleware import validate_api_key
        result = validate_api_key(None)
        assert result is None


def test_has_valid_api_key_true():
    mock_config = _make_mock_config(auth_enabled=True)
    with patch("src.shared.auth.middleware._get_config", return_value=mock_config):
        from src.shared.auth.middleware import has_valid_api_key
        assert has_valid_api_key("test-key-1") is True


def test_has_valid_api_key_false():
    mock_config = _make_mock_config(auth_enabled=True)
    with patch("src.shared.auth.middleware._get_config", return_value=mock_config):
        from src.shared.auth.middleware import has_valid_api_key
        assert has_valid_api_key("wrong") is False


def test_has_valid_api_key_disabled():
    mock_config = _make_mock_config(auth_enabled=False)
    with patch("src.shared.auth.middleware._get_config", return_value=mock_config):
        from src.shared.auth.middleware import has_valid_api_key
        assert has_valid_api_key(None) is True


@pytest.mark.parametrize("published", ["dev-key-1", "GENERATED_BY_SETUP"])
def test_published_default_key_never_authenticates(published):
    """A key published in this repo must be refused even when it is configured:
    the wallet routes (incl. secret reveal) sit behind this check."""
    mock_config = _make_mock_config(auth_enabled=True, api_keys=f"{published},unique-key-abc")
    with patch("src.shared.auth.middleware._get_config", return_value=mock_config):
        from src.shared.auth.middleware import has_valid_api_key, validate_api_key
        with pytest.raises(ValueError, match="Invalid API key"):
            validate_api_key(published)
        assert has_valid_api_key(published) is False
        # Other configured keys keep working.
        assert validate_api_key("unique-key-abc") == "unique-key-abc"


def test_published_default_keys_configured_detection():
    from src.shared.auth.middleware import published_default_keys_configured
    assert published_default_keys_configured(_make_mock_config(True, "dev-key-1")) is True
    assert published_default_keys_configured(_make_mock_config(True, "a, GENERATED_BY_SETUP")) is True
    assert published_default_keys_configured(_make_mock_config(True, "unique-key-abc")) is False


def test_example_config_does_not_ship_a_usable_key():
    import json
    from pathlib import Path

    from src.shared.auth.middleware import PUBLISHED_DEFAULT_KEYS
    example = Path(__file__).resolve().parents[1] / "src" / "config" / "local-example-config.json"
    keys = {k.strip() for k in json.loads(example.read_text())["API_KEYS"].split(",") if k.strip()}
    assert keys <= PUBLISHED_DEFAULT_KEYS, "example API_KEYS must be a placeholder setup.sh replaces"
