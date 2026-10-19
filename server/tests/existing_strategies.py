"""Seed historical execution records without reintroducing client authoring."""
from types import SimpleNamespace
from uuid import uuid4


def existing_strategy(req):
    from src.services import strategy_service as service
    detail = SimpleNamespace(id=str(uuid4()), name=req.name, asset=req.asset, status="inactive")
    local_id = service._insert_cache(
        detail, entry=req.entry, exit_rules=req.exit,
        execution_config=req.execution_config or {}, generation_report=None,
        evaluation_lane=req.evaluation_lane, market_context=req.market_context,
    )
    return service.get_strategy(local_id)


def existing_response(*args, json, **kwargs):
    from src.services.strategy_service import StrategyManualRequest
    result = existing_strategy(StrategyManualRequest(**json))
    return SimpleNamespace(status_code=201, json=lambda: result.model_dump(mode="json"))
