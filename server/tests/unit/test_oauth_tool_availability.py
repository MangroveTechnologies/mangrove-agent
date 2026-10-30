"""OAuth discovery must not offer adapters using unrelated credential transports."""
from unittest.mock import AsyncMock

import pytest
from mcp.types import Tool

from src.mcp import mangrove_proxy, marketplace_proxy
from src.mcp.server import PricedFastMCP
from src.services import mangrove_oauth, tool_pricing


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(mangrove_oauth, 'enabled', lambda: True)
    monkeypatch.setattr(tool_pricing, 'enrich_tools', AsyncMock(side_effect=lambda entries: entries))
    remote = Tool(name='list_strategies', inputSchema={'type': 'object', 'properties': {}})
    monkeypatch.setattr(mangrove_proxy, 'catalog', AsyncMock(return_value=[remote]))
    markets = AsyncMock(return_value=[Tool(name='wallet_balance', inputSchema={'type': 'object'})])
    monkeypatch.setattr(marketplace_proxy, 'list_tools', markets)
    return markets


@pytest.mark.asyncio
async def test_real_local_catalog_preserves_custody_brokers_and_server_contract(catalog):
    from src.mcp.tools import register

    server = PricedFastMCP('availability-test')
    register(server)
    names = {tool.name for tool in await server.list_tools()}
    assert {'status', 'create_wallet', 'import_wallet', 'cex_validate_order',
            'agent_robinhood_status', 'agent_list_venue_orders', 'list_strategies'} <= names
    assert not names.intersection({
        'agent_start_paper_schedule', 'agent_list_paper_schedules',
        'agent_pause_paper_schedule', 'agent_get_paper_schedule',
        'agent_reconcile_paper_schedule', 'execute_swap', 'cex_sync_fills',
        'marketplace_prepare', 'marketplace_submit', 'wallet_balance',
    })
    catalog.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('name', [
    'agent_start_paper_schedule', 'agent_list_paper_schedules',
    'agent_pause_paper_schedule', 'agent_get_paper_schedule',
    'agent_reconcile_paper_schedule', 'execute_swap', 'cex_sync_fills',
    'marketplace_prepare', 'marketplace_submit', 'wallet_balance',
])
async def test_stale_or_direct_calls_never_reach_adapter_or_payment(catalog, monkeypatch, name):
    server = PricedFastMCP('availability-test')
    operation = AsyncMock(return_value='unexpected')
    server.add_tool(operation, name=name)
    markets_call = AsyncMock()
    upstream_call = AsyncMock()
    monkeypatch.setattr(marketplace_proxy, 'call_tool', markets_call)
    monkeypatch.setattr(mangrove_proxy, 'call_tool', upstream_call)
    result = await server.call_tool(name, {})
    assert result.isError
    assert result.structuredContent['code'] == 'OAUTH_TOOL_UNAVAILABLE'
    operation.assert_not_awaited()
    markets_call.assert_not_awaited()
    upstream_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_oauth_discovery_and_calls_keep_existing_paths(catalog, monkeypatch):
    monkeypatch.setattr(mangrove_oauth, 'enabled', lambda: False)
    server = PricedFastMCP('availability-test')

    @server.tool()
    async def agent_list_paper_schedules() -> str:
        return 'existing schedules'

    names = {tool.name for tool in await server.list_tools()}
    assert {'agent_list_paper_schedules', 'wallet_balance', 'list_strategies'} <= names
    catalog.assert_awaited_once()
    result = await server.call_tool('agent_list_paper_schedules', {})
    assert 'existing schedules' in str(result)
