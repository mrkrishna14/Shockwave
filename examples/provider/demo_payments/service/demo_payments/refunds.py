"""Refund routes, mounted under a deploy-time configured prefix."""

from fastapi import APIRouter, HTTPException

from demo_payments import settings, store

router = APIRouter(prefix=settings.REFUNDS_PREFIX, tags=["refunds"])


@router.get("/{id}")
def retrieve_refund(id: str) -> dict:
    refund = store.REFUNDS.get(id)
    if refund is None:
        raise HTTPException(status_code=404, detail="No such refund")
    return {
        "id": refund.id,
        "charge": refund.charge,
        "amount": refund.amount_minor,  # USD cents under every contract version
        "currency": refund.currency,
    }
