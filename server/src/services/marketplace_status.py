"""Parse server-owned product release context; this never grants execution."""

MESSAGE = "Agent to agent marketplace currently under construction."


def product_status(document: dict) -> dict | None:
    """Accept only the known bounded product declaration, discarding extras."""
    status = document.get("marketplace")
    if (not isinstance(status, dict) or type(status.get("version")) is not int
            or status["version"] != 1
            or status.get("product") != "agent_to_agent_marketplace"
            or status.get("availability") != "under_construction"
            or status.get("message") != MESSAGE):
        return None
    return {key: status[key] for key in ("version", "product", "availability", "message")}
