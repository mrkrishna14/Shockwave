"""Application entry point for the fixture shop (parsed by the scanner, never run)."""

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from shop import legacy
from shop.routers import files, v1
from shop.routers.reports import router as reports_router
from vendor_auth import auth_router  # lives outside the source root

HEALTH_PATH = os.environ.get("HEALTH_PATH", "/healthz")

app = FastAPI(title="Fixture shop")

# @app.get('/fake-comment') is a comment, not a route.
app.include_router(v1.router, prefix="/api")
app.include_router(reports_router)
app.include_router(files.router)
app.include_router(auth_router, prefix="/auth")


@app.get("/internal/metrics", include_in_schema=False)
def metrics() -> dict:
    return {"requests": 0}


@app.get(HEALTH_PATH)
def health() -> dict:
    return {"status": "ok"}


def get_status() -> dict:
    return {"status": "ok"}


app.add_api_route("/status", get_status, methods=["GET", "HEAD"])


@app.websocket("/ws/updates")
async def updates(websocket) -> None:
    await websocket.accept()


app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/legacy", legacy.legacy_app)


@reports_router.get("/late")
def late_report() -> dict:
    """Registered after app.include_router(reports_router) ran, so FastAPI never serves it."""
    return {}
