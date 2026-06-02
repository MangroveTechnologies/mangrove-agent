"""GCP Secret Manager utilities."""
import json
from typing import Optional

from google.api_core import exceptions as google_exceptions
from google.cloud import secretmanager


class SecretResolutionError(Exception):
    """Raised when a GCP secret cannot be resolved."""


class SecretUtils:
    @staticmethod
    def get_secret(
        project_id: str,
        secret_id: str,
        secret_property: str,
        version_id: str = "latest",
    ) -> Optional[str]:
        if not secret_id or not secret_property:
            raise SecretResolutionError("Secret ID or property not supplied.")

        if not project_id:
            raise SecretResolutionError("GCP project id not supplied.")

        try:
            client = secretmanager.SecretManagerServiceClient()
            name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"
            response = client.access_secret_version(request={"name": name})
            payload = response.payload.data.decode("UTF-8")
            return json.loads(payload)[secret_property]

        except google_exceptions.NotFound:
            raise SecretResolutionError(
                f"Secret '{secret_id}' not found in project '{project_id}'."
            )
        except SecretResolutionError:
            raise
        except Exception as e:
            raise SecretResolutionError(
                f"Error accessing secret '{secret_id}': {e}."
            ) from e
