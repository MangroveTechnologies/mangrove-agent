"""Generated protected configuration must resolve services without network I/O."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def installer():
    script = Path(__file__).resolve().parents[3] / "scripts/install-custody.py"
    spec = importlib.util.spec_from_file_location("custody_installer_config", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_custody_configuration_pins_reviewed_local_service_default(installer, tmp_path):
    from src.shared.clients.mangrove import _payment_destination

    root = Path(__file__).resolve().parents[3]
    catalog = json.loads((root / "server/src/config/mangrove-endpoints.json").read_text())
    example = json.loads((root / "server/src/config/local-example-config.json").read_text())
    application = installer.custody_application(example, catalog, tmp_path)
    assert "X402_MANGROVE_ENVIRONMENT" not in example
    assert application["X402_MANGROVE_ENVIRONMENT"] == catalog["default_environment"]["local"]
    config = SimpleNamespace(ENVIRONMENT="custody", MANGROVE_ENDPOINTS=catalog, **application)
    destination = _payment_destination(config)
    assert destination["environment"] == "prod"
    assert destination["base_url"] == catalog["environments"]["prod"]["base_url"]
    assert application["MANGROVE_API_KEY"] == "" and "CUSTODY_SOCKET" not in application


def test_explicit_reviewed_service_override_is_preserved(installer, tmp_path):
    catalog = {"default_environment": {"local": "prod"}, "environments": {"local": {}, "prod": {}}}
    application = installer.custody_application({"X402_MANGROVE_ENVIRONMENT": "local"}, catalog, tmp_path)
    assert application["X402_MANGROVE_ENVIRONMENT"] == "local"
    with pytest.raises(ValueError, match="reviewed"):
        installer.custody_application({"X402_MANGROVE_ENVIRONMENT": "unreviewed"}, catalog, tmp_path)


def test_backup_command_uses_protected_installation_and_policy_paths(monkeypatch):
    import shlex

    from src.custody import runtime
    from src.services.wallet_manager import _reveal_cmd_for_address

    prefix = (
        "sudo", "-u", "custody-user", "/Library/Protected Custody/.venv/bin/python", "-I",
        "/Library/Protected Custody/scripts/custody-service.py", "admin", "--config",
        "/Library/Protected Custody/configuration/policy.json",
    )
    address = "0x" + "11" * 20
    monkeypatch.setattr(runtime, "_admin_command_prefix", prefix)
    assert shlex.split(_reveal_cmd_for_address(address)) == [*prefix, "backup", address]
