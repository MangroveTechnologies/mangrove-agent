"""Unit tests for shared/gcp_secret_utils.py.

Each test documents:
  - what inputs were passed
  - what exception type and message is expected

Covers every raise path in SecretUtils.get_secret plus the happy path.
"""
import json
from unittest.mock import MagicMock, patch

import pytest
from google.api_core import exceptions as google_exceptions

from src.shared.gcp_secret_utils import SecretResolutionError, SecretUtils


# ---------------------------------------------------------------------------
# Input validation — checked before any GCP call is made
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_empty_secret_id_raises():
    # passed: project_id="my-project", secret_id="", secret_property="api_key"
    # expected: SecretResolutionError("Secret ID or property not supplied.")
    with pytest.raises(SecretResolutionError, match="Secret ID or property not supplied."):
        SecretUtils.get_secret("my-project", "", "api_key")


@pytest.mark.fast
def test_none_secret_id_raises():
    # passed: project_id="my-project", secret_id=None, secret_property="api_key"
    # expected: SecretResolutionError("Secret ID or property not supplied.")
    with pytest.raises(SecretResolutionError, match="Secret ID or property not supplied."):
        SecretUtils.get_secret("my-project", None, "api_key")


@pytest.mark.fast
def test_empty_secret_property_raises():
    # passed: project_id="my-project", secret_id="my-secret", secret_property=""
    # expected: SecretResolutionError("Secret ID or property not supplied.")
    with pytest.raises(SecretResolutionError, match="Secret ID or property not supplied."):
        SecretUtils.get_secret("my-project", "my-secret", "")


@pytest.mark.fast
def test_none_secret_property_raises():
    # passed: project_id="my-project", secret_id="my-secret", secret_property=None
    # expected: SecretResolutionError("Secret ID or property not supplied.")
    with pytest.raises(SecretResolutionError, match="Secret ID or property not supplied."):
        SecretUtils.get_secret("my-project", "my-secret", None)


@pytest.mark.fast
def test_both_secret_id_and_property_empty_raises():
    # passed: project_id="my-project", secret_id="", secret_property=""
    # expected: SecretResolutionError("Secret ID or property not supplied.")
    with pytest.raises(SecretResolutionError, match="Secret ID or property not supplied."):
        SecretUtils.get_secret("my-project", "", "")


@pytest.mark.fast
def test_empty_project_id_raises():
    # passed: project_id="", secret_id="my-secret", secret_property="api_key"
    # expected: SecretResolutionError("GCP project id not supplied.")
    with pytest.raises(SecretResolutionError, match="GCP project id not supplied."):
        SecretUtils.get_secret("", "my-secret", "api_key")


@pytest.mark.fast
def test_none_project_id_raises():
    # passed: project_id=None, secret_id="my-secret", secret_property="api_key"
    # expected: SecretResolutionError("GCP project id not supplied.")
    with pytest.raises(SecretResolutionError, match="GCP project id not supplied."):
        SecretUtils.get_secret(None, "my-secret", "api_key")


# ---------------------------------------------------------------------------
# GCP API failures
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_not_found_raises_with_secret_and_project_in_message():
    # passed: project_id="prod-project", secret_id="db-password", secret_property="value"
    # expected: SecretResolutionError("Secret 'db-password' not found in project 'prod-project'.")
    with patch("src.shared.gcp_secret_utils.secretmanager.SecretManagerServiceClient") as mock_cls:
        mock_cls.return_value.access_secret_version.side_effect = google_exceptions.NotFound("404")

        with pytest.raises(SecretResolutionError) as exc_info:
            SecretUtils.get_secret("prod-project", "db-password", "value")

        assert str(exc_info.value) == "Secret 'db-password' not found in project 'prod-project'."


@pytest.mark.fast
def test_generic_api_error_raises_with_secret_id_in_message():
    # passed: project_id="prod-project", secret_id="api-keys", secret_property="mangrove"
    # expected: SecretResolutionError("Error accessing secret 'api-keys': network timeout.")
    with patch("src.shared.gcp_secret_utils.secretmanager.SecretManagerServiceClient") as mock_cls:
        mock_cls.return_value.access_secret_version.side_effect = RuntimeError("network timeout")

        with pytest.raises(SecretResolutionError) as exc_info:
            SecretUtils.get_secret("prod-project", "api-keys", "mangrove")

        assert str(exc_info.value) == "Error accessing secret 'api-keys': network timeout."


@pytest.mark.fast
def test_generic_exception_chains_original_cause():
    # passed: project_id="prod-project", secret_id="my-secret", secret_property="key"
    # expected: SecretResolutionError.__cause__ is the original RuntimeError
    original = RuntimeError("connection refused")
    with patch("src.shared.gcp_secret_utils.secretmanager.SecretManagerServiceClient") as mock_cls:
        mock_cls.return_value.access_secret_version.side_effect = original

        with pytest.raises(SecretResolutionError) as exc_info:
            SecretUtils.get_secret("prod-project", "my-secret", "key")

        assert exc_info.value.__cause__ is original


# ---------------------------------------------------------------------------
# Payload parsing failures
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_property_missing_from_json_payload_raises():
    # passed: project_id="prod-project", secret_id="my-secret", secret_property="missing_key"
    # payload contains: {"api_key": "abc"} — "missing_key" is not present
    # expected: SecretResolutionError wrapping the KeyError
    payload = json.dumps({"api_key": "abc"}).encode("UTF-8")

    with patch("src.shared.gcp_secret_utils.secretmanager.SecretManagerServiceClient") as mock_cls:
        mock_response = MagicMock()
        mock_response.payload.data = payload
        mock_cls.return_value.access_secret_version.return_value = mock_response

        with pytest.raises(SecretResolutionError, match="Error accessing secret 'my-secret'"):
            SecretUtils.get_secret("prod-project", "my-secret", "missing_key")


@pytest.mark.fast
def test_malformed_json_payload_raises():
    # passed: project_id="prod-project", secret_id="bad-secret", secret_property="key"
    # payload is not valid JSON: b"not-json"
    # expected: SecretResolutionError wrapping the JSONDecodeError
    with patch("src.shared.gcp_secret_utils.secretmanager.SecretManagerServiceClient") as mock_cls:
        mock_response = MagicMock()
        mock_response.payload.data = b"not-json"
        mock_cls.return_value.access_secret_version.return_value = mock_response

        with pytest.raises(SecretResolutionError, match="Error accessing secret 'bad-secret'"):
            SecretUtils.get_secret("prod-project", "bad-secret", "key")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_returns_correct_property_from_multi_property_payload():
    # passed: project_id="prod-project", secret_id="app-secrets", secret_property="db_password"
    # payload contains: {"api_key": "abc123", "db_password": "hunter2", "webhook": "xyz"}
    # expected: returns "hunter2" (the exact property requested, not the others)
    payload = json.dumps({
        "api_key": "abc123",
        "db_password": "hunter2",
        "webhook": "xyz",
    }).encode("UTF-8")

    with patch("src.shared.gcp_secret_utils.secretmanager.SecretManagerServiceClient") as mock_cls:
        mock_response = MagicMock()
        mock_response.payload.data = payload
        mock_cls.return_value.access_secret_version.return_value = mock_response

        result = SecretUtils.get_secret("prod-project", "app-secrets", "db_password")

        assert result == "hunter2"


@pytest.mark.fast
def test_default_version_id_is_latest():
    # passed: project_id="prod-project", secret_id="my-secret", secret_property="key" (no version_id)
    # expected: GCP is called with name ending in "/versions/latest"
    payload = json.dumps({"key": "value"}).encode("UTF-8")

    with patch("src.shared.gcp_secret_utils.secretmanager.SecretManagerServiceClient") as mock_cls:
        mock_response = MagicMock()
        mock_response.payload.data = payload
        mock_cls.return_value.access_secret_version.return_value = mock_response

        SecretUtils.get_secret("prod-project", "my-secret", "key")

        expected_name = "projects/prod-project/secrets/my-secret/versions/latest"
        mock_cls.return_value.access_secret_version.assert_called_once_with(
            request={"name": expected_name}
        )


@pytest.mark.fast
def test_custom_version_id_is_passed_to_gcp():
    # passed: project_id="prod-project", secret_id="my-secret", secret_property="key", version_id="42"
    # expected: GCP is called with name ending in "/versions/42"
    payload = json.dumps({"key": "value"}).encode("UTF-8")

    with patch("src.shared.gcp_secret_utils.secretmanager.SecretManagerServiceClient") as mock_cls:
        mock_response = MagicMock()
        mock_response.payload.data = payload
        mock_cls.return_value.access_secret_version.return_value = mock_response

        SecretUtils.get_secret("prod-project", "my-secret", "key", version_id="42")

        expected_name = "projects/prod-project/secrets/my-secret/versions/42"
        mock_cls.return_value.access_secret_version.assert_called_once_with(
            request={"name": expected_name}
        )
