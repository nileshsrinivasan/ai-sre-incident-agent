"""The incident-response workflow, built with LangGraph.

   triage -> rca -> plan -> approval_gate --(approved)--> execute -> verify -> report
                                   \\--(rejected)------------------------------> report
The graph PAUSES at approval_gate (state saved in Postgres) until a human calls the approve API."""
import asyncio
import operator
import re
from datetime import datetime, timezone
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import db, llm
from .config import ALLOWED_ACTIONS, settings
from .mcp_client import mcp_session


class IncidentState(TypedDict, total=False):
    incident_id: int
    service: str
    incident: dict
    health: dict
    triage: dict
    logs: dict
    db_diag: dict
    similar: dict
    rca: dict
    plan: dict
    approval_id: int
    approval: dict
    execution: dict
    verification: dict
    status: str
    summary: str
    timeline: Annotated[list, operator.add]     # every step appends here -> traceable history


def ev(step, msg):
    return {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "step": step, "msg": msg}


def set_status(iid, status, severity=None, summary=None):
    db.run("UPDATE incidents SET status=%s, severity=COALESCE(%s,severity), "
           "summary=COALESCE(%s,summary), updated_at=now() WHERE id=%s", (status, severity, summary, iid))
    db.audit(iid, "workflow", "status_change", {"status": status})


# ------------------------------------------------------------------ 1. TRIAGE
def _triage_rules(h):
    er, p95 = h.get("error_ratio") or 0, h.get("p95_latency_seconds") or 0
    sev = "SEV1" if (h.get("status") == "down" or er > 0.5) else "SEV2" if (er > 0.1 or p95 > 1) \
        else "SEV3" if er > 0.05 else "SEV4"
    hyps = []
    if er > 0.05 and p95 < 1:
        hyps.append("Errors without high latency: possible bad release/code regression")
    if p95 > 1:
        hyps.append("High latency: possible database or dependency slowness")
    return {"severity": sev, "impact": f"error ratio {er:.0%}, p95 latency {p95:.2f}s",
            "hypotheses": hyps or ["No clear symptom yet"], "next_checks": ["logs", "database"]}


async def triage(state: IncidentState):
    iid = state["incident_id"]
    async with mcp_session(iid) as call:
        incident = await call("get_incident", {"incident_id": iid})
        health = await call("check_service_health", {"service": state["service"]})
    result, src = await llm.ask_json(
        "You are an SRE triaging an incident. Using the incident and Prometheus golden signals, return JSON: "
        '{"severity":"SEV1|SEV2|SEV3|SEV4","impact":"one sentence","hypotheses":["..."],"next_checks":["..."]}',
        {"incident": incident, "health": health}, lambda: _triage_rules(health))
    set_status(iid, "triaged", severity=result.get("severity"))
    return {"incident": incident, "health": health, "triage": result,
            "timeline": [ev("triage", f"{result.get('severity')} - {result.get('impact')} (by {src})")]}


# ------------------------------------------------------------------ 2. ROOT-CAUSE ANALYSIS
def _rca_rules(logs, dbd):
    long_q = dbd.get("long_running_queries") or []
    msgs = " ".join(m["message"] for m in logs.get("top_messages", []))
    if long_q:
        q = long_q[0]
        return {"category": "db_long_running_query", "confidence": 0.85,
                "root_cause": f"A long-running query from '{q['application_name']}' ({q['seconds']}s) is "
                              "holding the database, causing slow queries and lock timeouts.",
                "evidence": dbd["findings"] + ["Logs show lock timeouts / slow queries"]}
    if "NullPointerError" in msgs or "release" in msgs:
        return {"category": "bad_deploy", "confidence": 0.8,
                "root_cause": "A recent release (v2.4.1) introduced a code regression causing 5xx errors.",
                "evidence": ["Repeated NullPointerError in logs after deployment", "No database anomalies"]}
    return {"category": "unknown", "confidence": 0.3, "root_cause": "Cause not determined from evidence.",
            "evidence": []}


async def rca(state: IncidentState):
    iid = state["incident_id"]
    async with mcp_session(iid) as call:
        logs = await call("search_logs", {"service": state["service"], "minutes": 3})
        dbd = await call("db_diagnostics", {})
        words = " ".join(m["message"] for m in logs.get("top_messages", [])) + " " + \
            " ".join(state["triage"].get("hypotheses", []))
        similar = await call("find_similar_incidents", {"keywords": words})
    result, src = await llm.ask_json(
        "You are an SRE doing root-cause analysis. Use ONLY the evidence given. Trust CURRENT evidence (live health metrics and database diagnostics) over older log history, and ignore log lines about events that already ended. A long-running database query together with high p95 latency means db_long_running_query. Return JSON: "
        '{"category":"bad_deploy|db_long_running_query|unknown","root_cause":"...","confidence":0.0-1.0,'
        '"evidence":["..."]}',
        {"triage": state["triage"], "logs": logs, "database": dbd, "similar_past_incidents": similar},
        lambda: _rca_rules(logs, dbd))
    if dbd.get("long_running_queries") and result.get("category") != "db_long_running_query":
        result, src = _rca_rules(logs, dbd), "rules(guardrail: live database evidence overrides AI)"
    set_status(iid, "analyzed")
    return {"logs": logs, "db_diag": dbd, "similar": similar, "rca": result,
            "timeline": [ev("rca", f"{result.get('category')} (confidence {result.get('confidence')}, by {src}): "
                                   f"{result.get('root_cause')}")]}


# ------------------------------------------------------------------ 3. REMEDIATION PLAN
def _plan_rules(category):
    if category == "bad_deploy":
        return {"action": "rollback_deployment", "target": "demo-service", "risk": "low",
                "reason": "Errors started after release v2.4.1; roll back to v2.4.0.",
                "rollback_plan": "Redeploy v2.4.1 if rollback causes issues.",
                "expected_outcome": "Error ratio returns below 5%."}
    if category == "db_long_running_query":
        return {"action": "terminate_blocking_queries", "target": "demo_report_job", "risk": "medium",
                "reason": "Report job query is blocking the database; terminate it.",
                "rollback_plan": "Re-run the report job off-peak.",
                "expected_outcome": "p95 latency returns below 1s."}
    return {"action": "restart_service", "target": "demo-service", "risk": "low",
            "reason": "Cause unknown; restart is the least risky generic step.",
            "rollback_plan": "None needed.", "expected_outcome": "Possible transient recovery."}


async def plan(state: IncidentState):
    iid = state["incident_id"]
    result, src = await llm.ask_json(
        "You are an SRE proposing ONE remediation. You may ONLY choose from this allowlist "
        f"(action -> allowed targets): {ALLOWED_ACTIONS}. Return JSON: "
        '{"action":"...","target":"...","risk":"low|medium|high","reason":"...","rollback_plan":"...",'
        '"expected_outcome":"..."}',
        {"rca": state["rca"], "triage": state["triage"]}, lambda: _plan_rules(state["rca"].get("category")))
    # GUARDRAIL: never trust the model - re-check against the allowlist
    if result.get("target") not in ALLOWED_ACTIONS.get(result.get("action"), []):
        result, src = _plan_rules(state["rca"].get("category")), "rules(guardrail)"
    row = db.fetch_one(
        "INSERT INTO approvals(incident_id, proposed_action, target, risk, rationale) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (iid, result["action"], result["target"], result.get("risk"), result.get("reason")))
    set_status(iid, "awaiting_approval")
    db.audit(iid, "agent", "approval_requested", {"approval_id": row["id"], "plan": result})
    return {"plan": result, "approval_id": row["id"],
            "timeline": [ev("plan", f"Proposed {result['action']} on {result['target']} "
                                    f"(risk {result.get('risk')}, by {src}); waiting for human approval")]}


# ------------------------------------------------------------------ 4. HUMAN APPROVAL CHECKPOINT
async def approval_gate(state: IncidentState):
    # interrupt() freezes the workflow here. It resumes when POST /incidents/{id}/approve is called.
    decision = interrupt({"approval_id": state["approval_id"], "proposed_plan": state["plan"],
                          "root_cause": state["rca"].get("root_cause")})
    verdict = "APPROVED" if decision.get("approved") else "REJECTED"
    return {"approval": decision,
            "timeline": [ev("approval", f"{verdict} by {decision.get('approver')}: {decision.get('comment', '')}")]}


def after_gate(state: IncidentState):
    return "execute" if state["approval"].get("approved") else "report"


# ------------------------------------------------------------------ 5. EXECUTE
async def execute(state: IncidentState):
    iid, p = state["incident_id"], state["plan"]
    async with mcp_session(iid) as call:
        res = await call("remediate", {"action": p["action"], "target": p["target"],
                                       "incident_id": iid, "approval_id": state["approval_id"]})
    set_status(iid, "remediating")
    return {"execution": res, "timeline": [ev("execute", f"executed={res.get('executed')} {res.get('outcome') or res.get('error')}")]}


def after_execute(state: IncidentState):
    return "verify" if state["execution"].get("executed") else "report"


# ------------------------------------------------------------------ 6. POST-REMEDIATION VERIFICATION
async def verify(state: IncidentState):
    iid = state["incident_id"]
    await asyncio.sleep(settings.VERIFY_WAIT_SECONDS)     # let Prometheus collect fresh data
    async with mcp_session(iid) as call:
        health = await call("check_service_health", {"service": state["service"], "window": "30s"})
        dbd = await call("db_diagnostics", {})
    resolved = health.get("status") == "healthy" and not dbd.get("long_running_queries")
    result = {"resolved": resolved, "health_after": health, "db_findings_after": dbd.get("findings")}
    return {"verification": result,
            "timeline": [ev("verify", f"{'RESOLVED' if resolved else 'NOT RESOLVED'}: status={health.get('status')}, "
                                      f"errors={health.get('error_ratio')}, p95={health.get('p95_latency_seconds')}")]}


# ------------------------------------------------------------------ 7. REPORT
async def report(state: IncidentState):
    iid = state["incident_id"]
    if not state["approval"].get("approved"):
        status, text = "escalated", "Human rejected the proposed remediation; escalated for manual handling."
    elif not state.get("execution", {}).get("executed"):
        status, text = "escalated", f"Remediation was blocked: {state['execution'].get('error')}"
    elif state["verification"]["resolved"]:
        status = "resolved"
        text = f"Root cause: {state['rca']['root_cause']} Fixed by {state['plan']['action']}; metrics recovered."
    else:
        status, text = "escalated", "Remediation executed but metrics did not recover; escalated to on-call."
    set_status(iid, status, summary=text)
    return {"status": status, "summary": text, "timeline": [ev("report", f"{status.upper()}: {text}")]}


def build_graph(checkpointer):
    g = StateGraph(IncidentState)
    for name, fn in [("triage", triage), ("rca", rca), ("plan", plan), ("approval_gate", approval_gate),
                     ("execute", execute), ("verify", verify), ("report", report)]:
        g.add_node(name, fn)
    g.add_edge(START, "triage")
    g.add_edge("triage", "rca")
    g.add_edge("rca", "plan")
    g.add_edge("plan", "approval_gate")
    g.add_conditional_edges("approval_gate", after_gate, ["execute", "report"])
    g.add_conditional_edges("execute", after_execute, ["verify", "report"])
    g.add_edge("verify", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)
