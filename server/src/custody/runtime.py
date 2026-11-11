"""Internal custody role, enabled only after the service's OS checks.

This flag is routing, not the security boundary. Separate OS ownership of keys,
policy, installation and process provides that boundary.
"""
_active = False
_admin_command_prefix: tuple[str, ...] | None = None


def active() -> bool:
    return _active


def admin_command_prefix() -> tuple[str, ...] | None:
    """Command that runs the protected custody administrator, set by bootstrap."""
    return _admin_command_prefix


def require() -> None:
    if not active():
        from src.shared.errors import SigningError
        raise SigningError("Wallet secrets are available only to the isolated custody service.")
