"""Operational routes outside the versioned public API namespace."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
