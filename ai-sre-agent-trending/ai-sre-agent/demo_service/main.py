"""A tiny fake 'checkout' service that exposes Prometheus metrics, writes logs to Postgres,
and can be broken on purpose so the AI agent has something to investigate."""
import asyncio
import os
import random
import threading
import time
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE = "demo-service"
DSN = os.getenv("DATABASE_URL", "postgresql://sre:sre@postgres:5432/sre")

REQS = Counter("http_requests_total", "Total HTTP requests", ["service", "endpoint", "status"])
LAT = Histogram("http_request_duration_seconds", "Request latency in seconds",
                ["service", "endpoint"], buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10))

state = {"mode": "healthy", "version": "v2.4.0"}   # modes: healthy | bad_deploy | db_hog
_tasks: set = set()


def _write_log_sync(level: str, msg: str):
    try:
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute("INSERT INTO app_logs(service, level, message) VALUES (%s,%s,%s)",
                      (SERVICE, level, msg))
    except Exception as e:  # never crash the service because logging failed
        print("log write failed:", e)


async def log(level: str, msg: str):
    await asyncio.to_thread(_write_log_sync, level, msg)


def _hog():
    """Simulates a runaway report job: a REAL long-running query visible in pg_stat_activity."""
    try:
        with psycopg.connect(DSN, autocommit=True, application_name="demo_report_job") as c:
            c.execute("SELECT pg_sleep(900)")
    except Exception:
        pass  # query was terminated (e.g. by the agent's remediation)
    finally:
        if state["mode"] == "db_hog":
            state["mode"] = "healthy"


async def handle_request():
    t0, status, mode = time.time(), "200", state["mode"]
    if mode == "bad_deploy":
        await asyncio.sleep(random.uniform(0.03, 0.15))
        if random.random() < 0.7:
            status = "500"
            await log("ERROR", "NullPointerError in checkout handler (release v2.4.1)")
    elif mode == "db_hog":
        await asyncio.sleep(random.uniform(1.5, 3.0))
        if random.random() < 0.35:
            status = "500"
            await log("ERROR", "Timeout waiting for database lock on orders table")
        else:
            await log("WARN", "Slow database query detected on orders table")
    else:
        await asyncio.sleep(random.uniform(0.02, 0.12))
        if random.random() < 0.05:
            await log("INFO", "request completed")
    REQS.labels(SERVICE, "/checkout", status).inc()
    LAT.labels(SERVICE, "/checkout").observe(time.time() - t0)


async def loadgen():
    """Pretend users: ~4 requests per second."""
    while True:
        t = asyncio.create_task(handle_request())
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
        await asyncio.sleep(0.25)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(loadgen())
    yield
    task.cancel()


app = FastAPI(title="Demo Service", lifespan=lifespan)


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health():
    return state


@app.post("/admin/fault/{mode}")
async def inject_fault(mode: str):
    """Break the service on purpose. mode = bad_deploy | db_hog"""
    if mode == "bad_deploy":
        state.update(mode="bad_deploy", version="v2.4.1")
        await log("INFO", "Deployment of release v2.4.1 completed")
    elif mode == "db_hog":
        state["mode"] = "db_hog"
        await log("INFO", "Nightly report job started")
        threading.Thread(target=_hog, daemon=True).start()
    else:
        raise HTTPException(400, "mode must be bad_deploy or db_hog")
    return state


@app.post("/admin/restart")
async def restart():
    """Restart does NOT fix a bad release or a stuck DB query (on purpose - RCA matters!)."""
    await log("INFO", "Service restart completed")
    return state


@app.post("/admin/rollback")
async def rollback():
    if state["mode"] == "bad_deploy":
        state.update(mode="healthy", version="v2.4.0")
    await log("INFO", "Rollback to release v2.4.0 completed")
    return state
