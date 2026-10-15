"""Authenticated execution-venue control; no arbitrary broker tool proxy."""

from fastapi import APIRouter, Depends

from src.services import broker_execution
from src.shared.auth.dependency import require_api_key

router = APIRouter(prefix="/brokers", tags=["brokers"], dependencies=[Depends(require_api_key)])


@router.get("/robinhood/status")
def status():
    return broker_execution.run(broker_execution.inspect)


@router.put("/strategies/{strategy_id}/venue")
def configure(strategy_id: str, allocation: broker_execution.VenueAllocation):
    return broker_execution.run(broker_execution.configure, strategy_id, allocation.model_dump())


@router.get("/strategies/{strategy_id}/orders")
def orders(strategy_id: str):
    return broker_execution.journal(strategy_id)


@router.post("/strategies/{strategy_id}/reconcile")
def reconcile(strategy_id: str):
    return broker_execution.run(broker_execution.reconcile, strategy_id)


@router.post("/orders/{order_id}/cancel")
def cancel(order_id: str):
    return broker_execution.run(broker_execution.cancel, order_id)
