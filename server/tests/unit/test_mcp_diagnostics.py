"""SDK wire diagnostics cannot disclose payment or response secrets."""
import logging

import pytest

from src.shared.x402.mcp_diagnostics import protect_mcp_diagnostics


@pytest.mark.parametrize('name', ['mcp.client.streamable_http', 'client'])
def test_exception_diagnostics_omit_remote_body(name, caplog):
    protect_mcp_diagnostics()
    logger = logging.getLogger(name)
    with caplog.at_level(logging.DEBUG, logger=name):
        try:
            raise ValueError('synthetic-private-response')
        except ValueError:
            logger.exception('Remote failure: synthetic-private-response')
        logger.info('Connection closed')
    assert 'synthetic-private-response' not in caplog.text
    assert 'MCP diagnostic: ValueError' in caplog.text
    assert 'Connection closed' in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_filter_registration_is_idempotent_and_protects_delayed_logs(caplog):
    protect_mcp_diagnostics()
    logger = logging.getLogger('mcp.client.streamable_http')
    before = list(logger.filters)
    protect_mcp_diagnostics()
    assert logger.filters == before
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        logger.debug('Sending client message: synthetic-private-request')
        logger.debug('X-Payment-Recovery-Token: synthetic-private-token')
        logger.debug('Unrelated transport event')
    assert 'synthetic-private' not in caplog.text
    assert 'Unrelated transport event' in caplog.text
