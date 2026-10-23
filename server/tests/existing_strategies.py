"""Seed historical execution records without reintroducing client authoring."""
from types import SimpleNamespace
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from src.services.strategy_service import StrategyMarketContext


class StrategyManualRequest(BaseModel):
    name: str
    asset: str
    timeframe: str
    entry: list[dict[str, Any]]
    exit: list[dict[str, Any]] = Field(default_factory=list)
    execution_config: dict[str, Any] | None = None
    market_context: StrategyMarketContext | None = None
    evaluation_lane: Literal["server", "stateless"] | None = None


def existing_strategy(req):
    import json
    from datetime import datetime, timezone

    from src.services.strategy_service import get_strategy
    from src.shared.db.sqlite import get_connection

    local_id, remote_id = str(uuid4()), str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    conn.execute(
        """INSERT INTO strategies
           (id, mangrove_id, name, asset, timeframe, status, entry_json, exit_json,
            execution_config_json, evaluation_lane, created_at, updated_at, market_context_json)
           VALUES (?, ?, ?, ?, ?, 'inactive', ?, ?, ?, ?, ?, ?, ?)""",
        (local_id, remote_id, req.name, req.asset, req.timeframe,
         json.dumps(req.entry), json.dumps(req.exit), json.dumps(req.execution_config or {}),
         req.evaluation_lane, now, now,
         req.market_context.model_dump_json() if req.market_context else None),
    )
    conn.commit()
    return get_strategy(local_id)


def existing_response(*args, json, **kwargs):
    result = existing_strategy(StrategyManualRequest(**json))
    return SimpleNamespace(status_code=201, json=lambda: result.model_dump(mode="json"))
