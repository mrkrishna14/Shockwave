"""Report routes: a module-constant prefix and explicit api_route methods."""

from fastapi import APIRouter as Router

REPORTS_PREFIX = "/reports"

router = Router(prefix=REPORTS_PREFIX)


@router.api_route("/export", methods=["POST", "put"])
def export_report() -> dict:
    return {"queued": True}


@router.api_route("/summary")
def summary() -> dict:
    return {"total": 0}
