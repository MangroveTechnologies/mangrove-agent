"""Protect payment metadata in official MCP SDK diagnostics, including existing tasks."""
import logging


class _PaymentLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage().lower()
        if any(marker in message for marker in (
            "x402/payment", "recovery_token", "payment-signature", "payment-recovery-token",
            "sending client message:", "sse message:", "raw result:",
            "received session id:",
        )):
            return False
        if record.exc_info:
            # SDK parsing tracebacks can contain entire remote response bodies.
            record.msg = "MCP diagnostic: %s"
            error_type = record.exc_info[0]
            record.args = (error_type.__name__ if error_type else "unknown error",)
            record.exc_info = None
            record.exc_text = None
        return True


def protect_mcp_diagnostics() -> None:
    guard = _PaymentLogFilter()
    for name in ("mcp.client.streamable_http", "client"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, _PaymentLogFilter) for item in logger.filters):
            logger.addFilter(guard)
