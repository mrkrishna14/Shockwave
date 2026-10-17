"""A second application object, created through a module alias import."""

import fastapi as fa

admin_app = fa.FastAPI()


@admin_app.get("/admin/health")
def admin_health() -> dict:
    return {"status": "ok"}
