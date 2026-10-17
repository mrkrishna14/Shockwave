"""Intentionally invalid Python: the scanner must skip this file and say so."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/broken")
def broken(:
    return {}
