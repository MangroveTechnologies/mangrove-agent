"""Internal custody role, enabled only after the service's OS checks.

This flag is routing, not the security boundary. Separate OS ownership of keys,
policy, installation and process provides that boundary.
"""
_active = False


def active() -> bool:
    return _active


def require() -> None:
    if not active():
        from src.shared.errors import SigningError
        raise SigningError("Wallet secrets are available only to the isolated custody service.")
