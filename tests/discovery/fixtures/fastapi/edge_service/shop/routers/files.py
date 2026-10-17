"""File downloads using a Starlette path converter."""

from fastapi.routing import APIRouter

router = APIRouter(prefix="/files")


@router.get("/{file_path:path}")
def download(file_path: str) -> dict:
    return {"path": file_path}
