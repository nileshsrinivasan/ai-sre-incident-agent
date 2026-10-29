"""MCP server: the ONLY way the agent touches logs, metrics, the database and the service.
Run standalone with:  python -m app.mcp_server   (the API starts it automatically)."""
import math
import re

import httpx
from mcp.server.fastmcp import FastMCP

from . import db
from .config import ALLOWED_ACTIONS, settings

mcp = FastMCP("sre-tools")
SAFE_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


# ---------------------------------------------------------------- 1. LOG SEARCH
@mcp.tool()
def search_logs(service: str, keyword: str = "", level: str = "", minutes: int = 15) -> dict:
    """Search recent application logs. Returns counts by level, most repeated messages, and recent lines."""
    where = "service=%s AND ts > now() - make_interval(mins => %s)"
    params = [service, minutes]
    if keyword:
        where += " AND message ILIKE %s"
        params.append(f"%{keyword}%")
    if level:
        where += " AND level=%s"
        params.append(level.upper())
    return {
        "service": service, "window_minutes": minutes,
        "by_level": db.fetch(f"SELECT level, count(*) AS n FROM app_logs WHERE {where} GROUP BY level", params),
        "top_messages": db.fetch(
            f"SELECT message, level, count(*) AS n FROM app_logs WHERE {where} "
            "GROUP BY message, level ORDER BY n DESC LIMIT 6", params),
        "recent": db.fetch(
            f"SELECT ts, level, message FROM app_logs WHERE {where} ORDER BY ts DESC LIMIT 15", params),
    }


# ---------------------------------------------------------------- 2. SERVICE HEALTH (Prometheus)
def _prom(query: str):
    r = httpx.get(f"{settings.PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    r.raise_for_status()
    result = r.json()["data"]["result"]
    if not result:
        return None
    v = float(result[0]["value"][1])
    return None if math.isnan(v) else v


@mcp.tool()
def check_service_health(service: str, window: str = "1m") -> dict:
    """Query Prometheus for the golden signals: availability, traffic, error ratio, p95 latency."""
    if not SAFE_NAME.match(service) or not re.match(r"^\d+[smh]$", window):
        return {"error": "invalid service or window"}
    sel = f'service="{service}"'
    up = _prom(f'up{{job="{service}"}}')
    rps = _prom(f"sum(rate(http_requests_total{{{sel}}}[{window}]))")
    err = _prom(f'sum(rate(http_requests_total{{{sel},status=~"5.."}}[{window}])) '
                f"/ sum(rate(http_requests_total{{{sel}}}[{window}]))")
    p95 = _prom("histogram_quantile(0.95, sum(rate("
                f"http_request_duration_seconds_bucket{{{sel}}}[{window}])) by (le))")
    if up == 0:
        status = "down"
    elif err is None and p95 is None:
        status = "unknown"
    elif (err or 0) > 0.05 or (p95 or 0) > 1.0:
        status = "degraded"
    else:
        status = "healthy"
    return {"service": service, "window": window, "status": status, "up": up,
            "requests_per_sec": rps, "error_ratio": err, "p95_latency_seconds": p95,
            "thresholds": {"error_ratio": 0.05, "p95_latency_seconds": 1.0}}


# ---------------------------------------------------------------- 3. DATABASE DIAGNOSTICS
@mcp.tool()
def db_diagnostics() -> dict:
    """Postgres health: connection saturation, long-running queries (>10s), lock waits, size."""
    conns = db.fetch_one("SELECT count(*) AS total, count(*) FILTER (WHERE state='active') AS active "
                         "FROM pg_stat_activity WHERE datname=current_database()")
    max_conn = int(db.fetch_one("SHOW max_connections")["max_connections"])
    long_q = db.fetch(
        "SELECT pid, application_name, state, extract(epoch from now()-query_start)::int AS seconds, "
        "left(query,100) AS query FROM pg_stat_activity "
        "WHERE datname=current_database() AND state='active' AND pid<>pg_backend_pid() "
        "AND now()-query_start > interval '10 seconds' ORDER BY query_start")
    waiting = db.fetch_one("SELECT count(*) AS n FROM pg_stat_activity "
                           "WHERE wait_event_type='Lock' AND datname=current_database()")["n"]
    size = db.fetch_one("SELECT pg_size_pretty(pg_database_size(current_database())) AS s")["s"]
    util = round(100 * conns["total"] / max_conn, 1)
    findings = []
    if long_q:
        findings.append(f"{len(long_q)} query(ies) running >10s, e.g. app '{long_q[0]['application_name']}'")
    if util > 80:
        findings.append(f"connection saturation {util}%")
    if waiting:
        findings.append(f"{waiting} session(s) waiting on locks")
    return {"connections": conns, "max_connections": max_conn, "utilization_pct": util,
            "long_running_queries": long_q, "lock_waiters": waiting, "db_size": size,
            "findings": findings or ["no database anomalies"]}


# ---------------------------------------------------------------- 4. INCIDENT RETRIEVAL
@mcp.tool()
def get_incident(incident_id: int) -> dict:
    """Fetch the current incident record."""
    return db.fetch_one("SELECT * FROM incidents WHERE id=%s", (incident_id,)) or {"error": "not found"}


@mcp.tool()
def find_similar_incidents(keywords: str) -> dict:
    """Search the past-incident knowledge base by keywords (space separated)."""
    words = list(dict.fromkeys(w for w in re.split(r"\W+", keywords) if len(w) > 3))[:8]
    if not words:
        return {"matches": []}
    rows = db.fetch("SELECT title, symptoms, root_cause, resolution FROM past_incidents "
                    "WHERE symptoms ILIKE ANY(%s) OR title ILIKE ANY(%s) LIMIT 3",
                    ([f"%{w}%" for w in words], [f"%{w}%" for w in words]))
    return {"matches": rows}


# ---------------------------------------------------------------- 5. CONTROLLED REMEDIATION
@mcp.tool()
def remediate(action: str, target: str, incident_id: int, approval_id: int) -> dict:
    """Execute ONE allow-listed fix. Refuses unless a human approved this exact action in the database."""
    def deny(reason):
        db.audit(incident_id, "mcp:remediate", "DENIED", {"action": action, "target": target, "reason": reason})
        return {"executed": False, "error": reason}

    if action not in ALLOWED_ACTIONS or target not in ALLOWED_ACTIONS[action]:
        return deny("action/target not in allowlist")
    ap = db.fetch_one("SELECT * FROM approvals WHERE id=%s AND incident_id=%s", (approval_id, incident_id))
    if not ap or ap["status"] != "approved":
        return deny("no human approval on record (status=%s)" % (ap["status"] if ap else "missing"))
    if ap["proposed_action"] != action or ap["target"] != target:
        return deny("approved action does not match requested action")

    if action in ("restart_service", "rollback_deployment"):
        path = "restart" if action == "restart_service" else "rollback"
        r = httpx.post(f"{settings.DEMO_SERVICE_URL}/admin/{path}", timeout=15)
        outcome = {"http_status": r.status_code, "service_state": r.json()}
    else:  # terminate_blocking_queries
        rows = db.fetch("SELECT pg_terminate_backend(pid) AS ok FROM pg_stat_activity "
                        "WHERE application_name=%s AND pid<>pg_backend_pid()", (target,))
        outcome = {"sessions_terminated": len(rows)}

    db.run("UPDATE approvals SET status='executed' WHERE id=%s", (approval_id,))
    db.audit(incident_id, "mcp:remediate", "EXECUTED",
             {"action": action, "target": target, "approval_id": approval_id, "outcome": outcome})
    return {"executed": True, "action": action, "target": target, "outcome": outcome}


if __name__ == "__main__":
    mcp.run()   # stdio transport
