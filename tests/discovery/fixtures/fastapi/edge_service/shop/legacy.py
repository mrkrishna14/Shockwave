"""A legacy sub-application that main.py attaches with app.mount (served below /legacy)."""

from fastapi import FastAPI

legacy_app = FastAPI()


@legacy_app.get("/old")
def old() -> dict:
    return {"legacy": True}
