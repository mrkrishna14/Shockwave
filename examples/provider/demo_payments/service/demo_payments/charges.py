"""Charge routes."""

from fastapi import APIRouter, HTTPException

from demo_payments import settings, store

router = APIRouter(prefix="/v1/charges", tags=["charges"])


@router.get("/{id}")
def retrieve_charge(id: str) -> dict:
    charge = store.CHARGES.get(id)
    if charge is None:
        raise HTTPException(status_code=404, detail="No such charge")
    return {
        "id": charge.id,
        "amount": store.present_amount(charge.amount_minor, settings.CONTRACT_VERSION),
        "currency": charge.currency,
    }


# Implemented and served, but intentionally absent from the hand-maintained spec.
@router.get("/{id}/receipt")
def retrieve_charge_receipt(id: str) -> dict:
    charge = store.CHARGES.get(id)
    if charge is None:
        raise HTTPException(status_code=404, detail="No such charge")
    return {"charge": charge.id, "receipt_number": f"rcpt_{charge.id}", "currency": charge.currency}
