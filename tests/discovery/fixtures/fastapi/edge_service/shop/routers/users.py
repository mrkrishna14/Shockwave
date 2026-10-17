"""User routes, served below /api/v1/accounts."""

import fastapi

router = fastapi.APIRouter(prefix="/users", tags=["users"])


@router.get("/{user_id}")
def get_user(user_id: str) -> dict:
    return {"id": user_id}


@router.post("")
def create_user() -> dict:
    return {"id": "usr_new"}
