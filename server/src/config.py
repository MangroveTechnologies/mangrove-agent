"""Application configuration singleton.

Loads per-environment JSON config files from src/config/.
Resolves GCP Secret Manager references using secret:name:property syntax.

Key validation:
- "required" keys: must be present in config file, app fails without them
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

        required_keys, full_app_keys = self.get_configuration_keys()
        self.load_config_file()

        gcp_project_id = os.getenv("GCP_PROJECT_ID")
        if not gcp_project_id:
            print("GCP_PROJECT_ID not set. Secret Manager lookups will fail.")

        # Validate and load required keys -- fail if any missing
        for key in required_keys:
            if key not in self._raw_config:
                print(f"Required configuration key '{key}' missing from config file.")
                sys.exit(1)
            key_value = self.get_key_value(key, gcp_project_id)
            if str(key_value).strip().lower() in {"none", "null"}:
                key_value = None
            setattr(self, key, key_value)

        # Load full_app_keys only if present in config file.
        # If present, validate they have non-empty values (preflight check).
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
    def get_configuration_keys() -> tuple[set, set]:
        """Load required and full_app_keys from configuration-keys.json."""
        try:
            config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
            keys_path = os.path.join(config_dir, "configuration-keys.json")
            with open(keys_path, "r") as f:
                keys_data = json.load(f)
                required = set(keys_data.get("required", []))
                full_app = set(keys_data.get("full_app_keys", []))
                return required, full_app
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Failed to load configuration-keys.json: {e}")
            sys.exit(1)

    def load_config_file(self):
        environment = os.getenv("ENVIRONMENT") or os.getenv("APP_ENV")
        filename = f"{environment}-config.json"
        try:
            config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
            file_path = os.path.join(config_dir, filename)

            if not os.path.exists(file_path):
                print(f"Config file not found: {file_path}")
                sys.exit(1)

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
