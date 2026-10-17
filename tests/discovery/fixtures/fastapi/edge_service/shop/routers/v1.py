"""Version 1 API router; nests the users router below /accounts."""

from fastapi import APIRouter

from . import users

router = APIRouter(prefix="/v1")
router.include_router(users.router, prefix="/accounts")


@router.get("/ping")
def ping() -> dict:
    return {"pong": True}
