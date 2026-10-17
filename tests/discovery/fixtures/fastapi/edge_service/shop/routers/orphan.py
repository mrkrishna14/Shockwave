"""A router that nothing in the scanned tree includes (it may be mounted dynamically)."""

from fastapi import APIRouter

router = APIRouter(prefix="/orphan")


@router.get("/ping")
def orphan_ping() -> dict:
    return {"pong": True}
