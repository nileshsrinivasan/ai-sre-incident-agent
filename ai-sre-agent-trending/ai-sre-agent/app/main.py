"""FastAPI front door.
  Dashboard : http://localhost:8000/        (click-through console)
  API docs  : http://localhost:8000/docs    (every endpoint as a button)"""
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel

from . import db
from .config import settings
from .graph import build_graph

DASHBOARD_FILE = Path(__file__).parent / "dashboard.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.CHECKPOINTER == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        async with AsyncPostgresSaver.from_conn_string(settings.DATABASE_URL) as saver:
            await saver.setup()                       # creates LangGraph's own tables
            app.state.graph = build_graph(saver)
            yield
    else:
        app.state.graph = build_graph(MemorySaver())
        yield


app = FastAPI(title="AI SRE Incident Response Agent", lifespan=lifespan)


class IncidentIn(BaseModel):
    title: str = "Checkout failing for customers"
    service: str = "demo-service"
    description: str = ""


class DecisionIn(BaseModel):
    approved: bool
    approver: str = "oncall-engineer"
    comment: str = ""


def _cfg(incident_id: int):
    return {"configurable": {"thread_id": f"incident-{incident_id}"}}


async def _snapshot(graph, incident_id: int):
    st = await graph.aget_state(_cfg(incident_id))
    return {"awaiting_approval": "approval_gate" in (st.next or ()), "next_step": list(st.next or ()),
            "state": st.values}


# ------------------------------------------------------------------ dashboard
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return DASHBOARD_FILE.read_text(encoding="utf-8")


@app.get("/health")
def health():
    return {"ok": True, "ai_mode": "openai" if settings.OPENAI_API_KEY else "rules-only",
            "model": settings.OPENAI_MODEL if settings.OPENAI_API_KEY else None}


# ------------------------------------------------------------------ incidents
@app.get("/incidents", summary="List recent incidents")
def list_incidents():
    return db.fetch("SELECT id, title, service, severity, status, summary, created_at "
                    "FROM incidents ORDER BY id DESC LIMIT 30")


@app.post("/incidents", summary="1. Create an incident")
def create_incident(body: IncidentIn):
    row = db.fetch_one("INSERT INTO incidents(title, service, description) VALUES (%s,%s,%s) RETURNING *",
                       (body.title, body.service, body.description))
    db.audit(row["id"], "human", "incident_created", body.model_dump())
    return row


@app.post("/incidents/{incident_id}/investigate", summary="2. Run triage -> RCA -> plan (stops at approval)")
async def investigate(incident_id: int, request: Request):
    inc = db.fetch_one("SELECT * FROM incidents WHERE id=%s", (incident_id,))
    if not inc:
        raise HTTPException(404, "incident not found")
    if inc["status"] != "open":
        raise HTTPException(409, f"incident already in status '{inc['status']}'")
    graph = request.app.state.graph
    await graph.ainvoke({"incident_id": incident_id, "service": inc["service"], "timeline": []}, _cfg(incident_id))
    return await _snapshot(graph, incident_id)


@app.get("/approvals/pending", summary="List plans waiting for a human decision")
def pending_approvals():
    return db.fetch("SELECT * FROM approvals WHERE status='pending' ORDER BY id")


@app.post("/incidents/{incident_id}/approve", summary="3. Approve or reject the plan (resumes the workflow)")
async def decide(incident_id: int, body: DecisionIn, request: Request):
    ap = db.fetch_one("SELECT * FROM approvals WHERE incident_id=%s AND status='pending' ORDER BY id DESC LIMIT 1",
                      (incident_id,))
    if not ap:
        raise HTTPException(409, "no pending approval for this incident")
    db.run("UPDATE approvals SET status=%s, approver=%s, comment=%s, decided_at=now() WHERE id=%s",
           ("approved" if body.approved else "rejected", body.approver, body.comment, ap["id"]))
    db.audit(incident_id, f"human:{body.approver}", "approved" if body.approved else "rejected",
             {"approval_id": ap["id"], "comment": body.comment})
    graph = request.app.state.graph
    # NOTE: when approved this call waits ~VERIFY_WAIT_SECONDS while the agent verifies the fix.
    await graph.ainvoke(Command(resume=body.model_dump()), _cfg(incident_id))
    return await _snapshot(graph, incident_id)


@app.get("/incidents/{incident_id}", summary="Full picture: incident, workflow state, approvals")
async def get_incident(incident_id: int, request: Request):
    inc = db.fetch_one("SELECT * FROM incidents WHERE id=%s", (incident_id,))
    if not inc:
        raise HTTPException(404, "incident not found")
    return {"incident": inc,
            "workflow": await _snapshot(request.app.state.graph, incident_id),
            "approvals": db.fetch("SELECT * FROM approvals WHERE incident_id=%s ORDER BY id", (incident_id,))}


@app.get("/incidents/{incident_id}/audit", summary="Append-only audit trail")
def audit_trail(incident_id: int):
    return db.fetch("SELECT ts, actor, event, detail FROM audit_log WHERE incident_id=%s ORDER BY id", (incident_id,))


# ------------------------------------------------------------------ demo controls (for the dashboard)
# These break / reset the FAKE demo service so you can practise. They are NOT agent actions.
def _demo(method: str, path: str):
    try:
        r = httpx.request(method, f"{settings.DEMO_SERVICE_URL}{path}", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise HTTPException(502, f"demo service not reachable: {e}")


@app.get("/demo/status", summary="Is the demo service healthy or broken?")
def demo_status():
    return _demo("GET", "/health")


@app.post("/demo/fault/{mode}", summary="Break the demo service on purpose (bad_deploy or db_hog)")
def demo_fault(mode: str):
    if mode not in ("bad_deploy", "db_hog"):
        raise HTTPException(400, "mode must be bad_deploy or db_hog")
    return _demo("POST", f"/admin/fault/{mode}")


@app.post("/demo/heal", summary="Manually reset the demo service (not done by the agent)")
def demo_heal():
    _demo("POST", "/admin/rollback")
    db.fetch("SELECT pg_terminate_backend(pid) AS ok FROM pg_stat_activity "
             "WHERE application_name='demo_report_job' AND pid<>pg_backend_pid()")
    db.audit(None, "human", "demo_manual_reset", {})
    time.sleep(1)
    return _demo("GET", "/health")
