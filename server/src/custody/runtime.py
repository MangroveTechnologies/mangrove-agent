"""Internal custody role, enabled only after the service's OS checks.

The active flag and the administrator command prefix are routing state, not the
security boundary. Separate OS ownership of keys, policy, installation and
process provides that boundary.
"""
_active = False
_admin_command_prefix: tuple[str, ...] | None = None


def active() -> bool:
    return _active


def admin_command_prefix() -> tuple[str, ...] | None:
    """Return the argv prefix that runs the protected custody administrator, or None outside an initialized custody process."""
    return _admin_command_prefix


def require() -> None:
    if not active():
        from src.shared.errors import SigningError
        raise SigningError("Wallet secrets are available only to the isolated custody service.")
