"""Demo Payments application. Run: uvicorn demo_payments.main:app (from the service directory)."""

from fastapi import FastAPI

from demo_payments import charges, health, refunds

app = FastAPI(title="Demo Payments (fictional)", version="1.0.0")
app.include_router(charges.router)
app.include_router(refunds.router)
app.include_router(health.router)
