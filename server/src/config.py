"""Application configuration singleton.

Loads per-environment JSON config files from src/config/.
Resolves GCP Secret Manager references using secret:name:property syntax.

Key validation:
- "required" keys: must be present in config file, app fails without them
- "optional" keys: resolve normally when present, otherwise None; consumers
  validate them only when the corresponding feature is used
- "full_app_keys": validated only if present in config file (preflight check
  for full-stack deployments with DB + Redis). If a full_app_key is present
  in the config but has an empty value, startup fails -- this catches
  misconfiguration early.
"""
import json
import os
import sys

from src.shared.gcp_secret_utils import SecretResolutionError, SecretUtils


class _Config:
    def __init__(self):
        self._raw_config = {}
        self.load_configuration()

    def load_configuration(self):
        environment = os.getenv("ENVIRONMENT") or os.getenv("APP_ENV")
        if not environment:
            print("ENVIRONMENT or APP_ENV is not set.")
            sys.exit(1)
        setattr(self, "ENVIRONMENT", environment)

        required_keys, full_app_keys, optional_keys = self.get_configuration_keys()
        self.load_config_file()

        gcp_project_id = os.getenv("GCP_PROJECT_ID")
        # Only warn when Secret Manager will actually be used — i.e. a config
        # value is a `secret:name:property` reference. Local configs use plain
        # values, so an unconditional warning here just alarms local
        # users for a lookup that never happens.
        uses_secret_refs = any(
            isinstance(v, str) and v.startswith("secret:")
            for v in self._raw_config.values()
        )
        if not gcp_project_id and uses_secret_refs:
            print("GCP_PROJECT_ID not set but config has secret: references; "
                  "Secret Manager lookups will fail.")

        self._load_required_keys(required_keys, gcp_project_id)
        self._load_full_app_keys(full_app_keys, gcp_project_id)
        for key in optional_keys:
            setattr(self, key, self.get_key_value(key, gcp_project_id))
        # Reviewed service destinations ship with the app. They are separate
        # from user config so old installs need no migration or URL entry.
        config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
        with open(os.path.join(config_dir, "mangrove-endpoints.json")) as f:
            self.MANGROVE_ENDPOINTS = json.load(f)
        self._anchor_state_paths()

        # LOCAL_AGENT_URL — this agent's own local surface. Wallet/secret ops
        # (key generation, the SecretVault, reveal/stash/confirm) live HERE, on
        # the client machine, and must never be a remote host. Distinct from
        # MANGROVEMARKETS_BASE_URL (the remote, keyless DEX-routing server).
        # Defaulted (not required) so existing configs without the key still boot.
        self.LOCAL_AGENT_URL = self._raw_config.get("LOCAL_AGENT_URL") or "http://localhost:9080"

        # X402_PAYER_WALLET — the wallet outbound x402 payments are signed
        # with. Optional and EMPTY by default on purpose: no wallet is chosen
        # on the user's behalf, so a caller must either pass an address
        # explicitly or opt in here. Defaulted (not required) so configs
        # written before this key still boot, same as LOCAL_AGENT_URL above.
        self.X402_PAYER_WALLET = self._raw_config.get("X402_PAYER_WALLET") or ""

    def _anchor_state_paths(self) -> None:
        """Resolve relative DB_PATH / MASTER_KEY_PATH against MANGROVE_AGENT_HOME.

        Without this, `./agent-data/agent.db` would follow the process cwd — for
        a plugin that could be the versioned cache dir, and a plugin update
        would silently start a fresh, empty wallet DB.
        """
        home = self.agent_home()
        if not home:
            return
        for key in ("DB_PATH", "MASTER_KEY_PATH"):
            value = getattr(self, key, None)
            if not value or str(value) == ":memory:":
                continue
            path = os.path.expanduser(str(value))
            if not os.path.isabs(path):
                setattr(self, key, os.path.normpath(os.path.join(home, path)))

    def _load_required_keys(self, required_keys: set, gcp_project_id: str) -> None:
        """Validate and set all required config keys. Exits on missing or null values."""
        for key in required_keys:
            if key not in self._raw_config:
                print(f"Required configuration key '{key}' missing from config file.")
                sys.exit(1)
            key_value = self.get_key_value(key, gcp_project_id)
            if str(key_value).strip().lower() in {"none", "null"}:
                key_value = None
            setattr(self, key, key_value)

    def _load_full_app_keys(self, full_app_keys: set, gcp_project_id: str) -> None:
        """Load full_app_keys only if present. Exits if present but empty (preflight check)."""
        for key in full_app_keys:
            if key in self._raw_config:
                value = self._raw_config[key]
                str_val = str(value).strip()
                if str_val == "" or str_val.lower() in {"none", "null"}:
                    print(f"Full-app key '{key}' is present in config but empty. "
                          f"Either provide a value or remove the key for minimal mode.")
                    sys.exit(1)
                key_value = self.get_key_value(key, gcp_project_id)
                setattr(self, key, key_value)

    @staticmethod
    def get_configuration_keys() -> tuple[set, set, set]:
        """Load required, full-app and optional keys from the config manifest."""
        try:
            config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
            keys_path = os.path.join(config_dir, "configuration-keys.json")
            with open(keys_path, "r") as f:
                keys_data = json.load(f)
                required = set(keys_data.get("required", []))
                full_app = set(keys_data.get("full_app_keys", []))
                return required, full_app, set(keys_data.get("optional", []))
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Failed to load configuration-keys.json: {e}")
            sys.exit(1)

    @staticmethod
    def agent_home() -> str | None:
        """MANGROVE_AGENT_HOME — where a plugin install keeps config + state.

        Plugin code lives in a versioned cache dir that Claude Code replaces on
        every update, so config, wallets, the DB and the master key must not.
        Unset (git clone): config stays in src/config/, state in ./agent-data.
        """
        home = os.getenv("MANGROVE_AGENT_HOME")
        return os.path.abspath(os.path.expanduser(home)) if home else None

    def load_config_file(self):
        environment = os.getenv("ENVIRONMENT") or os.getenv("APP_ENV")
        filename = f"{environment}-config.json"
        try:
            home = self.agent_home()
            if home:
                config_dir = os.path.join(home, "config")
            else:
                config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
            file_path = os.path.join(config_dir, filename)

            if not os.path.exists(file_path):
                print(f"Config file not found: {file_path}")
                sys.exit(1)

            # Local config can hold API credentials. Preserve owner-only mode
            # even when it was written outside setup.sh.
            if environment == "local" and os.name == "posix":
                os.chmod(file_path, 0o600)
            with open(file_path, "r") as f:
                config_data = json.load(f)
            self._raw_config.update(config_data)

        except Exception as e:
            print(f"Error loading {filename}: {e}")
            sys.exit(1)

    def get_key_value(self, key: str, project_id: str) -> str:
        value = self._raw_config.get(key)
        str_val = str(value)
        if str_val.startswith("secret:"):
            parts = str_val.split(":")
            if len(parts) != 3 or not parts[1] or not parts[2]:
                print(
                    f"Malformed secret reference for '{key}': '{str_val}'. "
                    f"Expected 'secret:<name>:<property>', got {len(parts)} segment(s)."
                )
                sys.exit(1)
            secret_id = parts[1]
            secret_property = parts[2]
            try:
                value = SecretUtils.get_secret(project_id, secret_id, secret_property)
            except SecretResolutionError as e:
                print(f"Secret resolution failed for '{key}': {e}")
                sys.exit(1)
        return value


app_config = _Config()
