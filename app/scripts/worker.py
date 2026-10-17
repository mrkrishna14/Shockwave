"""Run the durable local job worker. Start separately from the API server."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.main import create_app


def main():
    app=create_app()
    while True:
        app.state.outbox.reconcile_abandoned()
        pending=app.state.outbox.pending()
        for id in pending: app.state.outbox.dispatch(id)
        claim=app.state.queue.claim()
        if claim: app.state.orchestrator.run(claim)
        elif not pending: time.sleep(1)


if __name__=='__main__': main()
